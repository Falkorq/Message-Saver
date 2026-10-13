import functools
import logging
import json
import time
import re
from html import unescape
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from telebot import types
from telebot.asyncio_helper import ApiTelegramException

from profile_monitor.style import icon, styled_button, truncate_html, TEXT_EMOJI_IDS
from .service import format_until, duration_text, STAT_VIEWS
from .analytics import PERIODS


log = logging.getLogger(__name__)
PAGE_SIZE = 6
MUTELIST_LIMIT = 40
MOSCOW = timezone(timedelta(hours=3))
SECTIONS = {'overview':'Обзор', 'replies':'Ответы', 'activity':'Активность',
            'events':'Удаления и правки', 'media':'Медиа', 'words':'Слова',
            'records':'Рекорды', 'info':'Об учёте'}


def activity_time(value):
    return datetime.fromtimestamp(value, MOSCOW).strftime("%d.%m %H:%M") if value else "—"


def button(text, data):
    label, icon_id = styled_button(text.removesuffix(" ✓"))
    if icon_id is None and data.startswith('bt:filter:account:'):
        icon_id = TEXT_EMOJI_IDS['account'][0]
    elif icon_id is None and data.startswith('bt:detail:open:'):
        icon_id = TEXT_EMOJI_IDS['peer_messages'][0]
    elif icon_id is None and data.startswith('bt:screen:period:'):
        icon_id = TEXT_EMOJI_IDS['date'][0]
    elif icon_id is None and data.startswith('bt:detail:menu:'):
        icon_id = TEXT_EMOJI_IDS['list'][0]
    if text.endswith(" ✓"): label += " ✓"
    return types.InlineKeyboardButton(label, callback_data=data, icon_custom_emoji_id=icon_id)


def keyboard(*rows):
    markup = types.InlineKeyboardMarkup()
    for row in rows:
        markup.row(*row)
    return markup


def caption_pages(text, limit=850):
    """Preserve whole HTML lines and make long statistics accessible with buttons."""
    pages=[]; lines=[]; length=0
    for line in text.split('\n'):
        line = truncate_html(line,limit)
        visible = len(unescape(re.sub(r'<[^>]+>','',line)).encode('utf-16-le'))//2
        if lines and length+visible+1>limit:
            pages.append('\n'.join(lines)); lines=[]; length=0
        lines.append(line); length+=visible+1
    pages.append('\n'.join(lines))
    return pages


def guarded_callback(bot):
    """Dismiss the loading indicator even when rendering fails, and never crash on forged data."""
    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(call):
            text = None
            try:
                return await handler(call)
            except (ValueError, IndexError, KeyError, LookupError):
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


