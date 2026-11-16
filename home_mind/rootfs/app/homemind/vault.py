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
            key = bytes.fromhex(f.read().strip())
        if len(key) != 32:
            raise ValueError(f"{path} doesn't hold a 256-bit key (64 hex digits)")
        return key
    if not create:
        return None
    key = secrets.token_bytes(32)
    # written in full and on disk before anything is sealed with it: a key file lost or left empty by a power cut
    # right after the first seal would leave every secret unreadable
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(key.hex() + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)          # fails if another process made one meanwhile: never replace a key
    finally:
        os.unlink(tmp)
    try:
        dfd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
    return key


def opens(key, store):
    """True if key opens a sealed note (or none is sealed): another key would seal new secrets that this one can't
    read, and leave the old ones locked."""
    rows = store.db.execute("SELECT id, secret FROM items WHERE secret IS NOT NULL ORDER BY id DESC").fetchall()
    for r in rows:
        try:
            open_(key, r[1], aad=str(r[0]).encode())
            return True
        except Exception:  # noqa: BLE001 - not this one
            continue
    return not rows


def setup(store, path, enabled):
    """Option encrypt_secrets (#66): seal secret notes with the key in path; turned off, open them. A missing key or
    one that opens none of the sealed notes is reported and not used (a new key would strand the sealed notes: edits
    of a locked note would keep only its masked copy)."""
    status = {"enabled": False}
    if not available():
        status["error"] = "the cryptography library isn't installed"
        return status
    sealed = store.db.execute("SELECT COUNT(*) FROM items WHERE secret IS NOT NULL").fetchone()[0]
    try:
        key = load_key(path, create=bool(enabled) and not sealed) if enabled or os.path.exists(path) else None
    except (OSError, ValueError) as e:
        key, status["error"] = None, f"vault.key unreadable: {e}"
    if key is not None and not opens(key, store):
        key, status["error"] = None, (f"{path} doesn't open the {sealed} sealed notes; put back the key they were "
                                      "sealed with (GET /api/vault/key showed it)")
    elif key is None and sealed and enabled and "error" not in status:
        status["error"] = f"{path} is missing: {sealed} sealed notes stay locked until the key is put back"
    if key is not None and enabled:
        store.vault_key = key
        status.update(enabled=True, key=fingerprint(key), sealed_now=store.seal_all())
    elif key is not None:
        store.vault_key = key
        n = store.unseal_all()         # turned off: back to plain text, so nothing is stranded
        store.vault_key = None
        status["opened_now"] = n
    else:
        store.vault_key = None
    status["sealed"] = store.db.execute("SELECT COUNT(*) FROM items WHERE secret IS NOT NULL").fetchone()[0]
    return status


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
