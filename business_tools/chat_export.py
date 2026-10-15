"""Bounded, streaming reader for a single Telegram Desktop personal-chat export.

Only normalized statistics input is returned; file paths and message entities
are never followed, rendered as HTML, or used to download attachments.
"""
import codecs
import json
import re
import time
from collections import Counter

MAX_BYTES = 128 * 1024 * 1024
MAX_RECORD = 2 * 1024 * 1024
MAX_MESSAGES = 1_000_000


class ExportError(ValueError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExportError('В JSON повторяются поля. Создайте экспорт заново.')
        result[key] = value
    return result


class _JSONStream:
    def __init__(self, source):
        self.source, self.buffer, self.eof, self.bytes = source, '', False, 0
        self.utf8 = codecs.getincrementaldecoder('utf-8-sig')()
        self.decoder = json.JSONDecoder(object_pairs_hook=_unique,
            parse_constant=lambda value: (_ for _ in ()).throw(ExportError('Неверное число в JSON.')))

    def more(self):
        raw = self.source.read(65536)
        self.bytes += len(raw)
        if self.bytes > MAX_BYTES:
            raise ExportError('Экспорт слишком большой. Максимум — 128 МБ.')
        try:
            self.buffer += self.utf8.decode(raw, final=not raw)
        except UnicodeError:
            raise ExportError('Нужен JSON-экспорт в кодировке UTF-8.') from None
        self.eof = not raw

    def peek(self):
        while True:
            self.buffer = self.buffer.lstrip(' \t\r\n')
            if self.buffer or self.eof:
                return self.buffer[:1]
            self.more()

    def take(self, expected):
        if self.peek() != expected:
            raise ExportError('Некорректный JSON. Выберите result.json из экспорта чата.')
        self.buffer = self.buffer[1:]

    def value(self):
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer)
                # A number may continue in the next chunk.
                if end == len(self.buffer) and not self.eof:
                    self.more()
                    continue
                if end > MAX_RECORD:
                    raise ExportError('Одна запись в экспорте слишком большая.')
                self.buffer = self.buffer[end:]
                return value
            except json.JSONDecodeError:
                if self.eof or len(self.buffer) > MAX_RECORD:
                    raise ExportError('Некорректный JSON или слишком большая запись.') from None
                self.more()
            except RecursionError:
                raise ExportError('Слишком сложная структура JSON.') from None


def export_items(source):
    reader = _JSONStream(source)
    reader.take('{')
    keys, count = set(), 0
    while reader.peek() != '}':
        key = reader.value()
        if not isinstance(key, str) or key in keys:
            raise ExportError('Некорректные поля экспорта.')
        if key not in ('name','id','type','messages'):
            raise ExportError('Выберите JSON-экспорт одного чата, а не полный архив аккаунта.')
        keys.add(key)
        reader.take(':')
        if key == 'messages':
            reader.take('[')
            while reader.peek() != ']':
                message = reader.value()
                count += 1
                if not isinstance(message, dict) or count > MAX_MESSAGES:
                    raise ExportError('Неверная запись или слишком много сообщений.')
                yield 'message', message
                if reader.peek() == ']':
                    break
                reader.take(',')
                if reader.peek() == ']':
                    raise ExportError('Лишняя запятая в JSON.')
            reader.take(']')
        else:
            yield key, reader.value()
        if reader.peek() == '}':
            break
        reader.take(',')
        if reader.peek() == '}':
            raise ExportError('Лишняя запятая в JSON.')
    reader.take('}')
    if reader.peek() or 'messages' not in keys:
        raise ExportError('Нужен отдельный чат в формате JSON, а не полный архив аккаунта.')


def plain_text(value, depth=0):
    if depth > 20:
        raise ExportError('Слишком сложное форматирование сообщения.')
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(plain_text(part, depth+1) for part in value)
    if isinstance(value, dict):
        return plain_text(value.get('text', ''), depth+1)
    if value is None:
        return ''
    raise ExportError('Неизвестный формат текста сообщения.')


