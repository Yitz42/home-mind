"""Fitness goals: numbers to aim for, progress against them, the day's tally, workouts, a weekly review, and
targets that adjust from what actually happens.

Built on what Home Mind already collects: weigh-ins from the scale (body/weight), meals with protein and calorie
estimates (body/meals), and the watch's readiness, sleep and steps (patterns). Workouts are new: "squats 3x8 at
185", "bench press 3 sets of 5 at 135", "ran 3 miles in 28 minutes" become notes on body/workouts.

Nothing here changes a goal by itself: adjustments are suggestions the person accepts. And it never suggests eating
less than CALORIE_FLOOR or losing faster than about 1% of body weight a week.
"""
import json
import re
import statistics
import time
from datetime import date, datetime, timedelta

from . import nutrition

KV = "fitness_goals"
CALORIE_FLOOR = 1500          # never suggested below this
MAX_LOSS_SHARE = 0.01         # never suggest losing faster than ~1% of body weight a week
GOALS = {
    "weight_target": (float, 50, 600),       # lb
    "weight_by": (str, None, None),          # YYYY-MM-DD
    "protein_g": (int, 30, 400),             # a day; None = from body weight (1.6 g/kg)
    "calories": (int, 1200, 6000),           # a day
    "steps": (int, 500, 50000),              # a day
    "workouts_per_week": (int, 0, 14),
    "body_fat_target": (float, 3, 60),       # %
}
PROTEIN_FOODS = [("Greek yogurt", 17), ("cottage cheese", 14), ("protein shake", 25), ("eggs", 12), ("tuna", 25),
                 ("chicken", 30), ("turkey", 25), ("jerky", 10), ("edamame", 11), ("tofu", 15), ("milk", 8),
                 ("string cheese", 7), ("salmon", 25), ("lentils", 9)]


# ---- workouts: reading them --------------------------------------------------------------------------------------
UNIT = r"(?:lb|lbs|pounds?|kg|kilos?)"
STRENGTH = [
    re.compile(r"(?P<ex>[a-z][a-z '\-]*?)\s*[:,\-]?\s*(?P<sets>\d{1,2})\s*(?:x|×|by)\s*(?P<reps>\d{1,3})"
               r"(?:\s*(?:reps?)?\s*(?:at|@|with|x|×)?\s*(?P<w>\d{1,4}(?:\.\d)?)\s*(?P<u>" + UNIT + r")?)?\b", re.I),
    re.compile(r"(?P<ex>[a-z][a-z '\-]*?)\s*[:,\-]?\s*(?P<sets>\d{1,2})\s*sets?\s*(?:of\s*)?(?P<reps>\d{1,3})\s*(?:reps?)?"
               r"(?:\s*(?:at|@|with)\s*(?P<w>\d{1,4}(?:\.\d)?)\s*(?P<u>" + UNIT + r")?)?\b", re.I),
]
CARDIO_DIST = re.compile(r"\b(?P<act>ran|run|jogged|walked|walk|biked|cycled|rode|swam|rowed|hiked)\s+(?P<d>\d+(?:\.\d+)?)"
                         r"\s*(?P<du>miles?|mi|km|k|kilometers?)?(?:\s+in\s+(?P<m>\d{1,3})\s*(?:min|minutes|mins))?", re.I)
CARDIO_TIME = re.compile(r"\b(?P<m>\d{1,3})\s*(?:min|minutes|mins)\s+(?:of\s+)?(?P<act>running|run|bike|biking|cycling|"
                         r"walk|walking|yoga|swim|swimming|rowing|elliptical|hiit|cardio|stretching|treadmill|stairs|"
                         r"spin|pilates)\b", re.I)
LOG = re.compile(r"^\s*(?:log|logged|did|done|workout|gym|lifted|today i did|i did)\b[:\s,]*", re.I)
ALIASES = {"bench": "bench press", "squat": "squat", "squats": "squat", "back squat": "squat", "dead": "deadlift",
           "deadlifts": "deadlift", "dl": "deadlift", "ohp": "overhead press", "press": "overhead press",
           "pull ups": "pull-up", "pull-ups": "pull-up", "pullups": "pull-up", "chin ups": "chin-up",
           "push ups": "push-up", "push-ups": "push-up", "pushups": "push-up", "rows": "row", "lunges": "lunge",
           "curls": "curl", "dips": "dip"}
NOT_EXERCISE = {"i", "i did", "did", "and", "then", "plus", "also", "log", "workout"}


