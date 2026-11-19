"""Ten design checks on phases 8 to 15, each a property the design spec states, probed against the code.

  1 safety ceiling        nothing learned, drafted or from another assistant acts above the open level
  2 privacy by viewer     every new read path hides what the viewer may not see
  3 email and documents   never become a record, a link or an action
  4 shadow never acts     shadow and provisional records never act; audits answer the usual way
  5 one change a night    promotions are replay-gated, also before any corrections exist
  6 one ask cap           every new ask is a routine push (counted, batched, quiet on a rough day)
  7 forgotten stays gone  a purge reaches the stores phases 8-15 added, and survives a restore
  8 clear-day consent     new behaviours start only after a yes on a clear day plus 24 hours
  9 clinical safeguards   no dosing, no advice, no medicine experiments, red flags first
 10 failure modes         restarts, Home Assistant down, and no polling of every state

  python tests/test_design_checks.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types
from datetime import date, datetime

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
from homemind import health as HL, things as T, tracking as TR, upkeep as U, experiments as X  # noqa: E402
from homemind.consent import Consent  # noqa: E402
from homemind.day import Day  # noqa: E402
from homemind.home import Home  # noqa: E402
from homemind.learned import Learned  # noqa: E402
from homemind.learned_answers import LearnedAnswers  # noqa: E402
from homemind.speakers import VIEWER  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(c):
    return asyncio.run(c)


def store():
    return Store(os.path.join(tempfile.mkdtemp(), "mind.db"))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeHA:
    def __init__(self):
        self.calls = []
        self.available = True

    async def state(self, eid):
        return {"state": "on"} if eid == "input_boolean.mind_control_enabled" else None

    async def call(self, dom, svc, data, approved=False):
        self.calls.append((dom, svc, data))
        return 200


def home():
    h = Home(store(), FakeHA(), {"home_controls": ["light.*", "lock.*", "cover.*", "climate.*", "switch.*"]})
    h.catalog = {"light.desk": {"name": "desk lamp", "domain": "light", "state": "off", "attrs": {}},
                 "lock.front": {"name": "front door", "domain": "lock", "state": "locked", "attrs": {}},
                 "cover.garage": {"name": "garage door", "domain": "cover", "state": "closed", "attrs": {}},
                 "climate.hall": {"name": "hall heat", "domain": "climate", "state": "heat", "attrs": {}},
                 "switch.heater": {"name": "space heater", "domain": "switch", "state": "off", "attrs": {}}}
    h.areas = {e: "living" for e in h.catalog}
    h.access.baseline(list(h.catalog))
    return h


# ---- 1 safety ceiling ---------------------------------------------------------------------------------------------
h = home()
lr = Learned(h.store, h)
for eid, act in (("lock.front", "off"), ("cover.garage", "on"), ("climate.hall", "on"), ("switch.heater", "on")):
    check(f"1 safety: never a learned template for {eid}", lr.check({"entity_id": eid, "action": act}) is not None)
sc = T.Scenes(types.SimpleNamespace(home=h, mind=types.SimpleNamespace(store=h.store)))
check("1 safety: a scene can't hold a lock, a cover, heating or a heater",
      not any(sc.allowed_step({"entity_id": e, "action": "on"}) for e in
              ("lock.front", "cover.garage", "climate.hall", "switch.heater")) and
      sc.allowed_step({"entity_id": "light.desk", "action": "on"}))
src = open(os.path.join(os.path.dirname(T.__file__), "main.py")).read()
check("1 safety: travel mode only switches an input_boolean or a switch", "must be an input_boolean or a switch" in src)
check("1 safety: other assistants: anything above the open level refused in _run_plan",
      'str(source or "").startswith("other:") and need != "open"' in src)

# ---- 2 privacy by viewer ------------------------------------------------------------------------------------------
st = store()
th = T.Things(st)
for n in ("red toolbox", "shed", "drill"):
    th.create(n)
st.remember("The drill is in the shed.", title="Drill", meta={"scope": "personal", "owner": "Dana"})
st.remember("The passport is in the blue folder.", title="Passport", meta={"scope": "personal", "owner": "Dana"})
th.link_notes()
check("2 privacy: a personal note never makes an 'in' link others can follow",
      th.answer_where("where's the drill?", "Sam") is None, th.answer_where("where's the drill?", "Sam"))
check("2 privacy: names from personal notes are never offered as things", "passport" not in th.noticed() and
      "blue folder" not in th.noticed(), th.noticed())
la = LearnedAnswers(st)
it = st.remember("Dana's dentist is Dr Lee.", title="Dentist", meta={"scope": "personal", "owner": "Dana"})
rid = la._new_answer(st.get(it["id"]), "Dana's dentist is Dr Lee.", "who is dana's dentist", "t")
la.set_status(rid, "trusted")
VIEWER.set("Sam")
check("2 privacy: a learned answer from a personal note isn't said to someone else",
      run(la.reflex("who is dana's dentist")) is None)
VIEWER.set(None)

# ---- 3 email and documents ----------------------------------------------------------------------------------------
st = store()
th = T.Things(st)
for n in ("drill", "shed"):
    th.create(n)
st.remember("Your drill is in the shed, says this mail.", title="Mail", source="email")
th.link_notes()
check("3 email: a mailed note makes no link", th.where(th.find("drill")["id"]) == [])
st.remember("The kayak is in the garage.", title="Doc", source="doc:inventory.pdf")
check("3 documents: a document's names aren't offered as things", "kayak" not in th.noticed(), th.noticed())
m = st.remember("The boiler was serviced in March 2025.", title="Boiler", source="email")
check("3 email: never a learned answer", "never a learned record" in
      (LearnedAnswers(st).check_answer(st.get(m["id"]), "It was serviced in March 2025.") or ""))

# ---- 4 shadow never acts ------------------------------------------------------------------------------------------
h = home()
lr = Learned(h.store, h)
t1 = lr._new("light.desk", "on", None, "turn on the desk lamp", "t")
check("4 shadow: a shadow template never acts", run(lr.reflex("turn on the desk lamp")) is None)
lr.set_status(t1, "provisional")
check("4 shadow: nor a provisional one", run(lr.reflex("turn on the desk lamp")) is None)
la = LearnedAnswers(h.store)
n = h.store.remember("Bins go out Tuesday night.", title="Bins")
a1 = la._new_answer(h.store.get(n["id"]), "Tuesday night.", "when do the bins go out", "t")
check("4 shadow: a shadow answer is never said", run(la.reflex("when do the bins go out")) is None)

# ---- 5 one change a night, always gated --------------------------------------------------------------------------
st = store()
good = st.remember("Bins go out Tuesday night.", title="Bins")
other = st.remember("Recycling goes out Thursday night.", title="Recycling")
la = LearnedAnswers(st)
ok_ = la._new_answer(st.get(good["id"]), "Tuesday night.", "when do the bins go out", "t")
bad = la._new_answer(st.get(other["id"]), "Thursday night.", "when do the bins go out", "t")
la.set_status(ok_, "provisional")
la.set_status(bad, "provisional")
p = run(la.nightly())
check("5 gate: with no corrections yet, the replay still runs: an answer whose note doesn't come up for its own "
      "question isn't promoted", la.get(bad)["status"] != "trusted", (p, la.get(bad)["status"]))
src = open(os.path.join(os.path.dirname(T.__file__), "main.py")).read()
check("5 one kind of change a night: house and answers alternate", "toordinal() % 2" in src)

# ---- 6 one ask cap -----------------------------------------------------------------------------------------------
check("6 asks: trackers and the events checklist are routine and batched",
      src.count('tier="routine", batch=True') >= 2)
check("6 asks: nudges and the exertion notice are routine (counted against the day's budget)",
      'tk.nudge_message(n), "data"' in src and src.count('tier="routine")') >= 2)
st = store()
d = Day(st)
tk = TR.Tracking(st, d)
tk.add_tracker("fog", "Dana")
d.set("rough", "Dana")
check("6 asks: nothing from trackers or nudges on a rough day", tk.paused("Dana") == "a rough day" and
      tk.due("Dana") == [])

# ---- 7 forgotten stays gone --------------------------------------------------------------------------------------
folder = tempfile.mkdtemp()
st = Store(os.path.join(folder, "mind.db"))
fl = U.ForgetList(st, folder)


def forgotten(rows, vals):
    fl.record_notes(rows, vals)
    U.scrub(st, [r["id"] for r in rows], vals)
st.on_forget = forgotten
code = st.remember("The shed lock combination is 7391.", title="Shed lock")
from homemind.ha import HA  # noqa: E402
from homemind.outbox import Outbox  # noqa: E402
from homemind.rooms import Surprises  # noqa: E402
from homemind.upkeep import LearnedHistory  # noqa: E402
ob = Outbox(st)
ob.add("mobile_app_phone", {"message": "Shed combination 7391"}, "routine", "sent")
Surprises(st).add("dont_know", "is the shed code 7391?", "x1")
la = LearnedAnswers(st)
la._new_answer(st.get(code["id"]), "It's 7391.", "what's the shed code", "t")
LearnedHistory(st).save_week()
st.purge([code["id"]])
left = json.dumps([dict(r) for r in st.db.execute("SELECT data FROM outbox")] +
                  [dict(r) for r in st.db.execute("SELECT what FROM surprises")] +
                  [dict(r) for r in st.db.execute("SELECT answer FROM learned_answers")] +
                  [dict(r) for r in st.db.execute("SELECT data FROM learned_weeks")])
check("7 forget: a purged value leaves the outbox, the surprises queue, learned answers and their weekly saves",
      "7391" not in left, left[:300])

# ---- 8 clear-day consent -----------------------------------------------------------------------------------------
st = store()
noon = datetime.combine(date.today(), datetime.min.time()).replace(hour=11).timestamp()
cc = Clock(noon)
day = Day(st, clock=cc)
con = Consent(st, day, clock=cc)
started = []


async def start(p):
    started.append(p)
    return {"started": True}
con.appliers["experiment_start"] = start
c = con.propose("experiment_start", "Experiment: lie down after lunch", {"change": "x"})
day.set("good")
con.yes(c["id"])
run(con.due())
check("8 consent: an experiment doesn't start the moment you say yes", started == [])
cc.t += 86401
run(con.due())
check("8 consent: it starts 24 hours after a yes on a clear day", len(started) == 1)
check("8 consent: automations switched on go through consent", 'A.consent.propose("automation_on"' in src)

# ---- 9 clinical safeguards ----------------------------------------------------------------------------------------
st = store()
tk = TR.Tracking(st, Day(st))
r = tk.med_said("took ibuprofen 400mg", "Dana")
check("9 clinical: the medicines log never advises", not any(w in r["answer"].lower() for w in
                                                              ("should", "safe", "can take", "max", "limit")))
try:
    X.Experiments(st).plan("Does melatonin help?", "take melatonin at 10", "sleep_timing")
    check("9 clinical: never an experiment with a supplement", False)
except ValueError:
    check("9 clinical: never an experiment with a supplement", True)
check("9 clinical: the zones notice is neutral", TR.Zones(st, {"exertion_zones": True}, None).notice() is None and
      "Rest when you can" in open(TR.__file__).read())
check("9 clinical: health answers describe, never judge", all(w not in open(HL.__file__).read().split('"""')[2]
                                                               for w in ("You should", "great job", "worrying")))
