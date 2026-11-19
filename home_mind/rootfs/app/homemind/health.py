"""How am I doing today: health questions answered from the readings, not from notes.

    "how's my health looking today?"   -> Today: readiness 72 (usual 68), slept 6 h 50 min (about usual), HRV 41 ms
                                          (lower than usual, 46), resting heart rate 58 bpm (about usual).
                                          Your check-in: low.
    "what's my heart rate?"            -> a live heart-rate sensor if Home Assistant has one, else this morning's resting
                                          and overnight low heart rate, said plainly as that
    "what's my HRV today?"             -> HRV 41 ms this morning (usual 46)

Before this, such questions went to a model with whatever notes the search found (a fog reading, the scale's BMR),
and the answer described the wrong data. The readings are the daily rows patterns.py saves from the watch (Steady:
readiness, sleep, HRV, resting heart rate, overnight low) plus the morning check-in. "Usual" is the middle half
(25th to 75th percentile) of the 28 days before, said as its median, and only with at least 14 of them.

Descriptive only: higher, lower or about usual, never good or bad, and no advice (red flags stay in redflags.py).
Personal: an unknown voice gets no numbers, and a recognised one gets them only alone (App.private_by_voice).
Questions about a period ("average sleep this week", "readiness in October") stay with datafacts.py.
"""
import re
import statistics
import time
from datetime import date, datetime, timedelta

HEALTH = re.compile(
    r"\bhow(?:'s|s| is| are| am)?\s+(?:my\s+)?(?:health|body|recovery|readings|numbers|vitals|stats)\b|"
    r"\bhow am i (?:doing|looking)\b|\b(?:my|today'?s?) (?:health|vitals|readings|body) (?:today|now|looking|doing)\b|"
    r"\bhealth (?:check|summary|update|today)\b", re.I)
PERIOD = re.compile(r"\b(?:week|month|year|average|avg|mean|trend|since|ago|yesterday|days|nights|january|february|"
                    r"march|april|may|june|july|august|september|october|november|december)\b", re.I)
ONE = [   # (key, words) - the first that fits wins, so the specific ones come first
    ("resting_hr", r"\bresting (?:heart ?rate|hr|pulse)\b"),
    ("night_low_hr", r"\b(?:lowest|overnight|night(?:time)?) (?:heart ?rate|hr|pulse)\b"),
    ("hrv", r"\bhrv\b|\bheart ?rate variability\b"),
    ("heart", r"\b(?:heart ?rate|pulse|bpm)\b"),
    ("readiness", r"\breadiness\b"),
    ("recovery", r"\brecovery\b"),
    ("sleep_h", r"\b(?:sleep|slept)\b"),
]
ONE_RX = [(k, re.compile(rx, re.I)) for k, rx in ONE]
ASKING = re.compile(r"\?\s*$|^\s*(?:what|what's|whats|how|how's|hows|tell|give|show)\b", re.I)
NOT_THIS = re.compile(r"\b(?:why|should|how (?:do|can|to)|tips?|improve|advice|normal|healthy|lower my|raise my|"
                      r"remind|set|log|record)\b", re.I)
LABEL = {"readiness": ("readiness", ""), "recovery": ("recovery", "%"), "sleep_h": ("slept", ""),
         "hrv": ("HRV", " ms"), "resting_hr": ("resting heart rate", " bpm"),
         "night_low_hr": ("overnight low heart rate", " bpm")}
SUMMARY = ("readiness", "sleep_h", "hrv", "resting_hr")
LIVE_MAX_AGE = 30 * 60
NEAR_MIN = 0.02       # "about usual" is never narrower than 2% either side of the median
USUAL_MIN_DAYS = 14


def _hm(h):
    m = round(h * 60)
    return f"{m // 60} h {m % 60:02d} min" if m % 60 else f"{m // 60} h"


def _fmt(k, v):
    if k == "sleep_h":
        return _hm(v)
    return f"{round(v):g}{LABEL[k][1]}"


def which(question):
    """'summary', one reading's key, or None (not a question for this module)."""
    q = (question or "").replace("’", "'")
    if not ASKING.search(q) or NOT_THIS.search(q) or PERIOD.search(q):
        return None
    if HEALTH.search(q):
        return "summary"
    for k, rx in ONE_RX:
        if rx.search(q):
            if k in ("sleep_h", "readiness", "recovery") and not re.search(r"\b(?:today|now|this morning|last night|"
                                                                             r"current|currently)\b", q, re.I):
                return None       # "what's my readiness?" alone stays with datafacts (its "latest")
            return k
    return None


