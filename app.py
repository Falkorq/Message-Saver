"""Личный журнал правок и удалений для Telegram Business аккаунтов."""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from telebot import types
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_helper import ApiTelegramException
from telebot.util import content_type_media, content_type_service

from access_control import AccessManager
from business_notifications import chat_identity
from business_tools import BusinessToolsModule
from profile_monitor import ProfileMonitorModule


load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def required_int(name):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError) as exc:
        raise SystemExit(f"Укажите числовое значение {name} в .env") from exc


TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN or TOKEN == "123456:replace_me":
    raise SystemExit("Укажите BOT_TOKEN в .env")

my_chat_id = required_int("MY_CHAT_ID")
try:
    my_account_ids = [int(item.strip()) for item in os.environ["MY_ACCOUNT_IDS"].split(",") if item.strip()]
except (KeyError, ValueError) as exc:
    raise SystemExit("Укажите числовые ID в MY_ACCOUNT_IDS (через запятую)") from exc
if not my_account_ids or len(set(my_account_ids)) != len(my_account_ids):
    raise SystemExit("MY_ACCOUNT_IDS должен содержать хотя бы один уникальный ID")

second_chat_id = None
second_account_ids = []
if os.getenv("SECOND_ACCOUNT_IDS"):
    if os.getenv("SECOND_CHAT_ID") is None:
        raise SystemExit("Укажите SECOND_CHAT_ID вместе с SECOND_ACCOUNT_IDS")
    second_chat_id = required_int("SECOND_CHAT_ID")
    try:
        second_account_ids = [int(item.strip()) for item in os.environ["SECOND_ACCOUNT_IDS"].split(",") if item.strip()]
    except ValueError as exc:
        raise SystemExit("SECOND_ACCOUNT_IDS должен содержать числовые ID через запятую") from exc
    if not second_account_ids or len(set(second_account_ids)) != len(second_account_ids):
        raise SystemExit("SECOND_ACCOUNT_IDS должен содержать хотя бы один уникальный ID")
    if second_chat_id == my_chat_id:
        raise SystemExit("MY_CHAT_ID и SECOND_CHAT_ID должны быть разными, чтобы уведомления не смешивались")
    overlap = set(second_account_ids) & set(my_account_ids)
    if overlap:
        raise SystemExit(f"SECOND_ACCOUNT_IDS не должен пересекаться с MY_ACCOUNT_IDS: {sorted(overlap)}")

try:
    retention_days = int(os.getenv("RETENTION_DAYS", "90"))
except ValueError as exc:
    raise SystemExit("RETENTION_DAYS должен быть числом") from exc
if retention_days < 1:
    raise SystemExit("RETENTION_DAYS должен быть положительным")


