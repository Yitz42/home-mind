"""Data answers (0.28): questions about your own numbers are computed from what Home Mind saved and written from a
template, with no model doing arithmetic. Anything that isn't one measure over one period goes to a model."""
import asyncio
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.store import Store  # noqa: E402
from homemind.fitness import Fitness  # noqa: E402
from homemind.datafacts import DataFacts, parse, period  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


TODAY = date.today()


def ts(d, hour=12):
    return datetime.combine(d, datetime.min.time()).timestamp() + hour * 3600


class Patterns:
    """Nightly rows like the watch's: the last 10 days."""
    def __init__(self):
        self.data = {}
        for i in range(10):
            d = TODAY - timedelta(days=i)
            self.data[d.isoformat()] = {"day": d.isoformat(), "sleep_h": [7.0, 6.0, 8.0, 5.5, 7.5, 6.5, 7.0, 0, 6.0, 9.0][i],
                                        "bedtime": [23.5, 24.0, 23.0, 24.5, 23.25, 23.75, 23.5, 23.0, 24.0, 22.5][i],
                                        "readiness": [70, 55, 82, 40, 75, 60, 68, 0, 50, 90][i],
                                        "steps_prev": [8000, 6000, 10000, 4000, 9000, 7000, 5000, 3000, 12000, 6500][i]}

    def rows(self, days=120):
        since = (TODAY - timedelta(days=days)).isoformat()
        return [r for k, r in sorted(self.data.items()) if k >= since]


def setup():
    st = Store(tempfile.mktemp())
    fi = Fitness(st)
    for i in range(10):
        d = TODAY - timedelta(days=i)
        st.remember(text=f"weighed {180 - i * 0.2:.1f}", title="Weigh-in", kind="fact", shelf="body/weight",
                    meta={"weigh_in": ts(d, 7), "series": "weigh-in", "weight": round(180 + i * 0.2, 1)}, at=ts(d, 7))
        for h, p, c in ((8, 30, 400), (13, 45, 700), (19, 50, 900)):
            st.remember(text="meal", title="meal", shelf="body/meals", meta={"at": ts(d, h), "protein_g": p,
                                                                                "calories": c}, at=ts(d, h))
    for i, (txt) in enumerate(["squats 3x5 at 185", "ran 3 miles in 27 min", "squats 3x5 at 195, bench 3x5 at 135",
                               "ran 2 miles", "squats 3x5 at 190"]):
        d = TODAY - timedelta(days=i * 2)
        fi.log(txt, when=ts(d, 18))
    return st, fi, DataFacts(st, fi, Patterns())


def test_parse():
    T = date(2026, 11, 18)
    check("this week starts Monday", period("this week?", T)[:2] == (date(2026, 11, 16), T))
    check("last week is Monday to Sunday", period("last week?", T)[:2] == (date(2026, 11, 9), date(2026, 11, 15)))
    check("a month name: the most recent one", period("in December?", T)[:2] == (date(2025, 12, 1), date(2025, 12, 31)))
    check("past 30 days", period("past 30 days", T)[0] == T - timedelta(days=29))
    for q in ["why am I so tired?", "how can I sleep better?", "am I on track?", "remind me to weigh in",
              "is 6 hours of sleep enough?", "should I eat more protein?", "turn off the bedroom light",
              "how many calories in an apple?", "how much does the fridge weigh?", "how many steps is a mile?"]:
        check(f"left to a model: {q!r}", parse(q, T) is None)


def test_answers():
    st, fi, df = setup()
    a = df.answer("what was my average sleep over the last 7 days?")
    check("average sleep from the rows", a and a["value"] == round((7 + 6 + 8 + 5.5 + 7.5 + 6.5 + 7) / 7, 2) and
          a["n"] == 7 and "on average" in a["answer"], a)
    a = df.answer("how did I sleep last night?")
    check("last night: one value", a and a["answer"] == "7 hours of sleep last night.", a)
    a = df.answer("what was my shortest night in the past 10 days?")
    check("shortest night, with the day (a 0 isn't a night)", a and a["value"] == 5.5 and "shortest" in a["answer"]
          and a["n"] == 9, a)
    a = df.answer("what time did I go to bed on average this past week?")
    check("average bedtime as a clock time", a and "PM" in a["answer"] or "AM" in a["answer"], a)
    a = df.answer("how many steps did I take yesterday?")
    check("yesterday's steps come from today's row", a and a["value"] == 8000, a)
    a = df.answer("how much protein did I eat yesterday?")
    check("protein yesterday: the day's meals", a and a["value"] == 125 and "125 g" in a["answer"], a)
    a = df.answer("what's my average calories a day this week?")
    check("calories average", a and a["value"] == 2000, a)
    a = df.answer("what is my weight trend over the last 10 days?")
    check("weight change, first to last", a and abs(a["value"] - (180.0 - 181.8)) < 0.01, a)
    a = df.answer("how many workouts did I do in the last 10 days?")
    check("workouts counted by day", a and a["value"] == 5 and "squat" in a["answer"], a)
    a = df.answer("what's my best squat?")
    check("best squat ever, by estimated 1-rep max", a and "195" in a["answer"] and "Last squat" in a["answer"], a)
    a = df.answer("how far did I run in the last 10 days?")
    check("distance run", a and a["value"] == 5.0, a)
    a = df.answer("what's my best deadlift?")
    check("no data: says so", a and a["n"] == 0 and "don't have any deadlift" in a["answer"], a)
    a = df.answer(f"how much did I sleep in {(TODAY.replace(day=1) - timedelta(days=200)).strftime('%B')}?")
    check("a month with no rows: says so", a and a["n"] == 0, a)
    fi.set_goals({"sleep_hours": 7})
    a = df.answer("average sleep the last 7 days?")
    check("against the sleep goal", a and "reached your 7-hour goal" in a["answer"], a)


def test_mind():
    from homemind.mind import Mind
    st, fi, df = setup()
    m = Mind.__new__(Mind)
    m.store, m.views, m.fitness, m.datafacts, m.phrasebook = st, None, fi, df, None
    m._conversations = lambda store: {}
    m._handoff = lambda *a: None
    called = []

    async def ctx(*a, **k):
        called.append(a)
        raise RuntimeError("model path reached")
    m.context_for = ctx
    out = asyncio.run(m._ask("what was my average sleep over the last 7 days?"))
    check("answered from the data, no model", out and out.get("data") and out["model"] is None and not called, out)
    try:
        asyncio.run(m._ask("why am I so tired lately?"))
    except RuntimeError:
        pass
    check("a 'why' question still goes to a model", len(called) == 1)


for fn in [test_parse, test_answers, test_mind]:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(f"{fn.__name__} raised {e!r}", False)
print(f"\n{len(FAILS)} failed" if FAILS else "\nall data answer checks passed")
sys.exit(1 if FAILS else 0)
