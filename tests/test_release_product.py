import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from telethon.errors import FloodWaitError
from profile_monitor.module import ProfileMonitorModule
from profile_monitor.telegram_client import cooldown_guard, ProfileTelegramClient
from profile_monitor.access import MTProtoAccess
from public_ui import PublicUI
from release_support import Harness


class OptionalModuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_mtproto_does_not_register_or_require_public_menu(self):
        h=Harness();self.addCleanup(h.close)
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{},clear=True):
            module=ProfileMonitorModule(h.bot,h.db,h.routes,folder,lambda *args:None)
            await module.start();self.assertEqual(module.state,'disabled');await module.close()
        ui=PublicUI(h.bot,h.db,h.routes,{},h.product,h.inbox,lambda:None,lambda:module,MTProtoAccess(h.routes,{100}))
        await ui.welcome(999)
        self.assertIn('по приглашению',h.bot.send_photo.await_args.kwargs['caption'])
        self.assertNotIn('TELEGRAM_SESSION',h.bot.send_photo.await_args.kwargs['caption'])

    async def test_failed_session_is_background_state_and_recovers(self):
        module=object.__new__(ProfileMonitorModule)
        module.enabled=True;module.state='disabled';module.owner_routes={100:1}
        module.access_policy=SimpleNamespace(operator_ids={1});module.enqueue_notification=lambda *args:None
        module.telegram=SimpleNamespace(start=AsyncMock(side_effect=RuntimeError('synthetic invalid session')),close=AsyncMock())
        module.scheduler=SimpleNamespace(run=AsyncMock())
        await module.start()
        await asyncio.sleep(.01)
        self.assertEqual(module.state,'unavailable')
        await module.close();module.telegram.close.assert_awaited()

    async def test_global_mt_cooldown_including_manual_and_avatar_paths(self):
        class Fake:
            cooldown_until=0
            db=None
            @cooldown_guard
            async def profile(self):raise FloodWaitError(request=None,capture=1200)
            @cooldown_guard
            async def avatar(self):raise AssertionError('must not call Telegram')
        client=Fake()
        with self.assertRaises(FloodWaitError):await client.profile()
        self.assertGreater(client.cooldown_until,time.time()+1198)
        with self.assertRaises(FloodWaitError) as context:await client.avatar()
        self.assertGreater(context.exception.seconds,1198)

    async def test_waiting_avatar_rechecks_cooldown_after_lock(self):
        client=object.__new__(ProfileTelegramClient)
        client.cooldown_until=0;client.db=None;client.avatar_lock=asyncio.Lock()
        client._fetch=AsyncMock(side_effect=FloodWaitError(request=None,capture=1200))
        await client.avatar_lock.acquire()
        first=asyncio.create_task(client.fetch('synthetic'))
        second=asyncio.create_task(client.avatar(123))
        await asyncio.sleep(0)
        client.avatar_lock.release()
        results=await asyncio.gather(first,second,return_exceptions=True)
        self.assertTrue(all(isinstance(value,FloodWaitError) for value in results))
        self.assertGreater(client.cooldown_until,time.time()+1198)


