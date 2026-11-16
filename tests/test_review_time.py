"""Expert review (round 4, time): each check reproduces a date/time bug found in review and fixed. Runs in a fixed time
zone with DST (America/New_York) and a controlled clock. No network needed.

  python tests/test_review_time.py
"""
import asyncio
import os
import sys
import time

os.environ["TZ"] = "America/New_York"
time.tzset()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from datetime import date, datetime  # noqa: E402

from homemind import calendars, journal, overnight, patterns, structure as S  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def ts(*a):
    return datetime(*a).timestamp()


# ---- "every month" / "every year" are calendar steps, not 30 / 365 days -------------------------------------------
rec = S.recurrence("We pay the rent every month", ts(2026, 1, 1, 9))
nxt = S.next_time(rec, ts(2026, 11, 16, 10))
check("recur: every month filed Jan 1 is next on Dec 1 (not Nov 27)", nxt == date(2026, 12, 1), nxt)
rec = S.recurrence("The block party is every year", ts(2024, 7, 4, 9))
nxt = S.next_time(rec, ts(2028, 7, 1))
check("recur: every year stays on Jul 4 across leap days", nxt == date(2028, 7, 4), nxt)
rec = S.recurrence("Pay the card every month", ts(2026, 1, 31, 9))
check("recur: Jan 31 every month falls on Feb 28", S.next_time(rec, ts(2026, 2, 10)) == date(2026, 2, 28))
check("recur: ...and back to Mar 31", S.next_time(rec, ts(2026, 3, 5)) == date(2026, 3, 31))
legacy = {"kind": "interval", "days": 30, "anchor": S._day(ts(2026, 1, 1, 9)).timestamp()}
check("recur: notes filed before keep working (every month)", S.next_time(legacy, ts(2026, 11, 16)) == date(2026, 12, 1))
rec = S.recurrence("Water the fern every 10 days", ts(2026, 3, 1, 9))
check("recur: day intervals unchanged (across DST)", S.next_time(rec, ts(2026, 3, 9, 8)) == date(2026, 3, 11))

# ---- dates written day-first, and leap days ------------------------------------------------------------------------
check("dates: 13/3/2027 is day-first (was dropped)", S.parse_date("13/3/2027") == date(2027, 3, 13))
check("dates: 3/4/2027 stays month-first", S.parse_date("3/4/2027") == date(2027, 3, 4))
check("dates: a Feb 29 birthday comes on Feb 28 in a common year",
      S.parse_date("Feb 29", ts(2027, 1, 5), future=True) == date(2027, 2, 28))
check("dates: ...and on Feb 29 in a leap year", S.parse_date("Feb 29", ts(2027, 12, 5), future=True) == date(2028, 2, 29))

# ---- calendar questions around new year ----------------------------------------------------------------------------
check("calendar: 'Dec 31' asked on Jan 2 is the one just past",
      calendars.asked_days("what was on Dec 31", date(2027, 1, 2)) == [date(2026, 12, 31)])
check("calendar: 'Jan 5' asked in December is next month",
      calendars.asked_days("what's on Jan 5", date(2026, 12, 20)) == [date(2027, 1, 5)])

# ---- days are midnight to midnight, 23 or 25 hours on DST days -----------------------------------------------------
t = [ts(2026, 11, 1, 23, 20)]           # Nov 1 2026: clocks go back, the day has 25 hours
st = Store(":memory:", clock=lambda: t[0])
st.remember("Late thought before bed", shelf="journal", at=ts(2026, 11, 1, 23, 20))
st.remember("Morning after", shelf="journal", at=ts(2026, 11, 2, 0, 30))
j = journal.Journal(st, None)
got = [e["body"] for e in j.entries(date(2026, 11, 1))]
check("journal: an entry at 23:20 on the 25-hour day is that day's", got == ["Late thought before bed"], got)
st2 = Store(":memory:")
st2.remember("Spring entry 23:30", shelf="journal", at=ts(2026, 3, 8, 23, 30))
st2.remember("Next day 00:30", shelf="journal", at=ts(2026, 3, 9, 0, 30))
got = [e["body"] for e in journal.Journal(st2, None).entries(date(2026, 3, 8))]
check("journal: the 23-hour day doesn't take the next day's first hour", got == ["Spring entry 23:30"], got)
lo, hi = patterns._day_span(date(2026, 11, 1))
check("patterns: a day's span follows DST", hi - lo == 25 * 3600 and patterns._day_span(date(2026, 3, 8))[1]
      - patterns._day_span(date(2026, 3, 8))[0] == 23 * 3600)

# ---- overnight learning set late: the window runs past midnight, once a night ---------------------------------------
class FakeNow(datetime):
    at = None

    @classmethod
    def now(cls, tz=None):
        return cls.at


ran = []


async def nights():
    st3 = Store(":memory:")
    o = overnight.Overnight(st3, None, None, at="23:00")

    async def run():
        ran.append(FakeNow.at)
        return {}
    o.run = run
    real = overnight.datetime
    overnight.datetime = FakeNow
    try:
        for when in (datetime(2026, 11, 16, 22, 59), datetime(2026, 11, 16, 23, 5), datetime(2026, 11, 17, 0, 30),
                     datetime(2026, 11, 17, 2, 30), datetime(2026, 11, 17, 23, 1)):
            FakeNow.at = when
            await o.tick()
        o.done_for = None                       # a restart at 00:40 after tonight's run
        FakeNow.at = datetime(2026, 11, 18, 0, 40)
        await o.tick()
        o2 = overnight.Overnight(st3, None, None, at="23:00")
        o2.run = run
        FakeNow.at = datetime(2026, 11, 19, 0, 15)      # down at 23:00, up at 00:15: tonight still runs
        await o2.tick()
    finally:
        overnight.datetime = real


asyncio.run(nights())
check("overnight: 23:00 runs once at 23:05, not again after midnight, again next night, after a missed 23:xx",
      ran == [datetime(2026, 11, 16, 23, 5), datetime(2026, 11, 17, 23, 1), datetime(2026, 11, 19, 0, 15)], ran)

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall ok")
