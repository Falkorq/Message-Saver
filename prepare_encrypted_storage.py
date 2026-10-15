"""Pterodactyl one-off entry point; select instead of app.py with the bot stopped."""
from pathlib import Path
from dotenv import dotenv_values
from message_crypto import MessageCipher
from migrate_message_texts import migrate


def main():
    env=Path(__file__).with_name('.env')
    MessageCipher.from_env(env)
    print('Используется существующий ключ из .env; ключ не изменяется.')
    configured=dotenv_values(env,interpolate=False).get('DB_PATH') or 'messages.sqlite3'
    database=Path(configured);database=database if database.is_absolute() else env.parent/database
    counts,backup=migrate(database,env,apply=True,bot_stopped=True)
    print(f"Готово: зашифровано messages={counts['messages']}, outbox={counts['outbox']}.")
    if backup:print(f'Резервная копия: {backup}')
    print('Подготовка завершена. Верните PY_FILE=app.py и запустите бота.')


if __name__=='__main__':main()
