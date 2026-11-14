"""Weigh-ins from the scale, kept in memory. Every new weigh-in (sensor.<person>_last_weigh_in changes) becomes one
note on the body/weight shelf, dated when it happened: "Weigh-in Fri Nov 13 9:53 AM: weight 167.8 lb, body fat
23.1%, ...". At start-up the last 10 days of Home Assistant history are filed too, so nothing measured while the app
was off is missed. Notes have no key, so every weigh-in stays findable ("what did I weigh on Nov 11?")."""
import logging
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger("homemind.body")

FIELDS = [("weight", "weight", "lb"), ("body_fat", "body fat", "%"), ("bmi", "BMI", ""),
          ("muscle_mass", "muscle mass", "lb"), ("body_water", "body water", "%"),
          ("visceral_fat_level", "visceral fat level", ""), ("basal_metabolic_rate", "BMR", " kcal"),
          ("metabolic_age", "metabolic age", "")]
KG_LB = 2.20462


def _num(v, unit):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if unit == "kg":
        x *= KG_LB
    return x


def note_text(when, vals):
    parts = []
    for k, label, unit in FIELDS:
        if vals.get(k) is not None:
            x = vals[k]
            s = f"{x:.1f}" if k in ("weight", "muscle_mass", "body_fat", "body_water", "bmi") else f"{x:.0f}"
            parts.append(f"{label} {s}{unit}" if unit in ("%", "") else f"{label} {s} {unit.strip()}")
    day = when.strftime("%a %b %d %Y %I:%M %p").replace(" 0", " ")
    return f"Weigh-in {day}: my " + ", ".join(parts) + "."


class Body:
    def __init__(self, store, ha, person="yitz"):
        self.store, self.ha, self.p = store, ha, person
        self.last = None

    def ent(self, k):
        return f"sensor.{self.p}_{k}"

    def logged(self, ts):
        return self.store.db.execute("SELECT 1 FROM items WHERE shelf='body/weight' AND meta LIKE ?",
                                     (f'%"weigh_in": "{ts}"%',)).fetchone() is not None

    def file(self, ts, vals):
        if vals.get("weight") is None or self.logged(ts):
            return None
        when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        text = note_text(when, vals)
        out = self.store.remember(text=text, title=text.split(": my ")[0] + f": {vals['weight']:.1f} lb",
                                  kind="fact", shelf="body/weight",
                                  tags=["weight", "scale", "body"], source="scale",
                                  meta={"weigh_in": ts, "series": "weigh-in", **{k: round(v, 2) for k, v in vals.items() if v is not None}},
                                  at=when.timestamp())
        log.info("filed weigh-in %s as #%s", ts, out.get("id"))
        return out

    async def poll(self):
        """Called every minute: file the newest weigh-in if it is new."""
        s = await self.ha.state(self.ent("last_weigh_in"))
        ts = s and s.get("state")
        if not ts or ts in ("unknown", "unavailable") or ts == self.last:
            return None
        self.last = ts
        if self.logged(ts):
            return None
        vals = {}
        for k, _, _ in FIELDS:
            st = await self.ha.state(self.ent(k))
            if st:
                vals[k] = _num(st.get("state"), (st.get("attributes") or {}).get("unit_of_measurement"))
        return self.file(ts, vals)

    async def backfill(self, days=10):
        """File every weigh-in Home Assistant still has in its history."""
        ids = [self.ent("last_weigh_in")] + [self.ent(k) for k, _, _ in FIELDS]
        start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        hist = await self.ha.history(ids, start)
        if not hist:
            return 0
        rows = {}
        for series in hist:
            if not series:
                continue
            eid = series[0]["entity_id"]
            unit = None
            pts = []
            for r in series:
                unit = (r.get("attributes") or {}).get("unit_of_measurement", unit)
                t = datetime.fromisoformat(r["last_changed"].replace("Z", "+00:00")).timestamp()
                pts.append((t, r["state"], unit))
            rows[eid] = pts
        n = 0
        for t_in, ts, _ in rows.get(self.ent("last_weigh_in"), []):
            if ts in ("unknown", "unavailable", None):
                continue
            vals = {}
            for k, _, _ in FIELDS:
                # the value the scale reported for this weigh-in: the latest one within two minutes of it
                cand = [(t, v, u) for t, v, u in rows.get(self.ent(k), []) if t <= t_in + 120 and v not in ("unknown", "unavailable")]
                if cand and t_in - max(cand)[0] < 6 * 3600:
                    t, v, u = max(cand)
                    vals[k] = _num(v, u)
            if self.file(ts, vals):
                n += 1
        return n
