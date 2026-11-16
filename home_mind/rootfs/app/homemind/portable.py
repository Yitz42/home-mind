"""Export and import (#35): the whole memory as one JSON file you can keep, read, or load into another Home Mind.

Every note is exported with its history (the versions it replaced), its links and its fields. Secret notes are
exported masked unless secrets are asked for and the vault key is loaded. Importing merges: notes already here (same
text, same time) are skipped, ids are renumbered, and versions and links follow the new ids.
"""
import json
import math
import time

FORMAT = "home-mind-export"
FORMAT_VERSION = 1
COLS = ("kind", "key", "title", "body", "tags", "shelf", "status", "created", "updated", "last_used", "use_count",
        "strength", "source")
SKIP_SOURCES = ("home",)          # house readings come back from Home Assistant; they'd only bloat the file


def export(store, secrets=False, include_home=False, app_version=""):
    rows = store.db.execute("SELECT * FROM items ORDER BY id").fetchall()
    items = []
    for r in rows:
        if r["source"] in SKIP_SOURCES and not include_home:
            continue
        d = store._row(r)
        sensitive = bool(d["meta"].get("sensitive"))
        if sensitive and not secrets and r["secret"] is None and not d.get("locked"):
            # an open secret note (no vault): masked like everywhere else it leaves the house
            from .privacy import mask_all as mask
            d["title"], d["body"] = mask(d["title"]), mask(d["body"])
            d["masked"] = True
        elif sensitive and (not secrets or d.get("locked")):
            d["masked"] = True          # sealed: the masked copy is all that leaves (and all there is, without the key)
            if r["secret"] is not None and not d.get("locked"):
                from .privacy import mask_all as mask
                d["title"], d["body"] = mask(d["title"]), mask(d["body"])
        out = {"id": d["id"], **{k: d.get(k) for k in COLS}, "tags": d["tags"], "supersedes": d["supersedes"],
               "meta": d["meta"], "keys": d.get("keys") or ""}
        if d.get("masked"):
            out["masked"] = True
        items.append(out)
    ids = {i["id"] for i in items}
    links = [{"a": r["a"], "b": r["b"], "kind": r["kind"]} for r in store.db.execute("SELECT * FROM links")
             if r["a"] in ids and r["b"] in ids]
    return {"format": FORMAT, "version": FORMAT_VERSION, "app": app_version, "exported": time.time(),
            "counts": {"items": len(items), "links": len(links)}, "items": items, "links": links}


