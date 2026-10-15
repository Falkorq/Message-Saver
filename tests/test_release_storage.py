import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from encrypted_backup import restore_backup, encrypt_backup
from message_crypto import MessageCipher
from erasure_guard import ErasureGuard
from release_support import Harness


class StorageReleaseTests(unittest.TestCase):
    def test_real_bootstrap_backup_and_quarantined_restore_use_same_key(self):
        source=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            # Copy code only; never use the real .env, sessions, DB or profile directory.
            for path in source.glob('*.py'):shutil.copy2(path,root/path.name)
            for name in ('business_tools','profile_monitor'):
                shutil.copytree(source/name,root/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            key=base64.b64encode(b's'*32).decode()
            env_text=f'BOT_TOKEN=123456789:synthetic_token_for_local_test\nMESSAGE_ENCRYPTION_KEY={key}\nMY_CHAT_ID=1\nMY_ACCOUNT_IDS=100\nDB_PATH=synthetic.sqlite3\n'
            (root/'.env').write_text(env_text,encoding='utf-8')
            environment={k:v for k,v in os.environ.items() if k.upper() in ('PATH','SYSTEMROOT','WINDIR','TEMP','TMP','COMSPEC')}
            environment['PYTHONUTF8']='1'
            def run(*args,success=True):
                result=subprocess.run([sys.executable,*args],cwd=root,env=environment,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=30)
                if success:self.assertEqual(result.returncode,0,result.stderr)
                else:self.assertNotEqual(result.returncode,0)
                self.assertNotIn(key,result.stdout+result.stderr)
                return result
            run('-c','import app; assert app.profile_monitor.state=="disabled"; app.db.close()')
            self.assertEqual((root/'.env').read_text(encoding='utf-8'),env_text)
            run('storage_admin.py','backup')
            backup=next((root/'backups').glob('*-regular-*.aes'))
            self.assertFalse(list((root/'backups').glob('*.sqlite3')))
            guard=root/'erasure-guard.aes'
            run('storage_admin.py','restore','--source',str(backup),'--target',str(root/'restored.sqlite3'),'--ledger',str(guard))
            import sqlite3
            db=sqlite3.connect(root/'restored.sqlite3')
            try:
                self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM invited_accounts').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT SUM(collecting) FROM account_state').fetchone()[0],0)
            finally:db.close()
            self.assertEqual((root/'.env').read_text(encoding='utf-8'),env_text)
            # An older snapshot has no account_state. Current erasures still apply before reactivation.
            db=sqlite3.connect(root/'synthetic.sqlite3')
            db.execute('INSERT INTO business_chat_stats(owner_id,account_id,chat_id,total_messages) VALUES(1,100,200,7)')
            db.execute('DROP TABLE account_state');db.commit();db.close()
            ErasureGuard(guard,MessageCipher(key)).record(1,__import__('time').time_ns()//1000000)
            run('storage_admin.py','backup')
            older=max((root/'backups').glob('*-regular-*.aes'),key=lambda p:p.stat().st_mtime_ns)
            run('storage_admin.py','restore','--source',str(older),'--target',str(root/'legacy-restored.sqlite3'),'--ledger',str(guard))
            db=sqlite3.connect(root/'legacy-restored.sqlite3')
            try:self.assertEqual(db.execute('SELECT COUNT(*) FROM business_chat_stats WHERE owner_id=1').fetchone()[0],0)
            finally:db.close()
            db=sqlite3.connect(root/'synthetic.sqlite3')
            try:self.assertEqual(db.execute('SELECT total_messages FROM business_chat_stats').fetchone()[0],7)
            finally:db.close()
            db=sqlite3.connect(root/'unsupported.sqlite3');db.execute('CREATE TABLE arbitrary(value INTEGER)');db.commit();db.close()
            encrypt_backup(root/'unsupported.sqlite3',root/'unsupported.aes',MessageCipher(key))
            run('storage_admin.py','restore','--source',str(root/'unsupported.aes'),'--target',str(root/'rejected.sqlite3'),'--ledger',str(guard),success=False)
            self.assertFalse((root/'rejected.sqlite3').exists())

    def test_erasure_ledger_reapplies_when_erasure_was_interrupted_after_pause(self):
        h=Harness();self.addCleanup(h.close)
        h.db.execute("INSERT INTO business_chat_stats(owner_id,account_id,chat_id,total_messages) VALUES(1,100,200,1)");h.db.commit()
        with tempfile.TemporaryDirectory() as folder:
            guard=ErasureGuard(Path(folder)/'ledger.aes',h.cipher)
            stamp=__import__('time').time_ns()//1000000
            guard.record(1,stamp)
            h.product.collection(1,False)  # crash after the collection barrier, before deletion
            self.assertIsNotNone(h.repo.stat(1,100,200))
            guard.apply(h.product,h.inbox)
            self.assertIsNone(h.repo.stat(1,100,200))
            self.assertGreaterEqual(h.db.execute('SELECT erased_through FROM account_state WHERE account_id=100').fetchone()[0],stamp)
