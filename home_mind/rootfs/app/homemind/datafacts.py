"""Data answers: questions about your own numbers, answered from the numbers.

    "what was my average sleep this week?"      -> 6.4 hours a night over this week (6 nights)
    "how many workouts did I do in October?"     -> 11 workouts in October, on 11 days
    "what's my best squat?"                       -> squat 3×5 at 225 lb on Oct 30 (estimated 1-rep max 262 lb)
    "how much protein did I eat yesterday?"       -> 128 g yesterday (target 130)

A model reading notes and doing the arithmetic gets these wrong too often. Here the question is matched to a
measure (sleep, bedtime, readiness, steps, weight, body fat, protein, calories, workouts, a lift, distance) and a
period (last night, this week, last month, in October, the past 30 days...), the value is computed from what Home
Mind saved, and the sentence is written from a template. No model is involved, so every number is right or
the answer says there's no data. Anything that isn't clearly a question about one measure over one period
("why am I tired?", "how can I sleep better?") goes to a model as before.
"""
import calendar
import re
import statistics
from datetime import date, datetime, timedelta

from .fitness import LIFT_RX, describe, e1rm

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})

ASKING = re.compile(r"\?\s*$|^\s*(?:what|what's|whats|how|how's|when|which|did|was|were|show|tell|give|list)\b", re.I)
NOT_DATA = re.compile(r"\b(?:why|should|could|can i|how (?:do|can|to|should)|tips?|improve|advice|recommend|better|"
                      r"worse|help me|normal|healthy|enough)\b", re.I)
WANTS_NUMBER = re.compile(r"\b(?:average|avg|mean|typical|usual|total|how (?:much|many|long|far|often)|most|max|"
                          r"highest|best|longest|least|min|lowest|shortest|worst|latest|last|trend|change|lost|gained|"
                          r"what (?:was|were|is|'?s) my|what'?s my|my\s+\w+\s+(?:this|last))\b", re.I)

METRICS = [
    ("bedtime", r"\b(?:bed ?time|go(?:ing)? to bed|went to bed|in bed by|fall asleep|fell asleep)\b"),
    ("sleep", r"\b(?:sleep|slept|hours of sleep)\b|\b(?:shortest|longest|worst|best)\s+nights?\b"),
    ("readiness", r"\breadiness\b"),
    ("steps", r"\bsteps?\b"),
    ("body_fat", r"\bbody ?fat\b"),
    ("weight", r"\b(?:weigh|weight|weighed|pounds|lbs?)\b"),
    ("protein", r"\bprotein\b"),
    ("calories", r"\b(?:calories|kcal|cals?)\b"),
    ("distance", r"\bhow far\b|\b(?:miles|km|kilomet(?:er|re)s)\b"),
    ("workouts", r"\b(?:workouts?|worked out|work ?outs?|gym|sessions?|trained|training sessions?|exercised)\b"),
]
METRIC_RX = [(k, re.compile(rx, re.I)) for k, rx in METRICS]
CARDIO_WORDS = {"run": r"\brun|ran|running|jog", "walk": r"\bwalk", "bike": r"\bbike|biked|cycl|rode|ride",
                "swim": r"\bswim|swam", "row": r"\brow(?:ed|ing)\b", "hike": r"\bhike|hiked|hiking"}


def _monday(d):
    return d - timedelta(days=d.weekday())


def period(question, today=None):
    """(first day, last day, label) the question is about; default the last 7 days."""
    t = (question or "").lower()
    today = today or date.today()
    if re.search(r"\blast night\b", t):
        return today, today, "last night"
    if re.search(r"\btoday\b|\bso far\b", t):
        return today, today, "today"
    if re.search(r"\byesterday\b", t):
        y = today - timedelta(days=1)
        return y, y, "yesterday"
    m = re.search(r"\b(?:past|last)\s+(\d{1,3})\s+(day|week|month)s?\b", t)
    if m:
        n = int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)]
        return today - timedelta(days=n - 1), today, f"the last {m.group(1)} {m.group(2)}s"
    if re.search(r"\bthis week\b", t):
        return _monday(today), today, "this week"
    if re.search(r"\blast week\b", t):
        s = _monday(today) - timedelta(days=7)
        return s, s + timedelta(days=6), "last week"
    if re.search(r"\bthis month\b", t):
        return today.replace(day=1), today, "this month"
    if re.search(r"\blast month\b", t):
        e = today.replace(day=1) - timedelta(days=1)
        return e.replace(day=1), e, "last month"
    if re.search(r"\bthis year\b", t):
        return today.replace(month=1, day=1), today, "this year"
    m = re.search(r"\b(?:in|during|for|of)\s+(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b", t)
    if m:
        mo = MONTHS[m.group(1)]
        y = today.year if mo <= today.month else today.year - 1
        last = date(y, mo, calendar.monthrange(y, mo)[1])
        return date(y, mo, 1), min(last, today), calendar.month_name[mo]
    if re.search(r"\b(?:lately|recently|this past week)\b", t):
        return today - timedelta(days=6), today, "the last 7 days"
    return today - timedelta(days=6), today, "the last 7 days"


