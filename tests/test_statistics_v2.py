import ast
import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from collections import Counter
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from business_tools.analytics import MOSCOW, histogram_summary, period_bounds
from business_tools.backup import backup_database
from business_tools.repository import BusinessToolsRepository
from business_tools.service import BusinessToolsService
from business_tools.ui import BusinessToolsUI, caption_pages


def message(stamp, sender=200, kind='text', text='привет тест', duration=None):
    return dict(chat_id=200,chat_name='Chat',chat_username='chat',sender_id=sender,
                type=kind,text=text,sent_at=stamp,metadata={'duration':duration})


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.routes={100:1,101:1,300:3}
        self.repo=BusinessToolsRepository(self.db,self.routes);self.repo.migrate()
        self.service=BusinessToolsService(None,self.repo,None,{100:'Account'})
        self.stamp=int(datetime(2026,10,7,12,tzinfo=MOSCOW).timestamp())

    def tearDown(self): self.db.close()

    def add(self, n, offset=0, sender=200, **kwargs):
        return self.repo.record_message('c',100,1,n,message(self.stamp+offset,sender,**kwargs))

    def data(self, period='all'): return self.repo.analytics(1,100,200,period,now=self.stamp+86400)

    def source(self, count=4):
        self.db.execute('CREATE TABLE connections(connection_id TEXT PRIMARY KEY,account_id INTEGER)')
        self.db.execute('CREATE TABLE messages(connection_id TEXT,chat_id INTEGER,message_id INTEGER,data TEXT,created_at INTEGER)')
        self.db.execute("INSERT INTO connections VALUES('c',100)")
        self.db.executemany('INSERT INTO messages VALUES(?,?,?,?,?)',
                            [('c',200,n,json.dumps(message(self.stamp+n)),self.stamp+n) for n in range(1,count+1)])
        self.db.commit()

    def test_periods_moscow_half_open_calendar_days(self):
        now=int(datetime(2026,10,7,0,1,tzinfo=MOSCOW).timestamp())
        start,end=period_bounds('today',now)
        self.assertEqual(datetime.fromtimestamp(start,MOSCOW).hour,0)
        self.assertEqual(end-start,86400)
        self.assertEqual(period_bounds('7',now)[0],start-6*86400)
        self.assertEqual(period_bounds('30',now)[0],start-29*86400)
        self.repo.record_message('c',100,1,1,message(start-1))
        self.repo.record_message('c',100,1,2,message(start))
        self.repo.record_message('c',100,1,3,message(end))
        self.assertEqual(self.repo.analytics(1,100,200,'today',now)['metrics']['p_n'],1)

    def test_runs_replies_and_long_pauses(self):
        self.add(1,0);self.add(2,10);self.add(3,30,100)
        self.add(4,40,100);self.add(5,60)
        self.add(6,60+14400,100);self.add(7,60+28801)
        m=self.data()['metrics']
        self.assertEqual(histogram_summary(m,'reply_o_'),{'count':2,'mean':7210,'median':7210})
        self.assertEqual(histogram_summary(m,'reply_p_')['count'],1)
        self.assertEqual(histogram_summary(m,'long_p_')['count'],1)
        self.assertEqual((m['o_starts'],m['p_starts']),(0,1))

    def test_same_side_does_not_reply_to_self(self):
        self.add(1,0,100);self.add(2,100,100);self.add(3,20000,100)
        m=self.data()['metrics']
        self.assertEqual(histogram_summary(m,'reply_o_')['count'],0)
        self.assertEqual(m['o_starts'],1)

    def test_late_history_repairs_neighbor_links(self):
        self.add(1,0);self.add(3,100,100)
        self.add(2,90)
        self.assertEqual(histogram_summary(self.data()['metrics'],'reply_o_')['mean'],10)
        self.assertFalse(self.add(2,90))
        self.assertEqual(self.data()['metrics']['p_n'],2)

    def test_edit_dedup_unique_and_total(self):
        self.add(1)
        edited=message(self.stamp,text='изменённый текст');edited['edit_at']=self.stamp+30
        for _ in range(2): self.repo.record_event(1,100,'c',200,1,'edit',edited)
        edited['text']='другой текст';edited['edit_at']+=1
        self.repo.record_event(1,100,'c',200,1,'edit',edited)
        m=self.data()['metrics']
        self.assertEqual((m['p_edited'],m['p_edits'],m['p_n']),(1,2,1))
        self.assertEqual(histogram_summary(m,'reply_p_')['count'],0)

    def test_delete_dedup_delay_and_last_survivor(self):
        self.add(1,0);self.add(2,60,100)
        for _ in range(2): self.repo.record_event(1,100,'c',200,2,'delete',detected_at=self.stamp+160)
        data=self.data()
        self.assertEqual(data['metrics']['o_deleted'],1)
        self.assertEqual(histogram_summary(data['metrics'],'delete_o_')['median'],100)
        self.assertEqual(data['last']['side'],'p')
        self.assertIn('Удалено ваших сообщений: 1',self.service.stats_text(1,100,200,'events',now=self.stamp+200))

    def test_unknown_delete_never_invents_author_or_count(self):
        self.add(1)
        self.repo.record_event(1,100,'c',200,999,'delete')
        self.repo.record_event(1,100,'c',200,999,'delete')
        data=self.data()
        self.assertEqual(data['unknown'],1)
        self.assertEqual(data['metrics']['p_deleted'],0)
        self.assertEqual(data['legacy'][0]['total_messages'],1)
        self.assertIn('контекст неизвестен',self.service.stats_text(1,100,200))

    def test_delete_before_backfill_is_attributed_only_after_original_available(self):
        self.source(1)
        self.repo.record_event(1,100,'c',200,1,'delete',detected_at=self.stamp+100)
        self.repo.backfill()
        data=self.data()
        self.assertEqual(data['metrics']['p_deleted'],1)
        self.assertEqual(data['legacy'][0]['total_messages'],1)
        self.assertIsNone(data['last'])

    def test_partial_first_message_not_initiator(self):
        self.add(1,0,100)
        self.assertEqual(self.data()['metrics']['o_starts'],0)
        self.assertIn('История частичная',self.service.stats_text(1,100,200,'info'))

    def test_duration_missing_is_not_zero(self):
        self.add(1,kind='voice');self.add(2,10,kind='voice',duration=25)
        self.add(3,20,100,kind='video_note',duration=40)
        m=self.data()['metrics']
        self.assertEqual((m['p_voice_missing'],m['p_voice_seconds'],m['o_video_note_seconds']),(1,25,40))
        self.assertIn('Неполный учёт',self.service.stats_text(1,100,200,'media',view='duration'))

    def test_word_sides_and_original_common_top(self):
        self.add(1,text='привет привет');self.add(2,1,100,text='ответ ответ')
        own=self.repo.analytics(1,100,200,side='o')['words']
        peer=self.repo.analytics(1,100,200,side='p')['words']
        self.assertEqual(own[0]['word'],'ответ');self.assertEqual(peer[0]['word'],'привет')
        self.assertEqual(sum(w['count'] for w in self.data()['words']),4)

    def test_records_activity_and_weekends(self):
        self.add(1,text='a'*20);self.add(2,86400,100,text='b'*30);self.add(3,2*86400)
        text=self.service.stats_text(1,100,200,'records',now=self.stamp+3*86400)
        self.assertIn('Последовательных дней общения: 3',text)
        self.assertIn('30 симв.',text)
        self.assertIn('Будни: 3',self.service.stats_text(1,100,200,'activity',now=self.stamp+3*86400,view='week'))

    def test_activity_zero_days_distinguished_from_unavailable_history(self):
        self.add(1)
        data=self.repo.analytics(1,100,200,'7',now=self.stamp)
        self.assertEqual(len(data['activity_days']),7)
        self.assertIsNone(data['activity_days'][0][1])
        self.db.execute("UPDATE business_stats_progress SET value=? WHERE name='coverage_start'",(self.stamp-10*86400,))
        self.repo.reset_continuity(100,self.stamp-10*86400)
        self.db.commit()
        data=self.repo.analytics(1,100,200,'7',now=self.stamp)
        self.assertEqual(data['activity_days'][0][1],0)

    def test_activity_views_do_not_mix_distributions(self):
        for n in range(1,9): self.add(n,n*86400)
        now=self.stamp+10*86400
        days=self.service.stats_text(1,100,200,'activity',now=now)
        self.assertNotIn('Будни:',days)
        self.assertNotIn('Сравнение:',days)
        self.assertNotIn('Ночь',days)
        self.assertEqual(sum('  ·  ' in line for line in days.splitlines()),5)
        week=self.service.stats_text(1,100,200,'activity',view='week',now=now)
        self.assertIn('Будни: 6 · выходные: 2',week)
        self.assertNotIn('Ночь',week)
        hours=self.service.stats_text(1,100,200,'activity',view='time',now=now)
        self.assertIn('Ночь',hours)
        self.assertNotIn('Будни:',hours)

    def test_previous_zero_no_invalid_growth(self):
        self.add(1)
        self.db.execute("UPDATE business_stats_progress SET value=? WHERE name='coverage_start'",(self.stamp-10*86400,))
        self.db.execute("INSERT INTO business_stats_progress VALUES('backfill_complete',1)")
        self.db.commit()
        text=self.service.stats_text(1,100,200,'activity','today',now=self.stamp+60,view='compare')
        self.assertIn('процент не рассчитан (база 0)',text)

    def test_comparison_rejects_restart_or_connection_gap(self):
        self.add(1)
        self.db.execute("UPDATE business_stats_progress SET value=? WHERE name='coverage_start'",(self.stamp-10*86400,))
        self.db.execute("INSERT INTO business_stats_progress VALUES('backfill_complete',1)");self.db.commit()
        self.repo.reset_continuity(100,self.stamp)
        text=self.service.stats_text(1,100,200,'activity','today',now=self.stamp+60,view='compare')
        self.assertIn('история недостаточно полная',text)
        self.assertEqual(self.data()['legacy'][0]['total_messages'],1)

    def test_retention_does_not_remove_aggregates(self):
        self.source();self.repo.backfill()
        before=self.data()
        self.db.execute('DELETE FROM messages');self.db.commit()
        self.repo.backfill();after=self.data()
        self.assertEqual(before['metrics'],after['metrics'])
        self.assertEqual(after['legacy'][0]['total_messages'],4)

    def test_backfill_restart_and_live_overlap(self):
        self.source(5)
        self.repo.backfill(batch_size=2,max_batches=1)
        self.assertEqual(self.data()['legacy'][0]['total_messages'],2)
        self.add(5,5)
        restarted=BusinessToolsRepository(self.db,self.routes);restarted.migrate()
        restarted.backfill(batch_size=2)
        self.assertEqual(self.data()['legacy'][0]['total_messages'],5)
        self.assertEqual(restarted.backfill(),0)
        self.assertEqual(self.data()['metrics']['p_n'],5)

    def test_failed_live_record_is_atomic(self):
        with patch.object(self.repo,'_daily_add',side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError): self.add(1)
        self.db.commit()
        self.assertIsNone(self.repo.stat(1,100,200))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_stats_seen').fetchone()[0],0)
        self.assertTrue(self.add(1))

    def test_restart_in_uncommitted_recovery_batch(self):
        self.source(4)
        original=self.repo._backfill_batch
        def interrupted(rows):
            original(rows[:1])
            raise RuntimeError('simulated interruption before checkpoint')
        with patch.object(self.repo,'_backfill_batch',side_effect=interrupted):
            with self.assertRaises(RuntimeError): self.repo.backfill(batch_size=2)
        self.db.rollback()  # Closing a failed process would roll back this transaction.
        restarted=BusinessToolsRepository(self.db,self.routes);restarted.migrate()
        restarted.backfill(batch_size=2)
        self.assertEqual(self.data()['legacy'][0]['total_messages'],4)
        self.assertEqual(self.data()['metrics']['p_n'],4)

    def test_original_delete_while_recovery_pending_not_lost(self):
        self.source(1)
        original=message(self.stamp+1)
        self.service.record_deletion(1,100,('c',200,1),original)
        self.db.execute('DELETE FROM messages');self.db.commit()
        self.repo.backfill()
        self.assertEqual(self.data()['metrics']['p_deleted'],1)
        self.assertEqual(self.data()['legacy'][0]['total_messages'],1)

    def test_deleted_bot_command_does_not_create_unknown_context(self):
        self.add(1)
        self.assertFalse(self.service.record_deletion(1,100,('c',200,999),self_deleted=True))
        self.assertEqual(self.data()['unknown'],0)
        self.assertIsNotNone(self.data()['last'])

    def test_owner_and_account_isolation(self):
        self.add(1)
        self.repo.record_message('b',101,1,1,message(self.stamp,101))
        self.repo.record_message('z',300,3,1,message(self.stamp,300))
        self.assertEqual(self.service.overview(1)['summary']['messages'],2)
        self.assertEqual(self.service.overview(1,101)['summary']['messages'],1)
        with self.assertRaises(LookupError): self.service.stats_text(3,100,200)
        with self.assertRaises(LookupError): self.repo.record_event(3,100,'c',200,1,'delete')
        with self.assertRaises(LookupError): self.service.overview(1,300)
        self.routes.pop(101)
        self.assertEqual(self.service.overview(1)['summary']['messages'],1)

    def test_rankings_and_mine(self):
        self.add(1,kind='voice',duration=50);self.add(2,1,100)
        self.repo.record_event(1,100,'c',200,1,'delete')
        view=self.service.overview(1,screen='rankings',ranking='seconds')
        self.assertEqual(view['rows'][0]['metrics']['p_voice_seconds'],50)
        self.assertIn('Ваша активность: 1',self.service.overview(1,screen='mine')['text'])

    def test_first_seen_not_new_contact(self):
        self.add(1)
        text=self.service.overview(1,screen='summary')['text']
        self.assertIn('Впервые замеченные',text)
        self.assertIn('старую переписку',text)

    def test_legacy_unknown_first_seen_not_reset_to_today(self):
        self.db.execute('INSERT INTO business_chat_stats(owner_id,account_id,chat_id,total_messages,first_observed_at) VALUES(1,100,200,900,0)')
        self.db.commit();self.add(1)
        self.assertEqual(self.repo.first_seen_chats(1,100,self.stamp-7*86400),0)

    def test_queries_never_read_source_messages(self):
        self.add(1)
        queries=[];self.db.set_trace_callback(queries.append)
        for section in ('overview','replies','activity','events','media','words','records'):
            self.service.stats_text(1,100,200,section)
        self.service.overview(1)
        self.assertFalse(any('FROM messages' in q for q in queries))

    def test_reassigned_account_cannot_transfer_another_owners_history(self):
        self.add(1)
        self.routes[100]=3
        with self.assertRaises(LookupError): self.repo.record_message('c',100,3,2,message(self.stamp+1))
        with self.assertRaises(LookupError): self.repo.record_event(3,100,'c',200,1,'delete')
        self.assertEqual(self.service.overview(3)['summary']['messages'],0)

    def test_unknown_timestamp_excluded_from_periods(self):
        record=message(self.stamp);record['sent_at']=None
        self.repo.record_message('c',100,1,1,record)
        data=self.data()
        self.assertEqual(data['unknown'],1)
        self.assertEqual(data['metrics']['p_n'],0)
        self.assertEqual(data['legacy'][0]['total_messages'],1)


