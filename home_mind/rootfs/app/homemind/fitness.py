"""Fitness goals: numbers to aim for, progress against them, the day's tally, workouts, a weekly review, and
targets that adjust from what actually happens.

Built on what Home Mind already collects: weigh-ins from the scale (body/weight), meals with protein and calorie
estimates (body/meals), and the watch's readiness, sleep and steps (patterns). Workouts are new: "squats 3x8 at
185", "bench press 3 sets of 5 at 135", "ran 3 miles in 28 minutes" become notes on body/workouts.

Nothing here changes a goal by itself: adjustments are suggestions the person accepts. And it never suggests eating
less than CALORIE_FLOOR or losing faster than about 1% of body weight a week.
"""
import json
import math
import re
import statistics
import time
from datetime import date, datetime, timedelta, timezone

from . import nutrition

KV = "fitness_goals"
CALORIE_FLOOR = 1500          # never suggested below this
MAX_LOSS_SHARE = 0.01         # never suggest losing faster than ~1% of body weight a week
GOALS = {
    "weight_target": (float, 50, 600),       # lb
    "weight_by": (str, None, None),          # YYYY-MM-DD
    "protein_g": (int, 30, 400),             # a day; None = from body weight (1.6 g/kg)
    "calories": (int, 1200, 6000),           # a day
    "carbs_g": (int, 20, 600),               # a day; None = 45% of the calorie goal
    "fiber_g": (int, 10, 80),                # a day, at least; None = 30
    "sugar_g": (int, 10, 300),               # a day, at most; None = 50
    "fat_g": (int, 20, 300),                 # a day; None = 30% of the calorie goal
    "steps": (int, 500, 50000),              # a day
    "workouts_per_week": (int, 0, 14),
    "body_fat_target": (float, 3, 60),       # %
    "sleep_hours": (float, 4, 12),           # a night
    "bedtime": ("time", None, None),         # HH:MM, in bed by
}
PROTEIN_FOODS = [("Greek yogurt", 17), ("cottage cheese", 14), ("protein shake", 25), ("eggs", 12), ("tuna", 25),
                 ("chicken", 30), ("turkey", 25), ("jerky", 10), ("edamame", 11), ("tofu", 15), ("milk", 8),
                 ("string cheese", 7), ("salmon", 25), ("lentils", 9)]


# ---- workouts: reading them --------------------------------------------------------------------------------------
# The pieces (exercise, sets x reps, weight, distance, time) are found wherever they are in the sentence, so "squats
# 3x8 at 185", "did 3 sets of 8 squats at 185", "squatted 185 for 3 sets of 8" and "185 squat 3x8" all read the same.
LIFTS = {
    "squat": ["back squats?", "squats?", "squatted", "squatting"], "front squat": ["front squats?"],
    "bench press": ["bench press(?:es)?", "bench(?:ed)?", "benching"], "incline bench": ["incline bench(?: press)?"],
    "deadlift": ["deadlifts?", "deadlifted", "deadlifting", "dl"], "romanian deadlift": ["romanian deadlifts?", "rdls?"],
    "overhead press": ["overhead press(?:es)?", "ohp", "shoulder press(?:es)?", "military press(?:es)?", "pressed"],
    "row": ["barbell rows?", "bent[- ]over rows?", "dumbbell rows?", "cable rows?"],
    "pull-up": ["pull[- ]?ups?"], "chin-up": ["chin[- ]?ups?"], "push-up": ["push[- ]?ups?"], "dip": ["dips"],
    "curl": ["bicep curls?", "biceps curls?", "curls", "curled"], "lunge": ["lunges?"], "leg press": ["leg press(?:es)?"],
    "lat pulldown": ["lat pull[- ]?downs?", "pull[- ]?downs?"], "hip thrust": ["hip thrusts?"],
    "sit-up": ["sit[- ]?ups?"], "crunch": ["crunch(?:es)?"], "burpee": ["burpees?"], "plank": ["planks?"],
    "kettlebell swing": ["kettlebell swings?", "kb swings?", "swings"], "step-up": ["step[- ]?ups?"],
    "lateral raise": ["lateral raises?", "side raises?"], "calf raise": ["calf raises?"], "leg curl": ["leg curls?"],
    "leg extension": ["leg extensions?"], "tricep extension": ["tricep extensions?", "triceps extensions?"],
    "power clean": ["power cleans?"], "clean": ["cleans"], "snatch": ["snatch(?:es)?"],
}
CARDIO = {
    "run": ["run", "ran", "running", "jog", "jogged", "jogging"], "walk": ["walk", "walked", "walking"],
    "bike": ["bike", "biked", "biking", "cycled", "cycling", "ride", "rode", "spin", "spinning"],
    "swim": ["swim", "swam", "swimming"], "row": ["rowed", "rowing", "rower", "row"], "hike": ["hike", "hiked", "hiking"],
    "yoga": ["yoga"], "pilates": ["pilates"], "stretching": ["stretching", "stretched", "stretch"],
    "elliptical": ["elliptical"], "treadmill": ["treadmill"], "stairs": ["stairs", "stairmaster", "stair climber"],
    "hiit": ["hiit"], "cardio": ["cardio"],
}
BODYWEIGHT = {"pull-up", "chin-up", "push-up", "dip", "sit-up", "crunch", "burpee", "lunge", "step-up", "kettlebell swing"}


def _alt(d):
    pairs = sorted(((p, k) for k, ps in d.items() for p in ps), key=lambda x: -len(x[0]))
    return [(re.compile(r"(?<![a-z])" + p + r"(?![a-z])", re.I), k) for p, k in pairs]


LIFT_RX, CARDIO_RX = _alt(LIFTS), _alt(CARDIO)
UNIT = r"(?:lb|lbs|pounds?|kg|kilos?|kgs)"
W_UNIT = re.compile(r"(\d{1,4}(?:\.\d)?)\s*(" + UNIT + r")\b", re.I)
W_AT = re.compile(r"(?:\bat|@|\bwith)\s*(\d{1,4}(?:\.\d)?)\b(?!\s*(?:sets?|reps?|x|×|min|minutes|mins|miles?|km))", re.I)
W_FOR = re.compile(r"(?<![\d.x×])(\d{2,4}(?:\.\d)?)\s*(?:" + UNIT + r")?\s+for\b", re.I)
XS = re.compile(r"(\d{1,4}(?:\.\d)?)\s*(?:x|×|by)\s*(\d{1,3})(?:\s*(?:x|×)\s*(\d{1,4}(?:\.\d)?))?", re.I)
SETS_OF = re.compile(r"(\d{1,2})\s*sets?\s*(?:of\s*)?(\d{1,3})\b", re.I)
REPS_SETS = re.compile(r"(\d{1,3})\s*reps?\b.{0,20}?(\d{1,2})\s*sets?\b", re.I)
SETS_REPS = re.compile(r"(\d{1,2})\s*sets?\b.{0,20}?(\d{1,3})\s*reps?\b", re.I)
REPS = re.compile(r"(\d{1,3})\s*reps?\b", re.I)
DIST = re.compile(r"(\d+(?:\.\d+)?)\s*-?\s*(miles?|mi|km|kilometers?|k)(?![a-z])", re.I)
MINS = re.compile(r"(\d{1,3})\s*-?\s*(?:min|mins|minutes?)\b", re.I)
HRS = re.compile(r"\b(?:(an|one|1)\s+hours?|(\d(?:\.\d+)?)\s*(?:hours?|hrs?)|(half an?|a half)\s+hours?|"
                 r"an hour and a half|(\d)\s+and a half hours?)\b", re.I)
LOG = re.compile(r"^\s*(?:log|logged|did|done|workout|gym|lifted|today i did|i did|hit the gym|at the gym)\b[:\s,]*", re.I)
NOT_EXERCISE = {"i", "i did", "did", "and", "then", "plus", "also", "log", "workout", "for", "at", "the", "a", "my"}
STRENGTH = [
    re.compile(r"(?P<ex>[a-z][a-z '\-]*?)\s*[:,\-]?\s*(?P<sets>\d{1,2})\s*(?:x|×)\s*(?P<reps>\d{1,3})\b", re.I),
    re.compile(r"(?P<ex>[a-z][a-z '\-]*?)\s*[:,\-]?\s*(?P<sets>\d{1,2})\s*sets?\s*(?:of\s*)?(?P<reps>\d{1,3})\b", re.I),
]
ALIASES = {"bench": "bench press", "squats": "squat", "deadlifts": "deadlift", "ohp": "overhead press"}


def exercise_name(s):
    s = re.sub(r"\s+", " ", (s or "").lower().strip(" ,.:-"))
    s = re.sub(r"^(?:and|then|plus|also|did|i did|log|logged)\s+", "", s)
    for rx, k in LIFT_RX + CARDIO_RX:
        if rx.fullmatch(s):
            return k
    s = ALIASES.get(s, s)
    if s.endswith("s") and not s.endswith("ss") and len(s) > 4:
        s = s[:-1]
    return s


def _minutes(t):
    m = MINS.search(t)
    if m:
        return int(m.group(1))
    h = HRS.search(t)
    if not h:
        return None
    if h.group(1):
        return 60
    if h.group(2):
        return int(float(h.group(2)) * 60)
    if h.group(3):
        return 30
    if h.group(4):
        return int(h.group(4)) * 60 + 30
    return 90


def _weight(t):
    m = W_UNIT.search(t)
    if m:
        w = float(m.group(1))
        return round(w * 2.20462, 1) if m.group(2).lower().startswith("k") else w
    for rx in (W_AT, W_FOR):
        m = rx.search(t)
        if m and float(m.group(1)) >= 5:
            return float(m.group(1))
    return None


