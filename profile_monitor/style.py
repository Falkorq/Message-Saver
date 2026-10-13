from html import escape
from html.parser import HTMLParser


# Premium emoji used inside message text/captions.
TEXT_EMOJI_IDS = {
    "bot": ("5931415565955503486", "🤖"),
    "profiles": ("5883964170268840032", "👤"),
    "watch": ("6019295596173596341", "👁"),
    "events": ("5967456680940671207", "😀"),
    "tools": ("5875450995332353523", "🔨"),
    "settings": ("5877260593903177342", "⚙"),
    "count": ("5886412370347036129", "👤"),
    "success": ("5776375003280838798", "✅"),
    "error": ("5778527486270770928", "❌"),
    "add": ("5775937998948404844", "➕"),
    "back": ("5877629862306385808", "◀️"),
    "info": ("5879785854284599288", "ℹ️"),
    "question": ("5873121512445187130", "❓"),
    "disabled": ("5877413297170419326", "🚫"),
    "warning": ("5879813604068298387", "❗️"),
    "empty_list": ("5832546462478635761", "🔒"),
    "empty_history": ("5778335621491723621", "📷"),
    "list": ("5942877472163892475", "👥"),
    "check": ("5843799474362652262", "🔄"),
    "history": ("5839380464116175529", "✏️"),
    "card": ("5764747792371160364", "👤"),
    "date": ("5967382867632722656", "📅"),
    "last_error": ("6028226658543082010", "🔨"),
    "changed": ("5877530150345641603", "👤"),
    "popup_info": ("5879501875341955281", "ℹ️"),
    "photos": ("5775949822993371030", "🖼"),
    "time": ("5900104897885376843", "🕓"),
    "no_photos": ("5960888357390126718", "🖼"),
    "notifications": ("5909201569898827582", "🔔"),
    "interval": ("5936170807716745162", "🎛"),
    "photo_storage": ("5776134897429122849", "🖼"),
    "format": ("5886330010054168711", "📝"),
    "duration": ("5776213190387961618", "🕓"),
    "compact": ("5877332341331857066", "📁"),
    "detailed": ("6017174676898321263", "📂"),
    "saved": ("5877680341057015789", "📁"),
    "username": ("5814550759961793482", "👤"),
    "name": ("5870525453822859417", "🏷"),
    "bio": ("5854776233950188167", "🏷"),
    "avatar": ("5764747792371160364", "👤"),
    "avatar_added": ("5766879414704935108", "🖼"),
    "avatar_removed": ("5776004296063585809", "🖼"),
    "checking": ("5891243564309942507", "💬"),
    "denied": ("5881702736843511327", "⚠️"),
    "recent_changes": ("5839380464116175529", "✏️"),
    "username_info": ("5879501875341955281", "ℹ️"),
    "no_name": ("5872829476143894491", "🚫"),
    "no_username": ("5879785854284599288", "ℹ️"),
    "unknown_value": ("5873121512445187130", "❓"),
    "available_chats": ("5913702317667913862", "📊"),
    "chat_list_stats": ("5994378914636500516", "📈"),
    "chat_stats": ("5931472654660800739", "📊"),
    "stats_empty": ("5879501875341955281", "ℹ️"),
    "messages": ("5891169510483823323", "💬"),
    "account": ("5879770735999717115", "👤"),
    "owner_messages": ("5886455371559604605", "➡️"),
    "peer_messages": ("5886436057091673541", "💬"),
    "average_length": ("5985833664884250583", "📱"),
    "media": ("5877495434124988415", "📎"),
    "top_words": ("5967548335542767952", "💳"),
    "stat_info": ("5879501875341955281", "ℹ️"),
    "video": ("6005986106703613755", "📷"),
    "voice": ("5897554554894946515", "🎤"),
    "video_note": ("5891119667388354506", "📷"),
    "document": ("5875206779196935950", "📁"),
    "audio": ("5891249688933305846", "🎵"),
    "sticker": ("5784982040432611567", "🙂"),
    "animation": ("5945068566909815651", "🎞"),
    "location": ("5778661935927004845", "📍"),
    "venue": ("5886446115905082831", "📍"),
    "poll": ("5931472654660800739", "📊"),
    "contact": ("5942826671290715541", "🔎"),
    "watch_disabled": ("5962916891918864588", "👁"),
    "saved_notice": ("5839323457015256759", "📄"),
}


