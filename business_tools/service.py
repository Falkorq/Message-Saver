import re
import time
import logging
from datetime import datetime, timedelta, timezone
from html import escape

from profile_monitor.style import icon
from .analytics import PERIODS, day_of, histogram_summary, period_bounds
from collections import Counter
import json


log = logging.getLogger(__name__)


DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
MEDIA_NAMES = {
    "photo": "Фото", "video": "Видео", "voice": "Голосовые", "video_note": "Видеосообщения",
    "document": "Файлы", "audio": "Аудио", "sticker": "Стикеры", "animation": "GIF",
    "location": "Геопозиции", "venue": "Места", "poll": "Опросы", "contact": "Контакты",
}
MEDIA_ICONS = {
    "photo": "photos", "video": "video", "voice": "voice",
    "video_note": "video_note", "document": "document", "audio": "audio",
    "sticker": "sticker", "animation": "animation", "location": "location",
    "venue": "venue", "poll": "poll", "contact": "contact",
}


def decorate_statistics(lines):
    """One premium icon per heading; let numbers carry the rest of the screen."""
    headings = {'Ответы и инициатива': 'messages', 'Удаления и правки': 'history',
                'Активность': 'chat_list_stats', 'Рекорды доступной истории': 'chat_list_stats',
                'Об учёте': 'stat_info'}
    output=[]
    for line in lines:
        if '<b>' not in line:
            line=re.sub(r'<tg-emoji[^>]*>.*?</tg-emoji>\s*','',line)
        key=next((key for title,key in headings.items() if line==f'<b>{title}</b>'),None)
        output.append(f'{icon(key)} {line}' if key else line)
    return output


MAX_DURATION = 10 * 365 * 86400  # 10 лет; сверх этого INTEGER SQLite переполняется

STAT_VIEWS = {
    'overview': {'main':'Кратко', 'detail':'Подробнее'},
    'replies': {'normal':'Ответы', 'long':'Паузы >4 ч', 'initiative':'Инициатива'},
    'events': {'delete':'Удаления', 'edit':'Правки'},
    'activity': {'days':'Дни', 'week':'Неделя', 'time':'Часы', 'compare':'Сравнение'},
    'media': {'count':'Количество', 'duration':'Длительность'},
}


def short_date(value):
    return datetime.fromisoformat(value).strftime('%d.%m.%y')


def percent(value, total):
    return round(value*100/total, 1) if total else 0


def duration_text(seconds):
    if seconds is None:
        return '—'
    seconds = max(0, round(seconds))
    if seconds < 60: return f'{seconds} с'
    minutes = seconds//60
    if minutes < 60: return f'{minutes} мин'
    hours, minutes = divmod(minutes,60)
    return f'{hours} ч {minutes} мин'


def parse_duration(value, default="1h"):
    text = (value or default).strip().lower()
    if text in {"0", "forever", "навсегда", "∞"}:
        return 0
    matches = list(DURATION_RE.finditer(text))
    if not matches or "".join(match.group(0).replace(" ", "") for match in matches) != text.replace(" ", ""):
        raise ValueError("Неверный срок. Примеры: 30m, 2h, 1d, 1w или навсегда")
    seconds = sum(int(match.group(1)) * UNITS[match.group(2).lower()] for match in matches)
    if seconds <= 0:
        raise ValueError("Срок должен быть больше нуля")
    if seconds > MAX_DURATION:
        raise ValueError("Слишком большой срок: максимум 10 лет (или навсегда)")
    return seconds


def format_until(until_at):
    moscow = timezone(timedelta(hours=3))
    return ("навсегда" if not until_at else
            "до " + datetime.fromtimestamp(until_at, moscow).strftime("%d.%m.%Y %H:%M МСК"))


