import asyncio
from collections import Counter
import json
from pathlib import Path
import tempfile
import time
import unittest
from aiohttp.test_utils import TestClient, TestServer
from business_tools.webapp import create_app
from business_tools.service import BusinessToolsService
from business_tools.web_metrics import web_chats
from release_support import Harness
from test_webapp import signed, TOKEN


class ApiReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.h=Harness(str(Path(self.temp.name)/'synthetic.sqlite3'))
        self.h.db.execute('PRAGMA journal_mode=WAL')
        self.service=BusinessToolsService(None,self.h.repo,None,{100:'Synthetic',300:'Other'})
        self.client=TestClient(TestServer(create_app(TOKEN,self.h.repo,self.service,self.h.routes,product=self.h.product)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close();self.h.close();self.temp.cleanup()

    async def get(self,path,actor=100):
        return await self.client.get(path,headers={'Authorization':'tma '+signed(actor)})

    async def test_pagination_search_summary_and_revocation_invalidate_cache(self):
        h=self.h
        h.db.executemany('INSERT INTO business_chat_stats(owner_id,account_id,chat_id,display_name,total_messages,peer_messages,last_at) VALUES(1,100,?,?,1,1,?)',
                        [(2000+i,'Chat '+str(i),int(time.time())+i) for i in range(120)])
        h.db.commit()
        first=await (await self.get('/api/overview?period=all')).json()
        self.assertEqual(len(first['chats']),50);self.assertEqual(first['summary']['messages'],120)
        second=await (await self.get('/api/overview?period=all&offset=50')).json()
        self.assertEqual(len(second['chats']),50)
        self.assertFalse({r['chat_id'] for r in first['chats']} & {r['chat_id'] for r in second['chats']})
        found=await (await self.get('/api/overview?period=all&q=Chat%20119')).json()
        self.assertEqual(len(found['chats']),1)
        h.routes.pop(100)
        self.assertEqual((await self.get('/api/overview?period=all')).status,403)

    async def test_no_n_plus_one_and_chart_bounded_at_all_time(self):
        h=self.h
        h.db.executemany('INSERT INTO business_chat_stats(owner_id,account_id,chat_id,display_name,total_messages,peer_messages) VALUES(1,100,?,?,1,1)',[(1000+i,'Synthetic') for i in range(500)])
        h.db.commit();queries=[];h.db.set_trace_callback(queries.append)
        rows=web_chats(h.repo,1,100,'all')
        h.db.set_trace_callback(None)
        self.assertEqual(len(rows),500);self.assertLessEqual(len(queries),3)
        self.assertEqual((await self.get('/api/overview?period=all&account=300')).status,400)

    async def test_account_states_and_rate_limit(self):
        state=await (await self.get('/api/overview')).json()
        self.assertEqual(state['connection_states'][0]['state'],'waiting')
        self.h.db.execute("UPDATE connections SET enabled=0 WHERE connection_id='c'");self.h.db.commit()
        state=await (await self.get('/api/overview')).json()
        self.assertEqual(state['connection_states'][0]['state'],'disconnected')
        responses=await asyncio.gather(*(self.get('/api/overview') for _ in range(41)))
        self.assertIn(429,[r.status for r in responses])
        limited=next(r for r in responses if r.status==429)
        self.assertEqual((await limited.json())['code'],'rate_limit')
        self.assertEqual((await self.get('/api/overview',300)).status,200)