def parse_account_labels(all_account_ids):
    """ACCOUNT_LABELS вида '111111:Home,222222:Work' — необязательные подписи аккаунтов."""
    labels = {}
    for chunk in os.getenv("ACCOUNT_LABELS", "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        account, _, label = chunk.partition(":")
        try:
            labels[int(account.strip())] = label.strip()
        except ValueError as exc:
            raise SystemExit("ACCOUNT_LABELS должен иметь вид 'ID:подпись,ID:подпись'") from exc
    return {account_id: labels.get(account_id, f"Account {str(account_id)[-4:]}") for account_id in all_account_ids}


routes = {account_id: my_chat_id for account_id in my_account_ids}
routes.update({account_id: second_chat_id for account_id in second_account_ids})
account_labels = parse_account_labels(my_account_ids + second_account_ids)
admin_ids = set(my_account_ids)
db_path = Path(os.getenv("DB_PATH", "messages.sqlite3"))
db_path.parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row
from business_tools.backup import backup_database
if not db.execute("SELECT 1 FROM sqlite_master WHERE name='business_stats_progress'").fetchone():
    backup_database(db, "before-update")
db.execute("PRAGMA journal_mode=WAL")
db.executescript("""
CREATE TABLE IF NOT EXISTS connections (
    connection_id TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    recipient_id INTEGER NOT NULL,
    enabled INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    connection_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (connection_id, chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS messages_created_at ON messages(created_at);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_at INTEGER NOT NULL DEFAULT 0,
    media_sent INTEGER NOT NULL DEFAULT 0,
    text_parts_sent INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    recipient_id INTEGER PRIMARY KEY,
    only_others INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS style_emojis (
    recipient_id INTEGER NOT NULL,
    event TEXT NOT NULL,
    emoji_id TEXT NOT NULL,
    alt TEXT NOT NULL,
    PRIMARY KEY (recipient_id, event)
);
""")
outbox_columns = {row[1] for row in db.execute("PRAGMA table_info(outbox)")}
for column, definition in {
    "media_sent": "INTEGER NOT NULL DEFAULT 0",
    "text_parts_sent": "INTEGER NOT NULL DEFAULT 0",
    "failed": "INTEGER NOT NULL DEFAULT 0",
    "last_error": "TEXT",
}.items():
    if column not in outbox_columns:
        db.execute(f"ALTER TABLE outbox ADD COLUMN {column} {definition}")
if db.execute("PRAGMA user_version").fetchone()[0] < 1:
    # Новое оформление заменяет сохранённые ранее значки заголовков один раз.
    db.execute("DELETE FROM style_emojis WHERE event IN ('delete', 'edit')")
    db.execute("PRAGMA user_version=1")
db.commit()
access = AccessManager(db, routes, account_labels, set(routes))
bot = AsyncTeleBot(TOKEN)


def notification_target(payload):
    if payload.get("event") in ("profile_change", "text_notification"):
        owner_id = payload.get("owner_id")
        return owner_id if owner_id in routes.values() else None
    return routes.get(payload.get("account_id"))


def refresh_saved_routes():
    """Apply the current allowlist to connections and queued notifications."""
    for row in db.execute("SELECT connection_id, account_id FROM connections").fetchall():
        recipient_id = routes.get(row["account_id"])
        db.execute(
            "UPDATE connections SET recipient_id=? WHERE connection_id=?",
            (recipient_id or 0, row["connection_id"]),
        )
    for row in db.execute("SELECT id, data FROM outbox").fetchall():
        try:
            payload = json.loads(row["data"])
        except (ValueError, KeyError, TypeError):
            payload = {}
        recipient_id = notification_target(payload)
        if recipient_id is None:
            db.execute("UPDATE outbox SET failed=1, last_error=? WHERE id=?", ("Аккаунт удалён из настроек", row["id"]))
        else:
            db.execute("UPDATE outbox SET recipient_id=? WHERE id=?", (recipient_id, row["id"]))
    db.commit()


refresh_saved_routes()


def message_key(msg):
    return (msg.business_connection_id, msg.chat.id, msg.message_id)


def record_from_message(msg):
    kind = msg.content_type or (
        "paid_media" if getattr(msg, "paid_media", None) else
        "checklist" if getattr(msg, "checklist", None) else "unknown"
    )
    media = getattr(msg, kind, None)
    if kind == "photo":
        media = media[-1] if media else None
    chat = msg.chat
    chat_name = getattr(chat, "title", None) or " ".join(
        part for part in (getattr(chat, "first_name", None), getattr(chat, "last_name", None)) if part
    )
    chat_name = chat_name or getattr(chat, "username", None) or str(chat.id)
    return {
        "name": (msg.from_user.first_name if msg.from_user else "Пользователь"),
        "sender_id": (msg.from_user.id if msg.from_user else None),
        "username": (getattr(msg.from_user, "username", None) if msg.from_user else None),
        "type": kind,
        "text": msg.text if kind == "text" else None,
        "caption": msg.caption if kind != "text" else None,
        "details": message_details(msg),
        "file_id": getattr(media, "file_id", None),
        "file_unique_id": getattr(media, "file_unique_id", None),
        "chat_id": chat.id,
        "chat_name": chat_name,
        "chat_username": getattr(chat, "username", None),
        "sent_at": getattr(msg, "date", None),
        "edit_at": getattr(msg, "edit_date", None),
        "metadata": {"duration": getattr(media, "duration", None)},
    }


def message_details(msg):
    kind = msg.content_type
    if kind == "contact" and msg.contact:
        contact = msg.contact
        name = " ".join(part for part in (contact.first_name, contact.last_name) if part)
        return f"{name}\nТелефон: {contact.phone_number}"
    if kind in ("location", "venue"):
        location = msg.location if kind == "location" else getattr(msg.venue, "location", None)
        if location:
            coordinates = f"{location.latitude}, {location.longitude}"
            if kind == "venue" and msg.venue:
                return f"{msg.venue.title}\n{msg.venue.address}\n{coordinates}"
            return coordinates
    if kind == "poll" and msg.poll:
        options = "\n".join(f"• {option.text}" for option in msg.poll.options)
        return f"{msg.poll.question}\n{options}"
    if kind == "dice" and msg.dice:
        return f"{msg.dice.emoji}: {msg.dice.value}"
    if getattr(msg, "checklist", None):
        checklist = msg.checklist
        return getattr(checklist, "title", None)
    if getattr(msg, "paid_media", None):
        return "Платное медиа (содержимое может быть недоступно боту)"
    return None


def only_others_enabled(recipient_id):
    row = db.execute("SELECT only_others FROM settings WHERE recipient_id=?", (recipient_id,)).fetchone()
    return bool(row["only_others"]) if row else False


def is_own_message(connection_id, record):
    return record.get("sender_id") == account_for(connection_id)


def custom_emoji_from_message(msg):
    for source in (msg, getattr(msg, "reply_to_message", None)):
        if source is None:
            continue
        value = getattr(source, "text", None) or getattr(source, "caption", None) or ""
        entities = getattr(source, "entities", None) or getattr(source, "caption_entities", None) or []
        encoded = value.encode("utf-16-le")
        for entity in entities:
            emoji_id = str(getattr(entity, "custom_emoji_id", "") or "")
            if entity.type != "custom_emoji" or not emoji_id.isdigit():
                continue
            start = entity.offset * 2
            end = start + entity.length * 2
            try:
                alt = encoded[start:end].decode("utf-16-le")
            except UnicodeDecodeError:
                continue
            if alt and len(alt) <= 16:
                return emoji_id, alt
    return None


def saved_emoji(recipient_id, event):
    return db.execute(
        "SELECT emoji_id, alt FROM style_emojis WHERE recipient_id=? AND event=?",
        (recipient_id, event),
    ).fetchone()


def save_message(key, record):
    db.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(connection_id, chat_id, message_id) DO UPDATE SET data=excluded.data",
        (*key, json.dumps(record, ensure_ascii=False), int(time.time())),
    )
    db.commit()


