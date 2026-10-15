import asyncio
import base64
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from encrypted_backup import restore_backup
from erasure_guard import ErasureGuard
from message_crypto import MessageCipher
from offsite_backup import prepare_bundle, join_bundle, send_bundle, deliver_once, backup_loop


class OffsiteBackupTests(unittest.TestCase):
    def test_wal_snapshot_encrypted_allowlist_split_join_restore_and_tamper(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db_path = root/'data.sqlite3'
            cipher = MessageCipher(base64.b64encode(b'z'*32).decode())
            ErasureGuard(root/'erasure-guard.aes', cipher).record(42, 12345)
            (root/'.env').write_text('BOT_TOKEN=secret; KEY=private')
            with closing(sqlite3.connect(db_path)) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE sample(value TEXT)')
                db.execute('INSERT INTO sample VALUES(?)', ('private text',))
                db.commit()
                out = root/'output'; out.mkdir()
                parts, manifest = prepare_bundle(db_path, cipher, out, part_bytes=1024)
                self.assertGreater(len(parts), 1)
                self.assertTrue(all(p.stat().st_size <= 1024 for p in parts))
                self.assertFalse(any(p.suffix == '.sqlite3' for p in out.iterdir()))
                self.assertNotIn(b'private text', b''.join(p.read_bytes() for p in parts))
                joined = root/'joined.zip'
                join_bundle(manifest, joined)
                with self.assertRaises(FileExistsError): join_bundle(manifest, joined)
                with zipfile.ZipFile(joined) as archive:
                    self.assertEqual(set(archive.namelist()), {'database.sqlite3.aes', 'erasure-guard.aes'})
                    archive.extractall(root/'restoring')
                restore_backup(root/'restoring/database.sqlite3.aes', root/'restored.sqlite3', cipher)
                with closing(sqlite3.connect(root/'restored.sqlite3')) as restored:
                    self.assertEqual(restored.execute('SELECT value FROM sample').fetchone()[0], 'private text')
                self.assertEqual(ErasureGuard(root/'restoring/erasure-guard.aes', cipher).read(), {'42':12345})
                parts[0].write_bytes(b'broken')
                with self.assertRaises(ValueError): join_bundle(manifest, root/'broken.zip')
                self.assertFalse((root/'broken.zip').exists())

    def test_single_file_and_ledger_required(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); path = root/'db.sqlite3'
            cipher = MessageCipher(base64.b64encode(b'x'*32).decode())
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE t(id INTEGER)'); db.commit()
            with self.assertRaises(FileNotFoundError): prepare_bundle(path, cipher, root)
            ErasureGuard(root/'erasure-guard.aes', cipher).record(0,0)
            parts, manifest = prepare_bundle(path, cipher, root)
            self.assertEqual(len(parts), 1)
            with zipfile.ZipFile(parts[0]) as archive: self.assertIsNone(archive.testzip())


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_schedule_survives_restart_and_failure_is_not_success(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder)/'db.sqlite3'
            state_path = Path(folder)/'offsite-backup-state.json'
            calls = 0
            async def stop_after_cycle(seconds):
                nonlocal calls
                calls += 1
                if calls == 2: raise asyncio.CancelledError()
            with patch('offsite_backup.deliver_once', new_callable=AsyncMock) as deliver, \
                 patch('offsite_backup.asyncio.sleep', side_effect=stop_after_cycle):
                with self.assertRaises(asyncio.CancelledError):
                    await backup_loop(None, db_path, None, '123')
                deliver.assert_awaited_once()
            saved = state_path.read_bytes()
            calls = 0
            with patch('offsite_backup.deliver_once', new_callable=AsyncMock) as deliver, \
                 patch('offsite_backup.asyncio.sleep', side_effect=stop_after_cycle):
                with self.assertRaises(asyncio.CancelledError):
                    await backup_loop(None, db_path, None, '123')
                deliver.assert_not_awaited()
            self.assertEqual(saved, state_path.read_bytes())
            state_path.unlink(); calls = 0
            with patch('offsite_backup.deliver_once', new_callable=AsyncMock, side_effect=OSError()) as deliver, \
                 patch('offsite_backup.asyncio.sleep', side_effect=stop_after_cycle) as sleep:
                with self.assertRaises(asyncio.CancelledError):
                    await backup_loop(None, db_path, None, '123')
                sleep.assert_any_call(3600)
                self.assertFalse(state_path.exists())

    async def test_disabled_and_group_rejected(self):
        bot = SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(type='group')), send_document=AsyncMock())
        await backup_loop(bot, 'unused', None, None)
        await backup_loop(bot, 'unused', None, '-100')
        bot.get_chat.assert_not_awaited()
        with self.assertRaises(ValueError): await deliver_once(bot, 1, 'unused', None)
        bot.send_document.assert_not_awaited()

    async def test_retry_reopens_stream_and_manifest_is_last(self):
        class Flood(Exception):
            error_code=429
            result_json={'parameters':{'retry_after':3}}
        calls=[]
        async def send(recipient, document, **kwargs):
            calls.append((recipient, Path(document.name).name, document.read()))
            if len(calls)==1: raise Flood()
        bot = SimpleNamespace(send_document=send)
        with tempfile.TemporaryDirectory() as folder:
            part=Path(folder)/'backup.zip';part.write_bytes(b'encrypted')
            manifest=Path(folder)/'manifest.json';manifest.write_bytes(b'{}')
            with patch('offsite_backup.asyncio.sleep', new_callable=AsyncMock) as sleep:
                await send_bundle(bot, 123, [part], manifest)
                self.assertEqual(calls, [(123,'backup.zip',b'encrypted'),(123,'backup.zip',b'encrypted'),(123,'manifest.json',b'{}')])
                sleep.assert_any_await(3)
