"""Telegram HTML for public menu copy; dynamic values always stay escaped."""
import re
from html import escape
from html.parser import HTMLParser

from profile_monitor.style import icon


TITLE_ICONS = {
    'Nyan message': 'bot', 'Добро пожаловать в Nyan': 'bot',
    'Подключить аккаунт': 'account', 'Состояние бота': 'success',
    'Уведомления о сообщениях': 'notifications', 'Мои данные': 'saved',
    'Данные и приватность': 'info', 'Помощь': 'question',
    'Статистика чатов': 'chat_stats', 'Старые правила автоудаления': 'history',
    'Удалить мои данные?': 'warning', 'Данные удалены.': 'success',
    'Сохранение приостановлено.': 'disabled', 'Сохранение возобновлено.': 'success',
    'Уведомления о профилях': 'notifications', 'Как часто проверять профили': 'interval',
    'Сохранение фото профилей': 'photos', 'Вид уведомлений о профилях': 'format',
}

LABELS = ('События:', 'Сообщения:', 'Последнее сообщение:', 'Ваш Telegram ID:',
          'Уведомлений ожидают отправки:', 'Не удалось доставить уведомлений:')
EMPHASIS = ('Отправка включена.', 'Отправка приостановлена.',
            'Сохранение новых сообщений остановлено.', 'Часть аккаунтов на паузе.',
            'Сохранение новых сообщений включено для подключённых чатов.',
            'Пауза отключает только доставку:', 'Пауза сохранения',
            'Удаление уберёт сохранённую историю и статистику',
            'Восстановить удалённую историю в Nyan нельзя.',
            'Нет статистики или уведомлений?', 'Хотите меньше уведомлений?',
            'Сейчас включены.', 'Сейчас выключены.',
            'Новые фото сохраняются.', 'Новые фото не сохраняются.',
            'Кратко:', 'Подробно:')
BUTTON_NAMES = ('Подключить аккаунт', 'Мои данные', 'Проверить подключение',
                'Проверить приглашение', 'Помощь', 'Отмена')


def menu_html(text):
    title, separator, body = text.partition('\n')
    if not separator:
        return escape(text)
    heading = (icon(TITLE_ICONS[title]) + ' ') if title in TITLE_ICONS else ''
    lines = []
    for line in body.split('\n'):
        value = escape(line)
        for label in LABELS:
            if line.startswith(label):
                value = '<b>' + escape(label) + '</b>' + escape(line[len(label):])
                if label == 'Ваш Telegram ID:':
                    value = '<b>' + label + '</b> <code>' + escape(line[len(label):].strip()) + '</code>'
                break
        for phrase in EMPHASIS:
            value = value.replace(escape(phrase), '<b>' + escape(phrase) + '</b>')
        for label in BUTTON_NAMES:
            value = value.replace('«' + label + '»', '<b>«' + label + '»</b>')
        # Keep the three connection steps readable without adding an icon to every line.
        value = re.sub(r'^([123]\.) ', r'<b>\1</b> ', value)
        lines.append(value)
    return heading + '<b>' + escape(title) + '</b>\n' + '\n'.join(lines)


def without_custom_emoji(value):
    """Retain Unicode fallbacks and all other formatting on an eligibility error."""
    return re.sub(r'</?tg-emoji\b[^>]*>', '', value)


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def telegram_length(value, parse_mode=None):
    """Conservative caption budget in UTF-16 units, excluding HTML entities/tags."""
    if parse_mode and parse_mode.upper() == 'HTML':
        parser = _VisibleText()
        parser.feed(value)
        parser.close()
        value = ''.join(parser.parts)
    return len(value.encode('utf-16-le')) // 2
