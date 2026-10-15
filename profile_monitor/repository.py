import time

from .models import ProfileSnapshot


class ProfileRepository:
    def __init__(self, db):
        self.db = db

    def migrate(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS profile_watchers (
            owner_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            bio TEXT,
            photo_id TEXT,
            access_hash INTEGER,
            last_checked_at INTEGER,
            added_at INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_error TEXT,
            PRIMARY KEY (owner_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS profile_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            batch_id TEXT NOT NULL,
            field TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS profile_changes_owner_time
            ON profile_changes(owner_id, created_at DESC, id DESC);
        CREATE TABLE IF NOT EXISTS profile_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            photo_id TEXT,
            local_path TEXT,
            created_at INTEGER NOT NULL,
            UNIQUE(owner_id, user_id, photo_id)
        );
        CREATE TABLE IF NOT EXISTS profile_settings (
            owner_id INTEGER PRIMARY KEY,
            interval_seconds INTEGER NOT NULL DEFAULT 900,
            notifications INTEGER NOT NULL DEFAULT 1,
            keep_photos INTEGER NOT NULL DEFAULT 1,
            notification_format TEXT NOT NULL DEFAULT 'compact'
        );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(profile_watchers)")}
        if "access_hash" not in columns:
            self.db.execute("ALTER TABLE profile_watchers ADD COLUMN access_hash INTEGER")
        # БД старых версий могла создать watchers без строки настроек; без неё INNER JOIN
        # в due() навсегда исключает владельца из проверки.
        self.db.execute(
            "INSERT OR IGNORE INTO profile_settings(owner_id) "
            "SELECT DISTINCT owner_id FROM profile_watchers"
        )
        self.db.commit()

    @staticmethod
    def snapshot(row):
        return ProfileSnapshot(
            row["user_id"], row["username"], row["first_name"], row["last_name"],
            row["bio"], row["photo_id"],
        )

    def add(self, owner_id, snapshot, photo_path=None, access_hash=None):
        now = int(time.time())
        self.db.execute(
            "INSERT INTO profile_watchers(owner_id,user_id,username,first_name,last_name,bio,photo_id,access_hash,last_checked_at,added_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,user_id) DO UPDATE SET enabled=1, username=excluded.username, "
            "first_name=excluded.first_name,last_name=excluded.last_name,bio=excluded.bio,photo_id=excluded.photo_id,"
            "access_hash=COALESCE(excluded.access_hash,profile_watchers.access_hash),"
            "last_checked_at=excluded.last_checked_at,added_at=excluded.added_at,last_error=NULL",
            (owner_id, snapshot.user_id, snapshot.username, snapshot.first_name, snapshot.last_name,
             snapshot.bio, snapshot.photo_id, access_hash, now, now),
        )
        if snapshot.photo_id:
            self.add_photo(owner_id, snapshot.user_id, snapshot.photo_id, photo_path, now)
        self.ensure_settings(owner_id)
        self.db.commit()

    def ensure_settings(self, owner_id):
        self.db.execute("INSERT OR IGNORE INTO profile_settings(owner_id) VALUES(?)", (owner_id,))

    def list(self, owner_id, enabled_only=True):
        suffix = " AND enabled=1" if enabled_only else ""
        return self.db.execute(
            "SELECT * FROM profile_watchers WHERE owner_id=?" + suffix + " ORDER BY COALESCE(username,first_name,CAST(user_id AS TEXT))",
            (owner_id,),
        ).fetchall()

    def get(self, owner_id, user_id):
        return self.db.execute(
            "SELECT * FROM profile_watchers WHERE owner_id=? AND user_id=?", (owner_id, user_id)
        ).fetchone()

    def find(self, owner_id, target):
        text = str(target).strip().lstrip("@").lower()
        if text.isdigit():
            return self.get(owner_id, int(text))
        return self.db.execute(
            "SELECT * FROM profile_watchers WHERE owner_id=? AND lower(username)=? AND enabled=1",
            (owner_id, text),
        ).fetchone()

    def disable(self, owner_id, user_id):
        self.db.execute(
            "UPDATE profile_watchers SET enabled=0 WHERE owner_id=? AND user_id=?", (owner_id, user_id)
        )
        self.db.commit()

    def set_access_hash(self, owner_id, user_id, access_hash, username=None):
        if access_hash is None and username is None:
            return
        self.db.execute(
            "UPDATE profile_watchers SET access_hash=COALESCE(?,access_hash),"
            "username=COALESCE(username,?),last_error=NULL WHERE owner_id=? AND user_id=?",
            (access_hash, username, owner_id, user_id),
        )
        self.db.commit()

    def due(self, now):
        return self.db.execute(
            "SELECT w.* FROM profile_watchers w JOIN profile_settings s ON s.owner_id=w.owner_id "
            "WHERE w.enabled=1 AND COALESCE(w.last_checked_at,0)+s.interval_seconds<=? ORDER BY COALESCE(w.last_checked_at,0)",
            (now,),
        ).fetchall()

    def save_check(self, owner_id, old, new, changes, batch_id, photo_path=None, checked_at=None,
                   access_hash=None):
        checked_at = checked_at or int(time.time())
        with self.db:
            self.db.execute(
                "UPDATE profile_watchers SET username=?,first_name=?,last_name=?,bio=?,photo_id=?,"
                "access_hash=COALESCE(?,access_hash),last_checked_at=?,last_error=NULL "
                "WHERE owner_id=? AND user_id=?",
                (new.username, new.first_name, new.last_name, new.bio, new.photo_id,
                 access_hash, checked_at, owner_id, new.user_id),
            )
            for field, before, after in changes:
                self.db.execute(
                    "INSERT INTO profile_changes(owner_id,user_id,batch_id,field,old_value,new_value,created_at) VALUES(?,?,?,?,?,?,?)",
                    (owner_id, new.user_id, batch_id, field, before, after, checked_at),
                )
            if old.photo_id != new.photo_id:
                self.add_photo(owner_id, new.user_id, new.photo_id, photo_path, checked_at)

    def add_photo(self, owner_id, user_id, photo_id, path, created_at):
        self.db.execute(
            "INSERT OR IGNORE INTO profile_photos(owner_id,user_id,photo_id,local_path,created_at) VALUES(?,?,?,?,?)",
            (owner_id, user_id, photo_id, path, created_at),
        )

    def set_error(self, owner_id, user_id, error):
        self.db.execute(
            "UPDATE profile_watchers SET last_checked_at=?,last_error=? WHERE owner_id=? AND user_id=?",
            (int(time.time()), type(error).__name__, owner_id, user_id),
        )
        self.db.commit()

    def history(self, owner_id, user_id=None, limit=50, offset=0, field=None):
        clauses, args = ["owner_id=?"], [owner_id]
        if user_id is not None:
            clauses.append("user_id=?"); args.append(user_id)
        if field:
            clauses.append("field=?"); args.append(field)
        args.extend((limit, offset))
        return self.db.execute(
            "SELECT * FROM profile_changes WHERE " + " AND ".join(clauses) +
            " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", args,
        ).fetchall()

    def history_page(self, owner_id, user_id=None, page=0, batches=5):
        clauses, args = ["owner_id=?"], [owner_id]
        if user_id is not None:
            clauses.append("user_id=?"); args.append(user_id)
        where = " AND ".join(clauses)
        ids = self.db.execute(
            "SELECT batch_id,MAX(created_at) AS changed_at,MAX(id) AS last_id FROM profile_changes WHERE "
            + where + " GROUP BY batch_id ORDER BY changed_at DESC,last_id DESC LIMIT ? OFFSET ?",
            args + [batches, page * batches],
        ).fetchall()
        if not ids:
            return []
        order = {row["batch_id"]: index for index, row in enumerate(ids)}
        marks = ",".join("?" for _ in ids)
        rows = self.db.execute(
            f"SELECT * FROM profile_changes WHERE owner_id=? AND batch_id IN ({marks}) ORDER BY created_at DESC,id DESC",
            [owner_id] + [row["batch_id"] for row in ids],
        ).fetchall()
        return sorted(rows, key=lambda row: (order[row["batch_id"]], -row["id"]))

    def photos(self, owner_id, user_id):
        return self.db.execute(
            "SELECT * FROM profile_photos WHERE owner_id=? AND user_id=? ORDER BY created_at DESC,id DESC",
            (owner_id, user_id),
        ).fetchall()

    def settings(self, owner_id):
        self.ensure_settings(owner_id); self.db.commit()
        return self.db.execute("SELECT * FROM profile_settings WHERE owner_id=?", (owner_id,)).fetchone()

    def set_setting(self, owner_id, field, value):
        if field not in {"interval_seconds", "notifications", "keep_photos", "notification_format"}:
            raise ValueError("unknown setting")
        self.ensure_settings(owner_id)
        self.db.execute(f"UPDATE profile_settings SET {field}=? WHERE owner_id=?", (value, owner_id))
        self.db.commit()