def import_(store, data, dry_run=False):
    """Merge an export into this memory. Returns what was (or would be) added and skipped."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("not a Home Mind export (format should be home-mind-export)")
    try:
        version = int(data.get("version") or 0)
    except (TypeError, ValueError):
        raise ValueError("export version should be a number") from None
    if version > FORMAT_VERSION:
        raise ValueError(f"export version {data.get('version')} is newer than this app reads ({FORMAT_VERSION})")
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError("export has no items list")
    # the notes here before the import, by the millisecond they were filed: an imported note is matched against those
    # few, not by a scan of every note (re-importing 30,000 notes took minutes, holding the store's lock each scan)
    before = {}
    for r in store.db.execute("SELECT id, title, body, created FROM items ORDER BY id"):
        before.setdefault(math.floor(r["created"] * 1000), []).append(r)
    seen = {(r["title"], r["body"], round(r["created"], 3)) for rs in before.values() for r in rs}

    def filed_at(t):
        """Notes here before the import filed within a millisecond of t, oldest id first."""
        b = math.floor(t * 1000)
        return sorted((r for k in range(b - 2, b + 3) for r in before.get(k, ()) if abs(r["created"] - t) < 0.001),
                      key=lambda r: r["id"])
    new_id, added, skipped, bad, sealed = {}, 0, 0, 0, 0
    existing = {}
    # notes are matched against what was here before the import, not against notes it just added: two notes of the
    # file filed in the same millisecond (two secret notes masked alike, a value and the one that replaced it) are
    # two notes (filed_at only holds those)
    for it in sorted(items, key=lambda x: (x.get("id") or 0) if isinstance(x, dict) else 0):
        if not isinstance(it, dict) or not isinstance(it.get("body", ""), str) or "created" not in it:
            bad += 1
            continue
        try:
            created = float(it["created"])
        except (TypeError, ValueError):
            bad += 1
            continue
        k = (it.get("title") or "", it.get("body") or "", round(created, 3))
        if it.get("masked") and k not in seen:
            # a masked secret: the same note if one filed at that moment masks to the same text
            from .privacy import mask_all
            for r in filed_at(created):
                if mask_all(r["body"]) == k[1] or r["body"] == k[1]:
                    seen.add(k)
                    break
        if k in seen:
            skipped += 1
            near = filed_at(created)
            r = next((x for x in near if x["title"] == k[0] and x["body"] == k[1]), None) or next(iter(near), None)
            if r and it.get("id") is not None:
                existing[it["id"]] = r[0]
            continue
        meta = dict(it["meta"]) if isinstance(it.get("meta"), dict) else {}
        if it.get("masked"):
            meta["imported_masked"] = True
        meta["imported"] = True
        if dry_run:
            added += 1
            continue
        from .store import clean_tags, clean_shelf
        tags = it.get("tags") or []
        try:
            row = (str(it.get("kind") or "note"), str(it["key"]) if it.get("key") else None, str(k[0]), k[1],
                   clean_tags(tags if isinstance(tags, (list, str)) else []), clean_shelf(str(it.get("shelf") or "inbox")),
                   # a note held for review (#16) stays held: importing must not make it searchable
                   it.get("status") if it.get("status") in ("active", "faded", "archived", "superseded", "review")
                   else "active",
                   created, float(it.get("updated") or created),
                   float(it["last_used"]) if it.get("last_used") else None, int(it.get("use_count") or 0),
                   float(it.get("strength") or 1.0), str(it.get("source") or "import"), json.dumps(meta, default=str))
        except (TypeError, ValueError):
            bad += 1            # one broken item doesn't stop the rest
            continue
        with store.lock:
            cur = store.db.execute(
                "INSERT INTO items(kind,key,title,body,tags,shelf,status,created,updated,last_used,use_count,strength,"
                "source,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            store.db.commit()
        nid = cur.lastrowid
        if it.get("id") is not None:
            new_id[it["id"]] = nid
        if it.get("keys"):
            try:
                store.add_keys(nid, str(it["keys"]).split(), by="import")
            except Exception:  # noqa: BLE001
                pass
        if store.vault_key and meta.get("sensitive") and not it.get("masked"):
            sealed += bool(store.seal_item(nid, _scrub=False))
        added += 1
    if not dry_run:
        ids = {**existing, **new_id}
        with store.lock:
            for it in items:
                if isinstance(it, dict) and it.get("id") in new_id and it.get("supersedes") in ids:
                    store.db.execute("UPDATE items SET supersedes=? WHERE id=?", (ids[it["supersedes"]], new_id[it["id"]]))
            nl = 0
            for ln in data.get("links") or []:
                try:
                    a, b = ids.get(ln["a"]), ids.get(ln["b"])
                except (TypeError, KeyError):
                    continue
                if a and b and (ln["a"] in new_id or ln["b"] in new_id):
                    store.db.execute("INSERT OR IGNORE INTO links(a,b,kind) VALUES(?,?,?)", (a, b, str(ln.get("kind") or "same_thing")))
                    nl += 1
            store.db.commit()
        if sealed:
            store._scrub_index()        # once, not per note: the clear text the index held before sealing
        store._vocab_n = None
        store.log("import", None, added=added, skipped=skipped, bad=bad)
    else:
        nl = 0
    return {"added": added, "skipped": skipped, "bad": bad, "links": nl, "dry_run": bool(dry_run)}
