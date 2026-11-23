"""Check-ins by time, not by day (0.31): log how you feel as often as it changes, plus one-tap water and coffee.

A check-in is any of: head fog 0-5, energy (low / ok / high), a few "also noticing" tags (headache, can't focus...)
and a short note. Each is kept with the time it was for (now, or "30 min ago"), so the day becomes a line, not one
number. Those with a fog rating are filed on the body/fog shelf with the fog in meta, so the patterns (patterns.py)
keep working: a day's fog there is the latest rating that day. The rest go on body/checkins. Each check-in also
answers the day's good / low / rough check-in (day.py), so the ask budget follows how the day is actually going.

Quick logs: water is a count per day; coffee is filed as a drink on the meals shelf with caffeine set, so the
patterns' "caffeine after 2 PM" factor sees it.
"""
import json
import math
import time
from datetime import datetime

from .day import today as day_of
from .speakers import VIEWER, visible

SHELF = "body/fog"              # check-ins with a fog rating (the patterns read the newest one each day)
OTHER = "body/checkins"         # check-ins without one (energy, tags or a note only)
ENERGY = ("low", "ok", "high")
ENERGY_LABEL = {"low": "Low", "ok": "OK", "high": "High"}
TAGS = ["Headache", "Can't focus", "Tired eyes", "Anxious", "Hungry"]
FOG_WORD = ["clear", "slight", "mild", "noticeable", "heavy", "can't think"]
MAX_BACK = 36 * 3600            # a check-in can be for earlier, but not for days ago
LOG_KV = "quick_logs"
LOG_KINDS = ("water", "coffee")


def _meta(r):
    m = r["meta"]
    return json.loads(m or "{}") if isinstance(m, str) else (m or {})


def _clock_text(t):
    d = datetime.fromtimestamp(t)
    return f"{(d.hour % 12) or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def state_for(fog=None, energy=None):
    """The day's good / low / rough from one check-in."""
    if fog is not None and fog >= 4 or (energy == "low" and fog is not None and fog >= 3):
        return "rough"
    if energy == "low" or (fog is not None and fog >= 3):
        return "low"
    return "good"