def load_message(key):
    row = db.execute(
        "SELECT data FROM messages WHERE connection_id=? AND chat_id=? AND message_id=?", key
    ).fetchone()
    return json.loads(row["data"]) if row else None


def enqueue(recipient_id, payload):
    db.execute(
        "INSERT INTO outbox(recipient_id, data, created_at) VALUES (?, ?, ?)",
        (recipient_id, json.dumps(payload, ensure_ascii=False), int(time.time())),
    )


async def recipient_for(connection_id):
    row = db.execute(
        "SELECT account_id, enabled FROM connections WHERE connection_id=?", (connection_id,)
    ).fetchone()
    if row:
        return routes.get(row["account_id"]) if row["enabled"] else None
    for attempt in range(3):
        try:
            connection = await bot.get_business_connection(connection_id)
            break
        except Exception:
            log.exception("Не удалось получить бизнес-подключение %s (попытка %s)", connection_id, attempt + 1)
            if attempt == 2:
                return None
            await asyncio.sleep(attempt + 1)
    await store_connection(connection)
    return routes.get(connection.user.id) if connection.is_enabled else None


async def store_connection(connection):
    account_id = connection.user.id
    recipient_id = routes.get(account_id)
    known = db.execute("SELECT enabled FROM connections WHERE connection_id=?", (connection.id,)).fetchone()
    if recipient_id is None and not known:
        log.warning("Игнорирую неизвестный аккаунт %s; выдайте доступ через /grant %s", account_id, account_id)
    db.execute(
        "INSERT INTO connections VALUES (?, ?, ?, ?) "
        "ON CONFLICT(connection_id) DO UPDATE SET account_id=excluded.account_id, "
        "recipient_id=excluded.recipient_id, enabled=excluded.enabled",
        (connection.id, account_id, recipient_id or 0, int(connection.is_enabled)),
    )
    db.commit()
    if recipient_id is not None:
        if not known or known['enabled'] != int(connection.is_enabled):
            business_tools.repo.reset_continuity(account_id)
        log.info("Подключение %s: аккаунт %s, активно=%s", connection.id, account_id, connection.is_enabled)


@bot.business_connection_handler()
async def on_connection(connection):
    await store_connection(connection)


