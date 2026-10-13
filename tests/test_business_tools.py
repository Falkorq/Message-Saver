import sqlite3
import json
import time
import unittest
from types import SimpleNamespace

from business_tools.repository import BusinessToolsRepository
from business_tools.service import BusinessToolsService, parse_duration
from business_tools.ui import BusinessToolsUI
from business_tools.ui import button as business_button


def record(sender_id=200, message_type="text", text="привет мир", message_id=1):
    return {
        "sender_id": sender_id,
        "chat_id": 200,
        "chat_name": "Tester",
        "chat_username": "tester",
        "username": "tester" if sender_id == 200 else "owner",
        "type": message_type,
        "text": text if message_type == "text" else None,
        "caption": text if message_type != "text" else None,
        "sent_at": 1_700_000_000 + message_id,
    }


class FakeBot:
    def __init__(self, fail=False):
        self.deleted = []
        self.fail = fail

    async def delete_business_messages(self, connection_id, message_ids):
        if self.fail:
            raise RuntimeError("no permission")
        self.deleted.append((connection_id, message_ids))
        return True


class DurationTests(unittest.TestCase):
    def test_duration_formats(self):
        self.assertEqual(parse_duration("30m"), 1800)
        self.assertEqual(parse_duration("1h 30m"), 5400)
        self.assertEqual(parse_duration("навсегда"), 0)
        with self.assertRaises(ValueError):
            parse_duration("tomorrow")


