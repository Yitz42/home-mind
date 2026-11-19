"""Phase 12: things (an item type, typed links, from devices or a yes, look-alikes proposed for merging, renames kept,
links filtered by viewer, "where's the drill?" followed through links) and scenes drafted from a description (never
heaters or locks, a preview that undoes itself, kept scenes scored).

  python tests/test_things.py
"""
import asyncio
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind import things as T  # noqa: E402
from homemind.home import Home  # noqa: E402
from homemind.speakers import VIEWER  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


st = Store(os.path.join(tempfile.mkdtemp(), "mind.db"))
th = T.Things(st)

# ---- from devices; renames ---------------------------------------------------------------------------------------
cat = {"light.desk": {"name": "Desk lamp", "state": "off", "attrs": {}},
       "switch.dishwasher": {"name": "Dishwasher", "state": "off", "attrs": {}},
       "sensor.temp": {"name": "Temperature", "state": "20", "attrs": {}}}
check("devices: a thing for each device, none for sensors", th.from_devices(cat) == (2, 0) and
      sorted(t["name"] for t in th.all()) == ["Desk lamp", "Dishwasher"])
cat["light.desk"]["name"] = "Office lamp"
check("devices: a renamed device keeps its thing, the old name as another name", th.from_devices(cat) == (0, 1) and
      th.find("the desk lamp")["name"] == "Office lamp")

# ---- noticed in notes, a yes, links -------------------------------------------------------------------------------
st.remember("The drill is in the red toolbox.", title="Drill")
st.remember("The red toolbox is in the shed.", title="Toolbox")
st.remember("Drill bits are in the kitchen drawer", title="Bits")
noticed = th.noticed()
check("notes: things named in notes are noticed, not created", "drill" in noticed and "red toolbox" in noticed and
      not th.find("drill"), noticed)
for n in ("drill", "red toolbox", "shed"):
    th.create(n)
th.dismiss("kitchen drawer")
check("notes: a dismissed name isn't noticed again", "kitchen drawer" not in th.noticed())
n = th.link_notes()
d, tb = th.find("the drill"), th.find("red toolbox")
check("links: 'X is in Y' between two things", any(x["relation"] == "in" and x["id"] == tb["id"]
                                                   for x in th.links(d["id"])), th.links(d["id"]))
check("links: notes about a thing", any(x["relation"] == "about" for x in th.links(d["id"])))
check("where: followed through the links", th.answer_where("where's the drill?") == "In the red toolbox, in the shed.",
      th.answer_where("where's the drill?"))
check("where: not a thing: left for the notes", th.answer_where("where's the passport?") is None)
th.link(d["id"], th.find("shed")["id"], "in")
check("links: a thing is in one place at a time", th.where(d["id"]) == ["shed"], th.where(d["id"]))

# privacy through links
sec = st.remember("Dana's drill battery is charging in her room.", title="Dana drill",
                  meta={"scope": "personal", "owner": "Dana"})["id"]
th.link(sec, d["id"], "about")
VIEWER.set("Sam")
check("links: a link never shows a note the viewer may not see",
      all(x["id"] != sec for x in th.links(d["id"], viewer="Sam")))
VIEWER.set(None)
check("links: its owner sees it", any(x["id"] == sec for x in th.links(d["id"], viewer="Dana")))

# look-alikes
lamp = th.create("lamp")
dup = th.duplicates()
check("duplicates: look-alike names proposed, never merged silently",
      any(set(p["names"]) == {"lamp", "Office lamp"} for p in dup) and th.get(lamp), dup)
p = next(p for p in dup if set(p["names"]) == {"lamp", "Office lamp"})
k = th.merge(p["keep"], p["merge"])
check("merge: on a yes, names and links move to the kept one", k["name"] == "Office lamp" and "lamp" in k["aliases"]
      and th.get(lamp) is None or st.get(lamp)["status"] == "archived", k)
try:
    th.link(1, 2, "owns")
    check("links: only the four relations", False)
except ValueError:
    check("links: only the four relations", True)

# ---- scenes -------------------------------------------------------------------------------------------------------


