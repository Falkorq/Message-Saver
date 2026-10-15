import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from profile_monitor.access import MTProtoAccess
from public_ui import PublicUI
from release_support import Harness


class OwnNotificationDefaultsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        h = self.h
        self.ui = PublicUI(h.bot, h.db, h.routes, {}, h.product, h.inbox,
            lambda:SimpleNamespace(repo=h.repo), lambda:None, MTProtoAccess(h.routes,{100}))

    async def own_events(self):
        h = self.h
        original = h.message()
        changed = h.message(2, 'synthetic edit', int(time.time()))
        original['business_message']['from']['id'] = 100
        changed['edited_business_message']['from']['id'] = 100
        await h.feed(original)
        await h.feed(changed)
        await h.feed(h.delete())

    async def test_missing_preference_suppresses_own_notifications_but_counts_all_events(self):
        self.assertTrue(self.h.ctx['only_others_enabled'](1))
        await self.own_events()
        h = self.h
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)
        metrics = h.repo.analytics(1,100,200)['metrics']
        self.assertEqual((metrics['o_n'],metrics['o_edits'],metrics['o_deleted']),(1,1,1))
        await self.ui.page(100,'notifications')
        rows = h.bot.send_photo.await_args.kwargs['reply_markup'].to_dict()['inline_keyboard']
        self.assertIn('Сообщать о моих сообщениях', str(rows))
        self.assertIn('ny:set:own:1', str(rows))
        self.assertNotIn('Не сообщать о моих сообщениях', str(rows))

    async def test_explicit_enable_survives_restart_and_delivers_own_events(self):
        h = self.h
        h.db.execute('INSERT INTO settings(recipient_id,only_others) VALUES(1,0)')
        h.db.commit()
        from product_state import ProductState
        ProductState(h.db,h.routes)
        self.assertFalse(h.ctx['only_others_enabled'](1))
        await self.own_events()
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],2)
        await self.ui.page(100,'notifications')
        rows = h.bot.send_photo.await_args.kwargs['reply_markup'].to_dict()['inline_keyboard']
        self.assertIn('Не сообщать о моих сообщениях', str(rows))

    async def test_previously_queued_own_event_is_discarded_by_default(self):
        h = self.h
        payload = dict(event='edit',account_id=100,old=dict(sender_id=100,text='old'),
                       new=dict(sender_id=100,text='new'))
        h.ctx['enqueue'](1,payload)
        h.db.commit()
        row = h.db.execute('SELECT * FROM outbox').fetchone()
        self.assertIsNotNone(row)
        h.ctx['send_notification'] = AsyncMock()
        await h.ctx['deliver_row'](row)
        h.ctx['send_notification'].assert_not_awaited()
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)
        self.assertEqual(h.ctx['notification_target'](dict(payload,old=None)),None)

    async def test_old_toggle_enables_own_notifications_from_new_default(self):
        handlers = {}
        h = self.h
        h.bot.message_handler = lambda **kw: lambda fn:fn
        def register(**kw):
            def save(fn):
                handlers['callback'] = fn
                return fn
            return save
        h.bot.callback_query_handler = register
        h.bot.answer_callback_query = AsyncMock()
        self.ui.register()
        call = SimpleNamespace(data='ny:toggle:own',id='synthetic',from_user=SimpleNamespace(id=100),
            message=SimpleNamespace(chat=SimpleNamespace(type='private',id=100)))
        await handlers['callback'](call)
        self.assertFalse(h.ctx['only_others_enabled'](1))
        await handlers['callback'](call)
        self.assertTrue(h.ctx['only_others_enabled'](1))

    def test_new_settings_row_uses_disabled_default(self):
        self.h.db.execute('INSERT INTO settings(recipient_id) VALUES(1)')
        self.assertTrue(self.h.ctx['only_others_enabled'](1))