class CommandUITests(unittest.IsolatedAsyncioTestCase):
    async def test_mute_button_uses_custom_icon(self):
        value = business_button("Снять мут · Alice", "test").to_dict()
        self.assertEqual(value["text"], "Снять мут · Alice")
        self.assertEqual(value["icon_custom_emoji_id"], "5775937998948404844")

    async def test_chatstats_command_sends_response(self):
        class ReplyBot:
            def __init__(self):
                self.sent = []

            async def send_message(self, chat_id, text, **kwargs):
                self.sent.append((chat_id, text))

        reply_bot = ReplyBot()
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        repo = BusinessToolsRepository(db, {100: 1})
        repo.migrate()
        service = BusinessToolsService(reply_bot,repo,lambda *args:None,{100:'Account 1'})
        ui = BusinessToolsUI(reply_bot, service, repo, {100: 1}, "unused.png")
        await ui.handle_command(SimpleNamespace(
            from_user=SimpleNamespace(id=100), chat=SimpleNamespace(id=100), text="/chatstats"
        ))
        self.assertEqual(len(reply_bot.sent), 1)
        self.assertIn("Статистика чатов", reply_bot.sent[0][1])
        self.assertIn('emoji-id="5994378914636500516"', reply_bot.sent[0][1])


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.repo = BusinessToolsRepository(self.db, {100: 1})
        self.repo.migrate()

    def test_statistics_are_deduplicated_and_split_by_sender(self):
        self.assertTrue(self.repo.record_message("c", 100, 1, 1, record(message_id=1)))
        self.assertFalse(self.repo.record_message("c", 100, 1, 1, record(message_id=1)))
        self.assertTrue(self.repo.record_message("c", 100, 1, 2, record(sender_id=100, text="мой ответ", message_id=2)))
        row, days, words = self.repo.stat(1, 100, 200)
        self.assertEqual(row["total_messages"], 2)
        self.assertEqual(row["owner_messages"], 1)
        self.assertEqual(row["peer_messages"], 1)
        self.assertEqual(days, 1)
        self.assertTrue(any(item["word"] == "привет" for item in words))

    def test_summary_sorting_and_same_chat_on_multiple_accounts(self):
        self.repo.routes[101] = 1
        first = record(message_id=1)
        second = record(message_id=2)
        second["chat_id"] = 300
        second["chat_username"] = "other"
        second["sent_at"] += 100
        self.repo.record_message("a", 100, 1, 1, first)
        self.repo.record_message("a", 100, 1, 2, first)
        self.repo.record_message("a", 100, 1, 3, second)
        self.repo.record_message("b", 101, 1, 1, first)
        summary = self.repo.stats_summary(1)
        self.assertEqual((summary["chats"], summary["messages"]), (3, 4))
        self.assertEqual(self.repo.stats_page(1, sort="messages")[0]["chat_id"], 200)
        self.assertEqual(self.repo.stats_page(1, sort="recent")[0]["chat_id"], 300)
        self.assertEqual(len(self.repo.matching_chats(1, "@tester")), 2)
        self.assertEqual(len(self.repo.matching_chats(1, "@tester", 101)), 1)

    def test_stats_pagination_has_no_200_chat_limit(self):
        self.db.executemany(
            "INSERT INTO business_chat_stats(owner_id,account_id,chat_id,display_name) VALUES(1,100,?,?)",
            [(1000 + i, f"Chat {i}") for i in range(205)],
        )
        self.db.commit()
        self.assertEqual(self.repo.stats_summary(1)["chats"], 205)
        self.assertEqual(len(self.repo.stats_page(1, page=34, size=6)), 1)

    def test_media_and_restart_state(self):
        self.repo.record_message("c", 100, 1, 3, record(message_type="photo", text="подпись", message_id=3))
        restarted = BusinessToolsRepository(self.db, {100: 1})
        restarted.migrate()
        row, _, _ = restarted.stat(1, 100, 200)
        self.assertEqual(row["total_messages"], 1)
        self.assertIn('"photo": 1', row["media_json"])

    def test_expired_mute_is_removed(self):
        row = {"account_id": 100, "chat_id": 200, "display_name": "Tester", "username": "tester"}
        self.repo.mute(1, row, int(time.time()) - 1)
        self.assertIsNone(self.repo.is_muted(100, 200))
        self.assertEqual(self.repo.active_mutes(1), [])

    def test_backfill_reads_legacy_chat_id_and_skips_bad_rows(self):
        self.db.execute("CREATE TABLE connections(connection_id TEXT, account_id INTEGER)")
        self.db.execute(
            "CREATE TABLE messages(connection_id TEXT, chat_id INTEGER, message_id INTEGER, data TEXT)"
        )
        self.db.execute("INSERT INTO connections VALUES('c',100)")
        legacy = record(message_id=1)
        del legacy["chat_id"]
        self.db.execute(
            "INSERT INTO messages VALUES('c',200,1,?)", (json.dumps(legacy),)
        )
        broken = record(message_id=2)
        broken["text"] = {"unexpected": "object"}
        self.db.execute(
            "INSERT INTO messages VALUES('c',200,2,?)", (json.dumps(broken),)
        )
        self.db.execute("INSERT INTO messages VALUES('c',200,3,'not json')")
        self.db.commit()

        self.assertEqual(self.repo.backfill(), 1)
        self.assertEqual(self.repo.backfill(), 0)
        stats, _, _ = self.repo.stat(1, 100, 200)
        self.assertEqual(stats["total_messages"], 1)
        seen = self.db.execute("SELECT message_id FROM business_stats_seen").fetchall()
        self.assertEqual([row[0] for row in seen], [1])


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.repo = BusinessToolsRepository(self.db, {100: 1})
        self.repo.migrate()
        self.bot = FakeBot()
        self.notifications = []
        self.service = BusinessToolsService(
            self.bot, self.repo, lambda owner, text: self.notifications.append((owner, text)), {100: "Account 1"}
        )

    async def test_stats_sections_show_only_relevant_details(self):
        self.repo.record_message("c", 100, 1, 1, record(message_type="photo", text="подпись", message_id=1))
        overview = self.service.stats_text(1, 100, 200)
        media = self.service.stats_text(1, 100, 200, "media")
        words = self.service.stats_text(1, 100, 200, "words")
        self.assertIn("Сообщений: 1", overview)
        self.assertNotIn("<b>Медиа</b>", overview)
        self.assertIn("Фото: 1", media)
        self.assertIn("Топ слов", words)
        self.assertNotIn("Пик активности", words)

    @staticmethod
    def message(message_id, text=None):
        return SimpleNamespace(
            business_connection_id="c", message_id=message_id,
            chat=SimpleNamespace(id=200),
        )

    async def test_muted_incoming_is_deleted_and_suppressed(self):
        row = {"account_id": 100, "chat_id": 200, "display_name": "Tester", "username": "tester"}
        self.repo.mute(1, row, 0)
        result = await self.service.handle_business_message(
            self.message(5), record(message_id=5), 100, 1
        )
        self.assertEqual(result, "muted")
        self.assertEqual(self.bot.deleted, [("c", [5])])
        self.assertTrue(self.repo.consume_self_deleted("c", 200, 5))
        self.assertFalse(self.repo.consume_self_deleted("c", 200, 5))

    async def test_chat_command_is_not_counted_as_stat_message(self):
        command_record = record(sender_id=100, text="!mute 30m", message_id=6)
        result = await self.service.handle_business_message(
            self.message(6), command_record, 100, 1
        )
        self.assertEqual(result, "command")
        self.assertIsNotNone(self.repo.is_muted(100, 200))
        self.assertIsNone(self.repo.stat(1, 100, 200))
        self.assertEqual(len(self.notifications), 1)

    async def test_chat_command_succeeds_when_telegram_refuses_to_delete_it(self):
        self.bot.fail = True
        command_record = record(sender_id=100, text="!mute 30m", message_id=9)
        result = await self.service.handle_business_message(
            self.message(9), command_record, 100, 1
        )
        self.assertEqual(result, "command")
        self.assertIsNotNone(self.repo.is_muted(100, 200))
        self.assertIsNone(self.repo.stat(1, 100, 200))
        self.assertEqual(len(self.notifications), 1)
        self.assertIn("не разрешил удалить", self.notifications[0][1])
        self.assertFalse(self.repo.consume_self_deleted("c", 200, 9))

    async def test_delete_error_is_recorded_and_reported_once(self):
        self.bot.fail = True
        row = {"account_id": 100, "chat_id": 200, "display_name": "Tester", "username": "tester"}
        self.repo.mute(1, row, 0)
        with self.assertRaises(RuntimeError):
            await self.service.handle_business_message(self.message(7), record(message_id=7), 100, 1)
        self.assertIn("no permission", self.repo.is_muted(100, 200)["last_error"])
        self.assertEqual(len(self.notifications), 1)
        with self.assertRaises(RuntimeError):
            await self.service.handle_business_message(self.message(8), record(message_id=8), 100, 1)
        self.assertEqual(len(self.notifications), 1)


if __name__ == "__main__":
    unittest.main()
