import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from business_tools.service import BusinessToolsService
from business_tools.webapp import create_app
from profile_monitor.access import MTProtoAccess
from public_pages import public_url, render_public_page
from public_ui import PublicUI
from ui_text import telegram_length
from release_support import Harness


class PublicExperienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.close)
        self.env = patch.dict(os.environ, {'WEBAPP_URL':'https://stats.example.test','PUBLIC_SITE_URL':'','BOT_USERNAME':'nyan_test_bot'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.ui = PublicUI(self.h.bot,self.h.db,self.h.routes,{},self.h.product,self.h.inbox,
                           lambda:SimpleNamespace(repo=self.h.repo),lambda:None,MTProtoAccess(self.h.routes,{100}))

    def last_page(self):
        call = self.h.bot.send_photo.await_args
        return call.kwargs['caption'], call.kwargs['reply_markup'].to_dict()['inline_keyboard']

    async def test_core_pages_have_short_captions_context_actions_and_back(self):
        for page in ('connect','status','notifications','data','help','privacy','stats','rules'):
            await self.ui.page(100,page)
            text, rows = self.last_page()
            self.assertLessEqual(telegram_length(text, 'HTML'),900,(page,telegram_length(text, 'HTML')))
            self.assertIn('<tg-emoji emoji-id="',text)
            callbacks = [b['callback_data'] for row in rows for b in row if 'callback_data' in b]
            self.assertEqual(callbacks[-1],'ny:menu')
            self.assertNotIn('can_read_messages',text)
            self.assertNotIn('AES-256',text)
            if page!='stats':
                self.assertTrue(all('web_app' not in b for row in rows for b in row))
        self.h.bot.send_message.assert_not_awaited()

    async def test_privacy_is_public_and_links_to_unauthenticated_site(self):
        await self.ui.page(999,'privacy')
        text,rows = self.last_page()
        urls=[b['url'] for row in rows for b in row if 'url' in b]
        self.assertIn('https://stats.example.test/privacy',urls)
        self.assertIn('https://stats.example.test/terms',urls)
        self.assertNotIn('ny:erase',str(rows))
        self.assertNotIn('100',text)

    async def test_section_titles_are_formatted_and_account_labels_are_escaped(self):
        self.ui.labels[100] = '<b>Untrusted account</b>'
        await self.ui.page(100,'status')
        text,_ = self.last_page()
        self.assertIn('<b>Состояние бота</b>\n',text)
        self.assertTrue(text.startswith('<tg-emoji '))
        self.assertIn('&lt;b&gt;Untrusted account&lt;/b&gt;',text)
        self.assertEqual(self.h.bot.send_photo.await_args.kwargs['parse_mode'],'HTML')

    async def test_new_buttons_set_explicit_preferences_and_old_buttons_still_work(self):
        handlers={}
        self.h.bot.message_handler=lambda **kw:lambda fn:fn
        def callback_handler(**kw):
            def register(fn):handlers['callback']=fn;return fn
            return register
        self.h.bot.callback_query_handler=callback_handler
        self.h.bot.answer_callback_query=AsyncMock()
        self.ui.register()
        def call(data,actor=100):
            return SimpleNamespace(data=data,id='synthetic',from_user=SimpleNamespace(id=actor),
                message=SimpleNamespace(chat=SimpleNamespace(type='private',id=actor)))
        await self.ui.page(100,'notifications')
        _,rows=self.last_page()
        self.assertIn('ny:set:paused:1',str(rows))
        for _ in range(2):await handlers['callback'](call('ny:set:edits:0'))
        self.assertEqual(self.h.db.execute('SELECT edits FROM notification_preferences WHERE owner_id=1').fetchone()[0],0)
        await handlers['callback'](call('ny:toggle:edits'))
        self.assertEqual(self.h.db.execute('SELECT edits FROM notification_preferences WHERE owner_id=1').fetchone()[0],1)
        await handlers['callback'](call('ny:set:own:0'))
        self.assertEqual(self.h.db.execute('SELECT only_others FROM settings WHERE recipient_id=1').fetchone()[0],1)
        self.assertTrue(self.h.product.allows(100))
        await handlers['callback'](call('ny:set:paused:1'))
        self.assertTrue(self.h.product.allows(100),'notification pause must leave collection active')
        await handlers['callback'](call('ny:set:edits:0',300))
        self.assertEqual(self.h.db.execute('SELECT edits FROM notification_preferences WHERE owner_id=1').fetchone()[0],1)

    async def test_unconfigured_site_still_explains_data_and_does_not_invent_links(self):
        with patch.dict(os.environ,{},clear=True):
            await self.ui.page(999,'privacy')
        text,rows=self.last_page()
        self.assertIn('/data',text)
        self.assertFalse(any('url' in b for row in rows for b in row))


class PublicSiteTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_privacy_terms_and_styles_are_public_but_api_is_private(self):
        h=Harness();self.addCleanup(h.close)
        service=BusinessToolsService(h.bot,h.repo,None,{})
        async with TestClient(TestServer(create_app('test-token',h.repo,service,h.routes))) as client:
            for route,title in [('/help','Помощь с Nyan'),('/privacy','Данные и приватность'),('/privacy.html','Данные и приватность'),('/terms','Условия использования')]:
                response=await client.get(route)
                self.assertEqual(response.status,200)
                self.assertIn('text/html',response.headers['Content-Type'])
                text=await response.text()
                self.assertIn(title,text)
                self.assertIn('public.css',text)
                self.assertNotIn('test-token',text)
                self.assertNotIn('telegram-web-app.js',text)
            self.assertEqual((await client.get('/public.css')).status,200)
            self.assertEqual((await client.get('/api/overview')).status,401)
            self.assertEqual((await client.get('/.env')).status,404)

    async def test_urls_configuration_and_html_escaping(self):
        with patch.dict(os.environ,{'PUBLIC_SITE_URL':'','WEBAPP_URL':'https://stats.example.test/'},clear=True):
            self.assertEqual(public_url('help#connect'),'https://stats.example.test/help#connect')
        with patch.dict(os.environ,{'PUBLIC_SITE_URL':'https://docs.example.test','WEBAPP_URL':'https://stats.example.test'},clear=True):
            self.assertEqual(public_url('privacy'),'https://docs.example.test/privacy')
        for value in ('javascript:bad','http://example.test','https://user:secret@example.test','https://[','https://example.test/private','https://example.test/?token=secret'):
            with patch.dict(os.environ,{'PUBLIC_SITE_URL':value},clear=True):
                self.assertIsNone(public_url('privacy'))
        with patch.dict(os.environ,{'SUPPORT_CONTACT':'<script>private</script>','BOT_USERNAME':'bad"onclick="x','RETENTION_DAYS':'42'},clear=True):
            page=render_public_page('privacy')
            self.assertNotIn('<script>',page)
            self.assertIn('&lt;script&gt;',page)
            self.assertIn('42 дней',page)
            self.assertNotIn('onclick=',page)
