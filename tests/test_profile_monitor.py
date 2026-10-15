import asyncio
import sqlite3
import unittest
from types import SimpleNamespace

from profile_monitor.models import ProfileSnapshot, compare_snapshots
from profile_monitor.repository import ProfileRepository
from profile_monitor.scheduler import ProfileScheduler
from profile_monitor.service import ProfileMonitorService
from profile_monitor.telegram_client import ProfileTelegramClient
from telethon.tl.types import InputUser


def snapshot(user_id=10, username="old", first_name="Test User", last_name=None,
             bio="old bio", photo_id="photo-1"):
    return ProfileSnapshot(user_id, username, first_name, last_name, bio, photo_id)


class SnapshotComparisonTests(unittest.TestCase):
    def test_username_changed_removed_and_returned(self):
        old = snapshot()
        changed = snapshot(username="new")
        self.assertEqual(compare_snapshots(old, changed), [("username", "old", "new")])
        self.assertEqual(compare_snapshots(changed, snapshot(username=None)), [("username", "new", None)])
        self.assertEqual(compare_snapshots(snapshot(username=None), old), [("username", None, "old")])

    def test_name_bio_and_multiple_fields(self):
        changes = compare_snapshots(snapshot(), snapshot(first_name="Updated User", last_name="Dev", bio=None))
        self.assertEqual([item[0] for item in changes], ["first_name", "last_name", "bio"])

    def test_photo_changed_removed_and_returned(self):
        self.assertEqual(compare_snapshots(snapshot(), snapshot(photo_id="photo-2"))[0][0], "photo_id")
        self.assertEqual(compare_snapshots(snapshot(), snapshot(photo_id=None))[0], ("photo_id", "photo-1", None))
        self.assertEqual(compare_snapshots(snapshot(photo_id=None), snapshot())[0], ("photo_id", None, "photo-1"))


