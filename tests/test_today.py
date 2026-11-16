"""The Today screen's data (#60): /api/today gathers sleep, fog, the week, food to eat first, protein so far, the
bedroom, what was remembered today and the house, and copes with Home Assistant being away. No Home Assistant needed."""
import asyncio
import os
import sys
import tempfile
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.store import Store  # noqa: E402
from homemind.patterns import Patterns, E  # noqa: E402
from homemind.pantry import Pantry  # noqa: E402
from homemind import today  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeHA:
    available = True

    def __init__(self):
        self.st = {E["weather"]: {"state": "clear-night", "attributes": {"temperature": 45}},
                   E["room_temp"]: {"state": "68.4"}, E["room_hum"]: {"state": "49"}, E["aqi_in"]: {"state": "9"},
                   E["pm"]: {"state": "unknown"}, "input_number.steady_readiness": {"state": "81"},
                   "input_number.steady_sleep_hours": {"state": "6.4"}}

    async def state(self, eid):
        return self.st.get(eid)


class Mind:
    def __init__(self, store):
        self.store = store


class App:
    pass


def setup(with_ha=True):
    st = Store(tempfile.mktemp())
    ha = FakeHA() if with_ha else None
    A = App()
    A.mind, A.ha, A.comfort = Mind(st), ha, None
    A.pantry = Pantry(st, ha, None, tempfile.mkdtemp())
    A.patterns = Patterns(st, ha)
    return A, st


async def main():
    A, st = setup()
    d0 = date.today()
    for i, (fog, sleep) in enumerate([(None, 7.2), (0, 7.5), (None, 2.8), (3, 7.7), (None, None), (None, 6.0), (None, 6.4),
                                      (2, 7.0), (None, 7.1)]):
        day = d0 - timedelta(days=8 - i)
        A.patterns.save({"day": day.isoformat(), "fog": fog, "sleep_h": sleep, "readiness": 80, "bedtime": 22.5})
    A.pantry.add("2% milk", "half gallon", "fridge", use_by=(d0 + timedelta(days=2)).isoformat())
    A.pantry.add("mango", "1", "pantry", use_by=(d0 + timedelta(days=4)).isoformat())
    A.pantry.add("oatmeal", "1 box", "pantry", use_by=(d0 - timedelta(days=400)).isoformat())
    A.pantry.add("Stouffer's lasagna", "1", "freezer", use_by=(d0 + timedelta(days=120)).isoformat())
    st.remember(text="Ate: Healthy Choice pineapple chicken (16 g protein, 290 cal)",
                title="Ate: Healthy Choice pineapple chicken (16 g protein, 290 cal)", shelf="body/meals", source="chat")
    st.remember(text="The spare key is in the blue bowl.", source="api")
    st.remember(text="Morning summary", title="Morning summary", shelf="summaries/morning", source="morning")
    st.kv_set("house_model", {"habits": [{"text": "Lights off around 10:16 PM", "minute": 1336, "state": "off",
                                          "name": "Desk Light"}], "baselines": [{}, {}], "suggestions": []})
    t = await today.build(A)
    check("week: the last 7 days, oldest first", len(t["week"]) == 7 and t["week"][-1]["day"] == d0.isoformat(),
          [w["day"] for w in t["week"]])
    check("fog days counted over the whole record", t["fog_days"] == 3, t["fog_days"])
    check("readiness and sleep from the watch", t["day"]["signals"]["readiness"] == 81.0 and
          t["day"]["signals"]["sleep_hours"] == 6.4, t["day"])
    check("food to eat first: within a week, soonest first, frozen food left out",
          [s["name"] for s in t["soon"]] == ["2% milk", "mango"], t["soon"])
    check("past its date listed apart", [e["name"] for e in t["expired"]] == ["oatmeal"], t["expired"])
    check("where things are counted", t["where"] == {"fridge": 1, "pantry": 2, "freezer": 1}, t["where"])
    check("protein read from the meal note", t["protein_g"] == 16 and t["meals"][0]["title"].startswith("Healthy Choice"),
          (t["protein_g"], t["meals"]))
    check("bedroom readings, an unknown one left empty", t["room"]["temp"] == 68.4 and t["room"]["pm"] is None and
          t["room"]["filter"] is None, t["room"])
    check("outside from the weather entity", t["outside"] == {"state": "clear night", "temp": 45}, t["outside"])
    titles = [r["title"] for r in t["remembered"]]
    check("remembered today: your notes and meals, not summaries", "The spare key is in the blue bowl." in " ".join(titles)
          and any("pineapple" in x for x in titles) and not any("Morning summary" in x for x in titles), titles)
    check("house in a line", t["house"]["habits"] == 1 and t["house"]["baselines"] == 2 and t["house"]["notable"] == 0,
          t["house"])
    check("version filled in", bool(t["version"]))

    A2, _ = setup(with_ha=False)
    t2 = await today.build(A2)
    check("no Home Assistant: still answers, sensors empty", t2["room"] == {} and t2["outside"] is None and
          t2["day"] is None and t2["week"] == [], t2)


asyncio.run(main())
print(f"\n{len(FAILS)} failed" if FAILS else "\nALL TODAY CHECKS PASSED")
sys.exit(1 if FAILS else 0)
