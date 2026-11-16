"""Expert review findings (long-running growth): what grows without bound over months on a small box.
No network needed.

  python tests/test_review_growth.py
"""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def fts_ok(s):
    try:
        s.db.execute("INSERT INTO items_fts(items_fts, rank) VALUES('integrity-check', 1)")
        return True
    except sqlite3.DatabaseError:
        return False


T = [datetime(2026, 1, 1).timestamp()]
s = Store(os.path.join(tempfile.mkdtemp(), "m.db"), clock=lambda: T[0])


def reading(eid, name, v, unit, when, **extra):
    """As home.log_changes files a reading."""
    meta = {"series": "home:" + eid, "entity_id": eid, "state": str(v), **extra}
    return s.remember(text=f"{datetime.fromtimestamp(when):%a %b %d %Y %I:%M %p}: {name}: {v} {unit}.",
                      title=f"{name}: {v} {unit}", kind="fact", shelf="home/readings", tags=["home", "sensor"],
                      source="home", meta=meta, at=when)["id"]


day0 = datetime(2026, 1, 1)
# 120 days of a temperature reading every half hour (48 a day) and a lamp switched twice a day
for d in range(120):
    for h in range(48):
        t = (day0 + timedelta(days=d, minutes=30 * h)).timestamp()
        reading("sensor.bed_temp", "Bedroom temperature", round(18 + (h % 12) * 0.5, 1), "°C", t)
    for h in (7, 22):
        t = (day0 + timedelta(days=d, hours=h)).timestamp()
        s.remember(text=f"Desk lamp: {'on' if h == 7 else 'off'}.", title=f"Desk lamp: {'on' if h == 7 else 'off'}",
                   kind="fact", shelf="home/devices", tags=["home", "light"], source="home", at=t,
                   meta={"series": "home:light.desk", "entity_id": "light.desk", "state": "on" if h == 7 else "off"})
odd = reading("sensor.bed_temp", "Bedroom temperature", 35, "°C", (day0 + timedelta(days=3, hours=12, minutes=1)).timestamp(),
              unusual="Bedroom temperature 35 °C is unusually high")
# day 5 already has a daily note from the history backfill
d5 = day0 + timedelta(days=5)
s.remember(text="Tuesday Jan 6 2026: Bedroom temperature averaged 20 °C.", title="Bedroom temperature on Tue Jan 06: avg 20 °C",
           kind="fact", shelf="home/daily", tags=["home", "daily"], source="home", at=d5.timestamp() + 86399,
           meta={"series": "home:sensor.bed_temp", "entity_id": "sensor.bed_temp", "daily": True, "backfill": True})
T[0] = (day0 + timedelta(days=120, hours=3)).timestamp()
q = lambda sql, *a: s.db.execute(sql, a).fetchone()[0]
before = q("SELECT COUNT(*) FROM items WHERE shelf='home/readings'")
n = s.roll_up_house_log(90)
cut = (day0 + timedelta(days=30)).timestamp()
check("old raw readings and device changes are rolled up and deleted (#growth: kept forever, ~100+ KiB a day)",
      q("SELECT COUNT(*) FROM items WHERE shelf IN ('home/readings','home/devices') AND created < ?", cut) == 1
      and n == 30 * 50, n)
check("readings and device changes inside the window stay",
      q("SELECT COUNT(*) FROM items WHERE shelf='home/readings'") == before - 30 * 48 and
      q("SELECT COUNT(*) FROM items WHERE shelf='home/devices'") == 180)
check("an unusual reading is kept", s.get(odd) is not None)
dailies = s.db.execute("SELECT created, body, meta FROM items WHERE shelf='home/daily' ORDER BY created").fetchall()
check("one daily note per thing per rolled day (the backfilled day not doubled)", len(dailies) == 60, len(dailies))
lamp = [r["body"] for r in dailies if "Desk lamp" in r["body"]]
check("a device's daily note keeps its changes with their times",
      lamp and lamp[0] == "Thursday Jan 1 2026: Desk lamp changed 2 times: on at 7:00 AM, off at 10:00 PM.", lamp[:1])
d1 = next((r for r in dailies if datetime.fromtimestamp(r["created"]).date() == (day0 + timedelta(days=1)).date()), None)
check("the daily note says average, low and high in the backfill's words",
      d1 is not None and "Bedroom temperature averaged 20.8 °C (low 18, high 23.5)." in d1["body"], d1 and d1["body"])
check("daily notes are skipped by the habit learner (meta daily)", d1 is not None and json.loads(d1["meta"]).get("daily"))
check("the search index stays consistent", fts_ok(s))
check("a rolled-up day is in the search index",
      q("SELECT COUNT(*) FROM items_fts WHERE items_fts MATCH 'averaged AND bedroom'") == 30)
check("running again changes nothing", s.roll_up_house_log(90) == 0)
check("0 keeps everything", s.roll_up_house_log(0) == 0)
recent = lambda: q("SELECT COUNT(*) FROM items WHERE shelf IN ('home/readings','home/devices') AND created >= ?",
                   T[0] - 29 * 86400)
r0 = recent()
s.roll_up_house_log(1)
check("never shorter than the house learner's 28 days", r0 > 0 and recent() == r0, (r0, recent()))

# steady state: another 60 days filed and rolled nightly: the reading notes stay about 90 days' worth
for d in range(120, 180):
    for h in range(48):
        t = (day0 + timedelta(days=d, minutes=30 * h)).timestamp()
        reading("sensor.bed_temp", "Bedroom temperature", 20.0, "°C", t)
    T[0] = (day0 + timedelta(days=d + 1, hours=3)).timestamp()
    s.roll_up_house_log(90)
r = q("SELECT COUNT(*) FROM items WHERE shelf='home/readings'")
check("reading notes stop growing (about keep_days x per-day)", r <= 91 * 48 + 1, r)
# the search index sheds the deleted notes too (delete markers merged away), not only the rows
fts_kb = lambda: q("SELECT SUM(LENGTH(block)) FROM items_fts_data") / 1024
k0 = fts_kb()
for d in range(180, 210):
    for h in range(48):
        reading("sensor.bed_temp", "Bedroom temperature", 20.0 + h / 10, "°C", (day0 + timedelta(days=d, minutes=30 * h)).timestamp())
    T[0] = (day0 + timedelta(days=d + 1, hours=3)).timestamp()
    s.roll_up_house_log(90)
check("the search index does not keep growing in steady state", fts_kb() <= k0 * 1.1, (round(k0), round(fts_kb())))

# preference observations: only the last prefs.DAYS are read; older ones were kept for ever
from homemind import prefs as P  # noqa: E402
pf = P.Prefs(s)
s.db.execute("INSERT INTO pref_obs VALUES('light.desk', ?, 50, 'evening')", (T[0] - (P.DAYS + 5) * 86400,))
s.db.execute("INSERT INTO pref_obs VALUES('light.desk', ?, 50, 'evening')", (T[0] - 86400,))
s.db.commit()
pf.file_notes({}, now=T[0])
check("old preference observations are pruned, recent kept",
      [r[0] for r in s.db.execute("SELECT ts FROM pref_obs")] == [T[0] - 86400])

# the upkeep loop runs it with events_keep_days, daily, beside prune_events
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app", "homemind",
                        "main.py")).read()
check("the upkeep loop rolls up readings with events_keep_days",
      'roll_up_house_log(int(self.opts.get("events_keep_days")' in src)

print(f"\n{len(FAILS)} failures" if FAILS else "\nall ok")
sys.exit(1 if FAILS else 0)