class Health:
    def __init__(self, patterns=None, day=None, ha=None, clock=None):
        self.patterns = patterns
        self.day = day
        self.ha = ha
        self.clock = clock or time.time

    def _rows(self):
        try:
            return self.patterns.rows(40) if self.patterns is not None else []
        except Exception:  # noqa: BLE001
            return []

    def _usual(self, rows, k, before):
        """(median, low, high) of the 28 days before: "usual" is the middle half of your own days (the 25th to the
        75th percentile), so a reading that swings a lot (HRV, sleep) isn't called higher or lower most days, and a
        steady one (resting heart rate) is. At least 14 days first."""
        vals = [r[k] for r in rows if r.get("day", "") < before and r.get(k) is not None][-28:]
        if len(vals) < USUAL_MIN_DAYS:
            return None
        q = statistics.quantiles(vals, n=4, method="inclusive")
        med = statistics.median(vals)
        lo, hi = min(q[0], med * (1 - NEAR_MIN)), max(q[2], med * (1 + NEAR_MIN))
        return med, lo, hi

    def _vs(self, k, v, usual):
        if not usual or not usual[0]:
            return _fmt(k, v)
        med, lo, hi = usual
        if lo <= v <= hi:
            return f"{_fmt(k, v)} (about usual)"
        return f"{_fmt(k, v)} ({'higher' if v > hi else 'lower'} than usual, {_fmt(k, med)})"

    def _latest(self, rows):
        """Today's row, or the latest with readings, and how to say which day it is."""
        today = date.fromtimestamp(self.clock()).isoformat()
        for r in reversed(rows):
            if any(r.get(k) is not None for k in LABEL):
                if r["day"] == today:
                    return r, "Today"
                d = date.fromisoformat(r["day"])
                return r, ("Yesterday" if d == date.fromisoformat(today) - timedelta(days=1)
                           else f"On {d.strftime('%a %b')} {d.day}")
        return None, None

    async def live_heart_rate(self):
        """A heart-rate sensor in Home Assistant updated in the last 30 minutes: (bpm, name, minutes ago) or None.
        The sensor is looked for among all states at most once an hour; in between only it is read."""
        if self.ha is None or not getattr(self.ha, "available", True):
            return None
        found = self.__dict__.get("_hr_sensor")
        try:
            if found and self.clock() - found[1] < 3600 and hasattr(self.ha, "state"):
                one = await self.ha.state(found[0])
                states = [dict(one, entity_id=found[0])] if one else []
            else:
                states = await self.ha.states()
        except Exception:  # noqa: BLE001
            return None
        best = None
        for s in states or []:
            eid, a = s.get("entity_id", ""), s.get("attributes") or {}
            if not eid.startswith("sensor."):
                continue
            name = (a.get("friendly_name") or eid).lower()
            if "resting" in name or "variab" in name or "steady" in eid or "low" in name:
                continue
            if not ((a.get("unit_of_measurement") or "").lower() in ("bpm", "beats/min", "count/min")
                    or "heart rate" in name or "heart_rate" in eid):
                continue
            try:
                v = float(s.get("state"))
                at = datetime.fromisoformat(s.get("last_updated") or s.get("last_changed")).timestamp()
            except (TypeError, ValueError):
                continue
            age = self.clock() - at
            if 20 <= v <= 230 and age <= LIVE_MAX_AGE and (best is None or age < best[2] * 60):
                best = (round(v), a.get("friendly_name") or eid, max(0, round(age / 60)))
                if not found or found[0] != eid or self.clock() - found[1] >= 3600:
                    self.__dict__["_hr_sensor"] = (eid, self.clock())
        if best is None and found:
            self.__dict__.pop("_hr_sensor", None)      # gone quiet: look among all states next time
        return best

    async def answer(self, question, person=None):
        k = which(question)
        if not k:
            return None
        rows = self._rows()
        r, when = self._latest(rows)
        if k == "heart":
            live = await self.live_heart_rate()
            if live:
                bpm, name, mins = live
                ago = "just now" if mins < 2 else f"{mins} minutes ago"
                return {"answer": f"Your heart rate is {bpm} bpm ({name}, {ago}).", "kind": "heart", "live": True}
            parts = [f"{LABEL[x][0]} {self._vs(x, r[x], self._usual(rows, x, r['day']))}"
                     for x in ("resting_hr", "night_low_hr") if r and r.get(x) is not None]
            if not parts:
                return {"answer": "I don't get a live heart rate, and there's no heart-rate reading from your watch "
                                  "yet.", "kind": "heart"}
            return {"answer": "I don't get a live heart rate, only what your watch reports each morning. "
                              f"{when}: {', '.join(parts)}.", "kind": "heart"}
        if k == "summary":
            if r is None:
                return {"answer": "I don't have any readings from your watch yet, so I can't say.", "kind": k}
            parts = [f"{LABEL[x][0]} {self._vs(x, r[x], self._usual(rows, x, r['day']))}"
                     for x in SUMMARY if r.get(x) is not None]
            out = f"{when}: " + ", ".join(parts) + "." if parts else f"{when}: no watch readings."
            if r.get("fog") is not None and when == "Today":
                out += f" Fog: {round(r['fog']):g}."
            if self.day is not None and when == "Today":
                try:
                    st = self.day.get(person)
                except Exception:  # noqa: BLE001
                    st = None
                if st and st != "unknown":
                    out += f" Your check-in: {st}."
            return {"answer": out[0].upper() + out[1:], "kind": k}
        if r is None or r.get(k) is None:
            return {"answer": f"There's no {LABEL[k][0]} reading from your watch yet.", "kind": k}
        if k == "sleep_h":
            night = "Last night" if when == "Today" else when       # a row's sleep is the night before that day
            return {"answer": f"{night}: slept {self._vs(k, r[k], self._usual(rows, k, r['day']))}.", "kind": k}
        said = f"{when}: {LABEL[k][0]} {self._vs(k, r[k], self._usual(rows, k, r['day']))}."
        return {"answer": said, "kind": k}
