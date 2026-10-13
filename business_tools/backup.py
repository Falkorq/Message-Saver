"""Consistent SQLite snapshots, including committed WAL pages."""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def backup_database(db, tag="statistics-v2"):
    path = next((row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"), "")
    if not path:
        return None
    db.commit()
    source = Path(path)
    directory = source.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"{source.stem}-{tag}-{stamp}.sqlite3"
    try:
        with closing(sqlite3.connect(target)) as copy:
            db.backup(copy, pages=256)
            if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite backup verification failed")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target