def aggregate(question):
    t = (question or "").lower()
    if re.search(r"\b(?:trend|change|lost|gained|gone (?:up|down)|going (?:up|down))\b", t):
        return "change"
    if re.search(r"\b(?:most|max|highest|best|longest|latest bedtime|latest)\b", t):
        return "max"
    if re.search(r"\b(?:least|min|lowest|shortest|worst|earliest)\b", t):
        return "min"
    if re.search(r"\b(?:total|altogether|in all|how many|how often)\b", t):
        return "total"
    if re.search(r"\blast time\b|\bwhen did i last\b|\blast (?:squat|bench|deadlift|run|workout)\b", t):
        return "last"
    if re.search(r"\b(?:average|avg|mean|typical|usual|per (?:day|night)|a (?:day|night))\b", t):
        return "mean"
    return None


def parse(question, today=None):
    """{metric, exercise?, agg, start, end, label} for a question about one measure over one period, or None."""
    q = (question or "").strip()
    if not q or len(q) > 200 or not ASKING.search(q) or NOT_DATA.search(q):
        return None
    me = re.search(r"\b(?:i|i'?ve|i'?m|my|me|we|our|us)\b", q, re.I)
    when = re.search(r"\b(?:last night|today|yesterday|this (?:week|month|year)|last (?:week|month)|(?:past|last) \d+|"
                     r"in (?:" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r"))\b", q, re.I)
    if not me and (not when or re.search(r"\b(?:in|of|does|do)\s+(?:a|an|one|the)\b", q, re.I)):
        return None              # "calories in an apple", "how much does the fridge weigh": not your data
    lift = next((k for rx, k in LIFT_RX if rx.search(q)), None)
    metric = "lift" if lift else next((k for k, rx in METRIC_RX if rx.search(q)), None)
    if metric is None:
        return None
    if metric not in ("lift",) and not (WANTS_NUMBER.search(q) or re.search(
            r"\b(?:last night|today|yesterday|this week|last week|this month|last month|in \w+)\b", q, re.I)):
        return None
    start, end, label = period(q, today)
    out = {"metric": metric, "agg": aggregate(q), "start": start, "end": end, "label": label}
    if lift:
        out["exercise"] = lift
        if not re.search(r"\b(?:this|last|past|in|since|during)\b.*\b(?:week|month|year|days?|"
                         + "|".join(MONTHS) + r")\b", q, re.I):
            out["start"], out["label"] = date(2000, 1, 1), "ever"
    if metric == "distance":
        out["activity"] = next((k for k, rx in CARDIO_WORDS.items() if re.search(rx, q, re.I)), None)
    return out


