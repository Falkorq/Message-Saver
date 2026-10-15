"""Authenticated field encryption. Message bodies are opened only for delivery."""
import base64
import hashlib
import hmac
import json
import os
import re
from collections import Counter
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import dotenv_values

VERSION = 'aes256gcm-v1'
FIELDS = ('text', 'caption', 'details')
STATS_FIELD = '_encrypted_stats'
ENV_KEY = 'MESSAGE_ENCRYPTION_KEY'


class EncryptionError(ValueError):
    """Only generic diagnostics: never include a key, ciphertext or message."""


def protected(value):
    return isinstance(value, dict) and '$encrypted' in value


def content_fingerprint(record):
    fields = {k: record.get(k) for k in ('text','caption','type','file_unique_id','details','metadata')}
    return hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def text_metrics(record):
    summary = record.get('_stats_metrics')
    if summary is not None:
        return summary['chars'], Counter(summary['words'])
    text = record.get('text') or record.get('caption') or ''
    if not isinstance(text,str):
        raise EncryptionError('Для статистики зашифрованной записи нужен расчётный снимок.')
    return len(text), Counter(re.findall(r'[а-яёa-z]{3,}',text.lower()))


class MessageCipher:
    def __init__(self, key):
        try:
            raw = base64.b64decode(key.encode('ascii'), altchars=b'-_', validate=True)
            if len(raw) != 32:
                raise ValueError()
        except (ValueError, TypeError, AttributeError, UnicodeError):
            raise EncryptionError('MESSAGE_ENCRYPTION_KEY должен содержать base64-ключ длиной 32 байта.') from None
        self.aes = AESGCM(raw)
        self.kid = hashlib.sha256(b'nyan/key-id/v1'+raw).hexdigest()[:32]
        self.compare_key = hmac.digest(raw,b'nyan/message-comparison/v1','sha256')

    @classmethod
    def from_env(cls, path):
        # Deliberately do not accept process environment, interpolation or a default key.
        path=Path(path)
        if not path.is_file():
            raise EncryptionError('Не найден .env с MESSAGE_ENCRYPTION_KEY.')
        value=dotenv_values(path,interpolate=False).get(ENV_KEY)
        if not value:
            raise EncryptionError('Добавьте MESSAGE_ENCRYPTION_KEY в .env перед запуском.')
        return cls(value)

    def _check(self, value):
        if not protected(value) or value.get('$encrypted') != VERSION:
            raise EncryptionError('Неизвестный формат шифрования.')
        if value.get('kid') != self.kid:
            raise EncryptionError('Ключ в .env не соответствует зашифрованным данным.')
        try:
            nonce=base64.b64decode(value['nonce'],altchars=b'-_',validate=True)
            data=base64.b64decode(value['data'],altchars=b'-_',validate=True)
            tag=value['tag']
            if len(nonce)!=12 or len(data)<16 or not re.fullmatch(r'[0-9a-f]{64}',tag):
                raise ValueError()
        except (ValueError,KeyError,TypeError):
            raise EncryptionError('Повреждён формат зашифрованной записи.') from None
        return nonce,data,tag

    def _aad(self, field, tag):
        return f'nyan/{VERSION}/{self.kid}/{field}/{tag}'.encode('ascii')

    def seal(self, text, field):
        if protected(text):
            self._check(text)
            return dict(text)
        if text is None:
            return None
        if not isinstance(text,str):
            raise EncryptionError('Некорректный тип текстового поля.')
        nonce=os.urandom(12)
        tag=hmac.digest(self.compare_key,nonce+field.encode()+text.encode('utf-8'),'sha256').hex()
        data=self.aes.encrypt(nonce,text.encode('utf-8'),self._aad(field,tag))
        return {'$encrypted':VERSION,'kid':self.kid,'nonce':base64.urlsafe_b64encode(nonce).decode(),
                'data':base64.urlsafe_b64encode(data).decode(),'tag':tag}

    def open(self, value, field):
        if value is None or isinstance(value,str):
            return value  # Mixed legacy/new databases remain readable.
        nonce,data,tag=self._check(value)
        try:
            return self.aes.decrypt(nonce,data,self._aad(field,tag)).decode('utf-8')
        except (InvalidTag,UnicodeError,ValueError):
            raise EncryptionError('Не удалось проверить целостность зашифрованной записи.') from None

    def validate_record(self, record, authenticate=False):
        if not isinstance(record,dict):
            raise EncryptionError('Запись сообщения должна быть JSON-объектом.')
        for field in (*FIELDS,STATS_FIELD):
            value=record.get(field)
            if protected(value):
                if authenticate:
                    self.open(value,field)
                else:
                    self._check(value)
            elif value is not None and not isinstance(value,str):
                raise EncryptionError('Некорректный тип текстового поля.')

    def protect_record(self, record):
        self.validate_record(record)
        result=dict(record)
        if any(protected(record.get(f)) for f in FIELDS) or protected(record.get(STATS_FIELD)):
            if not protected(record.get(STATS_FIELD)):
                raise EncryptionError('У зашифрованной записи отсутствует расчётный снимок.')
        else:
            chars,words=text_metrics(record)
            summary={'chars':chars,'words':dict(words),'fingerprint':content_fingerprint(record)}
            result[STATS_FIELD]=self.seal(json.dumps(summary,ensure_ascii=False),STATS_FIELD)
        for field in FIELDS:
            if field in record:
                result[field]=self.seal(record[field],field)
        result.pop('_stats_metrics',None)
        return result

    def stats_record(self, record):
        """Decrypt numerical/token counters, never text/caption/details."""
        if STATS_FIELD not in record:
            return record
        self.validate_record(record)
        try:
            summary=json.loads(self.open(record[STATS_FIELD],STATS_FIELD))
            if (type(summary['chars']) is not int or summary['chars']<0
                    or not isinstance(summary['words'],dict)
                    or any(not isinstance(w,str) or type(n) is not int or n<0 for w,n in summary['words'].items())
                    or not re.fullmatch(r'[0-9a-f]{64}',summary['fingerprint'])):
                raise ValueError()
        except (ValueError,KeyError,TypeError):
            raise EncryptionError('Повреждён расчётный снимок сообщения.') from None
        return dict(record,_stats_metrics=summary)

    def protect_payload(self, payload):
        result=dict(payload)
        for field in ('old','new'):
            if field in result:
                result[field]=self.protect_record(result[field])
        if 'text' in result:
            result['text']=self.seal(result['text'],'notification-text')
        return result

    def open_payload(self, payload):
        """The notification delivery boundary is the sole body-decryption caller."""
        result=dict(payload)
        for name in ('old','new'):
            if name in result:
                record=dict(result[name])
                for field in FIELDS:
                    if field in record:
                        record[field]=self.open(record[field],field)
                record.pop(STATS_FIELD,None);record.pop('_stats_metrics',None)
                result[name]=record
        if 'text' in result:
            result['text']=self.open(result['text'],'notification-text')
        return result

    def records_equal(self, saved, incoming):
        if not saved or not incoming:
            return saved==incoming
        # Preserve original full-record comparison; ignore only storage-only metadata.
        keys=(set(saved)|set(incoming))-{STATS_FIELD,'_stats_metrics'}
        for field in keys:
            old,new=saved.get(field),incoming.get(field)
            if field in FIELDS and protected(old):
                nonce,_,tag=self._check(old)
                if not isinstance(new,str):
                    return False
                candidate=hmac.digest(self.compare_key,nonce+field.encode()+new.encode('utf-8'),'sha256').hex()
                if not hmac.compare_digest(tag,candidate):
                    return False
            elif old!=new or (field in saved)!=(field in incoming):
                return False
        return True

    def validate_database_key(self, db):
        for table in ('messages','outbox'):
            if not db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?',(table,)).fetchone():
                continue
            for row in db.execute(f'SELECT data FROM {table}'):
                try:
                    value=json.loads(row[0])
                    if table=='messages':
                        self.validate_record(value)
                    else:
                        for field in ('old','new'):
                            if field in value:self.validate_record(value[field])
                        if protected(value.get('text')):self._check(value['text'])
                except (ValueError,KeyError,TypeError):
                    raise EncryptionError('Проверка формата/ключа сохранённых сообщений не пройдена.') from None