@bot.message_handler(commands=["start", "id"])
async def on_command(msg):
    if msg.chat.type == "private":
        reply = f"Ваш Telegram ID: {msg.from_user.id}"
        if msg.from_user.id in routes:
            reply += ("\nКоманды: /menu, /watch, /watchlist, /mute, /unmute, /mutelist, "
                      "/chatstats, /status, /retry_failed, /only_others on|off, /style")
        if msg.from_user.id in admin_ids:
            reply += "\nДоступ: /grant, /revoke, /access"
        await bot.send_message(msg.chat.id, reply)


@bot.message_handler(commands=["grant", "revoke", "access"])
async def on_access(msg):
    if msg.chat.type != "private" or msg.chat.id != msg.from_user.id or msg.from_user.id not in admin_ids:
        return
    command, *args = (msg.text or "").split(maxsplit=2)
    command = command.split("@")[0]
    if command == "/access":
        rows = access.list()
        lines = [f"• {row['label']} ({row['user_id']})" for row in rows]
        await bot.send_message(msg.chat.id, "Доступ выдан:\n" + ("\n".join(lines) if lines else "никому"))
        return
    if not args:
        usage = "/grant user_id [имя]" if command == "/grant" else "/revoke user_id"
        await bot.send_message(msg.chat.id, f"Использование: {usage}")
        return
    try:
        user_id = int(args[0])
        if command == "/grant":
            access.grant(user_id, args[1] if len(args) > 1 else None)
            refresh_saved_routes()
            await bot.send_message(
                msg.chat.id,
                f"Доступ для {user_id} выдан. Другу нужно отправить боту /start, "
                "затем подключить его в Chat Automation. Уведомления придут в его личный чат.",
            )
        else:
            removed = access.revoke(user_id)
            if removed:
                refresh_saved_routes()
            await bot.send_message(msg.chat.id, "Доступ отозван" if removed else "ID не найден в списке")
    except ValueError as exc:
        await bot.send_message(msg.chat.id, str(exc))


@bot.message_handler(commands=["status", "retry_failed", "only_others", "style", "setemoji"])
async def on_control(msg):
    if msg.chat.type != "private" or msg.chat.id != msg.from_user.id:
        return
    recipient_id = routes.get(msg.from_user.id)
    if recipient_id is None:
        return
    command, *args = (msg.text or "").split()
    command = command.split("@")[0]
    if command == "/retry_failed":
        retried = 0
        for row in db.execute(
            "SELECT id, data FROM outbox WHERE recipient_id=? AND failed=1", (recipient_id,)
        ).fetchall():
            try:
                payload = json.loads(row["data"])
            except (ValueError, KeyError, TypeError):
                continue
            target = (payload.get("owner_id") if payload.get("event") in ("profile_change", "text_notification")
                      else routes.get(payload.get("account_id")))
            if target != recipient_id:
                continue
            db.execute(
                "UPDATE outbox SET failed=0, attempts=0, retry_at=0, last_error=NULL WHERE id=?",
                (row["id"],),
            )
            retried += 1
        db.commit()
        await bot.send_message(msg.chat.id, f"Повторно поставлено в очередь: {retried}")
        return
    if command == "/setemoji":
        if not args or args[0].lower() not in ("delete", "edit"):
            await bot.send_message(msg.chat.id, "Использование: /setemoji delete 🗑 или /setemoji edit ✏️. Можно ответить командой на сообщение с премиум-эмодзи.")
            return
        found = custom_emoji_from_message(msg)
        if found is None:
            await bot.send_message(msg.chat.id, "Не вижу премиум-эмодзи. Добавь его к команде или ответь командой на сообщение с ним.")
            return
        emoji_id, alt = found
        db.execute(
            "INSERT INTO style_emojis VALUES (?, ?, ?, ?) "
            "ON CONFLICT(recipient_id, event) DO UPDATE SET emoji_id=excluded.emoji_id, alt=excluded.alt",
            (recipient_id, args[0].lower(), emoji_id, alt),
        )
        db.commit()
        await bot.send_message(msg.chat.id, "Эмодзи сохранён. Проверить оформление: /style")
        return
    if command == "/style":
        await bot.send_message(
            msg.chat.id,
            "Отправь /setemoji delete с премиум-эмодзи для удалений и /setemoji edit с другим эмодзи для правок. "
            "Можно ответить командой на сообщение, содержащее нужный эмодзи. Оформление задаётся отдельно для каждого получателя.\n\nПример:",
        )
        example = {"name": "Пример", "sender_id": None, "username": None, "sent_at": int(time.time())}
        example_account_id = next(iter(routes))
        example.update({"chat_id": 123456789, "chat_name": "Пример чата"})
        await send_rich_text(msg.chat.id, "delete", "Удалено сообщение", example_account_id, example, "Текст удалённого сообщения")
        await send_rich_text(msg.chat.id, "edit", "Изменено сообщение", example_account_id, example, "старый текст", "новый текст")
        return
    if command == "/only_others":
        if len(args) != 1 or args[0].lower() not in ("on", "off"):
            await bot.send_message(msg.chat.id, "Использование: /only_others on или /only_others off")
            return
        enabled = args[0].lower() == "on"
        db.execute(
            "INSERT INTO settings(recipient_id, only_others) VALUES (?, ?) "
            "ON CONFLICT(recipient_id) DO UPDATE SET only_others=excluded.only_others",
            (recipient_id, int(enabled)),
        )
        db.commit()
        await bot.send_message(
            msg.chat.id,
            "Уведомления о своих сообщениях выключены" if enabled else "Уведомления о своих сообщениях включены",
        )
        return
    account_lines = []
    for account_id, destination in routes.items():
        if destination != recipient_id:
            continue
        rows = db.execute(
            "SELECT enabled FROM connections WHERE account_id=? ORDER BY enabled DESC", (account_id,)
        ).fetchall()
        state = "активно" if any(row["enabled"] for row in rows) else "отключено" if rows else "ещё не обнаружено"
        account_lines.append(f"• {account_labels[account_id]} ({account_id}): {state}")
    queue_count = db.execute(
        "SELECT COUNT(*) FROM outbox WHERE recipient_id=? AND failed=0", (recipient_id,)
    ).fetchone()[0]
    failed_count = db.execute(
        "SELECT COUNT(*) FROM outbox WHERE recipient_id=? AND failed=1", (recipient_id,)
    ).fetchone()[0]
    db_files = [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]
    db_size = sum(path.stat().st_size for path in db_files if path.exists())
    mode = "только чужие" if only_others_enabled(recipient_id) else "все сообщения"
    await bot.send_message(
        msg.chat.id,
        "Состояние бота:\n" + "\n".join(account_lines)
        + f"\nФильтр: {mode}\nОчередь уведомлений: {queue_count}"
        + f"\nТребуют внимания: {failed_count}\nБаза: {db_size / 1024 / 1024:.2f} МБ",
    )