def exercise_name(s):
    s = re.sub(r"\s+", " ", (s or "").lower().strip(" ,.:-"))
    s = re.sub(r"^(?:and|then|plus|also|did|i did|log|logged)\s+", "", s)
    s = ALIASES.get(s, s)
    if s.endswith("es") and s[:-2] in ALIASES.values():
        s = s[:-2]
    elif s.endswith("s") and not s.endswith("ss") and len(s) > 4:
        s = ALIASES.get(s[:-1], s[:-1])
    return s


def parse_workout(text):
    """'squats 3x8 at 185, bench 3 sets of 5 at 135 and ran 2 miles in 18 min' -> entries, or [] if it isn't a
    workout. Weights are kept in pounds (kg converted)."""
    t = LOG.sub("", (text or "").replace("’", "'"))
    out = []
    for part in re.split(r"\s*(?:,|;|\band\b|\bthen\b|\bplus\b)\s*", t):
        part = part.strip()
        if not part:
            continue
        m = next((rx.search(part) for rx in STRENGTH if rx.search(part)), None)
        if m:
            ex = exercise_name(m.group("ex"))
            if not ex or ex in NOT_EXERCISE or len(ex) > 40:
                continue
            sets, reps = int(m.group("sets")), int(m.group("reps"))
            if not (1 <= sets <= 20 and 1 <= reps <= 100):
                continue
            e = {"exercise": ex, "sets": sets, "reps": reps}
            if m.group("w"):
                w = float(m.group("w"))
                if m.group("u") and m.group("u").lower().startswith("k"):
                    w = round(w * 2.20462, 1)
                e["weight_lb"] = w
            out.append(e)
            continue
        m = CARDIO_DIST.search(part)
        if m:
            d = float(m.group("d"))
            km = (m.group("du") or "mi").lower().startswith("k")
            act = {"ran": "run", "jogged": "run", "walked": "walk", "biked": "bike", "cycled": "bike", "rode": "bike",
                   "swam": "swim", "rowed": "row", "hiked": "hike"}.get(m.group("act").lower(), m.group("act").lower())
            e = {"exercise": act, "miles": round(d * 0.621371, 2) if km else d}
            if m.group("m"):
                e["minutes"] = int(m.group("m"))
            out.append(e)
            continue
        m = CARDIO_TIME.search(part)
        if m:
            act = m.group("act").lower()
            act = {"running": "run", "biking": "bike", "cycling": "bike", "walking": "walk", "swimming": "swim",
                   "spin": "bike"}.get(act, act)
            out.append({"exercise": act, "minutes": int(m.group("m"))})
    return out


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
        return cur

    # ---- weight ----------------------------------------------------------------------------------------------------
    def weigh_ins(self, days=120, now=None):
        now = now or self.store.clock()
        out = []
        for r in self.store.db.execute("SELECT created, meta FROM items WHERE shelf='body/weight' AND status='active' "
                                       "AND created >= ? ORDER BY created", (now - days * 86400,)):
            m = json.loads(r["meta"] or "{}")
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
    def log(self, text, when=None, source="api", meta=None):
        entries = parse_workout(text)
        if not entries:
            raise ValueError("couldn't read a workout (try 'squats 3x8 at 185' or 'ran 3 miles in 28 min')")
        now = when or self.store.clock()
        prev = {e["exercise"]: self.last(e["exercise"], before=now) for e in entries}
        words = "; ".join(describe(e) for e in entries)
        r = self.store.remember(text=f"Workout {datetime.fromtimestamp(now):%a %b %d}: {words}.",
                                title=f"Workout: {words}"[:100], kind="note", shelf="body/workouts",
                                tags=["workout"] + sorted({e["exercise"].split()[0] for e in entries})[:5],
                                source=source, meta=dict(meta or {}, workout=entries, at=now), at=now)
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
        if wa.get("sleep_h"):
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


FIT_Q = re.compile(r"\b(?:weigh|weight|lb|pounds|protein|calories|kcal|macros|workout|work out|lift|lifted|squat|bench|"
                   r"deadlift|press|reps|gym|run|ran|steps|on track|fitness|body fat|goal|pr|personal best|exercise|"
                   r"training|muscle|cut|bulk)\b", re.I)


def is_workout_log(text):
    """A message that is a workout to log ('squats 3x8 at 185', 'log: ran 3 miles'), not a question about one."""
    t = (text or "").strip()
    if not t or "?" in t or re.match(r"^(?:what|when|how|did|do|is|was|should|can|which|show|tell)\b", t, re.I):
        return False
    entries = parse_workout(t)
    if not entries:
        return False
    return bool(LOG.match(t)) or any("sets" in e for e in entries) or bool(re.match(
        r"^(?:i\s+)?(?:ran|jogged|walked|biked|cycled|rode|swam|rowed|hiked|\d+\s*min)", t, re.I))
