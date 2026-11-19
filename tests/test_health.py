"""Health questions answered from the watch's readings, not from whatever notes the search finds (0.29.2).

  python tests/test_health.py
"""
import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind import health as H  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


today = date.today()
ROWS = [{"day": (today - timedelta(days=i)).isoformat(), "readiness": 68, "sleep_h": 7.2, "hrv": 46, "resting_hr": 57,
         "night_low_hr": 51, "fog": 2} for i in range(20, 0, -1)]
ROWS.append({"day": today.isoformat(), "readiness": 72, "sleep_h": 6.0, "hrv": 41, "resting_hr": 58,
             "night_low_hr": 52, "fog": 1})


class P:
    def __init__(self, rows):
        self.rows_ = rows

    def rows(self, n):
        return self.rows_


class D:
    def get(self, p):
        return "low"


class HA:
    available = True

    def __init__(self, st):
        self.st = st

    async def states(self):
        return self.st


def ask(h, q):
    r = asyncio.run(h.answer(q))
    return (r or {}).get("answer")


h = H.Health(P(ROWS), D(), HA([]))
a = ask(h, "How my health looking today?")
check("summary: the readings, each against usual", a and a.startswith("Today: readiness 72") and
      "HRV 41 ms (lower than usual, 46 ms)" in a and "resting heart rate 58 bpm (about usual)" in a, a)
check("summary: slept, in hours and minutes", "slept 6 h (lower than usual, 7 h 12 min)" in a, a)
check("summary: the check-in and fog", "Your check-in: low." in a and "Fog: 1." in a, a)
check("summary: no judgement or advice", not any(w in a.lower() for w in ("good", "bad", "should", "try")), a)
check("how am I doing", ask(h, "how am I doing?").startswith("Today:"))
a = ask(h, "What is my current heart rate?")
check("heart rate: no live sensor: says so, then this morning's readings", a.startswith("I don't get a live heart rate")
      and "resting heart rate 58 bpm" in a and "overnight low heart rate 52 bpm" in a, a)
now = datetime.now(timezone.utc)
live = [{"entity_id": "sensor.watch_heart_rate", "state": "74", "last_updated": now.isoformat(),
         "attributes": {"unit_of_measurement": "bpm", "friendly_name": "Watch heart rate"}},
        {"entity_id": "sensor.watch_resting_heart_rate", "state": "58", "last_updated": now.isoformat(),
         "attributes": {"unit_of_measurement": "bpm", "friendly_name": "Watch resting heart rate"}}]
check("heart rate: a live sensor updated recently", ask(H.Health(P(ROWS), D(), HA(live)), "what's my heart rate?")
      == "Your heart rate is 74 bpm (Watch heart rate, just now).")
old = [dict(live[0], last_updated=(now - timedelta(hours=3)).isoformat())]
check("heart rate: a stale one isn't 'current'", ask(H.Health(P(ROWS), D(), HA(old)), "what's my heart rate?")
      .startswith("I don't get a live heart rate"))
check("HRV today", ask(h, "what's my hrv today?") == "Today: HRV 41 ms (lower than usual, 46 ms).")
check("sleep last night", ask(h, "how did I sleep last night?").startswith("Last night: slept 6 h"))
for q in ("what was my average sleep this week?", "why is my heart rate high?", "how can I improve my HRV?",
          "turn on the light", "what's my readiness?", "set a reminder to check my heart rate"):
    check(f"not for this module: {q!r}", H.which(q) is None, H.which(q))
y = [r for r in ROWS if r["day"] != today.isoformat()]
check("no reading yet today: says which day it's from",
      ask(H.Health(P(y), D(), HA([])), "how's my health today?").startswith("Yesterday:"))
check("no readings at all: says so", "don't have any readings" in ask(H.Health(P([]), D(), HA([])), "how's my health?"))
check("too few days for a usual: just the number", ask(H.Health(P(ROWS[-3:]), D(), HA([])), "what's my hrv today?")
      == "Today: HRV 41 ms.")

# wired into Mind.ask, ahead of the notes: no model, and personal
import tempfile  # noqa: E402
from homemind import mind as MI  # noqa: E402
from homemind.speakers import VIEWER  # noqa: E402
m = MI.Mind(tempfile.mkdtemp(), None)
m.health = h
r = asyncio.run(m.ask("How my health looking today?"))
check("ask: answered from the readings, no model, marked personal", r["answer"].startswith("Today: readiness 72") and
      r["model"] is None and r.get("personal"), r)
VIEWER.set("")
r = asyncio.run(m.ask("How my health looking today?"))
check("ask: an unknown voice gets no numbers", "private" in r["answer"] and "72" not in r["answer"], r)
VIEWER.set(None)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