class BusinessToolsService:
    def __init__(self, bot, repository, enqueue_notification, account_labels):
        self.bot = bot
        self.repo = repository
        self.enqueue = enqueue_notification
        self.account_labels = account_labels

    async def handle_business_message(self, msg, record, account_id, owner_id):
        self.repo.check_account(owner_id, account_id)
        text = (record.get("text") or "").strip()
        own = record.get("sender_id") == account_id
        if own and text.startswith("!"):
            handled = await self._handle_chat_command(msg, record, account_id, owner_id, text)
            if handled:
                return "command"
        self.repo.record_message(
            msg.business_connection_id, account_id, owner_id, msg.message_id, record
        )
        mute = self.repo.is_muted(account_id, msg.chat.id) if not own else None
        if mute:
            previous_error = mute["last_error"]
            try:
                await self._delete(msg.business_connection_id, msg.chat.id, msg.message_id, account_id)
            except Exception as exc:
                current_error = type(exc).__name__
                if previous_error != current_error:
                    self.enqueue(
                        owner_id,
                        f"{icon('warning')} Не удалось удалить сообщение от "
                        f"{escape(self.chat_name(mute))}: {escape(current_error)}",
                    )
                raise
            return "muted"
        return None

    def record_edit(self, owner_id, account_id, key, old, new):
        self.repo.check_account(owner_id,account_id)
        self.repo.check_chat_owner(owner_id,account_id,key[1])
        with self.repo.db:
            if old:
                self.repo.record_details(key[0],account_id,key[2],dict(old,chat_id=key[1]))
            return self.repo.record_event(owner_id,account_id,*key,'edit',new)

    def record_deletion(self, owner_id, account_id, key, original=None, self_deleted=False):
        self.repo.check_account(owner_id,account_id)
        if self_deleted:
            known=self.repo.db.execute('SELECT counted FROM business_stats_seen WHERE connection_id=? AND chat_id=? AND message_id=?',key).fetchone()
            # Bot chat commands were deliberately excluded from message statistics.
            # Muted peer messages, in contrast, were counted before being deleted.
            if not known or not known[0]: return False
        return self.repo.record_event(owner_id,account_id,*key,'delete',original)

    async def _handle_chat_command(self, msg, record, account_id, owner_id, text):
        command, *rest = text.split(maxsplit=1)
        command = command.lower()
        if command not in {"!mute", "!unmute", "!mutestatus", "!stats"}:
            return False
        row = self.repo.stat(owner_id, account_id, msg.chat.id)
        chat_row = row[0] if row else {
            "owner_id": owner_id, "account_id": account_id, "chat_id": msg.chat.id,
            "display_name": record.get("chat_name"), "username": record.get("chat_username"),
        }
        try:
            if command == "!mute":
                seconds = parse_duration(rest[0] if rest else None)
                until = int(time.time()) + seconds if seconds else 0
                self.repo.mute(owner_id, chat_row, until)
                response = f"{icon('disabled')} {escape(self.chat_name(chat_row))} в муте {format_until(until)}"
            elif command == "!unmute":
                removed = self.repo.unmute(owner_id, account_id, msg.chat.id)
                response = (f"{icon('success')} Мут снят" if removed
                            else f"{icon('info')} Этот чат не находится в муте")
            elif command == "!mutestatus":
                mute = self.repo.is_muted(account_id, msg.chat.id)
                response = (f"{icon('disabled')} Мут активен {format_until(mute['until_at'])}" if mute
                            else f"{icon('info')} Мут не активен")
            else:
                response = self.stats_text(owner_id, account_id, msg.chat.id)
        except ValueError as exc:
            response = f"{icon('warning')} {escape(str(exc))}"
        try:
            await self._delete_command(msg.business_connection_id, msg.chat.id, msg.message_id)
        except Exception as exc:
            log.warning("Команда %s выполнена, но сообщение %s не удалено: %s", command, msg.message_id, exc)
            response += (f"\n{icon('warning')} Команда выполнена, но Telegram не разрешил "
                         "удалить её из чата. Проверьте права Business-бота и доступ к этому чату.")
        self.enqueue(owner_id, response)
        return True

    async def _delete_command(self, connection_id, chat_id, message_id):
        self.repo.mark_self_deleted(connection_id, chat_id, message_id)
        try:
            await self.bot.delete_business_messages(connection_id, [message_id])
        except Exception:
            self.repo.consume_self_deleted(connection_id, chat_id, message_id)
            raise

    async def _delete(self, connection_id, chat_id, message_id, account_id):
        self.repo.mark_self_deleted(connection_id, chat_id, message_id)
        try:
            await self.bot.delete_business_messages(connection_id, [message_id])
            self.repo.set_mute_error(account_id, chat_id, None)
        except Exception as exc:
            self.repo.consume_self_deleted(connection_id, chat_id, message_id)
            self.repo.set_mute_error(account_id, chat_id, exc)
            raise

    def mute_target(self, owner_id, target, duration=None):
        row = self.repo.resolve_chat(owner_id, target)
        if not row:
            raise LookupError("Чат не найден. Бот должен сначала получить хотя бы одно сообщение из этого чата")
        seconds = parse_duration(duration)
        until = int(time.time()) + seconds if seconds else 0
        self.repo.mute(owner_id, row, until)
        return row, until

    def unmute_target(self, owner_id, target):
        row = self.repo.resolve_chat(owner_id, target) or self.repo.resolve_mute(owner_id, target)
        if not row:
            return None, False
        return row, self.repo.unmute(owner_id, row["account_id"], row["chat_id"])

    def stats_text(self, owner_id, account_id, chat_id, section="overview", period="all", side="both", page=0, now=None, view=None):
        now = int(time.time()) if now is None else int(now)
        data = self.repo.analytics(owner_id, account_id, chat_id, period, now, side, page)
        if not data:
            return f"{icon('stats_empty')} Статистика пока пуста"
        row, active_days, words = data["legacy"]
        m = data["metrics"]
        days = data["days"]
        available_total = m['o_n'] + m['p_n']
        own = row['owner_messages'] if period == 'all' else m['o_n']
        peer = row['peer_messages'] if period == 'all' else m['p_n']
        total = own + peer
        selected = ('o', 'p') if side == 'both' else (side,)
        if any(s not in ('o', 'p') for s in selected):
            raise ValueError('Неизвестная сторона')
        names = {'o': 'Вы', 'p': 'Собеседник'}
        choices = STAT_VIEWS.get(section)
        view = view or (next(iter(choices)) if choices else None)
        if choices and view not in choices: raise ValueError('Неизвестный экран')
        lines = [f"{icon('chat_stats')} <b>{escape(self.chat_name(row))}</b>",
                 f"{escape(self.account_labels.get(account_id, str(account_id)))} · {PERIODS[period]} · МСК", '']
        if section == 'overview':
            if view == 'main':
                lines += [f"Сообщений: {total}", f"Вы: {own} — {percent(own,total)}%",
                          f"Собеседник: {peer} — {percent(peer,total)}%",
                          f"Активных дней: {active_days if period=='all' else sum(n>0 for n in days.values())}", '']
                if data['last'] and not data['unknown']:
                    last=data['last'];author='вы' if last['side']=='o' else 'собеседник'
                    lines.append(f"Последним написал {author} · {duration_text(max(0,now-last['sent_at']))} назад")
                else: lines.append('Последнее сообщение: контекст неизвестен')
            else:
                lines.append('<b>Длина и пик активности</b>')
                for side_key,count,chars in [('o',own,row['owner_chars'] if period=='all' else m['o_chars']),
                                             ('p',peer,row['peer_chars'] if period=='all' else m['p_chars'])]:
                    lines.append(f"Средняя длина · {names[side_key].lower()}: {round(chars/count) if count else 0} симв.")
                hours=json.loads(row['hours_json']) if period=='all' else [m[f'h_{i}'] for i in range(24)]
                if max(hours,default=0):
                    peak=hours.index(max(hours));lines.append(f"Пик: {peak:02}:00–{(peak+1)%24:02}:00 МСК")
        elif section == 'replies':
            if view == 'initiative':
                starts=m['o_starts']+m['p_starts']
                lines += ['<b>Кто начал разговор</b>',
                          f"Вы: {m['o_starts']} · {percent(m['o_starts'],starts)}%",
                          f"Собеседник: {m['p_starts']} · {percent(m['p_starts'],starts)}%",
                          '', 'После паузы >4 ч. Первый разговор исключён.']
            else:
                lines.append('<b>'+('Ответы · до 4 ч' if view=='normal' else 'Ответы · паузы >4 ч')+'</b>')
                for side_key in ('o','p'):
                    result=histogram_summary(m,f"{'reply' if view=='normal' else 'long'}_{side_key}_")
                    lines += ['',f"<b>{names[side_key]}</b> · {result['count']} ответов",
                              f"Среднее: {duration_text(result['mean'])}",f"Медиана: {duration_text(result['median'])}"]
        elif section == 'events':
            if view == 'edit':
                lines.append('<b>Правки</b>')
                for side_key in ('o','p'):
                    lines += ['', '<b>'+names[side_key]+'</b>',
                              f"Сообщений: {m[f'{side_key}_edited']}",f"Правок: {m[f'{side_key}_edits']}"]
            else:
                lines.append('<b>Удаления</b>')
                for side_key,label in [('o','ваших сообщений'),('p','сообщений собеседника')]:
                    denominator=own if side_key=='o' else peer
                    delay=histogram_summary(m,f'delete_{side_key}_')
                    lines += ['',f"Удалено {label}: {m[f'{side_key}_deleted']} · {percent(m[f'{side_key}_deleted'],denominator)}%",
                              f"До обнаружения · среднее: {duration_text(delay['mean'])}",
                              f"Медиана: {duration_text(delay['median'])}"]
                lines += ['', 'Кто удалил, неизвестно.']
            if data['unknown']: lines.append(f"Без контекста: {data['unknown']}")
        elif section == 'activity':
            if view == 'days':
                lines.append('<b>Сообщения по дням</b>')
                ordered=list(reversed(data['activity_days']))
                lines += [f"{short_date(d)}  ·  {n if n is not None else 'нет данных'}"
                          for d,n in ordered[max(0,page)*5:(max(0,page)+1)*5]] or ['Нет данных']
            elif view == 'week':
                lines.append('<b>По дням недели</b>')
                weekdays=[0]*7
                for d,n in days.items(): weekdays[datetime.fromisoformat(d).weekday()]+=n
                lines += [f'{label}  ·  {n}' for label,n in zip(('Пн','Вт','Ср','Чт','Пт','Сб','Вс'),weekdays)]
                lines += ['',f'Будни: {sum(weekdays[:5])} · выходные: {sum(weekdays[5:])}']
            elif view == 'time':
                lines.append('<b>Время суток · МСК</b>')
                lines += [f"{label}  ·  {sum(m[f'h_{h}'] for h in range(i*6,(i+1)*6))}"
                          for i,label in enumerate(('Ночь 00–06','Утро 06–12','День 12–18','Вечер 18–00'))]
            else:
                lines += ['<b>Сравнение периодов</b>',self.comparison(data,owner_id,account_id,chat_id,now)]
        elif section == 'media':
            lines.append(f"{icon('media')} <b>Медиа · {'количество' if view=='count' else 'длительность'}</b>")
            if view == 'count':
                if side=='both':
                    counts=json.loads(row['media_json']) if period=='all' else {
                        key[len('o_media_'):]:m[key]+m['p_media_'+key[len('o_media_'):]]
                        for key in m if key.startswith('o_media_')}
                    if period!='all':
                        for key in m:
                            if key.startswith('p_media_'):
                                kind=key[len('p_media_'):];counts[kind]=m['o_media_'+kind]+m[key]
                    items=[(k,n) for k,n in sorted(counts.items()) if n]
                    lines += [f'{escape(MEDIA_NAMES.get(k,k))}: {n}' for k,n in items[max(0,page)*5:(max(0,page)+1)*5]] or ['Пока нет медиа']
                    lines.append('Оба автора · '+('накопленные итоги' if period=='all' else 'доступная история'))
                else:
                    lines.append(names[side])
                    items=[(k[len(f'{side}_media_'):],n) for k,n in m.items() if k.startswith(f'{side}_media_') and n]
                    lines += [f'{escape(MEDIA_NAMES.get(k,k))}: {n}' for k,n in sorted(items)[max(0,page)*5:(max(0,page)+1)*5]] or ['Пока нет медиа']
            else:
                for side_key in selected:
                    lines += ['', '<b>'+names[side_key]+'</b>']
                    for kind in ('voice','video_note'):
                        lines.append(f"{MEDIA_NAMES[kind]}: {duration_text(m[f'{side_key}_{kind}_seconds'])}")
                        if m[f'{side_key}_{kind}_missing']:
                            lines.append(f"Неполный учёт · без длительности: {m[f'{side_key}_{kind}_missing']}")
        elif section == 'words':
            lines.append(f"{icon('top_words')} <b>Топ слов</b> · " + {'both':'Общие','o':'Ваши слова','p':'Слова собеседника'}[side])
            lines += [f"{escape(w['word'])} — {w['count']}" for w in data['words']] or ['Пока нет слов']
            lines.append(f'Страница {max(0,page)+1}')
        elif section == 'records':
            lines.append('<b>Рекорды доступной истории</b>')
            if days:
                best = max(days, key=days.get); lines.append(f'Самый активный день: {short_date(best)} · {days[best]} сообщений')
            for s in ('o','p'): lines.append(f"Самое длинное — {names[s].lower()}: {data['lengths'].get(s,0)} симв.")
            streak = best_streak = 0; previous = None
            for d in sorted(d for d,n in days.items() if n):
                date = datetime.fromisoformat(d)
                streak = streak+1 if previous and (date-previous).days==1 else 1
                best_streak = max(best_streak,streak); previous = date
            lines.append(f'Последовательных дней общения: {best_streak}')
        elif section == 'info':
            lines += [f"{icon('stat_info')} <b>Об учёте</b>",
                      'История частичная: до подключения и пропущенные события не восстановить.',
                      'Сообщения, правки и удаления относятся к дате отправки в МСК.',
                      'Ответ: от последнего сообщения серии до первого ответного. Паузы >4 ч — отдельно.',
                      'Первый доступный разговор не считается началом.',
                      'Telegram не сообщает, кто удалил. Время — обнаружение ботом.',
                      'Длительность без metadata неизвестна. Старые общие итоги сохранены.']
            if data['available']:
                lines.append('Подробная история доступна с '+day_of(data['available'])+'.')
            lines.append('Новые показатели учитываются с '+day_of(data['progress']['coverage_start'])+'.')
            lines.append(f"Подробная история: {available_total} из {row['total_messages']} сообщений.")
            explanation=lines[4:]
            lines=lines[:4]+explanation[max(0,page)*3:(max(0,page)+1)*3]
        else:
            raise ValueError('Неизвестный раздел')
        if section != 'info':
            if data['available']: lines += ['', 'Данные с '+short_date(day_of(data['available']))+' · частично']
            else: lines += ['', 'Подробных данных пока нет']
        if not data['progress'].get('backfill_complete'):
            lines.append('История восстанавливается…')
        return '\n'.join(decorate_statistics(lines))

    def comparison(self, data, owner_id, account_id, chat_id, now):
        if data['start'] is None:
            return 'Сравнение: выберите ограниченный период.'
        # Compare the same elapsed part, including today, not a complete day to a partial one.
        span = data['end']-data['start']
        previous_start = data['start']-span
        continuous = data['progress'].get(f'continuous_{account_id}',data['progress']['coverage_start'])
        if (previous_start < max(continuous,data['progress']['coverage_start']) or not data['progress'].get('backfill_complete')
                or data['unknown']):
            return 'Сравнение недоступно: предыдущая история недостаточно полная.'
        return 'Сравнение равных интервалов до текущего времени: '+self.repo.compare_counts(owner_id,account_id,chat_id,data['start'],data['end'],now)

    def overview(self, owner_id, account_id=None, period='all', screen='chats', sort='recent', page=0, ranking='active', now=None):
        self.repo.check_account(owner_id, account_id)
        rows = self.repo.filtered_chats(owner_id, account_id, period, now)
        summary = dict(chats=len(rows), messages=sum(r['total_messages'] for r in rows),
                       own=sum(r['owner_messages'] for r in rows), peer=sum(r['peer_messages'] for r in rows))
        labels = {'active':'Сообщения собеседника','voice':'Голосовые собеседника',
                  'seconds':'Длительность голосовых собеседника','deleted':'Удалено сообщений собеседника',
                  'own_deleted':'Удалено ваших сообщений'}
        if screen=='rankings':
            if ranking not in labels: raise ValueError('Неизвестный рейтинг')
            def score(r):
                return (r['peer_messages'] if ranking=='active' else r['metrics'][{
                    'voice':'p_media_voice','seconds':'p_voice_seconds','deleted':'p_deleted','own_deleted':'o_deleted'}[ranking]])
            rows.sort(key=lambda r:(-score(r),r['account_id'],r['chat_id']))
        else:
            rows.sort(key=lambda r:(-r['total_messages'],-(r['last_at'] or 0)) if sort=='messages' else (-(r['last_at'] or 0),-r['total_messages']))
        page = max(0,min(page,max(0,(len(rows)-1)//6)))
        shown = rows[page*6:(page+1)*6]
        title = {'summary':'Сводка','chats':'Чаты','mine':'Моя статистика','rankings':'Рейтинги'}[screen]
        lines = [f"{icon('chat_list_stats')} <b>Статистика чатов · {title}</b>",
                 'Аккаунт: '+escape(self.account_labels.get(account_id,str(account_id))) if account_id is not None else 'Все свои аккаунты',
                 f'Период: {PERIODS[period]} · МСК',
                 f"Чатов: {summary['chats']} · сообщений: {summary['messages']}",
                 f"Вы: {summary['own']} · собеседники: {summary['peer']}"]
        lines[1:3]=[('Все аккаунты' if account_id is None else escape(self.account_labels.get(account_id,str(account_id))))
                    +' · '+PERIODS[period]+' · МСК', '']
        if screen=='mine':
            lines=lines[:3]
            m = Counter()
            for r in rows: m.update(r['metrics'])
            lines += [f"Ваша активность: {summary['own']} сообщений",
                      f"Голосовые: {m['o_media_voice']} · {duration_text(m['o_voice_seconds'])}",
                      f"Удалено ваших сообщений: {m['o_deleted']} · правок: {m['o_edits']}",
                      f"Начали разговоры: {m['o_starts']}"]
            if m['o_voice_missing']: lines.append(f"Без длительности: {m['o_voice_missing']}")
        elif screen in ('chats','rankings'):
            if screen=='rankings': lines.append(labels[ranking])
            if screen=='rankings' and ranking=='seconds' and any(r['metrics']['p_voice_missing'] for r in shown):
                lines.append('* Неполная длительность: есть голосовые без metadata.')
            if not shown: lines.append('Пока нет чатов')
        else:
            stamp = int(time.time()) if now is None else now
            lines += ['', '<b>Впервые замеченные чаты</b>']
            for days in (7,30):
                boundary,_ = period_bounds(str(days),stamp)
                count = self.repo.first_seen_chats(owner_id,account_id,boundary)
                lines.append(f'{days} дней: {count}')
            lines.append('Впервые замеченный чат может содержать старую переписку.')
        lines += ['', 'Подробные данные · доступная история']
        return dict(text='\n'.join(decorate_statistics(lines)),rows=shown,page=page,total=len(rows),summary=summary)

    @staticmethod
    def chat_name(row):
        return ("@" + row["username"]) if row["username"] else (row["display_name"] or str(row["chat_id"]))
