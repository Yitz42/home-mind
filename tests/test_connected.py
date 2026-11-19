"""Connected answers (connect.py): which questions go the connected way, the symptom log, the brief, the answer
with every number checked, the side-by-side pick, private mode, and your own pattern.

  python tests/test_connected.py
"""
import asyncio
import os
import sys
import tempfile
import time
import types
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: None
    m.WSMsgType = types.SimpleNamespace(TEXT=1)
    m.ClientSession, m.ClientError, m.BasicAuth = object, Exception, (lambda *a: a)
    m.web = types.ModuleType("aiohttp.web")
    sys.modules["aiohttp"] = m
    sys.modules["aiohttp.web"] = m.web
from homemind import connect as CN, main as M  # noqa: E402
from homemind.day import Day  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.tracking import Tracking  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(c):
    return asyncio.run(c)


# ---- which questions -----------------------------------------------------------------------------------------------
for q in ("Why am I feeling so gassy lately?", "How is my health looking?", "Is my schedule really busy?",
          "how am I doing?", "why have I been so tired this week", "is my week packed?",
          "why do I keep getting headaches?"):
    check(f"deep: {q!r}", CN.wants_deep(q))
for q in ("how are my plants", "how do I reset the router", "what is the wifi password", "turn off the lights",
          "what's my HRV today?", "when is my dentist appointment?", "why is the hallway light on",
          "remember I felt gassy after lunch"):
    check(f"quick: {q!r}", not CN.wants_deep(q))
check("window: lately is 14 days, this week 7, today 3", CN.window_days("why so gassy lately") == 14 and
      CN.window_days("busy this week?") == 7 and CN.window_days("how am I today") == 3)

# ---- the app ---------------------------------------------------------------------------------------------------------
NOW = time.mktime(datetime(2026, 11, 19, 15, 0).timetuple())
clock = lambda: NOW  # noqa: E731
st = Store(os.path.join(tempfile.mkdtemp(), "mind.db"))
TODAY = date.fromtimestamp(NOW)


def day(i):
    return (TODAY - timedelta(days=i)).isoformat()


ROWS = []
for i in range(60, -1, -1):
    r = {"day": day(i), "sleep_h": 7.2 + (0.3 if i % 3 else -0.3), "hrv": 46 + (i % 5) - 2, "resting_hr": 58 + i % 2,
         "readiness": 70 + i % 4, "steps_prev": 6000 + 100 * (i % 7), "stress_prev": i % 3,
         "late_meal_prev": 19.5, "events": 2 + i % 2, "fog": 1}
    if i < 7:
        r.update(sleep_h=5.6 + 0.1 * (i % 2), hrv=36 + i % 2)        # the last week: short sleep, low HRV
    ROWS.append(r)


class Pat:
    def rows(self, days=120):
        lo = (TODAY - timedelta(days=days)).isoformat()
        return [r for r in ROWS if r["day"] >= lo]


class Cal:
    def events_on(self, d, question=None):
        n = 7 if d == TODAY else 2
        base = datetime.combine(d, datetime.min.time())
        return [{"title": f"Class {k + 1}", "body": f"Class {k + 1}", "meta": {
            "start": (base + timedelta(hours=8 + k * 1.5)).isoformat(),
            "end": (base + timedelta(hours=9 + k * 1.5)).isoformat()}} for k in range(n)]


tk = Tracking(st, None, folder=tempfile.mkdtemp(), clock=clock) if "clock" in Tracking.__init__.__code__.co_varnames \
    else Tracking(st, None, folder=tempfile.mkdtemp())
tk.clock = lambda: NOW - 7 * 86400
tk.med_said("took magnesium 200 mg", "Dana")
dy = Day(st, clock=clock)
for i in range(5):
    meal_t = NOW - i * 86400 - 3 * 3600
    st.remember(text=f"Ate lentil soup and bread at noon.", title="Ate: lentil soup and bread", shelf="body/meals",
                meta={"at": meal_t}, at=meal_t) if i in (0, 2, 3) else \
        st.remember(text="Ate salad at noon.", title="Ate: salad", shelf="body/meals", meta={"at": meal_t}, at=meal_t)

calls = []


class FakeModel:
    def __init__(self, name, reply):
        self.name, self.reply = name, reply

    async def chat(self, system, messages, max_tokens=500, temperature=0.2):
        calls.append((self.name, messages[-1]["content"][:4000], system))
        return {"text": self.reply(messages) if callable(self.reply) else self.reply}


