"""Личный журнал правок и удалений для Telegram Business аккаунтов."""

import asyncio
from contextvars import ContextVar
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
from telebot import asyncio_helper
from reliable import AtomicConnection, DurableInbox, DeliveryBudget, ensure_outbox_indexes
from product_state import ProductState
from erasure_guard import ErasureGuard
from telebot.asyncio_helper import ApiTelegramException
from telebot.util import content_type_media, content_type_service

from access_control import AccessManager
from message_crypto import MessageCipher, EncryptionError
from business_notifications import chat_identity
from business_tools import BusinessToolsModule
from profile_monitor import ProfileMonitorModule
from profile_monitor.access import MTProtoAccess


load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

try:
    message_cipher = MessageCipher.from_env(Path(__file__).with_name('.env'))
except EncryptionError as exc:
    raise SystemExit(str(exc)) from None


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
    return {account_id: labels.get(account_id, f"Аккаунт · {str(account_id)[-4:]}") for account_id in all_account_ids}


routes = {account_id: my_chat_id for account_id in my_account_ids}
routes.update({account_id: second_chat_id for account_id in second_account_ids})
account_labels = parse_account_labels(my_account_ids + second_account_ids)
admin_ids = set(my_account_ids)
db_path = Path(os.getenv("DB_PATH", "messages.sqlite3"))
db_path.parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(db_path, factory=AtomicConnection)
db.row_factory = sqlite3.Row
db.execute("PRAGMA synchronous=FULL")
db.execute("PRAGMA secure_delete=ON")
try:
    os.chmod(db_path, 0o600)
except OSError:
    pass
try:
    message_cipher.validate_database_key(db)
except EncryptionError as exc:
    db.close()
    raise SystemExit(str(exc)) from None
from business_tools.backup import backup_database
if not db.execute("SELECT 1 FROM sqlite_master WHERE name='business_stats_progress'").fetchone():
    backup_database(db, "before-update", cipher=message_cipher)
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
    only_others INTEGER NOT NULL DEFAULT 1
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
ensure_outbox_indexes(db)
if db.execute("PRAGMA user_version").fetchone()[0] < 1:
    # Новое оформление заменяет сохранённые ранее значки заголовков один раз.
    db.execute("DELETE FROM style_emojis WHERE event IN ('delete', 'edit')")
    db.execute("PRAGMA user_version=1")
db.commit()
access = AccessManager(db, routes, account_labels, set(routes))
try:
    mtproto_access = MTProtoAccess(routes, set(my_account_ids) | {my_chat_id},
                                  (os.getenv("MTPROTO_ALLOWED_USER_IDS") or "6640457762,6851862358,803489862"))
except ValueError as exc:
    raise SystemExit(str(exc)) from None
bot = AsyncTeleBot(TOKEN)
inbox = DurableInbox(db, message_cipher)
# Seed explicitly configured friends once; /revoke remains effective across restarts.
db.execute('CREATE TABLE IF NOT EXISTS initial_access_seeds(user_id INTEGER PRIMARY KEY)')
for invited_id in mtproto_access.allowed_ids - mtproto_access.operator_ids:
    if not db.execute('SELECT 1 FROM initial_access_seeds WHERE user_id=?',(invited_id,)).fetchone():
        if invited_id not in routes:
            access.grant(invited_id)
        db.execute('INSERT INTO initial_access_seeds VALUES(?)',(invited_id,))
db.commit()
product = ProductState(db, routes)
product.erasure_guard = ErasureGuard(db_path.with_name('erasure-guard.aes'), message_cipher)
mtproto_access.collection_check = lambda owner: any(product.allows(a) for a,o in routes.items() if o==owner)
delivery_budget = DeliveryBudget(db)
current_delivery = ContextVar('current_delivery', default=None)


def paced_send(method):
    async def send(recipient, *args, **kwargs):
        return await limited_send(method, recipient, *args, **kwargs)
    send._nyan_paced = True
    return send


