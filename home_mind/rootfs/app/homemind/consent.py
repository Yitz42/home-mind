"""New behaviours are agreed on a clear day, with 24 hours to change your mind (design spec, Throughout; open
decision 4's starting value; phase 10).

A clear day, for a person: a "good" check-in today, no "rough" day in the past 2 days, before 8 pm, and at most one
consent ask already today. An unknown check-in is never clear. Safety never waits for one.

How a change goes:
  proposed   something new is waiting for a yes (an automation to switch on, an experiment, a feature)
  waiting    a yes came on a day that wasn't clear: it is asked again on the next clear day
  agreed     a yes on a clear day; it takes effect 24 hours later
  done       it took effect
  cancelled  "changed my mind" before it took effect, or a no
Each change names what to do when it takes effect (a kind the app knows how to apply); nothing else can be run.
"""
import json
import time
from datetime import date, datetime, timedelta

WAIT = 24 * 3600
LATEST_HOUR = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS consent (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, person TEXT, title TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL, created REAL NOT NULL, agreed REAL, effective REAL, done REAL, note TEXT);
"""


class Consent:
    def __init__(self, store, day=None, clock=None):
        self.store = store
        self.day = day
        self.clock = clock or time.time
        self.appliers = {}            # kind -> async f(payload) -> result
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()

    # ---- the clear day ----------------------------------------------------------------------------------------
    def clear_day(self, person=None):
        """(bool, why)."""
        if self.day is None:
            return False, "no check-in"
        now = datetime.fromtimestamp(self.clock())
        if now.hour >= LATEST_HOUR:
            return False, "it's late in the day"
        state = self.day.get(person)
        if state != "good":
            return False, "today's check-in isn't 'good'" if state != "unknown" else "no check-in today"
        allx = self.day._all()
        key = self.day._key(person)
        for back in (1, 2):
            d = (now.date() - timedelta(days=back)).isoformat()
            r = (allx.get(d) or {}).get(key) or {}
            if r.get("state") == "rough":
                return False, "a rough day in the past two days"
        asked = self.store.db.execute("SELECT COUNT(*) FROM consent WHERE agreed>=?",
                                      (datetime.combine(now.date(), datetime.min.time()).timestamp(),)).fetchone()[0]
        if asked >= 1:
            return False, "one yes is enough for one day"
        return True, ""

    # ---- changes ----------------------------------------------------------------------------------------------
    def propose(self, kind, title, payload=None, person=None):
        if kind not in self.appliers:
            raise ValueError(f"unknown kind of change: {kind}")
        with self.store.lock:
            cur = self.store.db.execute("INSERT INTO consent (kind, person, title, payload, status, created) VALUES "
                                        "(?,?,?,?,?,?)", (kind, person, title, json.dumps(payload or {}),
                                                          "proposed", self.clock()))
            self.store.db.commit()
        return self.get(cur.lastrowid)

    def get(self, cid):
        r = self.store.db.execute("SELECT * FROM consent WHERE id=?", (cid,)).fetchone()
        return dict(r, payload=json.loads(r["payload"] or "{}")) if r else None

    def list(self, status=None):
        q = "SELECT * FROM consent" + (" WHERE status=?" if status else "") + " ORDER BY id DESC LIMIT 100"
        return [dict(r, payload=json.loads(r["payload"] or "{}"))
                for r in self.store.db.execute(q, (status,) if status else ())]

    def _set(self, cid, **kw):
        cols = ", ".join(f"{k}=?" for k in kw)
        with self.store.lock:
            self.store.db.execute(f"UPDATE consent SET {cols} WHERE id=?", (*kw.values(), cid))
            self.store.db.commit()

    def yes(self, cid, person=None):
        c = self.get(cid)
        if not c or c["status"] not in ("proposed", "waiting"):
            raise ValueError("nothing waiting for a yes with that id")
        ok, why = self.clear_day(person if person is not None else c.get("person"))
        if not ok:
            self._set(cid, status="waiting", note=f"asked again on a clear day ({why})")
            return dict(self.get(cid), why=why)
        now = self.clock()
        self._set(cid, status="agreed", agreed=now, effective=now + WAIT, note=None)
        return self.get(cid)

    def no(self, cid, note="said no"):
        c = self.get(cid)
        if not c or c["status"] in ("done", "cancelled"):
            raise ValueError("nothing to cancel with that id")
        self._set(cid, status="cancelled", note=note)
        return self.get(cid)

    def change_mind(self, cid):
        c = self.get(cid)
        if not c or c["status"] != "agreed":
            raise ValueError("only an agreed change that hasn't taken effect can be undone this way")
        return self.no(cid, "changed my mind within 24 hours")

    def to_ask(self, person=None):
        """Waiting changes to ask about again: only on a clear day."""
        if not self.clear_day(person)[0]:
            return []
        return [c for c in self.list("waiting") if not c.get("person") or c["person"] == person] + \
            [c for c in self.list("proposed") if not c.get("person") or c["person"] == person]

    async def due(self):
        """Agreed changes whose 24 hours are up: applied. Returns what happened."""
        out = []
        now = self.clock()
        for c in self.list("agreed"):
            if c["effective"] and c["effective"] <= now:
                f = self.appliers.get(c["kind"])
                try:
                    res = await f(c["payload"]) if f else {"error": "no longer known"}
                    self._set(c["id"], status="done", done=now, note=None if not (res or {}).get("error")
                              else str(res["error"])[:200])
                except Exception as e:  # noqa: BLE001
                    self._set(c["id"], status="done", done=now, note=f"failed: {e}"[:200])
                    res = {"error": str(e)}
                out.append({"id": c["id"], "kind": c["kind"], "result": res})
        return out


def today_iso(clock=time.time):
    return date.fromtimestamp(clock()).isoformat()
