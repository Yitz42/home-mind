"""Home Mind storage: one SQLite file holding what the mind knows and everything that happened to it.

Stores
  items    what is kept: facts, notes, tasks, procedures, episodes. Each has a shelf (a path like
           "projects/pebble") and tags, so it can be found by browsing as well as by searching.
           Changing an item never overwrites it: a new version is written that supersedes the old one,
           so the mind can say what it believed before and when that changed.
  events   every store, recall, update, feedback and test step, with the time (the episodic log).
  runs / metrics   test runs and the numbers they produced, kept for trends.

Search is SQLite FTS5 (BM25) plus small boosts for recent and often-used items. If FTS5 is missing it
falls back to word overlap so the mind still works.
"""
import json
import math
import os
import re
import sqlite3
import threading
import time

STOP = set("""a an the of to in on at for and or is are was were be been it its this that these those with
from by as what which who whom whose when where why how do does did can could should would will i me my we our you
your he she they them his her their there here about into over under than then so if not no yes any some all""".split())

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'note',
  key TEXT,
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '',
  shelf TEXT NOT NULL DEFAULT 'inbox',
  status TEXT NOT NULL DEFAULT 'active',
  supersedes INTEGER,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  last_used REAL,
  use_count INTEGER NOT NULL DEFAULT 0,
  strength REAL NOT NULL DEFAULT 1.0,
  source TEXT NOT NULL DEFAULT '',
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS items_key ON items(key, status);
CREATE INDEX IF NOT EXISTS items_shelf ON items(shelf, status);
CREATE INDEX IF NOT EXISTS items_kind ON items(kind, status);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, type TEXT NOT NULL, item_id INTEGER, data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, ts REAL NOT NULL, suite TEXT, arm TEXT, model TEXT, seed INTEGER,
  status TEXT, config TEXT, summary TEXT, details TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
  run_id TEXT, ts REAL, suite TEXT, arm TEXT, model TEXT, name TEXT, value REAL
);
CREATE INDEX IF NOT EXISTS metrics_suite ON metrics(suite, arm, name, ts);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, body, tags, shelf, content='items', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, body, tags, shelf) VALUES (new.id, new.title, new.body, new.tags, new.shelf);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, body, tags, shelf) VALUES('delete', old.id, old.title, old.body, old.tags, old.shelf);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE OF title, body, tags, shelf ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, body, tags, shelf) VALUES('delete', old.id, old.title, old.body, old.tags, old.shelf);
  INSERT INTO items_fts(rowid, title, body, tags, shelf) VALUES (new.id, new.title, new.body, new.tags, new.shelf);
