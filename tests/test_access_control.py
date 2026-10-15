import sqlite3
import unittest

from access_control import AccessManager
from profile_monitor.models import ProfileSnapshot
from profile_monitor.repository import ProfileRepository
from profile_monitor.scheduler import ProfileScheduler


class AccessManagerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.routes = {1: 1, 2: 2}
        self.labels = {1: "Account 1", 2: "Account 2"}
        self.access = AccessManager(self.db, self.routes, self.labels, {1, 2})

    def test_grant_persists_and_revoke_removes_route(self):
        self.access.grant(3, "Друг")
        self.assertEqual(self.routes[3], 3)
        self.assertEqual(self.labels[3], "Друг")
        restarted = AccessManager(self.db, {1: 1, 2: 2}, {1: "Account 1", 2: "Account 2"}, {1, 2})
        self.assertEqual(restarted.routes[3], 3)
        self.assertTrue(self.access.revoke(3))
        self.assertNotIn(3, self.routes)
        after_revoke = AccessManager(self.db, {1: 1, 2: 2}, {}, {1, 2})
        self.assertNotIn(3, after_revoke.routes)
        self.assertFalse(self.access.revoke(3))

    def test_protected_accounts_cannot_be_changed(self):
        with self.assertRaises(ValueError):
            self.access.grant(1)
        with self.assertRaises(ValueError):
            self.access.revoke(2)
        self.assertEqual(self.routes, {1: 1, 2: 2})


class RevokedOwnerSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_revoked_owner_is_skipped(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        repo = ProfileRepository(db)
        repo.migrate()
        repo.add(3, ProfileSnapshot(7, "friend", "Friend", None, None, None))
        db.execute("UPDATE profile_watchers SET last_checked_at=0")
        db.commit()

        class Service:
            def __init__(self):
                self.calls = []

            async def check_row(self, row):
                self.calls.append(row["user_id"])

        service = Service()
        routes = {1: 1, 3: 3}
        scheduler = ProfileScheduler(repo, service, routes)
        routes.pop(3)
        await scheduler.run_cycle(pause_between=False)
        self.assertEqual(service.calls, [])


if __name__ == "__main__":
    unittest.main()