for method_name in ('send_message','send_photo','send_voice','send_video','send_video_note',
                    'send_animation','send_sticker','send_document','send_audio','send_media_group'):
    setattr(bot, method_name, paced_send(getattr(bot, method_name)))


def paced_edit(method, recipient_position=1):
    async def edit(*args, **kwargs):
        recipient = kwargs.get('chat_id', args[recipient_position] if len(args)>recipient_position else None)
        if recipient is not None:
            await delivery_budget.take(recipient)
        try:
            return await method(*args, **kwargs)
        except ApiTelegramException as exc:
            if recipient is not None and exc.error_code == 429:
                delay = int((exc.result_json or {}).get('parameters',{}).get('retry_after',1))
                delivery_budget.postpone(recipient, max(1,delay))
            raise
    return edit


for method_name in ('edit_message_media','edit_message_caption','edit_message_text','edit_message_reply_markup'):
    setattr(bot, method_name, paced_edit(getattr(bot, method_name), 0 if method_name=='edit_message_reply_markup' else 1))


def notification_target(payload):
    if payload.get('event') not in ('edit','delete','profile_change','text_notification'):
        return None
    if payload.get('event')=='text_notification' and payload.get('source')!='business_tools':
        return None
    if not product.notifications(payload.get("owner_id") or routes.get(payload.get("account_id")), payload.get("event"), ((payload.get("old") or payload.get("new") or {}).get("sender_id")==payload.get("account_id")) if payload.get("account_id") is not None else None):
        return None
    if payload.get('account_id') is not None and only_others_enabled(routes.get(payload['account_id'])) and (payload.get('old') or payload.get('new') or {}).get('sender_id') == payload['account_id']:
        return None
    if payload.get("account_id") is not None and not product.allows(payload["account_id"]):
        return None
    if payload.get("event") == "profile_change" and not mtproto_access.owner(payload.get("owner_id")):
        return None
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
            db.execute("UPDATE outbox SET failed=1, last_error=? WHERE id=?", ("Доступ или источник уведомления не подтверждён", row["id"]))
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
    return bool(row["only_others"]) if row else True


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
        (*key, json.dumps(message_cipher.protect_record(record), ensure_ascii=False), int(time.time())),
    )
    db.commit()


def load_message(key):
    row = db.execute(
        "SELECT data FROM messages WHERE connection_id=? AND chat_id=? AND message_id=?", key
    ).fetchone()
    record = json.loads(row["data"]) if row else None
    if record is not None:
        message_cipher.validate_record(record)
    return record


