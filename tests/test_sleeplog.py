"""Sleep said in words (0.28): "Slept from 12 to 7am last night" in the Ask box is filed as last night's sleep and
beats the watch. The watch's 0s (off or not synced) aren't readings, and yesterday's fog rating doesn't make today a
"brain fog" day. No Home Assistant needed."""
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.store import Store  # noqa: E402
from homemind.sleeplog import parse_sleep, log_sleep  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


NOW = datetime(2026, 11, 17, 11, 3).timestamp()


def test_parse():
    want = {"Slept from 12 to 7am last night": (7.0, 0, 7), "slept 11:30pm-6:45": (7.25, 23, 6),
            "went to bed at 11 and woke up at 6": (7.0, 23, 6), "slept from 1 to 7": (6.0, 1, 7),
            "slept from midnight to 8": (8.0, 0, 8), "slept from 10 to 6": (8.0, 22, 6),
            "Slept from 12:30 am to 7:15 am": (6.75, 0, 7)}
    for t, (h, a, b) in want.items():
        r = parse_sleep(t, NOW)
        check(f"parse {t!r}", r and r["hours"] == h and r["start"].hour == a and r["end"].hour == b, r)
    for t, h in {"slept 6.5 hours": 6.5, "got 7 hours of sleep last night": 7.0, "slept 7 and a half hours": 7.5}.items():
        r = parse_sleep(t, NOW)
        check(f"parse {t!r}", r and r["hours"] == h, r)
    for t in ["how long did I sleep?", "slept 7 hours on Friday", "I slept well", "what time did I go to bed",
              "slept 40 hours", "turn on the bedroom light", "remind me to sleep at 11", ""]:
        check(f"not a sleep log: {t!r}", parse_sleep(t, NOW) is None)
    said = {"I got to sleep around midnight and woke at 7": 7.0, "crashed at 12, up at 7": 7.0, "was in bed 12-7": 7.0,
            "fell asleep at 1 and got up at 7:30": 6.5, "Slept between 12 and 7": 7.0, "sleep last night 12-7": 7.0,
            "I slept like 6 hours": 6.0, "only got 5 hours last night": 5.0, "7 hours of sleep last night": 7.0,
            "bed at 12, up at 7": 7.0, "I went to bed at midnight, alarm at 7": 7.0,
            "dozed off around 11:30, woke 6:45": 7.25, "slept poorly, maybe 4 hours": 4.0, "slept 6-7 hours": 6.5,
            "hit the sack at 11, woke up at 6": 7.0, "Got 6h sleep": 6.0}
    for t, h in said.items():
        r = parse_sleep(t, NOW)
        check(f"other wording: {t!r}", r and r["hours"] == h, r)
    for t in ["I need 8 hours of sleep", "I usually sleep 7 hours", "going to bed at 11", "took a 2 hour nap",
              "set the bed heater to 3", "did I sleep 7 hours", "bedroom light to 30%", "ran 3 miles last night in 28 min",
              "watched TV for 2 hours last night", "watched TV 9-11 last night", "the party went from 8 to 12 last night",
              "charged the car for 3 hours last night"]:
        check(f"not a sleep log: {t!r}", parse_sleep(t, NOW) is None)
    check("night is the morning woken up on", parse_sleep("slept from 12 to 7", NOW)["night"].isoformat() == "2026-11-17")


def test_log():
    st = Store(tempfile.mktemp(), clock=lambda: NOW)
    r = log_sleep(st, "Slept from 12 to 7am last night", NOW)
    check("answer", r["answer"].startswith("Logged: 7 hours of sleep Monday night (12 AM–7 AM)"), r)
    row = st.db.execute("SELECT body, meta, shelf FROM items WHERE id=?", (r["id"],)).fetchone()
    m = json.loads(row["meta"])
    check("on body/sleep with hours in meta", row["shelf"] == "body/sleep" and m["sleep"] == 7.0, m)
    check("body keeps the (N hours) form the watch override reads", "(7 hours)" in row["body"], row["body"])
    from homemind.patterns import Patterns
    p = Patterns.__new__(Patterns)
    p.store = st
    check("patterns reads it for that day", p._answer(datetime.fromtimestamp(NOW).date(), "sleep") == 7.0)
    check("not sleep: None", log_sleep(st, "how did I sleep?", NOW) is None)


class HA:
    available = True

    def __init__(self, st):
        self.st = st

    async def state(self, eid):
        return self.st.get(eid)