class Checkins:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or time.time

    # ---- check-ins -------------------------------------------------------------------------------------------------
    def add(self, fog=None, energy=None, tags=None, note="", at=None, minutes_ago=None, person=None):
        """Keep one check-in. Returns it, with how many there are that day."""
        now = self.clock()
        if fog is not None and fog != "":
            if isinstance(fog, bool):
                raise ValueError("fog is a number from 0 to 5")
            try:
                fog_f = float(fog)
            except (TypeError, ValueError):
                raise ValueError("fog is a number from 0 to 5") from None
            if not math.isfinite(fog_f):
                raise ValueError("fog is a number from 0 to 5")
            fog = int(round(fog_f))
            if not 0 <= fog <= 5:
                raise ValueError("fog is a number from 0 to 5")
        else:
            fog = None
        energy = (energy or "").strip().lower() or None
        if energy is not None and energy not in ENERGY:
            raise ValueError("energy is low, ok or high")
        if tags is None:
            tags = []
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValueError("tags is a list of words")
        tags = [t.strip()[:40] for t in tags if t.strip()][:8]
        note = str(note or "").strip()[:500]
        if fog is None and energy is None and not tags and not note:
            raise ValueError("a check-in needs at least one of fog, energy, a tag or a note")
        if at not in (None, ""):
            if isinstance(at, bool):
                raise ValueError("at is a time in seconds since 1970")
            try:
                t = float(at)
            except (TypeError, ValueError):
                raise ValueError("at is a time in seconds since 1970") from None
            if not math.isfinite(t):
                raise ValueError("at is a time in seconds since 1970")
        elif minutes_ago not in (None, ""):
            if isinstance(minutes_ago, bool):
                raise ValueError("minutes_ago is a number")
            try:
                ma = float(minutes_ago)
            except (TypeError, ValueError):
                raise ValueError("minutes_ago is a number") from None
            if not math.isfinite(ma):
                raise ValueError("minutes_ago is a number")
            t = now - ma * 60
        else:
            t = now
        if t > now + 60:
            raise ValueError("a check-in can't be for later")
        if now - t > MAX_BACK:
            raise ValueError("a check-in can be for up to a day and a half ago")
        parts = []
        if fog is not None:
            parts.append(f"head fog {fog}/5 ({FOG_WORD[fog]})")
        if energy:
            parts.append(f"energy {ENERGY_LABEL[energy]}")
        if tags:
            parts.append(", ".join(x.lower() for x in tags))
        when = datetime.fromtimestamp(t)
        text = f"Check-in {when:%a %b %d}, {_clock_text(t)}: " + ("; ".join(parts) or "note") + "."
        if note:
            text += f" Note: {note}"
        meta = {"checkin": True, "day": day_of(t), "at": t, "tags": tags}
        if fog is not None:
            meta["fog"] = fog
        if energy:
            meta["energy"] = energy
        if note:
            meta["note"] = note
        who = person if person is not None else VIEWER.get()
        if who:
            meta["scope"] = "personal"
            meta["owner"] = who
        title = "Check-in" + (f" · fog {fog}/5" if fog is not None else "") + (f" · {ENERGY_LABEL[energy]}" if energy
                                                                            else "")
        iid = self.store.remember(text=text, title=title, shelf=SHELF if fog is not None else OTHER,
                                  source="check-in", meta=meta, at=t)
        iid = iid.get("id") if isinstance(iid, dict) else iid
        n = len(self.entries(day=meta["day"], person=who))
        return {"id": iid, "at": t, "day": meta["day"], "fog": fog, "energy": energy, "tags": tags, "note": note,
                "nth": n, "state": state_for(fog, energy), "time": _clock_text(t)}

    def entries(self, day=None, since=None, person=None, limit=200):
        """Check-ins, newest first. A fog rating from the old once-a-day check-in counts as one with fog only."""
        q = "SELECT id, meta, created FROM items WHERE shelf IN (?, ?) AND status='active'"
        args = [SHELF, OTHER]
        if since is not None:
            q += " AND created >= ?"
            args.append(since)
        elif day is not None:
            d0 = datetime.fromisoformat(day).replace(hour=5).timestamp()
            q += " AND created >= ? AND created < ?"
            args += [d0 - 3600 * 2, d0 + 86400 + 3600 * 2]
        q += " ORDER BY created DESC LIMIT ?"
        args.append(int(limit))
        out = []
        viewer = person if person is not None else VIEWER.get()
        who = viewer or ""
        for r in self.store.db.execute(q, args):
            m = _meta(r)
            if not visible(m, viewer):
                continue
            if who and m.get("owner") and str(m["owner"]).lower() != who.lower():
                continue
            if viewer == "" and m.get("owner"):
                continue
            t = float(m.get("at") or r["created"])
            d = m.get("day") or day_of(t)
            if day is not None and d != day:
                continue
            fog = m.get("fog")
            out.append({"id": r["id"], "at": t, "day": d, "time": _clock_text(t),
                        "fog": int(round(float(fog))) if fog is not None else None, "energy": m.get("energy"),
                        "tags": m.get("tags") or [], "note": m.get("note") or ""})
        out.sort(key=lambda e: -e["at"])
        return out

    def latest(self, person=None):
        """The newest check-in today, or None."""
        e = self.entries(day=day_of(self.clock()), person=person)
        return e[0] if e else None

    def series(self, days=7, person=None):
        """Per day for the last `days` days, oldest first: the highest fog, the last fog, the lowest energy (1 low,
        2 ok, 3 high) and how many check-ins there were."""
        from datetime import timedelta
        end = datetime.fromisoformat(day_of(self.clock())).date()
        since = datetime.combine(end - timedelta(days=days), datetime.min.time()).timestamp()
        ent = self.entries(since=since, person=person, limit=2000)
        out = []
        for i in range(days - 1, -1, -1):
            d = (end - timedelta(days=i)).isoformat()
            es = [e for e in ent if e["day"] == d]
            fogs = [e["fog"] for e in es if e["fog"] is not None]
            en = [ENERGY.index(e["energy"]) + 1 for e in es if e["energy"] in ENERGY]
            out.append({"day": d, "fog": max(fogs) if fogs else None, "fog_last": fogs[0] if fogs else None,
                        "energy": min(en) if en else None, "n": len(es)})
        return out

    # ---- quick logs ------------------------------------------------------------------------------------------------
    def _logs(self):
        d = dict(self.store.kv_get(LOG_KV) or {})
        return {k: d[k] for k in sorted(d)[-14:]}

    def _pkey(self, person):
        return ((person if person is not None else VIEWER.get()) or "_").strip().lower() or "_"

    def coffees(self, day=None, person=None):
        day = day or day_of(self.clock())
        d0 = datetime.fromisoformat(day).replace(hour=5).timestamp()
        viewer = person if person is not None else VIEWER.get()
        n = 0
        for r in self.store.db.execute("SELECT meta FROM items WHERE shelf='body/meals' AND status='active' AND "
                                       "created >= ? AND created < ?", (d0, d0 + 86400)):
            m = _meta(r)
            if m.get("drink") == "coffee" and visible(m, viewer):
                if viewer == "" and m.get("owner"):
                    continue
                if viewer and m.get("owner") and str(m["owner"]).lower() != viewer.lower():
                    continue
                n += 1
        return n

    def counts(self, day=None, person=None):
        day = day or day_of(self.clock())
        w = ((self._logs().get(day) or {}).get(self._pkey(person)) or {}).get("water", 0)
        return {"water": w, "coffee": self.coffees(day, person)}

    def log(self, kind, n=1, person=None):
        """One more glass of water or cup of coffee (n=-1 takes the last one back). Returns today's counts."""
        if kind not in LOG_KINDS:
            raise ValueError(f"log one of {', '.join(LOG_KINDS)}")
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise ValueError("n is a whole number") from None
        if not -1 <= n <= 6 or n == 0:
            raise ValueError("n is -1 (take one back) or 1 to 6")
        now = self.clock()
        day = day_of(now)
        if kind == "water":
            allx = self._logs()
            rec = allx.setdefault(day, {}).setdefault(self._pkey(person), {})
            rec["water"] = max(0, int(rec.get("water", 0)) + n)
            self.store.kv_set(LOG_KV, allx)
        elif n > 0:
            who = person if person is not None else VIEWER.get()
            for _ in range(n):
                meta = {"drink": "coffee", "caffeine": True, "at": now, "protein_g": 0}
                if who:
                    meta["scope"] = "personal"
                    meta["owner"] = who
                self.store.remember(text=f"Coffee at {_clock_text(now)} ({datetime.fromtimestamp(now):%a %b %d}).",
                                    title="Coffee", shelf="body/meals", source="quick-log", meta=meta, at=now)
        else:
            d0 = datetime.fromisoformat(day).replace(hour=5).timestamp()
            viewer = person if person is not None else VIEWER.get()
            row = None
            for r in self.store.db.execute("SELECT id, meta FROM items WHERE shelf='body/meals' AND status='active' "
                                           "AND created >= ? ORDER BY created DESC", (d0,)):
                m = _meta(r)
                if m.get("drink") == "coffee" and visible(m, viewer):
                    if viewer == "" and m.get("owner"):
                        continue
                    if viewer and m.get("owner") and str(m["owner"]).lower() != viewer.lower():
                        continue
                    row = r
                    break
            if row is not None:
                self.store.set_status(row["id"], "archived")
        return self.counts(day, person)


__all__ = ["Checkins", "ENERGY", "TAGS", "FOG_WORD", "state_for", "LOG_KINDS"]
