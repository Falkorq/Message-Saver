import ast
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from access_control import AccessManager
from business_tools.avatars import AvatarCache
from profile_monitor.access import MTProtoAccess
from profile_monitor.models import ProfileSnapshot
from profile_monitor.repository import ProfileRepository
from profile_monitor.scheduler import ProfileScheduler
from profile_monitor.service import ProfileMonitorService
from profile_monitor.ui import ProfileUI

JPEG = b'\xff\xd8\xffavatar'


class AccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.routes = {1: 1, 2: 2, 3: 3}
        self.policy = MTProtoAccess(self.routes, {1}, '2')
        self.repo = ProfileRepository(self.db)
        self.repo.migrate()
        self.telegram = SimpleNamespace(fetch=AsyncMock(), download_photo=AsyncMock())
        self.service = ProfileMonitorService(self.repo, self.telegram, AsyncMock(), self.policy.owner)

    def tearDown(self):
        self.db.close()

    def test_grant_does_not_open_mtproto_and_revoke_closes_it(self):
        access = AccessManager(self.db, self.routes, {}, {1})
        access.grant(4)
        self.assertFalse(self.policy.actor(4))
        self.assertTrue(self.policy.actor(2))
        self.policy.allowed_ids.add(4)
        self.assertTrue(self.policy.actor(4))
        access.revoke(4)
        self.assertFalse(self.policy.actor(4))
        self.assertFalse(self.policy.owner(4))

    def test_invalid_configuration_fails_closed(self):
        for value in ('2,bad', '-2', '0'):
            with self.assertRaises(ValueError):
                MTProtoAccess(self.routes, {1}, value)

    async def test_service_and_scheduler_reject_unlisted_owner(self):
        self.repo.add(3, ProfileSnapshot(10, 'target', 'Name', None, None, None))
        self.db.execute('UPDATE profile_watchers SET last_checked_at=0')
        self.db.commit()
        with self.assertRaises(PermissionError):
            await self.service.watch(3, '@target')
        with self.assertRaises(PermissionError):
            await self.service.check(3, 10)
        scheduler = ProfileScheduler(self.repo, self.service, self.routes, self.policy.owner)
        await scheduler.run_cycle(pause_between=False)
        self.telegram.fetch.assert_not_awaited()

    async def test_owner_and_explicit_friend_can_watch(self):
        self.telegram.fetch.return_value = (ProfileSnapshot(10, 'target', 'Name', None, None, None),
                                           SimpleNamespace(access_hash=123))
        for owner in (1, 2):
            await self.service.watch(owner, '@target')
            self.assertIsNotNone(self.repo.get(owner, 10))
        self.assertEqual(self.telegram.fetch.await_count, 2)

    async def test_public_menu_works_but_forged_profile_callback_is_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            bot = SimpleNamespace(answer_callback_query=AsyncMock())
            ui = ProfileUI(bot, self.service, self.repo, self.routes, directory, access_policy=self.policy)
            def call(data):
                return SimpleNamespace(id='x', data=data, from_user=SimpleNamespace(id=3),
                                       message=SimpleNamespace(chat=SimpleNamespace(id=3)))
            self.assertTrue(await ui.authorized_callback(call('pm:nav:tools')))
            self.assertTrue(await ui.authorized_callback(call('pm:nav:main')))
            for data in ('pm:nav:profiles', 'pm:photos:10:0:0:watch', 'pm:check:10:0:watch', 'pm:add'):
                self.assertFalse(await ui.authorized_callback(call(data)))
            self.assertEqual([b.callback_data for row in ui.main_keyboard(3).keyboard for b in row],
                             ['pm:nav:tools'])
            self.assertIn('pm:nav:profiles', [b.callback_data for row in ui.main_keyboard(2).keyboard for b in row])

    async def test_avatar_fallback_and_cached_mt_photo_require_current_permission(self):
        bot = SimpleNamespace(get_user_profile_photos=AsyncMock(return_value=SimpleNamespace(photos=[])))
        telegram = SimpleNamespace(client=SimpleNamespace(is_connected=lambda: True),
                                   avatar=AsyncMock(return_value=(JPEG, 123)))
        module = SimpleNamespace(enabled=True, telegram=telegram, repo=self.repo,
                                 can_access_owner=self.policy.owner)
        cache = AvatarCache(bot, self.db, module, mt_interval=0)
        await cache._load(3, 3, 10, 'target', None)
        telegram.avatar.assert_not_awaited()
        await cache._load(2, 2, 10, 'target', None)
        self.assertEqual(cache.request(2, 2, 10, 'target')[0], 'ready')
        self.policy.allowed_ids.remove(2)
        self.assertNotEqual(cache.request(2, 2, 10, 'target')[0], 'ready')
        await cache._load(2, 2, 10, 'target', None)
        self.assertEqual(telegram.avatar.await_count, 1)
        cache._save((3, 11), JPEG, 86400, source='bot')
        self.assertEqual(cache.request(3, 3, 11, '')[0], 'ready')

    def test_legacy_avatar_cache_is_refreshed_without_touching_other_data(self):
        self.db.execute('CREATE TABLE business_avatar_cache(account_id INTEGER,chat_id INTEGER,image BLOB,mime TEXT,expires_at REAL,last_used REAL,access_hash INTEGER,PRIMARY KEY(account_id,chat_id))')
        self.db.execute('INSERT INTO business_avatar_cache VALUES(3,10,?,\'image/jpeg\',9999999999,1,123)', (JPEG,))
        AvatarCache(None, self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_avatar_cache').fetchone()[0], 0)
        self.assertIsNotNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='profile_watchers'").fetchone())

    def test_profile_outbox_is_checked_separately_from_business_notifications(self):
        source = Path(__file__).resolve().parents[1] / 'app.py'
        node = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'notification_target')
        self.db.execute('CREATE TABLE IF NOT EXISTS settings(recipient_id INTEGER PRIMARY KEY,only_others INTEGER DEFAULT 0)')
        context = {'db':self.db,'mtproto_access': self.policy, 'routes': self.routes, 'product':SimpleNamespace(notifications=lambda *args:True,allows=lambda *args:True)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), context)
        target = context['notification_target']
        self.assertIsNone(target({'event': 'profile_change', 'owner_id': 3}))
        self.assertEqual(target({'event': 'profile_change', 'owner_id': 2}), 2)
        self.assertEqual(target({'event': 'delete', 'account_id': 3}), 3)
        self.assertIsNone(target({'event':'text_notification','owner_id':3}))
        self.assertEqual(target({'event':'text_notification','source':'business_tools','owner_id':3}),3)
