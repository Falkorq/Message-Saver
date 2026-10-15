"""Durable polling and version guards. No plaintext business body enters the inbox."""
import asyncio
from contextlib import contextmanager
import json
import logging
import sqlite3
import time

log = logging.getLogger(__name__)
ALLOWED_UPDATES = ['message', 'callback_query', 'business_connection', 'business_message',
                   'edited_business_message', 'deleted_business_messages']


class AtomicConnection(sqlite3.Connection):
    """Existing repository commits join a synchronous business-event transaction."""
    _atomic = False

    def commit(self):
        if not self._atomic:
            super().commit()

    def __exit__(self, kind, value, traceback):
        if self._atomic:
            if kind:
                raise value
            return False
        return super().__exit__(kind, value, traceback)

    @contextmanager
    def event_transaction(self):
        self.commit()
        self.execute('BEGIN IMMEDIATE')
        self._atomic = True
        try:
            yield
        except BaseException:
            super().rollback()
            raise
        else:
            super().commit()
        finally:
            self._atomic = False


def ensure_outbox_indexes(db):
    """Run after legacy columns are added; preserve FIFO for each recipient."""
    db.execute('CREATE INDEX IF NOT EXISTS outbox_recipient_fifo ON outbox(recipient_id,failed,id)')
    db.execute('CREATE INDEX IF NOT EXISTS outbox_ready ON outbox(failed,retry_at,id)')
    db.execute('CREATE INDEX IF NOT EXISTS outbox_created_at ON outbox(created_at)')


class InboxFull(RuntimeError):
    pass


