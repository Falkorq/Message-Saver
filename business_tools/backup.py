"""Consistent SQLite snapshots, including committed WAL pages."""
import sqlite3
import os
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def backup_database(db, tag="statistics-v2", cipher=None, directory=None):
    path = next((row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"), "")
    if not path:
        return None
    db.commit()
    source = Path(path)
    directory = Path(directory) if directory is not None else source.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"{source.stem}-{tag}-{stamp}.sqlite3"
    try:
        fd=os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.close(fd)
        with closing(sqlite3.connect(target)) as copy:
            db.backup(copy, pages=256)
            if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite backup verification failed")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if cipher is not None:
        from encrypted_backup import encrypt_backup
        encrypted = target.with_suffix(target.suffix+'.aes')
        try:
            encrypt_backup(target, encrypted, cipher)
        finally:
            target.unlink(missing_ok=True)
        return encrypted
    return target
