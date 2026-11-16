"""Off-site backups (#60): a snapshot of the memory every few hours (option backup_every_hours) when it has changed,
encrypted with option backup_passphrase (AES-256-GCM, key by scrypt), sent to one of:

  folder   a path the add-on can write, e.g. /media/home-mind or /share/backups (a NAS mounted in Home Assistant)
  webdav   a URL (Nextcloud, a Hetzner storage box, any WebDAV server), with backup_user and backup_password
  s3       an S3-compatible bucket URL (https://s3.REGION.amazonaws.com/BUCKET, Backblaze B2, MinIO, Cloudflare R2)
           with backup_user (access key), backup_password (secret key) and backup_region

The newest backup_keep snapshots are kept; older ones are deleted. Restore one on any machine with
tools/restore_backup.py and the passphrase. Off-site targets need a passphrase; a folder can take plain copies.
"""
import datetime
import gzip
import hashlib
import hmac
import logging
import os
import sqlite3
import tempfile
import time
import urllib.parse

from . import vault

log = logging.getLogger("homemind.offsite")


def snapshot(store):
    """A consistent copy of the database (SQLite's backup API), gzipped."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "mind.db")
        dst = sqlite3.connect(p)
        with store.lock:
            store.db.backup(dst)
        dst.close()
        with open(p, "rb") as f:
            return gzip.compress(f.read(), 6)


def _sigv4(method, url, region, access, secret, payload, now=None):
    """Headers for an S3 request signed with AWS Signature Version 4."""
    u = urllib.parse.urlparse(url)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    ph = hashlib.sha256(payload).hexdigest()
    headers = {"host": u.netloc, "x-amz-content-sha256": ph, "x-amz-date": amz}
    signed = ";".join(sorted(headers))
    canon = "\n".join([method, urllib.parse.quote(u.path or "/"), u.query or "",
                       "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed, ph])
    scope = f"{day}/{region}/s3/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canon.encode()).hexdigest()])
    k = ("AWS4" + secret).encode()
    for part in (day, region, "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return headers


class Offsite:
    def __init__(self, store, opts, session=None):
        self.store, self.opts, self._session = store, opts, session     # a session, or a function returning one
        self.last = store.kv_get("offsite_last") or {}

    @property
    def session(self):
        return self._session() if callable(self._session) else self._session

    @property
    def target(self):
        return (self.opts.get("backup_target") or "").strip()

    def _stamp(self):
        r = self.store.db.execute("SELECT COUNT(*), MAX(updated) FROM items").fetchone()
        return f"{r[0]}:{r[1]}"

    def due(self, now=None):
        if not self.target:
            return False
        now = now or time.time()
        hours = float(self.opts.get("backup_every_hours") or 6)
        return now - (self.last.get("at") or 0) >= hours * 3600 and self._stamp() != self.last.get("stamp")

    async def run(self, now=None):
        t = self.target
        if t not in ("folder", "webdav", "s3"):
            return {"error": f"backup_target must be folder, webdav or s3, not {t!r}"}
        pw = self.opts.get("backup_passphrase") or ""
        if t != "folder" and not pw:
            return {"error": "set backup_passphrase: off-site backups are always encrypted"}
        data = snapshot(self.store)
        blob = vault.seal_file(pw, data) if pw else data
        name = time.strftime("home-mind-%Y%m%d-%H%M%S", time.localtime(now or time.time())) + (".hmbk" if pw else ".db.gz")
        await self._put(name, blob)
        kept = (self.store.kv_get("offsite_files") or []) + [name]
        keep = int(self.opts.get("backup_keep") or 14)
        left = []
        for old in kept[:-keep]:
            try:
                await self._delete(old)
            except Exception as e:  # noqa: BLE001 - a stale file is better than a failed backup
                log.warning("off-site prune %s: %s", old, e)
                left.append(old)            # remembered, so the next run tries again instead of leaking it
        self.store.kv_set("offsite_files", left[-50:] + kept[-keep:])
        self.last = {"at": now or time.time(), "stamp": self._stamp(), "file": name, "bytes": len(blob),
                     "target": t, "encrypted": bool(pw)}
        self.store.kv_set("offsite_last", self.last)
        self.store.log("offsite", None, file=name, bytes=len(blob), target=t)
        return self.last

    def _url(self, name):
        return self.opts["backup_url"].rstrip("/") + "/" + name

    async def _put(self, name, blob):
        t = self.target
        if t == "folder":
            d = self.opts.get("backup_url") or "/media/home-mind"
            os.makedirs(d, exist_ok=True)
            tmp = os.path.join(d, name + ".part")
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, os.path.join(d, name))
            return
        url = self._url(name)
        if t == "webdav":
            import aiohttp
            auth = aiohttp.BasicAuth(self.opts.get("backup_user") or "", self.opts.get("backup_password") or "")
            async with self.session.put(url, data=blob, auth=auth) as r:
                if r.status not in (200, 201, 204):
                    raise RuntimeError(f"WebDAV PUT {r.status}")
            return
        h = _sigv4("PUT", url, self.opts.get("backup_region") or "us-east-1", self.opts.get("backup_user") or "",
                   self.opts.get("backup_password") or "", blob)
        async with self.session.put(url, data=blob, headers=h) as r:
            if r.status not in (200, 201, 204):
                raise RuntimeError(f"S3 PUT {r.status}: {(await r.text())[:200]}")

    async def _delete(self, name):
        t = self.target
        if t == "folder":
            p = os.path.join(self.opts.get("backup_url") or "/media/home-mind", name)
            if os.path.exists(p):
                os.remove(p)
            return
        url = self._url(name)
        if t == "webdav":
            import aiohttp
            auth = aiohttp.BasicAuth(self.opts.get("backup_user") or "", self.opts.get("backup_password") or "")
            async with self.session.delete(url, auth=auth) as r:
                if r.status not in (200, 202, 204, 404):        # 404: already gone
                    raise RuntimeError(f"WebDAV DELETE {r.status}")
            return
        h = _sigv4("DELETE", url, self.opts.get("backup_region") or "us-east-1", self.opts.get("backup_user") or "",
                   self.opts.get("backup_password") or "", b"")
        async with self.session.delete(url, headers=h) as r:
            if r.status not in (200, 202, 204, 404):
                raise RuntimeError(f"S3 DELETE {r.status}")