class DurableInbox:
    def __init__(self, db, cipher, max_rows=10000, max_bytes=64*1024*1024):
        self.db, self.cipher = db, cipher
        self.max_rows, self.max_bytes = max_rows, max_bytes
        self.last_poll = 0
        db.executescript('''
          CREATE TABLE IF NOT EXISTS incoming_updates(
            update_id INTEGER PRIMARY KEY, payload TEXT, received_at INTEGER NOT NULL,
            done_at INTEGER, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT);
          CREATE INDEX IF NOT EXISTS incoming_pending ON incoming_updates(done_at,update_id);
          CREATE TABLE IF NOT EXISTS polling_checkpoint(id INTEGER PRIMARY KEY CHECK(id=1), offset INTEGER);
          CREATE TABLE IF NOT EXISTS message_versions(
            connection_id TEXT,chat_id INTEGER,message_id INTEGER,version INTEGER NOT NULL,
            update_id INTEGER NOT NULL,deleted INTEGER NOT NULL DEFAULT 0,changed_at INTEGER NOT NULL,
            PRIMARY KEY(connection_id,chat_id,message_id));
          CREATE TABLE IF NOT EXISTS receiver_runtime(id INTEGER PRIMARY KEY,last_poll INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS delivery_cooldowns(recipient_id INTEGER PRIMARY KEY,until_at REAL NOT NULL);
        ''')
        columns={row[1] for row in db.execute('PRAGMA table_info(incoming_updates)')}
        for column, definition in [('lane',"TEXT NOT NULL DEFAULT 'global'"),('retry_at','REAL NOT NULL DEFAULT 0')]:
            if column not in columns: db.execute(f'ALTER TABLE incoming_updates ADD COLUMN {column} {definition}')
        db.commit()

    @property
    def offset(self):
        row = self.db.execute('SELECT offset FROM polling_checkpoint WHERE id=1').fetchone()
        return row[0] if row else None

    def persist(self, updates, normalize):
        """All-or-nothing batch: only a committed batch can advance the next getUpdates."""
        if not updates:
            return
        with self.db:
            for raw in updates:
                uid = raw['update_id']
                if self.db.execute('SELECT 1 FROM incoming_updates WHERE update_id=?', (uid,)).fetchone():
                    continue
                normalized=normalize(raw)
                body = json.dumps(normalized, ensure_ascii=False)
                connection=normalized.get('connection_id') or raw.get('business_connection',{}).get('id')
                lane='business:'+connection if connection else 'control'
                sealed = json.dumps(self.cipher.seal(body, 'incoming'), ensure_ascii=False)
                self.db.execute('INSERT INTO incoming_updates(update_id,payload,received_at,lane) VALUES(?,?,?,?)',
                                (uid, sealed, int(time.time()),lane))
            size = self.db.execute('SELECT COUNT(*),COALESCE(SUM(length(payload)),0) FROM incoming_updates WHERE done_at IS NULL').fetchone()
            if size[0] > self.max_rows or size[1] > self.max_bytes:
                raise InboxFull('incoming_capacity')
            self.db.execute('INSERT OR REPLACE INTO polling_checkpoint VALUES(1,?)',
                            (max(u['update_id'] for u in updates)+1,))

    def next(self):
        return self.db.execute('SELECT i.* FROM incoming_updates i WHERE i.done_at IS NULL AND i.retry_at<=? AND NOT EXISTS(SELECT 1 FROM incoming_updates p WHERE p.done_at IS NULL AND p.lane=i.lane AND p.update_id<i.update_id) ORDER BY i.update_id LIMIT 1',(time.time(),)).fetchone()

    def payload(self, row):
        # Business text remains field-encrypted inside this envelope.
        return json.loads(self.cipher.open(json.loads(row['payload']), 'incoming'))

    def done(self, uid):
        self.db.execute('UPDATE incoming_updates SET payload=NULL,done_at=?,last_error=NULL WHERE update_id=?',
                        (int(time.time()), uid))
        self.db.commit()

    def accept_version(self, key, record, uid, deleted=False):
        row = self.db.execute('SELECT * FROM message_versions WHERE connection_id=? AND chat_id=? AND message_id=?', key).fetchone()
        version = int(record.get('edit_at') or record.get('sent_at') or 0)*2 + int(bool(record.get('edit_at')))
        if not row:
            previous = self.db.execute('SELECT data FROM messages WHERE connection_id=? AND chat_id=? AND message_id=?', key).fetchone()
            if previous and not deleted:
                old = json.loads(previous[0])
                old_version = int(old.get('edit_at') or old.get('sent_at') or 0)*2+int(bool(old.get('edit_at')))
                if version < old_version:
                    return False
            tombstone = self.db.execute('SELECT deleted_at FROM business_stats_seen WHERE connection_id=? AND chat_id=? AND message_id=?',key).fetchone()
            if tombstone and tombstone[0]:
                self.db.execute('INSERT OR IGNORE INTO message_versions VALUES(?,?,?,?,?,?,?)',(*key,version,uid,1,int(time.time())))
                return False
        if row and (row['deleted'] or (not deleted and (version, uid) <= (row['version'], row['update_id']))):
            return False
        self.db.execute('INSERT OR REPLACE INTO message_versions VALUES(?,?,?,?,?,?,?)',
                        (*key, version, uid, int(deleted), int(time.time())))
        return True

    def prune(self, days=7):
        self.db.execute('DELETE FROM incoming_updates WHERE done_at<?', (int(time.time())-days*86400,))
        self.db.execute('DELETE FROM incoming_updates WHERE update_id IN (SELECT update_id FROM incoming_updates WHERE done_at IS NOT NULL ORDER BY done_at DESC,update_id DESC LIMIT -1 OFFSET 100000)')
        self.db.commit()

    def health(self):
        row = self.db.execute('SELECT COUNT(*),MIN(received_at),COALESCE(MAX(attempts),0) FROM incoming_updates WHERE done_at IS NULL').fetchone()
        return dict(pending=row[0], oldest_seconds=max(0,int(time.time())-row[1]) if row[1] else 0,
                    attempts=row[2], poll_age=max(0,int(time.time())-self.last_poll) if self.last_poll else None)

    async def poll(self, get_updates, normalize):
        while True:
            try:
                batch = await get_updates(offset=self.offset, limit=100, timeout=25,
                                          allowed_updates=ALLOWED_UPDATES, request_timeout=35)
                self.persist(batch, normalize)
                self.last_poll = int(time.time())
                self.db.execute('INSERT OR REPLACE INTO receiver_runtime VALUES(1,?)',(self.last_poll,))
                self.db.commit()
                self.prune()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error('Приём приостановлен (%s); offset не повышен', type(exc).__name__)
                await asyncio.sleep(5)

    async def work(self, dispatch):
        while True:
            row = self.next()
            if row is None:
                await asyncio.sleep(.1)
                continue
            try:
                await dispatch(row['update_id'], self.payload(row))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.db.execute('UPDATE incoming_updates SET attempts=attempts+1,last_error=?,retry_at=? WHERE update_id=?',
                                (type(exc).__name__,time.time()+min(60,2**min(row['attempts'],5)),row['update_id']))
                self.db.commit()
                log.error('Входящее событие %s ожидает повторения (%s)', row['update_id'], type(exc).__name__)
                await asyncio.sleep(.05)


class DeliveryBudget:
    """Recipient cooldown survives restart; each actual media/text API call takes a slot."""
    def __init__(self, db, per_second=20, per_recipient=1.1):
        self.db = db
        self.interval, self.per_recipient = 1/per_second, per_recipient
        self.next_global = 0
        self.lock = asyncio.Lock()

    def postpone(self, recipient, seconds):
        self.db.execute('INSERT INTO delivery_cooldowns VALUES(?,?) ON CONFLICT(recipient_id) '
                        'DO UPDATE SET until_at=MAX(until_at,excluded.until_at)',
                        (recipient, time.time()+seconds))
        self.db.commit()

    async def take(self, recipient):
        while True:
            async with self.lock:
                row = self.db.execute('SELECT until_at FROM delivery_cooldowns WHERE recipient_id IN (?,0) ORDER BY until_at DESC LIMIT 1', (recipient,)).fetchone()
                delay = max(self.next_global-time.time(), (row[0]-time.time()) if row else 0)
                if delay <= 0:
                    self.next_global = time.time()+self.interval
                    self.postpone(recipient, self.per_recipient)
                    return
            await asyncio.sleep(min(delay, 1))
