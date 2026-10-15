"""Small, private avatar cache with one bounded background download queue."""
import asyncio
import time
from collections import deque


MAX_IMAGE_BYTES = 128 * 1024


def image_type(data):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        return None
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def retry_seconds(error):
    value = getattr(error, 'seconds', None)
    if value is None:
        result = getattr(error, 'result_json', {}) or {}
        value = result.get('parameters', {}).get('retry_after')
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError):
        return 0


class AvatarCache:
    def __init__(self, bot, db, profile_module=None, *, job_interval=1, mt_interval=10,
                 mt_hour_limit=30, queue_size=24, max_entries=500, clock=time.time):
        self.bot, self.db, self.profile_module = bot, db, profile_module
        self.clock = clock
        self.job_interval, self.mt_interval = job_interval, mt_interval
        self.mt_hour_limit, self.max_entries = mt_hour_limit, max_entries
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.pending = set()
        self.mt_attempts = deque()
        self.bot_until = self.mt_until = 0
        self.worker = None
        self.db.execute('''CREATE TABLE IF NOT EXISTS business_avatar_cache (
            account_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
            image BLOB, mime TEXT, expires_at REAL NOT NULL, last_used REAL NOT NULL,
            access_hash INTEGER, source TEXT NOT NULL DEFAULT 'unknown', PRIMARY KEY(account_id,chat_id))''')
        if 'source' not in {row[1] for row in self.db.execute('PRAGMA table_info(business_avatar_cache)')}:
            self.db.execute("ALTER TABLE business_avatar_cache ADD COLUMN source TEXT NOT NULL DEFAULT 'unknown'")
        # Old cache entries cannot prove their source. Refresh them through current permissions.
        self.db.execute("DELETE FROM business_avatar_cache WHERE source='unknown'")
        self.db.commit()

    def _mt_allowed(self, owner):
        check = getattr(self.profile_module, 'can_access_owner', None)
        return bool(check and check(owner))

    async def start(self):
        self.worker = asyncio.create_task(self._run(), name='web-avatars')

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        self.pending.clear()
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()

    def request(self, owner, account, chat, username):
        """Caller must verify ownership before lookup or queueing. Never waits on Telegram."""
        now, key = self.clock(), (account, chat)
        if not self._collection_allowed(account):
            return 'none', None, None
        row = self.db.execute('SELECT * FROM business_avatar_cache WHERE account_id=? AND chat_id=?', key).fetchone()
        if row and row['source'] == 'mtproto' and not self._mt_allowed(owner):
            self.db.execute('DELETE FROM business_avatar_cache WHERE account_id=? AND chat_id=?', key)
            self.db.commit()
            row = None
        if row and row['expires_at'] > now:
            self.db.execute('UPDATE business_avatar_cache SET last_used=? WHERE account_id=? AND chat_id=?', (now, *key))
            self.db.commit()
            return ('ready', bytes(row['image']), row['mime']) if row['image'] else ('none', None, None)
        if key not in self.pending:
            if self.queue.full() or self.worker is None:
                return 'busy', None, None
            self.pending.add(key)
            self.queue.put_nowait((owner, account, chat, username, row['access_hash'] if row else None))
        return 'pending', None, None

    def _collection_allowed(self, account):
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='account_state'").fetchone():
            return True
        row = self.db.execute('SELECT collecting FROM account_state WHERE account_id=?',(account,)).fetchone()
        return bool(row and row[0])

    def _save(self, key, data, ttl, access_hash=None, source='bot'):
        if not self._collection_allowed(key[0]):
            return
        mime = image_type(data)
        now = self.clock()
        self.db.execute('''INSERT INTO business_avatar_cache
            (account_id,chat_id,image,mime,expires_at,last_used,access_hash,source) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id,chat_id) DO UPDATE SET image=excluded.image,
            mime=excluded.mime, expires_at=excluded.expires_at,last_used=excluded.last_used,
            access_hash=COALESCE(excluded.access_hash,business_avatar_cache.access_hash),source=excluded.source''',
            (*key, data if mime else None, mime, now + ttl, now, access_hash, source))
        # Bound disk use, including negative entries. At most 500 small pictures.
        self.db.execute('''DELETE FROM business_avatar_cache WHERE rowid IN (
            SELECT rowid FROM business_avatar_cache ORDER BY last_used DESC LIMIT -1 OFFSET ?)''', (self.max_entries,))
        self.db.commit()

    async def _bot_photo(self, chat):
        if not self.bot or chat <= 0 or self.clock() < self.bot_until:
            return None
        photos = await self.bot.get_user_profile_photos(chat, limit=1)
        if not photos.photos:
            return None
        photo = min(photos.photos[0], key=lambda p: p.width * p.height)
        if (getattr(photo, 'file_size', None) or 0) > MAX_IMAGE_BYTES:
            return None
        file = await self.bot.get_file(photo.file_id)
        if (getattr(file, 'file_size', None) or 0) > MAX_IMAGE_BYTES:
            return None
        return await self.bot.download_file(file.file_path)

    async def _load(self, owner, account, chat, username, access_hash):
        key = account, chat
        transient = False
        try:
            data = await asyncio.wait_for(self._bot_photo(chat), timeout=8)
            if image_type(data):
                self._save(key, data, 86400)
                return
        except Exception as error:
            transient = getattr(error, 'error_code', None) not in (400, 403)
            retry = retry_seconds(error)
            if retry:
                self.bot_until = max(self.bot_until, self.clock() + retry + 1)

        module = self.profile_module
        now = self.clock()
        allowed = self._mt_allowed(owner)
        if allowed and module and module.enabled and not module.telegram.client.is_connected():
            transient = True
        while self.mt_attempts and self.mt_attempts[0] <= now - 3600:
            self.mt_attempts.popleft()
        if allowed and module and module.enabled and module.telegram.client.is_connected() and chat > 0:
            if now < self.mt_until or len(self.mt_attempts) >= self.mt_hour_limit:
                self._save(key, None, max(900, self.mt_until - now), access_hash)
                return
            # Budget accounts for lookup + download as one avatar attempt, at most 30/hour.
            wait = self.mt_interval - (now - self.mt_attempts[-1]) if self.mt_attempts else 0
            if wait > 0:
                await asyncio.sleep(wait)
            if not self._mt_allowed(owner):
                self._save(key, None, 900)
                return
            self.mt_attempts.append(self.clock())
            known = module.repo.get(owner, chat)
            if known:
                access_hash = known['access_hash'] or access_hash
            try:
                data, access_hash = await asyncio.wait_for(module.telegram.avatar(
                    chat, username=username, access_hash=access_hash), timeout=10)
                if image_type(data):
                    if not self._mt_allowed(owner):
                        self._save(key, None, 900)
                        return
                    self._save(key, data, 86400, access_hash, source='mtproto')
                    return
            except Exception as error:
                transient = True
                # A timeout can include Telethon's internal short FloodWait sleep.
                retry = retry_seconds(error)
                if retry or isinstance(error, TimeoutError):
                    self.mt_until = max(self.mt_until, self.clock() + max(300, retry + 1))
        self._save(key, None, 900 if transient else 21600, access_hash)

    async def _run(self):
        while True:
            job = await self.queue.get()
            key = job[1], job[2]
            try:
                await self._load(*job)
            except Exception:
                # Avatar failure must never stop statistics or leak Telegram credentials.
                self.db.rollback()
            finally:
                self.pending.discard(key)
                self.queue.task_done()
            await asyncio.sleep(self.job_interval)
