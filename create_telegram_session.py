"""One-time local helper that creates TELEGRAM_SESSION for the profile monitor."""

import asyncio
import os

from pathlib import Path
from dotenv import load_dotenv, set_key
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    load_dotenv()
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        env = Path(__file__).with_name(".env")
        if not env.is_file():
            raise RuntimeError("Сначала создайте .env из .env.example")
        set_key(str(env), "TELEGRAM_SESSION", client.session.save())
        os.chmod(env, 0o600)
        print("Сессия сохранена в .env. Не передавайте этот файл другим людям.")


if __name__ == "__main__":
    asyncio.run(main())
