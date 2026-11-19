"""The now board: working memory (design spec step 14 and Records › Now).

A small table of what is true right now, per person and per room, each entry with an expiry. One writer (this class,
called from code); every answer's brief reads a snapshot, so the model sees "NOW:" lines before the notes. Fading is
just the expiry. The model may only propose entries (propose()): typed, short, time-limited, checked here before
anything is written.

What goes on it now:
  checkin     how the day is going (per person)
  home        who is home: each person sees only their own presence; nobody can ask where someone else is
  did         what Home Mind did in the last 15 minutes
  waiting     an approval or a yes it is waiting for
  proposed    the model's own notes: waiting_on ("Anna's answer about Friday"), unsure_which ("which lamp")

Some days, at random per person (about 1 in 7), the board is left out of the brief, so its worth can be measured
against days without it (counted as board_off_days).
"""
import hashlib
import json
import time
from datetime import date

KINDS = ("checkin", "home", "did", "waiting", "waiting_on", "unsure_which", "doing", "note")
MODEL_KINDS = ("waiting_on", "unsure_which", "doing", "note")
MAX_TTL = 24 * 3600
MAX_VALUE = 140
OFF_ONE_IN = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS now_board (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, value TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
  owner TEXT, room TEXT, set_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS now_board_key ON now_board(key, owner);
"""


class NowBoard:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or time.time
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()

    def set(self, key, value, kind, ttl, source="code", owner=None, room=None):
        """Write (or replace) one entry. The only writer."""
        if kind not in KINDS:
            raise ValueError(f"kind is one of {', '.join(KINDS)}")
        value = str(value or "").strip()[:MAX_VALUE]
        if not value:
            return None
        now = self.clock()
        ttl = max(1, min(int(ttl), MAX_TTL))
        with self.store.lock:
            self.store.db.execute("DELETE FROM now_board WHERE key=? AND owner IS ?", (key, owner))
            cur = self.store.db.execute(
                "INSERT INTO now_board (key, value, kind, source, owner, room, set_at, expires_at) VALUES "
                "(?,?,?,?,?,?,?,?)", (key, value, kind, source, owner, room, now, now + ttl))
            self.store.db.commit()
        return cur.lastrowid

    def clear(self, key, owner=None):
        with self.store.lock:
            self.store.db.execute("DELETE FROM now_board WHERE key=? AND owner IS ?", (key, owner))
            self.store.db.commit()

    def propose(self, kind, value, owner=None, ttl=3600):
        """An entry the model suggests: only its own kinds, short, at most a day, no instructions. Returns the id or
        raises ValueError."""
        if kind not in MODEL_KINDS:
            raise ValueError(f"the model may propose only {', '.join(MODEL_KINDS)}")
        from .store import INSTRUCTION
        v = str(value or "").strip()
        if not v or len(v) > MAX_VALUE or INSTRUCTION.search(v):
            raise ValueError("a proposed entry is a short note, not an instruction")
        key = f"{kind}:{hashlib.sha1(v.lower().encode()).hexdigest()[:8]}"
        return self.set(key, v, kind, min(int(ttl), MAX_TTL), source="model", owner=owner)

    def snapshot(self, viewer=None, room=None):
        """What is current, for a viewer: household entries and their own (None = sees all, like the panel)."""
        now = self.clock()
        with self.store.lock:
            self.store.db.execute("DELETE FROM now_board WHERE expires_at < ?", (now - 3600,))
            rows = [dict(r) for r in self.store.db.execute(
                "SELECT * FROM now_board WHERE expires_at >= ? ORDER BY set_at DESC", (now,))]
        out = []
        for r in rows:
            if viewer is not None and r["owner"] and r["owner"].lower() != (viewer or "").lower():
                continue            # someone else's: their check-in, their presence, their notes
            if room and r["room"] and r["room"] != room:
                continue
            out.append(r)
        return out

    def off_today(self, person, day=None):
        """Is today a board-off day for this person (about 1 in 7, fixed per person and day)?"""
        d = (day or date.fromtimestamp(self.clock())).isoformat()
        h = int(hashlib.sha1(f"{(person or '').lower()}|{d}".encode()).hexdigest(), 16)
        return h % OFF_ONE_IN == 0

    def lines(self, viewer=None, room=None):
        """The brief's NOW: block (empty on a board-off day)."""
        if viewer is not None and self.off_today(viewer):
            return ""
        snap = self.snapshot(viewer, room)
        if not snap:
            return ""
        return "NOW (current, expires on its own):\n" + "\n".join(f"- {r['value']}" for r in snap[:12])

    # ---- what code puts there ----------------------------------------------------------------------------------
    def refresh(self, app):
        """Rebuild the code-written entries from the app's state (called every 20 s by the upkeep loop)."""
        day = getattr(app, "day", None)
        people = []
        try:
            people = app.people()
        except Exception:  # noqa: BLE001
            pass
        for p in people:
            if day is not None:
                s = day.get(p["name"])
                if s != "unknown":
                    self.set("checkin", f"{p['name']}'s day: {s}", "checkin", 6 * 3600, owner=p["name"])
            for e in p.get("persons") or []:
                c = (app.home.catalog or {}).get(e) or {}
                if c.get("state"):
                    where = "home" if c["state"] == "home" else "out"
                    self.set("home", f"{p['name']} is {where}", "home", 600, owner=p["name"])
        dlog = getattr(app, "decisions", None)
        if dlog is not None:
            for d in dlog.recent(5, status="done", since=self.clock() - 900):
                name = (app.home.catalog.get(d["entity_id"]) or {}).get("name") or d["entity_id"]
                self.set(f"did:{d['entity_id']}", f"Home Mind {d['action']} the {name} "
                         f"({int((self.clock() - d['ts']) // 60)} min ago)", "did", 900,
                         owner=d.get("person") or None)
        ap = getattr(app, "approvals", None)
        if ap is not None:
            ap.prune()
            for aid, p in list(ap.pending.items()):
                self.set(f"waiting:{aid}", f"Waiting for phone approval to {p['verb']} the {p['name']}", "waiting",
                         120, owner=(p.get("why") or {}).get("person") or None)
        return True

    def dump(self):
        return json.dumps(self.snapshot(), default=str)
