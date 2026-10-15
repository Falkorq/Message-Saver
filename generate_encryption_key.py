"""Write a new secret directly to .env once, never to the console."""
import argparse
import base64
import os
import re
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import dotenv_values
from message_crypto import ENV_KEY, MessageCipher, EncryptionError


def generate_key(path):
    path=Path(path).resolve()
    if not path.is_file():
        raise EncryptionError('Сначала создайте .env с настройками бота.')
    current=dotenv_values(path,interpolate=False).get(ENV_KEY)
    if current:
        MessageCipher(current)
        return False
    source=path.read_text(encoding='utf-8')
    key=base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode()
    line=f'{ENV_KEY}={key}'
    pattern=rf'(?m)^\s*(?:export\s+)?{ENV_KEY}\s*=.*$'
    source=re.sub(pattern,line,source) if re.search(pattern,source) else source.rstrip('\n')+'\n'+line+'\n'
    fd,temp=tempfile.mkstemp(prefix='.env-key-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
            stream.write(source);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
        if hasattr(os,'O_DIRECTORY'):
            directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)
    finally:
        Path(temp).unlink(missing_ok=True)
    return True


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',default=str(Path(__file__).with_name('.env')))
    args=parser.parse_args()
    try:
        print('Ключ сохранён в .env.' if generate_key(args.env_file) else 'Ключ уже существует и не изменён.')
    except (EncryptionError,OSError) as exc:
        raise SystemExit(str(exc)) from None
