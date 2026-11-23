"""Weigh-ins from the scale, kept in memory. Every new weigh-in (sensor.<person>_last_weigh_in changes) becomes one
note on the body/weight shelf, dated when it happened: "Weigh-in Fri Nov 13 9:53 AM: weight 167.8 lb, body fat
23.1%, ...". At start-up the last 10 days of Home Assistant history are filed too, so nothing measured while the app
was off is missed. Notes have no key, so every weigh-in stays findable ("what did I weigh on Nov 11?")."""
import logging
import math
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger("homemind.body")

FIELDS = [("weight", "weight", "lb"), ("body_fat", "body fat", "%"), ("bmi", "BMI", ""),
          ("muscle_mass", "muscle mass", "lb"), ("body_water", "body water", "%"),
          ("visceral_fat_level", "visceral fat level", ""), ("basal_metabolic_rate", "BMR", " kcal"),
          ("metabolic_age", "metabolic age", ""),
          # derived by the scale integration (0.4+): BodyScan numbers
          ("wyze_scale_fat_mass", "fat mass", "lb"), ("wyze_scale_skeletal_muscle_percentage", "skeletal muscle", "%"),
          ("wyze_scale_fat_free_mass_index", "fat-free mass index", ""),
          ("wyze_scale_arm_balance_right_vs_left", "arm balance (right vs left)", "%"),
          ("wyze_scale_leg_balance_right_vs_left", "leg balance (right vs left)", "%"),
          ("wyze_scale_fluid_balance_100_20_khz_impedance_ratio", "fluid balance (impedance ratio)", "")]
DECIMALS = {"weight": 1, "muscle_mass": 1, "body_fat": 1, "body_water": 1, "bmi": 1, "wyze_scale_fat_mass": 1,
            "wyze_scale_skeletal_muscle_percentage": 1, "wyze_scale_fat_free_mass_index": 1,
            "wyze_scale_arm_balance_right_vs_left": 1, "wyze_scale_leg_balance_right_vs_left": 1,
            "wyze_scale_fluid_balance_100_20_khz_impedance_ratio": 3}
KG_LB = 2.20462


def _num(v, unit):
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or x <= 0:
        return None
    if unit == "kg":
        x *= KG_LB
    return x


def note_text(when, vals):
    parts = []
    for k, label, unit in FIELDS:
        if vals.get(k) is not None:
            x = vals[k]
            s = f"{x:.{DECIMALS.get(k, 0)}f}"
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
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        except ValueError:
            return None
        text = note_text(when, vals)
        out = self.store.remember(text=text, title=text.split(": my ")[0] + f": {vals['weight']:.1f} lb",
                                  kind="fact", shelf="body/weight",
                                  tags=["weight", "scale", "body"], source="scale",
                                  meta={"weigh_in": ts, "series": "weigh-in", **{k: round(v, 3) for k, v in vals.items() if v is not None}},
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
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")   # "+" would break the URL
        hist = await self.ha.history(ids, start)
        self.debug = {"start": start, "series": len(hist or []), "error": getattr(self.ha, "last_error", None)}
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
                try:
                    t = datetime.fromisoformat(str(r.get("last_changed") or "").replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                pts.append((t, r.get("state"), unit))
            rows[eid] = pts
        n = 0
        for t_in, ts, _ in rows.get(self.ent("last_weigh_in"), []):
            if ts in ("unknown", "unavailable", None):
                continue
            vals = {}
            for k, _, _ in FIELDS:
                # the value the scale reported for this weigh-in: the latest one within two minutes of it
                cand = [(t, v, u) for t, v, u in rows.get(self.ent(k), []) if t <= t_in + 120 and v not in ("unknown", "unavailable")]
                if cand:
                    best = max(cand, key=lambda x: x[0])
                    if t_in - best[0] < 6 * 3600:
                        vals[k] = _num(best[1], best[2])
            self.debug.setdefault("weigh_ins", []).append({"ts": ts, "vals": vals})
            if self.file(ts, vals):
                n += 1
        return n
