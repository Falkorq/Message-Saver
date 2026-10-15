import hashlib
import hmac
import json
import sqlite3
import time
import unittest
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from business_tools.repository import BusinessToolsRepository
from business_tools.service import BusinessToolsService
from business_tools.webapp import create_app, validate_init_data, validate_url, server_ssl_context

TOKEN = 'test-token'


def signed(actor=100, stamp=None):
    fields = {'auth_date': str(int(time.time()) if stamp is None else stamp),
              'user': json.dumps({'id': actor}), 'query_id': 'test'}
    secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, '\n'.join(f'{k}={v}' for k, v in sorted(fields.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class AuthTests(unittest.TestCase):
    def test_tls_config_fails_closed(self):
        self.assertIsNone(server_ssl_context())
        for cert, key in (('ssl/origin.pem', None), (None, 'ssl/origin.key')):
            with self.assertRaises(ValueError): server_ssl_context(cert, key)
        with self.assertRaises(OSError):
            server_ssl_context('missing-certificate.pem', 'missing-key.key')

    def test_signed_user_and_rejections(self):
        self.assertEqual(validate_init_data(signed(stamp=1000), TOKEN, now=1001), 100)
        for raw in (signed(stamp=1000).replace('auth_date=1000', 'auth_date=1001'),
                    signed(stamp=1000) + '&auth_date=1000', '', signed(actor='100', stamp=1000)):
            with self.assertRaises(web.HTTPUnauthorized):
                validate_init_data(raw, TOKEN, now=1001)
        with self.assertRaises(web.HTTPUnauthorized):
            validate_init_data(signed(stamp=1000), TOKEN, now=100000)
        with self.assertRaises(web.HTTPUnauthorized):
            validate_init_data(signed(stamp=1000), TOKEN, now=900)

    def test_url(self):
        self.assertEqual(validate_url('https://stats.example.com'), 'https://stats.example.com')
        for value in ('http://stats.example.com', 'https://', 'https://user@stats.example.com', 'https://stats.example.com/#bad'):
            with self.assertRaises(ValueError): validate_url(value)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(':memory:'); self.db.row_factory = sqlite3.Row
        self.routes = {100: 1, 300: 3}
        self.repo = BusinessToolsRepository(self.db, self.routes); self.repo.migrate()
        for account, owner, chat in ((100, 1, 200), (300, 3, 400)):
            self.repo.record_message(str(account), account, owner, 1,
                dict(chat_id=chat, chat_name='<script>private</script>', chat_username='',
                     sender_id=chat, type='text', text='secret message body', sent_at=int(time.time()), metadata={}))
        service = BusinessToolsService(None, self.repo, None, {100:'Home',300:'Other'})
        self.client = TestClient(TestServer(create_app(TOKEN, self.repo, service, self.routes)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close(); self.db.close()

    async def get(self, path, actor=100):
        return await self.client.get(path, headers={'Authorization': 'tma ' + signed(actor)})

    async def test_requires_auth_and_current_access(self):
        self.assertEqual((await self.client.get('/api/overview')).status, 401)
        self.assertEqual((await self.get('/api/overview', 999)).status, 403)
        del self.routes[100]
        self.assertEqual((await self.get('/api/overview')).status, 403)

    async def test_scoped_totals_and_cross_owner_denied(self):
        response = await self.get('/api/overview?period=all')
        data = await response.json()
        self.assertEqual(data['summary']['messages'], 1)
        self.assertEqual([c['chat_id'] for c in data['chats']], [200])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual((await self.get('/api/overview?account=300')).status, 400)
        self.assertEqual((await self.get('/api/chats/400?account=300')).status, 400)
        self.assertEqual((await self.get('/api/chats/400?account=100')).status, 404)

    async def test_sections_no_message_body_and_bad_filters(self):
        for section in ('overview','replies','activity','events','media','words','records','info'):
            response = await self.get(f'/api/chats/200?account=100&period=all&section={section}')
            self.assertEqual(response.status, 200)
            data = await response.json()
            self.assertEqual(data['peer'], 1)
            self.assertNotIn('secret message body', json.dumps(data))
        for path in ('/api/overview?period=bad', '/api/chats/200?account=100&section=bad',
                     '/api/chats/200?account=100&side=bad', '/api/chats/200?account=100&page=-1'):
            self.assertEqual((await self.get(path)).status, 400)

    async def test_static_allowlist(self):
        self.assertEqual((await self.client.get('/')).status, 200)
        self.assertEqual((await self.client.get('/.env')).status, 404)

    async def test_native_dashboard_scoped_series_and_totals(self):
        response = await self.get('/api/overview?period=30')
        data = await response.json()
        self.assertEqual(len(data['days']), 30)
        self.assertEqual(sum(d['peer'] or 0 for d in data['days']), 1)
        self.assertEqual(sum(d['own'] or 0 for d in data['days']), 0)
        self.assertIsNone(data['days'][0]['peer'])
        self.assertEqual(sum(data['hours']), 1)
        detail = await (await self.get('/api/chats/200?account=100&period=all')).json()
        self.assertEqual(detail['active_days'], 1)
        self.assertEqual(detail['records']['day_messages'], 1)
        self.assertEqual(detail['records']['streak'], 1)
        self.assertEqual(detail['coverage']['detailed'], 1)
        self.assertEqual(detail['lengths']['p']['max'], len('secret message body'))

    async def test_web_words_use_fifty_and_preserve_bot_page_size(self):
        words = ['термин'+chr(97+i//26)+chr(97+i%26) for i in range(65)]
        self.repo.record_message('100',100,1,2,dict(chat_id=200,chat_name='Chat',chat_username='',
                                 sender_id=200,type='text',text=' '.join(words),sent_at=int(time.time()),metadata={}))
        first = await (await self.get('/api/chats/200?account=100&period=all&section=words')).json()
        second = await (await self.get('/api/chats/200?account=100&period=all&section=words&page=1')).json()
        self.assertEqual(len(first['words']), 50)
        self.assertEqual(len(first['words'])+len(second['words']), first['word_total'])
        self.assertEqual(len({w['word'] for w in first['words']+second['words']}), first['word_total'])
        self.assertEqual(len(self.repo.analytics(1,100,200)['words']), 5)
        own = await (await self.get('/api/chats/200?account=100&period=all&side=o')).json()
        self.assertEqual(own['words'], [])

    async def test_native_reply_histograms_and_media_metadata(self):
        stamp = int(time.time())
        for n, offset, sender, kind in ((2,-120,200,'voice'),(3,-60,100,'text')):
            self.repo.record_message('100',100,1,n,dict(chat_id=200,chat_name='Chat',chat_username='',
                                    sender_id=sender,type=kind,text='',sent_at=stamp+offset,metadata={'duration':42}))
        detail = await (await self.get('/api/chats/200?account=100&period=all')).json()
        self.assertEqual(detail['replies']['o']['normal']['median'], 60)
        self.assertEqual(detail['durations']['p']['voice']['seconds'], 42)
        voice = next(m for m in detail['media'] if m['kind']=='voice')
        self.assertEqual((voice['own'],voice['peer'],voice['total']),(0,1,1))

    async def test_thirty_days_matches_all_for_retained_seventeen_day_chat(self):
        stamp = int(time.time())
        self.repo._daily_add(100,200,time.strftime('%Y-%m-%d',time.gmtime(stamp+3*3600)),{'o_n':354,'p_n':2629})
        self.db.execute('UPDATE business_chat_stats SET total_messages=5433,owner_messages=2797,'
                        'peer_messages=2636,first_at=?,last_at=? WHERE account_id=100 AND chat_id=200',
                        (stamp-17*86400,stamp))
        self.db.commit()
        self.db.execute("INSERT INTO business_stats_words VALUES(100,200,'староеслово',123)")
        self.db.commit()
        for period in ('30','all'):
            detail = await (await self.get(f'/api/chats/200?account=100&period={period}')).json()
            self.assertEqual((detail['own'],detail['peer']),(2797,2636))
            self.assertEqual(detail['coverage']['detailed'],2984)
            self.assertEqual(detail['coverage']['undetailed'],2449)
            self.assertEqual(detail['coverage']['basis'],'retained')
            self.assertIn({'word':'староеслово','count':123},detail['words'])
            overview = await (await self.get(f'/api/overview?period={period}')).json()
            self.assertEqual(overview['summary']['messages'],5433)
        narrow = await (await self.get('/api/chats/200?account=100&period=7')).json()
        self.assertEqual((narrow['own'],narrow['peer']),(354,2630))
        self.assertTrue(narrow['coverage']['partial_period'])
        self.assertNotIn('староеслово',[w['word'] for w in narrow['words']])

    async def test_unknown_legacy_time_range_never_allocated_to_period(self):
        self.db.execute('UPDATE business_chat_stats SET total_messages=11,peer_messages=11,first_at=NULL '
                        'WHERE account_id=100 AND chat_id=200')
        self.db.commit()
        detail = await (await self.get('/api/chats/200?account=100&period=30')).json()
        self.assertEqual(detail['peer'],1)
        self.assertTrue(detail['coverage']['partial_period'])
        self.assertEqual(detail['coverage']['undetailed'],10)


if __name__ == '__main__': unittest.main()
