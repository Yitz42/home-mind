"""The outbox: every phone message is sent once, retried if it failed, and given up on when it's no longer useful
(design spec, Throughout: "an outbox so every message is sent once, with delivery confirmation and expiry").

Each push is a row: who it's for, what it says, its tier, when it expires, and what happened.
  sent       Home Assistant accepted it (its answer is the delivery confirmation Home Mind can get; whether the phone
             showed it is up to the phone)
  held       waiting for quiet hours (or a rough day) to end; kept in the database, so a restart doesn't lose it
  failed     Home Assistant didn't accept it (down, or an error); tried again every minute until it expires
  expired    too late to be useful: safety after 1 hour, important after 6, routine after 12
  duplicate  the same message to the same phone was sent in the last 10 minutes: not sent again
"""
import hashlib
import json
import time

EXPIRES = {"safety": 3600, "important": 6 * 3600, "routine": 12 * 3600}
DEDUP_SECONDS = 600
MAX_TRIES = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, target TEXT NOT NULL, data TEXT NOT NULL, tier TEXT NOT NULL,
  created REAL NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0,
  sent_at REAL, error TEXT);
CREATE INDEX IF NOT EXISTS outbox_status ON outbox(status, created);
CREATE INDEX IF NOT EXISTS outbox_key ON outbox(key, created);
"""


def key_of(target, data):
    d = data or {}
    target = target[len("notify."):] if target.startswith("notify.") else target
    # the whole message, extras included: two approval requests differ by their action ids, so both go
    raw = f"{target}|{json.dumps(d, sort_keys=True, default=str)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


class Outbox:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or time.time
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()

    def duplicate(self, target, data):
        k = key_of(target, data)
        return self.store.db.execute(
            "SELECT 1 FROM outbox WHERE key=? AND status='sent' AND sent_at>=? LIMIT 1",
            (k, self.clock() - DEDUP_SECONDS)).fetchone() is not None

    def add(self, target, data, tier, status):
        now = self.clock()
        with self.store.lock:
            cur = self.store.db.execute(
                "INSERT INTO outbox (key, target, data, tier, created, expires, status) VALUES (?,?,?,?,?,?,?)",
                (key_of(target, data), target, json.dumps(data or {}), tier, now, now + EXPIRES.get(tier, 43200),
                 status))
            self.store.db.commit()
        return cur.lastrowid

    def mark(self, oid, status, error=None):
        with self.store.lock:
            self.store.db.execute(
                "UPDATE outbox SET status=?, tries=tries+?, sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END, "
                "error=? WHERE id=?", (status, 1 if status in ("sent", "failed") else 0, status, self.clock(),
                                       (error or None) and str(error)[:200], oid))
            self.store.db.commit()

    def take(self, status):
        """Rows waiting in a status (oldest first); expired ones are marked so on the way."""
        now = self.clock()
        out = []
        for r in self.store.db.execute("SELECT * FROM outbox WHERE status=? ORDER BY created", (status,)).fetchall():
            if r["expires"] < now or r["tries"] >= MAX_TRIES:
                self.mark(r["id"], "expired")
                continue
            out.append(dict(r, data=json.loads(r["data"] or "{}")))
        return out

    def stats(self, days=7):
        since = self.clock() - days * 86400
        rows = self.store.db.execute("SELECT status, COUNT(*) n FROM outbox WHERE created>=? GROUP BY status",
                                     (since,)).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def prune(self, days=30):
        with self.store.lock:
            self.store.db.execute("DELETE FROM outbox WHERE created<?", (self.clock() - days * 86400,))
            self.store.db.commit()