def _clause(part):
    """One exercise from one piece of a sentence, or None."""
    p = part.strip()
    if not p:
        return None
    lift = next(((m, k) for rx, k in LIFT_RX for m in [rx.search(p)] if m), None)
    cardio = next(((m, k) for rx, k in CARDIO_RX for m in [rx.search(p)] if m), None)
    sets = reps = w = None
    x = XS.search(p)
    if x:
        a, b, c = float(x.group(1)), int(x.group(2)), x.group(3)
        if a > 20:                         # "275 x 5": weight x reps (x sets)
            w, reps, sets = a, b, int(float(c)) if c else 1
        else:                              # "3x8" (x 185)
            sets, reps = int(a), b
            if c and float(c) > 20:
                w = float(c)
    for rx, order in ((SETS_OF, "sr"), (SETS_REPS, "sr"), (REPS_SETS, "rs")):
        if sets is None:
            m = rx.search(p)
            if m:
                sets, reps = (int(m.group(1)), int(m.group(2))) if order == "sr" else (int(m.group(2)), int(m.group(1)))
    if sets is None and lift:
        m = REPS.search(p) or re.search(r"(?<![\d.])(\d{1,3})\s+(?:\w+\s+)?$", p[:lift[0].start()])
        if m and (lift[1] in BODYWEIGHT or REPS.search(p)):
            sets, reps = 1, int(m.group(1))
    if lift and sets:
        if w is None:
            w = _weight(p)
        if w is None:                      # "185 squat 3x8": a number right before the lift's name
            m = re.search(r"(?<![\d.x×])(\d{2,4}(?:\.\d)?)\s*$", p[:lift[0].start()].strip())
            if m and float(m.group(1)) > 20:
                w = float(m.group(1))
        if not (1 <= sets <= 20 and 1 <= reps <= 200):
            return None
        e = {"exercise": lift[1], "sets": sets, "reps": reps}
        if w and 5 <= w <= 1500 and lift[1] not in BODYWEIGHT or (w and W_UNIT.search(p)):
            e["weight_lb"] = w
        return e
    if cardio and not sets:
        d = DIST.search(p)
        mins = _minutes(p)
        if not d and not mins:
            return None
        e = {"exercise": cardio[1]}
        if d:
            km = d.group(2).lower().startswith("k")
            v = float(d.group(1))
            e["miles"] = round(v * 0.621371, 2) if km else v
        if mins:
            e["minutes"] = mins
        return e
    if sets and not lift:                  # an exercise not in the list: "face pulls 3x12"
        for rx in STRENGTH:
            m = rx.search(p)
            if m:
                ex = exercise_name(m.group("ex"))
                bad = re.match(r"(?:in|on|at|to|of|the|my|a|an|it|this|that|for|with|from)\b", ex) or \
                    re.search(r"\b(?:the|my|your|room|light|lamp|den|kitchen)\b", ex) or len(ex.split()) > 4
                if ex and not bad and ex not in NOT_EXERCISE and len(ex) <= 40 and 1 <= sets <= 20 and 1 <= reps <= 200:
                    e = {"exercise": ex, "sets": sets, "reps": reps}
                    if _weight(p):
                        e["weight_lb"] = _weight(p)
                    return e
    return None


def parse_workout(text):
    """'squats 3x8 at 185, bench 3 sets of 5 at 135 and ran 2 miles in 18 min' -> entries, or [] if it isn't a
    workout. Weights are kept in pounds (kg converted)."""
    from .langs import digits
    t = LOG.sub("", digits((text or "").replace("’", "'")))
    t = re.sub(r"\b1 hour\b", "an hour", t, flags=re.I)
    parts = [x for x in re.split(r"\s*(?:,|;|\band then\b|\bthen\b|\bplus\b|\band\b(?!\s+a half))\s*", t) if x.strip()]
    # a piece with numbers but no exercise belongs to the one before ("benched 135 for 5 reps, 3 sets")
    merged = []
    for x in parts:
        named = any(rx.search(x) for rx, _ in LIFT_RX + CARDIO_RX)
        if merged and not named and re.search(r"\d", x) and not _clause(x):
            merged[-1] += " " + x
        else:
            merged.append(x)
    return [e for e in (_clause(x) for x in merged) if e]


def describe(e):
    if "sets" in e:
        w = f" at {e['weight_lb']:g} lb" if e.get("weight_lb") else ""
        return f"{e['exercise']} {e['sets']}×{e['reps']}{w}"
    bits = [f"{e['miles']:g} mi" if e.get("miles") else "", f"{e['minutes']} min" if e.get("minutes") else ""]
    return f"{e['exercise']} " + " in ".join(b for b in bits if b)


def e1rm(e):
    """Estimated one-rep max (Epley), for comparing sessions with different reps."""
    if not e.get("weight_lb") or not e.get("reps"):
        return None
    return round(e["weight_lb"] * (1 + e["reps"] / 30.0), 1) if e["reps"] > 1 else e["weight_lb"]


def _day(ts):
    return datetime.fromtimestamp(ts).date()


def _midnight(d):
    return datetime(d.year, d.month, d.day).timestamp()


