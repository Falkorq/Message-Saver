import asyncio
import logging
import os
from pathlib import Path

from .repository import ProfileRepository
from .scheduler import ProfileScheduler
from .service import ProfileMonitorService, notification_text
from .telegram_client import ProfileTelegramClient
from .ui import ProfileUI


log = logging.getLogger(__name__)


class ProfileMonitorModule:
    """Lifecycle boundary for the optional MTProto profile monitor."""

    def __init__(self, bot, db, owner_routes, data_dir, enqueue_notification, banner_path=None,
                 access_policy=None):
        self.bot = bot
        self.repo = ProfileRepository(db)
        self.repo.migrate()
        self.owner_routes = owner_routes
        self.access_policy = access_policy
        self.data_dir = Path(data_dir)
        self.enqueue_notification = enqueue_notification
        self.telegram = None
        self.service = None
        self.scheduler = None
        self.task = None
        self.enabled = False
        self.state = "disabled"
        self.lifecycle_task = None

        api_id = os.getenv("TELEGRAM_API_ID")
        api_hash = os.getenv("TELEGRAM_API_HASH")
        session = os.getenv("TELEGRAM_SESSION")
        supplied = [bool(api_id), bool(api_hash), bool(session)]
        if any(supplied) and not all(supplied):
            self.state = "configuration_error"
            log.error("MTProto: неполная конфигурация; основной бот продолжит работу")
            return
        if not all(supplied):
            log.warning("Мониторинг профилей выключен: MTProto-параметры не настроены")
            return

        try:
            api_id = int(api_id)
        except ValueError as exc:
            self.state = "configuration_error"
            return
        try:
            self.telegram = ProfileTelegramClient(api_id, api_hash, session, self.data_dir / "photos", db=db)
        except Exception:
            self.state = "configuration_error"
            log.error("MTProto: сессия недоступна; основной бот продолжит работу")
            return

        async def notifier(owner_id, old, new, changes, format_name):
            self.enqueue_notification(owner_id, notification_text(old, new, changes, format_name))

        self.service = ProfileMonitorService(self.repo, self.telegram, notifier, self.can_access_owner)
        self.scheduler = ProfileScheduler(self.repo, self.service, self.owner_routes, self.can_access_owner)
        self.ui = ProfileUI(
            bot, self.service, self.repo, self.owner_routes, self.data_dir, banner_path,
            access_policy=self.access_policy,
        )
        self.ui.register()
        self.enabled = True

    def can_access_owner(self, owner_id):
        # An omitted policy never implicitly opens the shared session.
        return bool(self.access_policy and self.access_policy.owner(owner_id))

    async def start(self):
        if not self.enabled:
            return
        self.lifecycle_task = asyncio.create_task(self._supervise(), name='optional-mtproto')

    async def _supervise(self):
        reported = False
        while True:
            try:
                self.state = 'connecting'
                await asyncio.wait_for(self.telegram.start(), 20)
                self.state = 'ready'
                reported = False
                self.task=asyncio.create_task(self.scheduler.run(),name='profile-scheduler')
                while True:
                    done,_=await asyncio.wait({self.task},timeout=30)
                    if done:
                        self.task.result()
                        return
                    if self.telegram.auth_failed or not self.telegram.client.is_connected():
                        raise RuntimeError('mtproto_disconnected')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if getattr(self,'task',None):
                    self.task.cancel()
                    await asyncio.gather(self.task,return_exceptions=True)
                    self.task=None
                self.state = 'unavailable'
                log.error('MTProto недоступен (%s); основной бот работает', type(exc).__name__)
                if not reported:
                    for owner in set(self.owner_routes.values()):
                        if self.access_policy and owner in getattr(self.access_policy, 'operator_ids', set()):
                            self.enqueue_notification(owner, 'Закрытый модуль профилей недоступен. Основной бот и статистика работают. Проверьте личную сессию на сервере.')
                    reported = True
                await self.telegram.close()
                await asyncio.sleep(60)
            finally:
                if getattr(self,'task',None):
                    self.task.cancel()
                    await asyncio.gather(self.task,return_exceptions=True)
                    self.task=None

    async def close(self):
        if self.lifecycle_task:
            self.lifecycle_task.cancel()
            await asyncio.gather(self.lifecycle_task, return_exceptions=True)
        if self.telegram:
            await self.telegram.close()
