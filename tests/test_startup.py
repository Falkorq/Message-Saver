"""Exercise the real startup function without loading credentials or connecting."""
import ast
import asyncio
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telebot import types
from telebot.asyncio_helper import ApiTelegramException


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def run_startup(self, poll_error=None):
        source=Path(__file__).resolve().parents[1]/'app.py'
        tree=ast.parse(source.read_text(encoding='utf-8'))
        main=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='main')
        limited=ApiTelegramException('setMyCommands',None,{
            'ok':False,'error_code':429,'description':'Too Many Requests',
            'parameters':{'retry_after':79744}})
        bot=SimpleNamespace(set_my_name=AsyncMock(side_effect=limited),
            set_my_commands=AsyncMock(side_effect=limited),
            infinity_polling=AsyncMock(side_effect=poll_error),close_session=AsyncMock())
        async def background():
            await asyncio.Event().wait()
        profile=SimpleNamespace(start=AsyncMock(),close=AsyncMock())
        db=Mock()
        context={'asyncio':asyncio,'os':SimpleNamespace(getenv=lambda _:None),
            'bot':bot,'types':types,'profile_monitor':profile,'db':db,
            'db_path':Path('synthetic.sqlite3'),'message_cipher':None,
            'business_tools':SimpleNamespace(recover_history=background),
            'supervise_outbox':background,'maintain_storage':background,'durable_polling':bot.infinity_polling,'log':logging.getLogger('startup-test')}
        exec(compile(ast.Module(body=[main],type_ignores=[]),str(source),'exec'),context)
        if poll_error:
            with self.assertRaisesRegex(RuntimeError,'poll stopped'):
                await context['main']()
        else:
            await context['main']()
        bot.set_my_name.assert_not_awaited()
        bot.infinity_polling.assert_awaited_once()
        bot.close_session.assert_awaited_once()
        profile.close.assert_awaited_once();db.close.assert_called_once()

    async def test_telegram_name_and_command_limits_do_not_prevent_polling(self):
        await self.run_startup()

    async def test_http_session_closed_on_poll_failure(self):
        await self.run_startup(RuntimeError('poll stopped'))


if __name__=='__main__':unittest.main()
