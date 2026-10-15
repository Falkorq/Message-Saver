"""Small encrypted erasure ledger. Keep its newest copy separately from DB backups."""
import json
import os
from pathlib import Path


class ErasureGuard:
    def __init__(self, path, cipher):
        self.path, self.cipher = Path(path), cipher

    def read(self):
        if not self.path.exists():
            return {}
        return json.loads(self.cipher.open(json.loads(self.path.read_text(encoding='utf-8')), 'erasure_ledger'))

    def record(self, owner, stamp):
        ledger = self.read()
        ledger[str(owner)] = max(stamp, ledger.get(str(owner), 0))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(self.cipher.seal(json.dumps(ledger), 'erasure_ledger'), stream)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        if os.name != 'nt':
            fd = os.open(self.path.parent, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)

    def apply(self, product, inbox):
        for owner, stamp in self.read().items():
            rows = product.db.execute('SELECT erased_through FROM account_state WHERE owner_id=?',(int(owner),)).fetchall()
            if any(row[0] < stamp for row in rows):
                product.erase(int(owner), inbox, stamp)