# Exact button label -> premium icon. The visible Unicode prefix is removed when
# a custom icon is present, preventing duplicated icons.
BUTTON_ICON_IDS = {
    "👤 Профили": "5883964170268840032",
    "📡 Наблюдение": "6019295596173596341",
    "🗃 События": "5967456680940671207",
    "🔧 Инструменты": "5875450995332353523",
    "⚙️ Настройки": "5877260593903177342",
    "➕ Добавить": "5775937998948404844",
    "👥 Список": "5942877472163892475",
    "🕘 Последние изменения": "5839042506024555846",
    "◀ Назад": "5877629862306385808",
    "◀": "5877629862306385808",
    "▶": "5839042506024555846",
    "🕘 История": "5839380464116175529",
    "🖼 Аватарки": "5775949822993371030",
    "🔄 Проверить сейчас": "5843908536467198016",
    "🔕 Отключить": "5909123362839335003",
    "🔕 Муты": "5909123362839335003",
    "Да, отключить": "5776375003280838798",
    "Отмена": "5778527486270770928",
    "🔔 Уведомления": "5909201569898827582",
    "⏱ Интервал проверки": "5936170807716745162",
    "🖼 Хранение аватарок": "5776134897429122849",
    "📝 Формат уведомлений": "5886330010054168711",
    "Включить": "5775937998948404844",
    "Выключить": "5778527486270770928",
    "5 минут": "5776213190387961618",
    "15 минут": "5776213190387961618",
    "30 минут": "5776213190387961618",
    "1 час": "5776213190387961618",
    "Хранить": "5832546462478635761",
    "Не хранить": "5778335621491723621",
    "Компактный": "5877332341331857066",
    "Подробный": "6017174676898321263",
    "📊 Статистика": "5931472654660800739",
    "📊 Обзор": "5931472654660800739",
    "📎 Медиа": "5877495434124988415",
    "💳 Топ слов": "5967548335542767952",
}