class FakeHA:
    def __init__(self):
        self.calls = []

    async def state(self, eid):
        return {"state": "on"} if eid == "input_boolean.mind_control_enabled" else None

    async def call(self, dom, svc, data):
        self.calls.append((dom, svc, data))
        return 200


h = Home(Store(os.path.join(tempfile.mkdtemp(), "m.db")), FakeHA(), {"home_controls": ["light.*", "fan.*", "lock.*",
                                                                                        "climate.*", "switch.*"]})
h.catalog = {"light.sofa": {"name": "sofa lamp", "state": "on", "attrs": {"brightness": 255}, "domain": "light"},
             "light.ceiling": {"name": "ceiling light", "state": "off", "attrs": {}, "domain": "light"},
             "fan.living": {"name": "living fan", "state": "off", "attrs": {}, "domain": "fan"},
             "lock.front": {"name": "front door", "state": "locked", "attrs": {}, "domain": "lock"},
             "climate.living": {"name": "living heat", "state": "heat", "attrs": {}, "domain": "climate"},
             "switch.space_heater": {"name": "space heater", "state": "off", "attrs": {}, "domain": "switch"},
             "light.bedroom": {"name": "bed lamp", "state": "off", "attrs": {}, "domain": "light"}}
h.areas = {"light.sofa": "living", "light.ceiling": "living", "fan.living": "living", "lock.front": "living",
           "climate.living": "living", "switch.space_heater": "living", "light.bedroom": "bedroom"}
h.access.baseline(list(h.catalog))
ran = []


async def run_plan(plan, text, source, cid, t0=None):
    ran.append((plan["entity_id"], plan["action"], plan.get("value")))
    return {"done": {"entity_id": plan["entity_id"]}}
clk = Clock(1000.0)
app = types.SimpleNamespace(home=h, mind=types.SimpleNamespace(store=st), _run_plan=run_plan)
sc = T.Scenes(app, clock=clk)
s = run(sc.draft("make the living room cozy", "living"))
eids = {x["entity_id"] for x in s["steps"]}
check("scene: cozy: the room's lights at 30%", s["steps"] and all(x["value"] == 30 for x in s["steps"]
                                                                   if x["entity_id"].startswith("light.")) and
      eids <= {"light.sofa", "light.ceiling"}, s)
check("scene: never locks, heating or heaters, and only that room", not eids & {"lock.front", "climate.living",
                                                                                "switch.space_heater", "light.bedroom"})


async def model_draft(text, devs):
    return [{"entity_id": "lock.front", "action": "off"}, {"entity_id": "light.sofa", "action": "brightness",
                                                            "value": 55}, {"entity_id": "light.kitchen", "action": "on"}]
s2 = run(sc.draft("party time", "living", model_draft))
check("scene: a model's draft where words don't fit, checked (no lock, nothing outside the room)",
      s2["steps"] == [{"entity_id": "light.sofa", "action": "brightness", "value": 55}], s2)
pv = run(sc.preview(s))
check("scene: the first run is a preview through the usual checks", len(ran) == len(s["steps"]) and
      pv["preview_until"] == 1600)
clk.t += 601
undone = run(sc.undo_due())
check("scene: not kept: put back as it was after 10 minutes", undone == [s["name"]] and
      ("light", "turn_off", {"entity_id": "light.ceiling"}) in h.ha.calls, h.ha.calls)
run(sc.preview(s))
kept = sc.keep(s["name"])
check("scene: kept: saved, and the undo is off", s["name"] in sc.saved() and run(sc.undo_due()) == [])
sc.keep(s["name"], [{"entity_id": "light.sofa", "action": "brightness", "value": 20},
                    {"entity_id": "lock.front", "action": "off"}])
check("scene: an edit is counted, and checked again (no lock sneaks in)",
      sc.saved()[s["name"]]["edits"] == 1 and all(x["entity_id"] != "lock.front" for x in sc.saved()[s["name"]]["steps"]))
ran.clear()
run(sc.run(s["name"]))
check("scene: a kept scene runs by name", ran == [("light.sofa", "brightness", 20)])
clk.t += 8 * 86400
check("scene: scored: still kept after a week", sc.scores()[s["name"]]["kept_a_week"])

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