class FakeTelegram:
    def __init__(self, values):
        self.values = list(values)
        self.downloads = []
        self.fetch_calls = []

    async def fetch(self, target, access_hash=None, username=None, allow_id_search=True):
        self.fetch_calls.append((target, access_hash, username, allow_id_search))
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value, SimpleNamespace(access_hash=987654321)

    async def download_photo(self, entity, user_id, photo_id):
        self.downloads.append(photo_id)
        return f"/{user_id}/{photo_id}.jpg"


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.repo = ProfileRepository(self.db)
        self.repo.migrate()
        self.notifications = []

    async def notifier(self, *args):
        self.notifications.append(args)

    async def test_first_watch_is_baseline_without_change_event(self):
        tg = FakeTelegram([snapshot()])
        service = ProfileMonitorService(self.repo, tg, self.notifier)
        await service.watch(1, "@old")
        self.assertEqual(self.repo.history(1), [])
        self.assertEqual(self.notifications, [])
        self.assertIsNotNone(self.repo.get(1, 10))

    async def test_repeated_watch_does_not_reset_baseline_or_call_api(self):
        self.repo.add(1, snapshot())
        tg = FakeTelegram([])
        service = ProfileMonitorService(self.repo, tg, self.notifier)
        current, added = await service.watch(1, "@old")
        self.assertFalse(added)
        self.assertEqual(current.username, "old")

    async def test_multiple_changes_create_one_notification_and_one_batch(self):
        self.repo.add(1, snapshot())
        tg = FakeTelegram([snapshot(username="new", first_name="Updated User", bio="new bio")])
        service = ProfileMonitorService(self.repo, tg, self.notifier)
        changes = await service.check(1, 10)
        self.assertEqual(len(changes), 3)
        self.assertEqual(len(self.notifications), 1)
        rows = self.repo.history(1)
        self.assertEqual(len({row["batch_id"] for row in rows}), 1)

    async def test_returned_username_and_photo_are_new_events(self):
        self.repo.add(1, snapshot())
        tg = FakeTelegram([
            snapshot(username="new", photo_id="photo-2"),
            snapshot(username="old", photo_id="photo-1"),
        ])
        service = ProfileMonitorService(self.repo, tg, self.notifier)
        await service.check(1, 10)
        await service.check(1, 10)
        rows = self.repo.history(1)
        self.assertEqual(sum(row["field"] == "username" for row in rows), 2)
        self.assertEqual(sum(row["field"] == "photo_id" for row in rows), 2)
        self.assertEqual(len(self.notifications), 2)

    async def test_photo_download_only_after_change(self):
        self.repo.add(1, snapshot())
        tg = FakeTelegram([snapshot(), snapshot(photo_id="photo-2"), snapshot(photo_id=None)])
        service = ProfileMonitorService(self.repo, tg, self.notifier)
        await service.check(1, 10)
        self.assertEqual(tg.downloads, [])
        await service.check(1, 10)
        self.assertEqual(tg.downloads, ["photo-2"])
        await service.check(1, 10)
        self.assertEqual(tg.downloads, ["photo-2"])

    async def test_restart_preserves_snapshot_and_history(self):
        self.repo.add(1, snapshot())
        service = ProfileMonitorService(self.repo, FakeTelegram([snapshot(username="new")]), self.notifier)
        await service.check(1, 10)
        restarted = ProfileRepository(self.db)
        restarted.migrate()
        self.assertEqual(restarted.get(1, 10)["username"], "new")
        self.assertEqual(restarted.get(1, 10)["access_hash"], 987654321)
        self.assertEqual(len(restarted.history(1)), 1)

    async def test_check_passes_saved_access_hash_after_restart(self):
        self.repo.add(1, snapshot(), access_hash=123456789)
        tg = FakeTelegram([snapshot(username="new")])
        service = ProfileMonitorService(self.repo, tg, self.notifier)
        await service.check(1, 10)
        self.assertEqual(tg.fetch_calls, [(10, 123456789, "old", False)])
        self.assertEqual(self.repo.get(1, 10)["access_hash"], 987654321)

    async def test_rewatch_current_username_recovers_hash_without_resetting_baseline(self):
        self.repo.add(1, snapshot(), access_hash=None)
        tg = FakeTelegram([snapshot(username="new")])
        service = ProfileMonitorService(self.repo, tg, self.notifier)
        current, added = await service.watch(1, "@new")
        self.assertFalse(added)
        self.assertEqual(current.username, "old")
        self.assertEqual(self.repo.get(1, 10)["access_hash"], 987654321)
        self.assertEqual(self.repo.history(1), [])

    async def test_rewatch_fills_missing_username_without_change_event(self):
        self.repo.add(1, snapshot(username=None), access_hash=None)
        tg = FakeTelegram([snapshot(username="test_profile")])
        service = ProfileMonitorService(self.repo, tg, self.notifier)
        current, added = await service.watch(1, "@test_profile")
        self.assertFalse(added)
        self.assertEqual(current.username, "test_profile")
        self.assertEqual(self.repo.history(1), [])

    async def test_rewatch_known_id_recovers_hash_without_resetting_baseline(self):
        self.repo.add(1, snapshot(username=None), access_hash=None)
        tg = FakeTelegram([snapshot(username=None, first_name="New name")])
        service = ProfileMonitorService(self.repo, tg, self.notifier)
        current, added = await service.watch(1, "10")
        self.assertFalse(added)
        self.assertEqual(current.first_name, "Test User")
        self.assertEqual(self.repo.get(1, 10)["access_hash"], 987654321)
        self.assertEqual(self.repo.history(1), [])

    def test_migrates_existing_watchlist_without_access_hash(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute(
            "CREATE TABLE profile_watchers(owner_id INTEGER,user_id INTEGER,username TEXT,"
            "first_name TEXT,last_name TEXT,bio TEXT,photo_id TEXT,last_checked_at INTEGER,"
            "added_at INTEGER,enabled INTEGER,last_error TEXT)"
        )
        db.execute(
            "INSERT INTO profile_watchers VALUES(1,10,'old','Test User',NULL,NULL,NULL,0,0,1,NULL)"
        )
        migrated = ProfileRepository(db)
        migrated.migrate()
        self.assertIsNone(migrated.get(1, 10)["access_hash"])

    async def test_unavailable_user_does_not_overwrite_snapshot(self):
        self.repo.add(1, snapshot())
        service = ProfileMonitorService(self.repo, FakeTelegram([RuntimeError("unavailable")]), self.notifier)
        with self.assertRaises(RuntimeError):
            await service.check(1, 10)
        self.assertEqual(self.repo.get(1, 10)["username"], "old")
        self.assertEqual("RuntimeError", self.repo.get(1, 10)["last_error"])

    async def test_scheduler_continues_after_one_user_error(self):
        self.repo.add(1, snapshot(user_id=10))
        self.repo.add(1, snapshot(user_id=11, username="second"))
        self.db.execute("UPDATE profile_watchers SET last_checked_at=0")
        self.db.commit()
        service = ProfileMonitorService(
            self.repo, FakeTelegram([RuntimeError("bad user"), snapshot(user_id=11, username="updated")]), self.notifier
        )
        scheduler = ProfileScheduler(self.repo, service)
        await scheduler.run_cycle(pause_between=False)
        self.assertEqual("RuntimeError", self.repo.get(1, 10)["last_error"])
        self.assertEqual(self.repo.get(1, 11)["username"], "updated")


class TelegramClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_secondary_username_is_read(self):
        class FakeClient:
            async def __call__(self, request):
                user = SimpleNamespace(
                    id=10, username=None,
                    usernames=[SimpleNamespace(username="test_profile", active=True)],
                    access_hash=123456789, first_name="Name", last_name=None, photo=None,
                )
                return SimpleNamespace(users=[user], full_user=SimpleNamespace(about=None))

        wrapper = ProfileTelegramClient.__new__(ProfileTelegramClient)
        wrapper.avatar_lock = asyncio.Lock()
        wrapper.client = FakeClient()
        profile, _ = await wrapper.fetch(10, access_hash=123456789)
        self.assertEqual(profile.username, "test_profile")

    async def test_username_resolution_keeps_hash_when_full_user_is_minimal(self):
        class FakeClient:
            async def get_entity(self, target):
                self.target = target
                return SimpleNamespace(
                    id=10, username="test_profile", access_hash=123456789,
                    first_name="Name", last_name=None, photo=None,
                )

            async def __call__(self, request):
                return SimpleNamespace(
                    users=[SimpleNamespace(id=10, username=None, access_hash=None,
                                           first_name="Name", last_name=None, photo=None)],
                    full_user=SimpleNamespace(about="bio"),
                )

        wrapper = ProfileTelegramClient.__new__(ProfileTelegramClient)
        wrapper.avatar_lock = asyncio.Lock()
        wrapper.client = FakeClient()
        profile, entity = await wrapper.fetch("test_profile")
        self.assertEqual(profile.username, "test_profile")
        self.assertEqual(entity.access_hash, 123456789)

    async def test_saved_hash_fetches_profile_without_entity_cache(self):
        class FakeClient:
            def __init__(self):
                self.requests = []

            async def __call__(self, request):
                self.requests.append(request)
                user = SimpleNamespace(
                    id=10, username="new", first_name="Updated User", last_name=None,
                    photo=None, access_hash=987654321,
                )
                return SimpleNamespace(users=[user], full_user=SimpleNamespace(about="bio"))

        wrapper = ProfileTelegramClient.__new__(ProfileTelegramClient)
        wrapper.avatar_lock = asyncio.Lock()
        wrapper.client = FakeClient()
        profile, entity = await wrapper.fetch(10, access_hash=123456789, username="old")
        self.assertEqual(profile.username, "new")
        self.assertEqual(profile.bio, "bio")
        self.assertEqual(entity.access_hash, 987654321)
        self.assertEqual(len(wrapper.client.requests), 1)
        self.assertIsInstance(wrapper.client.requests[0].id, InputUser)
        self.assertEqual(wrapper.client.requests[0].id.access_hash, 123456789)


if __name__ == "__main__":
    unittest.main()
