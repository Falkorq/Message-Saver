"""Static section artwork. Originals live in assets/banners and contain no user data."""
from pathlib import Path
from telebot.asyncio_helper import ApiTelegramException

BANNER_DIR = Path(__file__).resolve().parent / 'assets' / 'banners'


def banner_for(section):
    if section == 'connect_guide':
        path = BANNER_DIR.parent.parent / 'business_tools' / 'web' / 'connect-ios.jpg'
        return path if path.is_file() else None
    names = {'menu', 'invite', 'connect', 'status', 'notifications', 'data', 'help',
             'erase', 'rules', 'private_profiles', 'profiles', 'watch', 'events', 'settings'}
    if section not in names:
        return None
    path = BANNER_DIR / (section + '.png')
    return path if path.is_file() else None


class BannerCache:
    """Per-bot, in-memory Telegram file IDs. Never cache user profile photos."""
    def __init__(self):
        self.entries = {}

    async def use(self, path, operation):
        path = Path(path).resolve()
        stat = path.stat()
        version = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        entry = self.entries.get(path)
        # No lock around API calls: one recipient's cooldown must not stall other users.
        # Concurrent first requests may each upload once; all later requests reuse file IDs.
        if entry and entry[0] == version:
            try:
                result = await operation(entry[1])
            except ApiTelegramException as exc:
                description = str(exc.description).lower()
                if exc.error_code != 400 or not any(term in description for term in (
                        'wrong file identifier', 'file_id_invalid', 'file_reference', 'invalid file id', 'wrong remote file identifier')):
                    raise
                if self.entries.get(path) == entry:
                    self.entries.pop(path, None)
            else:
                self._remember(path, version, result)
                return result
        with path.open('rb') as image:
            result = await operation(image)
            self._remember(path, version, result)
            return result

    def _remember(self, path, version, message):
        photos = getattr(message, 'photo', None)
        if isinstance(photos, (list, tuple)) and photos:
            file_id = getattr(photos[-1], 'file_id', None)
            if isinstance(file_id, str) and file_id:
                self.entries[path] = (version, file_id)


def banner_cache(bot):
    cache = getattr(bot, '_nyan_banner_cache', None)
    if not isinstance(cache, BannerCache):
        cache = BannerCache()
        bot._nyan_banner_cache = cache
    return cache