def enqueue(recipient_id, payload):
    if not product.notifications(recipient_id, payload.get("event"), ((payload.get("old") or payload.get("new") or {}).get("sender_id")==payload.get("account_id")) if payload.get("account_id") is not None else None):
        return
    if (db.execute("SELECT COUNT(*) FROM outbox WHERE recipient_id=?", (recipient_id,)).fetchone()[0] >= 2000
            or db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] >= 20000):
        raise RuntimeError("outbox_capacity")
    db.execute(
        "INSERT INTO outbox(recipient_id, data, created_at) VALUES (?, ?, ?)",
        (recipient_id, json.dumps(message_cipher.protect_payload(payload), ensure_ascii=False), int(time.time())),
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
            log.error("Не удалось получить бизнес-подключение (попытка %s)", attempt + 1)
            if attempt == 2:
                raise RuntimeError("connection_unavailable") from None
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
        product.ensure(account_id, recipient_id)
        product.observe(account_id,connection.is_enabled)
        rights = getattr(connection, 'rights', None)
        db.execute('UPDATE account_state SET rights=? WHERE account_id=?',
                   (json.dumps({name:bool(getattr(rights,name,False)) for name in ('can_reply','can_read_messages','can_delete_sent_messages','can_delete_all_messages')} if rights else {}), account_id))
        db.commit()
        if not known or known['enabled'] != int(connection.is_enabled):
            business_tools.repo.reset_continuity(account_id)
        log.info("Состояние Business-подключения обновлено")
        if not known or known['enabled'] != int(connection.is_enabled):
            enqueue_html_notification(recipient_id, 'Аккаунт подключён. Откройте /status: отправьте, измените и удалите тестовое сообщение в разрешённом чате. Статистика: /chatstats.' if connection.is_enabled else 'Соединение отключено. Сбор новых сообщений остановлен. Подключение: /connect.')


@bot.business_connection_handler()
async def on_connection(connection):
    await store_connection(connection)


from public_ui import PublicUI
public_ui = PublicUI(bot, db, routes, account_labels, product, inbox, lambda: business_tools,
                     lambda: profile_monitor, mtproto_access)
public_ui.register()


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
            newly_admitted = user_id not in routes
            access.grant(user_id, args[1] if len(args) > 1 else None)
            refresh_saved_routes()
            product.ensure(user_id, user_id)
            if newly_admitted:
                product.collection(user_id, True)
                business_tools.repo.reset_continuity(user_id)
            try:
                await public_ui.welcome(user_id, invited=True)
            except Exception:
                pass
            await bot.send_message(
                msg.chat.id,
                f"Доступ для {user_id} выдан. Другу нужно отправить боту /start, "
                "затем открыть «Подключение». Уведомления придут в его личный чат.",
            )
        else:
            removed = access.revoke(user_id)
            if removed:
                product.observe(user_id,False)
                db.execute('UPDATE account_state SET collecting=0,cutoff=? WHERE account_id=?',(int(time.time()*1000),user_id))
                db.commit()
                business_tools.repo.reset_continuity(user_id)
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
            "SELECT id, data FROM outbox WHERE recipient_id=? AND failed=1 AND created_at>?", (recipient_id,int(time.time())-7*86400)
        ).fetchall():
            try:
                payload = json.loads(row["data"])
            except (ValueError, KeyError, TypeError):
                continue
            target = notification_target(payload)
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
        example_account_id = next(a for a,o in routes.items() if o==recipient_id)
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
        "event": "text_notification", "source":"business_tools", "owner_id": recipient_id,
        "text": text, "parse_mode": "HTML",
    })
    db.commit()


def enqueue_profile_notification(recipient_id, text):
    if mtproto_access.owner(recipient_id):
        enqueue(recipient_id, {
            "event": "profile_change", "owner_id": recipient_id,
            "text": text, "parse_mode": "HTML",
        })
        db.commit()


profile_monitor = ProfileMonitorModule(
    bot, db, routes, os.getenv("PROFILE_DATA_DIR", "profile_data"), enqueue_profile_notification,
    os.getenv("UI_BANNER_PATH", "banner.jpg"),
    access_policy=mtproto_access,
)
business_tools = BusinessToolsModule(
    bot, db, routes, account_labels, os.getenv("PROFILE_DATA_DIR", "profile_data"),
    enqueue_html_notification, os.getenv("UI_BANNER_PATH", "banner.jpg"), message_cipher=message_cipher,
)


if not product.erasure_guard.path.exists():
    product.erasure_guard.record(0, 0)
product.erasure_guard.apply(product, inbox)

CONTENT_TYPES = list(dict.fromkeys(
    content_type_media + content_type_service + ["paid_media", "checklist", "live_photo", "rich_message", None]
))


def account_for(connection_id):
    row = db.execute("SELECT account_id FROM connections WHERE connection_id=?", (connection_id,)).fetchone()
    return row[0] if row else None


def normalize_update(raw):
    for kind in ('business_message', 'edited_business_message'):
        if kind in raw:
            msg = types.Message.de_json(raw[kind])
            return {'kind': kind, 'connection_id': msg.business_connection_id,
                    'account_id': account_for(msg.business_connection_id), 'chat_id': msg.chat.id,
                    'message_id': msg.message_id, 'received_at': int(time.time()*1000),
                    'record': message_cipher.protect_record(record_from_message(msg))}
    if 'deleted_business_messages' in raw:
        event = raw['deleted_business_messages']
        return dict(kind='delete', connection_id=event['business_connection_id'],
                    account_id=account_for(event['business_connection_id']),
                    chat_id=event['chat']['id'], message_ids=event['message_ids'], received_at=int(time.time()*1000))
    return {'kind': 'control', 'raw': raw}