# Statistics tabs reuse the established premium emoji palette.
BUTTON_ICON_IDS.update({
    'Аккаунт': TEXT_EMOJI_IDS['account'][0],
    'Все свои аккаунты': TEXT_EMOJI_IDS['account'][0],
    'Сегодня': TEXT_EMOJI_IDS['date'][0],
    '7 дней': TEXT_EMOJI_IDS['date'][0],
    '30 дней': TEXT_EMOJI_IDS['date'][0],
    'Всё время': TEXT_EMOJI_IDS['time'][0],
    'Сводка': TEXT_EMOJI_IDS['chat_stats'][0],
    'Чаты': TEXT_EMOJI_IDS['list'][0],
    'Моя статистика': TEXT_EMOJI_IDS['owner_messages'][0],
    'Рейтинги': TEXT_EMOJI_IDS['chat_list_stats'][0],
    'Ответы': TEXT_EMOJI_IDS['messages'][0],
    'Обзор': TEXT_EMOJI_IDS['chat_stats'][0],
    'Медиа': TEXT_EMOJI_IDS['media'][0],
    'Слова': TEXT_EMOJI_IDS['top_words'][0],
    'Об учёте': TEXT_EMOJI_IDS['stat_info'][0],
    'Кратко': TEXT_EMOJI_IDS['compact'][0],
    'Подробнее': TEXT_EMOJI_IDS['detailed'][0],
    'Паузы >4 ч': TEXT_EMOJI_IDS['time'][0],
    'Инициатива': TEXT_EMOJI_IDS['owner_messages'][0],
    'Удаления': TEXT_EMOJI_IDS['disabled'][0],
    'Правки': TEXT_EMOJI_IDS['history'][0],
    'Дни': TEXT_EMOJI_IDS['date'][0],
    'Неделя': TEXT_EMOJI_IDS['date'][0],
    'Часы': TEXT_EMOJI_IDS['time'][0],
    'Сравнение': TEXT_EMOJI_IDS['chat_list_stats'][0],
    'Количество': TEXT_EMOJI_IDS['media'][0],
    'Активность': TEXT_EMOJI_IDS['date'][0],
    'Удаления и правки': TEXT_EMOJI_IDS['history'][0],
    '💳 Слова': TEXT_EMOJI_IDS['top_words'][0],
    'Рекорды': TEXT_EMOJI_IDS['chat_stats'][0],
    'Вы': TEXT_EMOJI_IDS['owner_messages'][0],
    'Собеседник': TEXT_EMOJI_IDS['peer_messages'][0],
    'Оба': TEXT_EMOJI_IDS['list'][0],
    'Голосовые': TEXT_EMOJI_IDS['voice'][0],
    'Длительность': TEXT_EMOJI_IDS['duration'][0],
    'Удалено: собеседник': TEXT_EMOJI_IDS['disabled'][0],
    'Удалено: вы': TEXT_EMOJI_IDS['disabled'][0],
    '◀ Текст': TEXT_EMOJI_IDS['back'][0],
    'Текст ▶': BUTTON_ICON_IDS['▶'],
})


UNICODE_PREFIXES = (
    "👤", "📡", "🗃", "🔧", "⚙️", "⚙", "➕", "👥", "🕘", "🖼",
    "🔄", "🔕", "🔔", "⏱", "📝", "📊", "📎", "💳", "◀️", "◀", "▶️", "▶",
)


def icon(name):
    emoji_id, alt = TEXT_EMOJI_IDS[name]
    return f'<tg-emoji emoji-id="{emoji_id}">{escape(alt)}</tg-emoji>'


def styled_button(text):
    if text.startswith("Снять мут · "):
        return text, "5775937998948404844"
    emoji_id = BUTTON_ICON_IDS.get(text)
    if not emoji_id:
        return text, None
    label = text
    for prefix in UNICODE_PREFIXES:
        if label.startswith(prefix):
            label = label[len(prefix):].lstrip()
            break
    return label or "\u200b", emoji_id


class _CaptionTruncator(HTMLParser):
    def __init__(self, limit):
        super().__init__(convert_charrefs=False)
        self.limit = limit
        self.visible = 0
        self.output = []
        self.stack = []
        self.truncated = False

    def handle_starttag(self, tag, attrs):
        if self.truncated:
            return
        self.output.append(self.get_starttag_text())
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.truncated:
            return
        self.output.append(f"</{tag}>")
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            self.stack.pop(index)

    def handle_data(self, data):
        if self.truncated:
            return
        room = self.limit - self.visible
        if len(data) <= room:
            self.output.append(data)
            self.visible += len(data)
            return
        self.output.append(data[:max(0, room - 1)] + "…")
        self.visible = self.limit
        self.truncated = True

    def handle_entityref(self, name):
        self._entity(f"&{name};")

    def handle_charref(self, name):
        self._entity(f"&#{name};")

    def _entity(self, value):
        if self.truncated:
            return
        if self.visible >= self.limit:
            self.truncated = True
            return
        self.output.append(value)
        self.visible += 1

    def result(self):
        return "".join(self.output) + "".join(f"</{tag}>" for tag in reversed(self.stack))


def truncate_html(value, limit=1024):
    """Truncate by visible characters without cutting Telegram HTML tags."""
    parser = _CaptionTruncator(limit)
    parser.feed(value)
    parser.close()
    return parser.result()
