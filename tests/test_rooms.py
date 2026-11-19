"""Phase 11: device access by room and group (deny wins, personal spaces), the room model (memory only, nobody can
ask where someone is), backing off after a person's change, the surprises queue, and settling conflicts.

  python tests/test_rooms.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind import rooms as R  # noqa: E402
from homemind.cases import Cases  # noqa: E402
from homemind.decisions import DecisionLog  # noqa: E402
from homemind.home import Home  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def store():
    return Store(os.path.join(tempfile.mkdtemp(), "mind.db"))


# ---- device access by room and group ------------------------------------------------------------------------------
h = Home(store(), None, {"home_controls": ["light.*", "camera.*"]})
h.catalog = {"light.desk": {"name": "desk lamp", "attrs": {}}, "light.bed": {"name": "bed lamp", "attrs": {}},
             "light.upstairs": {"name": "upstairs lights", "attrs": {"entity_id": ["light.bed", "light.hall"]}},
             "light.hall": {"name": "hall light", "attrs": {}}}
h.areas = {"light.desk": "office", "light.bed": "bedroom", "light.hall": "hall"}
h.access.baseline(list(h.catalog))
check("access: by default, as before", h.access_of("light.desk") == "act")
h.access.set("room:bedroom", "none")
check("access: a device inherits its room's setting", h.access_of("light.bed") == "none")
check("access: other rooms are unaffected", h.access_of("light.desk") == "act")
h.access.set("light.bed", "act")
check("access: a deny on the room beats an allow on the device", h.access_of("light.bed") == "none")
h.access.set("group:light.upstairs", "read")
check("access: a group's setting reaches its members", h.access_of("light.hall") == "read" and
      h.access_groups("light.hall") == ["room:hall", "group:light.upstairs"], h.access_groups("light.hall"))
h.access.set_space("office", "Dana")
check("spaces: anyone can make it stricter (a veto)", h.access.set("room:office", "read", by="Sam")["to"] == "read")
try:
    h.access.set("room:office", "act", by="Sam")
    check("spaces: only the person whose space it is can open it up", False)
except PermissionError as e:
    check("spaces: only the person whose space it is can open it up", "Dana" in str(e))
check("spaces: its person can open it up (in 24 hours, as any loosening)",
      h.access.set("room:office", "act", by="Dana")["when"] == "in 24 hours")

# ---- the room model -----------------------------------------------------------------------------------------------
clk = Clock(5000.0)
h.catalog.update({"person.dana": {"state": "home", "attrs": {}}, "person.sam": {"state": "home", "attrs": {}},
                  "binary_sensor.office_motion": {"state": "on", "attrs": {"device_class": "motion"},
                                                  "last_changed_ts": 4900.0}})
h.areas["binary_sensor.office_motion"] = "office"
rm = R.RoomModel(h, clock=clk)
check("rooms: movement in a room", rm.occupied() == {"office"})
check("rooms: nobody placed yet: not alone with two people home", not rm.alone("Dana"))
rm.heard_in("Office", "Dana")
check("rooms: heard in the office: on her own there", rm.where("Dana") == "office" and rm.alone("Dana"))
rm.heard_in("office", "Sam")
check("rooms: someone else heard there too: not alone", not rm.alone("Dana"))
clk.t += 700
check("rooms: no history: after 10 minutes nobody is anywhere", rm.where("Dana") is None and rm.in_room("office") == [])
names = ["Dana", "Sam"]
for q in ("where's Dana?", "where is sam right now", "is Dana home?", "when did Sam leave?", "where is my wife"):
    check(f"where: refused {q!r}", R.where_question(q, names))
for q in ("where's the drill?", "where is the dentist's number", "is the garage closed?"):
    check(f"where: things are fine {q!r}", not R.where_question(q, names))

# ---- backing off --------------------------------------------------------------------------------------------------
st = store()
bc = Clock(time.time())
bo = R.BackOff(st, clock=bc)
lc = Clock(time.time())
dl = DecisionLog(":memory:", clock=lc, quiet_seconds=0)
dl.on_override = lambda eid: bo.mark(eid, "a person undid it")
dl.record("light.desk", "on", None, "light.turn_on", {"entity_id": "light.desk"}, context_id="c1",
          request="desk lamp on", prior="off")
lc.t += 2
dl.on_state("light.desk", {"state": "on", "attributes": {}, "context": {"id": "c1"}}, {"state": "off", "attributes": {}})
check("back off: nothing yet while what Home Mind did stands", not bo.active("light.desk"))
lc.t += 30
dl.on_state("light.desk", {"state": "off", "attributes": {}, "context": {"id": "z", "user_id": "u1"}},
            {"state": "on", "attributes": {}})
check("back off: a person's change keeps Home Mind's automations off it", bo.active("light.desk"))
bc.t += 3 * 3600 + 1
check("back off: for 3 hours", not bo.active("light.desk"))

# ---- surprises ----------------------------------------------------------------------------------------------------
st = store()
sp = R.Surprises(st)
dl = DecisionLog(":memory:", quiet_seconds=0)
d1 = dl.record("light.desk", "on", None, "light.turn_on", {"entity_id": "light.desk"}, context_id="c2", request="desk on")
dl.db.execute("UPDATE decisions SET seen='mismatch' WHERE id=?", (d1,))
dl.record("light.hall", "on", None, "light.turn_on", {}, status="error", reason="HA said 500", request="hall on")
dl.db.commit()
cs = Cases(st)
cs.add("when do the bins go out?", "Wednesday", "Tuesday")
st.log("cant_do", None, request="make me a coffee")
st.log("ask", None, question="where's the warranty?", answer="I don't know; nothing in memory matches.")
st.log("ask", None, question="bins?", answer="Wednesday night.")
n = sp.collect(dl, cs)
k = sp.counts()
check("surprises: one queue, every kind", k == {"missed": 1, "failed": 1, "corrected": 1, "cant_do": 1, "dont_know": 1},
      k)
check("surprises: collected once", sp.collect(dl, cs) == 0)
first = sp.open()[0]["id"]
sp.seen(first)
check("surprises: marked seen leaves the open list", all(x["id"] != first for x in sp.open()))

# ---- settling conflicts -------------------------------------------------------------------------------------------
v, why = R.settle("climate.living", {"Dana": 21, "Sam": 19}, room="living", rooms={"Dana": "living"},
                  home={"Dana": True, "Sam": True})
check("settle: the person in the room wins", v == 21 and "room" in why, (v, why))
v, why = R.settle("climate.living", {"Dana": 21, "Sam": 19, "Ana": 20}, room="living")
check("settle: nobody there: the middle", v == 20, (v, why))
v, why = R.settle("light.porch", {"Dana": 50, "Sam": 80}, home={"Dana": True, "Sam": False})
check("settle: only those home count", v == 50, (v, why))
v, why = R.settle("media_player.tv", {"Dana": "on", "Sam": "off"})
check("settle: on or off for noise or heat: the careful choice (off)", v == "off", (v, why))
v, why = R.settle("light.hall", {"Dana": "on", "Sam": "off"})
check("settle: a light: on", v == "on", (v, why))

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
