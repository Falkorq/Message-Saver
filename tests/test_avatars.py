import asyncio
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer
from business_tools.avatars import AvatarCache, MAX_IMAGE_BYTES, image_type
from business_tools.repository import BusinessToolsRepository
from business_tools.service import BusinessToolsService
from business_tools.webapp import create_app
from tests.test_webapp import signed, TOKEN
from profile_monitor.telegram_client import ProfileTelegramClient

JPEG = b'\xff\xd8\xffavatar'


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(':memory:'); self.db.row_factory = sqlite3.Row
        self.bot = SimpleNamespace(
            get_user_profile_photos=AsyncMock(return_value=SimpleNamespace(photos=[[
                SimpleNamespace(width=90,height=90,file_size=16,file_id='small'),
                SimpleNamespace(width=640,height=640,file_size=1000,file_id='big')]])),
            get_file=AsyncMock(return_value=SimpleNamespace(file_path='private/path',file_size=16)),
            download_file=AsyncMock(return_value=JPEG))
        self.mt = SimpleNamespace(client=SimpleNamespace(is_connected=lambda:True), avatar=AsyncMock(return_value=(JPEG, 123)))
        self.module = SimpleNamespace(enabled=True,telegram=self.mt,repo=SimpleNamespace(get=lambda *_:None),
                                      can_access_owner=lambda owner: owner == 1)
        self.now = 1000
        self.cache = AvatarCache(self.bot, self.db, self.module, job_interval=0, mt_interval=0,
                                 clock=lambda:self.now)
        await self.cache.start()

    async def asyncTearDown(self):
        await self.cache.close(); self.db.close()

    async def drain(self):
        await asyncio.wait_for(self.cache.queue.join(), 2)

    async def test_dedup_small_photo_and_persistent_cache(self):
        self.assertEqual(self.cache.request(1,100,200,'alice')[0], 'pending')
        self.cache.request(1,100,200,'alice')
        await self.drain()
        self.assertEqual(self.cache.request(1,100,200,'alice'), ('ready',JPEG,'image/jpeg'))
        self.bot.get_user_profile_photos.assert_awaited_once_with(200,limit=1)
        self.bot.get_file.assert_awaited_once_with('small')
        self.mt.avatar.assert_not_awaited()
        restarted = AvatarCache(None,self.db,clock=lambda:self.now)
        self.assertEqual(restarted.request(1,100,200,'alice')[0], 'ready')
        self.now += 86401
        self.assertEqual(self.cache.request(1,100,200,'alice')[0], 'pending')
        await self.drain()
        self.assertEqual(self.bot.get_user_profile_photos.await_count,2)

    async def test_mt_fallback_and_hourly_budget(self):
        self.bot.get_user_profile_photos.return_value = SimpleNamespace(photos=[])
        self.cache.mt_hour_limit = 1
        self.cache.request(1,100,200,'alice'); await self.drain()
        self.assertEqual(self.cache.request(1,100,200,'alice')[0],'ready')
        self.mt.avatar.assert_awaited_once_with(200,username='alice',access_hash=None)
        self.cache.request(1,100,201,'bob'); await self.drain()
        self.assertEqual(self.cache.request(1,100,201,'bob')[0],'none')
        self.assertEqual(self.mt.avatar.await_count,1)

    async def test_negative_cache_and_bounded_storage(self):
        self.bot.get_user_profile_photos.return_value=SimpleNamespace(photos=[])
        self.mt.avatar.return_value=(None,None)
        self.cache.max_entries=2
        for chat in (200,201,202):
            self.cache.request(1,100,chat,''); await self.drain();self.now+=1
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_avatar_cache').fetchone()[0],2)
        calls=self.bot.get_user_profile_photos.await_count
        self.assertEqual(self.cache.request(1,100,202,'')[0],'none')
        self.assertEqual(self.bot.get_user_profile_photos.await_count,calls)

    async def test_floodwait_cools_down_mt_for_other_chats(self):
        self.bot.get_user_profile_photos.return_value=SimpleNamespace(photos=[])
        error=RuntimeError('not logged');error.seconds=600
        self.mt.avatar.side_effect=error
        self.cache.request(1,100,200,'alice');await self.drain()
        self.cache.request(1,100,201,'bob');await self.drain()
        self.assertEqual(self.mt.avatar.await_count,1)
        self.assertGreaterEqual(self.cache.mt_until,self.now+600)

    async def test_invalid_image_and_queue_bound(self):
        self.assertIsNone(image_type(b'<html>private token</html>'))
        self.assertIsNone(image_type(JPEG+b'x'*MAX_IMAGE_BYTES))
        bounded=AvatarCache(None,self.db,queue_size=1)
        bounded.worker=True  # Inspect admission without allowing a worker to drain.
        self.assertEqual(bounded.request(1,100,300,'')[0],'pending')
        self.assertEqual(bounded.request(1,100,301,'')[0],'busy')


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_needs_current_chat_ownership_and_auth(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        routes={100:1,300:3};repo=BusinessToolsRepository(db,routes);repo.migrate()
        repo.record_message('100',100,1,1,dict(chat_id=200,chat_name='Alice',sender_id=200,
            type='text',text='private',sent_at=1000,metadata={}))
        cache=AvatarCache(None,db);cache._save((100,200),JPEG,86400)
        client=TestClient(TestServer(create_app(TOKEN,repo,BusinessToolsService(None,repo,None,{}),routes,cache)))
        await client.start_server()
        try:
            path='/api/avatars/200?account=100'
            self.assertEqual((await client.get(path)).status,401)
            response=await client.get(path,headers={'Authorization':'tma '+signed()})
            self.assertEqual(response.status,200);self.assertEqual(await response.read(),JPEG)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            self.assertEqual(response.headers['Content-Type'],'image/jpeg')
            self.assertEqual((await client.get(path,headers={'Authorization':'tma '+signed(300)})).status,400)
            self.assertEqual((await client.get('/api/avatars/999?account=100',headers={'Authorization':'tma '+signed()})).status,404)
            del routes[100]
            self.assertEqual((await client.get(path,headers={'Authorization':'tma '+signed()})).status,403)
        finally:
            await client.close();db.close()


class MtPhotoTests(unittest.IsolatedAsyncioTestCase):
    async def test_recycled_username_never_downloads_another_user(self):
        wrapper=ProfileTelegramClient.__new__(ProfileTelegramClient)
        wrapper.avatar_lock=asyncio.Lock()
        class FakeClient:
            session=SimpleNamespace(get_input_entity=lambda _:None)
            download_profile_photo=AsyncMock()
            async def __call__(self, request, **kwargs):
                return SimpleNamespace(users=[SimpleNamespace(id=999,photo=SimpleNamespace(photo_id=5))])
        wrapper.client=FakeClient()
        self.assertEqual(await wrapper.avatar(200,username='recycled'),(None,None))
        wrapper.client.download_profile_photo.assert_not_awaited()

    async def test_saved_hash_uses_small_photo_without_dialog_scan(self):
        wrapper=ProfileTelegramClient.__new__(ProfileTelegramClient)
        wrapper.avatar_lock=asyncio.Lock()
        user=SimpleNamespace(id=200,access_hash=123,photo=SimpleNamespace(photo_id=5))
        class FakeClient:
            download_profile_photo=AsyncMock(return_value=JPEG)
            async def __call__(self, request, **kwargs):
                self.asserted=kwargs['flood_sleep_threshold']==0
                return [user]
        wrapper.client=FakeClient()
        self.assertEqual(await wrapper.avatar(200,access_hash=123),(JPEG,123))
        self.assertTrue(wrapper.client.asserted)
        wrapper.client.download_profile_photo.assert_awaited_once_with(user,file=bytes,download_big=False)


if __name__=='__main__':unittest.main()
