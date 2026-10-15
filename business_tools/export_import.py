"""Encrypted temporary uploads and small atomic statistics import batches."""
import os
import struct
import tempfile
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .chat_export import ExportError, MAX_BYTES


class EncryptedUpload:
    """The temporary file contains ciphertext; its random key lives only in RAM."""
    def __init__(self):
        self.file = tempfile.TemporaryFile(prefix='nyan-import-')
        self.aes = AESGCM(AESGCM.generate_key(bit_length=256))
        self.size = self.index = 0

    def write(self, data):
        if self.size + len(data) > MAX_BYTES:
            raise ExportError('Максимальный размер экспорта — 128 МБ.')
        if len(data) > 262144:
            raise ExportError('Слишком большой блок загрузки.')
        nonce = os.urandom(12)
        encrypted = self.aes.encrypt(nonce, data, self.index.to_bytes(8,'big'))
        self.file.write(struct.pack('>I',len(encrypted)) + nonce + encrypted)
        self.size += len(data)
        self.index += 1

    def reader(self):
        self.file.flush()
        self.file.seek(0)
        upload = self
        class Reader:
            buffer = b''
            index = 0
            def read(self, size):
                while len(self.buffer) < size:
                    header = upload.file.read(4)
                    if not header:
                        break
                    if len(header)!=4:
                        raise ExportError('Повреждён временный файл импорта.')
                    length, = struct.unpack('>I',header)
                    if not 16 <= length <= 262144+16:
                        raise ExportError('Повреждён временный файл импорта.')
                    nonce, encrypted = upload.file.read(12), upload.file.read(length)
                    self.buffer += upload.aes.decrypt(nonce,encrypted,self.index.to_bytes(8,'big'))
                    self.index += 1
                value, self.buffer = self.buffer[:size], self.buffer[size:]
                return value
        return Reader()

    def close(self):
        self.file.close()
        self.aes = None


def import_batch(repository, owner, account, batch):
    """Return added/skipped counts; never write bodies or emit notifications.

    Imported messages share their statistics identity with subsequent Business
    updates. A repeated/overlapping export cannot increase message totals.
    """
    repository.check_account(owner,account)
    db = repository.db
    added = skipped = 0
    db.execute('SAVEPOINT export_batch')
    repository._import_metrics = {}
    try:
        for mid, record in batch:
            chat = record['chat_id']
            repository.check_chat_owner(owner,account,chat)
            mapped = db.execute('SELECT 1 FROM business_stats_imported WHERE account_id=? AND chat_id=? AND message_id=?',
                                (account,chat,mid)).fetchone()
            existing = db.execute('SELECT connection_id,counted,detailed FROM business_stats_seen '
                'WHERE account_id=? AND chat_id=? AND message_id=? ORDER BY counted DESC,detailed DESC LIMIT 1',
                (account,chat,mid)).fetchone()
            if mapped or (existing and existing['counted'] and existing['detailed']):
                skipped += 1
                continue
            connection = existing['connection_id'] if existing else f'nyan-export:{account}'
            db.execute('INSERT INTO business_stats_imported VALUES(?,?,?,?,?)',
                       (account,chat,mid,connection,int(time.time())))
            counted = repository.record_message(connection,account,owner,mid,record,commit=False)
            added += int(counted)
            skipped += int(not counted)
        pending = repository._import_metrics
        repository._import_metrics = None
        for (account_id,chat_id,day),values in pending.items():
            repository._daily_add(account_id,chat_id,day,values)
        db.execute('RELEASE SAVEPOINT export_batch')
    except BaseException:
        db.execute('ROLLBACK TO SAVEPOINT export_batch')
        db.execute('RELEASE SAVEPOINT export_batch')
        raise
    finally:
        repository._import_metrics = None
    return added, skipped
