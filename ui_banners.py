"""Static section artwork. Originals live in assets/banners and contain no user data."""
from pathlib import Path

BANNER_DIR = Path(__file__).resolve().parent / 'assets' / 'banners'


def banner_for(section):
    names = {'menu', 'invite', 'connect', 'status', 'notifications', 'data', 'help',
             'erase', 'rules', 'private_profiles', 'profiles', 'watch', 'events', 'settings'}
    if section not in names:
        return None
    path = BANNER_DIR / (section + '.png')
    return path if path.is_file() else None
