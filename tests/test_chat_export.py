import asyncio
import io
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from business_tools.chat_export import ExportError, inspect_export, records, rich_text
from business_tools.export_import import EncryptedUpload, import_batch
from business_tools.service import BusinessToolsService
from business_tools.webapp import create_app
from release_support import Harness
from test_webapp import signed, TOKEN


def export(count=3, **changes):
    stamp = int(time.time())-1000
    messages = [dict(id=n+1,type='message',date_unixtime=str(stamp+n),
        from_id='user100' if n%2==0 else 'user200',text='synthetic original sentence') for n in range(count)]
    value = dict(name='Synthetic chat',type='personal_chat',id=200,messages=messages)
    value.update(changes)
    return value


def source(value):
    return io.BytesIO(json.dumps(value,ensure_ascii=False).encode('utf-8'))


class ExportReaderTests(unittest.TestCase):
    def test_streams_utf8_bom_entities_captions_service_rich_and_duration(self):
        data = export()
        data['messages'][0]['text'] = ['Привет ',dict(type='bold',text='мир 🔔'),dict(type='text_link',text='ссылка',href='https://invalid.test')]
        data['messages'][0]['edited_unixtime'] = str(int(time.time()))
        data['messages'][1].update(text='caption',media_type='voice_message',duration_seconds=13.5,file='../../private')
        data['messages'][2].pop('text')
        data['messages'][2]['rich_message'] = dict(blocks=[dict(type='paragraph',text=dict(type='plain',text='rich text'))])
        data['messages'].append(dict(id=4,type='service',action='phone_call'))
        raw = b'\xef\xbb\xbf'+source(data).read()
        class Tiny(io.BytesIO):
            def read(self,size=-1):
                return super().read(min(size,7))
        preview = inspect_export(Tiny(raw),100)
        self.assertEqual((preview['messages'],preview['service'],preview['own'],preview['peer']),(3,1,2,1))
        normalized = list(records(Tiny(raw),preview))
        self.assertEqual(normalized[0][1]['text'],'Привет мир 🔔ссылка')
        self.assertEqual(normalized[1][1]['metadata'],{'duration':13})
        self.assertEqual(normalized[1][1]['type'],'voice')
        self.assertEqual(normalized[2][1]['text'],'rich text')
        self.assertNotIn('file',normalized[1][1])
        self.assertEqual(preview['marked_edited'],1)

    def test_rejects_invalid_root_account_dates_duplicate_fields_and_trailing_json(self):
        for data in (export(type='private_group'),export(messages=[]),export(id=-1)):
            with self.assertRaises(ExportError):inspect_export(source(data),100)
        with self.assertRaises(ExportError):inspect_export(source(export()),999)
        for raw in (b'{"id":1,"id":2,"messages":[]}',b'{"messages":[],}',b'{"messages":[{},]}',
                    b'{"messages":[]} extra',b'{"messages":NaN}',b'[]',b'\xff'):
            with self.subTest(raw=raw),self.assertRaises(ExportError):inspect_export(io.BytesIO(raw),100)
        data = export();data['messages'][0]['date_unixtime']='bad'
        with self.assertRaises(ExportError):inspect_export(source(data),100)

    def test_record_and_total_size_limits(self):
        with patch('business_tools.chat_export.MAX_RECORD',20):
            with self.assertRaises(ExportError):inspect_export(source(export()),100)
        with patch('business_tools.chat_export.MAX_BYTES',20):
            with self.assertRaises(ExportError):inspect_export(source(export()),100)

    def test_rich_table_and_list_text_is_not_counted_twice(self):
        value = dict(blocks=[dict(items=[dict(content='metadata',text={'text':'one'})]),
            dict(rows=[dict(cells=[dict(text={'text':['two ',{'text':'three'}]})])])])
        self.assertEqual(rich_text(value),'one\ntwo three')

    def test_upload_is_encrypted_and_can_be_read_twice(self):
        upload = EncryptedUpload()
        self.addCleanup(upload.close)
        upload.write(b'private plaintext');upload.write(' 🔔'.encode())
        upload.file.seek(0)
        self.assertNotIn(b'private plaintext',upload.file.read())
        for _ in range(2):
            reader = upload.reader()
            self.assertEqual(reader.read(3)+reader.read(100),b'private plaintext'+ ' 🔔'.encode())
            self.assertEqual(reader.read(10),b'')

    def test_multi_chunk_encrypted_upload_preserves_utf8_and_statistics_input(self):
        data=export(800)
        for message in data['messages']:
            message['text']='Текст 🔔 '*40
        raw=source(data).read()
        self.assertGreater(len(raw),262144)
        upload=EncryptedUpload();self.addCleanup(upload.close)
        for offset in range(0,len(raw),262144):
            upload.write(raw[offset:offset+262144])
        preview=inspect_export(upload.reader(),100)
        self.assertEqual(preview['messages'],800)
        normalized=list(records(upload.reader(),preview))
        self.assertEqual(len(normalized),800)
        self.assertTrue(all(row['text']==data['messages'][0]['text'] for mid,row in normalized))


class ImportStatisticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.h = Harness();self.addCleanup(self.h.close)

    def batch(self,data):
        preview = inspect_export(source(data),100)
        return list(records(source(data),preview))

    async def test_repeated_and_live_overlap_preserve_counts_and_never_store_bodies(self):
        h = self.h
        data = export()
        original = h.message(text='already received');await h.feed(original)
        data['messages'][0]['from_id']='user200'
        batch = self.batch(data)
        self.assertEqual(import_batch(h.repo,1,100,batch),(2,1))
        self.assertEqual(import_batch(h.repo,1,100,batch),(0,3))
        self.assertEqual(h.repo.stat(1,100,200)[0]['total_messages'],3)
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],1)
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)
        self.assertNotIn('synthetic original sentence','\n'.join(h.db.iterdump()))

    async def test_live_message_edit_delete_after_import_reuse_statistics_identity(self):
        h = self.h
        data = export(2);data['messages'][0]['from_id']='user200'
        # Keep original source fingerprints identical to a later live update.
        live = h.message(text=data['messages'][0]['text'])
        data['messages'][0]['date_unixtime']=str(live['business_message']['date'])
        data['messages'][1]['from_id']='user100'
        self.assertEqual(import_batch(h.repo,1,100,self.batch(data)),(2,0))
        await h.feed(live)
        await h.feed(h.message(2,'changed',int(time.time())))
        await h.feed(h.delete())
        self.assertEqual(h.repo.stat(1,100,200)[0]['total_messages'],2)
        metrics = h.repo.analytics(1,100,200)['metrics']
        self.assertEqual((metrics['p_edits'],metrics['p_deleted']),(1,1))
        h.product.erase(1,h.inbox)
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM business_stats_events').fetchone()[0],0)

    def test_failed_batch_rolls_back_and_erase_removes_imports(self):
        h = self.h;batch=self.batch(export())
        invalid = [(batch[1][0],dict(batch[1][1],chat_id=201))]
        h.db.execute('INSERT INTO business_chat_stats(owner_id,account_id,chat_id) VALUES(99,100,201)');h.db.commit()
        with self.assertRaises(LookupError):import_batch(h.repo,1,100,batch[:1]+invalid)
        self.assertEqual(h.db.execute('SELECT COUNT(*) FROM business_stats_imported').fetchone()[0],0)
        h.db.execute('DELETE FROM business_chat_stats WHERE chat_id=201');h.db.commit()
        import_batch(h.repo,1,100,batch)
        h.product.erase(1,h.inbox)
        for table in ('business_stats_imported','business_stats_seen','business_stats_daily','business_chat_stats'):
            self.assertEqual(h.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0,table)

    def test_batched_import_matches_live_metrics_even_in_reverse_order(self):
        live=Harness();self.addCleanup(live.close)
        data=export(80)
        start=int(time.time())-20*86400
        for i,message in enumerate(data['messages']):
            message.update(date_unixtime=str(start+i*9000+(i//5)*20000),text='Synthetic word '+str(i%4))
            if i%3==0:message.update(media_type='voice_message',duration_seconds=i)
        batch=self.batch(data)
        for mid,record in batch:
            live.repo.record_message('c',100,1,mid,record)
        for offset in range(0,len(batch),20):
            import_batch(self.h.repo,1,100,list(reversed(batch))[offset:offset+20])
        for table in ('business_stats_daily','business_stats_chat_metrics'):
            def values(h):
                return sorted(json.dumps(json.loads(r['metrics']),sort_keys=True) for r in h.db.execute('SELECT * FROM '+table))
            self.assertEqual(values(live),values(self.h),table)
        self.assertEqual(live.repo.stat(1,100,200)[0]['total_messages'],self.h.repo.stat(1,100,200)[0]['total_messages'])


class ImportApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h=Harness()
        self.client=TestClient(TestServer(create_app(TOKEN,self.h.repo,
            BusinessToolsService(self.h.bot,self.h.repo,None,{}),self.h.routes,product=self.h.product)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close();self.h.close()

    def auth(self,actor=100):
        return {'Authorization':'tma '+signed(actor)}

    async def upload(self,data=None):
        return await self.client.post('/api/import/preview?account=100',data=source(data or export()).read(),headers=self.auth())

    async def wait(self):
        for _ in range(100):
            response=await self.client.get('/api/import',headers=self.auth());job=await response.json()
            if job.get('state')!='importing':return job
            await asyncio.sleep(.01)
        self.fail('import did not finish')

    async def test_authenticated_preview_confirm_status_and_repeat(self):
        self.assertEqual((await self.client.post('/api/import/preview?account=100',data=b'{}')).status,401)
        response=await self.upload();self.assertEqual(response.status,200)
        job=await response.json();self.assertEqual(job['state'],'preview')
        self.assertEqual(self.h.db.execute('SELECT COUNT(*) FROM business_chat_stats').fetchone()[0],0)
        self.assertEqual((await self.client.post('/api/import/'+job['id']+'/start',headers=self.auth(300))).status,404)
        self.assertEqual((await self.client.post('/api/import/'+job['id']+'/start',headers=self.auth())).status,200)
        result=await self.wait();self.assertEqual((result['state'],result['added']),('done',3))
        job=await (await self.upload()).json()
        await self.client.post('/api/import/'+job['id']+'/start',headers=self.auth())
        result=await self.wait();self.assertEqual((result['added'],result['skipped']),(0,3))
        self.assertEqual((await self.client.get('/api/import',headers=self.auth(300))).status,200)
        self.assertEqual(await (await self.client.get('/api/import',headers=self.auth(300))).json(),{})

    async def test_cancel_preview_wrong_account_malformed_and_erasure_barrier(self):
        response=await self.client.post('/api/import/preview?account=300',data=source(export()).read(),headers=self.auth())
        self.assertEqual(response.status,400)
        response=await self.client.post('/api/import/preview?account=100',data=b'not json',headers=self.auth())
        self.assertEqual(response.status,400)
        job=await (await self.upload()).json()
        await self.client.delete('/api/import/'+job['id'],headers=self.auth())
        self.assertEqual(await (await self.client.get('/api/import',headers=self.auth())).json(),{})
        job=await (await self.upload()).json()
        self.h.product.erase(1,self.h.inbox)
        self.assertEqual((await self.client.post('/api/import/'+job['id']+'/start',headers=self.auth())).status,404)

    async def test_erasure_during_import_cannot_recreate_statistics(self):
        job=await (await self.upload(export(100))).json()
        real = import_batch
        def erase_after_first_batch(*args):
            result=real(*args)
            self.h.product.erase(1,self.h.inbox)
            return result
        with patch('business_tools.export_web.import_batch',side_effect=erase_after_first_batch):
            await self.client.post('/api/import/'+job['id']+'/start',headers=self.auth())
            await self.wait()
        self.assertEqual(self.h.db.execute('SELECT COUNT(*) FROM business_chat_stats').fetchone()[0],0)

    async def test_unconfirmed_preview_expires_without_writing_statistics(self):
        job=await (await self.upload()).json()
        future=time.monotonic()+601
        with patch('business_tools.export_web.time',SimpleNamespace(monotonic=lambda:future)):
            response=await self.client.post('/api/import/'+job['id']+'/start',headers=self.auth())
        self.assertEqual(response.status,404)
        self.assertEqual(self.h.db.execute('SELECT COUNT(*) FROM business_chat_stats').fetchone()[0],0)
        self.assertEqual((await self.upload()).status,200)
