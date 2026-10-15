import json
import time
import unittest
import asyncio
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient,TestServer
from business_tools.service import BusinessToolsService
from business_tools.webapp import create_app
from release_support import Harness
from test_webapp import TOKEN,signed


class CombinedChatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h=Harness()
        self.h.routes[101]=1
        self.h.product.ensure(101,1)
        self.h.db.execute("INSERT INTO connections VALUES('second',101,1,1)")
        self.h.db.commit()
        self.client=TestClient(TestServer(create_app(TOKEN,self.h.repo,
            BusinessToolsService(self.h.bot,self.h.repo,None,{100:'Личный',101:'Второй'}),
            self.h.routes,product=self.h.product)))
        await self.client.start_server()
        self.start=int(time.time())-1000

    async def asyncTearDown(self):
        await self.client.close();self.h.close()

    def record(self,account,mid,stamp,sender,chat=200,text='общий текст',kind='text',duration=None):
        record=dict(chat_id=chat,chat_name='Один собеседник',sender_id=sender,sent_at=stamp,
                    text=text,type=kind,metadata={'duration':duration} if duration is not None else {})
        self.h.repo.record_message('stream-'+str(account),account,self.h.routes[account],mid,record)

    def seed(self):
        for mid,(delta,sender) in enumerate(((0,100),(10,200),(50,100)),1):
            self.record(100,mid,self.start+delta,sender)
        for mid,(delta,sender) in enumerate(((5,101),(105,200),(110,200),(305,101)),1):
            self.record(101,mid,self.start+delta,sender,kind='voice',duration=mid)
        self.record(300,1,self.start+400,200,text='чужое секретное слово')

    async def get(self,path,actor=100):
        return await self.client.get(path,headers={'Authorization':'tma '+signed(actor)})

    async def data(self,path,actor=100):
        response=await self.get(path,actor)
        self.assertEqual(response.status,200,await response.text())
        return await response.json()

    async def test_one_contact_in_overview_rankings_search_and_legacy_pins(self):
        self.seed()
        combined=await self.data('/api/overview?period=all')
        self.assertEqual(combined['summary'],dict(messages=7,own=4,peer=3,chats=1))
        self.assertEqual(len(combined['chats']),1)
        row=combined['chats'][0]
        self.assertEqual((row['account_id'],row['account_ids'],row['total_messages']),(0,[100,101],7))
        self.assertIn(row['avatar_account_id'],[100,101])
        self.assertEqual(len(combined['top_chats']),1)
        for pins in ('100:200','101:200','0:200'):
            result=await self.data('/api/overview?period=all&favorites=1&pins='+pins)
            self.assertEqual(len(result['chats']),1)
        result=await self.data('/api/overview?period=all&account=100&favorites=1&pins=0:200')
        self.assertEqual(result['chats'][0]['total_messages'],3)
        self.record(100,4,self.start+500,201,chat=201)
        result=await self.data('/api/overview?period=all')
        self.assertEqual(result['summary']['chats'],2,'Names must not merge different Telegram IDs')

    async def test_detail_sums_all_metrics_but_never_invents_cross_account_replies(self):
        self.seed()
        data=await self.data('/api/chats/200?period=all&account=all')
        self.assertEqual((data['own'],data['peer'],data['active_days']),(4,3,1))
        self.assertEqual(data['account_ids'],[100,101])
        self.assertEqual(sum(data['hours']),7)
        self.assertEqual(sum(day['own'] or 0 for day in data['series']),4)
        self.assertEqual(sum(data['weekdays']),7)
        self.assertEqual(data['durations']['o']['voice']['seconds'],5)
        self.assertEqual(data['durations']['p']['voice']['seconds'],5)
        self.assertEqual(data['media'][0]['total'],4)
        reply=data['replies']['p']['normal']
        self.assertEqual((reply['count'],reply['mean'],reply['median']),(2,55,55))
        self.assertEqual(data['records']['day_messages'],7)
        self.assertNotIn('секретное',json.dumps(data,ensure_ascii=False))
        single=await self.data('/api/chats/200?period=all&account=100')
        self.assertEqual((single['own'],single['peer']),(2,1))
        other=await self.data('/api/chats/200?period=all&account=all',300)
        self.assertEqual(other['own']+other['peer'],1)
        self.assertEqual((await self.get('/api/chats/200?account=300')).status,400)

    async def test_words_group_before_pagination_and_respect_author_filter(self):
        self.seed()
        self.h.db.execute('DELETE FROM business_stats_words')
        for account in (100,101):
            self.h.db.executemany('INSERT INTO business_stats_words VALUES(?,?,?,?)',
                [(account,200,f'word{account}-{i:03}',3) for i in range(60)]+[(account,200,'shared',2)])
        self.h.db.commit()
        first=await self.data('/api/chats/200?account=all&period=all&section=words')
        second=await self.data('/api/chats/200?account=all&period=all&section=words&page=1')
        self.assertEqual(first['words'][0],dict(word='shared',count=4))
        self.assertEqual(first['word_total'],121)
        self.assertEqual(len(first['words']),50)
        self.assertFalse({w['word'] for w in first['words']} & {w['word'] for w in second['words']})
        own=await self.data('/api/chats/200?account=all&period=all&section=words&side=o')
        self.assertEqual(next(w['count'] for w in own['words'] if w['word']=='общий'),4)

    async def test_mixed_legacy_period_totals_match_overview_and_account_filters(self):
        self.seed()
        self.record(101,5,self.start-60*86400,200)
        self.h.db.execute('UPDATE business_chat_stats SET total_messages=total_messages+50,owner_messages=owner_messages+50,owner_chars=owner_chars+500 WHERE account_id=100')
        self.h.db.commit()
        detail=await self.data('/api/chats/200?account=all&period=7')
        overview=await self.data('/api/overview?period=7')
        self.assertEqual(detail['own']+detail['peer'],57)
        self.assertEqual(overview['chats'][0]['total_messages'],57)
        self.assertEqual(detail['totals']['basis'],'mixed')
        self.assertFalse(detail['coverage']['partial_period'])
        self.assertEqual(detail['coverage']['undetailed'],50)

    async def test_contact_stays_combined_when_only_one_account_is_active_in_period(self):
        self.record(100,1,self.start,100)
        self.record(101,1,self.start-60*86400,200)
        overview=await self.data('/api/overview?period=7')
        row=overview['chats'][0]
        self.assertEqual((row['account_id'],row['account_ids'],row['total_messages']),(0,[100,101],1))
        detail=await self.data('/api/chats/200?period=all&account=all')
        self.assertEqual(detail['own']+detail['peer'],2)
        self.assertEqual((await self.data('/api/overview?period=7&account=101'))['chats'],[])

    async def test_current_account_scope_is_used_after_revocation_and_erasure(self):
        self.seed()
        await self.data('/api/chats/200?account=all&period=all')
        self.h.routes.pop(101)
        data=await self.data('/api/chats/200?account=all&period=all')
        self.assertEqual(data['account_ids'],[100])
        self.assertEqual(data['own']+data['peer'],3)
        self.h.product.erase(1,self.h.inbox)
        self.assertEqual((await self.get('/api/chats/200?account=all&period=all')).status,404)
        self.assertEqual((await self.data('/api/chats/200?account=all&period=all',300))['peer'],1)

    async def test_account_revoked_during_worker_read_is_not_returned(self):
        from business_tools.webapp import web_chats
        with tempfile.TemporaryDirectory() as folder:
            h=Harness(str(Path(folder)/'synthetic.sqlite3'))
            h.routes[101]=1
            h.db.execute('PRAGMA journal_mode=WAL')
            entered,release=threading.Event(),threading.Event()
            def blocked(*args,**kwargs):
                entered.set()
                if not release.wait(3):raise RuntimeError('worker wait timed out')
                return web_chats(*args,**kwargs)
            client=TestClient(TestServer(create_app(TOKEN,h.repo,
                BusinessToolsService(h.bot,h.repo,None,{}),h.routes,product=h.product)))
            await client.start_server()
            try:
                with patch('business_tools.webapp.web_chats',side_effect=blocked):
                    pending=asyncio.create_task(client.get('/api/overview?period=all',headers={'Authorization':'tma '+signed(100)}))
                    self.assertTrue(await asyncio.to_thread(entered.wait,2))
                    h.routes.pop(101);release.set()
                    response=await pending
                    self.assertEqual(response.status,409)
                    self.assertNotIn('chats',await response.json())
            finally:
                release.set();await client.close();h.close()
