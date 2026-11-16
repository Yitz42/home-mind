"""Export and import (#35): the whole memory as one JSON file you can keep, read, or load into another Home Mind.

Every note is exported with its history (the versions it replaced), its links and its fields. Secret notes are
exported masked unless secrets are asked for and the vault key is loaded. Importing merges: notes already here (same
text, same time) are skipped, ids are renumbered, and versions and links follow the new ids.
"""
import json
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
    if int(data.get("version") or 0) > FORMAT_VERSION:
        raise ValueError(f"export version {data.get('version')} is newer than this app reads ({FORMAT_VERSION})")
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError("export has no items list")
    seen = {(r["title"], r["body"], round(r["created"], 3)) for r in
            store.db.execute("SELECT title, body, created FROM items")}
    new_id, added, skipped, bad = {}, 0, 0, 0
    existing = {}
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
            for r in store.db.execute("SELECT id, title, body FROM items WHERE abs(created-?)<0.001", (created,)):
                if mask_all(r["body"]) == k[1] or r["body"] == k[1]:
                    seen.add(k)
                    break
        if k in seen:
            skipped += 1
            r = store.db.execute("SELECT id FROM items WHERE title=? AND body=? AND abs(created-?)<0.001",
                                 (k[0], k[1], created)).fetchone() or \
                store.db.execute("SELECT id FROM items WHERE abs(created-?)<0.001", (created,)).fetchone()
            if r and it.get("id") is not None:
                existing[it["id"]] = r[0]
            continue
        if it.get("masked"):
            meta = dict(it.get("meta") or {}, imported_masked=True)
        else:
            meta = dict(it.get("meta") or {})
        meta["imported"] = True
        if dry_run:
            added += 1
            seen.add(k)
            continue
        from .store import clean_tags, clean_shelf
        tags = it.get("tags") or []
        with store.lock:
            cur = store.db.execute(
                "INSERT INTO items(kind,key,title,body,tags,shelf,status,created,updated,last_used,use_count,strength,"
                "source,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(it.get("kind") or "note"), it.get("key"), k[0], k[1], clean_tags(tags), clean_shelf(it.get("shelf")),
                 it.get("status") if it.get("status") in ("active", "faded", "archived", "superseded") else "active",
                 created, float(it.get("updated") or created), it.get("last_used"), int(it.get("use_count") or 0),
                 float(it.get("strength") or 1.0), str(it.get("source") or "import"), json.dumps(meta)))
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
            store.seal_item(nid)
        added += 1
        seen.add(k)
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
        store._vocab_n = None
        store.log("import", None, added=added, skipped=skipped, bad=bad)
    else:
        nl = 0
    return {"added": added, "skipped": skipped, "bad": bad, "links": nl, "dry_run": bool(dry_run)}
