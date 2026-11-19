"""How the day is going: the morning check-in (design spec step 33) and the day's ask budget (step 20).

The check-in is one of good, low or rough, per person, per day, answered by a word ("rough"), a tap on the morning
notification, or the API. It never starts as an alarm: by voice it is asked once, the first time the person speaks
that day, at the end of whatever they asked. No answer is "unknown": the day is treated cautiously, but it is never
recorded as rough.

What it changes:
  - the routine asks a day may hold (notifications, reminders, nudges): good about 4, low 1, rough 0, unknown 1.
    Safety always goes out. On a rough day every device stays quiet except safety and time-bound requests the
    person started (an approval they just asked for).
  - energy left on Today: the readiness score already there, held down on a low, rough or unknown day (no new
    number). Rough: "Taking it easy today. Nothing needs you."
"""
import re
import time
from datetime import datetime

STATES = ("good", "low", "rough")
BUDGET = {"good": 4, "low": 1, "rough": 0, "unknown": 1}
CAP = {"good": 100, "low": 50, "rough": 20, "unknown": 60}     # energy left is never above this on such a day
REPLIES = {"good": "Good to hear. I'll keep things moving.",
           "low": "OK, a low day. I'll keep it light: just one thing if it matters.",
           "rough": "Taking it easy today. Nothing needs you."}
QUESTION = "One thing: good, low or rough today?"
DAY_STARTS = 5            # before 5 am still counts as the day before

_ANSWER = re.compile(
    r"^\s*(?:(?:it'?s|i'?m|im|i am|i feel|feeling|having|today'?s?|today is|pretty|kind of|kinda|a|an|so|very|really|"
    r"quite|bit of a|a bit|honestly|um+|uh+|hmm+|well|,|\s)+)?"
    r"(good|great|fine|okay|ok|alright|decent|low|meh|tired|so-so|so so|not great|rough|bad|awful|terrible|"
    r"horrible|crashing|crashed|wiped|wrecked|ugh+|blah)"
    r"(?:\s+(?:day|one|morning|today|so far))*\s*[.!]*\s*$", re.I)
_MAP = {"good": "good", "great": "good", "fine": "good", "okay": "good", "ok": "good", "alright": "good",
        "decent": "good", "low": "low", "meh": "low", "tired": "low", "so-so": "low", "so so": "low",
        "not great": "low", "blah": "low", "rough": "rough", "bad": "rough", "awful": "rough", "terrible": "rough",
        "horrible": "rough", "crashing": "rough", "crashed": "rough", "wiped": "rough", "wrecked": "rough"}


_DAYISH = re.compile(r"\b(?:day|today|morning|feel|feeling|i'?m|im|i am)\b", re.I)
_BARE_OK = {"low", "meh", "rough", "awful", "terrible", "horrible", "crashing", "crashed", "wiped", "wrecked", "blah"}


def parse(text, expecting=False):
    """A check-in answer in a few words ("rough", "pretty good", "having a low day", "ugh"), or None. A bare
    everyday word ("ok", "fine", "good", "bad") only counts when the question was just asked (expecting); otherwise
    it needs the day in it ("good day", "I'm fine today")."""
    t = (text or "").replace("’", "'")
    m = _ANSWER.match(t)
    if not m:
        return None
    w = m.group(1).lower()
    if not expecting and not _DAYISH.search(t) and w not in _BARE_OK and not w.startswith("ugh"):
        return None
    w = m.group(1).lower()
    if w.startswith("ugh"):
        return "rough"
    return _MAP.get(w)


def today(now=None):
    d = datetime.fromtimestamp(now or time.time())
    if d.hour < DAY_STARTS:
        d = datetime.fromtimestamp((now or time.time()) - 86400)
    return d.date().isoformat()


class Day:
    KV = "day_checkin"

    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or time.time

    def _all(self):
        d = dict(self.store.kv_get(self.KV) or {})
        keep = sorted(d)[-14:]          # two weeks is plenty here; the record of days lives in patterns
        return {k: d[k] for k in keep}

    def _key(self, person):
        return (person or "").strip().lower() or "_"

    def set(self, state, person=None, how="said"):
        if state not in STATES:
            raise ValueError(f"the day is one of {', '.join(STATES)}")
        allx = self._all()
        t = today(self.clock())
        allx.setdefault(t, {})[self._key(person)] = {"state": state, "at": self.clock(), "how": how}
        self.store.kv_set(self.KV, allx)
        return state

    def get(self, person=None):
        """good, low, rough, or unknown (no answer yet). With no person: the household's own answer, else the
        most careful answer anyone gave today."""
        t = self._all().get(today(self.clock())) or {}
        r = t.get(self._key(person))
        if r:
            return r["state"]
        given = [v["state"] for k, v in t.items() if not k.startswith("?") and v.get("state") in STATES]
        if person is None and given:
            return max(given, key=STATES.index)
        return "unknown"

    def asked(self, person=None):
        """Has the check-in question been asked today (by voice)? Marks it asked."""
        allx = self._all()
        day = allx.setdefault(today(self.clock()), {})
        k = "?" + self._key(person)
        if day.get(k):
            return True
        day[k] = {"state": "asked", "at": self.clock()}
        self.store.kv_set(self.KV, allx)
        return False

    def expecting(self, person=None, window=900):
        """Was the question asked (by voice or the morning notification) in the last 15 minutes, with no answer?"""
        if self.get(person) != "unknown":
            return False
        day = self._all().get(today(self.clock())) or {}
        r = day.get("?" + self._key(person)) or day.get("?_")
        return bool(r) and self.clock() - r["at"] <= window

    def budget(self, person=None):
        return BUDGET[self.get(person)]

    def energy(self, readiness, person=None):
        """Energy left: the readiness score, held down on a low, rough or unknown day. None without a score."""
        try:
            r = float(readiness)
        except (TypeError, ValueError):
            return None
        return round(min(r, CAP[self.get(person)]))