END;
"""


def words(text):
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in STOP and len(w) > 1]


def names_in(text):
    """Lower-cased words written with a capital letter anywhere but at the start of a sentence: likely names."""
    out = set()
    for sent in re.split(r"[.!?]\s+", text or ""):
        toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9']*", sent)
        for t in toks[1:]:
            if t[0].isupper() and t.lower() not in STOP:
                out.add(t.lower().split("'")[0])
    return out


def clean_shelf(shelf):
    parts = [re.sub(r"[^a-z0-9_\-]+", "-", p.strip().lower()).strip("-") for p in (shelf or "inbox").split("/")]
    return "/".join(p for p in parts if p) or "inbox"


def clean_tags(tags):
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    return " ".join(sorted({re.sub(r"[^a-z0-9_\-]", "", t.lower()) for t in (tags or []) if t and t.strip()} - {""}))


class Store:
    def __init__(self, path, clock=None):
        self.path = path
        self.clock = clock or time.time
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            try:
                self.db.executescript(FTS)
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    # ---- helpers ------------------------------------------------------------------------------------------
    def _row(self, r):
        if r is None:
            return None
        d = dict(r)
        d["meta"] = json.loads(d.get("meta") or "{}")
        d["tags"] = d["tags"].split() if d.get("tags") else []
        return d

    def log(self, type_, item_id=None, **data):
        with self.lock:
            self.db.execute("INSERT INTO events(ts,type,item_id,data) VALUES(?,?,?,?)",
                            (self.clock(), type_, item_id, json.dumps(data, default=str)[:20000]))
            self.db.commit()

    def kv_get(self, k, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def kv_set(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))
            self.db.commit()

    def get(self, item_id, history=False):
        r = self._row(self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())
        if r and history:
            chain, cur = [], r
            while cur and cur.get("supersedes"):
                cur = self._row(self.db.execute("SELECT * FROM items WHERE id=?", (cur["supersedes"],)).fetchone())
                if cur:
                    chain.append({"id": cur["id"], "title": cur["title"], "body": cur["body"], "created": cur["created"]})
            later = self.db.execute("SELECT id FROM items WHERE supersedes=?", (item_id,)).fetchone()
            r["history"] = chain
            r["superseded_by"] = later["id"] if later else None
        return r

    def active_by_key(self, key):
        return self._row(self.db.execute(
            "SELECT * FROM items WHERE key=? AND status IN ('active','faded') ORDER BY id DESC LIMIT 1", (key,)).fetchone())

    # ---- writing ----------------------------------------------------------------------------------------------
    def remember(self, text="", title="", kind="note", shelf="inbox", tags=None, key=None, source="", meta=None):
        """Keep something. With a key (e.g. 'tavrin locker|code'), a different value for the same key supersedes
        the old item (kept as history); the same value just strengthens it."""
        text, title = (text or "").strip(), (title or "").strip()
        if not text and not title:
            raise ValueError("nothing to remember")
        if not title:
            title = text.split("\n")[0][:90]
        key = (key or "").strip().lower() or None
        shelf, tags = clean_shelf(shelf), clean_tags(tags)
        now = self.clock()
        with self.lock:
            old = self.active_by_key(key) if key else None
            if old and old["body"].strip().lower() == text.lower() and old["title"].strip().lower() == title.lower():
                self.db.execute("UPDATE items SET use_count=use_count+1, strength=strength+1, last_used=?, "
                                "status='active' WHERE id=?", (now, old["id"]))
                self.db.commit()
                self.log("reinforce", old["id"], key=key)
                return {"id": old["id"], "action": "reinforced"}
            m = dict(meta or {})
            cur = self.db.execute(
                "INSERT INTO items(kind,key,title,body,tags,shelf,status,supersedes,created,updated,source,meta) "
                "VALUES(?,?,?,?,?,?,'active',?,?,?,?,?)",
                (kind, key, title, text, tags, shelf, old["id"] if old else None, now, now, source, json.dumps(m)))
            new_id = cur.lastrowid
            if old:
                self.db.execute("UPDATE items SET status='superseded', updated=? WHERE id=?", (now, old["id"]))
            self.db.commit()
        out = {"id": new_id, "action": "updated" if old else "created", "shelf": shelf}
        if old:
            out["previous"] = {"id": old["id"], "title": old["title"], "body": old["body"]}
        self.log("remember", new_id, kind=kind, key=key, shelf=shelf, action=out["action"], source=source)
        return out

    def revise(self, item_id, **changes):
        """A new version of an item (old one kept as history)."""
        old = self.get(item_id)
        if not old:
            raise KeyError(f"no item {item_id}")
        f = {k: old[k] for k in ("kind", "key", "title", "body", "shelf", "source")}
        f["tags"], f["meta"] = old["tags"], old["meta"]
        for k, v in changes.items():
            if v is not None and k in ("kind", "key", "title", "body", "shelf", "tags", "meta", "source"):
                f[k] = v if k != "meta" else {**old["meta"], **v}
        now = self.clock()
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO items(kind,key,title,body,tags,shelf,status,supersedes,created,updated,source,meta,"
                "use_count,strength) VALUES(?,?,?,?,?,?,'active',?,?,?,?,?,?,?)",
                (f["kind"], f["key"], f["title"], f["body"], clean_tags(f["tags"]), clean_shelf(f["shelf"]), item_id,
                 now, now, f["source"], json.dumps(f["meta"]), old["use_count"], old["strength"]))
            self.db.execute("UPDATE items SET status='superseded', updated=? WHERE id=?", (now, item_id))
            self.db.commit()
            new_id = cur.lastrowid
        self.log("revise", new_id, previous=item_id, changes=list(changes))
        return {"id": new_id, "action": "updated", "previous": item_id}

    def set_status(self, item_id, status):
        with self.lock:
            self.db.execute("UPDATE items SET status=?, updated=? WHERE id=?", (status, self.clock(), item_id))
            self.db.commit()
        self.log("status", item_id, status=status)
        return {"id": item_id, "status": status}

    def feedback(self, item_id, helpful, note=""):
        with self.lock:
            self.db.execute("UPDATE items SET strength=max(0.1, strength + ?) WHERE id=?",
                            (0.5 if helpful else -0.5, item_id))
            self.db.commit()
        self.log("feedback", item_id, helpful=bool(helpful), note=note)
        return {"id": item_id, "helpful": bool(helpful)}

    # ---- finding ------------------------------------------------------------------------------------------------
    def recall(self, query, shelf=None, kind=None, limit=5, include_old=False, touch=True, newest_first_ties=True):
        """Ranked items for a query. Old versions and archived items are left out unless include_old."""
        ws = words(query)
        statuses = ("active", "faded", "superseded") if include_old else ("active", "faded")
        where = [f"i.status IN ({','.join('?' * len(statuses))})"]
        args = list(statuses)
        if shelf:
            where.append("(i.shelf = ? OR i.shelf LIKE ?)")
            args += [clean_shelf(shelf), clean_shelf(shelf) + "/%"]
        if kind:
            where.append("i.kind = ?")
            args.append(kind)
        rows = []
        if ws and self.fts:
            q = " OR ".join(f'"{w}"*' for w in dict.fromkeys(ws))
            sql = (f"SELECT i.*, bm25(items_fts, {self.BM25}) AS rank FROM items_fts JOIN items i ON i.id = items_fts.rowid "
                   f"WHERE items_fts MATCH ? AND {' AND '.join(where)} ORDER BY rank LIMIT ?")
            try:
                rows = self.db.execute(sql, [q] + args + [max(limit * 6, 30)]).fetchall()
            except sqlite3.OperationalError:
                rows = []
            scored = [(-r["rank"], r) for r in rows]
        elif ws:
            cand = self.db.execute(f"SELECT i.* FROM items i WHERE {' AND '.join(where)}", args).fetchall()
            scored = []
            for r in cand:
                text = set(words(" ".join([r["title"], r["title"], r["body"], r["tags"], r["shelf"]])))
                s = sum(1 for w in ws if w in text)
                if s:
                    scored.append((float(s), r))
        else:
            cand = self.db.execute(f"SELECT i.* FROM items i WHERE {' AND '.join(where)} ORDER BY i.updated DESC LIMIT ?",
                                   args + [limit]).fetchall()
            scored = [(1.0, r) for r in cand]
        now = self.clock()
        idf, matched = self._term_stats(ws, [r for _, r in scored]) if ws else ({}, {})
        total = sum(idf.values()) or 1.0
        qnames = names_in(query) & set(idf)
        out = []
        for base, r in scored:
            age_days = max(0.0, (now - r["created"]) / 86400.0)
            boost = 1 + 0.05 * math.log1p(r["use_count"]) + 0.05 * math.log1p(max(0.0, r["strength"] - 1))
            boost *= 1 + 0.02 / (1 + age_days)          # a slight lean toward newer
            if r["status"] == "faded":
                boost *= 0.7
            if r["status"] == "superseded":
                boost *= 0.5
            if qnames and self.NAME_BOOST != 1:
                # the question names something: notes that contain every name come first
                if qnames <= matched.get(r["id"], set()):
                    boost *= self.NAME_BOOST
            if idf and self.COVER_POW:
                # how much of the query's distinctive weight this item matches (rare words count for more)
                cover = sum(idf[w] for w in matched.get(r["id"], ())) / total
                boost *= max(cover, 0.05) ** self.COVER_POW
            out.append([base * boost, r["created"] if newest_first_ties else 0, r])
        out.sort(key=lambda x: (x[0], x[1]), reverse=True)
        older = {}
        if self.CLUSTER and idf:
            out, older = self._newest_in_cluster(out, idf, matched, names_in(query))
        res = []
        for score, _, r in out[:limit]:
            d = self._row(r)
            d["score"] = round(score, 4)
            d.pop("rank", None)
            if r["id"] in older:
                d["newer"] = older[r["id"]]
            res.append(d)
        if touch and res:
            with self.lock:
                self.db.executemany("UPDATE items SET use_count=use_count+1, last_used=?, "
                                    "status=CASE WHEN status='faded' THEN 'active' ELSE status END WHERE id=?",
                                    [(now, d["id"]) for d in res])
                self.db.commit()
        self.log("recall", None, query=query, shelf=shelf, kind=kind, ids=[d["id"] for d in res])
        return res

    # recall settings (class-level so tests can compare with and without)
    BM25 = "4.0, 1.0, 2.0, 1.5"  # weights for title, body, tags, shelf
    NAME_BOOST = 1.5     # how much a note containing every name in the question is lifted
    COVER_POW = 0.0      # weight by distinctive-word coverage (tested: no gain over BM25, so off)
    CLUSTER = True       # newest first among results that match the query on the same distinctive words
    CLUSTER_KINDS = ("fact",)  # only facts are replaced by newer ones; notes and ideas add up
    CLUSTER_OVERLAP = 0.6      # share of matched words two facts must have in common to be versions of one thing
    RARE_SHARE = 0.1     # a word is distinctive if it is in at most this share of items (or at most 3)
    COMMON_SHARE = 0.25  # words in more than this share of items are left out of the cluster key

    def _term_stats(self, ws, rows):
        """For each query word: how rare it is (IDF over all items) and which candidate items contain it."""
        n = max(1, self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0])
        ids = {r["id"] for r in rows}
        idf, matched, dfs = {}, {}, {}
        for w in dict.fromkeys(ws):
            if self.fts:
                try:
                    # whole words only: a prefix ("log" in "Logor") must not look like a rare match
                    hit = {x[0] for x in self.db.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH ?",
                                                          (f'"{w}"',))}
                except sqlite3.OperationalError:
                    continue
            else:
                hit = {r["id"] for r in rows if w in words(" ".join([r["title"], r["body"], r["tags"], r["shelf"]]))}
            df = len(hit)
            if not df:
                continue
            dfs[w] = df
            idf[w] = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for i in hit & ids:
                matched.setdefault(i, set()).add(w)
        self._rare = {w for w, df in dfs.items() if df <= max(3, self.RARE_SHARE * n)}
        self._common = {w for w, df in dfs.items() if df > self.COMMON_SHARE * n}
        return idf, matched

    def _newest_in_cluster(self, out, idf, matched, names=()):
        """Facts that are versions of the same thing should come newest first. The thing is pinned by the query's most
        distinctive word that any result contains (usually a name): facts containing it whose matched words overlap
        the best such fact's by at least CLUSTER_OVERLAP are taken as versions of it. The newest takes the best place;
        older ones follow, marked with the newer id. Other results keep their order. Nothing is merged or hidden."""
        present = [w for w in idf if any(w in matched.get(r[2]["id"], ()) for r in out)]
        # a name in the question (a capitalized word that doesn't start it) is what the question is about; a rare
        # word that is part of a value ("laundry room") is not. Names first, then the rarest word.
        named = [w for w in present if w in names]
        anchor = max(named or present, key=lambda w: idf[w], default=None)
        if anchor is None:
            return out, {}
        group, ref = [], None
        for row in out:
            r = row[2]
            m = matched.get(r["id"], set()) - self._common
            if r["kind"] not in self.CLUSTER_KINDS or anchor not in m:
                continue
            if ref is None:
                ref = m
                group.append(row)
            elif len(m & ref) / max(len(m), len(ref)) >= self.CLUSTER_OVERLAP:
                group.append(row)
        if len(group) < 2:
            return out, {}
        best = group[0][0]
        ordered = sorted(group, key=lambda x: x[2]["created"], reverse=True)
        older = {row[2]["id"]: ordered[0][2]["id"] for row in ordered[1:]}
        for k, row in enumerate(ordered):
            row[0] = best - 1e-6 * k
        ids = {id(x) for x in group}
        rest = [x for x in out if id(x) not in ids]
        first = out.index(group[0])
        res = [x for x in out[:first] if id(x) not in ids] + ordered + [x for x in rest if out.index(x) > first]
        return res, older

    def browse(self, shelf=None, limit=50):
        """The shelf tree (with counts) under a shelf, and the items kept directly on it."""
        base = clean_shelf(shelf) if shelf else ""
        rows = self.db.execute("SELECT shelf, COUNT(*) n FROM items WHERE status IN ('active','faded') "
                               "GROUP BY shelf").fetchall()
        tree = {}
        for r in rows:
            s = r["shelf"]
            if base and not (s == base or s.startswith(base + "/")):
                continue
            rest = s[len(base):].lstrip("/") if base else s
            child = rest.split("/")[0] if rest else ""
            if child:
                tree[child] = tree.get(child, 0) + r["n"]
        items = []
        if base:
            items = [self._row(r) for r in self.db.execute(
                "SELECT * FROM items WHERE shelf=? AND status IN ('active','faded') ORDER BY updated DESC LIMIT ?",
                (base, limit)).fetchall()]
        self.log("browse", None, shelf=base)
        return {"shelf": base or "/", "shelves": [{"name": k, "path": (base + "/" + k).strip("/"), "items": v}
                                                    for k, v in sorted(tree.items())],
                "items": [{"id": i["id"], "kind": i["kind"], "title": i["title"], "tags": i["tags"]} for i in items]}

    def tasks(self, project=None, status="open", due_before=None, limit=100):
        """Tasks are items of kind 'task' with meta {due, priority, project, done}. Sorted by due, then priority."""
        rows = [self._row(r) for r in self.db.execute(
            "SELECT * FROM items WHERE kind='task' AND status='active'").fetchall()]
        out = []
        for t in rows:
            m = t["meta"]
            done = bool(m.get("done"))
            if status == "open" and done or status == "done" and not done:
                continue
            if project and (m.get("project") or "").lower() != project.lower():
                continue
            if due_before is not None and (m.get("due") is None or m["due"] >= due_before):
                continue
            out.append(t)
        out.sort(key=lambda t: (t["meta"].get("due") if t["meta"].get("due") is not None else 1e18,
                                -int(t["meta"].get("priority") or 0), t["id"]))
        return out[:limit]

    # ---- upkeep ---------------------------------------------------------------------------------------------------
    def fade(self, days, kinds=("episode", "note")):
        """Items not used for `days` move to the recycle bin ('faded'): left out of nothing, just ranked lower, and
        brought back the moment they are found again. Nothing is deleted."""
        if not days:
            return 0
        cut = self.clock() - days * 86400
        with self.lock:
            cur = self.db.execute(
                f"UPDATE items SET status='faded' WHERE status='active' AND kind IN ({','.join('?' * len(kinds))}) "
                "AND COALESCE(last_used, created) < ? AND strength < 3", list(kinds) + [cut])
            self.db.commit()
        if cur.rowcount:
            self.log("fade", None, n=cur.rowcount, days=days)
        return cur.rowcount

    def stats(self):
        q = lambda s, a=(): self.db.execute(s, a).fetchone()[0]
        kinds = {r[0]: r[1] for r in self.db.execute(
            "SELECT kind, COUNT(*) FROM items WHERE status IN ('active','faded') GROUP BY kind")}
        return {"items": q("SELECT COUNT(*) FROM items WHERE status IN ('active','faded')"),
                "versions": q("SELECT COUNT(*) FROM items"),
                "faded": q("SELECT COUNT(*) FROM items WHERE status='faded'"),
                "shelves": q("SELECT COUNT(DISTINCT shelf) FROM items WHERE status IN ('active','faded')"),
                "events": q("SELECT COUNT(*) FROM events"), "runs": q("SELECT COUNT(*) FROM runs"),
                "by_kind": kinds, "fts": self.fts}

    # ---- test runs ------------------------------------------------------------------------------------------------
    def save_run(self, run):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO runs(id,ts,suite,arm,model,seed,status,config,summary,details) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (run["id"], run["ts"], run["suite"], run["arm"], run.get("model"), run.get("seed"),
                             run["status"], json.dumps(run.get("config", {})), json.dumps(run.get("summary", {})),
                             json.dumps(run.get("details", []))[:2_000_000]))
            if run["status"] == "done":
                self.db.execute("DELETE FROM metrics WHERE run_id=?", (run["id"],))
                self.db.executemany(
                    "INSERT INTO metrics(run_id,ts,suite,arm,model,name,value) VALUES(?,?,?,?,?,?,?)",
                    [(run["id"], run["ts"], run["suite"], run["arm"], run.get("model"), k, float(v))
                     for k, v in run.get("summary", {}).items() if isinstance(v, (int, float))])
            self.db.commit()

    def runs(self, limit=50, suite=None):
        sql, a = "SELECT id,ts,suite,arm,model,seed,status,config,summary FROM runs", []
        if suite:
            sql += " WHERE suite=?"
            a.append(suite)
        out = []
        for r in self.db.execute(sql + " ORDER BY ts DESC LIMIT ?", a + [limit]):
            d = dict(r)
            d["config"], d["summary"] = json.loads(d["config"] or "{}"), json.loads(d["summary"] or "{}")
            out.append(d)
        return out

    def run(self, run_id):
        r = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("config", "summary", "details"):
            d[k] = json.loads(d[k] or ("[]" if k == "details" else "{}"))
        return d

    def trend(self, suite, name, arm=None, limit=200):
        sql, a = "SELECT ts, arm, model, value, run_id FROM metrics WHERE suite=? AND name=?", [suite, name]
        if arm:
            sql += " AND arm=?"
            a.append(arm)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY ts DESC LIMIT ?", a + [limit])][::-1]
