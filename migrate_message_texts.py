"""One-off, transactional migration. Default mode only checks; --apply requires a stopped bot."""
import argparse
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from datetime import datetime,timezone
from uuid import uuid4
from contextlib import closing

from dotenv import dotenv_values
from message_crypto import MessageCipher, EncryptionError, FIELDS, STATS_FIELD, protected
from encrypted_backup import encrypt_backup, restore_backup


def convert(data,table,cipher):
    try:
        value=json.loads(data)
        if not isinstance(value,dict):raise ValueError()
        records=[value] if table=='messages' else [value[f] for f in ('old','new') if f in value]
        for record in records:
            cipher.validate_record(record,authenticate=True)
        if table=='outbox' and protected(value.get('text')):
            cipher.open(value['text'],'notification-text')
        result=cipher.protect_record(value) if table=='messages' else cipher.protect_payload(value)
        # Verify the generated ciphertext, not just its version marker.
        cipher.open_payload({'old':result} if table=='messages' else result)
        if result==value:return None
        return json.dumps(result,ensure_ascii=False)
    except (ValueError,KeyError,TypeError):
        raise EncryptionError('Повреждённая запись или неверный ключ: миграция отменена.') from None


def migrate(database,env_file,apply=False,bot_stopped=False):
    if apply and not bot_stopped:
        raise EncryptionError('Остановите бота и добавьте --bot-stopped.')
    cipher=MessageCipher.from_env(env_file)
    database=Path(database).resolve()
    if not database.is_file():raise EncryptionError('Рабочая база не найдена; новая не создаётся.')
    db=sqlite3.connect(database.as_uri()+'?mode=rw',uri=True,timeout=0)
    counts={'messages':0,'outbox':0};backup=None
    try:
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
            raise EncryptionError('Проверка SQLite не пройдена.')
        tables=[t for t in counts if db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?',(t,)).fetchone()]
        if 'messages' not in tables:raise EncryptionError('В этой базе нет таблицы messages.')
        # Reserve the writer before validation and snapshot; no concurrent writes can race migration.
        db.execute('BEGIN IMMEDIATE')
        for table in tables:
            for rowid,data in db.execute(f'SELECT rowid,data FROM {table}'):
                if convert(data,table,cipher) is not None:counts[table]+=1
        if not apply:
            db.rollback();return counts,None
        if not sum(counts.values()):
            db.rollback()
            if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]!=0:
                raise EncryptionError('WAL занят другим подключением. Остановите бота и повторите.')
            return counts,None
        directory=database.parent/'encryption_backups';directory.mkdir(mode=0o700,exist_ok=True)
        name=f'{database.stem}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}.sqlite3.aes'
        backup=directory/name
        fd,temp=tempfile.mkstemp(prefix='.snapshot-',suffix='.sqlite3',dir=directory);os.close(fd)
        try:
            with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=0)) as reader:
                snapshot=sqlite3.connect(temp)
                try:
                    reader.backup(snapshot)
                    if snapshot.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise EncryptionError('Ошибка копии SQLite.')
                finally:snapshot.close()
            encrypt_backup(temp,backup,cipher)
        finally:Path(temp).unlink(missing_ok=True)
        db.execute('PRAGMA secure_delete=ON')
        for table in tables:
            # Bounded iteration, one transaction; no row/column/schema changes besides data.
            cursor=None
            while True:
                rows=db.execute(f'SELECT rowid,data FROM {table} '+
                    ('WHERE rowid>? ' if cursor is not None else '')+'ORDER BY rowid LIMIT 250',
                    (cursor,) if cursor is not None else ()).fetchall()
                if not rows:break
                for rowid,data in rows:
                    changed=convert(data,table,cipher)
                    if changed is not None:db.execute(f'UPDATE {table} SET data=? WHERE rowid=?',(changed,rowid))
                cursor=rows[-1][0]
        db.commit()
        if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]!=0:
            raise EncryptionError('Данные зашифрованы, но WAL занят. Остановите подключения к базе и повторите очистку.')
        # Do not VACUUM: implicit rowids drive the statistics backfill checkpoint.
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise EncryptionError('Ошибка проверки после миграции.')
        return counts,backup
    except BaseException:
        db.rollback();raise
    finally:db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',default=str(Path(__file__).with_name('.env')))
    parser.add_argument('--db',help='По умолчанию DB_PATH из .env, относительно файла .env')
    parser.add_argument('--apply',action='store_true');parser.add_argument('--bot-stopped',action='store_true')
    parser.add_argument('--restore-backup');parser.add_argument('--output',help='Новый файл для восстановления, без замены рабочей базы')
    args=parser.parse_args();env=Path(args.env_file).resolve()
    try:
        if args.restore_backup:
            if not args.output:parser.error('Для восстановления нужен --output.')
            restore_backup(args.restore_backup,args.output,MessageCipher.from_env(env))
            print('Резервная копия восстановлена в новый файл.');return
        configured=args.db or dotenv_values(env,interpolate=False).get('DB_PATH') or 'messages.sqlite3'
        path=Path(configured);path=path if path.is_absolute() else env.parent/path
        counts,backup=migrate(path,env,args.apply,args.bot_stopped)
        print(('Зашифровано' if args.apply else 'Готово к шифрованию')+f": messages={counts['messages']}, outbox={counts['outbox']}.")
        if backup:print(f'Зашифрованная резервная копия: {backup}')
        if not args.apply:print('База не изменена. Для применения: --apply --bot-stopped после остановки бота.')
    except (EncryptionError,sqlite3.Error,OSError) as exc:
        raise SystemExit(str(exc)) from None


if __name__=='__main__':main()