GOOD = ("Commonly, gas comes from beans and lentils, more fibre and some supplements like magnesium. In your data, "
        "you started magnesium on Thu Nov 12 and had beans, lentils or chickpeas on 3 of 5 days with meals logged. "
        "Either could explain it; try a few days without lentils and see.")
models = {"sonnet": FakeModel("sonnet", GOOD), "local": FakeModel("local", GOOD)}


async def quick_ask(q, *a, **k):
    return {"answer": "I'm afraid I don't know why you've been feeling gassy.", "hits": [], "model": "haiku"}


async def context_for(store, q, k=8, shelf=None, model=None, expand=False, voice=False):
    return [], [], "MEMORY NOTES:\n[note | #9] Dr Lee said dairy might be worth cutting back on."
mind = types.SimpleNamespace(store=st, ask=quick_ask, context_for=context_for,
                             _chain=lambda name: [x for x in [name, "sonnet", "local"] if x],
                             route_lists=lambda: (["local"], ["sonnet"]), calendars=Cal())
modes = types.SimpleNamespace(mode=lambda p=None: "balanced")
A = types.SimpleNamespace(opts={"deep_model": "sonnet"}, mind=mind, models=types.SimpleNamespace(get=models.get),
                          patterns=Pat(), day=dy, tracking=tk, calendars=Cal(), modes=modes, decisions=None,
                          people=lambda: [{"name": "Dana"}], home=types.SimpleNamespace(catalog={}), PENDING_SECONDS=30,
                          on_red_flag=None, DEEP_VOICE_SECONDS=M.App.DEEP_VOICE_SECONDS)
A.symptoms = CN.Symptoms(st, clock=clock)
A.known_issues = CN.KnownIssues(st)
A.brief = CN.Brief(A, clock=clock)
for n in ("answer", "speaker_for", "deep_ok", "_deep_chain", "deep_answer", "answer_pick"):
    setattr(A, n, getattr(M.App, n).__get__(A))
A.has_pending = lambda cid: False
A.checkin_reply = lambda *a, **k: None
A._style = lambda res, *a, **k: asyncio.sleep(0, result=res)

# ---- the symptom log -------------------------------------------------------------------------------------------
A.symptoms.clock = lambda: NOW - 20 * 86400
A.symptoms.mention("I'm a bit bloated", "Dana")
A.symptoms.clock = lambda: NOW - 2 * 86400
A.symptoms.mention("ugh, so gassy today", "Dana")
A.symptoms.clock = clock
check("symptoms: what causes gas? is not a mention", CN.Symptoms(st).mention("what causes gas?", "Dana") == [])

# ---- the brief -------------------------------------------------------------------------------------------------
A.known_issues.add("Dana", "gas", suspects=["dairy"], source="doctor")
f = A.brief.build("Why am I feeling so gassy lately?", "Dana")
t = f["text"]
check("brief: the issue and the general causes, labelled general", "ISSUE: gas and bloating" in t and
      "GENERAL (what commonly causes it" in t, t[:400])
check("brief: the doctor's suspect", "SUSPECTED BY THEIR DOCTOR: dairy" in t, t)
check("brief: when to see a doctor", "WHEN TO SEE A DOCTOR" in t)
check("brief: the new supplement, with its day", "NEW MEDICINES OR SUPPLEMENTS" in t and "magnesium" in t, t)
check("brief: the foods that matter, counted against before", "beans, lentils or chickpeas on 3 of 5 days" in t, t)
check("brief: mentions so far", "MENTIONS of gas and bloating (said to Home Mind): 1 day(s)" in t, t)
check("brief: sleep and stress against their usual", "CHANGED AGAINST THEIR USUAL" in t and "sleep averaged" in t, t)
check("brief: no raw note text from other people (only Dana's own data)", "Dana" not in t)
fh = A.brief.build("How is my health looking?", "Dana")["text"]
check("brief (health): latest readings and what changed", "LATEST READINGS" in fh and "HRV averaged" in fh and
      "below their usual" in fh, fh)
fs = A.brief.build("Is my schedule really busy?", "Dana")["text"]
check("brief (schedule): today's load against the usual day", "CALENDAR LOAD" in fs and "7 event(s)" in fs and
      "usual day has" in fs, fs)

# ---- the answer -----------------------------------------------------------------------------------------------
r = run(A.answer({"question": "Why am I feeling so gassy lately?", "speaker": "Dana"}))
check("answer: connected, from the deep model", r.get("deep") and r["model"] == "sonnet" and "magnesium" in r["answer"],
      r)
check("answer: the quick answer beside it, with an id to pick", r.get("quick", {}).get("answer", "").startswith("I'm afraid")
      and r.get("compare_id"), r)
