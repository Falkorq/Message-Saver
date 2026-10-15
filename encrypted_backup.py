"""Chunked AES-256-GCM encryption for SQLite migration snapshots."""
import hashlib
import hmac
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.exceptions import InvalidTag
from message_crypto import EncryptionError

MAGIC=b'NYANDB1\x00'
HEADER_SIZE=len(MAGIC)+32+12


def backup_key(cipher):
    # Domain-separated key, independent of message nonces/comparison tags.
    return hmac.digest(cipher.compare_key,b'nyan/sqlite-backup/v1','sha256')


def encrypt_backup(source, target, cipher):
    header=MAGIC+cipher.kid.encode()+os.urandom(12)
    enc=Cipher(algorithms.AES(backup_key(cipher)),modes.GCM(header[-12:])).encryptor()
    enc.authenticate_additional_data(header)
    digest=hashlib.sha256()
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with open(source,'rb') as src, os.fdopen(fd,'wb') as out:
            out.write(header)
            while chunk:=src.read(1024*1024):
                digest.update(chunk);out.write(enc.update(chunk))
            out.write(enc.finalize());out.write(enc.tag);out.flush();os.fsync(out.fileno())
        if verify_backup(target,cipher)!=digest.digest():
            raise EncryptionError('Проверка резервной копии не пройдена.')
    except BaseException:
        Path(target).unlink(missing_ok=True)
        raise


def _decrypt_chunks(path,cipher):
    with open(path,'rb') as src:
        size=os.fstat(src.fileno()).st_size
        header=src.read(HEADER_SIZE)
        if (len(header)!=HEADER_SIZE or not header.startswith(MAGIC)
                or header[len(MAGIC):len(MAGIC)+32]!=cipher.kid.encode() or size<HEADER_SIZE+16):
            raise EncryptionError('Неверный ключ или формат резервной копии.')
        src.seek(-16,os.SEEK_END);tag=src.read(16);src.seek(HEADER_SIZE)
        dec=Cipher(algorithms.AES(backup_key(cipher)),modes.GCM(header[-12:],tag)).decryptor()
        dec.authenticate_additional_data(header)
        remaining=size-HEADER_SIZE-16
        try:
            while remaining:
                chunk=src.read(min(1024*1024,remaining))
                if not chunk:raise EncryptionError('Резервная копия обрезана.')
                remaining-=len(chunk);yield dec.update(chunk)
            yield dec.finalize()
        except InvalidTag:
            raise EncryptionError('Нарушена целостность резервной копии.') from None


def verify_backup(path,cipher):
    digest=hashlib.sha256()
    for chunk in _decrypt_chunks(path,cipher):digest.update(chunk)
    return digest.digest()


def restore_backup(path,target,cipher):
    # Exclusively create a new file; delete partial plaintext if authentication fails.
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as out:
            for chunk in _decrypt_chunks(path,cipher):out.write(chunk)
            out.flush();os.fsync(out.fileno())
    except BaseException:
        Path(target).unlink(missing_ok=True)
        raise
