from types import SimpleNamespace
import ast
import base64
import copy
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch
from contextlib import closing

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from message_crypto import MessageCipher, EncryptionError, FIELDS, STATS_FIELD, protected
from generate_encryption_key import generate_key
from migrate_message_texts import migrate
from encrypted_backup import restore_backup
from business_tools.repository import BusinessToolsRepository
from business_tools.service import BusinessToolsService


def key():return base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode()
def record(text='Секретная фраза: коты коты привет 🎀'):
    return dict(text=text,caption=None,details='секрет контакта',type='text',sender_id=200,
                chat_id=200,chat_name='Alice',sent_at=int(time.time()),metadata={},file_unique_id=None)


class CryptoTests(unittest.TestCase):
    def setUp(self):self.cipher=MessageCipher(key())

    def test_unicode_roundtrip_randomized_and_idempotent(self):
        original=record();encrypted=self.cipher.protect_record(original)
        self.assertNotIn(original['text'],json.dumps(encrypted,ensure_ascii=False))
        self.assertEqual(self.cipher.open_payload({'old':encrypted})['old'],original)
        self.assertNotEqual(encrypted['text'],self.cipher.protect_record(original)['text'])
        self.assertEqual(self.cipher.protect_record(encrypted),encrypted)
        self.assertEqual(original['text'],record()['text'])
        for text in ('',None,'aes256gcm-v1:обычный текст'):
            r=record(text);e=self.cipher.protect_record(r)
            self.assertEqual(self.cipher.open_payload({'old':e})['old'],r)
            self.assertEqual(self.cipher.protect_record(e),e)

    def test_integrity_wrong_key_and_comparison_without_opening_body(self):
        original=record();encrypted=self.cipher.protect_record(original)
        with patch.object(self.cipher,'open',side_effect=AssertionError('body was decrypted')):
            self.assertTrue(self.cipher.records_equal(encrypted,original))
            self.assertFalse(self.cipher.records_equal(encrypted,dict(original,text='другое')))
            self.assertFalse(self.cipher.records_equal(encrypted,dict(original,edit_at=123)))
        with self.assertRaises(EncryptionError):MessageCipher(key()).open_payload({'old':encrypted})
        altered=copy.deepcopy(encrypted);altered['text']['data']=altered['caption'] if altered['caption'] else base64.urlsafe_b64encode(b'x'*40).decode()
        with self.assertRaises(EncryptionError):self.cipher.open_payload({'old':altered})
        moved=copy.deepcopy(encrypted);moved['caption']=moved['text']
        with self.assertRaises(EncryptionError):self.cipher.open_payload({'old':moved})

    def test_stats_only_open_the_snapshot(self):
        encrypted=self.cipher.protect_record(record())
        real=self.cipher.open
        fields=[]
        def opened(value,field):fields.append(field);return real(value,field)
        with patch.object(self.cipher,'open',side_effect=opened):
            summary=self.cipher.stats_record(encrypted)
        self.assertEqual(fields,[STATS_FIELD]);self.assertTrue(protected(summary['text']))

    def test_key_only_from_dotenv_and_generator_never_rotates(self):
        with tempfile.TemporaryDirectory() as folder:
            env=Path(folder)/'.env';env.write_text('BOT_TOKEN=example\n',encoding='utf-8')
            with patch.dict(os.environ,{'MESSAGE_ENCRYPTION_KEY':key()}):
                with self.assertRaises(EncryptionError):MessageCipher.from_env(env)
            self.assertTrue(generate_key(env));before=env.read_bytes()
            self.assertFalse(generate_key(env));self.assertEqual(env.read_bytes(),before)
            MessageCipher.from_env(env)
        for bad in ('','password',base64.urlsafe_b64encode(b'x'*16).decode()):
            with self.assertRaises(EncryptionError):MessageCipher(bad)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
        self.env=self.folder/'.env';self.env.write_text('MESSAGE_ENCRYPTION_KEY='+key()+'\n',encoding='utf-8')
        self.cipher=MessageCipher.from_env(self.env);self.path=self.folder/'messages.sqlite3'
        self.db=sqlite3.connect(self.path);self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
          CREATE TABLE messages(connection_id TEXT,chat_id INTEGER,message_id INTEGER,data TEXT,
            created_at INTEGER,PRIMARY KEY(connection_id,chat_id,message_id));
          CREATE TABLE outbox(id INTEGER PRIMARY KEY,recipient_id INTEGER,data TEXT,attempts INTEGER);
          CREATE TABLE unrelated(id INTEGER PRIMARY KEY,value TEXT); INSERT INTO unrelated VALUES(1,'unchanged');''')
        self.original=record()
        self.db.execute('INSERT INTO messages(rowid,connection_id,chat_id,message_id,data,created_at) VALUES(-2,?,?,?,?,?)',
            ('conn',200,1,json.dumps(self.original,ensure_ascii=False),12345))
        self.payload={'event':'edit','account_id':100,'old':self.original,'new':dict(self.original,text='новый текст')}
        self.db.execute('INSERT INTO outbox VALUES(7,100,?,3)',(json.dumps(self.payload,ensure_ascii=False),))
        self.db.commit()

    def tearDown(self):self.db.close();self.temp.cleanup()

    def test_backup_atomic_conversion_repeat_and_unchanged_schema(self):
        schema=self.db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall()
        raw=self.db.execute('SELECT data FROM messages').fetchone()[0]
        counts,backup=migrate(self.path,self.env)
        self.assertEqual(counts,{'messages':1,'outbox':1});self.assertIsNone(backup)
        self.assertEqual(raw,self.db.execute('SELECT data FROM messages').fetchone()[0])
        self.db.close();self.db=sqlite3.connect(self.path);self.db.row_factory=sqlite3.Row
        counts,backup=migrate(self.path,self.env,True,True)
        self.assertTrue(backup.is_file());self.assertNotIn(self.original['text'].encode(),backup.read_bytes())
        encrypted=json.loads(self.db.execute('SELECT data FROM messages').fetchone()[0])
        self.assertEqual(self.cipher.open_payload({'old':encrypted})['old'],self.original)
        self.assertEqual(tuple(self.db.execute('SELECT rowid,created_at FROM messages').fetchone()),(-2,12345))
        self.assertEqual(tuple(self.db.execute('SELECT id,recipient_id,attempts FROM outbox').fetchone()),(7,100,3))
        self.assertEqual(schema,self.db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall())
        saved=self.db.execute('SELECT data FROM messages').fetchone()[0]
        self.assertEqual(migrate(self.path,self.env,True,True),({'messages':0,'outbox':0},None))
        self.assertEqual(saved,self.db.execute('SELECT data FROM messages').fetchone()[0])
        self.assertNotIn(self.original['text'].encode(),self.path.read_bytes())
        restored=self.folder/'restored.sqlite3';restore_backup(backup,restored,self.cipher)
        with closing(sqlite3.connect(restored)) as recovered:
            self.assertEqual(recovered.execute('SELECT data FROM messages').fetchone()[0],raw)
        with self.assertRaises(FileExistsError):restore_backup(backup,restored,self.cipher)

    def test_corrupted_rows_and_wrong_key_leave_everything_unchanged(self):
        self.db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',('conn',200,2,'broken JSON',9));self.db.commit()
        before=self.db.execute('SELECT data FROM messages ORDER BY message_id').fetchall()
        with self.assertRaises(EncryptionError):migrate(self.path,self.env,True,True)
        self.assertEqual(before,self.db.execute('SELECT data FROM messages ORDER BY message_id').fetchall())
        self.assertFalse((self.folder/'encryption_backups').exists())
        self.db.execute('DELETE FROM messages WHERE message_id=2');self.db.commit()
        migrate(self.path,self.env,True,True)
        self.env.write_text('MESSAGE_ENCRYPTION_KEY='+key(),encoding='utf-8')
        before=self.db.execute('SELECT data FROM messages').fetchone()[0]
        with self.assertRaises(EncryptionError):migrate(self.path,self.env,True,True)
        self.assertEqual(before,self.db.execute('SELECT data FROM messages').fetchone()[0])

    def test_apply_requires_stop_and_does_not_create_missing_db(self):
        with self.assertRaises(EncryptionError):migrate(self.path,self.env,True)
        missing=self.folder/'missing.sqlite3'
        with self.assertRaises(EncryptionError):migrate(missing,self.env)
        self.assertFalse(missing.exists())

    def test_mid_transaction_failure_rolls_back_and_keeps_backup(self):
        self.db.execute("CREATE TRIGGER deny_outbox BEFORE UPDATE ON outbox BEGIN SELECT RAISE(ABORT,'blocked'); END")
        self.db.commit();before=self.db.execute('SELECT data FROM messages').fetchone()[0]
        with self.assertRaises(sqlite3.DatabaseError):migrate(self.path,self.env,True,True)
        self.assertEqual(self.db.execute('SELECT data FROM messages').fetchone()[0],before)
        backup=next((self.folder/'encryption_backups').glob('*.aes'))
        damaged=self.folder/'damaged.aes';content=bytearray(backup.read_bytes());content[-1]^=1;damaged.write_bytes(content)
        target=self.folder/'bad-restore.sqlite3'
        with self.assertRaises(EncryptionError):restore_backup(damaged,target,self.cipher)
        self.assertFalse(target.exists())


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cipher=MessageCipher(key());self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.executescript('''CREATE TABLE messages(connection_id TEXT,chat_id INTEGER,message_id INTEGER,data TEXT,created_at INTEGER,
          PRIMARY KEY(connection_id,chat_id,message_id));
          CREATE TABLE connections(connection_id TEXT,account_id INTEGER);
          CREATE TABLE outbox(id INTEGER PRIMARY KEY,recipient_id INTEGER,data TEXT,created_at INTEGER);''')
        self.repo=BusinessToolsRepository(self.db,{100:1},message_cipher=self.cipher);self.repo.migrate()

    def tearDown(self):self.db.close()

    def application_functions(self,names,extra=None):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text(encoding='utf-8'))
        nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
        context={'db':self.db,'json':json,'time':time,'message_cipher':self.cipher,'product':SimpleNamespace(notifications=lambda *args:True)}
        context.update(extra or {})
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'app.py','exec'),context)
        return context

    async def test_actual_save_load_enqueue_and_delivery_boundary(self):
        bot=Mock();bot.send_message=AsyncMock()
        ctx=self.application_functions({'save_message','load_message','enqueue','send_notification'}, {'bot':bot,'limited_send':lambda method,*args,**kwargs:method(*args,**kwargs)})
        original=record();ctx['save_message'](('conn',200,1),original)
        loaded=ctx['load_message'](('conn',200,1));self.assertTrue(protected(loaded['text']))
        ctx['enqueue'](100,{'event':'text_notification','text':original['text']})
        data=self.db.execute('SELECT data FROM outbox').fetchone()[0]
        self.assertNotIn(original['text'],data)
        await ctx['send_notification'](100,json.loads(data))
        bot.send_message.assert_awaited_once_with(100,original['text'],parse_mode=None)

    async def test_backfill_and_edit_delete_match_plain_statistics(self):
        original=record();original['details']=None
        self.db.execute('INSERT INTO connections VALUES(?,?)',('conn',100))
        self.db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',('conn',200,1,json.dumps(self.cipher.protect_record(original)),original['sent_at']))
        self.db.commit()
        real_open=self.cipher.open
        def only_stats(value,field):
            self.assertEqual(field,STATS_FIELD);return real_open(value,field)
        with patch.object(self.cipher,'open',side_effect=only_stats):
            self.assertEqual(self.repo.backfill(),1)
            saved=self.cipher.protect_record(original)
            service=BusinessToolsService(None,self.repo,None,{})
            service.record_edit(1,100,('conn',200,1),saved,dict(original,text='правка',edit_at=original['sent_at']+1))
            service.record_deletion(1,100,('conn',200,1),saved)
        stats=self.repo.stat(1,100,200)[0]
        self.assertEqual(stats['total_messages'],1);self.assertEqual(stats['peer_chars'],len(original['text']))
        words=dict(self.db.execute('SELECT word,count FROM business_stats_words'))
        self.assertEqual(words['коты'],2)
        detail=self.repo.analytics(1,100,200,'all')
        self.assertEqual(detail['metrics']['p_deleted'],1);self.assertEqual(detail['metrics']['p_edits'],1)

    async def test_actual_delete_and_edit_deliver_readable_originals(self):
        rich=AsyncMock();media=AsyncMock()
        ctx=self.application_functions({'send_notification','describe'}, {'send_rich_text':rich,'send_media':media})
        original=record();changed=dict(original,text='Новый читаемый текст')
        for event in ('delete','edit'):
            rich.reset_mock()
            payload={'event':event,'account_id':100,'old':original}
            if event=='edit':payload['new']=changed
            encrypted=self.cipher.protect_payload(payload)
            await ctx['send_notification'](100,encrypted)
            args=rich.await_args.args
            self.assertEqual(args[4]['text'],original['text'])
            self.assertEqual(args[5],original['text'])
            if event=='edit':self.assertEqual(args[6],changed['text'])

    async def test_every_statistics_table_matches_plain_history(self):
        other=sqlite3.connect(':memory:');other.row_factory=sqlite3.Row
        try:
            other.executescript('''CREATE TABLE messages(connection_id TEXT,chat_id INTEGER,message_id INTEGER,data TEXT,created_at INTEGER,
                PRIMARY KEY(connection_id,chat_id,message_id));CREATE TABLE connections(connection_id TEXT,account_id INTEGER);''')
            plain=BusinessToolsRepository(other,{100:1});plain.migrate()
            stamp=int(time.time())-86400
            inputs=[record('коты коты привет!'),dict(record(None),type='voice',caption='Привет завтра',metadata={'duration':12}),dict(record('ответ вечером'),sender_id=100)]
            for db,encrypted in ((self.db,True),(other,False)):
                db.execute('INSERT INTO connections VALUES(?,?)',('conn',100))
                for i,source in enumerate(inputs,1):
                    source=dict(source,sent_at=stamp+i*120,_observed_at=stamp+i*120)
                    stored=self.cipher.protect_record(source) if encrypted else source
                    db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',('conn',200,i,json.dumps(stored),source['sent_at']))
                db.commit()
            self.repo.backfill();plain.backfill()
            for name, in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'business_stats_%' AND name NOT IN ('business_stats_progress')"):
                self.assertEqual([tuple(r) for r in self.db.execute(f'SELECT * FROM {name} ORDER BY rowid')],
                                 [tuple(r) for r in other.execute(f'SELECT * FROM {name} ORDER BY rowid')],name)
        finally:other.close()


if __name__=='__main__':unittest.main()
