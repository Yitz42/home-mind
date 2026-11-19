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
    r"horrible|crashing|crashed|wiped|wiped out|wrecked|ugh+|blah|exhausted|drained|foggy|flaring|"
    r"in a flare|knackered|shattered)"
    r"(?:\s+(?:day|one|morning|today|so far))*\s*[.!]*\s*$", re.I)
# "good morning", "good night": a greeting, never an answer
_GREETING = re.compile(r"^\s*(?:(?:hi|hello|hey)\b[\s,]*)?(?:good|great)\s+(?:morning|afternoon|evening|night)"
                       r"(?:\s+\w+)?\s*[.!,]*\s*$", re.I)
# a reply to something else said in the same breath ("ok do it", "yes please", "thanks"): not a check-in answer
_OTHER_REPLY = re.compile(r"\b(?:do it|go ahead|yes|yeah|yep|yup|sure|please|thanks|thank you|cancel|never ?mind|"
                          r"stop|remind|later)\b", re.I)
_MAP = {"good": "good", "great": "good", "fine": "good", "okay": "good", "ok": "good", "alright": "good",
        "decent": "good", "low": "low", "meh": "low", "tired": "low", "so-so": "low", "so so": "low",
        "not great": "low", "blah": "low", "rough": "rough", "bad": "rough", "awful": "rough", "terrible": "rough",
        "horrible": "rough", "crashing": "rough", "crashed": "rough", "wiped": "rough", "wrecked": "rough",
        "wiped out": "rough", "exhausted": "rough", "flaring": "rough", "in a flare": "rough", "knackered": "rough",
        "shattered": "rough", "drained": "low", "foggy": "low"}


_DAYISH = re.compile(r"\b(?:day|today|morning|feel|feeling|i'?m|im|i am)\b", re.I)
_BARE_OK = {"low", "meh", "rough", "awful", "terrible", "horrible", "crashing", "crashed", "wiped", "wrecked", "blah",
            "wiped out", "exhausted", "flaring", "in a flare", "knackered", "shattered", "drained", "foggy"}


_LOOSE = re.compile(
    r"\b(not (?:too |that |so )?bad|not (?:great|good|so good|too good)|so-so|so so|good|great|fine|okay|ok|alright|"
    r"decent|low|meh|tired|rough|bad|awful|terrible|horrible|crashing|crashed|wiped(?: out)?|wrecked|blah|ugh+|"
    r"exhausted|drained|foggy|flaring|in a flare|knackered|shattered)(?:ish|-ish)?\b",
    re.I)
_HOUSE = re.compile(r"\b(?:turn|switch|set|dim|brighten|open|close|shut|lock|unlock|start|stop|run|play|pause|resume|"
                    r"raise|lower|increase|decrease|activate|deactivate|toggle|lights?|lamps?|fans?|blinds?|heat(?:ing|er)?|"
                    r"\d+)\b", re.I)
_LOOSE_MAP = dict(_MAP, **{"not bad": "good", "not too bad": "good", "not that bad": "good", "not so bad": "good",
                           "not good": "low", "not so good": "low", "not too good": "low",
                           "not great": "low"})


def _loose(t):
    """Right after the question: a short reply with an answer word in it ("I would say it's about goodish",
    "pretty much fine I guess", "not too bad"). Every answer word counts and the most careful wins ("fine, just
    tired" is low); questions, long replies, greetings and replies to something else ("ok do it") don't count."""
    if "?" in t or len(re.findall(r"[A-Za-z']+", t)) > 12 or _HOUSE.search(t):
        return None       # a question, a long reply, or a house request ("set the fan to low"): not an answer
    if _OTHER_REPLY.search(t) or _GREETING.match(t):
        return None       # "ok do it", "yes please", "good morning": a reply to something else
    found = []
    for m in _LOOSE.finditer(t):
        w = re.sub(r"-?ish$", "", m.group(1).lower())
        found.append("rough" if w.startswith("ugh") else _LOOSE_MAP.get(w))
    found = [f for f in found if f]
    # "fine, just tired": every answer word counts, and the most careful one wins
    return max(found, key=("good", "low", "rough").index) if found else None


def parse(text, expecting=False):
    """A check-in answer in a few words ("rough", "pretty good", "having a low day", "ugh"), or None. A bare
    everyday word ("ok", "fine", "good", "bad") only counts when the question was just asked (expecting); otherwise
    it needs the day in it ("good day", "I'm fine today")."""
    t = (text or "").replace("’", "'")
    if _GREETING.match(t):
        return None
    m = _ANSWER.match(t)
    if not m:
        return _loose(t) if expecting else None
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
        """good, low, rough, or unknown (no answer yet). With no person: the most careful answer anyone gave today
        (the household's tap counts as one of them, never over them)."""
        t = self._all().get(today(self.clock())) or {}
        given = [v["state"] for k, v in t.items() if not k.startswith("?") and v.get("state") in STATES]
        if person is None:
            # the household: the most careful answer anyone gave, a tap with no name included (one person's rough
            # day is never overridden by someone else's "good" tap)
            return max(given, key=STATES.index) if given else "unknown"
        r = t.get(self._key(person))
        if r and r.get("state") in STATES:
            return r["state"]
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


class AskBatch:
    """Routine asks held for the morning check-in (design step 20: "batched into the morning check-in"). Each is
    kept for a day; the morning takes as many as the day's budget allows, newest first, and the rest expire quietly."""
    KV = "ask_batch"
    KEEP = 86400

    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or time.time

    def add(self, title, message):
        items = [i for i in (self.store.kv_get(self.KV) or []) if self.clock() - i["at"] <= self.KEEP]
        items.append({"title": str(title or "")[:120], "message": str(message or "")[:500], "at": self.clock()})
        self.store.kv_set(self.KV, items[-20:])
        return len(items)

    def take(self, limit):
        """Up to `limit` held asks (newest first); everything held is cleared (what isn't taken expires)."""
        items = [i for i in (self.store.kv_get(self.KV) or []) if self.clock() - i["at"] <= self.KEEP]
        self.store.kv_set(self.KV, [])
        return list(reversed(items))[:max(0, int(limit))]

    def pending(self):
        return len([i for i in (self.store.kv_get(self.KV) or []) if self.clock() - i["at"] <= self.KEEP])