class Fitness:
    def __init__(self, store, patterns=None, pantry=None):
        self.store, self.patterns, self.pantry = store, patterns, pantry

    # ---- goals -----------------------------------------------------------------------------------------------------
    def goals(self):
        g = self.store.kv_get(KV) or {}
        return {k: g.get(k) for k in GOALS}

    def set_goals(self, new):
        if not isinstance(new, dict):
            raise ValueError("goals must be an object")
        cur = self.goals()
        for k, v in new.items():
            if k not in GOALS:
                raise ValueError(f"unknown goal {k} (known: {', '.join(GOALS)})")
            if v in (None, ""):
                cur[k] = None
                continue
            typ, lo, hi = GOALS[k]
            if typ == "time":
                m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(v).strip())
                if not m:
                    raise ValueError(f"{k} must be a time like 23:00")
                cur[k] = f"{int(m.group(1)):02d}:{m.group(2)}"
                continue
            if typ is str:
                try:
                    date.fromisoformat(str(v))
                except ValueError:
                    raise ValueError(f"{k} must be a date like 2027-03-01") from None
                cur[k] = str(v)
                continue
            try:
                n = typ(float(v))
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number") from None
            if not lo <= n <= hi:
                raise ValueError(f"{k} must be between {lo} and {hi}")
            cur[k] = n
        self.store.kv_set(KV, cur)
        pins = dict(self.store.kv_get("fitness_goal_pins") or {})
        for k, v in new.items():
            if k in GOALS:
                if v in (None, ""):
                    pins.pop(k, None)
                else:
                    pins[k] = True
        self.store.kv_set("fitness_goal_pins", pins)
        return cur

    # ---- weight ----------------------------------------------------------------------------------------------------
    def weigh_ins(self, days=120, now=None):
        from .speakers import visible
        now = now or self.store.clock()
        out = []
        for r in self.store.db.execute("SELECT created, meta FROM items WHERE shelf='body/weight' AND status='active' "
                                       "AND created >= ? ORDER BY created", (now - days * 86400,)):
            m = json.loads(r["meta"] or "{}")
            if not visible(m):
                continue
            if isinstance(m.get("weight"), (int, float)) and 50 < m["weight"] < 700:
                out.append({"t": r["created"], "weight": float(m["weight"]), "body_fat": m.get("body_fat")})
        return out

    def trend(self, now=None):
        """7-day average weight (daily swings of 1-3 lb are water, not fat), and the weekly rate from a straight
        line through the last 28 days (at least 4 weigh-ins over 10 days)."""
        now = now or self.store.clock()
        w = self.weigh_ins(60, now)
        if not w:
            return {"n": 0}
        last7 = [x["weight"] for x in w if x["t"] >= now - 7 * 86400] or [w[-1]["weight"]]
        out = {"n": len(w), "latest": w[-1]["weight"], "latest_at": w[-1]["t"], "average_7d": round(statistics.mean(last7), 1)}
        win = [x for x in w if x["t"] >= now - 28 * 86400]
        if len(win) >= 4 and win[-1]["t"] - win[0]["t"] >= 10 * 86400:
            xs = [(x["t"] - win[0]["t"]) / (7 * 86400) for x in win]
            ys = [x["weight"] for x in win]
            mx, my = statistics.mean(xs), statistics.mean(ys)
            den = sum((a - mx) ** 2 for a in xs)
            if den > 0:
                out["rate_lb_week"] = round(sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / den, 2)
                out["rate_days"] = round((win[-1]["t"] - win[0]["t"]) / 86400)
        bf = [x["body_fat"] for x in w if x["t"] >= now - 7 * 86400 and isinstance(x.get("body_fat"), (int, float))]
        if bf:
            out["body_fat_7d"] = round(statistics.mean(bf), 1)
        return out

    def protein_goal(self, now=None):
        g = self.goals().get("protein_g")
        if g:
            return g
        t = self.trend(now)
        return nutrition.protein_target(t.get("average_7d")) if t.get("n") else None

    def progress(self, now=None):
        now = now or self.store.clock()
        g, t = self.goals(), self.trend(now)
        out = {"trend": t, "goals": g}
        tgt = g.get("weight_target")
        if tgt and t.get("n"):
            cur = t["average_7d"]
            left = round(tgt - cur, 1)
            out["weight"] = {"target": tgt, "current": cur, "left": left, "reached": abs(left) < 0.5}
            rate = t.get("rate_lb_week")
            if g.get("weight_by"):
                weeks = (_midnight(date.fromisoformat(g["weight_by"])) - now) / (7 * 86400)
                out["weight"]["weeks_left"] = round(weeks, 1)
                if weeks > 0:
                    need = left / weeks
                    out["weight"]["needed_lb_week"] = round(need, 2)
                    out["weight"]["too_fast"] = left < 0 and abs(need) > cur * MAX_LOSS_SHARE
            if rate is not None and abs(rate) >= 0.05 and (left < 0) == (rate < 0) and not out["weight"]["reached"]:
                days = left / rate * 7
                out["weight"]["projected"] = (datetime.fromtimestamp(now) + timedelta(days=days)).date().isoformat()
            if rate is not None and "needed_lb_week" in out["weight"]:
                need = out["weight"]["needed_lb_week"]
                out["weight"]["on_track"] = out["weight"]["reached"] or (
                    (rate < 0) == (need < 0) and abs(rate) >= 0.8 * abs(need))
        bft = g.get("body_fat_target")
        if bft and t.get("body_fat_7d") is not None:
            out["body_fat"] = {"target": bft, "current": t["body_fat_7d"], "left": round(bft - t["body_fat_7d"], 1)}
        return out

    # ---- food ------------------------------------------------------------------------------------------------------
    def tally(self, day=None, now=None):
        """Protein and calories eaten on a day against the targets."""
        now = now or self.store.clock()
        d = day or _day(now)
        start = _midnight(d)
        p = c = 0.0
        n = unknown = 0
        from .speakers import visible
        for r in self.store.db.execute("SELECT title, body, meta FROM items WHERE shelf='body/meals' AND status='active' "
                                       "AND created >= ? AND created < ?", (start, start + 86400 + 3600)):
            meta = json.loads(r["meta"] or "{}")
            if not visible(meta) or _day(meta.get("at") or start) != d and meta.get("at"):
                continue
            n += 1
            m = re.search(r"(\d{1,3}(?:\.\d)?)\s*g(?:rams)?\s*(?:of\s*)?protein", f"{r['title']} {r['body']}", re.I)
            pg = float(m.group(1)) if m else meta.get("protein_g")
            if pg is None:
                unknown += 1
            p += pg or 0
            c += meta.get("calories") or 0
        g = self.goals()
        pt = self.protein_goal(now)
        out = {"day": d.isoformat(), "meals": n, "unestimated": unknown, "protein_g": round(p), "calories": round(c),
               "protein_target": pt, "calorie_target": g.get("calories")}
        if pt:
            out["protein_left"] = max(0, round(pt - p))
        if g.get("calories"):
            out["calories_left"] = round(g["calories"] - c)
        return out

    def protein_ideas(self, grams, n=3):
        """Foods that would close a protein gap, the ones in the pantry first."""
        have = []
        if self.pantry:
            try:
                names = " | ".join(i["name"].lower() for i in self.pantry.items())
            except Exception:  # noqa: BLE001
                names = ""
            have = [(f, g) for f, g in PROTEIN_FOODS if f.lower().split()[-1] in names]
        rest = [x for x in PROTEIN_FOODS if x not in have]
        pick = (have + rest)[:n]
        return [f"{f} (~{g} g)" for f, g in pick if grams > 0]

    # ---- workouts --------------------------------------------------------------------------------------------------
    def log(self, text, when=None, source="api", meta=None, read_as=None):
        """read_as: the same workout in the plain form (a model's rewrite of other wording); the note keeps text."""
        entries = parse_workout(read_as or text)
        if not entries:
            raise ValueError("couldn't read a workout (try 'squats 3x8 at 185' or 'ran 3 miles in 28 min')")
        now = when or self.store.clock()
        prev = {e["exercise"]: self.last(e["exercise"], before=now) for e in entries}
        words = "; ".join(describe(e) for e in entries)
        from .speakers import VIEWER
        m_out = dict(meta or {}, workout=entries, at=now)
        who = VIEWER.get()
        if who and "owner" not in m_out and m_out.get("scope") != "household":
            m_out.update(scope="personal", owner=who)
        r = self.store.remember(text=f"Workout {datetime.fromtimestamp(now):%a %b %d}: {words}.",
                                title=f"Workout: {words}"[:100], kind="note", shelf="body/workouts",
                                tags=["workout"] + sorted({e["exercise"].split()[0] for e in entries})[:5],
                                source=source, meta=m_out, at=now)
        notes = []
        for e in entries:
            p = prev.get(e["exercise"])
            if p:
                a, b = e1rm(e), e1rm(p["entry"])
                tail = f" (last time {describe(p['entry'])}"
                if a and b and a > b:
                    tail += ", stronger"
                notes.append(describe(e) + tail + ")")
            else:
                notes.append(describe(e))
            if self.is_record(e, before=now):
                notes[-1] += " — a new best"
        mg = getattr(self, "memgraph", None)
        if mg is not None:
            try:
                mg.ingest_workout(
                    entries, ts=now, owner=m_out.get("owner"),
                    scope=m_out.get("scope") or ("personal" if m_out.get("owner") else "household"),
                    item_id=r.get("id"),
                )
            except Exception:  # noqa: BLE001 - graph telemetry never breaks workout logging
                pass
        return {"id": r.get("id"), "entries": entries, "answer": "Logged: " + "; ".join(notes) + "."}

    def workouts(self, days=90, now=None, before=None):
        now = now or self.store.clock()
        out = []
        from .speakers import visible
        for r in self.store.db.execute("SELECT id, created, meta FROM items WHERE shelf='body/workouts' AND status='active' "
                                       "AND created >= ? ORDER BY created", (now - days * 86400,)):
            m = json.loads(r["meta"] or "{}")
            if not visible(m) or (before is not None and r["created"] >= before):
                continue
            for e in m.get("workout") or []:
                if isinstance(e, dict) and e.get("exercise"):
                    out.append({"id": r["id"], "t": r["created"], "entry": e})
        return out

    def last(self, exercise, before=None):
        ex = exercise_name(exercise)
        hist = [w for w in self.workouts(3650, before=before) if w["entry"]["exercise"] == ex]
        return hist[-1] if hist else None

    def is_record(self, e, before=None):
        a = e1rm(e)
        if not a:
            return False
        older = [e1rm(w["entry"]) for w in self.workouts(3650, before=before) if w["entry"]["exercise"] == e["exercise"]]
        older = [x for x in older if x]
        return bool(older) and a > max(older)

    def records(self):
        best = {}
        for w in self.workouts(3650):
            v = e1rm(w["entry"])
            ex = w["entry"]["exercise"]
            if v and (ex not in best or v > best[ex]["e1rm"]):
                best[ex] = {"e1rm": v, "entry": w["entry"], "t": w["t"]}
        return best

    def stalls(self, now=None):
        """Lifts done at least 3 times over 3+ weeks whose last 3 sessions are no stronger than the best before."""
        now = now or self.store.clock()
        by = {}
        for w in self.workouts(120, now):
            if e1rm(w["entry"]):
                by.setdefault(w["entry"]["exercise"], []).append(w)
        out = []
        for ex, hist in by.items():
            if len(hist) < 4 or hist[-1]["t"] - hist[0]["t"] < 21 * 86400:
                continue
            recent = max(e1rm(w["entry"]) for w in hist[-3:])
            earlier = max(e1rm(w["entry"]) for w in hist[:-3])
            if recent <= earlier:
                out.append({"exercise": ex, "since": _day(hist[-3]["t"]).isoformat(), "best": earlier})
        return out

    def sessions(self, days=7, now=None):
        now = now or self.store.clock()
        return len({_day(w["t"]) for w in self.workouts(days, now) if w["t"] >= now - days * 86400})

    # ---- the logs, to look at and fix --------------------------------------------------------------------------------
    LOG_SHELVES = {"sleep": "body/sleep", "workout": "body/workouts"}

    def logs(self, kind, days=30, now=None):
        shelf = self.LOG_SHELVES.get(kind)
        if not shelf:
            raise ValueError("kind is sleep or workout")
        now = now or self.store.clock()
        from .speakers import visible
        out = []
        for r in self.store.db.execute("SELECT id, created, title, body, meta FROM items WHERE shelf=? AND status='active' "
                                       "AND created >= ? ORDER BY created DESC", (shelf, now - days * 86400)):
            m = json.loads(r["meta"] or "{}")
            if not visible(m):
                continue
            e = {"id": r["id"], "t": r["created"], "title": r["title"]}
            if kind == "sleep":
                e["hours"] = m.get("sleep")
                e["night"] = m.get("night")
                e["said"] = (re.search(r"Said: (.*)$", r["body"] or "") or [None, None])[1]
            else:
                e["entries"] = m.get("workout") or []
                e["text"] = "; ".join(describe(x) for x in e["entries"])
            out.append(e)
        return out

    def _own(self, rid, kind):
        row = self.store.db.execute("SELECT id, created, shelf, meta FROM items WHERE id=? AND status='active'",
                                    (rid,)).fetchone()
        from .speakers import visible
        if not row or row["shelf"] != self.LOG_SHELVES.get(kind) or not visible(json.loads(row["meta"] or "{}")):
            raise ValueError(f"no {kind} log {rid}")
        return row

    def edit_log(self, kind, rid, text):
        """Replace a sleep or workout log with what it should say ('slept 6.5 hours', 'squat 3x5 at 185 lb'),
        kept on the same day. The old one is archived, not deleted."""
        row = self._own(rid, kind)
        meta = {k: v for k, v in json.loads(row["meta"] or "{}").items() if k in ("said_by", "scope", "owner")}
        if kind == "sleep":
            from .sleeplog import log_sleep
            t = text if re.search(r"\bslept\b", text, re.I) else f"slept {text}"
            r = log_sleep(self.store, t, now=row["created"], meta=dict(meta, edited=rid))
            if not r:
                raise ValueError("couldn't read that as sleep (try 'slept 6.5 hours' or 'slept from 12 to 7')")
        else:
            r = self.log(text, when=row["created"], source="edit", meta=dict(meta, edited=rid))
        self.store.set_status(rid, "archived")
        return r

    def delete_log(self, kind, rid):
        self._own(rid, kind)
        self.store.set_status(rid, "archived")
        return {"removed": rid}

    # ---- the watch ---------------------------------------------------------------------------------------------------
    def watch(self, days=7):
        """Steps, sleep and readiness from the patterns table (the watch); steps are the day before each row."""
        if not self.patterns:
            return {}
        try:
            rows = self.patterns.rows(days + 1)
        except Exception:  # noqa: BLE001
            return {}
        avg = lambda xs: round(statistics.mean(xs), 1) if xs else None  # noqa: E731
        steps = [r["steps_prev"] for r in rows[-days:] if isinstance(r.get("steps_prev"), (int, float))]
        sleep = [r["sleep_h"] for r in rows[-days:] if isinstance(r.get("sleep_h"), (int, float))]
        ready = [r["readiness"] for r in rows[-days:] if isinstance(r.get("readiness"), (int, float))]
        return {"steps": round(avg(steps)) if steps else None, "steps_days": len(steps),
                "steps_hit": None, "sleep_h": avg(sleep), "readiness": avg(ready), "_steps": steps}

    def nights(self, days=7):
        """The last nights from the watch rows (said sleep already wins there): hours and time in bed."""
        if not self.patterns:
            return []
        try:
            rows = self.patterns.rows(days + 1)[-days:]
        except Exception:  # noqa: BLE001
            return []
        return [{"day": r.get("day"), "sleep_h": r.get("sleep_h"), "bedtime": r.get("bedtime")} for r in rows]

    @staticmethod
    def bed_hours(hhmm):
        """'23:00' -> 23.0, '00:30' -> 24.5 (hours after the evening's midnight, like the watch rows)."""
        h, m = (int(x) for x in hhmm.split(":"))
        v = h + m / 60
        return v + 24 if v < 12 else v

    def sleep_summary(self, days=7):
        g = self.goals()
        ns = self.nights(days)
        hrs = [n["sleep_h"] for n in ns if isinstance(n.get("sleep_h"), (int, float)) and n["sleep_h"] > 0]
        beds = [n["bedtime"] for n in ns if isinstance(n.get("bedtime"), (int, float))]
        out = {"nights": len(hrs), "avg_h": round(statistics.mean(hrs), 1) if hrs else None}
        if g.get("sleep_hours") and hrs:
            out["met"] = sum(1 for x in hrs if x >= g["sleep_hours"] - 0.25)
        if g.get("bedtime") and beds:
            goal = self.bed_hours(g["bedtime"])
            out["bed_nights"] = len(beds)
            out["bed_met"] = sum(1 for b in beds if b <= goal + 0.25)
        return out

    # ---- adjusting -------------------------------------------------------------------------------------------------
    def suggestions(self, now=None):
        """What the numbers say to change. Suggestions only: nothing is changed until the person accepts."""
        now = now or self.store.clock()
        g, p = self.goals(), self.progress(now)
        t = p["trend"]
        out = []
        days = [self.tally(_day(now - i * 86400), now) for i in range(1, 15)]
        logged = [d for d in days if d["meals"] >= 2 and d["calories"] > 0]
        cal_avg = round(statistics.mean(d["calories"] for d in logged)) if logged else None
        rate = t.get("rate_lb_week")
        w = p.get("weight") or {}
        if w and rate is not None and t.get("rate_days", 0) >= 21 and not w.get("reached"):
            losing = w["left"] < 0
            if losing and rate > -0.25:
                if g.get("calories") and len(logged) >= 5 and cal_avg <= g["calories"] + 100:
                    new = max(CALORIE_FLOOR, g["calories"] - 150)
                    if new < g["calories"]:
                        out.append({"kind": "calories", "set": {"calories": new},
                                    "why": f"weight has held steady for {t['rate_days']} days ({rate:+.2f} lb a week) while "
                                           f"you averaged {cal_avg} kcal: about 150 kcal less a day should restart it"})
                elif g.get("calories") and len(logged) >= 5 and cal_avg > g["calories"] + 100:
                    out.append({"kind": "note", "why": f"weight has held steady; you averaged {cal_avg} kcal against "
                                                       f"{g['calories']}, so the target itself may be fine"})
            if losing and rate < -(w["current"] * MAX_LOSS_SHARE) and g.get("calories"):
                out.append({"kind": "calories", "set": {"calories": g["calories"] + 150},
                            "why": f"losing {abs(rate):.1f} lb a week is faster than about 1% of body weight a week, "
                                   "which tends to cost muscle: about 150 kcal more a day"})
            if not losing and rate < 0.1 and g.get("calories") and len(logged) >= 5:
                out.append({"kind": "calories", "set": {"calories": g["calories"] + 150},
                            "why": f"you're aiming to gain but weight has held for {t['rate_days']} days: about 150 kcal more"})
        if w.get("too_fast"):
            out.append({"kind": "date", "why": f"reaching {w['target']:g} lb by {g['weight_by']} needs "
                                               f"{abs(w['needed_lb_week']):.1f} lb a week, faster than about 1% of body "
                                               "weight a week; a later date would be kinder to muscle and energy"})
        for s in self.stalls(now):
            out.append({"kind": "lift", "exercise": s["exercise"],
                        "why": f"{s['exercise']} hasn't gone up since {s['since']}: try a lighter week (about 90%), then "
                               "build back, or change the reps (e.g. 5×5 instead of 3×8)"})
        if g.get("sleep_hours") or g.get("bedtime"):
            sl = self.sleep_summary(7)
            if sl.get("nights", 0) >= 5 and g.get("sleep_hours") and sl.get("met", 99) <= sl["nights"] // 2:
                why = (f"sleep reached {g['sleep_hours']:g} h on {sl['met']} of {sl['nights']} nights (average "
                       f"{sl['avg_h']} h)")
                if g.get("bedtime") and sl.get("bed_nights"):
                    why += f", in bed by {g['bedtime']} on {sl['bed_met']} of {sl['bed_nights']}"
                out.append({"kind": "sleep", "why": why + ": a wind-down reminder 30 minutes before bedtime usually "
                                                          "helps more than a later alarm"})
        if g.get("steps"):
            wa = self.watch(7)
            st = wa.get("_steps") or []
            if len(st) >= 5 and sum(1 for x in st if x >= g["steps"]) <= len(st) // 3:
                out.append({"kind": "steps", "why": f"steps reached {g['steps']:,} on {sum(1 for x in st if x >= g['steps'])}"
                                                    f" of {len(st)} days (average {round(statistics.mean(st)):,}): a short walk "
                                                    "after dinner usually covers the gap"})
        return out

    # ---- words ---------------------------------------------------------------------------------------------------------
    def status_lines(self, now=None):
        """A few lines for answers and briefs."""
        now = now or self.store.clock()
        p, out = self.progress(now), []
        t, w = p["trend"], p.get("weight")
        if t.get("n"):
            s = f"Weight: {t['average_7d']:.1f} lb (7-day average)"
            if t.get("rate_lb_week") is not None:
                s += f", {t['rate_lb_week']:+.1f} lb a week over {t['rate_days']} days"
            if w:
                s += f"; goal {w['target']:g} lb" + (f" by {p['goals']['weight_by']}" if p["goals"].get("weight_by") else "")
                if w.get("reached"):
                    s += " — reached"
                else:
                    s += f", {abs(w['left']):.1f} lb to go"
                    if w.get("projected"):
                        s += f", on pace for {w['projected']}"
                    if "on_track" in w:
                        s += " (on track)" if w["on_track"] else f" (behind: needs {w['needed_lb_week']:+.1f} lb a week)"
            out.append(s)
        if p.get("body_fat"):
            out.append(f"Body fat: {p['body_fat']['current']}% (goal {p['body_fat']['target']:g}%)")
        d = self.tally(now=now)
        if d["protein_target"] or d["calorie_target"]:
            s = f"Today so far: {d['protein_g']} g protein" + (f" of {d['protein_target']}" if d["protein_target"] else "")
            if d["calorie_target"]:
                s += f", {d['calories']} of {d['calorie_target']} kcal"
            if d["unestimated"]:
                s += f" ({d['unestimated']} meal{'s' if d['unestimated'] != 1 else ''} not estimated yet)"
            out.append(s)
        g = p["goals"]
        if g.get("sleep_hours") or g.get("bedtime"):
            sl = self.sleep_summary(7)
            if sl.get("avg_h"):
                out.append(f"Sleep this week: {sl['avg_h']} h a night" + (
                    f", goal {g['sleep_hours']:g} h met {sl['met']} of {sl['nights']}" if "met" in sl else ""))
        n = self.sessions(7, now)
        if g.get("workouts_per_week") or n:
            out.append(f"Workouts this week: {n}" + (f" of {g['workouts_per_week']}" if g.get("workouts_per_week") else ""))
        return out

    def evening_lines(self, now=None):
        now = now or self.store.clock()
        d = self.tally(now=now)
        out = []
        if d.get("protein_target"):
            left = d.get("protein_left", 0)
            if left >= 15:
                ideas = self.protein_ideas(left)
                out.append(f"Protein: {d['protein_g']} of {d['protein_target']} g, about {left} g short"
                           + (": " + ", ".join(ideas) + " would close it" if ideas else ""))
            else:
                out.append(f"Protein: {d['protein_g']} of {d['protein_target']} g — target met")
        if d.get("calorie_target"):
            out.append(f"Calories: {d['calories']} of {d['calorie_target']}")
        g = self.goals()
        if g.get("bedtime"):
            out.append(f"Bedtime goal: in bed by {g['bedtime']}" + (f" for {g['sleep_hours']:g} hours" if g.get("sleep_hours")
                                                                    else ""))
        if datetime.fromtimestamp(now).weekday() == 6:          # Sunday: the week in review
            out += ["Week in review:"] + ["  " + x for x in self.review(now)]
        return out

    def review(self, now=None):
        """The week: weight, food, workouts, steps, sleep, and what to change."""
        now = now or self.store.clock()
        g, p = self.goals(), self.progress(now)
        t, w = p["trend"], p.get("weight") or {}
        out = []
        week = [x["weight"] for x in self.weigh_ins(7, now)]
        prev = [x["weight"] for x in self.weigh_ins(14, now) if x["t"] < now - 7 * 86400]
        if week and prev:
            ch = statistics.mean(week) - statistics.mean(prev)
            out.append(f"Weight: {statistics.mean(week):.1f} lb average, {ch:+.1f} lb from the week before"
                       + (f"; {abs(w['left']):.1f} lb to the goal" if w and not w.get("reached") else ""))
        elif week:
            out.append(f"Weight: {statistics.mean(week):.1f} lb average ({len(week)} weigh-ins)")
        days = [self.tally(_day(now - i * 86400), now) for i in range(0, 7)]
        logged = [d for d in days if d["meals"]]
        if logged:
            pa = round(statistics.mean(d["protein_g"] for d in logged))
            pt = self.protein_goal(now)
            hit = sum(1 for d in logged if pt and d["protein_g"] >= pt * 0.9)
            out.append(f"Protein: {pa} g a day on average" + (f", target met on {hit} of {len(logged)} days" if pt else ""))
            cals = [d["calories"] for d in logged if d["calories"]]
            if cals:
                out.append(f"Calories: {round(statistics.mean(cals))} a day on average"
                           + (f" (target {g['calories']})" if g.get("calories") else ""))
        n = self.sessions(7, now)
        out.append(f"Workouts: {n}" + (f" of {g['workouts_per_week']}" if g.get("workouts_per_week") else ""))
        recs = [e for e, r in self.records().items() if r["t"] >= now - 7 * 86400 and self.is_record(r["entry"], before=r["t"])]
        if recs:
            out.append("New bests: " + ", ".join(recs))
        wa = self.watch(7)
        if wa.get("steps"):
            out.append(f"Steps: {wa['steps']:,} a day" + (f" (goal {g['steps']:,})" if g.get("steps") else ""))
        sl = self.sleep_summary(7)
        if sl.get("avg_h"):
            s_ = f"Sleep: {sl['avg_h']} h a night"
            if g.get("sleep_hours") and "met" in sl:
                s_ += f", {g['sleep_hours']:g} h or more on {sl['met']} of {sl['nights']} nights"
            if g.get("bedtime") and sl.get("bed_nights"):
                s_ += f"; in bed by {g['bedtime']} on {sl['bed_met']} of {sl['bed_nights']}"
            if wa.get("readiness"):
                s_ += f"; readiness {wa['readiness']:g}"
            out.append(s_)
        elif wa.get("sleep_h"):
            out.append(f"Sleep: {wa['sleep_h']} h a night" + (f", readiness {wa['readiness']:g}" if wa.get("readiness") else ""))
        sug = self.suggestions(now)
        if sug:
            out.append("Suggestion: " + sug[0]["why"])
        return out

    def context_lines(self, question, now=None):
        """FITNESS lines for a model answering a question about weight, food or training."""
        now = now or self.store.clock()
        lines = self.status_lines(now)
        g = {k: v for k, v in self.goals().items() if v not in (None, "")}
        if g:
            lines.append("Goals: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in g.items()))
        q = (question or "").lower()
        seen = set()
        for w in reversed(self.workouts(365, now)):
            ex = w["entry"]["exercise"]
            if ex in seen or not (ex in q or ex.split()[0] in q or re.search(r"\b(?:workout|lift|gym|train)", q)):
                continue
            seen.add(ex)
            lines.append(f"Last {ex}: {describe(w['entry'])} on {_day(w['t']):%a %b %d}")
            if len(seen) >= 6:
                break
        if re.search(r"\b(?:pr|best|record|strongest|max)\b", q):
            for ex, r in list(self.records().items())[:8]:
                lines.append(f"Best {ex}: {describe(r['entry'])} (est. 1-rep max {r['e1rm']:g} lb) on {_day(r['t']):%b %d}")
        for s in self.suggestions(now)[:2]:
            lines.append("Note: " + s["why"])
        return ("FITNESS (from the scale, meals and workout log):\n" + "\n".join(lines)) if lines else ""

    def status(self, now=None):
        now = now or self.store.clock()
        return {"goals": self.goals(), "progress": self.progress(now), "today": self.tally(now=now),
                "protein_target": self.protein_goal(now), "workouts": [dict(w, t=w["t"]) for w in self.workouts(14, now)][-20:],
                "sessions_7d": self.sessions(7, now), "records": self.records(), "stalls": self.stalls(now),
                "suggestions": self.suggestions(now), "lines": self.status_lines(now)}

    # ---- auto-baseline, adaptive daily goals & progressive routine generator ---------------------------------------
    def prefs(self):
        p = self.store.kv_get("fitness_prefs") or {}
        if not isinstance(p, dict):
            p = {}
        return {
            "sync_adaptive": bool(p.get("sync_adaptive", True)),
            "split": p.get("split") if p.get("split") in ("ul", "ppl", "fb") else "ul",
            "equip": p.get("equip") if p.get("equip") in ("gym", "db", "bw") else "gym",
            "mins": int(p.get("mins") or 45) if int(p.get("mins") or 45) in (30, 45, 60) else 45,
            "note": str(p.get("note") or "")[:160],
            "use_memgraph": bool(p.get("use_memgraph", True)),
            "use_calendar": bool(p.get("use_calendar", True)),
        }

    def set_prefs(self, new):
        cur = self.prefs()
        if isinstance(new, dict):
            for k in ("sync_adaptive", "use_memgraph", "use_calendar"):
                if k in new and new[k] is not None:
                    cur[k] = bool(new[k])
            if new.get("split") in ("ul", "ppl", "fb"):
                cur["split"] = new["split"]
            if new.get("equip") in ("gym", "db", "bw"):
                cur["equip"] = new["equip"]
            if new.get("mins") in (30, 45, 60, "30", "45", "60"):
                cur["mins"] = int(new["mins"])
            if "note" in new and new["note"] is not None:
                cur["note"] = str(new["note"]).strip()[:160]
        self.store.kv_set("fitness_prefs", cur)
        return cur

    def pinned_goals(self):
        raw = self.store.kv_get("fitness_goal_pins")
        g = self.goals()
        if isinstance(raw, dict):
            return {k: True for k, v in raw.items() if v and k in GOALS and g.get(k) not in (None, "")}
        # Backwards compat: any goal already saved before pinning existed counts as pinned
        return {k: True for k, v in g.items() if v not in (None, "")}

    def reset_goals(self, key=None):
        """Unpin one goal key (or all keys if key is None) so it returns to Auto-Baseline."""
        cur = self.goals()
        pins = self.pinned_goals()
        if key:
            if key not in GOALS:
                raise ValueError(f"unknown goal {key}")
            cur[key] = None
            pins.pop(key, None)
        else:
            for k in GOALS:
                cur[k] = None
            pins = {}
        self.store.kv_set(KV, cur)
        self.store.kv_set("fitness_goal_pins", pins)
        return self.auto_baseline()

    def latest_body(self, now=None):
        from .speakers import visible
        now = now or self.store.clock()
        for row in self.store.db.execute(
            "SELECT created, meta FROM items WHERE shelf='body/weight' AND status='active' ORDER BY created DESC LIMIT 50"
        ):
            m = json.loads(row["meta"] or "{}")
            if not visible(m):
                continue
            out = {"at": row["created"]}
            for k in ("weight", "body_fat", "muscle_mass", "bmi", "basal_metabolic_rate", "body_water"):
                if isinstance(m.get(k), (int, float)):
                    out[k] = round(float(m[k]), 1)
            return out
        return {}

    def auto_baseline(self, now=None):
        """Compute sensible baseline goals from scale BMR/weigh-ins, 14d watch steps/sleep, and 4w workouts,
        respecting any manually pinned goals in KV."""
        now = now or self.store.clock()
        saved = self.goals()
        pins = self.pinned_goals()
        body = self.latest_body(now)
        tr = self.trend(now)
        cur_w = tr.get("trend") or body.get("weight")
        w14 = self.watch(14)
        sl14 = self.sleep_summary(14)
        sess4w = self.sessions(28, now) / 4.0

        auto = {}
        reasons = {}
        if cur_w:
            target_w = saved["weight_target"] if pins.get("weight_target") and saved.get("weight_target") else round(cur_w, 1)
            auto["weight_target"] = round(cur_w, 1)
            reasons["weight_target"] = f"Current {cur_w:.1f} lb"
        else:
            target_w = saved.get("weight_target")
            reasons["weight_target"] = "Log a weigh-in to auto-fill"

        bmr = body.get("basal_metabolic_rate")
        avg_steps = w14.get("steps") or 7000
        if bmr:
            mult = 1.28 + min(0.25, avg_steps / 40000.0) + min(0.12, sess4w * 0.025)
            tdee = int(round(bmr * mult))
            cal_src = f"BMR {int(bmr)} → TDEE ~{tdee}"
        elif cur_w:
            mult = 13.0 + (1.0 if avg_steps >= 8000 or sess4w >= 3 else 0.0)
            tdee = int(round(cur_w * mult))
            cal_src = f"{cur_w:.0f} lb × {mult:g}"
        else:
            tdee = 2100
            cal_src = "Default (sync scale for BMR)"

        diff = (target_w - cur_w) if (cur_w and target_w) else 0.0
        adj = -400 if diff < -1.0 else (200 if diff > 1.0 else 0)
        cals = max(1400, min(5000, int(round((tdee + adj) / 50.0) * 50)))
        auto["calories"] = cals
        reasons["calories"] = f"{cal_src}{f' {adj:+d}' if adj else ' (maint)'}"

        ref_w = target_w if (cur_w and target_w and diff < -1.0) else cur_w
        if ref_w:
            prot = max(60, min(320, int(round((ref_w * 0.8) / 5.0) * 5)))
            reasons["protein_g"] = f"{ref_w:.0f} lb × 0.8 g/lb"
        else:
            prot = 130
            reasons["protein_g"] = "Default (log weight for 1.6 g/kg)"
        auto["protein_g"] = prot

        fat = max(50, int(round((cals * 0.28) / 9.0)))
        carbs = max(80, int(round((cals - prot * 4 - fat * 9) / 4.0)))
        fiber = max(25, min(45, int(round((cals / 1000.0) * 14.0))))
        sugar = min(50, int(round((cals * 0.10) / 4.0)))
        auto.update(fat_g=fat, carbs_g=carbs, fiber_g=fiber, sugar_g=sugar)
        reasons.update(fat_g="~28% of kcal", carbs_g="Remaining kcal", fiber_g="14g / 1k kcal", sugar_g="≤10% kcal")

        if w14.get("steps"):
            step_auto = max(6000, min(12000, int(round((w14["steps"] + 500) / 500.0) * 500)))
            reasons["steps"] = f"14d avg {w14['steps']:,} + 500"
        else:
            step_auto = 8000
            reasons["steps"] = "Default 8,000 (no watch yet)"
        auto["steps"] = step_auto

        wpw = max(3, min(5, int(round(sess4w + 0.4)))) if sess4w >= 1 else 3
        auto["workouts_per_week"] = wpw
        reasons["workouts_per_week"] = f"4w avg {sess4w:.1f}/wk" if sess4w >= 0.5 else "Starter 3/wk"

        sl_h = 7.5
        if sl14.get("avg_h"):
            sl_h = max(7.0, min(9.0, round(sl14["avg_h"] * 4) / 4))
            reasons["sleep_hours"] = f"14d avg {sl14['avg_h']} h"
        else:
            reasons["sleep_hours"] = "Recommended 7.5 h"
        auto["sleep_hours"] = sl_h
        auto["bedtime"] = "22:30"
        reasons["bedtime"] = "Wake − sleep − 20m"
        auto["body_fat_target"] = round(max(12.0, body["body_fat"] - 2.0), 1) if body.get("body_fat") else None
        reasons["body_fat_target"] = f"Scale {body['body_fat']:.1f}%" if body.get("body_fat") else "Optional"
        auto["weight_by"] = None
        reasons["weight_by"] = "Optional target date"

        effective = {k: (saved[k] if pins.get(k) and saved.get(k) not in (None, "") else auto.get(k)) for k in GOALS}
        return {"auto": auto, "effective": effective, "pinned": pins, "reasons": reasons, "body": body, "tdee": tdee}

    @staticmethod
    def _shift_bedtime(hhmm, delta_min):
        m = re.match(r"^(\d{1,2}):(\d{2})$", str(hhmm or "22:30"))
        h, mn = (int(m.group(1)), int(m.group(2))) if m else (22, 30)
        tot = (h * 60 + mn + int(delta_min) + 1440) % 1440
        return f"{tot // 60:02d}:{tot % 60:02d}"

    def daily_plan(self, now=None, viewer=None):
        """Today's adaptive daily goals derived from baseline + yesterday's sleep/readiness/steps + 3d protein + weekly pace."""
        now = now or self.store.clock()
        bl = self.auto_baseline(now)
        eff = bl["effective"]
        pr = self.prefs()
        sess_7d = self.sessions(7, now)
        wpw = int(eff.get("workouts_per_week") or 3)
        sessions_left = max(0, wpw - sess_7d)

        # Pull latest watch / sleep signals
        sleep_prev, readiness, steps_prev = None, None, None
        if self.patterns is not None:
            try:
                rows = self.patterns.rows(4)
                if rows:
                    last_r = rows[-1]
                    sleep_prev = last_r.get("sleep_h")
                    readiness = last_r.get("readiness")
                    prev_r = rows[-2] if len(rows) >= 2 else last_r
                    steps_prev = prev_r.get("steps_prev") or last_r.get("steps_prev")
            except Exception:  # noqa: BLE001
                pass
        if sleep_prev is None:
            sl_last = self.nights(2)
            if sl_last:
                sleep_prev = sl_last[-1].get("sleep_h")

        # 3-day protein avg
        days3 = [self.tally(_day(now - i * 86400), now) for i in (1, 2, 3)]
        logged3 = [d["protein_g"] for d in days3 if d["meals"] and d["protein_g"]]
        prot_3d = round(statistics.mean(logged3)) if logged3 else None

        base_prot = int(eff.get("protein_g") or 130)
        base_cal = int(eff.get("calories") or 2100)
        base_steps = int(eff.get("steps") or 8000)
        base_bed = eff.get("bedtime") or "22:30"
        base_sleep = float(eff.get("sleep_hours") or 7.5)

        low_recovery = (sleep_prev is not None and sleep_prev < 6.0) or (readiness is not None and readiness < 65)
        already_trained_today = any(_day(w["t"]) == _day(now) for w in self.workouts(2, now))

        cal_delta, prot_delta, step_delta, bed_shift = 0, 0, 0, 0
        if low_recovery:
            mode, mode_label, intensity = "recovery", "Recovery Swap · Low Sleep", "Light / Mobility"
            step_delta = -1500
            bed_shift = -30
            why_w = (f"Slept {sleep_prev:.1f} h" if sleep_prev else "Low readiness") + " — swap heavy lifts for Zone-2 walk & mobility."
            why_c = "Hold baseline calories — avoid extra deficit while under-recovered."
            why_s = "-1,500 gentle recovery target today."
            why_b = "30 min earlier tonight to repay sleep debt."
        elif sessions_left == 0 and not already_trained_today:
            mode, mode_label, intensity = "rest", "Active Rest Day", "Rest & walk"
            cal_delta = -150
            why_w = f"Weekly {sess_7d}/{wpw} workout goal met — active recovery walk only."
            why_c = "-150 kcal rest-day trim balances lift-day fuel."
            why_s = f"Hold baseline {base_steps:,} steps."
            why_b = f"Hold normal {base_bed} bedtime."
        else:
            is_catchup = (steps_prev is not None and steps_prev < base_steps * 0.75) or (prot_3d is not None and prot_3d < base_prot - 15)
            mode = "catchup" if is_catchup else "lift"
            mode_label = "Catch-Up Lift Day" if is_catchup else "Lift Day"
            intensity = "Done today ✓" if already_trained_today else "Full lift"
            cal_delta = 100 if is_catchup else 150
            prot_delta = 15 if (prot_3d is not None and prot_3d < base_prot - 15) else 10
            if steps_prev is not None and steps_prev < base_steps * 0.75:
                step_delta = min(1000, max(500, int(round((base_steps - steps_prev) * 0.35 / 500) * 500)))
            why_w = f"{sess_7d}/{wpw} weekly sessions done — {sessions_left} left for your {wpw}/wk goal." if sessions_left else "Workout logged today!"
            why_c = f"+{cal_delta} kcal training-day fuel buffer."
            why_s = (f"+{step_delta:,} carry-over ({int(steps_prev):,} yesterday)." if step_delta
                     else f"On pace for {base_steps:,} steps.")
            why_b = f"Hold {base_bed} for {base_sleep:g} h sleep."

        why_p = (f"+{prot_delta} g to close 3-day avg gap ({prot_3d} g vs {base_prot} g)."
                 if (prot_3d is not None and prot_3d < base_prot - 15 and prot_delta)
                 else (f"+{prot_delta} g on lift days for muscle recovery." if prot_delta else f"Hold baseline {base_prot} g protein."))

        targets = {
            "workout": intensity,
            "protein_g": base_prot + prot_delta,
            "calories": max(1400, base_cal + cal_delta),
            "steps": max(3000, base_steps + step_delta),
            "bedtime": self._shift_bedtime(base_bed, bed_shift),
        }
        return {
            "mode": mode,
            "mode_label": mode_label,
            "sessions_left": sessions_left,
            "sync_adaptive": pr["sync_adaptive"],
            "targets": targets,
            "baseline": {"workouts_per_week": wpw, "protein_g": base_prot, "calories": base_cal,
                         "steps": base_steps, "bedtime": base_bed},
            "deltas": {"protein_g": prot_delta, "calories": cal_delta, "steps": step_delta, "bedtime_min": bed_shift},
            "notes": {"workout": why_w, "protein": why_p, "calories": why_c, "steps": why_s, "bedtime": why_b},
            "signals": {"sleep_prev": sleep_prev, "readiness": readiness, "steps_prev": steps_prev, "protein_3d": prot_3d},
        }

    def muscle_volume(self, days=7, now=None):
        """Weekly hard sets grouped into Push / Pull / Legs / Core."""
        now = now or self.store.clock()
        buckets = {"Push": 0, "Pull": 0, "Legs": 0, "Core": 0}
        push_k = ("bench", "press", "dip", "push-up", "pushup", "fly", "tricep", "lateral raise")
        pull_k = ("row", "pull-up", "pullup", "chin", "pulldown", "curl", "face pull", "shrug")
        leg_k = ("squat", "deadlift", "lunge", "leg", "rdl", "calf", "hip thrust", "step-up", "glute")
        for w in self.workouts(days, now):
            e = w["entry"]
            if not e.get("sets") and (e.get("miles") or e.get("minutes")):
                continue
            ex = (e.get("exercise") or "").lower()
            sets = int(e.get("sets") or 1)
            if any(k in ex for k in push_k):
                buckets["Push"] += sets
            elif any(k in ex for k in pull_k):
                buckets["Pull"] += sets
            elif any(k in ex for k in leg_k):
                buckets["Legs"] += sets
            else:
                buckets["Core"] += sets
        return [
            {"group": "Push", "sets": buckets["Push"], "target": 12},
            {"group": "Pull", "sets": buckets["Pull"], "target": 12},
            {"group": "Legs", "sets": buckets["Legs"], "target": 12},
            {"group": "Core", "sets": buckets["Core"], "target": 8},
        ]

    def cardio_summary(self, days=45, now=None):
        now = now or self.store.clock()
        runs = []
        week_mi, week_min = 0.0, 0.0
        for w in self.workouts(days, now):
            e = w["entry"]
            mi = e.get("miles")
            mn = e.get("minutes")
            if not mi and not (mn and not e.get("sets")):
                continue
            if w["t"] >= now - 7 * 86400:
                week_mi += float(mi or 0)
                week_min += float(mn or 0)
            pace = round(float(mn) / float(mi), 2) if (mi and mn and float(mi) > 0.2) else None
            runs.append({
                "d": _day(w["t"]).strftime("%b %d").replace(" 0", " "),
                "exercise": e.get("exercise") or "cardio",
                "miles": round(float(mi), 2) if mi else None,
                "minutes": round(float(mn), 1) if mn else None,
                "pace": pace,
            })
        paces = [r["pace"] for r in runs if r["pace"]]
        return {
            "week_miles": round(week_mi, 1),
            "week_minutes": round(week_min),
            "best_pace": min(paces) if paces else None,
            "runs": runs[-8:],
        }

    def lift_series(self, days=120, now=None):
        now = now or self.store.clock()
        by_ex = {}
        for w in self.workouts(days, now):
            e = w["entry"]
            r1 = e1rm(e)
            if not r1:
                continue
            ex = e["exercise"]
            by_ex.setdefault(ex, []).append({
                "d": _day(w["t"]).strftime("%b %d").replace(" 0", " "),
                "work": describe(e),
                "e1rm": round(r1),
                "weight_lb": e.get("weight_lb"),
                "sets": e.get("sets"),
                "reps": e.get("reps"),
            })
        # Keep top 6 most-logged lifts
        ranked = sorted(by_ex.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:6]
        return {ex: pts[-8:] for ex, pts in ranked}

    def week_strip(self, now=None):
        now = now or self.store.clock()
        today = _day(now)
        ws = {(_day(w["t"])).isoformat() for w in self.workouts(8, now)}
        prows = {}
        if self.patterns is not None:
            try:
                for r in self.patterns.rows(8):
                    prows[r["day"]] = r
            except Exception:  # noqa: BLE001
                pass
        out = []
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            iso = d.isoformat()
            pr = prows.get(iso) or {}
            out.append({
                "day": iso,
                "d": d.strftime("%a")[0],
                "short": d.strftime("%a"),
                "done": iso in ws,
                "today": i == 0,
                "sleep": pr.get("sleep_h"),
                "steps": pr.get("steps_prev"),
                "ready": pr.get("readiness"),
            })
        return out

    def streak_weeks(self, now=None):
        now = now or self.store.clock()
        wpw = int(self.auto_baseline(now)["effective"].get("workouts_per_week") or 3)
        streak = 0
        # Count consecutive past full 7-day windows meeting target (plus current if already met)
        if self.sessions(7, now) >= wpw:
            streak = 1
        for wk in range(1, 16):
            end_t = now - wk * 7 * 86400
            start_t = end_t - 7 * 86400
            days_hit = {_day(w["t"]) for w in self.workouts(wk * 7 + 8, now) if start_t <= w["t"] < end_t}
            if len(days_hit) >= max(1, wpw):
                streak += 1
            else:
                break
        return streak

    def routine_context(self, now=None, viewer=None):
        """Collect injury/equipment notes from MemGraph and busy/travel days from synced HA calendars."""
        now = now or self.store.clock()
        pr = self.prefs()
        notes = []
        knee_safe, shoulder_safe = False, False
        if pr["use_memgraph"] and getattr(self, "memgraph", None) is not None:
            try:
                for lnk in self.memgraph.query(categories=["HEALTH", "LIFE"], viewer=viewer, include_dormant=False, limit=16, record_access=False):
                    txt = f"{lnk.get('source_name', '')} {lnk.get('relation', '')} {lnk.get('target_name', '')}".lower()
                    if any(w in txt for w in ("knee", "patella", "meniscus", "acl")):
                        knee_safe = True
                        notes.append({"kind": "memgraph", "label": "Knee-safe swaps", "active": True})
                    elif any(w in txt for w in ("shoulder", "rotator", "impingement")):
                        shoulder_safe = True
                        notes.append({"kind": "memgraph", "label": "Shoulder-safe swaps", "active": True})
            except Exception:  # noqa: BLE001
                pass
        busy_days = {}
        if pr["use_calendar"] and getattr(self, "calendars", None) is not None:
            today = _day(now)
            busy_rx = re.compile(r"\b(?:flight|travel|airport|out of town|conference|all day|wedding|trip)\b", re.I)
            for i in range(7):
                d = today + timedelta(days=i)
                try:
                    for ev in self.calendars.events_on(d):
                        t = str(ev.get("title") or "")
                        if busy_rx.search(t):
                            busy_days[d.strftime("%a")] = re.sub(r"\s*\([^)]*\)\s*$", "", t).strip()[:32]
                            break
                except Exception:  # noqa: BLE001
                    pass
        if busy_days:
            first_d, first_t = next(iter(busy_days.items()))
            notes.append({"kind": "calendar", "label": f"Cal: {first_d} ({first_t}) → 30m Express", "active": True})
        return {"knee_safe": knee_safe, "shoulder_safe": shoulder_safe, "busy_days": busy_days, "pills": notes[:4]}

    SWAPS = {
        "bench press": ["DB Bench Press", "Incline DB Press", "Weighted Push-ups"],
        "db bench press": ["Bench Press", "Incline DB Press", "Weighted Push-ups"],
        "pull-up": ["Lat Pulldown", "Inverted Row", "Chest-Supported Row"],
        "overhead press": ["DB Shoulder Press", "Landmine Press", "Arnold Press"],
        "barbell row": ["Chest-Supported Row", "Seated Cable Row", "One-Arm DB Row"],
        "chest-supported row": ["Barbell Row", "Seated Cable Row", "One-Arm DB Row"],
        "squat": ["Goblet Squat", "Leg Press", "Box Squat"],
        "romanian deadlift": ["Hamstring Curl", "Kettlebell Swing", "Hip Thrust"],
        "walking lunges": ["Step-Ups (Knee-Safe)", "Leg Extension", "Glute Bridge"],
        "deadlift": ["Trap Bar Deadlift", "Romanian Deadlift", "Back Extension"],
        "face pulls": ["Band Pull-Aparts", "Rear Delt Fly", "Cable Lateral Raise"],
    }

    def generate_routine(self, split=None, equip=None, mins=None, note=None, now=None, viewer=None):
        """Build & save a progressive weekly workout routine from real lift history, stalls, MemGraph, and calendar."""
        now = now or self.store.clock()
        pr = self.set_prefs({k: v for k, v in (("split", split), ("equip", equip), ("mins", mins), ("note", note)) if v is not None})
        sp, eq, dur, user_note = pr["split"], pr["equip"], pr["mins"], pr["note"]
        express = dur <= 30
        stalled = {s["exercise"].lower() for s in self.stalls(now)}
        ctx = self.routine_context(now, viewer=viewer)
        dp = self.daily_plan(now, viewer=viewer)

        def _prog(ex_name, default_sets, default_reps, default_wt, group, fallback_tag="Starter"):
            key = exercise_name(ex_name)
            prev = self.last(key)
            if prev:
                pe = prev["entry"]
                pw = pe.get("weight_lb")
                ps = int(pe.get("sets") or default_sets)
                prp = int(pe.get("reps") or default_reps)
                prev_txt = f"Last: {describe(pe)}"
                if key in stalled and pw:
                    nw = max(45, int(round((pw * 0.9) / 5.0) * 5))
                    return {"name": ex_name, "sets": ps, "reps": prp, "wt": nw, "tag": "10% deload",
                            "prev": prev_txt, "kind": "deload", "group": group,
                            "swaps": self.SWAPS.get(ex_name.lower(), [])}
                if pw:
                    nw = int(round((pw + 5) / 5.0) * 5) if prp >= default_reps else int(round(pw))
                    tag = "+5 lb" if nw > pw else f"Hit {default_reps} reps"
                    return {"name": ex_name, "sets": ps, "reps": default_reps, "wt": nw, "tag": tag,
                            "prev": prev_txt, "kind": "prog", "group": group,
                            "swaps": self.SWAPS.get(ex_name.lower(), [])}
                return {"name": ex_name, "sets": ps, "reps": prp + 1, "wt": None, "tag": "+1 rep",
                        "prev": prev_txt, "kind": "prog", "group": group,
                        "swaps": self.SWAPS.get(ex_name.lower(), [])}
            return {"name": ex_name, "sets": default_sets, "reps": default_reps, "wt": default_wt,
                    "tag": fallback_tag, "prev": "No prior log yet", "kind": "prog", "group": group,
                    "swaps": self.SWAPS.get(ex_name.lower(), [])}

        bench = _prog("DB Bench Press" if eq == "db" else "Bench Press", 3, 8, 50 if eq == "db" else 135, 0)
        ohp_name = "Landmine Press" if ctx["shoulder_safe"] else ("DB Shoulder Press" if eq == "db" else "Overhead Press")
        ohp = _prog(ohp_name, 3, 5, 35 if eq == "db" else 95, 0, "Shoulder-safe" if ctx["shoulder_safe"] else "Strength")
        pullup = _prog("Pull-up", 3, 8, None, 1)
        row = _prog("Chest-Supported Row" if eq != "bw" else "Inverted Row", 3, 10, None if eq == "bw" else (45 if eq == "db" else 115), 1)
        face = {"name": "Face Pulls", "sets": 3, "reps": 15, "wt": 25 if eq == "gym" else None, "tag": "Prehab",
                "prev": "Shoulder balance", "kind": "rec", "group": 1, "swaps": self.SWAPS["face pulls"]}
        squat = _prog("Goblet Squat" if eq == "db" else "Squat", 3, 5, 50 if eq == "db" else 185, 2)
        rdl = _prog("Romanian Deadlift", 3, 8, 50 if eq == "db" else 155, 2)
        lunge = ({"name": "Step-Ups (Knee-Safe)", "sets": 3, "reps": 10, "wt": 25 if eq != "bw" else None,
                  "tag": "MemGraph", "prev": "Knee-safe swap", "kind": "rec", "group": 2,
                  "swaps": self.SWAPS["walking lunges"]}
                 if ctx["knee_safe"] else _prog("Walking Lunges", 3, 10, 30 if eq != "bw" else None, 2))
        dead = _prog("Deadlift", 1, 5, 225 if eq == "gym" else 90, 2)
        inc_db = _prog("Incline DB Press", 3, 10, 50, 0)
        core = {"name": "Hanging Knee Raises", "sets": 3, "reps": 12, "wt": None, "tag": "Core",
                "prev": "Trunk stability", "kind": "rec", "group": 3, "swaps": ["Plank (60s)", "Dead Bug", "Ab Wheel"]}

        def _warmup(main_ex):
            w = main_ex.get("wt")
            if not w or eq == "bw":
                return "2m dynamic warm-up + 1×10 easy reps"
            w1 = max(45, int(round((w * 0.55) / 5.0) * 5))
            w2 = max(45, int(round((w * 0.8) / 5.0) * 5))
            return f"{main_ex['name']}: 45×8 → {w1}×5 → {w2}×2" if eq == "gym" else f"{main_ex['name']}: {w1}×8 → {w2}×4"

        upper_a = {
            "short": "Mon · Upper A", "title": "Upper A — Bodyweight" if eq == "bw" else ("Upper A — Dumbbells" if eq == "db" else "Upper A — Strength & Push"),
            "mins": dur, "warmup": _warmup(bench),
            "why": f"Progressed from your last lifts{f' · {user_note}' if user_note else ''}.",
            "exercises": ([
                {"name": "Tempo Push-ups", "sets": 4, "reps": 12, "wt": None, "tag": "Push", "prev": "3s lower", "kind": "prog", "group": 0, "swaps": ["Incline Push-ups", "Dips"]},
                pullup,
                {"name": "Pike Push-ups", "sets": 3, "reps": 10, "wt": None, "tag": "Shoulders", "prev": "Control tempo", "kind": "prog", "group": 0, "swaps": ["Band Press"]},
                row,
            ] if eq == "bw" else [bench, pullup, ohp, row, face])[: (3 if express else 5)],
        }
        lower_a = {
            "short": "Wed · Lower A", "title": "Lower A — Squat & Hinge", "mins": dur, "warmup": _warmup(squat),
            "why": "Squat + posterior chain" + (" (knee-safe lunge swap)." if ctx["knee_safe"] else "."),
            "exercises": ([
                {"name": "Bodyweight Split Squat", "sets": 3, "reps": 12, "wt": None, "tag": "Legs", "prev": "Per leg", "kind": "prog", "group": 2, "swaps": ["Step-Ups", "Air Squats"]},
                {"name": "Single-Leg RDL", "sets": 3, "reps": 10, "wt": None, "tag": "Hinge", "prev": "Per leg", "kind": "prog", "group": 2, "swaps": ["Glute Bridge"]},
                core,
            ] if eq == "bw" else [squat, rdl, lunge, core])[: (3 if express else 4)],
        }
        fri_busy = ctx["busy_days"].get("Fri")
        fri_exp = express or bool(fri_busy)
        upper_b = {
            "short": "Fri · 30m ⚡" if fri_exp else "Fri · Upper B",
            "title": f"Upper B — 30m Express ({fri_busy})" if fri_busy else "Upper B — Pull & Conditioning",
            "mins": 30 if fri_exp else dur, "warmup": "2m band pull-aparts + 1×10 light rows",
            "why": f"Shortened for calendar event ({fri_busy})." if fri_busy else "Balances push/pull volume + aerobic base.",
            "exercises": ([inc_db, row] if fri_exp else [inc_db, row, face,
                {"name": "Zone-2 Run / Incline Walk", "sets": 1, "reps": "20 min", "wt": None, "tag": "Cardio",
                 "prev": "Aerobic base", "kind": "rec", "group": 3, "swaps": ["Cycling 20m", "Rowing 15m"]}]),
        }
        lower_b = {
            "short": "Sat · Lower B", "title": "Lower B — Deadlift Focus", "mins": dur, "warmup": _warmup(dead),
            "why": "Heavy hinge + lighter quad volume.",
            "exercises": [dead, lunge, core],
        }
        rec_day = {
            "short": "Today (Rec)", "title": "Active Recovery & Mobility", "mins": 30, "warmup": None,
            "why": dp["notes"]["workout"],
            "exercises": [
                {"name": "Brisk Zone-2 Walk", "sets": 1, "reps": "25 min", "wt": None, "tag": "Recovery", "prev": "Easy pace", "kind": "rec", "group": 3, "swaps": ["Easy Bike 20m"]},
                {"name": "Hip & Thoracic Mobility", "sets": 2, "reps": "5 min", "wt": None, "tag": "Mobility", "prev": "Zero fatigue", "kind": "rec", "group": 3, "swaps": ["Yoga Flow 10m"]},
                face,
            ],
        }

        if sp == "fb":
            days_out = [
                {**upper_a, "short": "Mon · FB A", "title": "Full Body A", "exercises": [squat, bench, pullup][: (3 if express else 4)]},
                {**lower_b, "short": "Wed · FB B", "title": "Full Body B", "exercises": [dead, ohp, row]},
                {**upper_b, "short": "Fri · 30m ⚡" if fri_exp else "Fri · FB C", "title": "Full Body C — Express" if fri_exp else "Full Body C",
                 "exercises": [rdl, inc_db, core]},
            ]
        elif sp == "ppl":
            days_out = [
                {**upper_a, "short": "Mon · Push", "title": "Push — Bench & Shoulders", "exercises": [bench, ohp, inc_db, face][: (3 if express else 4)]},
                {**upper_b, "short": "Wed · Pull", "title": "Pull — Deadlift & Back", "exercises": [dead, pullup, row]},
                {**lower_a, "short": "Fri · Legs", "title": "Legs — Squat & Posterior"},
            ]
        else:
            days_out = [upper_a, lower_a, upper_b, lower_b]

        if dp["mode"] in ("recovery", "rest"):
            days_out = [rec_day] + days_out[:3]

        saved_r = {"split": sp, "equip": eq, "mins": dur, "note": user_note, "days": days_out,
                   "context": ctx, "generated_at": now}
        self.store.kv_set("fitness_routine", saved_r)
        return saved_r

    def routine(self, now=None, viewer=None):
        now = now or self.store.clock()
        saved = self.store.kv_get("fitness_routine")
        if isinstance(saved, dict) and saved.get("days"):
            return saved
        return self.generate_routine(now=now, viewer=viewer)

    def log_weigh_in(self, weight, unit="lb", now=None, meta=None):
        from .body import KG_LB, note_text
        now = now or self.store.clock()
        if isinstance(weight, bool):
            raise ValueError("weight must be a number")
        try:
            v = float(weight)
        except (TypeError, ValueError):
            raise ValueError("weight must be a number") from None
        if not math.isfinite(v):
            raise ValueError("weight must be a finite number")
        lb = v * KG_LB if str(unit or "lb").lower().startswith("k") else v
        if not (50 <= lb <= 600):
            raise ValueError("weight must be between 50 and 600 lb")
        when = datetime.fromtimestamp(now).astimezone()
        ts = when.astimezone(timezone.utc).isoformat()
        note = note_text(when, {"weight": round(lb, 1)})
        from .speakers import VIEWER
        m = {"weigh_in": ts, "series": "weigh-in", "weight": round(lb, 1), **(meta or {})}
        who = VIEWER.get()
        if who and "owner" not in m:
            m.update(scope="personal", owner=who)
        out = self.store.remember(text=note, title=note.split(": my ")[0] + f": {lb:.1f} lb",
                                  kind="fact", shelf="body/weight", tags=["weight", "scale", "body"],
                                  source="api", meta=m, at=now)
        return {"id": out.get("id"), "weight": round(lb, 1), "answer": f"Logged weigh-in: {lb:.1f} lb."}

    def dashboard(self, now=None, viewer=None):
        """Full Fitness section payload (superset of status()): auto-baseline, adaptive daily goals,
        progressive routine, lift/cardio/weight charts, volume, streak, review, and protein boosters."""
        now = now or self.store.clock()
        base = self.status(now)
        bl = self.auto_baseline(now)
        dp = self.daily_plan(now, viewer=viewer)
        tod = base["today"]
        p_target = dp["targets"]["protein_g"] if dp["sync_adaptive"] else (bl["effective"].get("protein_g") or 0)
        p_gap = max(0, int(p_target - (tod.get("protein_g") or 0)))
        w30 = [{"d": _day(x["t"]).strftime("%b %d").replace(" 0", " "), "w": round(x["weight"], 1)}
               for x in self.weigh_ins(30, now)]
        base.update({
            "baseline": bl,
            "daily": dp,
            "routine": self.routine(now, viewer=viewer),
            "weigh_ins_30d": w30,
            "lift_series": self.lift_series(120, now),
            "cardio": self.cardio_summary(45, now),
            "volume": self.muscle_volume(7, now),
            "week_days": self.week_strip(now),
            "streak_weeks": self.streak_weeks(now),
            "protein_ideas": self.protein_ideas(p_gap, n=3) if p_gap >= 10 else [],
            "review": self.review(now),
            "prefs": self.prefs(),
        })
        return base


FIT_Q = re.compile(r"\b(?:weigh|weight|lb|pounds|protein|calories|kcal|macros|workout|work out|lift|lifted|squat|bench|"
                   r"deadlift|press|reps|gym|run|ran|steps|on track|fitness|body fat|goal|pr|personal best|exercise|"
                   r"training|muscle|cut|bulk)\b", re.I)


PLANS = re.compile(r"\b(?:remind|need to|want to|going to|gonna|will|should|plan(?:ning)? to|tomorrow|tonight|later|"
                   r"have to|how (?:many|much|long|far)|program|routine for)\b", re.I)
DONE = re.compile(r"\b(?:did|done|ran|jogged|walked|biked|cycled|rode|swam|rowed|hiked|went for|finished|completed|got in|"
                  r"squatted|benched|deadlifted|pressed|curled|lifted|hit|today|this morning|just)\b", re.I)


def is_workout_log(text):
    """A message that is a workout to log ('squats 3x8 at 185', 'went for a 3 mile run'), not a question or a plan."""
    from .langs import digits
    t = digits(text or "").strip()
    if not t or "?" in t or re.match(r"^(?:what|when|how|did i|do|is|was|should|can|which|show|tell)\b", t, re.I) \
            or PLANS.search(t):
        return False
    entries = parse_workout(t)
    if not entries:
        return False
    return bool(LOG.match(t)) or any("sets" in e for e in entries) or bool(DONE.search(t)) or bool(re.match(
        r"^(?:i\s+)?(?:\d+\s*(?:min|minutes|k|km|miles?)|an? (?:hour|\d))", t, re.I))


WORKOUT_CUE = re.compile(r"\b(?:gym|workout|worked out|work out|lift(?:ed|ing)?|sets?|reps?|run|ran|jog|walk(?:ed)?|bike|biked|"
                         r"swim|swam|row(?:ed)?|hike|hiked|yoga|treadmill|cardio|miles?|km|5k|10k)\b", re.I)


def workout_cue(text):
    """Worth asking a model to rewrite: it mentions training but the parser couldn't read it."""
    t = text or ""
    return bool(WORKOUT_CUE.search(t) or any(rx.search(t) for rx, _ in LIFT_RX)) and bool(re.search(r"\d|an hour", t))
