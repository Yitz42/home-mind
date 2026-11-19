"""Phase 3 of the design (0.27): the check-in and the day's ask budget, undo, why and "help me", two times per fact,
who may hear a private answer, the panel's signed-in person, guest tokens and token expiry. No Home Assistant needed.

  python tests/test_conversation.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import types

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
from homemind import conversation as conv, day as D, decisions as DE, migrations  # noqa: E402
from homemind import main as M  # noqa: E402
from homemind.approval import ApprovalNeeded  # noqa: E402
from homemind.decisions import DecisionLog  # noqa: E402
from homemind.ha import HA  # noqa: E402
from homemind.home import Home  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.tokens import Tokens  # noqa: E402

FAILS = []
LOOP = asyncio.new_event_loop()


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(c):
    return LOOP.run_until_complete(c)


def store():
    return Store(os.path.join(tempfile.mkdtemp(), "m.db"))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


# ---- the check-in ---------------------------------------------------------------------------------------------------
for text, want, expecting in (("rough", "rough", False), ("having a low day", "low", False), ("ugh", "rough", False),
                              ("pretty good today", "good", False), ("I'm feeling rough", "rough", False),
                              ("good", "good", True), ("ok", "good", True), ("fine", None, False),
                              ("ok", None, False), ("good", None, False), ("turn on the lights", None, True),
                              ("rough", "rough", True), ("I'm wiped", "rough", False), ("meh", "low", False)):
    got = D.parse(text, expecting)
    check(f"check-in words: {text!r}{' (just asked)' if expecting else ''} -> {want}", got == want, got)

clk = Clock(time.mktime((2026, 11, 18, 9, 0, 0, 0, 0, -1)))
day = D.Day(store(), clock=clk)
check("day: no answer is unknown", day.get("Dana") == "unknown" and day.budget("Dana") == 1)
check("day: unknown holds energy at 60", day.energy(85, "Dana") == 60)
check("day: the question is asked once a day", day.asked("Dana") is False and day.asked("Dana") is True)
check("day: a bare 'good' counts just after the question", day.expecting("Dana"))
clk.t += 16 * 60
check("day: ... but not 15 minutes later", not day.expecting("Dana"))
day.set("rough", "Dana")
check("day: rough is 0 asks and energy 20", day.get("Dana") == "rough" and day.budget("Dana") == 0 and
      day.energy(85, "Dana") == 20)
day.set("good", "Sam")
check("day: the household takes the most careful answer", day.get() == "rough")
clk.t += 24 * 3600
check("day: a new day starts unknown", day.get("Dana") == "unknown")
t0 = clk.t
clk.t = time.mktime((2026, 11, 20, 3, 0, 0, 0, 0, -1))
day.set("low", "Dana")
clk.t = time.mktime((2026, 11, 19, 23, 0, 0, 0, 0, -1))
check("day: before 5 am counts as the day before", day.get("Dana") == "low")
try:
    day.set("great", "Dana")
    check("day: only good, low, rough", False)
except ValueError:
    check("day: only good, low, rough", True)

# ---- the day's ask budget on phone pushes ------------------------------------------------------------------------
ha = HA(None)
sent = []


async def call(dom, svc, data, approved=False):
    sent.append(data.get("message"))
    return 200
ha.call = call
d2 = D.Day(store(), clock=Clock(time.time()))
ha.day = d2
check("budget: unknown day allows 1 routine", run(ha.push("notify.p", {"message": "r1"})) == "sent" and
      run(ha.push("notify.p", {"message": "r2"})) == "capped")
check("budget: important isn't held to it", run(ha.push("notify.p", {"message": "i1"}, "important")) == "sent")
d2.set("rough")
check("rough: routine expires quietly", run(ha.push("notify.p", {"message": "r3"})) == "quiet")
check("rough: important waits", run(ha.push("notify.p", {"message": "i2"}, "important")) == "held")
check("rough: what the person just asked for goes", run(ha.push("notify.p", {"message": "a"}, "important", now=True))
      == "sent")
check("rough: safety goes", run(ha.push("notify.p", {"message": "s"}, "safety")) == "sent")
d2.set("good")
ha.routine_sent = {}
check("good: about 4 routine", [run(ha.push("notify.p", {"message": f"g{i}"})) for i in range(5)] ==
      ["sent"] * 4 + ["capped"])

# ---- check-in through the app ------------------------------------------------------------------------------------
A = types.SimpleNamespace(day=D.Day(store()))
A.checkin_reply = M.App.checkin_reply.__get__(A)
A.on_day_tap = M.App.on_day_tap.__get__(A)
r = A.checkin_reply("rough day")
check("app: 'rough day' records it and says the rough-day line", r["checkin"] == "rough" and
      r["answer"] == "Taking it easy today. Nothing needs you.")
check("app: other words aren't a check-in", A.checkin_reply("what's the weather") is None)
run(A.on_day_tap("HM_DAY_GOOD"))
check("app: a tap on Good records it (replacing the earlier answer)", A.day.get() == "good")
check("app: unknown tap does nothing", run(A.on_day_tap("HM_DAY_MAYBE")) is None)

# ---- decisions: migration 2 on an existing log --------------------------------------------------------------------
p = os.path.join(tempfile.mkdtemp(), "log.db")
c = sqlite3.connect(p, isolation_level=None)
migrations.migrate(c, DE.STEPS[:1], p)
c.execute("INSERT INTO decisions (ts, status) VALUES (1, 'done')")
c.close()
L = DecisionLog(p)
cols = {r[1] for r in L.db.execute("PRAGMA table_info(decisions)")}
check("migration 2: an existing log gains prior_attrs and undone_by, rows kept",
      L.version == (1, len(DE.STEPS)) and {"prior_attrs", "undone_by"} <= cols and L.get(1)["status"] == "done", (L.version, cols))

# ---- undo ---------------------------------------------------------------------------------------------------------


class FakeHA:
    def __init__(self):
        self.calls = []

    async def state(self, eid):
        return {"state": "on"} if eid == "input_boolean.mind_control_enabled" else None

    async def call(self, dom, svc, data):
        self.calls.append((dom, svc, data))
        return 200


h = Home(store(), FakeHA(), {"home_controls": ["light.*", "lock.*", "fan.*"]})
h.catalog = {"light.desk": {"name": "desk light", "kind": "device", "domain": "light", "state": "on",
                            "attrs": {"brightness": 128}},
             "fan.bed": {"name": "bedroom fan", "kind": "device", "domain": "fan", "state": "off", "attrs": {}},
             "lock.front": {"name": "front door", "kind": "device", "domain": "lock", "state": "locked", "attrs": {}}}
clk = Clock(50_000_000.0)
h.decisions = DecisionLog(":memory:", clock=clk, quiet_seconds=0)
app = types.SimpleNamespace(home=h, decisions=h.decisions)
run(h.act("fan.bed", "speed", 40, source="voice", why={"request": "fan to 40", "conversation_id": "c1"}))
h.catalog["fan.bed"]["state"], h.catalog["fan.bed"]["attrs"] = "on", {"percentage": 40}
clk.t += 5
run(h.act("light.desk", "brightness", 20, source="voice", why={"request": "dim the desk light", "conversation_id": "c1",
                                                                "person": "Dana"}))
check("undo: the act recorded the light's brightness before", h.decisions.recent(1)[0]["prior_attrs"] ==
      {"brightness": 128})
clk.t += 5
r = run(conv.undo(app, "c1"))
check("undo: the light goes back to 50%", h.ha.calls[-1] == ("light", "turn_on", {"entity_id": "light.desk",
                                                                                  "brightness_pct": 50})
      and r["answer"] == "Undone. Desk light back at 50%.", (r, h.ha.calls[-1]))
r = run(conv.undo(app, "c1"))
check("undo again: steps back one more (the fan off)", h.ha.calls[-1] == ("fan", "turn_off", {"entity_id": "fan.bed"})
      and "Bedroom fan back off" in r["answer"], (r, h.ha.calls[-1]))
r = run(conv.undo(app, "c1"))
check("undo: nothing left says so", r["answer"].startswith("There's nothing") and r["undo"] is None)
run(h.act("lock.front", "unlock", source="voice", approved=True, why={"request": "unlock"}))
h.catalog["lock.front"]["state"] = "unlocked"
r = run(conv.undo(app))
check("undo: putting a lock back still goes through the checks (phone)", "error" in r and "phone" in r["error"], r)
clk.t += 700
run(h.act("fan.bed", "on", source="voice", why={"request": "fan on"}))
clk.t += 601
check("undo: only the last 10 minutes", run(conv.undo(app))["undo"] is None)
check("undo: the words", all(conv.UNDO.match(t) for t in ("undo", "Undo that", "put it back", "oops, undo it",
                                                            "change it back please")) and
      not conv.UNDO.match("undo the dishwasher timer") and not conv.UNDO.match("go back to sleep"))
check("inverse: unknown prior can't be undone", conv.inverse({"entity_id": "light.x", "prior": "unavailable"}) is None)

# ---- why ----------------------------------------------------------------------------------------------------------
clk.t = time.time()
h2 = Home(store(), FakeHA(), {"home_controls": ["light.*"]})
h2.catalog = {"light.hall": {"name": "hall light", "kind": "device", "domain": "light", "state": "off", "attrs": {}},
              "sensor.humidity": {"name": "humidity", "kind": "reading", "domain": "sensor", "state": "60",
                                  "attrs": {}}}
h2.decisions = DecisionLog(":memory:", quiet_seconds=0)
app2 = types.SimpleNamespace(home=h2, decisions=h2.decisions)
run(h2.act("light.hall", "on", source="voice", why={"request": "hall light on please", "person": "Dana",
                                                     "channel": "voice"}))
w = conv.why(app2, "why is the hall light on?", "Dana")
check("why: one sentence with who asked and how", w and "You asked me to by voice" in w["answer"] and
      "hall light on please" in w["answer"] and "undo" in w["answer"], w)
w = conv.why(app2, "why is the hall light on?", "Sam")
check("why: someone else's request is a household rule", w and w["answer"].startswith("A household rule"), w)
check("why: a question for the brain isn't taken", conv.why(app2, "why is the humidity high?") is None and
      conv.why(app2, "why is the sky blue?") is None)
h2.decisions.record("light.hall", "turn_off", service="light.turn_off", channel="automation", source="automation.bed",
                    records=[{"automation": "automation.bed", "name": "Bedtime"}])
w = conv.why(app2, "why did you turn off the hall light")
check("why: an automation names itself and offers to stop", w and "Bedtime" in w["answer"] and "stop" in w["answer"], w)
check("help: the task in 'help me' / 'walk me through'", conv.help_request("help me reset the router") ==
      "reset the router" and conv.help_request("Walk me through how to descale the kettle?") == "descale the kettle"
      and conv.help_request("turn on the light") is None)

# ---- two times per fact -------------------------------------------------------------------------------------------
st = store()
t1, t2, t3 = 1_000_000.0, 2_000_000.0, 3_000_000.0
st.clock = Clock(t1)
st.remember(text="The gate code is 1111.", key="gate|code")
st.clock = Clock(t2)
st.remember(text="The gate code is 2222.", key="gate|code")
st.clock = Clock(t3)
st.remember(text="The gate code is 1500.", key="gate|code", meta={"valid_from": 1_500_000.0})   # told late
v = lambda when, by: (st.as_of("gate|code", when, by) or {}).get("body")  # noqa: E731
check("as of (valid): before anything, nothing", v(500_000, "valid") is None)
check("as of (valid): what was true then", v(1_200_000, "valid") == "The gate code is 1111." and
      v(1_700_000, "valid") == "The gate code is 1500." and v(2_500_000, "valid") == "The gate code is 2222.")
check("as of (recorded): what Home Mind believed then", v(1_700_000, "recorded") == "The gate code is 1111." and
      v(2_500_000, "recorded") == "The gate code is 2222.")
row = st.as_of("gate|code", 1_700_000, "valid")
check("as of: with when it took over and when it was learned", row["valid_from"] == 1_500_000.0 and
      row["valid_to"] == t2 and row["recorded_at"] == t3, row)
try:
    st.as_of("gate|code", 1, "someday")
    check("as of: by is valid or recorded", False)
except ValueError:
    check("as of: by is valid or recorded", True)

# ---- who may hear it ----------------------------------------------------------------------------------------------
st = store()
pid = st.remember(text="My blood test is Friday.", kind="fact", meta={"scope": "personal", "owner": "Dana"})["id"]
hid = st.remember(text="The bins go out Tuesday.", kind="fact")["id"]
pushed = []


async def push(target, data, tier="routine", now=False):
    pushed.append((target, data["message"], tier, now))
    return "sent"
P = types.SimpleNamespace(mind=types.SimpleNamespace(store=st), ha=types.SimpleNamespace(push=push), opts={
    "people": [{"name": "Dana", "persons": "person.dana", "notify": "mobile_app_dana"}, {"name": "Sam"}]},
    home=types.SimpleNamespace(catalog={"person.dana": {"name": "Dana", "state": "home", "attrs": {}},
                                        "person.sam": {"name": "Sam", "state": "home", "attrs": {}}}))
for n in ("people", "phone_of", "alone", "private_by_voice"):
    setattr(P, n, getattr(M.App, n).__get__(P))
res = {"answer": "Your blood test is Friday.", "hits": [{"id": pid}]}
r = run(P.private_by_voice(res, "Dana"))
check("private: not alone, a personal answer goes to the phone", r["private"] and "private note" in r["answer"] and
      pushed[-1] == ("notify.mobile_app_dana", "Your blood test is Friday.", "important", True), (r, pushed))
r = run(P.private_by_voice({"answer": "Tuesday.", "hits": [{"id": hid}]}, "Dana"))
check("private: household answers are spoken", r["answer"] == "Tuesday.")
P.home.catalog["person.sam"]["state"] = "not_home"
r = run(P.private_by_voice(dict(res), "Dana"))
check("private: alone, it is spoken", r["answer"] == "Your blood test is Friday.")
P.home.catalog["person.sam"]["state"] = "home"
r = run(P.private_by_voice(dict(res), "Sam"))
check("private: no phone known: says so, sends nothing", r["private"] and "on your own" in r["answer"] and
      len(pushed) == 1, r)

# ---- who is asking ------------------------------------------------------------------------------------------------
Q = types.SimpleNamespace(opts={"people": [{"name": "Dana", "ha_users": "abc123"}]},
                          home=types.SimpleNamespace(catalog={}))
Q.people = M.App.people.__get__(Q)
Q.panel_person = M.App.panel_person.__get__(Q)
check("panel: the signed-in Home Assistant user is that person", Q.panel_person("abc123") == "Dana" and
      Q.panel_person("zzz") is None)
tk = Tokens(store())
t = tk.make("claude", "read", person="Dana")
check("tokens: expire after 90 days by default", abs(t["expires"] - time.time() - 90 * 86400) < 5)
check("tokens: a live one works", tk.check(t["token"]) is not None)
recs = tk._all()
recs[-1]["expires"] = time.time() - 1
tk.store.kv_set("tokens", recs)
check("tokens: an expired one doesn't", tk.check(t["token"]) is None)
t0 = tk.make("forever", "read", days=0)
check("tokens: days 0 never expires", t0["expires"] is None and tk.check(t0["token"]) is not None)
try:
    tk.make("x", "read", days=-1)
    check("tokens: days must be 0 to 3650", False)
except ValueError:
    check("tokens: days must be 0 to 3650", True)
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app", "homemind",
                        "main.py")).read()
check("tokens: a read/write token tied to no one is a guest",
      'if rec.get("person") or rec["scope"] != "admin":' in src and 'VIEWER.set(rec.get("person") or "")' in src)

# the check-in, right after the question: everyday replies with extra words count; questions and long replies don't
from homemind.day import parse as day_parse  # noqa: E402
for said, want in (("I would say it's about goodish.", "good"), ("pretty much fine I guess", "good"),
                   ("not too bad", "good"), ("not great honestly", "low"), ("kind of a rough one", "rough")):
    check(f"check-in reply right after the question: {said!r} -> {want}", day_parse(said, expecting=True) == want,
          day_parse(said, expecting=True))
for said in ("what's the weather?", "turn the lights off", "is the garage closed?", "set the fan to low",
             "ok turn off the kitchen lights", "turn the lights down low", "not bad, can you turn on the den lamp",
             "fan to 40"):
    check(f"not a check-in reply: {said!r}", day_parse(said, expecting=True) is None)
check("not right after the question, 'about goodish' alone isn't taken as the day", day_parse("about goodish") is None)

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
