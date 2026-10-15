import base64
import functools
import logging
from datetime import datetime
from html import escape
from pathlib import Path

from telebot import types
from telebot.asyncio_helper import ApiTelegramException

from .service import FIELD_NAMES, display_value
from .style import icon, styled_button, truncate_html


log = logging.getLogger(__name__)
PAGE_SIZE = 6
PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

def button(text, data):
    label, icon_id = styled_button(text)
    return types.InlineKeyboardButton(
        label, callback_data=data, icon_custom_emoji_id=icon_id
    )


def keyboard(*rows):
    markup = types.InlineKeyboardMarkup()
    for row in rows:
        markup.row(*row)
    return markup


def guarded_callback(bot):
    """Dismiss the loading indicator even when rendering fails, and never crash on forged data."""
    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(call):
            text = None
            try:
                return await handler(call)
            except (ValueError, IndexError, KeyError):
                text = "⚠️ Некорректные данные кнопки. Откройте раздел заново через /menu"
            except Exception:
                log.exception("Ошибка обработки кнопки %r", call.data)
                text = "⚠️ Не удалось обновить экран. Откройте раздел заново через /menu"
            finally:
                if not getattr(call, "_answered", False):
                    call._answered = True
                    try:
                        await bot.answer_callback_query(call.id, text, show_alert=bool(text))
                    except Exception:
                        log.exception("Не удалось ответить на callback %s", call.id)
        return wrapper
    return decorator


def stamp(value, full=False):
    if not value:
        return "ещё не проверялся"
    return datetime.fromtimestamp(value).strftime("%d.%m.%Y %H:%M" if full else "%H:%M")


