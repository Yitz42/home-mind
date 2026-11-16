"""Encryption for secrets at rest (#66) and for off-site backups (#60).

The whole database isn't encrypted (SQLCipher isn't in the add-on image). With option encrypt_secrets, each note
marked sensitive is stored as a masked copy that search can still read ("The garage code is ••••") plus the real
text encrypted with AES-256-GCM in column secret. The key lives in /data/vault.key, which Home Assistant backups
leave out, so a backup alone never holds the codes in the clear. Lose the key and the secrets are gone: copy it
somewhere safe (GET /api/vault/key, admin).

Backups sent off-site are encrypted the same way, with a key from option backup_passphrase (scrypt), so they can be
restored on a new machine with just the passphrase (tools/restore_backup.py).
"""
import base64
import hashlib
import os
import secrets

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
except ImportError:  # pragma: no cover
    AESGCM = Scrypt = None

PREFIX = "hmv1:"


def available():
    return AESGCM is not None


def load_key(path, create=True):
    if os.path.exists(path):
        with open(path) as f:
            return bytes.fromhex(f.read().strip())
    if not create:
        return None
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.hex() + "\n")
    return key


def fingerprint(key):
    return hashlib.sha256(key).hexdigest()[:12]


def seal(key, text, aad=b""):
    if text is None:
        return None
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, text.encode() if isinstance(text, str) else text, aad)
    return PREFIX + base64.b64encode(nonce + ct).decode()


def open_(key, blob, aad=b"", as_text=True):
    if not blob or not str(blob).startswith(PREFIX):
        return blob
    raw = base64.b64decode(blob[len(PREFIX):])
    out = AESGCM(key).decrypt(raw[:12], raw[12:], aad)
    return out.decode() if as_text else out


def passphrase_key(passphrase, salt):
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode())


def seal_file(passphrase, data):
    """Bytes for an off-site backup: magic, salt, nonce, ciphertext."""
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    return b"HMBK1" + salt + nonce + AESGCM(passphrase_key(passphrase, salt)).encrypt(nonce, data, b"home-mind-backup")


def open_file(passphrase, blob):
    if blob[:5] != b"HMBK1":
        raise ValueError("not a Home Mind backup")
    salt, nonce = blob[5:21], blob[21:33]
    return AESGCM(passphrase_key(passphrase, salt)).decrypt(nonce, blob[33:], b"home-mind-backup")