class MigrationTests(unittest.TestCase):
    def test_real_legacy_schema_backup_and_repeated_migration(self):
        # Reproduce the preceding repository schema by running its first DDL block,
        # then fill legacy counters independently of the retained source history.
        tree=ast.parse(Path('business_tools/repository.py').read_text(encoding='utf-8'))
        migrate=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='migrate')
        ddl=next(n.value.args[0].value for n in migrate.body if isinstance(n,ast.Expr)
                 and isinstance(n.value,ast.Call) and getattr(n.value.func,'attr','')=='executescript')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'old.sqlite3'
            db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.execute('PRAGMA journal_mode=WAL')
            db.executescript(ddl)
            db.execute("INSERT INTO business_chat_stats(owner_id,account_id,chat_id,total_messages,owner_messages,peer_messages,owner_chars,peer_chars) VALUES(1,100,200,900,400,500,600,700)")
            db.execute("INSERT INTO business_stats_words VALUES(100,200,'старое',12)")
            db.execute("INSERT INTO business_stats_seen VALUES('c',200,1,1700000000)")
            db.execute('CREATE TABLE connections(connection_id TEXT,account_id INTEGER)')
            db.execute('CREATE TABLE messages(connection_id TEXT,chat_id INTEGER,message_id INTEGER,data TEXT)')
            db.execute("INSERT INTO connections VALUES('c',100)")
            db.execute('INSERT INTO messages VALUES(?,?,?,?)',('c',200,1,json.dumps(message(1700000000))))
            db.commit()
            repo=BusinessToolsRepository(db,{100:1});repo.migrate();repo.backfill()
            before=dict(repo.stat(1,100,200)[0]);repo.migrate();repo.backfill()
            self.assertEqual(before,dict(repo.stat(1,100,200)[0]))
            self.assertEqual(before['total_messages'],900)
            self.assertEqual(repo.stat(1,100,200)[2][0]['count'],12)
            backups=list((Path(directory)/'backups').glob('*.sqlite3'))
            self.assertEqual(len(backups),1)
            with closing(sqlite3.connect(backups[0])) as copy:
                self.assertEqual(copy.execute('SELECT total_messages FROM business_chat_stats').fetchone()[0],900)
                self.assertEqual(copy.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            db.close()


class FakeUIBot:
    def __init__(self): self.callbacks=[];self.edits=[];self.answers=[]
    def message_handler(self,**kwargs): return lambda fn:fn
    def callback_query_handler(self,**kwargs):
        def register(fn): self.callbacks.append((kwargs['func'],fn));return fn
        return register
    async def answer_callback_query(self,*args,**kwargs): self.answers.append((args,kwargs))


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.repo=BusinessToolsRepository(self.db,{100:1,101:1,300:3});self.repo.migrate()
        self.repo.record_message('c',100,1,1,message(int(time.time())))
        self.bot=FakeUIBot();self.service=BusinessToolsService(self.bot,self.repo,None,{})
        self.ui=BusinessToolsUI(self.bot,self.service,self.repo,self.repo.routes,'unused')
        async def edit(*args): self.bot.edits.append(args)
        self.ui.edit=edit;self.ui.register()

    async def asyncTearDown(self): self.db.close()

    async def click(self,data,actor=100):
        call=SimpleNamespace(data=data,id='cb',from_user=SimpleNamespace(id=actor),
                             message=SimpleNamespace(chat=SimpleNamespace(id=actor),message_id=10))
        for match,handler in self.bot.callbacks:
            if match(call): await handler(call);return
        self.fail('No callback '+data)

    async def test_navigation_preserves_filters_page_and_restart(self):
        await self.click('bt:filter:account:100');await self.click('bt:filter:period:7')
        await self.click('bt:detail:open:100,200');await self.click('bt:detail:tab:words')
        await self.click('bt:detail:side:o')
        restarted=BusinessToolsUI(self.bot,self.service,self.repo,self.repo.routes,'unused')
        state=restarted.state(100,10)
        self.assertEqual((state['account'],state['period'],state['side']),(100,'7','o'))
        await self.click('bt:screen:back:0')
        state=self.ui.state(100,10)
        self.assertEqual((state['account'],state['period'],state['chat']),(100,'7',None))
        self.assertGreater(len(self.bot.edits),4)

    async def test_forged_account_callbacks_denied(self):
        await self.click('bt:detail:open:300,200')
        self.assertFalse(self.bot.edits)
        self.assertTrue(self.bot.answers[-1][1]['show_alert'])
        await self.click('bt:filter:account:300')
        self.assertFalse(self.bot.edits)

    async def test_old_callbacks_and_64_byte_limit(self):
        await self.click('bt:stats:recent:0');await self.click('bt:stat:100:200:0:recent:overview')
        for _,_,_,markup in self.bot.edits:
            for row in markup.to_dict()['inline_keyboard']:
                for item in row:
                    self.assertLessEqual(len(item.get('callback_data','').encode()),64)

    async def test_caption_pagination_no_new_messages(self):
        state=self.ui.state(100,10)
        text='\n'.join('Строка '+str(n)+' '+('текст '*15) for n in range(25))
        await self.ui.edit_stats(100,10,text,[],state)
        self.assertEqual(''.join(caption_pages(text)).replace('\n',''),text.replace('\n',''))
        for page in caption_pages(text): self.assertLessEqual(len(page),850)

    async def test_back_restores_list_page(self):
        for n in range(1,14):
            record=message(int(time.time()));record['chat_id']=200+n
            self.repo.record_message('c',100,1,n+1,record)
        await self.click('bt:screen:page:1')
        await self.click('bt:detail:open:100,201')
        await self.click('bt:screen:back:0')
        self.assertEqual(self.ui.state(100,10)['page'],1)

    async def test_compact_pickers_return_to_same_card(self):
        await self.click('bt:detail:open:100,200')
        await self.click('bt:detail:menu:0')
        self.assertIn('Об учёте',str(self.bot.edits[-1][3].to_dict()))
        await self.click('bt:detail:tab:info')
        self.assertIn('История частичная',self.bot.edits[-1][2])
        await self.click('bt:screen:period:0')
        await self.click('bt:filter:period:7')
        state=self.ui.state(100,10)
        self.assertEqual((state['chat'],state['section'],state['period']),(200,'info','7'))
        await self.click('bt:detail:tab:overview')
        text=self.bot.edits[-1][2]
        self.assertEqual(text.count('<tg-emoji'),1)
        self.assertNotIn('Длительность без metadata',text)
        self.assertEqual(len(self.bot.edits[-1][3].to_dict()['inline_keyboard']),3)

    async def test_subview_and_page_survive_navigation(self):
        for n in range(1,13):
            self.repo.record_message('c',100,1,n+1,message(int(time.time())-n*86400))
        await self.click('bt:detail:open:100,200')
        await self.click('bt:detail:tab:activity')
        await self.click('bt:detail:page:1')
        self.assertNotIn('Будни:',self.bot.edits[-1][2])
        await self.click('bt:detail:view:week')
        self.assertIn('Будни:',self.bot.edits[-1][2])
        await self.click('bt:detail:tab:words')
        await self.click('bt:detail:tab:activity')
        self.assertEqual(self.ui.state(100,10)['views']['activity'],'week')
        await self.click('bt:detail:view:days')
        self.assertEqual(self.ui.state(100,10)['detail_page'],0)
        await self.click('bt:detail:tab:info')
        await self.click('bt:detail:page:2')
        self.assertIn('Подробная история:',self.bot.edits[-1][2])


class HandlerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_count_before_notification_filters_and_self_delete_suppression(self):
        from release_support import Harness
        h=Harness()
        try:
            h.db.execute('INSERT INTO settings VALUES(1,1)');h.db.commit()
            original=h.message();original['business_message']['from']['id']=100
            changed=h.message(2,'правка',int(time.time()));changed['edited_business_message']['from']['id']=100
            await h.feed(original);await h.feed(changed)
            h.repo.mark_self_deleted('c',200,1)
            await h.feed(h.delete())
            data=h.repo.analytics(1,100,200)
            self.assertEqual((data['metrics']['o_edits'],data['metrics']['o_deleted']),(1,1))
            self.assertEqual(h.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)
        finally:h.close()



if __name__=='__main__': unittest.main()
