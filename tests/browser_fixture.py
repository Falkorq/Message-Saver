"""Local synthetic-only browser fixture. Never reads .env or opens the working DB."""
import argparse
import asyncio
import tempfile
from pathlib import Path
from aiohttp import web
from release_support import Harness
from business_tools.webapp import create_app
from business_tools.service import BusinessToolsService


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--combined',action='store_true');args=parser.parse_args()
    temporary=tempfile.TemporaryDirectory();h=Harness(str(Path(temporary.name)/'synthetic.sqlite3'))
    h.db.execute('PRAGMA journal_mode=WAL')
    async def seed():
        for i in range(65):
            raw=h.message(i+1,text='Синтетический пример '+str(i),mid=i+1)
            raw['business_message']['chat']['id']=1000+i
            raw['business_message']['chat']['first_name']='Синтетический чат '+str(i)
            await h.feed(raw)
    asyncio.run(seed())
    if args.combined:
        import time
        h.routes[101]=1;h.product.ensure(101,1)
        h.repo.record_message('second-account',101,1,1,dict(chat_id=1000,
            chat_name='Синтетический чат 0',sender_id=101,sent_at=int(time.time()),
            text='Второй аккаунт',type='text'))
    h.db.execute("UPDATE business_stats_progress SET value=1 WHERE name='backfill_complete'")
    for account in (500,600,700):
        h.routes[account]=account;h.product.ensure(account,account);h.repo.reset_continuity(account)
    h.db.execute("INSERT INTO connections VALUES('empty',600,600,1)")
    h.db.execute("INSERT INTO connections VALUES('off',700,700,0)");h.db.commit()
    service=BusinessToolsService(None,h.repo,None,{100:'Личный',101:'Второй',300:'Другой',500:'Новый',600:'Пустой',700:'Отключённый'})
    app=create_app('test-token',h.repo,service,h.routes,product=h.product)
    try:web.run_app(app,host='127.0.0.1',port=args.port,print=None)
    finally:h.close();temporary.cleanup()


if __name__=='__main__':main()