check("9 clinical: red flags are checked before the medicines log, Firefly and where-questions",
      src.index("urgent = redflags.check(b[\"question\"])") < src.index("fly = self.mind.store.kv_get(\"firefly\")")
      < src.index("where_question(b[\"question\"], names)") < src.index("tk.med_said(b[\"question\"]"))

# ---- 10 failure modes --------------------------------------------------------------------------------------------
st = store()
app = types.SimpleNamespace(home=home(), mind=types.SimpleNamespace(store=st))
s1 = T.Scenes(app, clock=Clock(1000.0))
s1.previews["x"] = {"scene": {"name": "x", "steps": []}, "before": {"light.desk": {"state": "off", "attrs": {}}},
                    "until": 1600.0}
s1._save_previews()
s2 = T.Scenes(app, clock=Clock(2000.0))           # after a restart
check("10 restart: a scene preview started before a restart is still undone", run(s2.undo_due()) == ["x"])
cc = Clock(noon)
day = Day(st, clock=cc)
con = Consent(st, day, clock=cc)
fails = {"n": 0}


async def flaky(p):
    fails["n"] += 1
    if fails["n"] < 3:
        raise OSError("Home Assistant is restarting")
    return {"on": True}
con.appliers["automation_on"] = flaky
day.set("good")
c = con.propose("automation_on", "Switch on: porch", {"entity_id": "automation.porch"})
con.yes(c["id"])
cc.t += 86401
run(con.due())
check("10 HA down: an agreed change that couldn't be applied stays agreed", con.get(c["id"])["status"] == "agreed")
run(con.due())
run(con.due())
check("10 HA down: and is applied once Home Assistant is back", con.get(c["id"])["status"] == "done" and fails["n"] == 3)


class CountHA:
    available = True

    def __init__(self):
        self.all = 0

    async def states(self):
        self.all += 1
        return [{"entity_id": "sensor.watch_heart_rate", "state": "72", "last_updated": datetime.now().astimezone()
                 .isoformat(), "attributes": {"unit_of_measurement": "bpm", "friendly_name": "Watch HR"}}]

    async def state(self, eid):
        return {"state": "73", "last_updated": datetime.now().astimezone().isoformat(),
                "attributes": {"unit_of_measurement": "bpm", "friendly_name": "Watch HR"}}


cha = CountHA()
hl = HL.Health(None, None, cha)
for _ in range(10):
    run(hl.live_heart_rate())
check("10 load: the heart-rate sensor is found once, then read on its own (not every state every 20 s)",
      cha.all == 1, cha.all)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