def _days(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _hm(hours):
    """23.75 -> '11:45 PM', 24.5 -> '12:30 AM'."""
    h = hours % 24
    hh, mm = int(h), int(round((h - int(h)) * 60))
    if mm == 60:
        hh, mm = hh + 1, 0
    return datetime(2000, 1, 1, hh % 24, mm).strftime("%I:%M %p").lstrip("0")


def _day(d):
    return d.strftime("%a %b ") + str(d.day)


class DataFacts:
    def __init__(self, store, fitness=None, patterns=None):
        self.store, self.fitness, self.patterns = store, fitness, patterns

    # ---- series: (day, value) -----------------------------------------------------------------------------------
    def _rows(self, start):
        if not self.patterns:
            return []
        try:
            return self.patterns.rows((date.today() - start).days + 2)
        except Exception:  # noqa: BLE001
            return []

    def series(self, metric, start, end, activity=None):
        rows = {r.get("day"): r for r in self._rows(start)}
        out = []
        if metric in ("sleep", "bedtime", "readiness"):
            key = {"sleep": "sleep_h", "bedtime": "bedtime", "readiness": "readiness"}[metric]
            for d in _days(start, end):
                v = (rows.get(d.isoformat()) or {}).get(key)
                if isinstance(v, (int, float)) and (v > 0 or metric == "bedtime"):
                    out.append((d, float(v)))
        elif metric == "steps":                    # a row's steps_prev are the day before's
            for d in _days(start, end):
                v = (rows.get((d + timedelta(days=1)).isoformat()) or {}).get("steps_prev")
                if isinstance(v, (int, float)) and v > 0:
                    out.append((d, float(v)))
        elif metric in ("weight", "body_fat") and self.fitness:
            days = (date.today() - start).days + 2
            k = "weight" if metric == "weight" else "body_fat"
            for w in self.fitness.weigh_ins(days):
                d = datetime.fromtimestamp(w["t"]).date()
                if start <= d <= end and isinstance(w.get(k), (int, float)):
                    out.append((d, float(w[k])))
        elif metric in ("protein", "calories") and self.fitness:
            for d in _days(start, end):
                t = self.fitness.tally(d, now=datetime.combine(min(d + timedelta(days=1), date.today()),
                                                              datetime.min.time()).timestamp() + 1)
                if t["meals"]:
                    v = t["protein_g"] if metric == "protein" else t["calories"]
                    if v:
                        out.append((d, float(v)))
        return out

    def _sessions(self, start, end):
        if not self.fitness:
            return []
        days = (date.today() - start).days + 2
        return [w for w in self.fitness.workouts(days)
                if start <= datetime.fromtimestamp(w["t"]).date() <= end]

    # ---- answers --------------------------------------------------------------------------------------------------
    def answer(self, question, today=None):
        """{answer, metric, value, n, ...} for a data question, or None when it isn't one."""
        q = parse(question, today)
        if not q:
            return None
        try:
            out = getattr(self, "_" + q["metric"], self._measure)(q)
        except Exception:  # noqa: BLE001 - a data answer that can't be computed: a model answers instead
            return None
        if out:
            out.update(metric=q["metric"], period=q["label"], start=q["start"].isoformat(), end=q["end"].isoformat())
        return out

    UNITS = {"sleep": ("hours", "a night"), "readiness": ("", "a day"), "steps": ("steps", "a day"),
             "protein": ("g of protein", "a day"), "calories": ("calories", "a day"),
             "weight": ("lb", ""), "body_fat": ("% body fat", "")}

    def _none(self, q, what):
        when = "yet" if q["label"] == "ever" else f"for {q['label']}"
        return {"answer": f"I don't have any {what} {when}.", "n": 0, "value": None}

    def _fmt(self, metric, v):
        if metric == "bedtime":
            return _hm(v)
        if metric in ("steps", "calories", "protein", "readiness"):
            return f"{round(v):,}"
        return f"{v:.1f}".rstrip("0").rstrip(".") if metric == "sleep" else f"{v:.1f}"

    def _measure(self, q):
        m = q["metric"]
        s = self.series(m, q["start"], q["end"])
        what = {"sleep": "sleep", "bedtime": "bedtimes", "readiness": "readiness scores", "steps": "step counts",
                "weight": "weigh-ins", "body_fat": "body fat readings", "protein": "meals with protein",
                "calories": "meals with calories"}[m]
        if not s:
            return self._none(q, what)
        unit, per = self.UNITS.get(m, ("", ""))
        vals = [v for _, v in s]
        single = q["start"] == q["end"] or len(s) == 1 and q["label"] in ("last night", "today", "yesterday")
        agg = q["agg"] or ("latest" if m in ("weight", "body_fat") else "mean")
        if m == "bedtime":
            unit, per = "", ""
        when = q["label"]
        if single:
            d, v = s[-1]
            val = self._fmt(m, v)
            extra = ""
            if m == "protein" and self.fitness and self.fitness.protein_goal():
                extra = f" (target {self.fitness.protein_goal()})"
            if m == "calories" and self.fitness and self.fitness.goals().get("calories"):
                extra = f" (target {self.fitness.goals()['calories']:,})"
            text = {"sleep": f"{val} hours of sleep {when}.", "bedtime": f"In bed at {val} {when}.",
                    "readiness": f"Readiness {val} {when}.", "steps": f"{val} steps {when}.",
                    "protein": f"{val} g of protein {when}{extra}.", "calories": f"{val} calories {when}{extra}.",
                    "weight": f"{val} lb {when}.", "body_fat": f"{val}% body fat {when}."}[m]
            return {"answer": text, "value": v, "n": 1}
        if agg == "change" and len(s) >= 2:
            a, b = s[0], s[-1]
            ch = b[1] - a[1]
            return {"answer": f"{self._fmt(m, b[1])} {unit} on {_day(b[0])}, {ch:+.1f} {unit} since {_day(a[0])} "
                              f"({len(s)} readings over {when}).".replace("  ", " "), "value": ch, "n": len(s)}
        if agg in ("max", "min"):
            d, v = (max if agg == "max" else min)(s, key=lambda x: x[1])
            word = {"bedtime": ("latest", "earliest"), "sleep": ("longest", "shortest")}.get(m, ("highest", "lowest"))
            w = word[0] if agg == "max" else word[1]
            return {"answer": f"The {w} {('night' if m in ('sleep', 'bedtime') else 'day')} {when}: "
                              f"{self._fmt(m, v)} {unit} on {_day(d)} ({len(s)} logged).".replace(" ()", "")
                    .replace("  ", " "), "value": v, "n": len(s), "day": d.isoformat()}
        if agg == "total" and m in ("steps", "protein", "calories"):
            v = sum(vals)
            return {"answer": f"{self._fmt(m, v)} {unit} in all over {when} ({len(s)} days logged).", "value": v,
                    "n": len(s)}
        if agg == "latest":
            d, v = s[-1]
            return {"answer": f"{self._fmt(m, v)} {unit} on {_day(d)}, the latest of {len(s)} over {when}.", "value": v,
                    "n": len(s)}
        v = statistics.mean(vals)
        if m == "bedtime":
            return {"answer": f"In bed at {_hm(v)} on average over {when} ({len(s)} nights).", "value": v,
                    "n": len(s)}
        goal = ""
        if m == "sleep" and self.fitness and self.fitness.goals().get("sleep_hours"):
            g = self.fitness.goals()["sleep_hours"]
            goal = f"; {sum(1 for x in vals if x >= g - 0.25)} of them reached your {g:g}-hour goal"
        return {"answer": f"{self._fmt(m, v)} {unit} {per} on average over {when} ({len(s)} "
                          f"{'nights' if m == 'sleep' else 'days'} logged{goal}).".replace("  ", " "),
                "value": round(v, 2), "n": len(s)}

    def _workouts(self, q):
        ws = self._sessions(q["start"], q["end"])
        days = sorted({datetime.fromtimestamp(w["t"]).date() for w in ws})
        if not ws:
            return self._none(q, "workouts")
        kinds = {}
        for w in ws:
            kinds[w["entry"]["exercise"]] = kinds.get(w["entry"]["exercise"], 0) + 1
        top = ", ".join(f"{k} ×{n}" if n > 1 else k for k, n in sorted(kinds.items(), key=lambda x: -x[1])[:5])
        goal = ""
        if self.fitness and self.fitness.goals().get("workouts_per_week") and q["label"] in ("this week", "last week"):
            goal = f" (goal {self.fitness.goals()['workouts_per_week']})"
        return {"answer": f"{len(days)} workout{'s' if len(days) != 1 else ''} {q['label']}{goal}: {top}.",
                "value": len(days), "n": len(days)}

    def _lift(self, q):
        ex = q["exercise"]
        hist = [w for w in self._sessions(q["start"], q["end"]) if w["entry"]["exercise"] == ex]
        if not hist:
            return self._none(q, f"{ex} sessions")
        best = max((w for w in hist if e1rm(w["entry"])), key=lambda w: e1rm(w["entry"]), default=None)
        last = hist[-1]
        when_last = datetime.fromtimestamp(last["t"]).date()
        lead = f"Last {ex}: {describe(last['entry'])} on {_day(when_last)}"
        if best:
            bd = datetime.fromtimestamp(best["t"]).date()
            span = "" if q["label"] == "ever" else f" {q['label']}"
            lead += (f". Best{span}: {describe(best['entry'])} on {_day(bd)} (estimated 1-rep max "
                     f"{e1rm(best['entry']):g} lb)")
        return {"answer": lead + f" ({len(hist)} sessions).", "value": e1rm(best["entry"]) if best else None,
                "n": len(hist)}

    def _distance(self, q):
        act = q.get("activity")
        ws = [w for w in self._sessions(q["start"], q["end"]) if w["entry"].get("miles") and
              (act is None or w["entry"]["exercise"] == act)]
        if not ws:
            return self._none(q, f"{act or 'distance'} logs")
        total = sum(w["entry"]["miles"] for w in ws)
        name = {"run": "ran", "walk": "walked", "bike": "rode", "swim": "swam", "row": "rowed", "hike": "hiked"}
        verb = name.get(act, "covered")
        return {"answer": f"You {verb} {total:.1f} miles over {q['label']}, in {len(ws)} "
                          f"session{'s' if len(ws) != 1 else ''}.", "value": round(total, 2), "n": len(ws)}

