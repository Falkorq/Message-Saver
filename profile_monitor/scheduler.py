import asyncio
import logging
import random
import time

from telethon.errors import FloodWaitError


log = logging.getLogger(__name__)


class ProfileScheduler:
    def __init__(self, repository, service, owner_routes=None, access_check=None):
        self.repo = repository
        self.service = service
        self.owner_routes = owner_routes
        self.access_check = access_check
        self._stopping = False

    async def run(self):
        # Existing snapshots are baselines. A restart never creates synthetic changes.
        while not self._stopping:
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.error("Цикл мониторинга приостановлен; повтор через 30 секунд")
            await asyncio.sleep(30)

    async def run_cycle(self, pause_between=True):
        rows = self.repo.due(int(time.time()))
        for row in rows:
            if self._stopping:
                break
            if self.owner_routes is not None and row["owner_id"] not in self.owner_routes.values():
                continue
            if self.access_check is not None and not self.access_check(row["owner_id"]):
                continue
            try:
                await self.service.check_row(row)
            except FloodWaitError as exc:
                self.repo.set_error(row["owner_id"], row["user_id"], f"FloodWait {exc.seconds}s")
                if pause_between:
                    # Один FloodWait — сигнал о лимите на весь аккаунт: прерываем цикл целиком.
                    await asyncio.sleep(0)
                    return
            except asyncio.CancelledError:
                raise
            except ValueError as exc:
                log.warning("Проверка профиля недоступна (%s)",type(exc).__name__)
                self.repo.set_error(row["owner_id"], row["user_id"], exc)
            except Exception as exc:
                log.error("Проверка профиля отложена (%s)",type(exc).__name__)
                self.repo.set_error(row["owner_id"], row["user_id"], exc)
            if pause_between:
                await asyncio.sleep(random.uniform(0.4, 1.2))

    def stop(self):
        self._stopping = True
