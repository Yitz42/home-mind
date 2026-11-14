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


class Updater:
    def __init__(self, store, url, session_factory=aiohttp.ClientSession):
        self.store, self.url, self.session_factory = store, url, session_factory
        self.last = None

    def installed(self):
        return self.store.kv_get("packs", {})

    def install_pack(self, pack, source):
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
                man = json.loads(await self._get(s, self.url))
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
                     "packs": packs, "manifest": man}
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
                    if p.get("sha256") and hashlib.sha256(raw).hexdigest() != p["sha256"]:
                        raise RuntimeError("sha256 does not match the manifest; not installed")
                    pack = json.loads(raw)
                    if pack.get("id") != p["id"]:
                        raise RuntimeError("pack id does not match the manifest")
                    done.append(self.install_pack(pack, url))
                except Exception as e:  # noqa: BLE001
                    errors.append({"id": p["id"], "error": str(e)[:300]})
        return {"installed": done, "errors": errors, "app": chk["app"]}
