"""Photos with notes (#36): a picture kept with a note (where the spare key is hidden, the label on the boiler, what
the shelf looked like). The photo is saved in the add-on's data folder; the note lists it, and its caption becomes
extra search words, so "the boiler label" finds the note that has the photo.
"""
import hashlib
import json
import os
import re
import time

MAX_BYTES = 12 * 1024 * 1024
KINDS = {b"\xff\xd8\xff": ".jpg", b"\x89PNG\r\n\x1a\n": ".png", b"GIF87a": ".gif", b"GIF89a": ".gif"}
NAME = re.compile(r"^\d+-[0-9a-f]{12}\.(?:jpg|png|gif|webp|heic)$")
TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic"}


def kind_of(data):
    for sig, ext in KINDS.items():
        if data.startswith(sig):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc"):
        return ".heic"
    return None


class Photos:
    def __init__(self, store, folder):
        self.store, self.folder = store, folder
        os.makedirs(folder, exist_ok=True)

    def add(self, item_id, data, caption=""):
        if not data:
            raise ValueError("empty file")
        if len(data) > MAX_BYTES:
            raise ValueError(f"photo too large (max {MAX_BYTES // 1024 // 1024} MB)")
        ext = kind_of(data)
        if not ext:
            raise ValueError("not a photo (JPEG, PNG, GIF, WebP or HEIC)")
        d = self.store.get(int(item_id))
        if not d:
            raise KeyError(f"no item {item_id}")
        name = f"{d['id']}-{hashlib.sha256(data).hexdigest()[:12]}{ext}"
        path = os.path.join(self.folder, name)
        if not os.path.exists(path):
            with open(path + ".tmp", "wb") as f:
                f.write(data)
            os.replace(path + ".tmp", path)
        caption = re.sub(r"\s+", " ", caption or "").strip()[:200]
        with self.store.lock:
            r = self.store.db.execute("SELECT meta FROM items WHERE id=?", (d["id"],)).fetchone()
            meta = json.loads(r["meta"] or "{}")
            ph = [p for p in meta.get("photos", []) if p["file"] != name]
            ph.append({"file": name, "caption": caption, "ts": time.time(), "bytes": len(data)})
            meta["photos"] = ph[-20:]
            self.store.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(meta), d["id"]))
            self.store.db.commit()
        if caption:
            from .store import words, STOP
            self.store.add_keys(d["id"], [w for w in words(caption) if w not in STOP and len(w) > 2][:12], by="photo")
        self.store.log("photo", d["id"], file=name)
        return {"id": d["id"], "file": name, "caption": caption}

    def path(self, name):
        if not NAME.match(name or ""):
            raise ValueError("bad photo name")
        p = os.path.join(self.folder, name)
        if not os.path.exists(p):
            raise KeyError(name)
        return p, TYPES[os.path.splitext(name)[1]]

    def remove(self, item_id, name):
        with self.store.lock:
            r = self.store.db.execute("SELECT meta FROM items WHERE id=?", (int(item_id),)).fetchone()
            if not r:
                return False
            meta = json.loads(r["meta"] or "{}")
            ph = [p for p in meta.get("photos", []) if p["file"] != name]
            gone = len(ph) != len(meta.get("photos", []))
            meta["photos"] = ph
            if not ph:
                meta.pop("photos")
            self.store.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(meta), int(item_id)))
            self.store.db.commit()
        self.sweep()
        return gone

    def sweep(self):
        """Delete photo files no note lists any more (after a note is purged, or a photo removed)."""
        used = set()
        for r in self.store.db.execute("SELECT meta FROM items WHERE meta LIKE '%\"photos\"%'"):
            for p in json.loads(r["meta"] or "{}").get("photos", []):
                used.add(p.get("file"))
        n = 0
        for f in os.listdir(self.folder):
            if NAME.match(f) and f not in used:
                os.remove(os.path.join(self.folder, f))
                n += 1
        return n
