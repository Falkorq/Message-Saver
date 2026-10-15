"""Public invitation, connection, notification and data flows."""
import json
import os
import secrets
import time
from datetime import datetime, timezone, timedelta
from contextvars import ContextVar
from functools import wraps
from copy import copy
from telebot import types
from telebot.asyncio_helper import ApiTelegramException
from profile_monitor.style import TEXT_EMOJI_IDS
from ui_banners import banner_for, banner_cache
from public_pages import public_url
from ui_text import menu_html, telegram_length, without_custom_emoji


class PublicUI:
    def __init__(self, bot, db, routes, labels, product, inbox, tools, profile, mt_access):
        self.bot, self.db, self.routes, self.labels = bot, db, routes, labels
        self.product, self.inbox, self.tools, self.profile = product, inbox, tools, profile
        self.mt_access = mt_access
        self.button_icons_enabled = True
        self.text_icons_enabled = True
        self.banners = banner_cache(bot)
        self.current_message = ContextVar('nyan_public_message', default=None)

    def edit_current(self, handler):
        @wraps(handler)
        async def wrapped(call):
            token = self.current_message.set(getattr(call, 'message', None))
            try:
                return await handler(call)
            finally:
                self.current_message.reset(token)
        return wrapped

    def button(self, text, **kwargs):
        # Verified against https://t.me/addemoji/TgAndroidIcons (2026-10-15).
        action = kwargs.get('callback_data', '').removeprefix('ny:')
        icons = {
            'connect': '5877465816030515018',
            'status': TEXT_EMOJI_IDS['success'][0],
            'notifications': TEXT_EMOJI_IDS['notifications'][0],
            'help': TEXT_EMOJI_IDS['question'][0],
            'data': '5778423822940114949',
            'profiles': TEXT_EMOJI_IDS['profiles'][0],
            'rules': TEXT_EMOJI_IDS['events'][0],
            'stop': TEXT_EMOJI_IDS['disabled'][0],
            'resume': TEXT_EMOJI_IDS['success'][0],
            'erase': '5879896690210639947',
            'confirm': '5879896690210639947',
            'unrule': '5879896690210639947',
            'paused': '5909123362839335003',
            'edits': TEXT_EMOJI_IDS['history'][0],
            'deletions': '5879896690210639947',
            'peers': TEXT_EMOJI_IDS['peer_messages'][0],
            'own': TEXT_EMOJI_IDS['account'][0],
            'menu': TEXT_EMOJI_IDS['back'][0],
            'privacy': '5778423822940114949',
            'stats': TEXT_EMOJI_IDS['chat_stats'][0],
            'toggle:paused': '5909123362839335003',
            'toggle:edits': TEXT_EMOJI_IDS['history'][0],
            'toggle:deletions': '5879896690210639947',
            'toggle:peers': TEXT_EMOJI_IDS['peer_messages'][0],
            'toggle:own': TEXT_EMOJI_IDS['account'][0],
        }
        icon_id = TEXT_EMOJI_IDS['chat_stats'][0] if kwargs.get('web_app') else icons.get(action, icons.get(action.split(':')[1] if action.startswith('set:') else action.split(':')[0]))
        if text == 'Назад':
            icon_id = TEXT_EMOJI_IDS['back'][0]
        elif text == 'Ещё правила':
            icon_id = '5877468380125990242'
        elif text == 'Отмена':
            icon_id = TEXT_EMOJI_IDS['back'][0]
        if self.button_icons_enabled and icon_id:
            kwargs['icon_custom_emoji_id'] = icon_id
        return types.InlineKeyboardButton(text, **kwargs)

    async def send(self, actor, text, banner=None, **kwargs):
        if banner and '\n' in text and 'parse_mode' not in kwargs:
            text = menu_html(text)
            kwargs['parse_mode'] = 'HTML'
        caption_fits = telegram_length(text, kwargs.get('parse_mode')) <= 900
        image_path = banner_for(banner)
        current = self.current_message.get()
        editable = current is not None and getattr(current, 'message_id', None) is not None
        # Auth checks happen before rendering; never edit a message outside this user's chat.
        editable = editable and getattr(getattr(current, 'chat', None), 'id', None) == actor
        async def edit(method, *args, **options):
            try:
                return True, await self._send_with_icons(method, *args, chat_id=actor,
                    message_id=current.message_id, **options)
            except ApiTelegramException as exc:
                description = str(exc.description).lower()
                if exc.error_code == 400 and 'message is not modified' in description:
                    # Callback snapshots can be older than the current server-side photo.
                    # Do not overwrite a banner's cached file ID with that stale snapshot.
                    return True, None
                if exc.error_code == 400 and any(term in description for term in (
                        'message to edit not found', "message can't be edited", 'message can not be edited', 'message_id_invalid')):
                    return False, None
                raise
        if image_path:
            async def render(image):
                if caption_fits:
                    if editable and getattr(current, 'photo', None):
                        photos = current.photo
                        if isinstance(image, str) and image == photos[-1].file_id:
                            done, result = await edit(self.bot.edit_message_caption, text, **kwargs)
                        else:
                            options = dict(kwargs)
                            mode = options.pop('parse_mode', None)
                            media = types.InputMediaPhoto(image, caption=text, parse_mode=mode)
                            done, result = await edit(self.bot.edit_message_media, media, **options)
                        if done:
                            return result
                        if hasattr(image, 'seek'):
                            image.seek(0)
                    return await self._send_with_icons(self.bot.send_photo, actor, image, caption=text, **kwargs)
                # Keep the complete privacy/help text; photo captions have a smaller limit.
                return await self.bot.send_photo(actor, image)
            result = await self.banners.use(image_path, render)
            if caption_fits:
                return result
        elif editable and caption_fits:
            method = self.bot.edit_message_caption if getattr(current, 'photo', None) else self.bot.edit_message_text
            done, result = await edit(method, text, **kwargs)
            if done:
                return result
        return await self._send_with_icons(self.bot.send_message, actor, text, **kwargs)

    async def _send_with_icons(self, method, *args, **kwargs):
        has_text_icons = any(isinstance(value, str) and '<tg-emoji ' in value
                             for value in (*args, kwargs.get('caption', ''), kwargs.get('text', '')))
        has_text_icons = has_text_icons or any(isinstance(value, types.InputMediaPhoto)
            and '<tg-emoji ' in (value.caption or '') for value in args)
        def plain_text():
            def clean(value):
                if isinstance(value, str):
                    return without_custom_emoji(value)
                if isinstance(value, types.InputMediaPhoto):
                    value = copy(value)
                    value.caption = without_custom_emoji(value.caption or '')
                return value
            options = dict(kwargs)
            for key in ('caption', 'text'):
                if key in options:
                    options[key] = clean(options[key])
            return tuple(clean(value) for value in args), options
        markup = kwargs.get('reply_markup')
        has_icons = markup is not None and any(getattr(b, 'icon_custom_emoji_id', None) for row in markup.keyboard for b in row)
        def plain_markup():
            # Copy the keyboard so a failed request never mutates a reused menu.
            data = markup.to_dict()
            for row in data['inline_keyboard']:
                for item in row:
                    item.pop('icon_custom_emoji_id', None)
            return types.InlineKeyboardMarkup.de_json(data)
        if has_icons and not self.button_icons_enabled:
            kwargs['reply_markup'] = plain_markup()
            has_icons = False
        if has_text_icons and not self.text_icons_enabled:
            args, kwargs = plain_text()
            has_text_icons = False
        try:
            return await method(*args, **kwargs)
        except ApiTelegramException as exc:
            description = str(exc.description).lower()
            if not ((has_icons or has_text_icons) and exc.error_code == 400 and any(term in description for term in ('custom_emoji', 'custom emoji', 'premium', 'button_icon', 'icon_custom'))):
                raise
            self.button_icons_enabled = False
            self.text_icons_enabled = False
            if has_icons:
                kwargs['reply_markup'] = plain_markup()
            args, kwargs = plain_text()
            for value in args:
                if hasattr(value, 'seek'):
                    value.seek(0)
                elif isinstance(value, types.InputMediaPhoto) and hasattr(value.media, 'seek'):
                    value.media.seek(0)
            return await method(*args, **kwargs)

    def keyboard(self, allowed=True):
        kb = types.InlineKeyboardMarkup()
        if allowed:
            if os.getenv('WEBAPP_URL'):
                kb.row(self.button('Открыть статистику', web_app=types.WebAppInfo(os.environ['WEBAPP_URL'])))
            kb.row(self.button('Подключить аккаунт', callback_data='ny:connect'),
                   self.button('Состояние бота', callback_data='ny:status'))
            kb.row(self.button('Настроить уведомления', callback_data='ny:notifications'))
            kb.row(self.button('Мои данные', callback_data='ny:data'),
                   self.button('Помощь', callback_data='ny:help'))
        else:
            kb.row(self.button('Проверить приглашение', callback_data='ny:menu'))
            kb.row(self.button('Помощь', callback_data='ny:help'),
                   self.button('Данные и приватность', callback_data='ny:privacy'))
        return kb

    def back(self, kb, target='menu', label='В главное меню'):
        kb.row(self.button(label, callback_data='ny:'+target))
        return kb

    def link(self, kb, title, page):
        url = public_url(page)
        if url:
            kb.row(self.button(title, url=url))
        return bool(url)

    def account_name(self, account):
        return str(self.labels.get(account) or f'Аккаунт · {str(account)[-4:]}')

    async def welcome(self, actor, invited=False):
        allowed = actor in self.routes
        if allowed:
            states = self.product.states(self.routes[actor])
            active = sum(r['state']=='connected' for r in states)
            if active:
                next_step = 'Бот получает сообщения. Откройте статистику или выберите, какие уведомления присылать.'
            elif any(r['state']=='paused' for r in states):
                next_step = 'Сохранение новых сообщений приостановлено. Возобновить его можно в разделе «Мои данные».'
            elif any(r['state']=='waiting' for r in states):
                next_step = 'Аккаунт подключён. Отправьте сообщение в выбранном чате, чтобы проверить работу бота.'
            else:
                next_step = 'Начните с кнопки «Подключить аккаунт». После подключения здесь появятся ваши данные.'
            text = ('Nyan message\n\n'
                    'Показывает правки, уведомляет об удалённых сообщениях и считает статистику ваших чатов.\n\n'
                    + ('Приглашение принято. ' if invited else '') + next_step + '\n\n'
                    'Новые сообщения учитываются с момента подключения. Старую переписку можно добавить в статистику через импорт в Mini App.')
        else:
            text = ('Добро пожаловать в Nyan\n\n'
                    'Бот показывает правки, удаления и статистику выбранных чатов. Сейчас доступ по приглашению.\n\n'
                    f'Ваш Telegram ID: {actor}\n'
                    'Передайте этот номер человеку, который вас пригласил. Когда он откроет доступ, нажмите «Проверить приглашение».')
        kb = self.keyboard(allowed)
        if allowed and self.mt_access.actor(actor):
            kb.row(self.button('Наблюдение за профилями', callback_data='ny:profiles'))
        await self.send(actor, text, banner='menu' if allowed else 'invite', reply_markup=kb)

    @staticmethod
    def support():
        contact = os.getenv('SUPPORT_CONTACT', '').strip()
        return f'Написать в поддержку: {contact}' if contact else 'За помощью обратитесь к человеку, который пригласил вас в Nyan.'

    async def page(self, actor, action):
        if action == 'menu':
            return await self.welcome(actor)
        allowed = actor in self.routes
        owner = self.routes.get(actor)
        if not allowed and action not in ('help', 'data', 'privacy'):
            return await self.welcome(actor)
        kb = types.InlineKeyboardMarkup()
        banner = action.split(':')[0]
        if action == 'profiles':
            if not self.mt_access.actor(actor):
                return await self.send(actor, 'Наблюдение за профилями доступно по отдельному приглашению.', reply_markup=self.back(kb))
            module = self.profile()
            if module.state != 'ready':
                return await self.send(actor, 'Наблюдение за профилями сейчас недоступно. Уведомления о сообщениях и статистика продолжают работать.', banner='private_profiles', reply_markup=self.back(kb))
            current = self.current_message.get()
            if current is not None and getattr(current, 'message_id', None) is not None and getattr(current, 'photo', None) and current.chat.id == actor:
                try:
                    return await module.ui.render_main(actor, current.message_id)
                except ApiTelegramException as exc:
                    if exc.error_code != 400 or not any(term in str(exc.description).lower() for term in (
                            'message to edit not found', "message can't be edited", 'message can not be edited', 'message_id_invalid')):
                        raise
            return await module.ui.send_main(actor)
        if action == 'connect':
            text = ('Подключить аккаунт\n\n'
                    '1. В Telegram откройте Настройки → Telegram для бизнеса → Чат-боты. '
                    'В некоторых версиях раздел называется «Автоматизация чатов».\n'
                    '2. Выберите Nyan и укажите чаты, которые он будет учитывать.\n'
                    '3. Вернитесь сюда и нажмите «Проверить подключение».\n\n'
                    'Для проверки уведомлений попросите собеседника отправить сообщение в выбранном чате, изменить и удалить его. '
                    'О своих сообщениях бот по умолчанию не уведомляет. Разрешать удаление сообщений самому боту не нужно.')
            kb.row(self.button('Проверить подключение', callback_data='ny:status'))
            self.link(kb, 'Инструкция с пояснениями', 'help#connect')
        elif action == 'status':
            states = self.product.states(owner)
            names = {'not_connected':'Не подключён. Добавьте Nyan в настройках Telegram.',
                     'disconnected':'Подключение отключено. Подключите Nyan заново.',
                     'waiting':'Подключён. Ждём первое сообщение из выбранного чата.',
                     'connected':'Работает. Сообщения поступают.',
                     'paused':'Сохранение новых сообщений приостановлено вами.'}
            lines = ['Состояние бота']
            for row in states:
                lines.append(f"\n{self.account_name(row['account_id'])}\n{names[row['state']]}")
                if row.get('last_event'):
                    stamp = datetime.fromtimestamp(row['last_event'], timezone(timedelta(hours=3))).strftime('%d.%m, %H:%M МСК')
                    lines.append(f'Последнее сообщение: {stamp}')
            if not states:
                lines.append('\nПока нет подключённых аккаунтов.')
            queue = self.db.execute('SELECT COUNT(*),SUM(failed) FROM outbox WHERE recipient_id=?', (owner,)).fetchone()
            failed = queue[1] or 0
            pending = queue[0] - failed
            if pending:
                lines.append(f'\nУведомлений ожидают отправки: {pending}. Бот отправит их по очереди.')
            if failed:
                lines.append(f'\nНе удалось доставить уведомлений: {failed}. Откройте «Помощь», чтобы разобраться.')
            text = '\n'.join(lines)
            kb.row(self.button('Обновить состояние', callback_data='ny:status'))
            if any(r['state'] in ('not_connected','disconnected') for r in states) or not states:
                kb.row(self.button('Подключить аккаунт', callback_data='ny:connect'))
            if any(r['state']=='paused' for r in states):
                kb.row(self.button('Управлять сохранением', callback_data='ny:data'))
            kb.row(self.button('Помощь', callback_data='ny:help'))
        elif action == 'notifications':
            row = self.db.execute('SELECT * FROM notification_preferences WHERE owner_id=?', (owner,)).fetchone()
            settings = dict(row) if row else dict(paused=0, edits=1, deletions=1, peers=1)
            own = self.db.execute('SELECT only_others FROM settings WHERE recipient_id=?', (owner,)).fetchone()
            own_enabled = not (own[0] if own else True)
            event_names = [name for field,name in [('edits','правки'),('deletions','удаления')] if settings[field]]
            author_names = (['ваши'] if own_enabled else []) + (['собеседников'] if settings['peers'] else [])
            text = ('Уведомления о сообщениях\n\n'
                    + ('Отправка приостановлена.' if settings['paused'] else 'Отправка включена.')
                    + '\nСобытия: ' + (', '.join(event_names) if event_names else 'не выбраны')
                    + '\nСообщения: ' + (' и '.join(author_names) if author_names else 'авторы не выбраны')
                    + '\n\nВыберите, о каких изменениях и чьих сообщениях сообщать. '
                    'На кнопках указано действие, которое выполнится после нажатия.\n\n'
                    'Пауза отключает только доставку: сообщения продолжают сохраняться, статистика обновляется. '
                    'Уведомления за время паузы потом не придут.')
            pause_label = 'Включить уведомления' if settings['paused'] else 'Приостановить уведомления'
            kb.row(self.button(pause_label, callback_data=f"ny:set:paused:{0 if settings['paused'] else 1}"))
            for field, enabled, enable, disable in [
                ('edits',settings['edits'],'Сообщать о правках','Не сообщать о правках'),
                ('deletions',settings['deletions'],'Сообщать об удалениях','Не сообщать об удалениях'),
                ('own',own_enabled,'Сообщать о моих сообщениях','Не сообщать о моих сообщениях'),
                ('peers',settings['peers'],'Сообщать о сообщениях собеседников','Не сообщать о сообщениях собеседников')]:
                kb.row(self.button(disable if enabled else enable, callback_data=f'ny:set:{field}:{0 if enabled else 1}'))
            kb.row(self.button('Остановить сохранение сообщений…', callback_data='ny:data'))
            self.link(kb, 'Как работают уведомления', 'help#notifications')
        elif action.startswith('rules'):
            rows = self.tools().repo.active_mutes(owner)
            page = int(action.split(':')[1]) if ':' in action and action.split(':')[1].isdigit() else 0
            page = max(0, min(page, max(0, (len(rows)-1)//6)))
            text = ('Старые правила автоудаления\n\n'
                    'Автоудаление сейчас отключено: Nyan не удаляет входящие сообщения по этим правилам. '
                    'Здесь можно убрать сохранённые правила. На историю чатов это не повлияет.')
            for row in rows[page*6:page*6+6]:
                name = row['display_name'] or row['username'] or str(row['chat_id'])
                account = self.account_name(row['account_id'])
                kb.row(self.button(('Убрать правило · '+name+' · '+account)[:64], callback_data=f"ny:unrule:{row['account_id']}:{row['chat_id']}"))
            if not rows:
                text += '\n\nСохранённых правил нет.'
            if page:
                kb.row(self.button('Предыдущие правила', callback_data=f'ny:rules:{page-1}'))
            if (page+1)*6 < len(rows):
                kb.row(self.button('Следующие правила', callback_data=f'ny:rules:{page+1}'))
        elif action == 'data':
            states = self.product.states(owner) if allowed else []
            paused = sum(r['state']=='paused' for r in states)
            text = ('Мои данные\n\nNyan сохраняет сообщения выбранных чатов и использует их для уведомлений и статистики. '
                    'Старую переписку автоматически не загружает. JSON-экспорт можно добавить в статистику через Mini App.\n\n')
            if allowed:
                text += ('Сохранение новых сообщений остановлено.' if states and paused == len(states)
                         else 'Часть аккаунтов на паузе.' if paused else 'Сохранение новых сообщений включено для подключённых чатов.')
                text += '\n\nПауза сохранения оставляет историю на месте. Сообщения за время паузы потом не загрузятся. '
                text += 'Удаление уберёт сохранённую историю и статистику всех ваших аккаунтов в Nyan.'
                if paused < len(states):
                    kb.row(self.button('Приостановить сохранение', callback_data='ny:stop'))
                if paused or not states:
                    kb.row(self.button('Возобновить сохранение', callback_data='ny:resume'))
                kb.row(self.button('Удалить мои данные…', callback_data='ny:erase'))
            else:
                text += 'После приглашения здесь можно будет приостановить сохранение или удалить свои данные.'
            self.link(kb, 'Что хранится и как защищено', 'privacy')
            self.link(kb, 'Условия использования', 'terms')
        elif action == 'privacy':
            banner = 'data'
            text = ('Данные и приватность\n\nNyan хранит сообщения выбранных чатов, сведения о правках и удалениях, '
                    'статистику и аватарки. Оператор сервера имеет технический доступ к сохранённым данным.\n\n'
                    'Подробности о хранении, защите и удалении открываются по кнопке ниже.')
            if not self.link(kb, 'Прочитать о данных и приватности', 'privacy'):
                text = ('Данные и приватность\n\nNyan сохраняет сообщения, изменения, статистику и аватарки. '
                        'Сообщения хранятся по умолчанию 90 дней; статистика и слова остаются до удаления вами. '
                        'Оператор сервера имеет технический доступ к данным. Управление и удаление: /data.\n\n'+self.support())
            self.link(kb, 'Условия использования', 'terms')
            if allowed:
                kb.row(self.button('Управлять моими данными', callback_data='ny:data'))
        elif action == 'help':
            text = ('Помощь\n\n'
                    'Нет статистики или уведомлений? Проверьте подключение. Для проверки уведомлений попросите собеседника изменить или удалить тестовое сообщение в выбранном чате.\n\n'
                    'Хотите меньше уведомлений? Откройте их настройки. Чтобы остановить само сохранение сообщений, выберите «Мои данные».\n\n'
                    + self.support())
            if allowed:
                kb.row(self.button('Проверить подключение', callback_data='ny:status'))
                kb.row(self.button('Настроить уведомления', callback_data='ny:notifications'))
            self.link(kb, 'Ответы на вопросы', 'help')
            self.link(kb, 'Данные и приватность', 'privacy')
            self.link(kb, 'Условия использования', 'terms')
            tools = self.tools() if allowed else None
            if tools and tools.repo.active_mutes(owner):
                kb.row(self.button('Старые правила автоудаления', callback_data='ny:rules'))
        elif action == 'stats':
            banner = 'menu'
            text = 'Статистика чатов\n\nВыберите период, найдите собеседника и откройте его карточку. Старую переписку можно добавить через «Ещё → Импорт старой переписки» в Mini App.'
            if os.getenv('WEBAPP_URL'):
                kb.row(self.button('Открыть статистику', web_app=types.WebAppInfo(os.environ['WEBAPP_URL'])))
            else:
                text = 'Статистика пока недоступна: оператор ещё не подключил веб-приложение. Уведомления о сообщениях продолжают работать.'
            kb.row(self.button('Проверить подключение', callback_data='ny:status'))
        else:
            return await self.welcome(actor)
        self.back(kb)
        await self.send(actor, text, banner=banner, reply_markup=kb)

    def register(self):
        @self.bot.message_handler(commands=['start','menu','id','connect','status','chatstats','help','privacy','data','notifications','mutelist'])
        async def command(msg):
            if msg.chat.type != 'private' or msg.chat.id != msg.from_user.id:
                return
            action = (msg.text or '').split()[0].split('@')[0][1:]
            tail = (msg.text or '').split(maxsplit=1)
            if action == 'start' and len(tail)>1 and tail[1] in ('connect','status','notifications','data','help','privacy'):
                return await self.page(msg.from_user.id, tail[1])
            if action == 'id':
                return await self.send(msg.chat.id, f'<b>Ваш Telegram ID:</b> <code>{int(msg.from_user.id)}</code>', parse_mode='HTML', reply_markup=self.keyboard(msg.from_user.id in self.routes))
            if action in ('start','menu'):
                return await self.welcome(msg.from_user.id)
            return await self.page(msg.from_user.id, {'chatstats':'stats','mutelist':'rules'}.get(action,action))

        @self.bot.callback_query_handler(func=lambda call: bool(call.data and (call.data.startswith('ny:') or call.data in ('pm:nav:main','pm:nav:tools'))))
        @self.edit_current
        async def callback(call):
            actor = call.from_user.id
            if not call.message or call.message.chat.type != 'private' or call.message.chat.id != actor:
                return
            await self.bot.answer_callback_query(call.id)
            action = 'menu' if call.data.startswith('pm:') else call.data[3:]
            if actor not in self.routes:
                return await self.page(actor, action)
            owner = self.routes[actor]
            if action.startswith('unrule:'):
                try:
                    _, account, chat = action.split(':')
                    self.tools().repo.unmute(owner, int(account), int(chat))
                except (ValueError,LookupError):
                    return await self.send(actor, 'Правило не найдено или уже убрано. Откройте /mutelist заново.')
                return await self.page(actor, 'rules')
            if action.startswith(('set:','toggle:')):
                parts = action.split(':')
                if action.startswith('set:'):
                    if len(parts)!=3 or parts[1] not in ('paused','edits','deletions','peers','own') or parts[2] not in ('0','1'):
                        return await self.page(actor, 'notifications')
                    field, value = parts[1], int(parts[2])
                else:
                    if len(parts)!=2 or parts[1] not in ('paused','edits','deletions','peers','own'):
                        return await self.page(actor, 'notifications')
                    field = parts[1]
                    if field == 'own':
                        old = self.db.execute('SELECT only_others FROM settings WHERE recipient_id=?',(owner,)).fetchone()
                        value = int(bool(old[0] if old else True))
                    else:
                        old = self.db.execute('SELECT '+field+' FROM notification_preferences WHERE owner_id=?',(owner,)).fetchone()
                        value = 1-int(old[0] if old else (0 if field=='paused' else 1))
                if field == 'own':
                    self.db.execute('INSERT INTO settings(recipient_id,only_others) VALUES(?,?) ON CONFLICT(recipient_id) DO UPDATE SET only_others=excluded.only_others', (owner,1-value))
                else:
                    self.db.execute('INSERT OR IGNORE INTO notification_preferences(owner_id) VALUES(?)',(owner,))
                    self.db.execute('UPDATE notification_preferences SET '+field+'=? WHERE owner_id=?',(value,owner))
                paused = self.db.execute('SELECT paused FROM notification_preferences WHERE owner_id=?',(owner,)).fetchone()
                if paused and paused[0]:
                    self.db.execute('DELETE FROM outbox WHERE recipient_id=?',(owner,))
                self.db.commit()
                return await self.page(actor, 'notifications')
            if action in ('stop','resume'):
                self.product.collection(owner, action == 'resume')
                kb = self.back(types.InlineKeyboardMarkup(), 'data', 'Вернуться к моим данным')
                text = ('Сохранение приостановлено.\n\nИстория осталась на месте. Новые сообщения не будут сохраняться, пока вы не возобновите работу.'
                        if action=='stop' else 'Сохранение возобновлено.\n\nСохраняются новые сообщения. История за время паузы не загружается. Проверьте, что Nyan подключён к нужным чатам.')
                return await self.send(actor, text, banner='data', reply_markup=kb)
            if action == 'erase':
                nonce = secrets.token_hex(8)
                self.db.execute('INSERT OR REPLACE INTO data_confirmations VALUES(?,?,?)',(owner,nonce,int(time.time())+300))
                self.db.commit()
                kb = types.InlineKeyboardMarkup()
                kb.row(self.button('Удалить данные безвозвратно', callback_data='ny:confirm:'+nonce, style='danger'))
                kb.row(self.button('Отмена, оставить данные', callback_data='ny:data'))
                return await self.send(actor, 'Удалить мои данные?\n\nNyan удалит сохранённые сообщения, статистику, слова, аватарки и историю профилей всех ваших аккаунтов. Сохранение новых сообщений остановится.\n\nВосстановить удалённую историю в Nyan нельзя. Уже отправленные уведомления останутся в Telegram.\n\nЕсли вы хотите только сделать перерыв, нажмите «Отмена» и приостановите сохранение.', banner='erase', reply_markup=kb)
            if action.startswith('confirm:'):
                row = self.db.execute('SELECT * FROM data_confirmations WHERE owner_id=?',(owner,)).fetchone()
                if not row or row['expires_at'] < time.time() or not secrets.compare_digest(row['nonce'],action[8:]):
                    return await self.send(actor, 'Время подтверждения истекло. Если вы всё ещё хотите удалить данные, откройте «Мои данные» заново.', reply_markup=self.back(types.InlineKeyboardMarkup(),'data','Открыть мои данные'))
                self.product.erase(owner, self.inbox)
                self.db.execute('DELETE FROM data_confirmations WHERE owner_id=?',(owner,))
                self.db.commit()
                return await self.send(actor, 'Данные удалены. Сохранение новых сообщений остановлено.\n\nЕсли захотите пользоваться Nyan снова, откройте «Мои данные» и возобновите сохранение. Статистика начнётся заново.', banner='erase', reply_markup=self.back(types.InlineKeyboardMarkup(),'data','Открыть мои данные'))
            await self.page(actor, action)