class ProfileUI:
    def __init__(self, bot, service, repository, owner_routes, placeholder_dir, banner_path=None,
                 access_policy=None):
        self.bot = bot
        self.service = service
        self.repo = repository
        self.owner_routes = owner_routes
        self.access_policy = access_policy
        self.pending_add = {}
        requested_banner = Path(banner_path) if banner_path else None
        if requested_banner and requested_banner.is_file():
            self.placeholder = requested_banner
        else:
            self.placeholder = Path(placeholder_dir) / "profile_ui.png"
            self.placeholder.parent.mkdir(parents=True, exist_ok=True)
            if not self.placeholder.exists():
                self.placeholder.write_bytes(PLACEHOLDER_PNG)

    def register(self):
        bot = self.bot

        @bot.message_handler(commands=["menu"])
        async def menu_command(msg):
            if self.authorized_message(msg):
                await self.send_main(msg.chat.id)

        @bot.message_handler(commands=["watch", "unwatch", "watchlist", "profile_history", "username_history"])
        async def profile_commands(msg):
            if self.authorized_message(msg):
                if self.profile_access(msg.from_user.id):
                    await self.handle_command(msg)
                else:
                    await self.send_html(msg.chat.id, "Мониторинг профилей доступен только выбранным пользователям.")

        # Commands must fall through to handlers registered later (e.g. /mute in
        # business_tools), so this handler never matches messages starting with "/".
        @bot.message_handler(func=lambda m: m.from_user and m.from_user.id in self.pending_add
                                     and not (m.text or "").startswith("/"),
                             content_types=["text"])
        async def add_input(msg):
            state = self.pending_add.pop(msg.from_user.id, None)
            if not state or not self.authorized_message(msg) or not self.profile_access(msg.from_user.id):
                return
            try:
                snapshot, added = await self.service.watch(self.owner(msg.from_user.id), msg.text)
                result = "добавлен.\nПервый snapshot сохранён без события." if added else "уже отслеживается."
                result_icon = icon("success" if added else "add")
                await self.render_watch(state["chat_id"], state["message_id"],
                                        f"{result_icon} @{escape(str(snapshot.username or snapshot.user_id))} {result}")
            except Exception as exc:
                await self.render_watch(state["chat_id"], state["message_id"], f"{icon('error')} Не удалось добавить: {escape(str(exc))}")

        guard = guarded_callback(bot)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pm:nav:"))
        @guard
        async def navigation(call):
            if not await self.authorized_callback(call): return
            self.pending_add.pop(call.from_user.id, None)
            page = call.data[7:]
            if page == "main": await self.render_main(call.message.chat.id, call.message.message_id)
            elif page == "watch": await self.render_watch(call.message.chat.id, call.message.message_id)
            elif page == "settings": await self.render_settings(call.message.chat.id, call.message.message_id)
            elif page == "events": await self.render_recent(call.message.chat.id, call.message.message_id, 0, "main")
            elif page == "profiles": await self.render_list(call.message.chat.id, call.message.message_id, 0, "main")
            elif page == "tools": await self.render_tools(call.message.chat.id, call.message.message_id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pm:settings:"))
        @guard
        async def settings_navigation(call):
            if not await self.authorized_callback(call): return
            await self.render_setting(call.message.chat.id, call.message.message_id, call.data.split(":")[2])

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pm:add"))
        @guard
        async def add_callback(call):
            if not await self.authorized_callback(call): return
            self.pending_add[call.from_user.id] = {"chat_id": call.message.chat.id, "message_id": call.message.message_id}
            await self.edit_text(call.message.chat.id, call.message.message_id,
                f"{icon('add')} <b>Добавление профиля</b>\n\n{icon('info')} Отправьте следующим сообщением "
                "<code>@username</code> или числовой ID.",
                keyboard([button("◀ Назад", "pm:nav:watch")]))

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pm:list:"))
        @guard
        async def list_callback(call):
            if not await self.authorized_callback(call): return
            parts = call.data.split(":")
            if len(parts) != 4:
                raise ValueError("bad callback data")
            await self.render_list(call.message.chat.id, call.message.message_id, int(parts[2]), parts[3])

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pm:user:"))
        @guard
        async def user_callback(call):
            if not await self.authorized_callback(call): return
            parts = call.data.split(":")
            if len(parts) != 5:
                raise ValueError("bad callback data")
            _, _, user_id, page, source = parts
            await self.render_user(call.message.chat.id, call.message.message_id, int(user_id), int(page), source)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pm:hist:"))
        @guard
        async def history_callback(call):
            if not await self.authorized_callback(call): return
            parts = call.data.split(":")
            if len(parts) != 6:
                raise ValueError("bad callback data")
            _, _, user_id, page, list_page, source = parts
            await self.render_history(call.message.chat.id, call.message.message_id, int(user_id), int(page), int(list_page), source)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pm:recent:"))
        @guard
        async def recent_callback(call):
            if not await self.authorized_callback(call): return
            parts = call.data.split(":")
            if len(parts) != 4:
                raise ValueError("bad callback data")
            await self.render_recent(call.message.chat.id, call.message.message_id, int(parts[2]), parts[3])

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pm:photos:"))
        @guard
        async def photos_callback(call):
            if not await self.authorized_callback(call): return
            parts = call.data.split(":")
            if len(parts) != 6:
                raise ValueError("bad callback data")
            _, _, user_id, index, list_page, source = parts
            await self.render_photos(call.message.chat.id, call.message.message_id, int(user_id), int(index), int(list_page), source)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pm:check:"))
        @guard
        async def check_callback(call):
            if not await self.authorized_callback(call): return
            parts = call.data.split(":")
            if len(parts) != 5:
                raise ValueError("bad callback data")
            _, _, user_id, page, source = parts
            try:
                changes = await self.service.check(self.owner(call.from_user.id), int(user_id))
                note = (f"{icon('changed')} Изменения найдены" if changes
                        else f"{icon('error')} Изменений нет")
            except Exception as exc:
                note = f"{icon('warning')} Ошибка: {escape(str(exc)[:120])}"
            await self.render_user(call.message.chat.id, call.message.message_id, int(user_id), int(page), source, note)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pm:askoff:"))
        @guard
        async def ask_disable(call):
            if not await self.authorized_callback(call): return
            parts = call.data.split(":")
            if len(parts) != 5:
                raise ValueError("bad callback data")
            _, _, user_id, page, source = parts
            row = self.repo.get(self.owner(call.from_user.id), int(user_id))
            name = "@" + row["username"] if row and row["username"] else str(user_id)
            await self.edit_text(call.message.chat.id, call.message.message_id,
                f"{icon('info')} Вы уверены, что хотите перестать отслеживать {escape(name)}?",
                keyboard([button("Да, отключить", f"pm:off:{user_id}:{page}:{source}"), button("Отмена", f"pm:user:{user_id}:{page}:{source}")]))

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pm:off:"))
        @guard
        async def disable_callback(call):
            if not await self.authorized_callback(call): return
            parts = call.data.split(":")
            if len(parts) != 5:
                raise ValueError("bad callback data")
            _, _, user_id, page, source = parts
            self.service.unwatch(self.owner(call.from_user.id), user_id)
            await self.render_list(call.message.chat.id, call.message.message_id, int(page), source)
            call._answered = True
            await bot.answer_callback_query(call.id, "👁 Отслеживание отключено")

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pm:set:"))
        @guard
        async def setting_callback(call):
            if not await self.authorized_callback(call): return
            parts = call.data.split(":")
            if len(parts) != 4:
                raise ValueError("bad callback data")
            _, _, field, value = parts
            owner_id = self.owner(call.from_user.id)
            if field == "interval":
                seconds = int(value)
                if seconds < 300:
                    raise ValueError("Интервал не может быть меньше 5 минут")
                self.repo.set_setting(owner_id, "interval_seconds", seconds)
            elif field == "notify": self.repo.set_setting(owner_id, "notifications", int(value))
            elif field == "photos": self.repo.set_setting(owner_id, "keep_photos", int(value))
            elif field == "format":
                if value not in ("compact", "detailed"):
                    raise ValueError("Неизвестный формат")
                self.repo.set_setting(owner_id, "notification_format", value)
            await self.render_setting(call.message.chat.id, call.message.message_id, field)
            call._answered = True
            await bot.answer_callback_query(call.id, "📄 Сохранено")

    def authorized_message(self, msg):
        return msg.chat.type == "private" and msg.from_user and msg.from_user.id in self.owner_routes

    async def authorized_callback(self, call):
        public_navigation = call.data in ("pm:nav:main", "pm:nav:tools")
        if (call.from_user.id in self.owner_routes and call.message.chat.id == call.from_user.id
                and (public_navigation or self.profile_access(call.from_user.id))):
            return True
        call._answered = True
        try:
            await self.bot.answer_callback_query(call.id, "⚠️ Нет доступа", show_alert=True)
        except Exception:
            log.exception("Не удалось ответить на callback %s", call.id)
        return False

    def profile_access(self, actor_id):
        return bool(self.access_policy and self.access_policy.actor(actor_id))

    async def handle_command(self, msg):
        self.pending_add.pop(msg.from_user.id, None)
        owner_id = self.owner(msg.from_user.id)
        command, *args = (msg.text or "").split(maxsplit=1)
        command = command.split("@")[0]
        if command == "/watch":
            if not args:
                await self.send_html(msg.chat.id, f"{icon('question')} Использование: <code>/watch @username</code> или <code>/watch user_id</code>")
                return
            try:
                snapshot, added = await self.service.watch(owner_id, args[0])
                result = "добавлен. Baseline сохранён." if added else "уже отслеживается."
                result_icon = icon("success" if added else "add")
                await self.send_html(msg.chat.id, f"{result_icon} Профиль @{escape(str(snapshot.username or snapshot.user_id))} {result}")
            except Exception as exc:
                await self.send_html(msg.chat.id, f"{icon('error')} Не удалось добавить: {escape(str(exc))}")
        elif command == "/unwatch":
            ok = bool(args) and self.service.unwatch(owner_id, args[0])
            await self.send_html(msg.chat.id, (f"{icon('watch_disabled')} Отслеживание отключено" if ok
                                                else f"{icon('warning')} Пользователь не найден"))
        elif command == "/watchlist":
            rows = self.repo.list(owner_id)
            text = "\n".join(f"{icon('username_info')} {escape(self.row_name(row))}" for row in rows) or f"{icon('empty_list')} Список пуст"
            await self.send_html(msg.chat.id, text)
        else:
            field = "username" if command == "/username_history" else None
            target = self.repo.find(owner_id, args[0]) if args else None
            rows = self.repo.history(owner_id, target["user_id"] if target else None, field=field)
            await self.send_html(msg.chat.id, self.history_text(rows) or f"{icon('empty_history')} История пуста")

    async def send_main(self, chat_id):
        with open(self.placeholder, "rb") as image:
            await self.bot.send_photo(chat_id, image, caption=f"{icon('bot')} <b>Nyan message</b>", parse_mode="HTML", reply_markup=self.main_keyboard(chat_id))

    async def render_main(self, chat_id, message_id):
        await self.edit_text(chat_id, message_id, f"{icon('bot')} <b>Nyan message</b>", self.main_keyboard(chat_id))

    def main_keyboard(self, actor_id=None):
        if not self.profile_access(actor_id):
            return keyboard([button("🔧 Инструменты", "pm:nav:tools")])
        return keyboard(
            [button("👤 Профили", "pm:nav:profiles"), button("📡 Наблюдение", "pm:nav:watch")],
            [button("🗃 События", "pm:nav:events"), button("🔧 Инструменты", "pm:nav:tools")],
            [button("⚙️ Настройки", "pm:nav:settings")],
        )

    async def render_watch(self, chat_id, message_id, notice=None):
        owner_id = self.owner(chat_id)
        rows = self.repo.list(owner_id)
        recent = self.repo.history(owner_id, limit=1)
        changed_icon = icon("success" if recent else "error")
        text = (f"{icon('watch')} <b>Наблюдение</b>\n\n{icon('count')} Отслеживается: {len(rows)}"
                f"\n{icon('recent_changes')} Последние изменения: {changed_icon} {'Есть' if recent else 'Нет'}")
        if notice: text += "\n\n" + notice
        await self.edit_text(chat_id, message_id, text, keyboard(
            [button("➕ Добавить", "pm:add"), button("👥 Список", "pm:list:0:watch")],
            [button("🕘 Последние изменения", "pm:recent:0:watch")], [button("◀ Назад", "pm:nav:main")]))

    async def render_list(self, chat_id, message_id, page, source="watch"):
        owner_id = self.owner(chat_id)
        rows = self.repo.list(owner_id); total = len(rows); page = max(0, min(page, max(0, (total-1)//PAGE_SIZE)))
        shown = rows[page*PAGE_SIZE:(page+1)*PAGE_SIZE]
        lines = [f"{icon('list')} <b>Список наблюдения</b>", ""]
        buttons = []
        for row in shown:
            changed = self.repo.history(owner_id, row["user_id"], limit=1)
            state_icon = icon("success" if changed else "error")
            lines.append(f"{icon('profiles')} {escape(self.row_name(row))}\n{icon('check')} Проверка: {stamp(row['last_checked_at'])}"
                         f"\n{icon('history')} Изменения: {state_icon} {'Есть' if changed else 'Нет'}")
            buttons.append([button(self.row_name(row)[:32], f"pm:user:{row['user_id']}:{page}:{source}")])
        if not shown: lines.append(f"{icon('empty_list')} Список пуст")
        nav = []
        if page: nav.append(button("◀", f"pm:list:{page-1}:{source}"))
        if (page+1)*PAGE_SIZE < total: nav.append(button("▶", f"pm:list:{page+1}:{source}"))
        if nav: buttons.append(nav)
        buttons.append([button("◀ Назад", f"pm:nav:{source}")])
        await self.edit_text(chat_id, message_id, "\n".join(lines), keyboard(*buttons))

    async def render_user(self, chat_id, message_id, user_id, page, source="watch", notice=None):
        row = self.repo.get(self.owner(chat_id), user_id)
        if not row: return await self.render_list(chat_id, message_id, page)
        name = " ".join(x for x in (row["first_name"], row["last_name"]) if x)
        name_line = f"{icon('card')} <b>{escape(name)}</b>" if name else f"{icon('no_name')} Без имени"
        username_line = f"{icon('username_info')} @{escape(row['username'])}" if row["username"] else f"{icon('no_username')} Без username"
        bio = row["bio"]
        bio_line = escape(bio[:300]) if bio else f"{icon('unknown_value')} Не указано или недоступно"
        text = (f"{name_line}\n{username_line}\n\n{icon('info')} Статус: "
                f"{'отслеживается' if row['enabled'] else 'отключён'}\n{icon('tools')} Bio: {bio_line}"
                f"\n{icon('history')} Последняя проверка: {stamp(row['last_checked_at'])}"
                f"\n{icon('date')} Дата добавления: {stamp(row['added_at'], True)}")
        if row["last_error"]: text += f"\n{icon('last_error')} Ошибка: {escape(row['last_error'])}"
        if notice: text += "\n\n" + notice
        await self.edit_text(chat_id, message_id, text, keyboard(
            [button("🕘 История", f"pm:hist:{user_id}:0:{page}:{source}"), button("🖼 Аватарки", f"pm:photos:{user_id}:0:{page}:{source}")],
            [button("🔄 Проверить сейчас", f"pm:check:{user_id}:{page}:{source}")],
            [button("🔕 Отключить", f"pm:askoff:{user_id}:{page}:{source}")], [button("◀ Назад", f"pm:list:{page}:{source}")]))

    async def render_history(self, chat_id, message_id, user_id, page, list_page=0, source="watch"):
        rows = self.repo.history_page(self.owner(chat_id), user_id, page, PAGE_SIZE)
        text = f"{icon('history')} <b>История профиля</b>\n\n" + (self.history_text(rows) or f"{icon('empty_history')} История пуста")
        nav = []
        batch_count = len({row["batch_id"] for row in rows})
        if page: nav.append(button("◀", f"pm:hist:{user_id}:{page-1}:{list_page}:{source}"))
        if batch_count == PAGE_SIZE: nav.append(button("▶", f"pm:hist:{user_id}:{page+1}:{list_page}:{source}"))
        buttons = [nav] if nav else []
        buttons.append([button("◀ Назад", f"pm:user:{user_id}:{list_page}:{source}")])
        await self.edit_text(chat_id, message_id, text, keyboard(*buttons))

    async def render_recent(self, chat_id, message_id, page, source="watch"):
        rows = self.repo.history_page(self.owner(chat_id), page=page, batches=PAGE_SIZE)
        text = f"{icon('history')} <b>Последние изменения</b>\n\n" + (self.history_text(rows) or f"{icon('empty_history')} История пуста")
        nav = []
        batch_count = len({row["batch_id"] for row in rows})
        if page: nav.append(button("◀", f"pm:recent:{page-1}:{source}"))
        if batch_count == PAGE_SIZE: nav.append(button("▶", f"pm:recent:{page+1}:{source}"))
        buttons = [nav] if nav else []
        buttons.append([button("◀ Назад", f"pm:nav:{source}")])
        await self.edit_text(chat_id, message_id, text, keyboard(*buttons))

    async def render_photos(self, chat_id, message_id, user_id, index, list_page=0, source="watch"):
        photos = self.repo.photos(self.owner(chat_id), user_id)
        if not photos:
            return await self.edit_text(chat_id, message_id, f"{icon('no_photos')} Сохранённых аватарок нет.", keyboard([button("◀ Назад", f"pm:user:{user_id}:{list_page}:{source}")]))
        index = max(0, min(index, len(photos)-1)); row = photos[index]
        caption = (f"{icon('photos')} <b>Аватарки</b>\n{icon('info')} Версия {index+1} из {len(photos)}"
                   f"\n{icon('time')} Обнаружена: {stamp(row['created_at'], True)}")
        nav = []
        if index: nav.append(button("◀", f"pm:photos:{user_id}:{index-1}:{list_page}:{source}"))
        if index+1 < len(photos): nav.append(button("▶", f"pm:photos:{user_id}:{index+1}:{list_page}:{source}"))
        buttons = [nav] if nav else []
        buttons.append([button("◀ Назад", f"pm:user:{user_id}:{list_page}:{source}")])
        path = row["local_path"]
        if path and Path(path).exists():
            with open(path, "rb") as image:
                media = types.InputMediaPhoto(image, caption=caption, parse_mode="HTML")
                await self.bot.edit_message_media(media, chat_id, message_id, reply_markup=keyboard(*buttons))
        else:
            await self.edit_text(chat_id, message_id, caption + f"\n{icon('warning')} Файл недоступен.", keyboard(*buttons))

    async def render_settings(self, chat_id, message_id):
        row = self.repo.settings(self.owner(chat_id))
        mins = row["interval_seconds"] // 60
        text = (f"{icon('settings')} <b>Настройки</b>\n\n{icon('notifications')} Уведомления: "
                f"{'включены' if row['notifications'] else 'выключены'}\n{icon('interval')} Интервал: "
                f"{mins if mins < 60 else 1} {'мин' if mins < 60 else 'час'}\n{icon('photo_storage')} "
                f"Хранение аватарок: {'включено' if row['keep_photos'] else 'выключено'}\n{icon('format')} "
                f"Формат: {row['notification_format']}")
        await self.edit_text(chat_id, message_id, text, keyboard(
            [button("🔔 Уведомления", "pm:settings:notify")],
            [button("⏱ Интервал проверки", "pm:settings:interval")],
            [button("🖼 Хранение аватарок", "pm:settings:photos")],
            [button("📝 Формат уведомлений", "pm:settings:format")],
            [button("◀ Назад", "pm:nav:main")]))

    async def render_setting(self, chat_id, message_id, field):
        titles = {"notify": "🔔 Уведомления", "interval": "⏱ Интервал проверки",
                  "photos": "🖼 Хранение аватарок", "format": "📝 Формат уведомлений"}
        rows = []
        if field == "notify": rows = [[button("Включить", "pm:set:notify:1"), button("Выключить", "pm:set:notify:0")]]
        elif field == "interval": rows = [
            [button("5 минут", "pm:set:interval:300"), button("15 минут", "pm:set:interval:900")],
            [button("30 минут", "pm:set:interval:1800"), button("1 час", "pm:set:interval:3600")]]
        elif field == "photos": rows = [[button("Хранить", "pm:set:photos:1"), button("Не хранить", "pm:set:photos:0")]]
        elif field == "format": rows = [[button("Компактный", "pm:set:format:compact"), button("Подробный", "pm:set:format:detailed")]]
        else: return await self.render_settings(chat_id, message_id)
        rows.append([button("◀ Назад", "pm:nav:settings")])
        title_icons = {"notify": "notifications", "interval": "interval", "photos": "photo_storage", "format": "format"}
        await self.edit_text(chat_id, message_id, f"{icon(title_icons[field])} {titles[field].split(' ', 1)[-1]}"
                             f"\n\n{icon('popup_info')} Выберите значение:", keyboard(*rows))

    async def render_tools(self, chat_id, message_id):
        text = f"{icon('tools')} <b>Инструменты</b>"
        if self.profile_access(chat_id):
            rows = self.repo.list(self.owner(chat_id)); errors = sum(bool(r["last_error"]) for r in rows)
            text += f"\n\n{icon('list')} Профилей: {len(rows)}\n{icon('warning')} Ошибок проверки: {errors}"
        await self.edit_text(chat_id, message_id, text, keyboard(
                                 [button("🔕 Муты", "bt:mutes:0"), button("📊 Статистика", "bt:stats:0")],
                                 [button("◀ Назад", "pm:nav:main")]))

    async def send_html(self, chat_id, text):
        await self.bot.send_message(chat_id, text, parse_mode="HTML")

    async def edit_text(self, chat_id, message_id, text, markup):
        # The UI message always remains a photo, allowing avatar versions to replace its media.
        with open(self.placeholder, "rb") as image:
            media = types.InputMediaPhoto(image, caption=truncate_html(text, 900), parse_mode="HTML")
            try:
                await self.bot.edit_message_media(media, chat_id, message_id, reply_markup=markup)
            except ApiTelegramException as exc:
                if exc.error_code != 400 or "message is not modified" not in str(exc).lower():
                    raise

    @staticmethod
    def row_name(row):
        name = " ".join(x for x in (row["first_name"], row["last_name"]) if x)
        return "@" + row["username"] if row["username"] else (name or str(row["user_id"]))

    def owner(self, actor_id):
        return self.owner_routes[actor_id]

    @staticmethod
    def history_text(rows):
        if not rows: return ""
        groups = []
        for row in rows:
            if not groups or groups[-1][0] != row["batch_id"]:
                groups.append((row["batch_id"], row["created_at"], []))
            groups[-1][2].append(row)
        chunks = []
        for _, created, items in groups:
            lines = [f"<b>{stamp(created, True)}</b>"]
            for item in items:
                icon_key = {"username": "username", "first_name": "name", "last_name": "name",
                            "bio": "bio", "photo_id": "avatar"}.get(item["field"], "history")
                lines.append(f"{icon(icon_key)} {FIELD_NAMES.get(item['field'], item['field'])}: "
                             f"{escape(display_value(item['field'], item['old_value']))} → "
                             f"{escape(display_value(item['field'], item['new_value']))}")
            chunks.append("\n".join(lines))
        return "\n\n".join(chunks)
