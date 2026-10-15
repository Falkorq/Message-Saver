"""Account lifecycle and erasure barriers, independent of optional modules."""
import json
import os
from pathlib import Path
import time


class ProductState:
    def __init__(self, db, routes):
        self.db, self.routes = db, routes
        db.executescript('''
          CREATE TABLE IF NOT EXISTS account_state(
            account_id INTEGER PRIMARY KEY,owner_id INTEGER NOT NULL,since INTEGER NOT NULL,
            collecting INTEGER NOT NULL DEFAULT 1,cutoff INTEGER NOT NULL DEFAULT 0,
            last_event INTEGER,rights TEXT NOT NULL DEFAULT '{}');
          CREATE TABLE IF NOT EXISTS observation_intervals(
            account_id INTEGER NOT NULL,start_at INTEGER NOT NULL,end_at INTEGER,
            PRIMARY KEY(account_id,start_at));
          CREATE TABLE IF NOT EXISTS notification_preferences(
            owner_id INTEGER PRIMARY KEY,paused INTEGER NOT NULL DEFAULT 0,
            edits INTEGER NOT NULL DEFAULT 1,deletions INTEGER NOT NULL DEFAULT 1);
          CREATE TABLE IF NOT EXISTS data_confirmations(
            owner_id INTEGER PRIMARY KEY,nonce TEXT NOT NULL,expires_at INTEGER NOT NULL);
        ''')
        if 'peers' not in {r[1] for r in db.execute('PRAGMA table_info(notification_preferences)')}:
            db.execute('ALTER TABLE notification_preferences ADD COLUMN peers INTEGER NOT NULL DEFAULT 1')
        if 'erased_through' not in {r[1] for r in db.execute('PRAGMA table_info(account_state)')}:
            db.execute('ALTER TABLE account_state ADD COLUMN erased_through INTEGER NOT NULL DEFAULT 0')
        heartbeat = db.execute("SELECT name FROM sqlite_master WHERE name='receiver_runtime'").fetchone()
        previous = db.execute('SELECT last_poll FROM receiver_runtime WHERE id=1').fetchone() if heartbeat else None
        db.execute('UPDATE observation_intervals SET end_at=MAX(start_at,?) WHERE end_at IS NULL',
                   (previous[0] if previous else int(time.time()),))
        for account, owner in routes.items():
            self.ensure(account, owner)
            connection = db.execute('SELECT MAX(enabled) FROM connections WHERE account_id=?',(account,)).fetchone()[0]
            self.observe(account,bool(connection))
        db.commit()

    def ensure(self, account, owner):
        since=int(time.time())
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='business_chat_stats'").fetchone():
            columns={r[1] for r in self.db.execute('PRAGMA table_info(business_chat_stats)')}
            if 'first_observed_at' in columns:
                known=self.db.execute('SELECT MIN(first_observed_at) FROM business_chat_stats WHERE account_id=?',(account,)).fetchone()[0]
                if known: since=min(since,known)
        self.db.execute('INSERT OR IGNORE INTO account_state(account_id,owner_id,since) VALUES(?,?,?)',
                        (account, owner, since))
        if self.db.execute('SELECT cutoff FROM account_state WHERE account_id=?',(account,)).fetchone()[0]==0:
            self.db.execute('UPDATE account_state SET since=MIN(since,?) WHERE account_id=?',(since,account))
        self.db.commit()

    def observe(self, account, enabled):
        now=int(time.time())
        if enabled and self.allows(account):
            if not self.db.execute('SELECT 1 FROM observation_intervals WHERE account_id=? AND end_at IS NULL',(account,)).fetchone():
                self.db.execute('INSERT OR REPLACE INTO observation_intervals VALUES(?,?,NULL)',(account,now))
        else:
            self.db.execute('UPDATE observation_intervals SET end_at=? WHERE account_id=? AND end_at IS NULL',(now,account))
        self.db.commit()

    def allows(self, account, received_at=None):
        row = self.db.execute('SELECT * FROM account_state WHERE account_id=?', (account,)).fetchone()
        return bool(row and row['collecting'] and account in self.routes and
                    (received_at is None or (received_at*1000 if received_at<100000000000 else received_at) > (row['cutoff']*1000 if 0<row['cutoff']<100000000000 else row['cutoff'])))

    def collection(self, owner, enabled):
        now = int(time.time())
        for account, destination in self.routes.items():
            if destination == owner: self.ensure(account, owner)
        accounts = [r[0] for r in self.db.execute('SELECT account_id FROM account_state WHERE owner_id=?',(owner,))]
        for account in accounts:
            self.db.execute('UPDATE account_state SET collecting=?,cutoff=?,since=CASE WHEN ? AND erased_through>0 AND last_event IS NULL THEN ? ELSE since END WHERE account_id=?',
                            (int(enabled), int(time.time()*1000), int(enabled), now, account))
            self.db.execute('INSERT OR REPLACE INTO business_stats_progress VALUES(?,?)', (f'continuous_{account}', now))
            enabled_connection=self.db.execute('SELECT MAX(enabled) FROM connections WHERE account_id=?',(account,)).fetchone()[0]
            self.observe(account, bool(enabled and enabled_connection))
        self.db.commit()

    def notifications(self, owner, event, own=None):
        row = self.db.execute('SELECT * FROM notification_preferences WHERE owner_id=?', (owner,)).fetchone()
        return not row or (not row['paused'] and (own is not False or row['peers']) and (event not in ('edit', 'delete') or row['edits' if event == 'edit' else 'deletions']))

    def erase(self, owner, inbox, stamp=None):
        """Keep a collection barrier; neither backfill nor queued events can recreate erased data."""
        stamp = int(time.time()*1000) if stamp is None else stamp
        guard = getattr(self, 'erasure_guard', None)
        if guard:
            guard.record(owner, stamp)
        accounts = [r[0] for r in self.db.execute('SELECT account_id FROM account_state WHERE owner_id=?',(owner,))]
        photo_paths = [r[0] for r in self.db.execute('SELECT local_path FROM profile_photos WHERE owner_id=? AND local_path IS NOT NULL',(owner,))]
        self.collection(owner, False)
        self.db.execute('UPDATE account_state SET cutoff=MAX(cutoff,?) WHERE owner_id=?',(stamp,owner))
        with self.db:
            for account in accounts:
                connections = [r[0] for r in self.db.execute('SELECT connection_id FROM connections WHERE account_id=?', (account,))]
                for connection in connections:
                    for table in ('messages','message_versions','business_stats_seen','business_stats_events','business_self_deletions'):
                        self.db.execute(f'DELETE FROM {table} WHERE connection_id=?', (connection,))
                for table in ('business_chat_stats','business_stats_days','business_stats_words','business_stats_daily',
                              'business_stats_chat_metrics','business_stats_side_words','business_mutes','business_avatar_cache','observation_intervals',
                              'business_stats_imported','business_stats_seen'):
                    if self.db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                        self.db.execute(f'DELETE FROM {table} WHERE account_id=?', (account,))
                self.db.execute('DELETE FROM business_stats_events WHERE connection_id=?', (f'nyan-export:{account}',))
                self.db.execute('UPDATE account_state SET last_event=NULL,since=? WHERE account_id=?', (int(time.time()),account))
            self.db.execute('DELETE FROM outbox WHERE recipient_id=?', (owner,))
            for table in ('profile_watchers','profile_changes','profile_photos','profile_settings'):
                self.db.execute(f'DELETE FROM {table} WHERE owner_id=?', (owner,))
            self.db.execute('DELETE FROM business_stats_ui WHERE actor_id IN (SELECT account_id FROM account_state WHERE owner_id=?)', (owner,))
            self.db.execute('DELETE FROM style_emojis WHERE recipient_id=?', (owner,))
            self.db.execute('DELETE FROM settings WHERE recipient_id=?', (owner,))
            self.db.execute('DELETE FROM notification_preferences WHERE owner_id=?', (owner,))
            self.db.execute('UPDATE account_state SET erased_through=MAX(erased_through,?) WHERE owner_id=?',(stamp,owner))
            account_set = set(accounts)
            connection_set = {row[0] for account in accounts for row in self.db.execute(
                'SELECT connection_id FROM connections WHERE account_id=?', (account,))}
            for row in self.db.execute('SELECT * FROM incoming_updates WHERE payload IS NOT NULL').fetchall():
                payload = inbox.payload(row)
                if payload.get('account_id') in account_set or payload.get('connection_id') in connection_set:
                    self.db.execute('DELETE FROM incoming_updates WHERE update_id=?', (row['update_id'],))


        photo_root = Path(os.getenv('PROFILE_DATA_DIR','profile_data')).resolve()
        for raw in photo_paths:
            path = Path(raw).resolve()
            if path.is_relative_to(photo_root) and not self.db.execute('SELECT 1 FROM profile_photos WHERE local_path=?',(raw,)).fetchone():
                path.unlink(missing_ok=True)

    def states(self, owner):
        result = []
        for account, destination in self.routes.items():
            if destination != owner:
                continue
            row = self.db.execute('SELECT * FROM account_state WHERE account_id=?', (account,)).fetchone()
            connection = self.db.execute('SELECT MAX(enabled) FROM connections WHERE account_id=?', (account,)).fetchone()[0]
            result.append(dict(account_id=account, since=row['since'] if row else None,
                last_event=row['last_event'] if row else None,
                state='paused' if row and not row['collecting'] else 'not_connected' if connection is None else
                      'disconnected' if not connection else 'waiting' if not row or not row['last_event'] else 'connected'))
        return result
