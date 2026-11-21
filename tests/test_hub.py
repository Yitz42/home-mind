"""The Home screen's data (/api/hub, 0.31): status tabs and their seven days, factors with notes, the For you queue,
place, the house tile, calendar, and the routes the page uses."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.store import Store  # noqa: E402
from homemind.checkins import Checkins  # noqa: E402
from homemind import hub  # noqa: E402

fails = 0


def check(name, ok, extra=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f"  {extra}" if extra and not ok else ""))
    fails += 0 if ok else 1


noon = datetime.now().replace(hour=13, minute=46, second=0, microsecond=0).timestamp()
d = tempfile.mkdtemp()
st = Store(os.path.join(d, "t.db"), clock=lambda: noon)
today = datetime.fromtimestamp(noon).date()


class Patterns:
    def rows(self, days):
        out = []
        for i in range(6, -1, -1):
            dd = (today - timedelta(days=i)).isoformat()
            out.append({"day": dd, "fog": 3 if i == 3 else 1, "sleep_h": 5.4 if i == 3 else 7.0, "readiness": 48 if i == 3
                        else 70, "wakeups": 4 if i == 3 else 1, "hrv": 41 if i == 3 else 55, "steps_prev": 8000,
                        "events": 2, "caffeine_late_prev": 0})
        return out


class Cal:
    def events_on(self, day):
        if day == today:
            s = datetime.combine(day, datetime.min.time()) + timedelta(hours=14)
            return [{"title": "Meeting block ends", "meta": {"start": s.isoformat()}}]
        return []


class Home:
    catalog = {"person.yitz": {"state": "not_home", "name": "Yitz"},
               "lock.back_door": {"state": "unlocked", "name": "Back door"},
               "lock.front_door": {"state": "locked", "name": "Front door"},
               "light.desk": {"state": "on", "name": "Desk"}}


class Mind:
    store = st


class A:
    mind = Mind()
    checkins = Checkins(st, clock=lambda: noon)
    patterns = Patterns()
    calendars = Cal()
    home = Home()
    ha = None


A.checkins.add(fog=3, energy="low", minutes_ago=330)
A.checkins.add(fog=2, energy="ok", tags=["Headache"])
A.checkins.log("water")
h = asyncio.run(hub.build(A, noon))
check("energy now is the latest check-in", h["status"]["energy"]["value"] == "OK" and h["status"]["energy"]["sub"] == "at 1:46 PM",
      h["status"]["energy"])
check("fog now is the latest check-in", h["status"]["fog"]["value"] == 2)
check("readiness from the patterns", h["status"]["readiness"]["value"] == 70)
check("seven days per tab", all(len(h["status"][k]["series"]) == 7 for k in ("energy", "fog", "readiness")))
check("fog series uses today's highest check-in", h["status"]["fog"]["series"][-1] == 3)
check("three days ago: readiness 48", h["status"]["readiness"]["series"][3] == 48)
f3 = {x["key"]: x for x in h["factors"]["readiness"][3]}
check("a short night is tagged and has a note", f3["sleep_h"]["tag"] == "short" and f3["sleep_h"]["kind"] == "low"
      and f3["sleep_h"]["note"], f3.get("sleep_h"))
check("low HRV against your own average", f3["hrv"]["tag"] == "low")
ft = {x["key"]: x for x in h["factors"]["fog"][-1]}
check("today adds live factors (water)", "water" in ft and ft["water"]["value"] == "1 glass")
check("logs: water 1, coffee 0", h["logs"]["water"] == 1 and h["logs"]["coffee"] == 0)
check("place from the person entity: away", h["place"] == "away")
check("house tile sees the unlocked back door", h["house"]["open"] == 1)
ids = [n["id"].split(":")[0] for n in h["needs"]]
check("away with a door open: that comes first", ids and ids[0] == "open", ids)
check("…and asks to lock it by words (so the phone approval runs)", h["needs"][0]["act"] == "do" and
      h["needs"][0]["text"] == "lock the Back door")
check("no check-in nudge right after a check-in", "checkin" not in ids)
check("calendar today", [e["title"] for e in h["calendar"]["today"]] == ["Meeting block ends"])
h2 = asyncio.run(hub.build(A, noon + 5 * 3600))
check("five hours later the check-in is due", any(n["act"] == "checkin" for n in h2["needs"]))
A.home.catalog["person.yitz"]["state"] = "home"
h3 = asyncio.run(hub.build(A, noon))
check("at home the open door isn't nagged about", not any(n["id"].startswith("open:") for n in h3["needs"]))

src = open(os.path.join(os.path.dirname(hub.__file__), "main.py")).read()
for route in ('"/api/hub"', '"/api/checkins"', '"/api/log"', '"/api/todo"'):
    check(f"route {route}", route in src)
web = open(os.path.join(os.path.dirname(hub.__file__), "web", "index.html")).read()
check("the page has no Talk tab any more", 'data-t="talk"' not in web)
check("the page loads the hub", "api/hub" in web and "api/checkins" in web)

n1 = hub.needs(A, {"review": 1}, None, noon, "home", {"open": 0})
check("1 note to check is singular", any(n["title"] == "1 note to check" for n in n1), n1)
n2 = hub.needs(A, {"review": 2}, None, noon, "home", {"open": 0})
check("2 notes to check is plural", any(n["title"] == "2 notes to check" for n in n2), n2)

print(f"\n{'all passed' if not fails else str(fails) + ' failed'}")
sys.exit(1 if fails else 0)