def apply_business(uid, payload, recipient):
    connection, chat = payload['connection_id'], payload['chat_id']
    account = account_for(connection)
    if not product.allows(account, payload['received_at']):
        return
    db.execute('UPDATE account_state SET last_event=? WHERE account_id=?', (int(time.time()), account))
    if payload['kind'] == 'delete':
        for mid in payload['message_ids']:
            key = (connection, chat, mid)
            old = load_message(key)
            if not inbox.accept_version(key, old or {}, uid, deleted=True):
                continue
            self_deleted = business_tools.consume_self_deleted(*key)
            business_tools.service.record_deletion(recipient, account, key, old, self_deleted)
            if old and not self_deleted and not (only_others_enabled(recipient) and old.get('sender_id') == account):
                enqueue(recipient, dict(event='delete', old=old, account_id=account))
            db.execute('DELETE FROM messages WHERE connection_id=? AND chat_id=? AND message_id=?', key)
        return
    key = (connection, chat, payload['message_id'])
    new = payload['record']
    if not inbox.accept_version(key, new, uid):
        tombstone=db.execute('SELECT deleted FROM message_versions WHERE connection_id=? AND chat_id=? AND message_id=?',key).fetchone()
        if tombstone and tombstone[0] and payload['kind']=='business_message':
            # Late metadata may complete statistics, never revive the message body.
            business_tools.repo.record_message(connection,account,recipient,key[2],new)
        return
    old = load_message(key)
    if payload['kind'] == 'business_message':
        business_tools.repo.record_message(connection, account, recipient, key[2], new)
    else:
        # A first-seen edit is a known message, with an unknown original body.
        business_tools.repo.record_message(connection, account, recipient, key[2], old or new)
        changed = business_tools.service.record_edit(recipient, account, key, old, new)
        if changed and old and not message_cipher.records_equal(old, new) and not (only_others_enabled(recipient) and new.get('sender_id') == account):
            enqueue(recipient, dict(event='edit', old=old, new=new, account_id=account))
    save_message(key, new)


async def dispatch_incoming(uid, payload):
    if payload['kind'] == 'control':
        raw = payload['raw']
        if 'business_connection' in raw:
            await store_connection(types.BusinessConnection.de_json(raw['business_connection']))
        else:
            await bot.process_new_updates([types.Update.de_json(raw)])
        inbox.done(uid)
        return
    recipient = await recipient_for(payload['connection_id'])
    # No await inside this transaction: repositories, outbox and checkpoint commit together.
    with db.event_transaction():
        if recipient is not None:
            apply_business(uid, payload, recipient)
        inbox.done(uid)


async def durable_polling():
    async def get_updates(**kwargs):
        return await asyncio_helper.get_updates(TOKEN, **kwargs)
    processor = asyncio.create_task(inbox.work(dispatch_incoming), name='incoming-worker')
    try:
        await inbox.poll(get_updates, normalize_update)
    finally:
        processor.cancel()
        await asyncio.gather(processor, return_exceptions=True)



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
        await limited_send(bot.send_message, recipient_id, value[pos:pos + 1800])


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
            await limited_send(bot.send_message, recipient_id, rich, parse_mode="HTML")
        except ApiTelegramException as exc:
            # Если Telegram отверг кастомные эмодзи, доставим обычный текст.
            if exc.error_code != 400:
                raise
            plain = (
                f"{heading_alt} {title}\n👤 Автор: {sender_plain}\n💬 Чат: {chat_plain}\n"
                f"👤 Аккаунт: {account}\n"
                f"🔄 Отправлено: {when}\n\n{body_plain}"
            )
            await limited_send(bot.send_message, recipient_id, plain)
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
        await limited_send(bot.send_message, recipient_id, continuation)
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
        await limited_send(method, recipient_id, file_id)


