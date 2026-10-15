"""Opt-in daily encrypted database delivery to an operator's private Telegram chat."""
import argparse
import asyncio
from contextlib import closing
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import time
import zipfile

from business_tools.backup import backup_database
from erasure_guard import ErasureGuard

PART_BYTES = 45 * 1024 * 1024
log = logging.getLogger(__name__)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_bundle(db_path, cipher, directory, part_bytes=PART_BYTES):
    """Only these two encrypted files are allowed into the archive; no directory scans."""
    if part_bytes < 1:
        raise ValueError('Part size must be positive')
    db_path, directory = Path(db_path).resolve(), Path(directory)
    with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=30)) as db:
        snapshot = backup_database(db, 'offsite', cipher=cipher, directory=directory)
    guard = ErasureGuard(db_path.with_name('erasure-guard.aes'), cipher)
    # Missing ledgers must not silently turn into an apparently recoverable backup.
    ledger = guard.path.read_bytes()
    cipher.open(json.loads(ledger), 'erasure_ledger')
    bundle = directory / (snapshot.stem + '.zip')
    with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_STORED) as archive:
        archive.write(snapshot, 'database.sqlite3.aes')
        archive.writestr('erasure-guard.aes', ledger)
    snapshot.unlink()
    if bundle.stat().st_size <= part_bytes:
        parts = [bundle]
    else:
        parts = []
        with bundle.open('rb') as source:
            while source.tell() < bundle.stat().st_size:
                part = directory / f'{bundle.name}.{len(parts)+1:03d}'
                with part.open('wb') as output:
                    remaining = part_bytes
                    while remaining and (chunk := source.read(min(remaining, 1024 * 1024))):
                        output.write(chunk)
                        remaining -= len(chunk)
                parts.append(part)
        bundle.unlink()
    manifest = directory / (bundle.name + '.manifest.json')
    manifest.write_text(json.dumps({'format': 1, 'archive': bundle.name, 'parts': [
        {'name': part.name, 'bytes': part.stat().st_size,
         'sha256': file_hash(part)}
        for part in parts]}, indent=2), encoding='utf-8')
    return parts, manifest


def join_bundle(manifest_path, target):
    """Join downloaded parts in manifest order, checking size/hash before restoration."""
    manifest_path, target = Path(manifest_path), Path(target)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    try:
        with target.open('xb') as output:
            for item in manifest['parts']:
                name = item['name']
                if Path(name).name != name or '/' in name or '\\' in name:
                    raise ValueError('Invalid part name')
                digest, size = hashlib.sha256(), 0
                with (manifest_path.parent / name).open('rb') as source:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk); digest.update(chunk); size += len(chunk)
                if size != item['bytes'] or digest.hexdigest() != item['sha256']:
                    raise ValueError('Backup part is incomplete or damaged')
    except FileExistsError:
        raise
    except BaseException:
        target.unlink(missing_ok=True)
        raise


async def send_bundle(bot, recipient, parts, manifest):
    for index, path in enumerate([*parts, manifest], 1):
        # Reopen on every retry: Telegram may already have consumed the stream.
        for attempt in range(5):
            try:
                with path.open('rb') as document:
                    await bot.send_document(recipient, document, timeout=180,
                        caption=f'Резервная копия Nyan · файл {index}/{len(parts)+1}. '
                                'Зашифровано. Без .env, токена и ключа. '
                                'Сохраните все части и manifest.json.')
                break
            except Exception as exc:
                if getattr(exc, 'error_code', None) != 429 or attempt == 4:
                    raise
                await asyncio.sleep(max(1, int((exc.result_json or {}).get(
                    'parameters', {}).get('retry_after', 1))))
        await asyncio.sleep(1)


async def deliver_once(bot, recipient, db_path, cipher):
    chat = await bot.get_chat(recipient)
    if chat.type != 'private':
        raise ValueError('Backups require a private operator chat')
    # All SQLite/crypto/file work stays off the bot's event loop. On shutdown wait
    # for the worker before cleaning its temporary directory.
    with tempfile.TemporaryDirectory(prefix='nyan-offsite-') as directory:
        task = asyncio.create_task(asyncio.to_thread(prepare_bundle, db_path, cipher, directory))
        try:
            parts, manifest = await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise
        await send_bundle(bot, recipient, parts, manifest)


async def backup_loop(bot, db_path, cipher, recipient):
    if not recipient:
        return
    try:
        recipient = int(recipient)
        if recipient <= 0:
            raise ValueError()
    except ValueError:
        log.error('BACKUP_CHAT_ID должен быть положительным ID личного чата; отправка отключена')
        return
    state_path = Path(db_path).with_name('offsite-backup-state.json')
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
        last = float(state['sent_at']) if state['recipient'] == recipient else 0
    except (OSError, ValueError, KeyError, TypeError):
        last = 0
    await asyncio.sleep(60)
    while True:
        if time.time() - last >= 86400:
            try:
                await deliver_once(bot, recipient, db_path, cipher)
                last = time.time()
                temporary = state_path.with_suffix('.tmp')
                temporary.write_text(json.dumps({'recipient': recipient, 'sent_at': last}), encoding='utf-8')
                temporary.replace(state_path)
                log.info('Зашифрованная резервная копия доставлена в личный чат оператора')
            except Exception as exc:
                # Never log request URLs, credentials or Telegram response text.
                log.error('Внешняя резервная копия не доставлена (%s); повтор через час', type(exc).__name__)
                await asyncio.sleep(3600)
                continue
        await asyncio.sleep(60)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Собрать скачанные части резервной копии в ZIP')
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--target', required=True, help='Новый ZIP-файл (существующий не перезаписывается)')
    args = parser.parse_args()
    join_bundle(args.manifest, args.target)
    print('ZIP собран и проверен. Распакуйте и выполните storage_admin.py restore.')
