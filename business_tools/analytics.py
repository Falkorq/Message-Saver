"""Extensions to the existing statistics repository; no message bodies are retained.

Periods select the sending date (also for edits/deletions); responses and starts
select the date of the responding/starting message. All intervals are half-open.
"""
import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from message_crypto import text_metrics, content_fingerprint

MOSCOW = timezone(timedelta(hours=3), "Europe/Moscow")
PERIODS = {"today": "Сегодня", "7": "7 дней", "30": "30 дней", "all": "Всё время"}
PAUSE = 4 * 3600


def period_bounds(period="all", now=None):
    if period not in PERIODS:
        raise ValueError("Неизвестный период")
    now = int(time.time()) if now is None else int(now)
    midnight = datetime.fromtimestamp(now, MOSCOW).replace(hour=0, minute=0, second=0, microsecond=0)
    start = None if period == "all" else int((midnight - timedelta(days=(1 if period == "today" else int(period)) - 1)).timestamp())
    return start, int((midnight + timedelta(days=1)).timestamp())


def day_of(stamp):
    return datetime.fromtimestamp(stamp, MOSCOW).strftime("%Y-%m-%d")


def histogram_summary(metrics, prefix):
    buckets = sorted((int(key[len(prefix):]), count) for key, count in metrics.items()
                     if key.startswith(prefix) and count > 0)
    count = sum(n for _, n in buckets)
    if not count:
        return {"count": 0, "mean": None, "median": None}
    targets = ((count - 1) // 2, count // 2)
    values = []
    cumulative = 0
    for seconds, n in buckets:
        values.extend(seconds for target in targets if cumulative <= target < cumulative + n)
        cumulative += n
    return {"count": count, "mean": sum(v * n for v, n in buckets) / count,
            "median": sum(values) / 2}


def observation_windows(db, accounts, progress, now):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='observation_intervals'").fetchone():
        windows={a:[] for a in accounts}
        if accounts:
            for row in db.execute('SELECT account_id,start_at,end_at FROM observation_intervals WHERE account_id IN ('+','.join('?' for _ in accounts)+')',accounts):
                windows[row[0]].append((row[1],row[2] if row[2] is not None else now))
        return windows
    return {a:[(max(progress.get('coverage_start',now),progress.get(f'continuous_{a}',now)),now)] for a in accounts}


def day_is_known(windows, stamp, now):
    return bool(windows) and all(any(start<=stamp and end>=min(stamp+86400,now) for start,end in intervals) for intervals in windows.values())


class AnalyticsRepository:
    def migrate_analytics(self):
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS business_stats_imported(
            account_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,
            connection_id TEXT NOT NULL,imported_at INTEGER NOT NULL,
            PRIMARY KEY(account_id,chat_id,message_id));
        ''')
        if 'first_observed_at' not in {r[1] for r in self.db.execute('PRAGMA table_info(business_chat_stats)')}:
            self.db.execute('ALTER TABLE business_chat_stats ADD COLUMN first_observed_at INTEGER')
            # Existing chats must never become "first seen today" just because
            # their original first observation cannot be recovered. Zero means unknown.
            self.db.execute('UPDATE business_chat_stats SET first_observed_at=0')
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='connections'").fetchone():
                self.db.execute('CREATE INDEX IF NOT EXISTS business_connection_account ON connections(account_id,connection_id)')
                self.db.execute('UPDATE business_chat_stats SET first_observed_at=COALESCE('
                                '(SELECT MIN(v.observed_at) FROM business_stats_seen v JOIN connections c '
                                'ON c.connection_id=v.connection_id WHERE c.account_id=business_chat_stats.account_id '
                                'AND v.chat_id=business_chat_stats.chat_id),0)')
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(business_stats_seen)")}
        for name, definition in {
            "account_id": "INTEGER", "sent_at": "INTEGER", "side": "TEXT",
            "chars": "INTEGER", "kind": "TEXT", "duration": "INTEGER",
            "deleted_at": "INTEGER", "edits": "INTEGER NOT NULL DEFAULT 0",
            "fingerprint": "TEXT", "detailed": "INTEGER NOT NULL DEFAULT 0",
            "counted": "INTEGER NOT NULL DEFAULT 1",
        }.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE business_stats_seen ADD COLUMN {name} {definition}")
        self.db.executescript("""
        CREATE INDEX IF NOT EXISTS business_seen_identity
          ON business_stats_seen(account_id,chat_id,message_id);
        CREATE INDEX IF NOT EXISTS business_seen_order
          ON business_stats_seen(account_id,chat_id,sent_at,connection_id,message_id) WHERE detailed=1;
        CREATE INDEX IF NOT EXISTS business_seen_last_live
          ON business_stats_seen(account_id,chat_id,sent_at DESC,message_id DESC) WHERE detailed=1 AND deleted_at IS NULL;
        CREATE INDEX IF NOT EXISTS business_seen_unknown ON business_stats_seen(account_id,chat_id) WHERE detailed=0;
        CREATE INDEX IF NOT EXISTS business_seen_longest ON business_stats_seen(account_id,chat_id,side,chars DESC) WHERE detailed=1;
        CREATE TABLE IF NOT EXISTS business_stats_daily(
          account_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,day TEXT NOT NULL,
          metrics TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(account_id,chat_id,day));
        CREATE TABLE IF NOT EXISTS business_stats_chat_metrics(
          account_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,metrics TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(account_id,chat_id));
        CREATE INDEX IF NOT EXISTS business_daily_period ON business_stats_daily(account_id,day,chat_id);
        CREATE TABLE IF NOT EXISTS business_stats_side_words(
          account_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,day TEXT NOT NULL,
          side TEXT NOT NULL,word TEXT NOT NULL,count INTEGER NOT NULL,
          PRIMARY KEY(account_id,chat_id,day,side,word));
        CREATE TABLE IF NOT EXISTS business_stats_events(
          connection_id TEXT NOT NULL,chat_id INTEGER NOT NULL,message_id INTEGER NOT NULL,
          event TEXT NOT NULL,version TEXT NOT NULL,detected_at INTEGER NOT NULL,
          PRIMARY KEY(connection_id,chat_id,message_id,event,version));
        CREATE TABLE IF NOT EXISTS business_stats_progress(
          name TEXT PRIMARY KEY,value INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS business_stats_ui(
          actor_id INTEGER NOT NULL,message_id INTEGER NOT NULL,state TEXT NOT NULL,
          updated_at INTEGER NOT NULL,PRIMARY KEY(actor_id,message_id));
        """)
        self.db.execute("INSERT OR IGNORE INTO business_stats_progress VALUES('coverage_start',?)", (int(time.time()),))
        self.db.execute("INSERT OR IGNORE INTO business_stats_progress VALUES('schema',2)")
        self.db.commit()

    def check_account(self, owner_id, account_id=None):
        accounts = [a for a, o in self.routes.items() if o == owner_id]
        if account_id is not None and account_id not in accounts:
            raise LookupError("Нет доступа к аккаунту")
        return [account_id] if account_id is not None else accounts

    def reset_continuity(self, account_id, now=None):
        self.db.execute('INSERT OR REPLACE INTO business_stats_progress VALUES(?,?)',
                        (f'continuous_{account_id}',int(time.time()) if now is None else int(now)))
        self.db.commit()

    def check_chat_owner(self, owner_id, account_id, chat_id):
        row=self.db.execute('SELECT owner_id FROM business_chat_stats WHERE account_id=? AND chat_id=?',
                            (account_id,chat_id)).fetchone()
        if row and row[0]!=owner_id:
            raise LookupError('Нет доступа к сохранённой истории чата')

    def compare_counts(self, owner_id, account_id, chat_id, start, end, now):
        self.check_account(owner_id,account_id)
        span = end-start
        query = "SELECT COUNT(*) FROM business_stats_seen WHERE account_id=? AND chat_id=? AND detailed=1 AND sent_at>=? AND sent_at<?"
        current = self.db.execute(query,(account_id,chat_id,start,now+1)).fetchone()[0]
        previous = self.db.execute(query,(account_id,chat_id,start-span,now+1-span)).fetchone()[0]
        return (f'{previous} → {current}; процент не рассчитан (база 0)' if not previous else
                f'{previous} → {current} ({(current-previous)*100/previous:+.1f}%)')

    def first_seen_chats(self, owner_id, account_id, boundary):
        accounts = self.check_account(owner_id,account_id)
        if not accounts: return 0
        return self.db.execute('SELECT COUNT(*) FROM business_chat_stats WHERE owner_id=? AND account_id IN ('+
                               ','.join('?' for _ in accounts)+') AND first_observed_at>=?',
                               (owner_id,*accounts,boundary)).fetchone()[0]

    def update_first_observed(self, account_id, chat_id, stamp):
        self.db.execute('UPDATE business_chat_stats SET first_observed_at=CASE WHEN first_observed_at IS NULL '
                        'THEN ? ELSE MIN(first_observed_at,?) END WHERE account_id=? AND chat_id=?',
                        (stamp,stamp,account_id,chat_id))

    def _daily_add(self, account_id, chat_id, day, values, sign=1):
        pending = getattr(self, '_import_metrics', None)
        if pending is not None:
            metrics = pending.setdefault((account_id,chat_id,day),Counter())
            for key,value in values.items():
                metrics[key] = max(metrics[key],value) if key.endswith('_max') else metrics[key]+sign*value
            return
        row = self.db.execute("SELECT metrics FROM business_stats_daily WHERE account_id=? AND chat_id=? AND day=?",
                              (account_id, chat_id, day)).fetchone()
        metrics = Counter(json.loads(row[0]) if row else {})
        for key, value in values.items():
            metrics[key] = max(metrics[key],value) if key.endswith('_max') else metrics[key]+sign*value
            if not metrics[key]:
                del metrics[key]
        self.db.execute("INSERT INTO business_stats_daily VALUES(?,?,?,?) ON CONFLICT(account_id,chat_id,day) "
                        "DO UPDATE SET metrics=excluded.metrics", (account_id, chat_id, day, json.dumps(metrics)))
        row = self.db.execute('SELECT metrics FROM business_stats_chat_metrics WHERE account_id=? AND chat_id=?',
                              (account_id,chat_id)).fetchone()
        combined = Counter(json.loads(row[0]) if row else {})
        for key,value in values.items():
            combined[key] = max(combined[key],value) if key.endswith('_max') else combined[key]+sign*value
            if not combined[key]: del combined[key]
        self.db.execute('INSERT INTO business_stats_chat_metrics VALUES(?,?,?) ON CONFLICT(account_id,chat_id) '
                        'DO UPDATE SET metrics=excluded.metrics',(account_id,chat_id,json.dumps(combined)))

    def _link(self, previous, current, sign=1):
        if not previous or not current:
            return
        gap = current["sent_at"] - previous["sent_at"]
        side = current["side"]
        values = {}
        if gap > PAUSE:
            values[f"{side}_starts"] = 1
        if previous["side"] != side:
            values[f"{'long' if gap > PAUSE else 'reply'}_{side}_{gap}"] = 1
        if values:
            self._daily_add(current["account_id"], current["chat_id"], day_of(current["sent_at"]), values, sign)

    def _neighbors(self, account_id, chat_id, stamp, connection_id, message_id):
        args = (account_id, chat_id, stamp, connection_id, message_id)
        base = "SELECT * FROM business_stats_seen WHERE detailed=1 AND account_id=? AND chat_id=? AND "
        before = self.db.execute(base + "(sent_at,connection_id,message_id)<(?,?,?) "
                                 "ORDER BY sent_at DESC,connection_id DESC,message_id DESC LIMIT 1", args).fetchone()
        after = self.db.execute(base + "(sent_at,connection_id,message_id)>(?,?,?) "
                                "ORDER BY sent_at,connection_id,message_id LIMIT 1", args).fetchone()
        return before, after

    @staticmethod
    def fingerprint(record):
        # A digest suffices for event deduplication; never store or log message text here.
        return record['_stats_metrics']['fingerprint'] if '_stats_metrics' in record else content_fingerprint(record)

    def stats_source(self, record):
        cipher = getattr(self, 'message_cipher', None)
        return cipher.stats_record(record) if cipher else record

    def statistics_connection(self, connection_id, account_id, chat_id, message_id):
        imported = self.db.execute('SELECT connection_id FROM business_stats_imported '
            'WHERE account_id=? AND chat_id=? AND message_id=?', (account_id,chat_id,message_id)).fetchone()
        return imported[0] if imported else connection_id

    def record_details(self, connection_id, account_id, message_id, record):
        from .repository import STOP_WORDS
        key = (connection_id, record["chat_id"], message_id)
        row = self.db.execute("SELECT * FROM business_stats_seen WHERE connection_id=? AND chat_id=? AND message_id=?", key).fetchone()
        if not row or row["detailed"]:
            return False
        record = self.stats_source(record)
        chars, words = text_metrics(record)
        # Missing original timestamps cannot be used for time-based statistics.
        if not record.get("sent_at") or record.get("sender_id") is None:
            return False
        stamp = int(record["sent_at"])
        side = "o" if record["sender_id"] == account_id else "p"
        kind = record.get("type") or "unknown"
        metadata = record.get("metadata") or {}
        duration = metadata.get("duration") if isinstance(metadata, dict) else None
        duration = int(duration) if isinstance(duration, (int, float)) and duration >= 0 else None
        before, after = self._neighbors(account_id, record["chat_id"], stamp, connection_id, message_id)
        self._link(before, after, -1)
        self.db.execute("UPDATE business_stats_seen SET account_id=?,sent_at=?,side=?,chars=?,kind=?,duration=?,"
                        "fingerprint=?,detailed=1 WHERE connection_id=? AND chat_id=? AND message_id=?",
                        (account_id, stamp, side, chars, kind, duration, self.fingerprint(record), *key))
        current = dict(account_id=account_id, chat_id=record["chat_id"], sent_at=stamp, side=side)
        self._link(before, current)
        self._link(current, after)
        dt = datetime.fromtimestamp(stamp, MOSCOW)
        values = {f"{side}_n": 1, f"{side}_chars": chars, f"{side}_max": chars, f"h_{dt.hour}": 1}
        if kind != "text":
            values[f"{side}_media_{kind}"] = 1
        if kind in ("voice", "video_note"):
            values[f"{side}_{kind}_{'missing' if duration is None else 'seconds'}"] = 1 if duration is None else duration
        self._daily_add(account_id, record["chat_id"], day_of(stamp), values)
        for word, count in words.items():
            if word in STOP_WORDS:
                continue
            self.db.execute("INSERT INTO business_stats_side_words VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(account_id,chat_id,day,side,word) DO UPDATE SET count=count+excluded.count",
                            (account_id, record["chat_id"], day_of(stamp), side, word, count))
        # A deletion can arrive before history backfill fills in the author.
        if row["deleted_at"]:
            self._daily_add(account_id, record["chat_id"], day_of(stamp),
                            {f"{side}_deleted": 1, f"delete_{side}_{max(0, row['deleted_at'] - stamp)}": 1})
        if row["edits"]:
            self._daily_add(account_id, record["chat_id"], day_of(stamp),
                            {f"{side}_edited": 1, f"{side}_edits": row["edits"]})
        return True

    def record_event(self, owner_id, account_id, connection_id, chat_id, message_id,
                     event, record=None, detected_at=None):
        connection_id = self.statistics_connection(connection_id,account_id,chat_id,message_id)
        if record:
            record = self.stats_source(record)
        self.check_account(owner_id, account_id)
        self.check_chat_owner(owner_id,account_id,chat_id)
        if event=='delete' and record:
            original=dict(record,chat_id=chat_id)
            self.record_message(connection_id,account_id,owner_id,message_id,original)
        now = int(time.time()) if detected_at is None else int(detected_at)
        key = (connection_id, chat_id, message_id)
        row = self.db.execute("SELECT * FROM business_stats_seen WHERE connection_id=? AND chat_id=? AND message_id=?", key).fetchone()
        if event == "edit":
            if record is None:
                raise ValueError("Edit requires metadata")
            digest = self.fingerprint(record)
            version = str(record.get("edit_at") or "") + ":" + digest
            if row and row["fingerprint"] == digest and not record.get("edit_at"):
                return False
        elif event == "delete":
            version = "deleted"
        else:
            raise ValueError("Unknown event")
        with self.db:
            if not self.db.execute("INSERT OR IGNORE INTO business_stats_events VALUES(?,?,?,?,?,?)",
                                   (*key, event, version, now)).rowcount:
                return False
            if not row:
                # Unknown original: preserve the event but never fabricate a sent message/author.
                self.db.execute("INSERT OR IGNORE INTO business_stats_seen(connection_id,chat_id,message_id,observed_at,account_id,counted) "
                                "VALUES(?,?,?,?,?,0)", (*key, now, account_id))
            else:
                self.db.execute('UPDATE business_stats_seen SET account_id=COALESCE(account_id,?) '
                                'WHERE connection_id=? AND chat_id=? AND message_id=?',(account_id,*key))
            if event == "delete":
                self.db.execute("UPDATE business_stats_seen SET deleted_at=COALESCE(deleted_at,?) "
                                "WHERE connection_id=? AND chat_id=? AND message_id=?", (now, *key))
                if row and row["detailed"] and not row["deleted_at"]:
                    self._daily_add(account_id, chat_id, day_of(row["sent_at"]),
                                    {f"{row['side']}_deleted": 1, f"delete_{row['side']}_{max(0, now-row['sent_at'])}": 1})
            else:
                self.db.execute("UPDATE business_stats_seen SET edits=edits+1,fingerprint=? "
                                "WHERE connection_id=? AND chat_id=? AND message_id=?", (digest, *key))
                if row and row["detailed"]:
                    self._daily_add(account_id, chat_id, day_of(row["sent_at"]),
                                    {f"{row['side']}_edited": int(not row["edits"]), f"{row['side']}_edits": 1})
        return True

    def daily_rows(self, owner_id, account_id=None, chat_id=None, period="all", now=None):
        accounts = self.check_account(owner_id, account_id)
        if not accounts:
            return []
        start, end = period_bounds(period, now)
        where = [f"d.account_id IN ({','.join('?' for _ in accounts)})", "s.owner_id=?", "d.day<?"]
        args = [*accounts, owner_id, day_of(end)]
        if start is not None:
            where.append("d.day>=?"); args.append(day_of(start))
        if chat_id is not None:
            where.append("d.chat_id=?"); args.append(chat_id)
        return self.db.execute("SELECT d.* FROM business_stats_daily d JOIN business_chat_stats s "
                               "ON s.account_id=d.account_id AND s.chat_id=d.chat_id WHERE " + " AND ".join(where) +
                               " ORDER BY d.day", args).fetchall()

    def analytics(self, owner_id, account_id, chat_id, period="all", now=None, side="both", page=0, word_limit=5):
        self.check_account(owner_id, account_id)
        legacy = self.stat(owner_id, account_id, chat_id)
        if not legacy:
            return None
        rows = self.daily_rows(owner_id, account_id, chat_id, period, now)
        metrics = Counter()
        days = Counter()
        for row in rows:
            item = json.loads(row["metrics"])
            for k,v in item.items():
                metrics[k] = max(metrics[k],v) if k.endswith('_max') else metrics[k]+v
            days[row["day"]] += item.get("o_n", 0) + item.get("p_n", 0)
        start, end = period_bounds(period, now)
        where = "account_id=? AND chat_id=? AND detailed=1 AND sent_at<?"
        args = [account_id, chat_id, end]
        if start is not None:
            where += " AND sent_at>=?"; args.append(start)
        lengths = {s: metrics[f'{s}_max'] for s in ('o','p')}
        last = self.db.execute("SELECT side,sent_at FROM business_stats_seen WHERE " + where +
                               " AND deleted_at IS NULL ORDER BY sent_at DESC,message_id DESC LIMIT 1", args).fetchone()
        unknown = self.db.execute("SELECT COUNT(*) FROM business_stats_seen WHERE account_id=? AND chat_id=? AND detailed=0",
                                  (account_id, chat_id)).fetchone()[0]
        clauses = ["account_id=?", "chat_id=?", "day<?"]
        word_args = [account_id, chat_id, day_of(end)]
        if start is not None:
            clauses.append("day>=?"); word_args.append(day_of(start))
        if side in ("o", "p"):
            clauses.append("side=?"); word_args.append(side)
        words = self.db.execute("SELECT word,SUM(count) AS count FROM business_stats_side_words WHERE " +
                                " AND ".join(clauses) + " GROUP BY word ORDER BY count DESC,word LIMIT ? OFFSET ?",
                                (*word_args, word_limit, max(0, page)*word_limit)).fetchall()
        word_count = self.db.execute("SELECT COUNT(DISTINCT word) FROM business_stats_side_words WHERE " +
                                     " AND ".join(clauses), word_args).fetchone()[0]
        if period == "all" and side == "both":
            words = self.db.execute("SELECT word,count FROM business_stats_words WHERE account_id=? AND chat_id=? "
                                    "ORDER BY count DESC,word LIMIT ? OFFSET ?", (account_id, chat_id, word_limit, max(0,page)*word_limit)).fetchall()
            word_count = self.db.execute("SELECT COUNT(*) FROM business_stats_words WHERE account_id=? AND chat_id=?", (account_id,chat_id)).fetchone()[0]
        progress = dict(self.db.execute("SELECT name,value FROM business_stats_progress"))
        available = self.db.execute("SELECT MIN(sent_at) FROM business_stats_seen WHERE account_id=? AND chat_id=? AND detailed=1", (account_id,chat_id)).fetchone()[0]
        activity_days = [(d,n) for d,n in sorted(days.items()) if n]
        if start is not None:
            activity_days=[]
            windows=observation_windows(self.db,[account_id],progress,int(time.time()) if now is None else now)
            for stamp in range(start,end,86400):
                day=day_of(stamp)
                activity_days.append((day,days[day] if days[day] or day_is_known(windows,stamp,int(time.time()) if now is None else now) else None))
        return dict(legacy=legacy, metrics=metrics, days=days, lengths=lengths, last=last,
                    unknown=unknown, words=words, word_count=word_count, progress=progress, available=available,
                    period=period, start=start, end=end, activity_days=activity_days)

    def filtered_chats(self, owner_id, account_id=None, period="all", now=None):
        accounts = self.check_account(owner_id, account_id)
        if not accounts:
            return []
        rows = [dict(r) for r in self.db.execute("SELECT s.*,m.metrics AS cached_metrics FROM business_chat_stats s "
            "LEFT JOIN business_stats_chat_metrics m ON m.account_id=s.account_id AND m.chat_id=s.chat_id "
            "WHERE s.owner_id=? AND s.account_id IN (" + ",".join("?" for _ in accounts) + ")", (owner_id,*accounts))]
        metrics = {}
        if period=='all':
            for r in rows:
                metrics[(r['account_id'],r['chat_id'])] = Counter(json.loads(r.pop('cached_metrics') or '{}'))
        else:
            for daily in self.daily_rows(owner_id, account_id, period=period, now=now):
                metrics.setdefault((daily["account_id"],daily["chat_id"]), Counter()).update(json.loads(daily["metrics"]))
        for row in rows:
            m = metrics.get((row["account_id"],row["chat_id"]), Counter())
            row["metrics"] = m
            if period != "all":
                row.update(total_messages=m["o_n"]+m["p_n"], owner_messages=m["o_n"], peer_messages=m["p_n"])
        return [r for r in rows if period == "all" or r["total_messages"]]
