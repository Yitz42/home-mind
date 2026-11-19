"""Keeping things sound over months (design spec, Throughout and step 35; phase 9).

The forget list. What you had deleted for good stays deleted, even after a restore from a snapshot or a backup made
before you deleted it. Every purge is remembered as fingerprints, never as text: a keyed hash of each deleted note,
of each value found in it (codes, serials), and of a name purged with "forget everything about". After any restore
(and every night) the list is applied again: a note written before the purge whose fingerprint matches is deleted
for good once more; a note written since is new information and stays. Sealed notes are matched by their sealed text
as stored, and the copies phases 8-15 keep are scrubbed on every re-apply too. The
list is a table in the main database and, so a restore can't roll it back, also a file beside it (forget.jsonl);
the two are joined when read. The key is a file of its own (forget.key).

The check. One command cross-checks every store: the database file, the forget list (nothing it lists is back), the
learned records (each points at a device or note that exists), the decision log, the snapshots (one in the last two
days), the vault key (if anything is sealed). Runs at startup, every night and after any restore; /api/check.

The weekly test restore. The newest snapshot is copied, opened and checked (integrity, the forget list, how many
notes against the live database), and the result kept. A backup nobody has tried to restore is only a hope.

Rolling back a bad week. Every week the learned records (house templates, learned answers) are saved; every status
change is in the change log (events 'learned_change'); new learned records are capped per day. A bad week can be
rolled back to a saved week; the forget list is applied after.

A new model (step 35). When the local control model, the local chat model or the cloud model changes, trusted
learned records go back to provisional (they act again only after the nightly replay passes with the new model)
and kinds the old local model had earned are taken away, to be earned again.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time

NAME_WORDS = 4
LEARNED_TABLES = ("learned", "learned_templates", "learned_answers")
DAILY_CAP = 25              # new learned records a day, per kind
KEEP_WEEKS = 8


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _grams(text, n=NAME_WORDS):
    w = re.findall(r"[a-z0-9']+", (text or "").lower())
    for size in range(1, n + 1):
        for i in range(len(w) - size + 1):
            yield " ".join(w[i:i + size])


class ForgetList:
    def __init__(self, store, folder):
        self.store = store
        self.folder = folder
        self.file = os.path.join(folder, "forget.jsonl")
        kf = os.path.join(folder, "forget.key")
        if not os.path.exists(kf):
            os.makedirs(folder, exist_ok=True)
            with open(kf, "w") as f:
                f.write(secrets.token_hex(32))
            os.chmod(kf, 0o600)
        with open(kf) as f:
            self.key = bytes.fromhex(f.read().strip())
        with store.lock:
            store.db.execute("CREATE TABLE IF NOT EXISTS forget_list (h TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                             "ts REAL NOT NULL)")
            store.db.commit()

    def h(self, text):
        return hmac.new(self.key, _norm(text).encode(), hashlib.sha256).hexdigest()[:32]

    def _add(self, entries):
        now = time.time()
        with self.store.lock:
            self.store.db.executemany("INSERT OR IGNORE INTO forget_list (h, kind, ts) VALUES (?,?,?)",
                                      [(h, k, now) for h, k in entries])
            self.store.db.commit()
        with open(self.file, "a") as f:
            for h, k in entries:
                f.write(json.dumps({"h": h, "kind": k, "ts": now}) + "\n")

    def record_notes(self, rows, values=()):
        """Called by Store.purge: fingerprints of what was deleted (never the text)."""
        e = [(self.h(f"{r.get('title') or ''}\n{r.get('body') or ''}"), "note") for r in rows]
        e += [(self.h(r["_sealed"]), "note") for r in rows if r.get("_sealed")]   # a sealed note, as stored
        e += [(self.h(v), "value") for v in values if v]
        if e:
            self._add(e)

    def record_name(self, name):
        self._add([(self.h(name), "name")])

    def entries(self, with_time=False):
        """{fingerprint: kind}, or with_time {fingerprint: (kind, when it was forgotten)}."""
        out = {r["h"]: (r["kind"], r["ts"]) for r in self.store.db.execute("SELECT h, kind, ts FROM forget_list")}
        if os.path.exists(self.file):
            with open(self.file) as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        out.setdefault(d["h"], (d["kind"], float(d.get("ts") or 0)))
                    except (ValueError, KeyError, TypeError):
                        continue
        return out if with_time else {h: k for h, (k, _) in out.items()}

    def matches(self, conn=None):
        """Note ids (in this database, or another connection's) that the list says should be gone.

        Only notes written before the purge they match: a restore brings back old notes, which go again; a note
        written after it is new information (a code changed back to a forgotten one, "1200 W" on a new heater) and
        stays. The scan holds no lock and touches only what it reads, so it can run off the event loop."""
        ent = self.entries(with_time=True)
        if not ent:
            return []
        db = conn or self.store.db
        cols = {r[1] for r in db.execute("PRAGMA table_info(items)")}
        sel = "SELECT id, title, body, created" + (", secret" if "secret" in cols else "") + " FROM items"
        ids = []

        def hit(h, kind, created):
            e = ent.get(h)
            return bool(e) and (kind is None or e[0] == kind) and (created is None or created <= e[1])

        for r in db.execute(sel):
            created = r["created"]
            text = f"{r['title'] or ''}\n{r['body'] or ''}"
            if hit(self.h(text), None, created):
                ids.append(r["id"])
                continue
            if "secret" in cols and r["secret"]:
                if hit(self.h(r["secret"]), "note", created):
                    ids.append(r["id"])     # a sealed note: its stored sealed text is what was fingerprinted
                continue
            vals = set(re.findall(r"(?<![\w-])(?=[\w!@#$%^&*-]*\d)[\w!@#$%^&*-]{4,}", text))
            if any(hit(self.h(v), "value", created) for v in vals) or \
                    any(hit(self.h(g), "name", created) for g in _grams(text)):
                ids.append(r["id"])
        return ids

    def apply(self, ids=None):
        """Delete again, for good, whatever the list says should be gone. Returns how many notes. The copies kept
        elsewhere (learned answers, phone messages, trackers) are scrubbed as after any purge."""
        ids = self.matches() if ids is None else ids
        if not ids:
            return 0
        hook, self.store.on_forget = getattr(self.store, "on_forget", None), None   # don't record them twice
        try:
            self.store.purge(ids)
        finally:
            self.store.on_forget = hook
        try:
            scrub(self.store, ids, ())
        except Exception:  # noqa: BLE001 - the notes are gone; the copies are tidied next night
            pass
        self.store.log("forget_reapplied", None, n=len(ids))
        return len(ids)


def scrub(store, ids, values):
    """After a purge: the copies phases 8 to 15 keep go too (learned answers and their weekly saves, phone messages,
    the surprises queue, tracker words). Store.purge already covers events, conversations and test cases."""
    ids, vals = set(ids or ()), [v for v in (values or ()) if v and len(v) >= 4]
    db = store.db
    have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def hit(text):
        return any(v in (text or "") for v in vals)
    n = 0
    with store.lock:
        if "learned_answers" in have:
            gone = [r["record_id"] for r in db.execute("SELECT record_id, item_id, answer, examples FROM "
                                                       "learned_answers")
                    if r["item_id"] in ids or hit(r["answer"]) or hit(r["examples"])]
            for rid in gone:
                db.execute("DELETE FROM learned_answers WHERE record_id=?", (rid,))
                db.execute("DELETE FROM learned WHERE id=?", (rid,))
            n += len(gone)
        if "learned_weeks" in have:
            for w in db.execute("SELECT week, data FROM learned_weeks").fetchall():
                d = json.loads(w["data"] or "{}")
                rows = d.get("learned_answers") or []
                keep = [r for r in rows if r.get("item_id") not in ids and not hit(r.get("answer")) and
                        not hit(r.get("examples"))]
                if len(keep) != len(rows):
                    dropped = {r["record_id"] for r in rows} - {r["record_id"] for r in keep}
                    d["learned_answers"] = keep
                    d["learned"] = [r for r in d.get("learned") or [] if r.get("id") not in dropped]
                    db.execute("UPDATE learned_weeks SET data=? WHERE week=?", (json.dumps(d), w["week"]))
                    n += len(dropped)
        for table, col in (("outbox", "data"), ("surprises", "what"), ("tracker_entries", "details"),
                           ("meds_log", "name")):
            if table in have and vals:
                for v in vals:
                    n += db.execute(f"DELETE FROM {table} WHERE {col} LIKE ?", (f"%{v}%",)).rowcount
        db.commit()
    return n


# ---- the check ---------------------------------------------------------------------------------------------------
def check(app):
    """Every store, cross-checked. {"ok": bool, "problems": [...], "checks": {...}}"""
    from . import dbguard
    probs, checks = [], {}
    st = app.mind.store
    try:
        r = st.db.execute("PRAGMA quick_check").fetchone()[0]
        checks["database"] = r
        if r != "ok":
            probs.append(f"the database check says: {r}")
    except Exception as e:  # noqa: BLE001
        probs.append(f"the database couldn't be checked: {e}")
    fl = getattr(app, "forget", None)
    if fl is not None:
        back = fl.matches()
        checks["forgotten_back"] = len(back)
        if back:
            probs.append(f"{len(back)} note(s) you had deleted for good are back (a restore?); they'll be deleted again")
    for name in ("learned", "learned_answers"):
        rec = getattr(app, name, None)
        if rec is None:
            continue
        bad = 0
        for r in rec.all():
            if r["status"] in ("retired", "dropped"):
                continue
            if name == "learned" and getattr(rec, "home", None) is not None and rec.home.catalog and \
                    r["entity_id"] not in rec.home.catalog:
                bad += 1
            if name == "learned_answers" and not st.get(r["item_id"]):
                bad += 1
        checks[name + "_dangling"] = bad
        if bad:
            probs.append(f"{bad} {name.replace('_', ' ')} point at something that's gone (retired tonight)")
    dl = getattr(app, "decisions", None)
    if dl is not None:
        try:
            dl.db.execute("SELECT COUNT(*) FROM decisions").fetchone()
            checks["decision_log"] = "ok"
        except Exception as e:  # noqa: BLE001
            probs.append(f"the decision log can't be read: {e}")
    try:
        s = dbguard.status(st.path)
        checks["snapshots"] = s
        last = s.get("last_snapshot")
        if not last:
            probs.append("there is no database snapshot yet")
        elif time.time() - time.mktime(time.strptime(last, "%Y-%m-%d")) > 2.5 * 86400:
            probs.append(f"the newest snapshot is from {last}")
    except Exception:  # noqa: BLE001
        pass
    try:
        sealed = st.db.execute("SELECT COUNT(*) FROM items WHERE meta LIKE '%\"sealed\": true%'").fetchone()[0]
    except Exception:  # noqa: BLE001
        sealed = 0
    if sealed and not getattr(st, "vault_key", None):
        probs.append(f"{sealed} sealed note(s) but no vault key: they can't be opened")
    rt = st.kv_get("restore_test")
    checks["restore_test"] = rt
    if rt and not rt.get("ok"):
        probs.append(f"the last test restore failed: {rt.get('why')}")
    return {"ok": not probs, "problems": probs, "checks": checks, "at": time.time()}


# ---- the weekly test restore -------------------------------------------------------------------------------------
def restore_test(store, forget=None):
    """Copy the newest snapshot, open it, check it. Kept in kv 'restore_test'."""
    from . import dbguard
    snaps = dbguard.snapshots(store.path)
    out = {"at": time.time(), "ok": False}
    if not snaps:
        out["why"] = "no snapshot to try"
        store.kv_set("restore_test", out)
        return out
    d = tempfile.mkdtemp(prefix="hm-restore-")
    try:
        copy = os.path.join(d, "mind.db")
        shutil.copy2(snaps[0], copy)
        conn = sqlite3.connect(copy)
        conn.row_factory = sqlite3.Row
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            n = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            back = len(forget.matches(conn)) if forget is not None else 0
        finally:
            conn.close()
        live = store.db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        out.update(snapshot=os.path.basename(snaps[0]), integrity=integrity, notes=n, live_notes=live,
                   forget_would_remove=back)
        if integrity != "ok":
            out["why"] = f"integrity check: {integrity}"
        elif live and n < 0.5 * live:
            out["why"] = f"the snapshot has {n} notes against {live} now"
        else:
            out["ok"] = True
    except Exception as e:  # noqa: BLE001
        out["why"] = f"{type(e).__name__}: {e}"
    finally:
        shutil.rmtree(d, ignore_errors=True)
    store.kv_set("restore_test", out)
    return out


# ---- learned records: weekly saves, the change log, rollback, the daily cap ---------------------------------------
class LearnedHistory:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or time.time
        with store.lock:
            store.db.execute("CREATE TABLE IF NOT EXISTS learned_weeks (week TEXT PRIMARY KEY, ts REAL, data TEXT)")
            store.db.commit()

    def _tables(self):
        return [t for t in LEARNED_TABLES if self.store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()]

    def save_week(self, force=False):
        week = time.strftime("%G-W%V", time.localtime(self.clock()))
        if not force and self.store.db.execute("SELECT 1 FROM learned_weeks WHERE week=?", (week,)).fetchone():
            return None
        data = {t: [dict(r) for r in self.store.db.execute(f"SELECT * FROM {t}")] for t in self._tables()}
        with self.store.lock:
            self.store.db.execute("INSERT OR REPLACE INTO learned_weeks (week, ts, data) VALUES (?,?,?)",
                                  (week, self.clock(), json.dumps(data)))
            old = [r["week"] for r in self.store.db.execute("SELECT week FROM learned_weeks ORDER BY week DESC")]
            for w in old[KEEP_WEEKS:]:
                self.store.db.execute("DELETE FROM learned_weeks WHERE week=?", (w,))
            self.store.db.commit()
        return week

    def weeks(self):
        out = []
        for r in self.store.db.execute("SELECT week, ts, data FROM learned_weeks ORDER BY week DESC"):
            d = json.loads(r["data"])
            out.append({"week": r["week"], "saved": r["ts"], "records": len(d.get("learned") or []),
                        "trusted": sum(1 for x in d.get("learned") or [] if x.get("status") == "trusted")})
        return out

    def rollback(self, week, forget=None):
        r = self.store.db.execute("SELECT data FROM learned_weeks WHERE week=?", (week,)).fetchone()
        if not r:
            raise ValueError(f"no saved week {week}")
        data = json.loads(r["data"])
        with self.store.lock:
            try:
                for t in self._tables():
                    self.store.db.execute(f"DELETE FROM {t}")
                    for row in data.get(t) or []:
                        cols = list(row)
                        self.store.db.execute(f"INSERT INTO {t} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                              [row[c] for c in cols])
                self.store.db.commit()
            except BaseException:
                self.store.db.rollback()
                raise
        self.store.log("learned_change", None, change="rollback", week=week)
        removed = forget.apply() if forget is not None else 0
        return {"week": week, "records": len(data.get("learned") or []), "forget_reapplied": removed}

    def changes(self, days=14):
        since = self.clock() - days * 86400
        return [dict(json.loads(r["data"]), at=r["ts"]) for r in self.store.db.execute(
            "SELECT ts, data FROM events WHERE type='learned_change' AND ts>=? ORDER BY ts DESC LIMIT 200", (since,))]

    def created_today(self, kind):
        start = time.mktime(time.localtime(self.clock())[:3] + (0, 0, 0, 0, 0, -1))
        return self.store.db.execute("SELECT COUNT(*) FROM learned WHERE type=? AND created>=?",
                                     (kind, start)).fetchone()[0]

    def room_today(self, kind):
        return self.created_today(kind) < DAILY_CAP


# ---- a new model (step 35) ---------------------------------------------------------------------------------------
def fingerprint(app):
    opts = getattr(app, "opts", None) or {}
    ms = getattr(app.models, "models", {}) or {}

    def fp(name):
        m = ms.get(name)
        cfg = getattr(m, "cfg", None) or {}
        return f"{name}:{cfg.get('model') or cfg.get('name') or ''}" if name else ""
    modes = getattr(app, "modes", None)
    cloud = modes.cloud() if modes is not None else ""
    return {"control": fp(opts.get("control_model") or ""), "local": fp(opts.get("local_model") or ""),
            "cloud": fp(cloud or "")}


def retest_if_swapped(app):
    """Compare the models with last time; on a change, trusted learned records and earned kinds go back on trial."""
    st = app.mind.store
    now_fp = fingerprint(app)
    before = st.kv_get("model_fingerprint")
    st.kv_set("model_fingerprint", now_fp)
    if before is None or before == now_fp:
        return None
    changed = [k for k in now_fp if before.get(k) != now_fp[k]]
    n = 0
    for name in ("learned", "learned_answers"):
        rec = getattr(app, name, None)
        if rec is None:
            continue
        for r in rec.all("trusted"):
            rec.set_status(r["id"], "provisional", f"model changed ({', '.join(changed)}): back on trial")
            n += 1
    earned = []
    modes = getattr(app, "modes", None)
    if modes is not None and "control" in changed:
        earned = list(st.kv_get(modes.EARNED) or [])
        st.kv_set(modes.EARNED, [])
    st.log("learned_change", None, change="model swap", models=changed, back_on_trial=n, kinds_unearned=earned)
    return {"changed": changed, "back_on_trial": n, "kinds_unearned": earned}
