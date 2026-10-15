import ast
import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from telebot.asyncio_helper import ApiTelegramException
from public_ui import PublicUI
from profile_monitor.ui import ProfileUI
from reliable import ensure_outbox_indexes
from release_support import Harness
from ui_banners import BannerCache, banner_for


def message(file_id='menu-file', actor=100, mid=20):
    return SimpleNamespace(message_id=mid, chat=SimpleNamespace(id=actor,type='private'),
                           photo=[SimpleNamespace(file_id=file_id)])


def api_error(code, description):
    return ApiTelegramException('synthetic', None, {'error_code':code,'description':description,
        'parameters':{'retry_after':60}})


class BannerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_reuses_ids_and_detects_replaced_image(self):
        cache=BannerCache()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'banner.png';path.write_bytes(b'first image')
            received=[]
            async def send(source):
                received.append(source if isinstance(source,str) else source.read())
                return message('file-'+str(len(received)))
            await cache.use(path,send);await cache.use(path,send)
            path.write_bytes(b'replaced image')
            await cache.use(path,send)
            self.assertEqual(received,[b'first image','file-1',b'replaced image'])

    async def test_invalid_id_uploads_again_but_429_does_not_retry(self):
        cache=BannerCache()
        path=banner_for('menu')
        await cache.use(path,AsyncMock(return_value=message()))
        inputs=[]
        async def expired(source):
            inputs.append(source)
            if isinstance(source,str):raise api_error(400,'Bad Request: wrong file identifier/HTTP URL specified')
            return message('renewed-file')
        await cache.use(path,expired)
        self.assertEqual(inputs[0],'menu-file');self.assertFalse(isinstance(inputs[1],str))
        limited=AsyncMock(side_effect=api_error(429,'Too Many Requests'))
        with self.assertRaises(ApiTelegramException):await cache.use(path,limited)
        self.assertEqual(limited.await_count,1)

    async def test_slow_recipient_does_not_lock_other_recipients(self):
        cache=BannerCache();path=banner_for('menu')
        await cache.use(path,AsyncMock(return_value=message()))
        entered=asyncio.Event();release=asyncio.Event()
        async def slow(source):
            entered.set();await release.wait();return message()
        task=asyncio.create_task(cache.use(path,slow))
        await entered.wait()
        try:
            result=await asyncio.wait_for(cache.use(path,AsyncMock(return_value=message('fast-file'))),.5)
            self.assertEqual(result.photo[-1].file_id,'fast-file')
        finally:
            release.set();await task


class MenuNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot=SimpleNamespace(send_message=AsyncMock(),send_photo=AsyncMock(return_value=message()),
            edit_message_media=AsyncMock(return_value=message('status-file')),
            edit_message_caption=AsyncMock(return_value=message()),edit_message_text=AsyncMock())
        self.ui=PublicUI(self.bot,None,{},None,None,None,None,None,None)

    async def test_command_sends_new_screen_and_callback_edits_same_message(self):
        await self.ui.send(100,'Menu',banner='menu')
        token=self.ui.current_message.set(message())
        try:
            await self.ui.send(100,'Changed caption',banner='menu')
            self.assertEqual(self.bot.send_photo.await_count,1)
            self.bot.edit_message_caption.assert_awaited_once()
            self.assertEqual(self.bot.edit_message_caption.await_args.kwargs['message_id'],20)
            await self.ui.send(100,'Status',banner='status')
            self.bot.edit_message_media.assert_awaited_once()
            self.assertEqual(self.bot.send_photo.await_count,1)
        finally:self.ui.current_message.reset(token)

    async def test_public_and_private_menus_share_banner_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            private=ProfileUI(self.bot,None,None,{},folder,access_policy=SimpleNamespace(actor=lambda actor:True))
            self.assertIs(private.banners,self.ui.banners)
            await private.send_main(100)
            await self.ui.send(100,'Profiles',banner='private_profiles')
            self.assertEqual(self.bot.send_photo.await_args.args[1],'menu-file')
            await private.edit_text(100,20,'Settings',None,banner='settings')
            await private.edit_text(100,20,'New settings',None,banner='settings')
            self.assertEqual(self.bot.edit_message_media.await_args.args[0].media,'status-file')

    async def test_not_modified_does_not_duplicate_or_poison_cached_banner(self):
        await self.ui.send(100,'Status',banner='status')
        # A later callback can carry a photo snapshot from an older menu screen.
        self.bot.edit_message_media.side_effect=api_error(400,'Bad Request: message is not modified')
        token=self.ui.current_message.set(message('stale-photo'))
        try:await self.ui.send(100,'Status',banner='status')
        finally:self.ui.current_message.reset(token)
        self.assertEqual(self.bot.send_photo.await_count,1)
        await self.ui.send(100,'Status',banner='status')
        self.assertEqual(self.bot.send_photo.await_args.args[1],'menu-file')

    async def test_uneditable_message_falls_back_once_and_429_does_not(self):
        await self.ui.send(100,'Menu',banner='menu')
        token=self.ui.current_message.set(message())
        try:
            self.bot.edit_message_caption.side_effect=api_error(400,'Bad Request: message to edit not found')
            await self.ui.send(100,'New',banner='menu')
            self.assertEqual(self.bot.send_photo.await_count,2)
            self.bot.edit_message_caption.side_effect=api_error(429,'Too Many Requests')
            with self.assertRaises(ApiTelegramException):await self.ui.send(100,'Again',banner='menu')
            self.assertEqual(self.bot.send_photo.await_count,2)
        finally:self.ui.current_message.reset(token)

    async def test_foreign_chat_is_never_edited(self):
        token=self.ui.current_message.set(message(actor=300))
        try:await self.ui.send(100,'Menu',banner='menu')
        finally:self.ui.current_message.reset(token)
        self.bot.edit_message_media.assert_not_awaited()
        self.bot.edit_message_caption.assert_not_awaited()
        self.bot.send_photo.assert_awaited_once()

    async def test_callback_context_is_task_local_and_reset_after_error(self):
        entered=asyncio.Event();release=asyncio.Event();seen=[]
        @self.ui.edit_current
        async def slow(call):
            entered.set();await release.wait();seen.append(self.ui.current_message.get().message_id)
        @self.ui.edit_current
        async def failing(call):
            seen.append(self.ui.current_message.get().message_id);raise RuntimeError('synthetic')
        task=asyncio.create_task(slow(SimpleNamespace(message=message(mid=20))))
        await entered.wait()
        with self.assertRaises(RuntimeError):await failing(SimpleNamespace(message=message(mid=30)))
        self.assertIsNone(self.ui.current_message.get())
        release.set();await task
        self.assertEqual(seen,[30,20])

    async def test_registered_callback_updates_the_existing_screen(self):
        h=Harness();self.addCleanup(h.close)
        handlers={}
        self.bot.answer_callback_query=AsyncMock()
        self.bot.message_handler=lambda **kwargs:lambda fn:fn
        def register_callback(**kwargs):
            def register(fn):handlers['callback']=fn;return fn
            return register
        self.bot.callback_query_handler=register_callback
        ui=PublicUI(self.bot,h.db,h.routes,{},h.product,h.inbox,lambda:SimpleNamespace(repo=h.repo),
            lambda:None,SimpleNamespace(actor=lambda actor:False))
        ui.register()
        await ui.welcome(100)
        await handlers['callback'](SimpleNamespace(id='synthetic',data='ny:notifications',
            from_user=SimpleNamespace(id=100),message=message()))
        self.bot.answer_callback_query.assert_awaited_once()
        self.bot.edit_message_media.assert_awaited_once()
        self.assertEqual(self.bot.send_photo.await_count,1)
        self.assertIsNone(ui.current_message.get())


class EditBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text(encoding='utf-8'))
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='paced_edit')
        self.budget=SimpleNamespace(take=AsyncMock(),postpone=unittest.mock.Mock())
        context={'delivery_budget':self.budget,'ApiTelegramException':ApiTelegramException}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'isolated-edit-budget','exec'),context)
        self.paced=context['paced_edit']

    async def test_correct_recipient_for_media_and_markup_signatures(self):
        method=AsyncMock()
        await self.paced(method)('caption',100,20)
        self.budget.take.assert_awaited_with(100)
        await self.paced(method,0)(300,20)
        self.budget.take.assert_awaited_with(300)
        await self.paced(method)('caption',chat_id=400,message_id=20)
        self.budget.take.assert_awaited_with(400)

    async def test_rate_limit_is_persisted_without_retrying_the_edit(self):
        method=AsyncMock(side_effect=api_error(429,'Too Many Requests'))
        with self.assertRaises(ApiTelegramException):await self.paced(method)('caption',100,20)
        self.budget.postpone.assert_called_once_with(100,60)
        method.assert_awaited_once()


class QueueIndexTests(unittest.TestCase):
    def test_indexes_are_idempotent_and_keep_recipient_fifo(self):
        h=Harness();self.addCleanup(h.close)
        ensure_outbox_indexes(h.db);ensure_outbox_indexes(h.db)
        now=time.time()
        h.db.executemany('INSERT INTO outbox(id,recipient_id,data,created_at,retry_at,failed) VALUES(?,?,?,?,?,?)',
            [(1,1,'{}',int(now),now+60,0),(2,1,'{}',int(now),0,0),
             (3,3,'{}',int(now),0,0),(4,4,'{}',int(now),0,1),(5,4,'{}',int(now),0,0)])
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text(encoding='utf-8'))
        worker=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='outbox_worker')
        sql=next(n.args[0].value for n in ast.walk(worker) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='execute' and n.args and isinstance(n.args[0],ast.Constant) and isinstance(n.args[0].value,str) and 'SELECT o.*' in n.args[0].value)
        self.assertEqual([r['id'] for r in h.db.execute(sql,(now,now))],[3,5])
        plan=' '.join(r[3] for r in h.db.execute('EXPLAIN QUERY PLAN '+sql,(now,now)))
        self.assertIn('outbox_recipient_fifo',plan)
        self.assertEqual({r[1] for r in h.db.execute('PRAGMA index_list(outbox)')},
                         {'outbox_recipient_fifo','outbox_ready','outbox_created_at'})

    def test_erase_connection_lookup_count_is_independent_of_pending_events(self):
        h=Harness();self.addCleanup(h.close)
        h.product.ensure(100,1)
        h.inbox.persist([{'update_id':i} for i in range(1,101)],lambda raw:{'connection_id':'c'})
        queries=[];h.db.set_trace_callback(queries.append)
        h.product.erase(1,h.inbox)
        h.db.set_trace_callback(None)
        lookups=[q for q in queries if 'SELECT connection_id FROM connections WHERE account_id=' in q]
        self.assertLessEqual(len(lookups),2)
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM incoming_updates').fetchone()[0],0)
