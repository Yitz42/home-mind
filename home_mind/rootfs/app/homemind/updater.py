"""Updates.

  App code   comes through Home Assistant: the add-on repository's version is bumped, HA shows "Update" (or installs
             it by itself with auto-update on). check() reports whether a newer version is out.
  Packs      knowledge written by the large model: procedures (how to do things), as JSON. Listed in a manifest at
             update_url with a sha256 each; downloaded, checked, and installed into the mind as items of kind
             'procedure' with a key, so a new pack version supersedes the old procedures (history kept).
             Packs bundled in the image are installed on first start, so it works offline.
"""
import glob
import hashlib
import json
import os

import aiohttp

from .version import VERSION

BUNDLED = os.path.join(os.path.dirname(os.path.dirname(__file__)), "packs")


def _vt(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


from . import ed25519

KEYS_FILE = os.path.join(os.path.dirname(__file__), "pack_keys.txt")


def pack_keys():
    """Public keys (hex, one per line) whose signature a pack manifest must carry (#28). Empty: sha256 only, as
    before (the manifest then checks only that each pack arrived intact, not who wrote it)."""
    try:
        with open(KEYS_FILE) as f:
            return [bytes.fromhex(x.split("#")[0].strip()) for x in f if x.split("#")[0].strip()]
    except (OSError, ValueError):
        return []

class Updater:
    def __init__(self, store, url, session_factory=aiohttp.ClientSession):
        self.store, self.url, self.session_factory = store, url, session_factory
        self.last = None

    def installed(self):
        return self.store.kv_get("packs", {})

    @staticmethod
    def _valid(pack):
        """Check a whole pack before any of it is filed, so a bad entry can't leave it half-installed (some
        procedures at the new version, the pack still recorded at the old one, and the same failure every night)."""
        if not isinstance(pack, dict) or not isinstance(pack.get("id"), str) or not pack["id"] \
                or not isinstance(pack.get("version"), (str, int, float)):
            raise ValueError("pack has no id/version")
        procs = pack.get("procedures", [])
        if not isinstance(procs, list):
            raise ValueError("pack procedures is not a list")
        for i, p in enumerate(procs):
            if not isinstance(p, dict) or not all(isinstance(p.get(k), str) and p.get(k) for k in ("id", "title", "body")) \
                    or not isinstance(p.get("tags", []), list):
                raise ValueError(f"pack procedure {i} needs id, title and body")

    def install_pack(self, pack, source):
        self._valid(pack)
        pid, ver = pack["id"], str(pack["version"])
        n = 0
        for p in pack.get("procedures", []):
            r = self.store.remember(text=p["body"], title=p["title"], kind="procedure",
                                    shelf=p.get("shelf") or f"procedures/{pid}", tags=p.get("tags", []),
                                    key=f"procedure:{pid}:{p['id']}", source=f"pack:{pid}@{ver}",
                                    meta={"pack": pid, "pack_version": ver, "written_by": pack.get("written_by", "")})
            n += r["action"] != "reinforced"
        inst = self.installed()
        inst[pid] = {"version": ver, "source": source, "procedures": len(pack.get("procedures", []))}
        self.store.kv_set("packs", inst)
        self.store.log("pack", None, id=pid, version=ver, source=source, changed=n)
        return {"id": pid, "version": ver, "changed": n}

    def install_bundled(self):
        out = []
        for path in sorted(glob.glob(os.path.join(BUNDLED, "*.json"))):
            if path.endswith("manifest.json"):
                continue
            with open(path) as f:
                pack = json.load(f)
            cur = self.installed().get(pack["id"])
            if not cur or _vt(pack["version"]) > _vt(cur["version"]):
                out.append(self.install_pack(pack, "bundled"))
        return out

    async def _get(self, session, url):
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                raise RuntimeError(f"{url}: HTTP {r.status}")
            return await r.read()

    async def check(self):
        if not self.url:
            return {"error": "no update_url set"}
        try:
            async with self.session_factory() as s:
                raw_man = await self._get(s, self.url)
                signed = None
                keys = pack_keys()
                if keys:            # #28: the manifest must carry a signature by a key built into the app
                    try:
                        sig = bytes.fromhex((await self._get(s, self.url + ".sig")).decode().strip())
                    except Exception as e:  # noqa: BLE001
                        raise RuntimeError(f"the manifest has no signature ({e}); nothing installed") from e
                    signed = next((k for k in keys if ed25519.verify(k, raw_man, sig)), None)
                    if not signed:
                        raise RuntimeError("the manifest's signature doesn't match Home Mind's key; nothing installed")
                man = json.loads(raw_man)
            if not isinstance(man, dict) or not isinstance(man.get("packs", []), list) or not all(
                    isinstance(p, dict) and isinstance(p.get("id"), str) and p.get("version") is not None
                    and isinstance(p.get("url"), str) for p in man.get("packs", [])):
                raise ValueError("the manifest isn't a list of packs with id, version and url")
        except Exception as e:  # noqa: BLE001
            self.last = {"error": f"{type(e).__name__}: {e}"[:300]}
            return self.last
        inst = self.installed()
        packs = []
        for p in man.get("packs", []):
            cur = inst.get(p["id"], {}).get("version")
            packs.append({"id": p["id"], "installed": cur, "latest": p["version"],
                          "update_available": cur is None or _vt(p["version"]) > _vt(cur)})
        app_latest = man.get("app_latest")
        self.last = {"app": {"current": VERSION, "latest": app_latest,
                             "update_available": bool(app_latest) and _vt(app_latest) > _vt(VERSION),
                             "how": "Settings > Apps > Home Mind > Update (or turn on auto-update)"},
                     "packs": packs, "manifest": man,
                     "signed": signed.hex()[:16] if signed else (False if not keys else None)}
        return self.last

    async def apply(self):
        chk = await self.check()
        if "error" in chk:
            return chk
        done, errors = [], []
        base = self.url.rsplit("/", 1)[0]
        async with self.session_factory() as s:
            for p in chk["manifest"].get("packs", []):
                if not next(x for x in chk["packs"] if x["id"] == p["id"])["update_available"]:
                    continue
                try:
                    url = p["url"] if "://" in p["url"] else f"{base}/{p['url']}"
                    raw = await self._get(s, url)
                    if chk["signed"] and not p.get("sha256"):
                        # the signature covers the manifest only: a pack it lists without a hash could be anything
                        raise RuntimeError("the signed manifest gives no sha256 for this pack; not installed")
                    if p.get("sha256") and hashlib.sha256(raw).hexdigest() != p["sha256"]:
                        raise RuntimeError("sha256 does not match the manifest; not installed")
                    pack = json.loads(raw)
                    if pack.get("id") != p["id"]:
                        raise RuntimeError("pack id does not match the manifest")
                    done.append(self.install_pack(pack, url))
                except Exception as e:  # noqa: BLE001
                    errors.append({"id": p["id"], "error": str(e)[:300]})
        return {"installed": done, "errors": errors, "app": chk["app"]}