class ProductTests(unittest.IsolatedAsyncioTestCase):
    async def test_peer_notification_setting_preserves_collection_and_own_notifications(self):
        h=Harness();self.addCleanup(h.close)
        h.db.execute('INSERT INTO notification_preferences(owner_id,peers) VALUES(1,0)');h.db.commit()
        await h.feed(h.message())
        await h.feed(h.message(2,'правка',int(time.time())))
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)
        self.assertEqual(h.repo.stat(1,100,200)[0]['total_messages'],1)
        original=h.message(3,mid=2);original['business_message']['from']['id']=100
        changed=h.message(4,'своя правка',int(time.time()),mid=2);changed['edited_business_message']['from']['id']=100
        await h.feed(original);await h.feed(changed)
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)

    async def test_late_grant_has_account_continuity_and_unknown_days(self):
        h=Harness();self.addCleanup(h.close)
        from telebot import types
        connection=types.BusinessConnection.de_json(dict(id='late',user=dict(id=500,is_bot=False,first_name='Synthetic'),user_chat_id=500,date=int(time.time()),is_enabled=True,rights=dict(can_read_messages=True,can_delete_all_messages=False)))
        await h.ctx['store_connection'](connection)
        self.assertEqual(h.db.execute("SELECT recipient_id FROM connections WHERE connection_id='late'").fetchone()[0],0)
        h.routes[500]=5;h.product.ensure(500,5)
        h.repo.reset_continuity(500)
        await h.ctx['store_connection'](connection)
        rights=json.loads(h.db.execute('SELECT rights FROM account_state WHERE account_id=500').fetchone()[0])
        self.assertTrue(rights['can_read_messages']);self.assertFalse(rights['can_delete_all_messages'])
        from business_tools.web_metrics import series
        chart,_,_=series(h.repo,5,500,'7',int(time.time()))
        self.assertIsNone(chart[0]['own'])
        self.assertNotEqual(h.product.states(5)[0]['state'],'connected')

    async def test_pause_stops_collection_and_resumption_rejects_old_queued_events(self):
        h=Harness();self.addCleanup(h.close)
        raw=h.message();payload=h.ctx['normalize_update'](raw)
        h.product.collection(1,False)
        h.product.collection(1,True)
        with h.db.event_transaction():h.ctx['apply_business'](1,payload,1)
        self.assertIsNone(h.repo.stat(1,100,200))
        payload['received_at']=int(time.time())+1
        with h.db.event_transaction():h.ctx['apply_business'](2,payload,1)
        self.assertEqual(h.repo.stat(1,100,200)[0]['total_messages'],1)

    async def test_no_public_auto_delete_activation_even_with_duplicate_accounts(self):
        h=Harness();self.addCleanup(h.close)
        await h.feed(h.message())
        with self.assertRaisesRegex(ValueError,'Автоудаление|автоудаление'):
            h.repo.mute(1,h.repo.stat(1,100,200)[0],0)
        self.assertEqual(h.repo.active_mutes(1),[])
        first=dict(h.repo.stat(1,100,200)[0])
        h.routes[101]=1
        h.repo._legacy_mute(1,first,0)
        h.repo._legacy_mute(1,dict(first,account_id=101),0)
        ui=PublicUI(h.bot,h.db,h.routes,{},h.product,h.inbox,lambda:SimpleNamespace(repo=h.repo),lambda:None,MTProtoAccess(h.routes,{100}))
        await ui.page(100,'rules')
        buttons=str(h.bot.send_photo.await_args.kwargs['reply_markup'].to_dict())
        self.assertIn('ny:unrule:100:200',buttons)
        self.assertIn('ny:unrule:101:200',buttons)

    async def test_real_public_buttons_and_confirmation_check_access_and_nonce(self):
        h=Harness();self.addCleanup(h.close)
        handlers={}
        class Bot:
            send_message=AsyncMock()
            send_photo=AsyncMock()
            answer_callback_query=AsyncMock()
            def message_handler(self,**kwargs):return lambda fn:fn
            def callback_query_handler(self,**kwargs):
                def register(fn):handlers['callback']=fn;return fn
                return register
        bot=Bot();ui=PublicUI(bot,h.db,h.routes,{},h.product,h.inbox,lambda:None,lambda:None,MTProtoAccess(h.routes,{100}))
        ui.register()
        def call(actor,data):return SimpleNamespace(from_user=SimpleNamespace(id=actor),id='callback',data=data,
                         message=SimpleNamespace(chat=SimpleNamespace(type='private',id=actor)))
        await handlers['callback'](call(100,'ny:erase'))
        nonce=h.db.execute('SELECT nonce FROM data_confirmations WHERE owner_id=1').fetchone()[0]
        await handlers['callback'](call(300,'ny:confirm:'+nonce))
        self.assertTrue(h.product.allows(100))
        await handlers['callback'](call(100,'ny:confirm:forged'))
        self.assertTrue(h.product.allows(100))
        await handlers['callback'](call(100,'ny:confirm:'+nonce))
        self.assertFalse(h.product.allows(100));self.assertTrue(h.product.allows(300))
