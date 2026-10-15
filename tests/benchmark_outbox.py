"""Synthetic queue benchmark. Never opens the working database or configuration."""
import ast,json,sqlite3,statistics,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from reliable import ensure_outbox_indexes

root=Path(__file__).resolve().parents[1]
tree=ast.parse((root/'app.py').read_text(encoding='utf-8'))
worker=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='outbox_worker')
sql=next(n.args[0].value for n in ast.walk(worker) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='execute' and n.args and isinstance(n.args[0],ast.Constant) and isinstance(n.args[0].value,str) and 'SELECT o.*' in n.args[0].value)
db=sqlite3.connect(':memory:')
db.executescript('CREATE TABLE outbox(id INTEGER PRIMARY KEY,recipient_id INTEGER,data TEXT,created_at INTEGER,failed INTEGER,retry_at REAL);CREATE TABLE delivery_cooldowns(recipient_id INTEGER PRIMARY KEY,until_at REAL);')
now=time.time()
db.executemany('INSERT INTO outbox VALUES(?,?,?,?,?,?)',[(i+1,i%500,'{}',int(now),0,now+3600 if i<500 else 0) for i in range(20000)])
db.commit()
result={'synthetic':True,'rows':20000,'recipients':500,'scenario':'recipient heads delayed; later rows ready but must remain FIFO'}
for name in ('without_indexes','with_indexes'):
    if name=='with_indexes':ensure_outbox_indexes(db)
    samples=[]
    for _ in range(3):
        start=time.perf_counter();rows=db.execute(sql,(now,now)).fetchall();samples.append((time.perf_counter()-start)*1000)
        assert not rows
    result[name]={'median_ms':round(statistics.median(samples),2),'plan':[r[3] for r in db.execute('EXPLAIN QUERY PLAN '+sql,(now,now))]}
db.close()
Path('outputs').mkdir(exist_ok=True)
Path('outputs/outbox-performance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v['median_ms'] for k,v in result.items() if isinstance(v,dict)}))
