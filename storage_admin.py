"""Offline backup/restore utility. Never prints credentials and never overwrites a DB."""
import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import time
from dotenv import dotenv_values
from message_crypto import MessageCipher
from encrypted_backup import restore_backup
from business_tools.backup import backup_database
from erasure_guard import ErasureGuard
from reliable import AtomicConnection, DurableInbox
from product_state import ProductState
from access_control import AccessManager
from business_tools.repository import BusinessToolsRepository
from profile_monitor.repository import ProfileRepository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['backup','restore'])
    parser.add_argument('--source', help='Encrypted backup for restore')
    parser.add_argument('--target', help='New, nonexistent SQLite path for restore')
    parser.add_argument('--ledger', help='Newest erasure-guard.aes, stored separately from backups')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    env = root/'.env'
    cipher = MessageCipher.from_env(env)
    settings = dotenv_values(env)
    db_path = Path(settings.get('DB_PATH') or 'messages.sqlite3')
    if not db_path.is_absolute(): db_path = root/db_path
    if args.action == 'backup':
        if not db_path.is_file(): parser.error('Database does not exist')
        with closing(sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True)) as db:
            result = backup_database(db,'regular',cipher=cipher)
        guard = ErasureGuard(db_path.with_name('erasure-guard.aes'),cipher)
        if not guard.path.exists(): guard.record(0,0)
        # Only regular snapshots are pruned, never migration copies or arbitrary files.
        for path in result.parent.glob(db_path.stem+'-regular-*.sqlite3.aes'):
            if path.stat().st_mtime < time.time()-7*86400: path.unlink()
        print('Encrypted backup verified:',result)
    else:
        if not args.source or not args.target or not args.ledger:
            parser.error('restore requires --source, --target and --ledger')
        guard = ErasureGuard(args.ledger,cipher)
        if not guard.path.is_file(): parser.error('Newest erasure ledger is required')
        guard.read()  # authenticate before creating anything
        target = Path(args.target).resolve()
        restore_backup(args.source,target,cipher)
        db=None
        try:
            db = sqlite3.connect(target,factory=AtomicConnection); db.row_factory=sqlite3.Row
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Restore integrity check failed')
            # Build ownership before quarantine, including copies made before lifecycle tables existed.
            routes = {}
            for prefix in ('MY','SECOND'):
                destination=settings.get(prefix+'_CHAT_ID')
                for account in (settings.get(prefix+'_ACCOUNT_IDS') or '').split(','):
                    if account.strip() and destination:
                        routes[int(account.strip())]=int(destination)
            for row in db.execute('SELECT account_id,recipient_id FROM connections'):
                routes.setdefault(row[0],row[1] or row[0])
            AccessManager(db,routes,{},set(routes))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='business_chat_stats'").fetchone():
                for row in db.execute('SELECT DISTINCT account_id,owner_id FROM business_chat_stats'):
                    routes[row[0]]=row[1]
            BusinessToolsRepository(db,routes,message_cipher=cipher).migrate()
            ProfileRepository(db).migrate()
            inbox = DurableInbox(db,cipher)
            product = ProductState(db,routes)
            guard.apply(product,inbox)
            # Old permissions and pending sends must never come alive from an old snapshot.
            with db:
                db.execute('DELETE FROM invited_accounts')
                db.execute('DELETE FROM outbox')
                db.execute('DELETE FROM incoming_updates')
                db.execute('UPDATE account_state SET collecting=0,cutoff=?',(int(time.time()*1000),))
                db.execute('UPDATE connections SET enabled=0,recipient_id=0')
                db.execute('UPDATE profile_watchers SET enabled=0')
                db.execute('CREATE TABLE IF NOT EXISTS initial_access_seeds(user_id INTEGER PRIMARY KEY)')
                configured=settings.get('MTPROTO_ALLOWED_USER_IDS') or '6640457762,6851862358,803489862'
                for value in configured.replace(',',' ').split():
                    db.execute('INSERT OR IGNORE INTO initial_access_seeds VALUES(?)',(int(value),))
            db.close()
            os.chmod(target,0o600)
        except BaseException:
            if db is not None:db.close()
            target.unlink(missing_ok=True)
            raise
        print('Restore verified and quarantined. Reissue invitations and reconnect accounts explicitly:',target)


if __name__ == '__main__':
    main()
