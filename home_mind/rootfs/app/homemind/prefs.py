"""Learn other preferences the way comfort learns temperatures (#30): how bright you like each light, how fast each
fan, how far each blind and how loud each speaker, by time of day and weekday or weekend, from the settings you
choose yourself.

Each time a person sets one of these (not an automation, not Home Mind itself), the setting is kept with its
situation. A situation with enough settings that agree (at least MIN_N, spread at most MAX_SPREAD points) becomes a
learned preference:
- filed as a note ("Desk lamp: usually 40% on weekday evenings"), so questions and the model see it;
- used when you ask to turn something on without saying how much (option `use_preferences`).
"""
import statistics
import time
from datetime import datetime, timedelta

DOMAINS = {"light": "brightness", "fan": "percentage", "cover": "current_position", "media_player": "volume_level"}
ACTION = {"light": "brightness", "fan": "speed"}
MIN_N = 5
MAX_SPREAD = 15.0         # median absolute deviation, in points (percent)
DAYS = 60
OWN_SECONDS = 600         # a change right after Home Mind acted on the thing is Home Mind's
BANDS = ((5, "morning"), (12, "afternoon"), (17, "evening"), (22, "night"))


def band(ts):
    d = datetime.fromtimestamp(ts)
    part = "night"
    for h, name in BANDS:
        if d.hour >= h:
            part = name
    if d.hour < 5:
        part = "night"
    # a night is one situation from 22:00 to 05:00, named for the day it ends on: Friday 23:30 and Saturday 00:30 are
    # both a weekend night, Sunday 23:30 and Monday 00:30 both a weekday night (not split at midnight)
    wd = (d + timedelta(days=1)).weekday() if d.hour >= 22 else d.weekday()
    return ("weekend " if wd >= 5 else "weekday ") + part


def value_of(dom, attrs, state):
    if state in ("off", "closed", "unavailable", "unknown") and dom != "cover":
        return None
    v = (attrs or {}).get(DOMAINS[dom])
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if dom == "light":
        v = v / 2.55
    elif dom == "media_player":
        v = v * 100
    return round(v, 1)


class Prefs:
    def __init__(self, store):
        self.store = store
        self.last = {}
        with store.lock:
            store.db.execute("CREATE TABLE IF NOT EXISTS pref_obs (eid TEXT, ts REAL, value REAL, band TEXT)")
            store.db.execute("CREATE INDEX IF NOT EXISTS pref_obs_eid ON pref_obs(eid, band, ts)")
            store.db.commit()

    def observe(self, catalog, own=None, now=None):
        """catalog: entity id -> {domain, state, attrs, ctx}; own: entity id -> when Home Mind last acted on it."""
        now = now or time.time()
        own = own or {}
        added = 0
        for eid, c in catalog.items():
            dom = c.get("domain") or eid.split(".")[0]
            if dom not in DOMAINS:
                continue
            v = value_of(dom, c.get("attrs"), c.get("state"))
            prev = self.last.get(eid)
            self.last[eid] = v
            if v is None or prev is None or abs(v - prev) < 3:
                continue        # first sight, off, or no real change
            ctx = c.get("ctx") or {}
            if ctx.get("parent_id") or now - own.get(eid, 0) < OWN_SECONDS:
                continue        # an automation's or Home Mind's own change
            with self.store.lock:
                self.store.db.execute("INSERT INTO pref_obs VALUES(?,?,?,?)", (eid, now, v, band(now)))
                self.store.db.commit()
            added += 1
        return added

    def learned(self, now=None):
        now = now or time.time()
        rows = self.store.db.execute("SELECT eid, ts, value FROM pref_obs WHERE ts > ? ORDER BY ts",
                                     (now - DAYS * 86400,)).fetchall()
        groups = {}
        for r in rows:          # the situation from the time, so settings kept before a change to band() still count
            groups.setdefault((r["eid"], band(r["ts"])), []).append(r["value"])
        out = []
        for (eid, b), vs in groups.items():
            vs = vs[-30:]
            if len(vs) < MIN_N:
                continue
            med = statistics.median(vs)
            mad = statistics.median(abs(v - med) for v in vs)
            if mad > MAX_SPREAD:
                continue
            out.append({"entity_id": eid, "band": b, "value": int(round(med)), "n": len(vs), "spread": round(mad, 1)})
        return sorted(out, key=lambda x: (x["entity_id"], x["band"]))

    def preferred(self, eid, now=None):
        now = now or time.time()
        b = band(now)
        for p in self.learned(now):
            if p["entity_id"] == eid and p["band"] == b:
                return p
        return None

    def file_notes(self, names=None, now=None):
        """One note per learned preference, kept up to date by key (a changed preference replaces the old note)."""
        names = names or {}
        n = 0
        keep = set()
        with self.store.lock:       # settings older than DAYS are never read again: don't keep them for ever
            self.store.db.execute("DELETE FROM pref_obs WHERE ts < ?", ((now or time.time()) - DAYS * 86400,))
            self.store.db.commit()
        for p in self.learned(now):
            dom = p["entity_id"].split(".")[0]
            what = {"light": "brightness", "fan": "speed", "cover": "open", "media_player": "volume"}[dom]
            name = names.get(p["entity_id"]) or p["entity_id"]
            text = f"{name}: usually {p['value']}% {what} on {p['band']}s (learned from {p['n']} settings)."
            key = f"preference|{p['entity_id']}|{p['band']}"
            keep.add(key)
            old = self.store.active_by_key(key)
            if old and old["body"].split(" (learned")[0] == text.split(" (learned")[0]:
                continue
            self.store.remember(text=text, kind="fact", shelf="home/preferences", key=key, tags=["preference", dom],
                                source="prefs", meta={"category": "preference", "entity_id": p["entity_id"],
                                                      "band": p["band"], "value": p["value"]})
            n += 1
        # a preference that no longer holds (the settings stopped agreeing, or are older than DAYS) stops being told as
        # one: its note is archived, as habits no longer found are
        for r in self.store.db.execute("SELECT id, key FROM items WHERE status IN ('active','faded') AND source='prefs' "
                                       "AND key LIKE 'preference|%'").fetchall():
            if r["key"] not in keep:
                self.store.set_status(r["id"], "archived")
        return n