async def send_notification(
    recipient_id, payload, media_sent=False, mark_media_sent=None,
    text_parts_sent=0, mark_text_part=None,
):
    payload = message_cipher.open_payload(payload)
    if payload["event"] in ("profile_change", "text_notification"):
        if text_parts_sent == 0:
            await limited_send(bot.send_message, recipient_id, payload["text"], parse_mode=payload.get("parse_mode"))
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
                log.warning("Удалённое медиа недоступно (400)")
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


async def limited_send(method, recipient, *args, **kwargs):
    if getattr(method,'_nyan_paced',False) is True:
        return await method(recipient,*args,**kwargs)
    await delivery_budget.take(recipient)
    current = current_delivery.get()
    if current and (not db.execute('SELECT 1 FROM outbox WHERE id=?',(current[0],)).fetchone() or notification_target(current[1]) != recipient):
        raise DeliveryCancelled()
    return await method(recipient, *args, **kwargs)


class DeliveryCancelled(Exception):
    pass


async def deliver_row(row):
    try:
        payload = json.loads(row['data'])
        current_delivery.set((row['id'],payload))
        if notification_target(payload) != row['recipient_id']:
            db.execute('DELETE FROM outbox WHERE id=?', (row['id'],)); db.commit()
            return
        def mark_media_sent():
            db.execute('UPDATE outbox SET media_sent=1 WHERE id=?', (row['id'],)); db.commit()
        def mark_text_part(count):
            db.execute('UPDATE outbox SET text_parts_sent=? WHERE id=?', (count,row['id'])); db.commit()
        await send_notification(row['recipient_id'], payload, media_sent=bool(row['media_sent']),
                                mark_media_sent=mark_media_sent, text_parts_sent=row['text_parts_sent'],
                                mark_text_part=mark_text_part)
    except DeliveryCancelled:
        db.execute('DELETE FROM outbox WHERE id=?',(row['id'],)); db.commit()
    except Exception as exc:
        attempts = row['attempts']+1
        code = getattr(exc, 'error_code', None)
        delay = min(3600, 15*2**min(attempts-1,8))
        if code == 429:
            delay = max(delay, int((exc.result_json or {}).get('parameters',{}).get('retry_after',1)))
            delivery_budget.postpone(row['recipient_id'], delay)
        failed = code in (400,401,403,404) or attempts >= 20
        db.execute('UPDATE outbox SET attempts=?,retry_at=?,failed=?,last_error=? WHERE id=?',
                   (attempts,int(time.time())+delay,int(failed),f'{type(exc).__name__}:{code or 0}',row['id']))
        db.commit()
        log.warning('Доставка %s отложена (%s)', row['id'], type(exc).__name__)
    else:
        db.execute('DELETE FROM outbox WHERE id=?',(row['id'],)); db.commit()


async def outbox_worker():
    active = {}
    try:
        while True:
            for recipient, task in list(active.items()):
                if task.done():
                    task.result(); del active[recipient]
            now = time.time()
            rows = db.execute("""SELECT o.* FROM outbox o
                WHERE o.failed=0 AND o.retry_at<=? AND NOT EXISTS(
                    SELECT 1 FROM delivery_cooldowns c WHERE c.recipient_id IN (o.recipient_id,0) AND c.until_at>?)
                AND o.id=(SELECT MIN(x.id) FROM outbox x WHERE x.recipient_id=o.recipient_id AND x.failed=0)
                ORDER BY o.id LIMIT 32""",(now,now)).fetchall()
            for row in rows:
                if row['recipient_id'] not in active and len(active)<8:
                    active[row['recipient_id']] = asyncio.create_task(deliver_row(row))
            await asyncio.sleep(.05 if rows else .2)
    finally:
        for task in active.values(): task.cancel()
        await asyncio.gather(*active.values(), return_exceptions=True)


async def supervise_outbox():
    while True:
        try:
            await outbox_worker()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("Очередь уведомлений остановилась; перезапущу через 15 секунд")
            await asyncio.sleep(15)