def enqueue_html_notification(recipient_id, text):
    enqueue(recipient_id, {
        "event": "text_notification", "owner_id": recipient_id,
        "text": text, "parse_mode": "HTML",
    })
    db.commit()


profile_monitor = ProfileMonitorModule(
    bot, db, routes, os.getenv("PROFILE_DATA_DIR", "profile_data"), enqueue_html_notification,
    os.getenv("UI_BANNER_PATH", "banner.jpg"),
)
business_tools = BusinessToolsModule(
    bot, db, routes, account_labels, os.getenv("PROFILE_DATA_DIR", "profile_data"),
    enqueue_html_notification, os.getenv("UI_BANNER_PATH", "banner.jpg"),
)


CONTENT_TYPES = list(dict.fromkeys(
    content_type_media + content_type_service + ["paid_media", "checklist", "live_photo", "rich_message", None]
))


@bot.business_message_handler(content_types=CONTENT_TYPES)
async def on_message(msg):
    recipient_id = await recipient_for(msg.business_connection_id)
    if recipient_id is None:
        return
    record = record_from_message(msg)
    tool_result = await business_tools.handle_business_message(
        msg, record, account_for(msg.business_connection_id), recipient_id
    )
    if tool_result in ("command", "muted"):
        return
    if only_others_enabled(recipient_id) and is_own_message(msg.business_connection_id, record):
        return
    save_message(message_key(msg), record)


@bot.edited_business_message_handler(content_types=CONTENT_TYPES)
async def on_edit(msg):
    recipient_id = await recipient_for(msg.business_connection_id)
    if recipient_id is None:
        return
    key = message_key(msg)
    old = load_message(key)
    new = record_from_message(msg)
    business_tools.service.record_edit(recipient_id,account_for(msg.business_connection_id),key,old,new)
    if old and not old.get("sent_at") and new.get("sent_at"):
        old["sent_at"] = new["sent_at"]
    if old and not old.get("username") and new.get("username"):
        old["username"] = new["username"]
    if only_others_enabled(recipient_id) and is_own_message(msg.business_connection_id, new):
        db.execute("DELETE FROM messages WHERE connection_id=? AND chat_id=? AND message_id=?", key)
        db.commit()
        return
    if old and old != new:
        enqueue(recipient_id, {"event": "edit", "old": old, "new": new, "account_id": account_for(msg.business_connection_id)})
    save_message(key, new)
    db.commit()