check("answer: the model saw the brief and the notes", "BRIEF" in calls[-1][1] and "Dr Lee" in calls[-1][1])
check("answer: the asking logged a mention (today)", A.symptoms.days("gas", "Dana").get(TODAY.isoformat()) == 1)
p = A.answer_pick(r["compare_id"], "deep")
check("pick: noted and counted", p["so_far"]["deep"] == 1)
try:
    A.answer_pick(r["compare_id"], "best")
    bad = False
except ValueError:
    bad = True
check("pick: only deep, quick or same", bad)

# a model that makes up a number: asked once more, then the quick path
models["sonnet"] = FakeModel("sonnet", "You've had gas on 11 days and 4,200 extra calories.")
models["local"] = FakeModel("local", "You've had gas on 11 days.")
calls.clear()
r = run(A.answer({"question": "Why am I feeling so gassy lately?", "speaker": "Dana"}))
check("numbers: an answer with numbers not in the brief is never given", not r.get("deep") and
      r["answer"].startswith("I'm afraid"), r)
check("numbers: it was asked again first", any("aren't in the brief" in c[1] for c in calls))
check("numbers_ok: small counts and brief numbers pass", CN.numbers_ok("3 of 5 days, 2 nights", "3 of 5 days") == [])

# private mode: local models only
models["sonnet"] = FakeModel("sonnet", GOOD)
models["local"] = FakeModel("local", GOOD)
A.modes = types.SimpleNamespace(mode=lambda p=None: "private")
calls.clear()
r = run(A.answer({"question": "Why am I feeling so gassy lately?", "speaker": "Dana"}))
check("private mode: only the local model sees the brief", r.get("model") == "local" and
      all(c[0] == "local" for c in calls), [c[0] for c in calls])
A.modes = modes

# guests and other assistants: the quick path, no log
calls.clear()
from homemind.speakers import VIEWER  # noqa: E402
tv = VIEWER.set("")
r = run(A.answer({"question": "Why am I feeling so gassy lately?"}))
VIEWER.reset(tv)
check("guest: never the connected path (it's personal)", not r.get("deep") and not calls)
A.opts["connected_answers"] = False
r = run(A.answer({"question": "How is my health looking?", "speaker": "Dana"}))
check("option off: the quick path", not r.get("deep"))
A.opts["connected_answers"] = True

# voice: short, and the quick answer if it takes too long
models["sonnet"] = FakeModel("sonnet", "Mostly diet, it seems. Want the details?")
calls.clear()
r = run(A.answer({"question": "Why am I feeling so gassy lately?", "speaker": "Dana", "voice": True}))
check("voice: told to be short, no side by side", "spoken aloud" in calls[-1][2] and not r.get("quick"), r)


class Slow(FakeModel):
    async def chat(self, *a, **k):
        await asyncio.sleep(0.5)
        return {"text": GOOD}
models["sonnet"] = Slow("sonnet", GOOD)
models["local"] = Slow("local", GOOD)
A.DEEP_VOICE_SECONDS = 0.1
r = run(A.answer({"question": "Why am I feeling so gassy lately?", "speaker": "Dana", "voice": True}))
check("voice: too slow, the quick answer instead", not r.get("deep") and r["answer"].startswith("I'm afraid"), r)

# ---- your own pattern ---------------------------------------------------------------------------------------------
rows = []
mentions = {}
for i in range(50):
    d = (TODAY - timedelta(days=50 - i)).isoformat()
    late = 22.0 if i % 5 == 0 else 19.0
    rows.append({"day": d, "late_meal_prev": late, "stress_prev": 1, "sleep_h": 7.0 + (i % 3) * 0.2})
    if late > 21:
        mentions[d] = 1
mentions[(TODAY - timedelta(days=13)).isoformat()] = 1
links = CN.personal_links("gas", mentions, rows, {})
check("pattern: a suspect that goes with the symptom is said as counts",
      links == ["mentioned on 10 of 10 days with a late meal the night before, against 1 of 40 other days"], links)
check("pattern: not with too little data", CN.personal_links("gas", {"x": 1}, rows[:10], {}) == [])

# ---- known issues ---------------------------------------------------------------------------------------------
ki = CN.KnownIssues(st)
ki.add("Dana", "eczema", suspects=["laundry soap"])
check("known issues: a new one gets its own words", CN.issues_in("my eczema is bad", ki.of("Dana")) == ["eczema"])
check("known issues: per person", ki.of("Sam") == {})

print()
print("all passed" if not FAILS else f"{len(FAILS)} failed: {', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
