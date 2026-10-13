import logging
import asyncio
from pathlib import Path

from telebot.asyncio_helper import ApiTelegramException

from profile_monitor.ui import PLACEHOLDER_PNG

from .repository import BusinessToolsRepository
from .service import BusinessToolsService
from .ui import BusinessToolsUI


log = logging.getLogger(__name__)


class BusinessToolsModule:
    def __init__(self, bot, db, routes, account_labels, data_dir, enqueue_notification, banner_path=None):
        self.repo = BusinessToolsRepository(db, routes)
        self.repo.migrate()
        for account_id in routes:
            self.repo.reset_continuity(account_id)
        self.service = BusinessToolsService(
            bot, self.repo, enqueue_notification, account_labels
        )
        requested_banner = Path(banner_path) if banner_path else None
        if requested_banner and requested_banner.is_file():
            placeholder = requested_banner
        else:
            placeholder = Path(data_dir) / "profile_ui.png"
            placeholder.parent.mkdir(parents=True, exist_ok=True)
            if not placeholder.exists():
                placeholder.write_bytes(PLACEHOLDER_PNG)
        self.ui = BusinessToolsUI(bot, self.service, self.repo, routes, placeholder)
        self.ui.register()

    async def recover_history(self):
        while True:
            try:
                self.repo.backfill(max_batches=1)
                complete = self.repo.db.execute("SELECT value FROM business_stats_progress WHERE name='backfill_complete'").fetchone()
                if complete and complete[0]: return
            except Exception:
                self.repo.db.rollback()
                log.error('Восстановление статистики приостановлено; повтор через 5 секунд')
                await asyncio.sleep(5)
            await asyncio.sleep(0.05)

    async def handle_business_message(self, msg, record, account_id, owner_id):
        try:
            return await self.service.handle_business_message(
                msg, record, account_id, owner_id
            )
        except ApiTelegramException as exc:
            if exc.error_code in (400, 403) and "BOT_ACCESS_FORBIDDEN" in str(exc):
                log.warning("Telegram запретил удаление Business-сообщения %s: %s", msg.message_id, exc)
                return None
            log.exception("Ошибка business tools для сообщения %s", msg.message_id)
            return None
        except Exception:
            log.exception("Ошибка business tools для сообщения %s", msg.message_id)
            return None

    def consume_self_deleted(self, connection_id, chat_id, message_id):
        return self.repo.consume_self_deleted(connection_id, chat_id, message_id)