def account_for(connection_id):
    row = db.execute("SELECT account_id FROM connections WHERE connection_id=?", (connection_id,)).fetchone()
    return row["account_id"] if row else "?"


@bot.deleted_business_messages_handler()
async def on_delete(event):
    recipient_id = await recipient_for(event.business_connection_id)
    if recipient_id is None:
        return
    for message_id in event.message_ids:
        key = (event.business_connection_id, event.chat.id, message_id)
        record = load_message(key)
        self_deleted = business_tools.consume_self_deleted(*key)
        business_tools.service.record_deletion(recipient_id,account_for(event.business_connection_id),key,record,self_deleted)
        if self_deleted:
            db.execute(
                "DELETE FROM messages WHERE connection_id=? AND chat_id=? AND message_id=?", key
            )
            db.commit()
            continue
        if record is None:
            continue
        record.setdefault("chat_id", event.chat.id)
        if not record.get("chat_name"):
            chat = event.chat
            record["chat_name"] = (getattr(chat, "title", None) or " ".join(
                value for value in (getattr(chat, "first_name", None), getattr(chat, "last_name", None)) if value
            ) or getattr(chat, "username", None) or str(chat.id))
        if not record.get("chat_username"):
            record["chat_username"] = getattr(event.chat, "username", None)
        if not record.get("username") and record.get("sender_id") == event.chat.id:
            record["username"] = getattr(event.chat, "username", None)
        with db:
            if not (only_others_enabled(recipient_id) and is_own_message(event.business_connection_id, record)):
                enqueue(recipient_id, {"event": "delete", "old": record, "account_id": account_for(event.business_connection_id)})
            db.execute(
                "DELETE FROM messages WHERE connection_id=? AND chat_id=? AND message_id=?", key
            )


def describe(record):
    if record["type"] == "text":
        return record.get("text") or "(пустой текст)"
    result = record["type"]
    if record.get("details"):
        result += "\n" + record["details"]
    if record.get("caption"):
        result += "\nПодпись: " + record["caption"]
    return result


def sent_time(record):
    stamp = record.get("sent_at")
    if stamp is None:
        return "неизвестно"
    moscow = timezone(timedelta(hours=3))
    return datetime.fromtimestamp(stamp, moscow).strftime("%d.%m.%Y %H:%M МСК")


def valid_username(record):
    username = record.get("username") or ""
    return username if re.fullmatch(r"[A-Za-z0-9_]{1,32}", username) else None


def emoji_html(emoji_id, alt):
    return f'<tg-emoji emoji-id="{emoji_id}">{escape(alt)}</tg-emoji>'


DECORATIONS = {
    "delete": ("5879896690210639947", "🗑"),
    "edit": ("5879841310902324730", "✏️"),
    "sender": ("5879770735999717115", "👤"),
    "account": ("5954175920506933873", "👤"),
    "time": ("5778202206922608769", "🔄"),
    "before": ("5854776233950188167", "🏷"),
    "after": ("5886763041541853781", "🏷"),
    "body": ("5879937509579820068", "🗑"),
    "chat": ("5891169510483823323", "💬"),
}


async def send_text(recipient_id, value):
    # 1800 символов оставляют запас для эмодзи, считающихся двумя UTF-16 единицами.
    for pos in range(0, len(value), 1800):
        await bot.send_message(recipient_id, value[pos:pos + 1800])


