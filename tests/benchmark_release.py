import sys
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import asyncio,json,tempfile,time
from pathlib import Path
from datetime import datetime,timedelta
from aiohttp.test_utils import TestClient,TestServer
from release_support import Harness
from test_webapp import TOKEN,signed
from business_tools.webapp import create_app
from business_tools.service import BusinessToolsService
from business_tools.web_metrics import web_chats,enrich_overview
async def main():
 with tempfile.TemporaryDirectory() as folder:
  h=Harness(str(Path(folder)/'synthetic.sqlite3'));h.db.execute('PRAGMA journal_mode=WAL');now=int(time.time())
  h.db.executemany('INSERT INTO business_chat_stats(owner_id,account_id,chat_id,display_name,total_messages,peer_messages,first_at,last_at) VALUES(1,100,?,?,366,366,?,?)',[(1000+i,'Synthetic '+str(i),now-365*86400,now) for i in range(500)])
  h.db.executemany('INSERT INTO business_stats_chat_metrics VALUES(100,?,?)',[(1000+i,json.dumps({'p_n':366,'p_chars':3660})) for i in range(500)])
  today=datetime.fromtimestamp(now).date()
  h.db.executemany('INSERT INTO business_stats_daily VALUES(100,?,?,?)',((1000+i,(today-timedelta(days=d)).isoformat(),'{"p_n":1,"p_chars":10}') for i in range(500) for d in range(366)))
  h.db.commit();queries=[];h.db.set_trace_callback(queries.append)
  rows=web_chats(h.repo,1,100,'all',now);enrich_overview(h.repo,1,100,'all',rows,now);h.db.set_trace_callback(None)
  query_count=len(queries);gaps=[];delivery=[];ingress=[];running=True
  async def ticker():
   previous=time.perf_counter()
   while running:
    await asyncio.sleep(.005);current=time.perf_counter();gaps.append(max(0,current-previous-.005));previous=current
  async def send(recipient,*args,**kwargs):delivery.append(time.perf_counter())
  h.ctx['send_notification']=send
  client=TestClient(TestServer(create_app(TOKEN,h.repo,BusinessToolsService(None,h.repo,None,{}),h.routes,product=h.product)))
  await client.start_server();tick=asyncio.create_task(ticker());worker=asyncio.create_task(h.ctx['outbox_worker']())
  async def request(actor,period):
   start=time.perf_counter();response=await client.get('/api/overview?period='+period,headers={'Authorization':'tma '+signed(actor)});data=await response.json()
   return dict(status=response.status,seconds=round(time.perf_counter()-start,4),chats=len(data.get('chats',[])))
  start=time.perf_counter()
  requests=[asyncio.create_task(request(actor,period)) for actor,period in [(100,'all'),(100,'30'),(300,'all'),(300,'30')]]
  for i in range(5):
   await asyncio.sleep(.02);event_start=time.perf_counter();await h.feed(h.message(10000+i,mid=10000+i));ingress.append(time.perf_counter()-event_start)
   h.ctx['enqueue'](3,dict(event='text_notification',source='business_tools',owner_id=3,text='synthetic heartbeat'));h.db.commit()
  responses=await asyncio.gather(*requests);await asyncio.sleep(.2)
  worker.cancel();running=False;await asyncio.gather(worker,tick,return_exceptions=True)
  result={'fixture':{'chats':500,'daily_rows':183000},'overview_sql_commands':query_count,'requests':responses,'max_loop_delay_ms':round(max(gaps)*1000,2),'max_ingress_processing_ms':round(max(ingress)*1000,2),'delivered_during_requests':len(delivery),'rows_returned_limit':50}
  await client.close();h.close()
  Path('outputs/performance.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))
asyncio.run(main())