def rich_text(value, depth=0):
    if depth > 20:
        raise ExportError('Слишком сложное форматирование сообщения.')
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(rich_text(part, depth+1) for part in value)
    if isinstance(value, dict):
        if 'text' in value:
            return plain_text(value['text'], depth+1)
        return '\n'.join(rich_text(value[k], depth+1) for k in ('title','blocks','items','rows','cells') if k in value)
    return ''


def positive_id(value):
    if type(value) is not int or not 0 < value < 2**53:
        raise ExportError('Некорректный идентификатор в экспорте.')
    return value


def normalize_message(message):
    if message.get('type') == 'service':
        return None
    if message.get('type') != 'message':
        raise ExportError('Неизвестный тип записи в экспорте.')
    mid = positive_id(message.get('id'))
    sender = message.get('from_id')
    if not isinstance(sender, str) or not re.fullmatch(r'user[0-9]+', sender):
        raise ExportError('Поддерживаются сообщения пользователей в личных чатах.')
    sender = positive_id(int(sender[4:]))
    stamp = message.get('date_unixtime')
    if isinstance(stamp, str) and re.fullmatch(r'[0-9]{1,12}', stamp):
        stamp = int(stamp)
    if type(stamp) is not int or not 0 < stamp <= int(time.time())+86400:
        raise ExportError('Некорректное время сообщения. Нужен экспорт с date_unixtime.')
    text = plain_text(message.get('text'))
    if not text and 'rich_message' in message:
        text = rich_text(message['rich_message'])
    if len(text) > 65536:
        raise ExportError('В экспорте есть слишком длинное сообщение. Максимум — 65 536 символов.')
    media = message.get('media_type')
    kind = {'sticker':'sticker','video_file':'video','animation':'animation',
            'video_message':'video_note','voice_message':'voice','audio_file':'audio'}.get(media)
    if kind is None:
        kind = next((kind for key,kind in (('photo','photo'),('file','document'),
            ('contact_information','contact'),('location_information','location'),('poll','poll')) if key in message), 'text')
    duration = message.get('duration_seconds')
    metadata = {}
    if duration is not None:
        if type(duration) not in (int,float) or not 0 <= duration <= 31_536_000:
            raise ExportError('Некорректная длительность медиа.')
        metadata['duration'] = int(duration)
    return mid, dict(sender_id=sender, sent_at=stamp, text=text, type=kind, metadata=metadata)


def inspect_export(source, account_id):
    positive_id(account_id)
    header, counts, has_account = {}, Counter(), False
    first = last = None
    for key, value in export_items(source):
        if key != 'message':
            header[key] = value
            continue
        counts['entries'] += 1
        normalized = normalize_message(value)
        if normalized is None:
            counts['service'] += 1
            continue
        _, record = normalized
        has_account = has_account or record['sender_id'] == account_id
        counts['messages'] += 1
        counts['own' if record['sender_id']==account_id else 'peer'] += 1
        counts['media_'+record['type']] += 1
        counts['marked_edited'] += int('edited_unixtime' in value)
        counts['rich_messages'] += int('rich_message' in value)
        first = min(first or record['sent_at'], record['sent_at'])
        last = max(last or record['sent_at'], record['sent_at'])
    if header.get('type') != 'personal_chat':
        raise ExportError('Пока можно импортировать только отдельный личный чат в JSON.')
    chat_id = positive_id(header.get('id'))
    name = header.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 512:
        raise ExportError('В экспорте не найдено название чата.')
    if not has_account or not counts['messages']:
        raise ExportError('В этом экспорте нет сообщений выбранного аккаунта. Проверьте аккаунт.')
    return dict(chat_id=chat_id, name=name, first_at=first, last_at=last, **counts)


def records(source, preview):
    for key, value in export_items(source):
        if key != 'message':
            continue
        normalized = normalize_message(value)
        if normalized:
            mid, record = normalized
            yield mid, dict(record, chat_id=preview['chat_id'], chat_name=preview['name'])