async def send_rich_text(
    recipient_id, event, title, account_id, record, body, after=None,
    start_part=0, mark_part=None,
):
    chosen = saved_emoji(recipient_id, event)
    if chosen:
        heading_emoji = emoji_html(chosen["emoji_id"], chosen["alt"])
        heading_alt = chosen["alt"]
    else:
        heading_emoji = emoji_html(*DECORATIONS[event])
        heading_alt = DECORATIONS[event][1]
    name = record.get("name") or "Пользователь"
    username = valid_username(record)
    if username:
        sender_html = f'<a href="https://t.me/{username}">{escape(name)}</a> <code>@{username}</code>'
        sender_plain = f"{name} (@{username})"
    elif isinstance(record.get("sender_id"), int) and record["sender_id"] > 0:
        sender_id = record["sender_id"]
        sender_html = f'<a href="tg://user?id={sender_id}">{escape(name)}</a> <code>ID {sender_id}</code>'
        sender_plain = f"{name} (ID {sender_id})"
    else:
        sender_html = escape(name)
        sender_plain = name
    chat_html, chat_plain = chat_identity(record)
    account = account_labels.get(account_id, str(account_id))
    when = sent_time(record)
    if event == "edit":
        before_first = body[:700]
        after_text = after or ""
        after_first = after_text[:700]
        body_rich = (
            f"{emoji_html(*DECORATIONS['before'])} <b>Было:</b>\n{escape(before_first)}\n\n"
            f"{emoji_html(*DECORATIONS['after'])} <b>Стало:</b>\n{escape(after_first)}"
        )
        body_plain = f"🏷 Было:\n{before_first}\n\n🏷 Стало:\n{after_first}"
    else:
        first = body[:1500]
        body_rich = f"{emoji_html(*DECORATIONS['body'])} {escape(first)}"
        body_plain = f"🗑 {first}"
    rich = (
        f"{heading_emoji} <b>{escape(title)}</b>\n"
        f"{emoji_html(*DECORATIONS['sender'])} <b>Автор:</b> {sender_html}\n"
        f"{emoji_html(*DECORATIONS['chat'])} <b>Чат:</b> {chat_html}\n"
        f"{emoji_html(*DECORATIONS['account'])} <b>Аккаунт:</b> {escape(account)}\n"
        f"{emoji_html(*DECORATIONS['time'])} <b>Отправлено:</b> {escape(when)}\n\n"
        f"{body_rich}"
    )
    if start_part == 0:
        try:
            await bot.send_message(recipient_id, rich, parse_mode="HTML")
        except ApiTelegramException as exc:
            # Если Telegram отверг кастомные эмодзи, доставим обычный текст.
            if exc.error_code != 400:
                raise
            plain = (
                f"{heading_alt} {title}\n👤 Автор: {sender_plain}\n💬 Чат: {chat_plain}\n"
                f"👤 Аккаунт: {account}\n"
                f"🔄 Отправлено: {when}\n\n{body_plain}"
            )
            await bot.send_message(recipient_id, plain)
        if mark_part:
            mark_part(1)
    continuations = []
    if event == "edit":
        if len(body) > 700:
            continuations.extend(
                "Продолжение «Было»:\n" + body[pos:pos + 1600]
                for pos in range(700, len(body), 1600)
            )
        if len(after_text) > 700:
            continuations.extend(
                "Продолжение «Стало»:\n" + after_text[pos:pos + 1600]
                for pos in range(700, len(after_text), 1600)
            )
    elif len(body) > 1500:
        continuations.extend(body[pos:pos + 1800] for pos in range(1500, len(body), 1800))
    for index, continuation in enumerate(continuations, start=1):
        if index < start_part:
            continue
        await bot.send_message(recipient_id, continuation)
        if mark_part:
            mark_part(index + 1)


async def send_media(recipient_id, record):
    file_id = record.get("file_id")
    if not file_id:
        return
    methods = {
        "photo": bot.send_photo,
        "voice": bot.send_voice,
        "video": bot.send_video,
        "video_note": bot.send_video_note,
        "animation": bot.send_animation,
        "sticker": bot.send_sticker,
        "document": bot.send_document,
        "audio": bot.send_audio,
    }
    method = methods.get(record["type"])
    if method:
        await method(recipient_id, file_id)


async def send_notification(
    recipient_id, payload, media_sent=False, mark_media_sent=None,
    text_parts_sent=0, mark_text_part=None,
):
    if payload["event"] in ("profile_change", "text_notification"):
        if text_parts_sent == 0:
            await bot.send_message(recipient_id, payload["text"], parse_mode=payload.get("parse_mode"))
            if mark_text_part:
                mark_text_part(1)
        return
    old = payload["old"]
    account_id = payload["account_id"]
    if payload["event"] == "delete":
        if not media_sent:
            try:
                await send_media(recipient_id, old)
            except ApiTelegramException as exc:
                if exc.error_code != 400:
                    raise
                log.warning("Не удалось переслать удалённое медиа: %s", exc)
            if mark_media_sent:
                mark_media_sent()
        await send_rich_text(
            recipient_id, "delete", "Удалено сообщение", account_id, old, describe(old),
            start_part=text_parts_sent, mark_part=mark_text_part,
        )
        return
    new = payload["new"]
    if not media_sent:
        if old.get("file_unique_id") and old.get("file_unique_id") != new.get("file_unique_id"):
            try:
                await send_media(recipient_id, old)
            except ApiTelegramException as exc:
                if exc.error_code != 400:
                    raise
                log.warning("Не удалось переслать исходное медиа после правки: %s", exc)
        if mark_media_sent:
            mark_media_sent()
    await send_rich_text(recipient_id, "edit", "Изменено сообщение", account_id, old,
                         describe(old), describe(new), start_part=text_parts_sent,
                         mark_part=mark_text_part)