class BusinessToolsUI:
    def __init__(self, bot, service, repository, routes, placeholder_path):
        self.bot = bot
        self.service = service
        self.repo = repository
        self.routes = routes
        self.placeholder = Path(placeholder_path)

    def register(self):
        bot = self.bot
        guard = guarded_callback(bot)

        @bot.message_handler(commands=["mute", "unmute", "mutelist", "chatstats"])
        async def commands(msg):
            if not self.authorized_message(msg):
                return
            await self.handle_command(msg)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("bt:mutes:"))
        @guard
        async def mutes_callback(call):
            if not await self.authorized_callback(call): return
            await self.render_mutes(call.message.chat.id, call.message.message_id, int(call.data.split(":")[2]))

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("bt:unmute:"))
        @guard
        async def unmute_callback(call):
            if not await self.authorized_callback(call): return
            parts = call.data.split(":")
            if len(parts) != 5:
                raise ValueError("bad callback data")
            _, _, account_id, chat_id, page = parts
            self.repo.unmute(self.owner(call.from_user.id), int(account_id), int(chat_id))
            await self.render_mutes(call.message.chat.id, call.message.message_id, int(page))
            call._answered = True
            await bot.answer_callback_query(call.id, "✅ Мут снят")

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("bt:stats:"))
        @guard
        async def stats_callback(call):
            if not await self.authorized_callback(call): return
            parts = call.data.split(":")
            sort, page = (parts[2], int(parts[3])) if len(parts) == 4 else ("recent", int(parts[2]))
            await self.render_stats(call.message.chat.id, call.message.message_id, page, sort)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("bt:stat:"))
        @guard
        async def stat_callback(call):
            if not await self.authorized_callback(call): return
            parts = call.data.split(":")
            if len(parts) < 5:
                raise ValueError("bad callback data")
            _, _, account_id, chat_id, page = parts[:5]
            sort = parts[5] if len(parts) > 5 else "recent"
            section = parts[6] if len(parts) > 6 else "overview"
            await self.render_stat(call.message.chat.id, call.message.message_id,
                                   int(account_id), int(chat_id), int(page), sort, section)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith('bt:filter:'))
        @guard
        async def filter_callback(call):
            if not await self.authorized_callback(call): return
            _, _, field, value = call.data.split(':')
            state = self.state(call.from_user.id,call.message.message_id)
            if field=='period':
                if value not in PERIODS: raise ValueError('period')
                state['period']=value
            elif field=='account':
                account = None if value=='all' else int(value)
                self.repo.check_account(self.owner(call.from_user.id),account)
                state.update(account=account,chat=None)
            elif field=='accounts':
                state['account_page']=max(0,int(value))
                self.save_state(call.from_user.id,call.message.message_id,state)
                await self.render_accounts(call.from_user.id,call.message.message_id)
                return
            else: raise ValueError('filter')
            state.update(page=0,detail_page=0)
            self.save_state(call.from_user.id,call.message.message_id,state)
            await self.render_current(call.from_user.id,call.message.message_id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith('bt:screen:'))
        @guard
        async def screen_callback(call):
            if not await self.authorized_callback(call): return
            _,_,screen,value = call.data.split(':')
            state = self.state(call.from_user.id,call.message.message_id)
            if screen=='period':
                await self.edit(call.from_user.id,call.message.message_id,
                                f"{icon('date')} <b>Период · МСК</b>",
                                keyboard(self.period_buttons(state),[button('◀ Назад','bt:screen:return:0')]))
                return
            if screen=='return':
                await self.render_current(call.from_user.id,call.message.message_id)
                return
            if screen=='accounts':
                await self.render_accounts(call.from_user.id,call.message.message_id)
                return
            if screen=='back':
                state['chat']=None
            elif screen=='page':
                state['page']=max(0,int(value))
            elif screen=='sort':
                if value not in ('recent','messages'): raise ValueError('sort')
                state.update(sort=value,page=0)
            elif screen=='rank':
                if value not in ('active','voice','seconds','deleted','own_deleted'): raise ValueError('rank')
                state.update(ranking=value,page=0)
            elif screen in ('summary','chats','mine','rankings'):
                state.update(screen=screen,page=0,chat=None)
            else: raise ValueError('screen')
            self.save_state(call.from_user.id,call.message.message_id,state)
            await self.render_current(call.from_user.id,call.message.message_id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith('bt:detail:'))
        @guard
        async def detail_callback(call):
            if not await self.authorized_callback(call): return
            _,_,field,value = call.data.split(':')
            state = self.state(call.from_user.id,call.message.message_id)
            if field=='menu':
                if state['chat'] is None: raise ValueError('chat')
                self.repo.check_account(self.owner(call.from_user.id),state['detail_account'])
                rows=[]
                entries=list(SECTIONS.items())
                for i in range(0,len(entries),2):
                    rows.append([button(label,f'bt:detail:tab:{key}') for key,label in entries[i:i+2]])
                rows.append([button('◀ Назад','bt:screen:return:0')])
                await self.edit(call.from_user.id,call.message.message_id,
                                f"{icon('list')} <b>Раздел статистики</b>",keyboard(*rows))
                return
            if field=='open':
                account,chat = map(int,value.split(','))
                self.repo.check_account(self.owner(call.from_user.id),account)
                if not self.repo.stat(self.owner(call.from_user.id),account,chat): raise LookupError('chat')
                state.update(detail_account=account,chat=chat,section='overview',side='both',detail_page=0)
            elif field=='tab':
                if value not in SECTIONS: raise ValueError('tab')
                state.update(section=value,detail_page=0)
            elif field=='side':
                if value not in ('both','o','p'): raise ValueError('side')
                state.update(side=value,detail_page=0)
            elif field=='view':
                if value not in STAT_VIEWS.get(state['section'],{}): raise ValueError('view')
                state.setdefault('views',{})[state['section']]=value
                state['detail_page']=0
            elif field=='page': state['detail_page']=max(0,int(value))
            else: raise ValueError('detail')
            self.save_state(call.from_user.id,call.message.message_id,state)
            await self.render_current(call.from_user.id,call.message.message_id)

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith('bt:caption:'))
        @guard
        async def caption_callback(call):
            if not await self.authorized_callback(call): return
            state=self.state(call.from_user.id,call.message.message_id)
            state['caption_page']=max(0,int(call.data.split(':')[2]))
            self.save_state(call.from_user.id,call.message.message_id,state)
            await self.render_current(call.from_user.id,call.message.message_id,keep_caption=True)

    def state(self, actor_id, message_id):
        row = self.repo.db.execute('SELECT state FROM business_stats_ui WHERE actor_id=? AND message_id=?',
                                   (actor_id,message_id)).fetchone()
        state = json.loads(row[0]) if row else dict(account=None,period='all',screen='chats',page=0,
                                                   sort='recent',ranking='active',chat=None,section='overview',
                                                   side='both',detail_page=0,account_page=0)
        self.repo.check_account(self.owner(actor_id),state['account'])
        return state

    def save_state(self, actor_id, message_id, state):
        self.repo.db.execute('INSERT INTO business_stats_ui VALUES(?,?,?,?) ON CONFLICT(actor_id,message_id) '
                             'DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at',
                             (actor_id,message_id,json.dumps(state),int(time.time())))
        self.repo.db.execute('DELETE FROM business_stats_ui WHERE updated_at<?',(int(time.time())-30*86400,))
        self.repo.db.commit()

    async def render_current(self, actor_id, message_id, keep_caption=False):
        state = self.state(actor_id,message_id)
        if not keep_caption: state['caption_page']=0
        if state['chat'] is not None:
            await self.render_stat(actor_id,message_id,state['detail_account'],state['chat'],state['page'],
                                   state['sort'],state['section'],state)
        else:
            await self.render_stats(actor_id,message_id,state['page'],state['sort'],state)

    def period_buttons(self, state):
        return [button(label+(' ✓' if key==state['period'] else ''),f'bt:filter:period:{key}')
                for key,label in PERIODS.items()]

    async def render_accounts(self, actor_id, message_id):
        state = self.state(actor_id,message_id)
        accounts = self.repo.check_account(self.owner(actor_id))
        page = min(state['account_page'],max(0,(len(accounts)-1)//6))
        buttons = [[button('Все свои аккаунты','bt:filter:account:all')]]
        buttons += [[button(self.account_name(a),f'bt:filter:account:{a}')] for a in accounts[page*6:(page+1)*6]]
        nav=[]
        if page: nav.append(button('◀',f'bt:filter:accounts:{page-1}'))
        if (page+1)*6<len(accounts): nav.append(button('▶',f'bt:filter:accounts:{page+1}'))
        if nav: buttons.append(nav)
        buttons.append([button('◀ Назад','bt:screen:back:0')])
        await self.edit(actor_id,message_id,f"{icon('account')} <b>Выбор аккаунта</b>",keyboard(*buttons))

    async def handle_command(self, msg):
        owner_id = self.owner(msg.from_user.id)
        command, *tail = (msg.text or "").split(maxsplit=1)
        command = command.split("@")[0]
        args = tail[0].split() if tail else []
        try:
            if command == "/mute":
                if not args:
                    text = f"{icon('question')} Использование: <code>/mute @username 1h</code>"
                else:
                    row, until = self.service.mute_target(owner_id, args[0], args[1] if len(args) > 1 else None)
                    text = f"{icon('disabled')} {escape(self.service.chat_name(row))} в муте {format_until(until)}"
            elif command == "/unmute":
                if not args:
                    text = f"{icon('question')} Использование: <code>/unmute @username</code>"
                else:
                    row, removed = self.service.unmute_target(owner_id, args[0])
                    text = (f"{icon('success')} Мут снят с {escape(self.service.chat_name(row))}" if removed
                            else f"{icon('warning')} Пользователь не найден или не находится в муте")
            elif command == "/mutelist":
                text = self.mutes_text(owner_id)
            else:
                if not args:
                    view = self.service.overview(owner_id,sort='messages')
                    text = view['text'] + '\n\n' + '\n'.join(
                        f"{escape(self.service.chat_name(r))} · {r['total_messages']}" for r in view['rows'])
                    text += '\n\nПодробно: <code>/chatstats @username</code> или «Статистика» в /menu'
                else:
                    if len(args) > 1 and not args[1].isdigit():
                        raise ValueError("После имени чата укажите числовой ID аккаунта")
                    account_id = int(args[1]) if len(args) > 1 else None
                    matches = self.repo.matching_chats(owner_id, args[0], account_id)
                    if len(matches) == 1:
                        row = matches[0]
                        text = self.service.stats_text(owner_id, row["account_id"], row["chat_id"])
                    elif matches:
                        choices = "\n".join(
                            f"• {escape(self.account_name(row['account_id']))}: "
                            f"<code>/chatstats {escape(args[0])} {row['account_id']}</code>"
                            for row in matches
                        )
                        text = f"{icon('info')} Этот чат есть у нескольких аккаунтов:\n{choices}"
                    else:
                        text = f"{icon('warning')} Чат не найден"
        except (ValueError, LookupError) as exc:
            text = f"{icon('warning')} {escape(str(exc))}"
        await self.bot.send_message(msg.chat.id, text, parse_mode="HTML")

    async def render_mutes(self, chat_id, message_id, page):
        rows = self.repo.active_mutes(self.owner(chat_id))
        total = len(rows); page = max(0, min(page, max(0, (total - 1) // PAGE_SIZE)))
        shown = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        lines = [f"{icon('disabled')} <b>Муты</b>", ""]
        buttons = []
        for row in shown:
            name = self.service.chat_name(row)
            lines.append(f"{escape(name)} — {format_until(row['until_at'])}")
            if row["last_error"]:
                lines.append(f"{icon('warning')} Ошибка удаления: {escape(row['last_error'])}")
            buttons.append([button("Снять мут · " + name[:24],
                                   f"bt:unmute:{row['account_id']}:{row['chat_id']}:{page}")])
        if not shown:
            lines.append(f"{icon('disabled')} Список мутов пуст")
        nav = []
        if page: nav.append(button("◀", f"bt:mutes:{page-1}"))
        if (page + 1) * PAGE_SIZE < total: nav.append(button("▶", f"bt:mutes:{page+1}"))
        if nav: buttons.append(nav)
        buttons.append([button("◀ Назад", "pm:nav:tools")])
        await self.edit(chat_id, message_id, "\n".join(lines), keyboard(*buttons))

    async def render_stats(self, chat_id, message_id, page, sort="recent", state=None):
        state = state or self.state(chat_id,message_id)
        state.update(page=page,sort=sort,chat=None)
        view = self.service.overview(self.owner(chat_id),state['account'],state['period'],state['screen'],
                                     state['sort'],state['page'],state['ranking'])
        state['page']=view['page']
        self.save_state(chat_id,message_id,state)
        buttons = [[button('Аккаунт','bt:screen:accounts:0'),
                    button(PERIODS[state['period']],'bt:screen:period:0')]]
        buttons += [[button('Сводка','bt:screen:summary:0'),button('Чаты','bt:screen:chats:0')],
                    [button('Моя статистика','bt:screen:mine:0'),button('Рейтинги','bt:screen:rankings:0')]]
        if state['screen'] in ('chats','rankings'):
            for row in view['rows']:
                metric={'voice':'p_media_voice','seconds':'p_voice_seconds','deleted':'p_deleted','own_deleted':'o_deleted'}
                rank=state['ranking']
                score=(row['peer_messages'] if rank=='active' else row['metrics'][metric[rank]]) if state['screen']=='rankings' else row['total_messages']
                value=duration_text(score) if state['screen']=='rankings' and rank=='seconds' else str(score)
                if state['screen']=='rankings' and rank=='seconds' and row['metrics']['p_voice_missing']:
                    value+='*'
                buttons.append([button(self.service.chat_name(row)[:18]+' ·'+str(row['account_id'])[-4:]+' · '+value,
                                       f"bt:detail:open:{row['account_id']},{row['chat_id']}")])
            if state['screen']=='chats':
                buttons.append([button('Недавние','bt:screen:sort:recent'),button('По сообщениям','bt:screen:sort:messages')])
            else:
                buttons += [[button(label,f'bt:screen:rank:{value}') for value,label in group] for group in (
                    [('active','Активность'),('voice','Голосовые'),('seconds','Длительность')],
                    [('deleted','Удалено: собеседник'),('own_deleted','Удалено: вы')])]
            nav=[]
            if view['page']: nav.append(button('◀',f"bt:screen:page:{view['page']-1}"))
            if (view['page']+1)*6<view['total']: nav.append(button('▶',f"bt:screen:page:{view['page']+1}"))
            if nav: buttons.append(nav)
        buttons.append([button('◀ Назад','pm:nav:tools')])
        await self.edit_stats(chat_id,message_id,view['text'],buttons,state)

    async def render_stat(self, chat_id, message_id, account_id, target_chat_id, page,
                          sort="recent", section="overview", state=None):
        state = state or self.state(chat_id,message_id)
        state.update(detail_account=account_id,chat=target_chat_id,page=page,sort=sort,section=section)
        self.repo.check_account(self.owner(chat_id),account_id)
        if not self.repo.stat(self.owner(chat_id),account_id,target_chat_id): raise LookupError('chat')
        data = self.repo.analytics(self.owner(chat_id),account_id,target_chat_id,state['period'],
                                   side=state['side'],page=state['detail_page'])
        views=STAT_VIEWS.get(section,{})
        view=state.get('views',{}).get(section,next(iter(views)) if views else None)
        media_count=0
        if section=='media' and view=='count':
            if state['side']=='both' and state['period']=='all':
                media_count=len(json.loads(data['legacy'][0]['media_json']))
            else:
                sides=('o','p') if state['side']=='both' else (state['side'],)
                media_count=len({k[len(f'{s}_media_'):] for s in sides for k,n in data['metrics'].items()
                                 if n and k.startswith(f'{s}_media_')})
        total_pages = (max(1,(data['word_count']+4)//5) if section=='words' else
                       max(1,(len(data['activity_days'])+4)//5) if section=='activity' and view=='days' else
                       max(1,(media_count+4)//5) if section=='media' and view=='count' else
                       3 if section=='info' else 1)
        state['detail_page']=min(state['detail_page'],total_pages-1)
        self.save_state(chat_id,message_id,state)
        text = self.service.stats_text(self.owner(chat_id),account_id,target_chat_id,section,
                                       state['period'],state['side'],state['detail_page'],view=view)
        buttons=[[button('Раздел','bt:detail:menu:0'),
                  button(PERIODS[state['period']],'bt:screen:period:0')]]
        if views:
            buttons.append([button(label+(' ✓' if key==view else ''),f'bt:detail:view:{key}')
                            for key,label in views.items()])
        if section in ('words','media'):
            buttons.append([button(label+(' ✓' if value==state['side'] else ''),f'bt:detail:side:{value}')
                            for value,label in [('o','Вы'),('p','Собеседник'),('both','Оба')]])
        nav=[]
        if state['detail_page']: nav.append(button('◀',f"bt:detail:page:{state['detail_page']-1}"))
        if state['detail_page']+1<total_pages: nav.append(button(f"{state['detail_page']+1}/{total_pages} ▶",f"bt:detail:page:{state['detail_page']+1}"))
        if nav: buttons.append(nav)
        buttons.append([button('Об учёте','bt:detail:tab:info'),button('◀ Назад','bt:screen:back:0')])
        await self.edit_stats(chat_id,message_id,text,buttons,state)

    async def edit_stats(self, chat_id, message_id, text, buttons, state):
        pages=caption_pages(text)
        page=min(state.get('caption_page',0),len(pages)-1)
        if len(pages)>1:
            nav=[]
            if page: nav.append(button('◀ Текст',f'bt:caption:{page-1}'))
            if page+1<len(pages): nav.append(button('Текст ▶',f'bt:caption:{page+1}'))
            buttons.insert(-1,nav)
        await self.edit(chat_id,message_id,pages[page],keyboard(*buttons))

    def account_name(self, account_id):
        label = self.service.account_labels.get(account_id, str(account_id))
        return f"{label} · {str(account_id)[-4:]}"

    def mutes_text(self, owner_id):
        rows = self.repo.active_mutes(owner_id)
        if not rows:
            return f"{icon('info')} Список мутов пуст"
        hidden = len(rows) - MUTELIST_LIMIT
        lines = [f"{escape(self.service.chat_name(row))} — {format_until(row['until_at'])}"
                 for row in rows[:MUTELIST_LIMIT]]
        if hidden > 0:
            lines.append(f"… и ещё {hidden} (полный список — «Муты» в /menu)")
        return f"{icon('disabled')} <b>Муты</b>\n\n" + "\n".join(lines)

    async def edit(self, chat_id, message_id, text, markup):
        with open(self.placeholder, "rb") as image:
            media = types.InputMediaPhoto(image, caption=truncate_html(text, 900), parse_mode="HTML")
            try:
                await self.bot.edit_message_media(media, chat_id, message_id, reply_markup=markup)
            except ApiTelegramException as exc:
                if exc.error_code != 400 or "message is not modified" not in str(exc).lower():
                    raise

    def authorized_message(self, msg):
        return msg.chat.type == "private" and msg.from_user and msg.from_user.id in self.routes

    async def authorized_callback(self, call):
        if call.from_user.id in self.routes and call.message.chat.id == call.from_user.id:
            return True
        call._answered = True
        try:
            await self.bot.answer_callback_query(call.id, "⚠️ Нет доступа", show_alert=True)
        except Exception:
            log.exception("Не удалось ответить на callback %s", call.id)
        return False

    def owner(self, actor_id):
        return self.routes[actor_id]
