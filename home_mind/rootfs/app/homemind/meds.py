"""Medications, supplements, and daily habit streaks.

Keeps a lightweight catalog of recurring meds/supplements (`kind='med'`) and daily habits (`kind='habit'`)
along with per-day completion logs and streak calculations. Taking a med/supplement also records a note
on `body/meds` so MemGraph and brain-fog patterns can correlate it with sleep and energy.
"""
import json
import time
from datetime import date, datetime, timedelta

from .speakers import VIEWER, visible

KINDS = {"med", "habit"}


class MedsHabits:
    def __init__(self, store):
        self.store = store
        with store.lock:
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS habits ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "name TEXT NOT NULL,"
                "kind TEXT NOT NULL DEFAULT 'med',"
                "dose TEXT DEFAULT '',"
                "schedule TEXT DEFAULT 'morning',"
                "owner TEXT,"
                "scope TEXT DEFAULT 'personal',"
                "active INTEGER NOT NULL DEFAULT 1,"
                "created REAL NOT NULL)"
            )
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS habit_logs ("
                "habit_id INTEGER NOT NULL,"
                "day TEXT NOT NULL,"
                "at REAL NOT NULL,"
                "PRIMARY KEY (habit_id, day))"
            )
            store.db.commit()

    @staticmethod
    def _day_str(day=None, now=None):
        if day:
            try:
                return date.fromisoformat(str(day).strip()).isoformat()
            except ValueError:
                raise ValueError("day should be YYYY-MM-DD") from None
        return datetime.fromtimestamp(now or time.time()).date().isoformat()

    def _visible(self, r):
        return visible({"scope": r["scope"] or "household", "owner": r["owner"]})

    def _own(self, hid):
        r = self.store.db.execute("SELECT * FROM habits WHERE id=? AND active=1", (int(hid),)).fetchone()
        if not r or not self._visible(r):
            raise ValueError(f"no item {hid}")
        return r

    def add(self, name, kind="med", dose="", schedule="morning", scope=None):
        nm = str(name or "").strip()[:100]
        if not nm:
            raise ValueError("give a name")
        kd = str(kind or "med").strip().lower()
        if kd not in KINDS:
            raise ValueError("kind must be med or habit")
        ds = str(dose or "").strip()[:60]
        sc_time = str(schedule or "morning").strip()[:40]
        who = VIEWER.get() or None
        sc = scope if scope in ("personal", "household") else ("personal" if who else "household")
        now = time.time()
        with self.store.lock:
            cur = self.store.db.execute(
                "INSERT INTO habits (name, kind, dose, schedule, owner, scope, active, created) VALUES (?,?,?,?,?,?,1,?)",
                (nm, kd, ds, sc_time, who if sc == "personal" else None, sc, now),
            )
            self.store.db.commit()
            hid = cur.lastrowid
        return {"id": hid, "name": nm, "kind": kd, "dose": ds, "schedule": sc_time, "scope": sc}

    def delete(self, hid):
        self._own(int(hid))
        with self.store.lock:
            self.store.db.execute("UPDATE habits SET active=0 WHERE id=?", (int(hid),))
            self.store.db.commit()
        return {"removed": int(hid)}

    def toggle(self, hid, day=None, done=None, now=None):
        row = self._own(int(hid))
        d = self._day_str(day, now)
        ts = now or time.time()
        existing = self.store.db.execute(
            "SELECT 1 FROM habit_logs WHERE habit_id=? AND day=?", (int(hid), d)
        ).fetchone()
        want = (not bool(existing)) if done is None else bool(done)
        with self.store.lock:
            if want:
                self.store.db.execute(
                    "INSERT OR REPLACE INTO habit_logs (habit_id, day, at) VALUES (?,?,?)",
                    (int(hid), d, ts),
                )
            else:
                self.store.db.execute("DELETE FROM habit_logs WHERE habit_id=? AND day=?", (int(hid), d))
            self.store.db.commit()
        if row["kind"] == "med":
            existing_ids = []
            for mr in self.store.db.execute(
                "SELECT id, meta FROM items WHERE shelf='body/meds' AND status='active'"
            ).fetchall():
                try:
                    mm = json.loads(mr["meta"] or "{}") if isinstance(mr["meta"], str) else (mr["meta"] or {})
                except ValueError:
                    mm = {}
                if mm.get("habit_id") == int(hid) and mm.get("day") == d:
                    existing_ids.append(mr["id"])
            if want and not existing_ids:
                meta = {"at": ts, "day": d, "habit_id": int(hid), "med": row["name"], "dose": row["dose"] or ""}
                if row["owner"] and row["scope"] == "personal":
                    meta.update(scope="personal", owner=row["owner"])
                label = f"{row['name']} ({row['dose']})" if row["dose"] else row["name"]
                self.store.remember(
                    text=f"Took {label} on {d}.",
                    title=f"Med: {label}"[:80], kind="fact", shelf="body/meds",
                    source="web", tags=["med", "health"], meta=meta, at=ts,
                )
            elif not want and existing_ids:
                for mid in existing_ids:
                    self.store.set_status(mid, "archived")
        return {"id": int(hid), "day": d, "done": want, "streak": self._streak(int(hid), d)}

    def _streak(self, hid, ref_day):
        days = {r["day"] for r in self.store.db.execute(
            "SELECT day FROM habit_logs WHERE habit_id=? AND day <= ? ORDER BY day DESC LIMIT 90",
            (int(hid), ref_day),
        )}
        d = date.fromisoformat(ref_day)
        if d.isoformat() not in days:
            d = d - timedelta(days=1)
        n = 0
        while d.isoformat() in days:
            n += 1
            d = d - timedelta(days=1)
        return n

    def dashboard(self, day=None, now=None):
        d = self._day_str(day, now)
        d0 = date.fromisoformat(d)
        week_days = [(d0 - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        rows = self.store.db.execute("SELECT * FROM habits WHERE active=1 ORDER BY kind DESC, schedule, name").fetchall()
        logs = {}
        for r in self.store.db.execute(
            "SELECT habit_id, day FROM habit_logs WHERE day >= ? AND day <= ?", (week_days[0], d)
        ):
            logs.setdefault(r["habit_id"], set()).add(r["day"])
        items = []
        for r in rows:
            if not self._visible(r):
                continue
            done_set = logs.get(r["id"], set())
            items.append({
                "id": r["id"],
                "name": r["name"],
                "kind": r["kind"],
                "dose": r["dose"] or "",
                "schedule": r["schedule"] or "morning",
                "done": d in done_set,
                "streak": self._streak(r["id"], d),
                "week": [wd in done_set for wd in week_days],
            })
        meds = [x for x in items if x["kind"] == "med"]
        habits = [x for x in items if x["kind"] == "habit"]
        return {
            "day": d,
            "week_labels": [date.fromisoformat(wd).strftime("%a")[0] for wd in week_days],
            "items": items,
            "summary": {
                "total": len(items),
                "done": sum(1 for x in items if x["done"]),
                "meds_total": len(meds),
                "meds_done": sum(1 for x in meds if x["done"]),
                "habits_total": len(habits),
                "habits_done": sum(1 for x in habits if x["done"]),
            },
        }


def register_routes(r, A, J, read_body, whole):
    @r.get("/api/habits")
    async def habits_get(q):
        return J(A.meds.dashboard(day=q.query.get("day") or None))

    @r.post("/api/habits/{op}")
    async def habits_op(q):
        op = q.match_info["op"]
        b = await read_body(q, optional=True, name=str, kind=str, dose=str, schedule=str, scope=str, day=str, done=bool)
        if op == "add":
            return J({"item": A.meds.add(b.get("name"), kind=b.get("kind") or "med",
                                         dose=b.get("dose") or "", schedule=b.get("schedule") or "morning",
                                         scope=b.get("scope"))})
        if op == "toggle":
            return J(A.meds.toggle(whole(b.get("id"), "id"), day=b.get("day") or None,
                                   done=b.get("done") if "done" in b else None))
        if op == "delete":
            return J(A.meds.delete(whole(b.get("id"), "id")))
        return J({"error": f"unknown {op}"}, status=404)