async def outbox_worker():
    last_cleanup = 0
    while True:
        if time.time() - last_cleanup > 3600:
            cutoff = int(time.time()) - retention_days * 86400
            recovered = db.execute("SELECT value FROM business_stats_progress WHERE name='backfill_complete'").fetchone()
            if recovered and recovered[0]:
                db.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
            db.commit()
            last_cleanup = time.time()
        row = db.execute(
            "SELECT id, recipient_id, data, attempts, media_sent, text_parts_sent FROM outbox "
            "WHERE failed=0 AND retry_at <= ? ORDER BY id LIMIT 1", (int(time.time()),)
        ).fetchone()
        if row is None:
            await asyncio.sleep(2)
            continue
        try:
            payload = json.loads(row["data"])
            if notification_target(payload) != row["recipient_id"]:
                db.execute(
                    "UPDATE outbox SET failed=1,last_error=? WHERE id=?",
                    ("Доступ к аккаунту отозван", row["id"]),
                )
                db.commit()
                continue
            def mark_media_sent():
                db.execute("UPDATE outbox SET media_sent=1 WHERE id=?", (row["id"],))
                db.commit()

            def mark_text_part(count):
                db.execute("UPDATE outbox SET text_parts_sent=? WHERE id=?", (count, row["id"]))
                db.commit()

            await send_notification(
                row["recipient_id"], payload,
                media_sent=bool(row["media_sent"]), mark_media_sent=mark_media_sent,
                text_parts_sent=row["text_parts_sent"], mark_text_part=mark_text_part,
            )
        except Exception as exc:
            log.exception("Не удалось отправить уведомление %s", row["id"])
            attempts = row["attempts"] + 1
            delay = min(3600, 15 * (2 ** min(attempts - 1, 8)))
            if isinstance(exc, ApiTelegramException):
                if exc.error_code == 429:
                    parameters = (exc.result_json or {}).get("parameters") or {}
                    delay = max(delay, int(parameters.get("retry_after") or 0))
                elif exc.error_code in (400, 401, 403, 404):
                    db.execute(
                        "UPDATE outbox SET attempts=?, failed=1, last_error=? WHERE id=?",
                        (attempts, str(exc)[:300], row["id"]),
                    )
                    db.commit()
                    continue
            db.execute(
                "UPDATE outbox SET attempts=?, retry_at=?, last_error=? WHERE id=?",
                (attempts, int(time.time()) + delay, str(exc)[:300], row["id"]),
            )
            db.commit()
        else:
            db.execute("DELETE FROM outbox WHERE id=?", (row["id"],))
            db.commit()


async def supervise_outbox():
    while True:
        try:
            await outbox_worker()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Очередь уведомлений остановилась; перезапущу через 15 секунд")
            await asyncio.sleep(15)


async def main():
    worker = asyncio.create_task(supervise_outbox())
    recovery = asyncio.create_task(business_tools.recover_history())
    try:
        await profile_monitor.start()
        await bot.set_my_commands([
            types.BotCommand("menu", "Главное меню"),
            types.BotCommand("watch", "Отслеживать профиль"),
            types.BotCommand("watchlist", "Список отслеживаемых"),
            types.BotCommand("mute", "Замутить чат: /mute @username 1h"),
            types.BotCommand("unmute", "Снять мут"),
            types.BotCommand("mutelist", "Активные муты"),
            types.BotCommand("chatstats", "Статистика чата"),
            types.BotCommand("status", "Состояние бота"),
        ])
        log.info("Бот запущен")
        await bot.infinity_polling(
            allowed_updates=["message", "callback_query", "business_connection", "business_message", "edited_business_message", "deleted_business_messages"],
            skip_pending=False,
        )
    finally:
        await profile_monitor.close()
        worker.cancel()
        recovery.cancel()
        await asyncio.gather(worker, recovery, return_exceptions=True)
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