def test_signals():
    from homemind.pantry import Pantry
    st = Store(tempfile.mktemp())
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    today = datetime.now(timezone.utc).isoformat()
    ha = HA({"input_number.steady_readiness": {"state": "0", "last_changed": today},
             "input_number.steady_sleep_hours": {"state": "0", "last_changed": today},
             "input_number.steady_fog_today": {"state": "4", "last_changed": yesterday}})
    pa = Pantry(st, ha, None, tempfile.mkdtemp())
    d = asyncio.run(pa.today())
    check("watch zeros aren't readings", d["signals"]["readiness"] is None and d["signals"]["sleep_hours"] is None, d)
    check("yesterday's fog rating doesn't count today", d["signals"]["fog_today"] is None and not d["tired"], d)
    log_sleep(st, "Slept from 12 to 7am last night", time.time())
    d = asyncio.run(pa.today())
    check("said sleep beats the watch", d["signals"]["sleep_hours"] == 7.0 and not d["tired"], d)
    ha.st["input_number.steady_fog_today"] = {"state": "4", "last_changed": today}
    d = asyncio.run(pa.today())
    check("today's fog rating still counts", d["tired"] and "brain fog" in d["tired_why"], d)


def test_ask():
    from homemind.mind import Mind
    st = Store(tempfile.mktemp())
    m = Mind.__new__(Mind)
    m.store, m.views, m.fitness = st, None, None
    m._conversations = lambda store: {}
    m._handoff = lambda *a: None
    called = []

    async def ctx(*a, **k):
        called.append(a)
        raise RuntimeError("model path reached")
    m.context_for = ctx

    class Pat:
        n = 0

        async def refresh(self, days=3):
            Pat.n += 1
    m.patterns = Pat()
    out = asyncio.run(m._ask("Slept from 12 to 7am last night"))
    check("Ask box logs sleep without a model", out.get("sleep") and not called and "7 hours" in out["answer"], out)
    check("the day's row is refreshed", Pat.n == 1)
    try:
        asyncio.run(m._ask("how long did I sleep last night?"))
    except RuntimeError:
        pass
    check("a question about sleep still goes to the model", len(called) == 1)


def test_rewrite():
    """Wording the parsers can't read goes to a model for a plain rewrite; numbers it adds are refused."""
    from homemind.mind import Mind
    from homemind.fitness import Fitness
    st = Store(tempfile.mktemp())
    m = Mind.__new__(Mind)
    m.store, m.views = st, None
    m.fitness = Fitness(st)
    m._conversations = lambda store: {}
    m._handoff = lambda *a: None
    m._chain = lambda model: [model]
    asked, reply = [], {"text": ""}

    class Model:
        async def chat(self, system, messages, **k):
            asked.append(messages[0]["content"])
            return dict(reply)

    class Models:
        models = {"m": Model()}

        def get(self, name):
            return self.models[name]
    m.models = Models()

    async def ctx(*a, **k):
        raise RuntimeError("model path reached")
    m.context_for = ctx

    def ask(q):
        try:
            return asyncio.run(m._ask(q, model="m"))
        except RuntimeError:
            return None
    out = ask("conked out half past twelve, alarm went off at 7")
    check("number words and 'half past' read without a model", out and out["sleep"]["hours"] == 6.5 and not asked, out)
    reply["text"] = "slept from 12:30 am to 7 am"
    out = ask("sleep: 12:30 → 7")
    check("odd sleep wording: model rewrite logged", out and out.get("sleep") and out["sleep"]["hours"] == 6.5
          and "read as" in out["answer"], out)
    reply["text"] = "squat 5x5 at 225 lb"
    out = ask("knocked out five sets of five on squats with 225")
    check("odd workout wording: model rewrite logged", out and out.get("workout") and
          out["workout"]["entries"][0]["weight_lb"] == 225, out)
    row = st.db.execute("SELECT body FROM items WHERE shelf='body/workouts' ORDER BY id DESC LIMIT 1").fetchone()
    check("the note keeps what was said", row is not None)
    reply["text"] = "squat 5x5 at 315 lb"
    n = len(asked)
    out = ask("squats were heavy today, did 5 sets")
    check("a rewrite with a number nobody said isn't logged", out is None and len(asked) == n + 1)
    reply["text"] = "NONE"
    check("NONE: answered as usual", ask("my bed is too soft, 2 pillows help") is None)
    n = len(asked)
    ask("how many hours did I sleep last night?")
    ask("turn off the bedroom light")
    check("questions and commands are never sent for a rewrite", len(asked) == n)
    reply["text"] = "slept from 12 am to 7 am"
    out = ask("ישנתי מ-12 עד 7 בבוקר")
    check("another language: rewritten and logged", out and out.get("sleep") and out["sleep"]["hours"] == 7.0, out)


for fn in [test_parse, test_log, test_signals, test_ask, test_rewrite]:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(f"{fn.__name__} raised {e!r}", False)
print(f"\n{len(FAILS)} failed" if FAILS else "\nall sleep checks passed")
sys.exit(1 if FAILS else 0)
