"""Public invitation, connection, notification and data flows."""
import json
import os
import secrets
import time
from datetime import datetime, timezone, timedelta
from telebot import types


class PublicUI:
    def __init__(self, bot, db, routes, labels, product, inbox, tools, profile, mt_access):
        self.bot, self.db, self.routes, self.labels = bot, db, routes, labels
        self.product, self.inbox, self.tools, self.profile = product, inbox, tools, profile
        self.mt_access = mt_access

    def keyboard(self, allowed=True):
        kb = types.InlineKeyboardMarkup()
        if allowed:
            url = os.getenv('WEBAPP_URL')
            if url:
                kb.row(types.InlineKeyboardButton('Открыть статистику', web_app=types.WebAppInfo(url)))
            kb.row(types.InlineKeyboardButton('Подключение', callback_data='ny:connect'),
                   types.InlineKeyboardButton('Проверить состояние', callback_data='ny:status'))
            kb.row(types.InlineKeyboardButton('Уведомления', callback_data='ny:notifications'))
        kb.row(types.InlineKeyboardButton('Помощь', callback_data='ny:help'),
               types.InlineKeyboardButton('Что хранится', callback_data='ny:data'))
        return kb

    async def welcome(self, actor, invited=False):
        allowed = actor in self.routes
        text = ('Nyan message показывает правки, удаления и статистику сообщений из выбранных чатов.\n'
                'История учитывается после подключения Nyan; старую переписку Telegram не загружаем.\n\n')
        if allowed:
            text += ('Доступ открыт. ' if invited else '') + 'Подключите аккаунт и проверьте состояние. Затем откройте статистику.'
        else:
            text += ('Сейчас доступ по приглашению. Передайте этот ID человеку, который пригласил вас, '
                     f'и после подтверждения нажмите /start.\nВаш Telegram ID: {actor}\n' + self.support())
        kb=self.keyboard(allowed)
        if self.mt_access.actor(actor):
            kb.row(types.InlineKeyboardButton('Закрытые профили',callback_data='ny:profiles'))
        await self.bot.send_message(actor, text, reply_markup=kb)

    @staticmethod
    def support():
        contact = os.getenv('SUPPORT_CONTACT', '').strip()
        return f'Поддержка: {contact}' if contact else 'Контакт поддержки пока не указан. Обратитесь к человеку, который дал ссылку на Nyan.'

    async def page(self, actor, action):
        allowed = actor in self.routes
        owner = self.routes.get(actor)
        if not allowed and action not in ('help','data'):
            return await self.welcome(actor)
        kb = self.keyboard(allowed)
        if action == 'profiles':
            if not self.mt_access.actor(actor):
                return await self.bot.send_message(actor,'Нет доступа к закрытому модулю.')
            module=self.profile()
            if module.state!='ready':
                return await self.bot.send_message(actor,'Закрытый модуль временно недоступен. Основной бот и статистика работают.')
            return await module.ui.send_main(actor)
        if action == 'connect':
            text = ('Подключение Nyan\n\n1. Обновите Telegram и откройте Настройки → Telegram для бизнеса → '
                    'Чат-боты (в некоторых клиентах «Автоматизация чатов»).\n'
                    '2. Выберите этого бота и разрешите нужные личные чаты. Если раздела нет, проверьте доступность '
                    'бизнес-функций в вашем клиенте Telegram.\n'
                    '3. Вернитесь сюда и нажмите «Проверить состояние».\n\n'
                    'Для статистики и уведомлений право удалять сообщения не требуется. '
                    'Право can_read_messages относится к отметке прочтения. '
                    'Получение событий проверяем тестовым сообщением, а не этим флагом.\n'
                    'Отправьте сообщение в разрешённом чате, измените и удалите его; проверьте уведомление и статистику.')
        elif action == 'status':
            states = self.product.states(owner)
            names = dict(not_connected='не подключён — откройте «Подключение»',
                         disconnected='соединение отключено — подключите повторно',
                         waiting='подключён, ждём первое событие — отправьте тестовое сообщение',
                         connected='события поступают', paused='сбор остановлен — управление в «Что хранится»')
            lines = ['Состояние подключения']
            for row in states:
                since = datetime.fromtimestamp(row['since'], timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК')
                lines.append(f"\n{self.labels.get(row['account_id'], row['account_id'])}: {names[row['state']]}\nНачало текущего наблюдения: {since}")
            queue = self.db.execute('SELECT COUNT(*),MIN(created_at),SUM(failed) FROM outbox WHERE recipient_id=?',(owner,)).fetchone()
            lines.append(f'\nОчередь уведомлений: {queue[0]}; окончательных ошибок: {queue[2] or 0}. Повтор: /retry_failed.')
            text = '\n'.join(lines)
        elif action == 'notifications':
            row = self.db.execute('SELECT * FROM notification_preferences WHERE owner_id=?',(owner,)).fetchone()
            settings = dict(row) if row else dict(paused=0, edits=1, deletions=1, peers=1)
            own = self.db.execute('SELECT only_others FROM settings WHERE recipient_id=?',(owner,)).fetchone()
            text = ('Уведомления\nПауза прекращает отправку уведомлений, включая уже ожидающие. '
                    'Сбор сообщений и статистика продолжаются. Чтобы остановить сбор, откройте «Что хранится». '
                    'Пропущенные во время паузы уведомления не накапливаются.\n\n'
                    'Автоудаление входящих — отдельное действие. Новое включение временно закрыто. '
                    'Проверить и отключить старые правила: /mutelist.')
            for field, title in [('paused','Пауза уведомлений'),('edits','Правки'),('deletions','Удаления'),('peers','Чужие сообщения')]:
                kb.row(types.InlineKeyboardButton(f'{title}: {"да" if settings[field] else "нет"}', callback_data=f'ny:toggle:{field}'))
            kb.row(types.InlineKeyboardButton('Автоудаление: сохранённые правила',callback_data='ny:rules'))
            kb.row(types.InlineKeyboardButton('Свои сообщения: '+('нет' if own and own[0] else 'да'),callback_data='ny:toggle:own'))
        elif action.startswith('rules'):
            rows=self.tools().repo.active_mutes(owner)
            page=int(action.split(':')[1]) if ':' in action and action.split(':')[1].isdigit() else 0
            page=max(0,min(page,max(0,(len(rows)-1)//6)))
            text='Автоудаление входящих приостановлено. Новое включение закрыто. Сохранённые правила можно удалить:\n'
            for row in rows[page*6:page*6+6]:
                name=row['display_name'] or row['username'] or str(row['chat_id'])
                account=self.labels.get(row['account_id'],str(row['account_id']))
                text+=f'\n{account}: {name}'
                kb.row(types.InlineKeyboardButton(('Отключить · '+account+' · '+name)[:60],callback_data=f"ny:unrule:{row['account_id']}:{row['chat_id']}"))
            if not rows:text+='\nСохранённых правил нет.'
            if page:kb.row(types.InlineKeyboardButton('Назад',callback_data=f'ny:rules:{page-1}'))
            if (page+1)*6<len(rows):kb.row(types.InlineKeyboardButton('Ещё правила',callback_data=f'ny:rules:{page+1}'))
        elif action == 'data':
            text = ('Что хранит Nyan\n\nТексты и подписи полученных сообщений, данные чатов и авторов, '
                    'правки, удаления, счётчики, слова и небольшие аватарки нужны для уведомлений и статистики. '
                    'История до подключения не загружается. «Всё время» — доступная история Nyan.\n\n'
                    'Тексты, расчётные снимки и очереди зашифрованы AES-256-GCM. Оператор с ключом технически '
                    'может их прочитать. Имена, ID, слова, агрегаты и закрытые профили остаются полями SQLite; '
                    'защита диска хостинга должна проверяться отдельно.\n\n'
                    'Сообщения хранятся до '+os.getenv('RETENTION_DAYS','90')+' дней. '
                    'Ожидающие и ошибочные уведомления — до 7 дней, обработанные входящие номера — до 7 дней '
                    '(их содержимое удаляется сразу). Подробные события — до 90 дней; агрегаты и слова '
                    'сохраняются до удаления вами. Аватарки — до 7 дней. Закрытая история и фото профилей — до 90 дней; текущие снимки — до удаления. Входящие события с ошибкой сохраняются в ограниченной очереди до устранения причины; оператор видит её возраст.\n\n'
                    'Удаление убирает ваши данные из активной базы и останавливает сбор. Доставленные сообщения '
                    'в Telegram не удаляются. Резервные копии изолированы; оператору нужно удалить ваши данные '
                    'перед возобновлением работы восстановленной копии. Регулярные копии хранятся до 7 дней '
                    'при выполнении инструкции оператора.\n\n'+self.support())
            if allowed:
                kb.row(types.InlineKeyboardButton('Остановить сбор',callback_data='ny:stop'))
                kb.row(types.InlineKeyboardButton('Возобновить сбор новых событий',callback_data='ny:resume'))
                kb.row(types.InlineKeyboardButton('Удалить мои данные…',callback_data='ny:erase'))
        elif action == 'help':
            text = ('Nyan message\nСтатистика: /chatstats\nПодключение и тест: /connect\n'
                    'Состояние: /status\nУведомления: /notifications\nУправление данными: /data\n\n'
                    'Не получаете события? Проверьте активность соединения и разрешённые чаты. '
                    'После истечения авторизации откройте Mini App заново через /chatstats.\n\n'+self.support())
        else:
            return await self.welcome(actor)
        await self.bot.send_message(actor, text, reply_markup=kb)

    def register(self):
        @self.bot.message_handler(commands=['start','menu','id','connect','status','chatstats','help','privacy','data','notifications','mutelist'])
        async def command(msg):
            if msg.chat.type != 'private' or msg.chat.id != msg.from_user.id:
                return
            action = (msg.text or '').split()[0].split('@')[0][1:]
            tail = (msg.text or '').split(maxsplit=1)
            if action=='start' and len(tail)>1 and tail[1] in ('connect','status','notifications','data','help'):
                return await self.page(msg.from_user.id,tail[1])
            if action=='id':return await self.bot.send_message(msg.chat.id,f'Ваш Telegram ID: {msg.from_user.id}',reply_markup=self.keyboard(msg.from_user.id in self.routes))
            if action=='mutelist':action='rules'
            if action in ('start','menu','chatstats'):
                await self.welcome(msg.from_user.id)
            else:
                await self.page(msg.from_user.id, 'data' if action == 'privacy' else action)

        @self.bot.callback_query_handler(func=lambda call: bool(call.data and (call.data.startswith('ny:') or call.data in ('pm:nav:main','pm:nav:tools'))))
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
                    _,account,chat=action.split(':')
                    self.tools().repo.unmute(owner,int(account),int(chat))
                except (ValueError,LookupError):
                    return await self.bot.send_message(actor,'Правило недоступно. Откройте /notifications заново.')
                return await self.page(actor,'rules')
            if action.startswith('toggle:'):
                field = action.split(':')[1]
                if field == 'own':
                    self.db.execute('INSERT INTO settings(recipient_id,only_others) VALUES(?,1) ON CONFLICT(recipient_id) DO UPDATE SET only_others=1-only_others',(owner,))
                elif field in ('paused','edits','deletions','peers'):
                    self.db.execute('INSERT OR IGNORE INTO notification_preferences(owner_id) VALUES(?)',(owner,))
                    self.db.execute(f'UPDATE notification_preferences SET {field}=1-{field} WHERE owner_id=?',(owner,))
                paused=self.db.execute('SELECT paused FROM notification_preferences WHERE owner_id=?',(owner,)).fetchone()
                if paused and paused[0]:self.db.execute('DELETE FROM outbox WHERE recipient_id=?',(owner,))
                self.db.commit()
                return await self.page(actor, 'notifications')
            if action in ('stop','resume'):
                self.product.collection(owner, action == 'resume')
                return await self.page(actor,'status')
            if action == 'erase':
                nonce = secrets.token_hex(8)
                self.db.execute('INSERT OR REPLACE INTO data_confirmations VALUES(?,?,?)',(owner,nonce,int(time.time())+300)); self.db.commit()
                kb = types.InlineKeyboardMarkup()
                kb.row(types.InlineKeyboardButton('Подтверждаю удаление и остановку сбора',callback_data='ny:confirm:'+nonce))
                kb.row(types.InlineKeyboardButton('Отмена',callback_data='ny:data'))
                return await self.bot.send_message(actor,'Удалить сообщения, статистику, слова, очередь, аватарки и закрытые профили ваших аккаунтов? Восстановить их из Nyan нельзя. Доставленные уведомления в Telegram останутся.',reply_markup=kb)
            if action.startswith('confirm:'):
                row = self.db.execute('SELECT * FROM data_confirmations WHERE owner_id=?',(owner,)).fetchone()
                if not row or row['expires_at'] < time.time() or not secrets.compare_digest(row['nonce'],action[8:]):
                    return await self.bot.send_message(actor,'Подтверждение истекло. Откройте /data заново.')
                self.product.erase(owner, self.inbox)
                self.db.execute('DELETE FROM data_confirmations WHERE owner_id=?',(owner,)); self.db.commit()
                return await self.bot.send_message(actor,'Данные удалены из активной базы. Сбор остановлен. Новые данные начнут учитываться только после «Возобновить сбор» в /data.')
            await self.page(actor,action)