async def maintain_storage():
    while True:
        try:
            now = int(time.time())
            with db:
                db.execute('DELETE FROM messages WHERE created_at<?',(now-retention_days*86400,))
                db.execute('DELETE FROM outbox WHERE created_at<?',(now-7*86400,))
                db.execute('DELETE FROM business_stats_events WHERE detected_at<?',(now-90*86400,))
                # Compact seen rows only when a durable version guard prevents recounting.
                db.execute('DELETE FROM business_stats_seen WHERE observed_at<? AND EXISTS('
                           'SELECT 1 FROM message_versions v WHERE v.connection_id=business_stats_seen.connection_id '
                           'AND v.chat_id=business_stats_seen.chat_id AND v.message_id=business_stats_seen.message_id)',(now-90*86400,))
                db.execute('DELETE FROM profile_changes WHERE created_at<?',(now-90*86400,))
                expired_photos=db.execute('SELECT id,local_path FROM profile_photos WHERE created_at<? LIMIT 500',(now-90*86400,)).fetchall()
                for photo in expired_photos:
                    db.execute('DELETE FROM profile_photos WHERE id=?',(photo['id'],))
                    if photo['local_path'] and not db.execute('SELECT 1 FROM profile_photos WHERE local_path=?',(photo['local_path'],)).fetchone():
                        photo_path=Path(photo['local_path']).resolve()
                        if photo_path.is_relative_to(Path(os.getenv('PROFILE_DATA_DIR','profile_data')).resolve()):
                            photo_path.unlink(missing_ok=True)
                db.execute('DELETE FROM business_stats_ui WHERE updated_at<?',(now-7*86400,))
                db.execute('DELETE FROM business_self_deletions WHERE created_at<?',(now-86400,))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='business_avatar_cache'").fetchone():
                    db.execute('DELETE FROM business_avatar_cache WHERE expires_at<?',(now,))
                db.execute('DELETE FROM data_confirmations WHERE expires_at<?',(now,))
            inbox.prune()
            # Pending inbox is never aged out: health reports the oldest event for operator action.
            db.execute("INSERT OR REPLACE INTO business_stats_progress VALUES('maintenance_at',?)",(int(time.time()),))
            db.commit()
        except Exception as exc:
            db.rollback()
            log.error('Очистка хранилища отложена (%s)',type(exc).__name__)
        await asyncio.sleep(60)


async def main():
    worker = asyncio.create_task(supervise_outbox())
    recovery = asyncio.create_task(business_tools.recover_history())
    maintenance = asyncio.create_task(maintain_storage())
    web_runner = None
    try:
        await profile_monitor.start()
        if os.getenv('WEBAPP_URL'):
            from business_tools.webapp import start_server, validate_url
            validate_url(os.environ['WEBAPP_URL'])
            web_runner = await start_server(TOKEN, business_tools.repo, business_tools.service,
                                            routes, os.getenv('WEBAPP_HOST', '127.0.0.1'),
                                            int(os.getenv('WEBAPP_PORT', '8080')),
                                            os.getenv('WEBAPP_SSL_CERT'), os.getenv('WEBAPP_SSL_KEY'),
                                            profile_module=profile_monitor, product=product, inbox=inbox)
            log.info('Mini App запущен на порту %s', os.getenv('WEBAPP_PORT', '8080'))
        # The name is managed in BotFather. Reapplying it on each restart can
        # hit Telegram's long setMyName flood limit and must not stop polling.
        try:
            await bot.set_my_commands([
                types.BotCommand("menu", "Главное меню"),
                types.BotCommand("chatstats", "Открыть статистику"),
                types.BotCommand("connect", "Подключение и состояние"),
                types.BotCommand("notifications", "Уведомления"),
                types.BotCommand("data", "Управление данными"),
                types.BotCommand("help", "Помощь"),
            ])
        except Exception as exc:
            log.warning('Меню команд не обновлено (%s); продолжаю запуск', type(exc).__name__)
        log.info("Бот запущен")
        await durable_polling()
    finally:
        if web_runner:
            await web_runner.cleanup()
        await profile_monitor.close()
        worker.cancel()
        recovery.cancel()
        maintenance.cancel()
        await asyncio.gather(worker, recovery, maintenance, return_exceptions=True)
        await bot.close_session()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
