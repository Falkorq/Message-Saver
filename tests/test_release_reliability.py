import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
import os
import subprocess
import sys
from unittest.mock import AsyncMock, patch
from telebot.asyncio_helper import ApiTelegramException
from reliable import DurableInbox, InboxFull
from message_crypto import protected, STATS_FIELD
from erasure_guard import ErasureGuard
from release_support import Harness


class BusinessPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h=Harness();self.addCleanup(self.h.close)

    async def test_original_edit_multiple_edits_duplicate_and_delivery_text(self):
        h=self.h
        await h.feed(h.message())
        await h.feed(h.message(2,'свежая правка',int(time.time())))
        await h.feed(h.message(3,'последняя правка',int(time.time())))
        await h.feed(h.message(3,'последняя правка',int(time.time())))
        await h.feed(h.delete(4))
        self.assertEqual(h.repo.stat(1,100,200)[0]['total_messages'],1)
        metrics=h.repo.analytics(1,100,200)['metrics']
        self.assertEqual((metrics['p_edits'],metrics['p_deleted']),(2,1))
        payload=json.loads(h.db.execute('SELECT data FROM outbox ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertEqual(h.cipher.open_payload(payload)['old']['text'],'последняя правка')
        self.assertIsNone(h.ctx['load_message'](('c',200,1)))

    async def test_edit_before_original_and_same_second_cannot_overwrite(self):
        h=self.h;raw=h.message(1,'правка',int(time.time())-100)
        raw['edited_business_message']['date']=raw['edited_business_message']['edit_date']
        await h.feed(raw)
        original=h.message(2,'старое');original['business_message']['date']=raw['edited_business_message']['date']
        await h.feed(original)
        stored=h.ctx['load_message'](('c',200,1))
        self.assertEqual(h.cipher.open_payload(dict(old=stored))['old']['text'],'правка')
        self.assertEqual(h.repo.stat(1,100,200)[0]['total_messages'],1)

    async def test_deletion_before_late_original_stays_deleted(self):
        h=self.h
        await h.feed(h.delete(1));await h.feed(h.message(2))
        self.assertIsNone(h.ctx['load_message'](('c',200,1)))
        self.assertEqual(h.repo.stat(1,100,200)[0]['total_messages'],1)
        self.assertEqual(h.repo.analytics(1,100,200)['metrics']['p_deleted'],1)
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)
        await h.feed(h.delete(3,mid=2))
        h.db.execute('DELETE FROM message_versions WHERE message_id=2');h.db.commit()
        await h.feed(h.message(4,mid=2))
        self.assertIsNone(h.ctx['load_message'](('c',200,2)))
        self.assertEqual(h.repo.stat(1,100,200)[0]['total_messages'],2)
        self.assertEqual(h.repo.analytics(1,100,200)['metrics']['p_deleted'],2)

    async def test_filtered_own_notifications_still_count_events(self):
        h=self.h
        h.db.execute('INSERT INTO settings VALUES(1,1)');h.db.commit()
        original=h.message();original['business_message']['from']['id']=100
        changed=h.message(2,'правка',int(time.time()));changed['edited_business_message']['from']['id']=100
        await h.feed(original);await h.feed(changed);await h.feed(h.delete())
        metrics=h.repo.analytics(1,100,200)['metrics']
        self.assertEqual((metrics['o_n'],metrics['o_edits'],metrics['o_deleted']),(1,1,1))
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)

    async def test_business_bodies_are_never_opened_by_ingress_or_statistics(self):
        h=self.h;real_open=h.cipher.open
        def guard(value,field):
            self.assertIn(field,('incoming',STATS_FIELD));return real_open(value,field)
        with patch.object(h.cipher,'open',side_effect=guard):
            await h.feed(h.message());await h.feed(h.message(2,'правка',int(time.time())))
            h.repo.analytics(1,100,200);await h.feed(h.delete())
        dump='\n'.join(h.db.iterdump())
        self.assertNotIn('исходное синтетическое сообщение',dump)
        self.assertNotIn('правка',dump)

    async def test_atomic_failure_after_outbox_creation_rolls_back_all_effects(self):
        h=self.h;await h.feed(h.message())
        raw=h.message(2,'правка',int(time.time()));h.inbox.persist([raw],h.ctx['normalize_update'])
        row=h.inbox.next();real=h.inbox.done
        with patch.object(h.inbox,'done',side_effect=RuntimeError('crash before checkpoint')):
            with self.assertRaises(RuntimeError):await h.ctx['dispatch_incoming'](2,h.inbox.payload(row))
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)
        self.assertEqual(h.repo.analytics(1,100,200)['metrics']['p_edits'],0)
        await h.ctx['dispatch_incoming'](2,h.inbox.payload(row))
        self.assertEqual(h.repo.analytics(1,100,200)['metrics']['p_edits'],1)
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)

    async def test_reopen_recovers_durable_payload_without_plaintext(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'synthetic.sqlite3');h=Harness(path)
            h.inbox.persist([h.message()],h.ctx['normalize_update']);self.assertEqual(h.inbox.offset,2);h.close()
            h=Harness(path)
            try:
                row=h.inbox.next();await h.ctx['dispatch_incoming'](1,h.inbox.payload(row))
                self.assertEqual(h.repo.stat(1,100,200)[0]['total_messages'],1)
                self.assertIsNone(h.inbox.next())
            finally:h.close()

    async def test_capacity_rejects_batch_without_advancing_offset(self):
        h=self.h;h.inbox.max_rows=1
        with self.assertRaises(InboxFull):
            h.inbox.persist([h.message(1),h.message(2,mid=2)],h.ctx['normalize_update'])
        self.assertIsNone(h.inbox.offset);self.assertIsNone(h.inbox.next())

    async def test_poll_never_confirms_before_persist(self):
        h=self.h;calls=[]
        async def get(**kwargs):
            calls.append(kwargs['offset'])
            if len(calls)==1:return [h.message()]
            self.assertIsNotNone(h.inbox.next());raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):await h.inbox.poll(get,h.ctx['normalize_update'])
        self.assertEqual(calls,[None,2])

    async def test_failed_connection_preserves_order_without_blocking_other_connection(self):
        h=self.h;other=h.message(3,mid=3);other['business_message']['business_connection_id']='d'
        h.inbox.persist([h.message(1),h.message(2,mid=2),other],h.ctx['normalize_update'])
        called=[]
        async def dispatch(uid,payload):
            called.append(uid)
            if uid==1:raise RuntimeError('synthetic transient error')
            h.inbox.done(uid)
        worker=asyncio.create_task(h.inbox.work(dispatch))
        try:await asyncio.sleep(.2)
        finally:worker.cancel();await asyncio.gather(worker,return_exceptions=True)
        self.assertEqual(called,[1,3])
        self.assertIsNone(h.db.execute('SELECT done_at FROM incoming_updates WHERE update_id=2').fetchone()[0])

    async def test_real_process_exit_before_checkpoint_rolls_back_then_recovers(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'crash.sqlite3')
            source=Path(__file__).resolve().parents[1]
            script='import asyncio,os;from release_support import Harness;h=Harness('+repr(path)+');h.inbox.done=lambda uid:os._exit(88);asyncio.run(h.feed(h.message()))'
            environment=dict(os.environ,PYTHONPATH=str(source)+os.pathsep+str(source/'tests'))
            result=await asyncio.to_thread(subprocess.run,[sys.executable,'-c',script],env=environment,capture_output=True,timeout=20)
            self.assertEqual(result.returncode,88)
            h=Harness(path)
            try:
                self.assertIsNone(h.repo.stat(1,100,200));row=h.inbox.next();self.assertIsNotNone(row)
                await h.ctx['dispatch_incoming'](row['update_id'],h.inbox.payload(row))
                self.assertEqual(h.repo.stat(1,100,200)[0]['total_messages'],1)
                self.assertIsNone(h.inbox.next())
            finally:h.close()

    async def test_erase_owner_only_blocks_backfill_queued_and_restart_restore(self):
        h=self.h;await h.feed(h.message())
        other=h.message(2,mid=2);other['business_message']['business_connection_id']='d'
        await h.feed(other)
        pending=h.message(3,mid=3);h.inbox.persist([pending],h.ctx['normalize_update'])
        with tempfile.TemporaryDirectory() as folder:
            guard=ErasureGuard(Path(folder)/'guard.aes',h.cipher);h.product.erasure_guard=guard
            h.product.erase(1,h.inbox)
            self.assertIsNone(h.repo.stat(1,100,200));self.assertIsNotNone(h.repo.stat(3,300,200))
            self.assertFalse(h.product.allows(100));self.assertIsNone(h.inbox.next())
            h.repo.backfill();self.assertIsNone(h.repo.stat(1,100,200))
            self.assertNotIn('100',guard.path.read_text())
            await h.feed(h.message(4,mid=4));self.assertIsNone(h.repo.stat(1,100,200))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_429_cools_recipient_while_other_recipient_continues(self):
        h=Harness();self.addCleanup(h.close)
        for recipient in (1,1,3):
            h.ctx['enqueue'](recipient,dict(event='text_notification',source='business_tools',owner_id=recipient,text='private synthetic notification'))
        h.db.commit();sent=[]
        async def send(recipient,*args,**kwargs):
            sent.append(recipient)
            if recipient==1:
                raise ApiTelegramException('sendMessage',None,dict(error_code=429,description='private text must not be logged',parameters={'retry_after':60}))
        h.ctx['send_notification']=send
        worker=asyncio.create_task(h.ctx['outbox_worker']())
        try:
            await asyncio.sleep(.25)
        finally:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)
        self.assertEqual(sent.count(1),1);self.assertIn(3,sent)
        until=h.db.execute('SELECT until_at FROM delivery_cooldowns WHERE recipient_id=1').fetchone()[0]
        self.assertGreater(until,time.time()+58)
        dump='\n'.join(h.db.iterdump());self.assertNotIn('private text must not be logged',dump)
        again=DurableInbox(h.db,h.cipher)
        self.assertGreater(h.db.execute('SELECT until_at FROM delivery_cooldowns WHERE recipient_id=1').fetchone()[0],time.time()+58)

    async def test_revoked_or_paused_payload_is_discarded(self):
        h=Harness();self.addCleanup(h.close)
        h.ctx['enqueue'](1,dict(event='text_notification',source='business_tools',owner_id=1,text='synthetic'))
        h.db.commit();row=h.db.execute('SELECT * FROM outbox').fetchone()
        h.routes.pop(100);h.ctx['send_notification']=AsyncMock()
        await h.ctx['deliver_row'](row)
        h.ctx['send_notification'].assert_not_awaited()
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)
