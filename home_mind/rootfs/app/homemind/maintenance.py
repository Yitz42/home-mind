"""Home maintenance, warranties, and recurring chores (House › Upkeep).

Tracks recurring maintenance items (furnace filter, car oil, water filter, smoke detector batteries),
warranty expiration dates, and linked manual/receipt documents. Marking an item done logs a note on
`home/maintenance` and automatically advances its next due date.
"""
import math
import time
from datetime import date, datetime, timedelta

CATEGORIES = {"hvac", "appliance", "auto", "yard", "plumbing", "safety", "general"}


class Maintenance:
    def __init__(self, store, docs=None):
        self.store = store
        self.docs = docs
        with store.lock:
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS maintenance ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "name TEXT NOT NULL,"
                "category TEXT DEFAULT 'general',"
                "every_days INTEGER DEFAULT 90,"
                "last_done REAL,"
                "next_due TEXT,"
                "warranty_until TEXT DEFAULT '',"
                "doc_name TEXT DEFAULT '',"
                "notes TEXT DEFAULT '',"
                "active INTEGER NOT NULL DEFAULT 1,"
                "created REAL NOT NULL)"
            )
            store.db.commit()

    @staticmethod
    def _opt_date(val, field):
        s = str(val or "").strip()
        if not s:
            return ""
        try:
            return date.fromisoformat(s).isoformat()
        except ValueError:
            raise ValueError(f"{field} should be YYYY-MM-DD") from None

    def _row(self, r, today):
        nd = r["next_due"] or ""
        days_left = None
        state = "ok"
        if nd:
            try:
                days_left = (date.fromisoformat(nd) - today).days
                state = "overdue" if days_left < 0 else ("soon" if days_left <= 14 else "ok")
            except ValueError:
                pass
        w_until = r["warranty_until"] or ""
        w_days = None
        if w_until:
            try:
                w_days = (date.fromisoformat(w_until) - today).days
            except ValueError:
                pass
        return {
            "id": r["id"],
            "name": r["name"],
            "category": r["category"] or "general",
            "every_days": r["every_days"] or 0,
            "last_done": datetime.fromtimestamp(r["last_done"]).date().isoformat() if r["last_done"] else None,
            "next_due": nd or None,
            "days_left": days_left,
            "state": state,
            "warranty_until": w_until or None,
            "warranty_days": w_days,
            "doc_name": r["doc_name"] or "",
            "notes": r["notes"] or "",
        }

    def list(self, now=None):
        today = datetime.fromtimestamp(now or time.time()).date()
        rows = self.store.db.execute("SELECT * FROM maintenance WHERE active=1").fetchall()
        items = [self._row(r, today) for r in rows]
        items.sort(key=lambda x: (99999 if x["days_left"] is None else x["days_left"], x["name"].lower()))
        doc_names = []
        if self.docs is not None:
            try:
                doc_names = [d["name"] for d in self.docs.list()][:40]
            except Exception:  # noqa: BLE001
                doc_names = []
        return {
            "items": items,
            "overdue": sum(1 for x in items if x["state"] == "overdue"),
            "soon": sum(1 for x in items if x["state"] == "soon"),
            "docs": doc_names,
        }

    def add(self, name, category="general", every_days=90, next_due=None, warranty_until=None, doc_name="", notes="", now=None):
        nm = str(name or "").strip()[:120]
        if not nm:
            raise ValueError("give a maintenance item name")
        cat = str(category or "general").strip().lower()
        if cat not in CATEGORIES:
            cat = "general"
        if isinstance(every_days, bool):
            raise ValueError("every_days must be a number")
        try:
            ev_f = float(every_days if every_days not in (None, "") else 90)
        except (TypeError, ValueError):
            raise ValueError("every_days must be a number") from None
        if not math.isfinite(ev_f) or not (0 <= ev_f <= 3650):
            raise ValueError("every_days must be between 0 and 3650")
        ev = int(round(ev_f))
        ts = now or time.time()
        today = datetime.fromtimestamp(ts).date()
        nd = self._opt_date(next_due, "next_due") or ((today + timedelta(days=ev)).isoformat() if ev > 0 else "")
        wu = self._opt_date(warranty_until, "warranty_until")
        dn = str(doc_name or "").strip()[:120]
        nt = str(notes or "").strip()[:500]
        with self.store.lock:
            cur = self.store.db.execute(
                "INSERT INTO maintenance (name, category, every_days, last_done, next_due, warranty_until, doc_name, notes, active, created) "
                "VALUES (?,?,?,?,?,?,?,?,1,?)",
                (nm, cat, ev, None, nd, wu, dn, nt, ts),
            )
            self.store.db.commit()
            mid = cur.lastrowid
        row = self.store.db.execute("SELECT * FROM maintenance WHERE id=?", (mid,)).fetchone()
        return self._row(row, today)

    def mark_done(self, mid, note="", now=None):
        row = self.store.db.execute("SELECT * FROM maintenance WHERE id=? AND active=1", (int(mid),)).fetchone()
        if not row:
            raise ValueError(f"no maintenance item {mid}")
        ts = now or time.time()
        today = datetime.fromtimestamp(ts).date()
        ev = int(row["every_days"] or 0)
        next_due = (today + timedelta(days=ev)).isoformat() if ev > 0 else ""
        with self.store.lock:
            self.store.db.execute(
                "UPDATE maintenance SET last_done=?, next_due=? WHERE id=?", (ts, next_due, int(mid))
            )
            self.store.db.commit()
        extra = f" ({str(note).strip()})" if str(note or "").strip() else ""
        self.store.remember(
            text=f"Completed maintenance: {row['name']} on {today.isoformat()}{extra}."
                 + (f" Next due {next_due}." if next_due else ""),
            title=f"Maintenance: {row['name']}"[:80], kind="fact", shelf="home/maintenance",
            source="web", tags=["maintenance", row["category"] or "general"],
            meta={"maintenance_id": int(mid), "at": ts, "next_due": next_due}, at=ts,
        )
        updated = self.store.db.execute("SELECT * FROM maintenance WHERE id=?", (int(mid),)).fetchone()
        return self._row(updated, today)

    def delete(self, mid):
        row = self.store.db.execute("SELECT 1 FROM maintenance WHERE id=? AND active=1", (int(mid),)).fetchone()
        if not row:
            raise ValueError(f"no maintenance item {mid}")
        with self.store.lock:
            self.store.db.execute("UPDATE maintenance SET active=0 WHERE id=?", (int(mid),))
            self.store.db.commit()
        return {"removed": int(mid)}


def register_routes(r, A, J, read_body, whole):
    @r.get("/api/maintenance")
    async def maintenance_list(_):
        return J(A.maintenance.list())

    @r.post("/api/maintenance/{op}")
    async def maintenance_op(q):
        op = q.match_info["op"]
        b = await read_body(q, optional=True, name=str, category=str, next_due=str,
                            warranty_until=str, doc_name=str, notes=str, note=str)
        if op == "add":
            return J({"item": A.maintenance.add(
                b.get("name"), category=b.get("category") or "general",
                every_days=b.get("every_days", 90), next_due=b.get("next_due"),
                warranty_until=b.get("warranty_until"), doc_name=b.get("doc_name") or "",
                notes=b.get("notes") or "",
            )})
        if op == "done":
            return J({"item": A.maintenance.mark_done(whole(b.get("id"), "id"), note=b.get("note") or "")})
        if op == "delete":
            return J(A.maintenance.delete(whole(b.get("id"), "id")))
        return J({"error": f"unknown {op}"}, status=404)
