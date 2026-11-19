"""Phase 1 of the design (0.27): the decision log, outcome checks, expected versus seen, migrations tried on a copy,
and replay of logged requests. No Home Assistant needed.

  python tests/test_decisions.py
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:                      # the engine only needs these names when it talks to Home Assistant
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: None
    m.WSMsgType = types.SimpleNamespace(TEXT=1)
    sys.modules["aiohttp"] = m
from homemind import migrations, replay  # noqa: E402
from homemind import automations as A  # noqa: E402
from homemind.decisions import DecisionLog, expect, satisfied, who, context_of, wilson  # noqa: E402
from homemind.home import Home  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


LOOP = asyncio.new_event_loop()


def run(c):
    return LOOP.run_until_complete(c)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def st(state, ctx=None, **attrs):
    return {"state": state, "attributes": attrs, "context": ctx or {}}


# ---- migrations ----------------------------------------------------------------------------------------------------
d = tempfile.mkdtemp()
p = os.path.join(d, "m.db")
c = sqlite3.connect(p, isolation_level=None)
steps = ["CREATE TABLE a (x INTEGER);", "ALTER TABLE a ADD COLUMN y TEXT;\nCREATE INDEX a_y ON a(y);"]
check("migrate: fresh file to version 2", migrations.migrate(c, steps, p) == (0, 2) and migrations.version(c) == 2)
check("migrate: nothing to do at the latest", migrations.migrate(c, steps, p) == (2, 2))
c.execute("INSERT INTO a (x, y) VALUES (1, 'kept')")
bad = steps + ["ALTER TABLE a ADD COLUMN z TEXT;", "SELECT * FROM no_such_table;"]
try:
    migrations.migrate(c, bad, p)
    check("migrate: a failing step is refused", False)
except migrations.MigrationError as e:
    check("migrate: a failing step is refused before the real file changes", "4" in str(e))
cols = [r[1] for r in c.execute("PRAGMA table_info(a)")]
check("migrate: the real file is untouched (no column z, still version 2)",
      "z" not in cols and migrations.version(c) == 2 and c.execute("SELECT y FROM a").fetchone()[0] == "kept", cols)
try:
    migrations.migrate(c, steps[:1], p)
    check("migrate: a newer database is refused", False)
except migrations.MigrationError:
    check("migrate: a database newer than the code is refused", True)
fn_steps = steps + [lambda db: db.execute("UPDATE a SET y = 'via function'")]
migrations.migrate(c, fn_steps, p)
check("migrate: a step can be a function", c.execute("SELECT y FROM a").fetchone()[0] == "via function")
c.close()

# ---- what should happen ------------------------------------------------------------------------------------------
check("expect: light on", expect("light.turn_on", {}) == {"state_in": ["on"]})
e = expect("light.turn_on", {"brightness_pct": 40})
check("expect: brightness 40% = 102 of 255", e["attr"] == "brightness" and e["value"] == 102)
check("expect: brightness 0 is off", expect("light.turn_on", {"brightness_pct": 0}) == {"state_in": ["off"]})
check("expect: toggle from on = off", expect("switch.toggle", {}, prior="on") == {"state_in": ["off"]})
check("expect: toggle from unknown = nothing to check", expect("switch.toggle", {}, prior="unavailable") is None)
check("expect: a script can't be checked", expect("script.turn_on", {}) is None)
check("expect: cover opening counts", "opening" in expect("cover.open_cover", {})["state_in"])
check("expect: fan 0% = off", expect("fan.set_percentage", {"percentage": 0}) == {"state_in": ["off"]})
check("expect: lock", expect("lock.lock", {}) == {"state_in": ["locked", "locking"]})
check("expect: number", expect("input_number.set_value", {"value": 42}) == {"number": 42.0, "tol": 1e-6})
check("satisfied: unavailable is unknown, never failure", satisfied({"state_in": ["on"]}, st("unavailable")) is None)
check("satisfied: brightness within 4", satisfied(e, st("on", brightness=100)) is True and
      satisfied(e, st("on", brightness=200)) is False)
check("satisfied: fan 3-speed rounding", satisfied(expect("fan.set_percentage", {"percentage": 50}),
                                                  st("on", percentage=67)) is True)
check("who: our call", who({"id": "c1"}, "c1") == "home_mind")
check("who: a person in HA", who({"id": "x", "user_id": "u9"}, "c1") == "user:u9")
check("who: an automation", who({"id": "x", "parent_id": "p"}, "c1") == "automation")
check("who: the device itself", who({"id": "x"}, "c1") == "device")
check("context_of: REST list", context_of([{"entity_id": "light.a", "context": {"id": "abc"}}])[0] == "abc")
check("context_of: return_response shape",
      context_of({"changed_states": [{"context": {"id": "r1"}}], "service_response": {}})[0] == "r1")
check("context_of: nothing changed", context_of([]) == (None, []))
lo, hi = wilson(19, 20)
check("wilson: 19/20 has a wide interval", 0.75 < lo < 0.9 and hi > 0.98, (lo, hi))

# ---- the log: match by event, by sweep; mismatch, unknown, unchecked -----------------------------------------------
clk = Clock()
path = os.path.join(d, "log.db")
L = DecisionLog(path, clock=clk, quiet_seconds=60, override_seconds=600)
i1 = L.record("light.desk", "on", None, "light.turn_on", {"entity_id": "light.desk"}, context_id="c1",
              request="desk light on", channel="voice", route="local", model="fg", latency_ms=420, prior="off")
clk.t += 2
L.on_state("light.desk", st("on", {"id": "c1"}), st("off"))
r = L.get(i1)
check("match from the event stream, by Home Mind, with its delay",
      r["seen"] == "match" and r["seen_by"] == "home_mind" and r["delay"] == 2.0, r)
check("logged how it was decided", r["route"] == "local" and r["latency_ms"] == 420 and r["request"] == "desk light on")

i2 = L.record("fan.f", "speed", 60, "fan.set_percentage", {"percentage": 60}, context_id="c2")
i3 = L.record("light.porch", "on", None, "light.turn_on", {}, context_id="c3")
i4 = L.record("script.bed", "run", None, "script.turn_on", {}, context_id="c4")
i5 = L.record("light.attic", "off", None, "light.turn_off", {}, context_id="c5")
check("a script is logged as unchecked", L.get(i4)["seen"] == "unchecked")
clk.t += 100
STATES = {"fan.f": st("on", percentage=20), "light.porch": st("unavailable"), "light.attic": st("off")}


async def get_state(eid):
    return STATES.get(eid)

run(L.sweep(get_state))
check("sweep: fan at the wrong speed is a mismatch", L.get(i2)["seen"] == "mismatch")
check("sweep: an unavailable light is unknown", L.get(i3)["seen"] == "unknown")
check("sweep: a light found off is a match, seen by the check",
      L.get(i5)["seen"] == "match" and L.get(i5)["seen_by"] == "checked")
check("sweep: the check's delay doesn't teach the profile", L.profile("light.attic")["delays"] == [])

# a refused request is logged and never waits for an outcome
i6 = L.record("lock.front", "unlock", None, status="refused", reason="needs a yes")
check("refused: logged with its reason, nothing expected",
      L.get(i6)["status"] == "refused" and L.get(i6)["expected"] is None and L.get(i6)["seen"] is None)

# ---- overrides ---------------------------------------------------------------------------------------------------
clk.t += 10        # past the quiet period
L.on_state("light.desk", st("off", {"id": "z", "user_id": "u1"}), st("on"))
ov = L.get(i1)["override"]
check("override: a person turning it back off within 10 min", ov and ov["by"] == "user:u1" and ov["to"]["state"] == "off",
      ov)
check("override: counted in the profile", L.profile("light.desk")["overrides"] == 1)

i7 = L.record("light.k", "on", None, "light.turn_on", {}, context_id="k1")
L.on_state("light.k", st("on", {"id": "k1"}))
clk.t += 5
L.on_state("light.k", st("off", {"id": "y"}), st("unavailable"))
check("rejoin: a device coming back from unavailable is not an override", L.get(i7)["override"] is None)
i8 = L.record("light.k", "off", None, "light.turn_off", {}, context_id="k2")
L.on_state("light.k", st("off", {"id": "k2"}))
check("Home Mind changing its own mind is not an override of the earlier one",
      L.get(i7)["override"] is None and L.get(i8)["seen"] == "match")
clk.t += 700
L.on_state("light.k", st("on", {"id": "q", "user_id": "u1"}), st("off"))
check("after the window a change is not an override", L.get(i8)["override"] is None)

# quiet period after a start
clk2 = Clock(5_000_000.0)
L2 = DecisionLog(os.path.join(d, "quiet.db"), clock=clk2, quiet_seconds=180)
j = L2.record("light.q", "on", None, "light.turn_on", {}, context_id="q1")
L2.on_state("light.q", st("on", {"id": "q1"}))
clk2.t += 30
L2.on_state("light.q", st("off", {"id": "x", "user_id": "u"}), st("on"))
check("quiet period: changes just after a start are not overrides", L2.get(j)["override"] is None)

# restart: what was awaited becomes unknown
k = L2.record("light.r", "on", None, "light.turn_on", {}, context_id="r1")
L2.db.close()
L3 = DecisionLog(os.path.join(d, "quiet.db"), clock=clk2)
check("restart: an awaited outcome is unknown (restart), never failure",
      L3.get(k)["seen"] == "unknown" and L3.get(k)["seen_by"] == "restart")
check("restart: the log reopens at its version without migrating again", L3.version == (2, 2))

# ---- device profiles ---------------------------------------------------------------------------------------------
clk3 = Clock(9_000_000.0)
L4 = DecisionLog(":memory:", clock=clk3, quiet_seconds=0)
for n in range(6):
    x = L4.record("cover.g", "open", None, "cover.open_cover", {}, context_id=f"g{n}")
    clk3.t += 150
    L4.on_state("cover.g", st("open", {"id": f"g{n}"}))
check("profile: a slow garage door's own delays lengthen its wait", L4.within("cover.g") > 200, L4.within("cover.g"))
check("profile: never past the cap", L4.within("cover.g") <= 300)
for n in range(12):
    x = L4.record("light.cloud", "on", None, "light.turn_on", {}, context_id=f"cl{n}")
    clk3.t += 60
    run(L4.sweep(lambda e: asyncio.sleep(0, result=st("unknown"))))
check("profile: mostly-unknown outcomes are not trusted for learning", L4.learnable("light.cloud") is False)
L4.set_learn("light.cloud", True)
check("profile: trust for learning can be set by hand", L4.learnable("light.cloud") is True)

# ---- stats ---------------------------------------------------------------------------------------------------------
s = L.stats()
check("stats: totals by outcome", s["total"]["match"] >= 3 and s["total"]["mismatch"] == 1 and
      s["total"]["refused"] == 1 and s["total"]["overrides"] == 1, s["total"])
check("stats: with an interval", s["total"]["match_90"] is not None)

# ---- retention and frozen sets ----------------------------------------------------------------------------------
n = L.freeze("v1")
check("freeze: cases kept under a name", n >= 1 and L.frozen("v1"))
clk.t += 200 * 86400
gone = L.prune()
left = {r["id"] for r in L.recent(100)}
check("prune: old decisions go, frozen ones stay", gone > 0 and set(L.frozen("v1")) <= left, (gone, left))

# ---- Home.act logs every decision ------------------------------------------------------------------------------
store = Store(os.path.join(d, "mind.db"))


class FakeHA:
    def __init__(self):
        self.calls = []

    async def state(self, eid):
        return {"state": "on"} if eid == "input_boolean.mind_control_enabled" else None

    async def call(self, dom, svc, data):
        self.calls.append((dom, svc, data))
        return 200


ha = FakeHA()
h = Home(store, ha, {"home_controls": ["light.*"]})
h.catalog = {"light.desk": {"name": "Desk light", "kind": "device", "domain": "light", "state": "off", "attrs": {}},
             "lock.front": {"name": "Front door", "kind": "device", "domain": "lock", "state": "locked", "attrs": {}}}
clk5 = Clock(20_000_000.0)
h.decisions = DecisionLog(":memory:", clock=clk5, quiet_seconds=0)


async def call_service(dom, svc, data):
    ha.calls.append((dom, svc, data))
    return 200, "ctx-9", [{"entity_id": "light.desk", "state": "on", "attributes": {}, "context": {"id": "ctx-9"}}]

h.call_service = call_service
res = run(h.act("light.desk", "on", source="voice", why={"request": "desk on", "route": "cloud", "model": "haiku"}))
dl = h.decisions.recent(5)
check("act: logged with the context id and checked at once from the answer",
      res["context_id"] == "ctx-9" and dl[0]["context_id"] == "ctx-9" and dl[0]["seen"] == "match", dl[0])
check("act: the answer keeps its shape (no private keys)", "_states" not in res and "_prior" not in res)
check("act: who and how", dl[0]["request"] == "desk on" and dl[0]["model"] == "haiku" and dl[0]["channel"] == "voice")
try:
    run(h.act("lock.front", "unlock", source="voice"))
except PermissionError:
    pass
check("act: a refusal is logged with its reason", h.decisions.recent(1)[0]["status"] == "refused" and
      "control list" in h.decisions.recent(1)[0]["reason"])
h.call_service = None
h.catalog["light.desk"]["state"] = "on"
run(h.act("light.desk", "toggle", source="api"))
check("act: without call_service it still works (no context), toggle expected from the state before",
      h.decisions.recent(1)[0]["expected"] == {"state_in": ["off"]} and h.decisions.recent(1)[0]["context_id"] is None)
h.decisions = None
run(h.act("light.desk", "off"))
check("act: without a log nothing changes", ha.calls[-1][1] == "turn_off")

# ---- the automation engine logs the calls its runs make ----------------------------------------------------------
eng = A.Engine(store, types.SimpleNamespace(token="t", session=None), d)
eng.decisions = DecisionLog(":memory:", clock=Clock(30_000_000.0), quiet_seconds=0)
eng.autos = {"automation.bed": {"name": "Bedtime", "config": {}, "ha_state": "off"}}
eng.states = {"light.a": st("on"), "light.b": st("on")}


async def post(path, data):
    return [{"entity_id": "light.a", "state": "off", "attributes": {}, "context": {"id": "e1"}}]

eng._post = post
A.RUNNING.set(("automation.bed", "t0"))
run(eng.act({"action": "light.turn_off", "target": {"entity_id": ["light.a", "light.b"]}}, {}, []))
rows = eng.decisions.recent(5)
check("engine: one decision per targeted entity, channel automation, source the automation",
      len(rows) == 2 and {r["entity_id"] for r in rows} == {"light.a", "light.b"} and
      all(r["channel"] == "automation" and r["source"] == "automation.bed" for r in rows), rows)
check("engine: checked from the answer where it changed", {r["entity_id"]: r["seen"] for r in rows} ==
      {"light.a": "match", "light.b": None})
eng.state_listeners.append(eng.decisions.on_state)
eng.on_state({"entity_id": "light.b", "old_state": st("on"), "new_state": st("off", {"id": "e1"})})
check("engine: its event stream feeds the outcome checks",
      {r["entity_id"]: r["seen"] for r in eng.decisions.recent(5)}["light.b"] == "match")

# ---- replay ---------------------------------------------------------------------------------------------------------
clk6 = Clock(40_000_000.0)
R = DecisionLog(":memory:", clock=clk6, quiet_seconds=0)
for n, (req, eid, act, val) in enumerate([("desk light on", "light.desk", "on", None),
                                          ("fan to 40", "fan.f", "speed", 40),
                                          ("bedroom off", "light.bed", "off", None)]):
    x = R.record(eid, act, val, {"on": "light.turn_on", "off": "light.turn_off", "speed": "fan.set_percentage"}[act],
                 {"percentage": val} if val else {}, context_id=f"r{n}", request=req, route="cloud")
    R.on_state(eid, st({"on": "on", "off": "off", "speed": "on"}[act], {"id": f"r{n}"}, percentage=40))
clk6.t += 30
R.on_state("light.bed", st("on", {"id": "w", "user_id": "me"}), st("off"))   # the person undid "bedroom off"
y = R.record("light.dead", "on", None, "light.turn_on", {}, context_id="dd", request="dead light on")
clk6.t += 60
run(R.sweep(lambda e: asyncio.sleep(0, result=st("off"))))
cases = R.cases()
check("cases: match, override and device kinds",
      sorted(c["kind"] for c in cases) == ["device", "match", "match", "override"], [c["kind"] for c in cases])

PLANS = {"desk light on": {"entity_id": "light.desk", "action": "on"},
         "fan to 40": {"entity_id": "fan.f", "action": "speed", "value": "42"},
         "bedroom off": {"entity_id": "light.bed", "action": "off"},
         "dead light on": {"entity_id": "light.dead", "action": "on"}}


async def planner(text):
    return PLANS[text]

rep = run(replay.replay(R, planner))
check("replay: agrees on what worked (40 vs 42 is close enough)", rep["match"] == 2 and rep["agree"] == 2, rep)
check("replay: repeating an overridden action is caught", rep["override"] == 1 and rep["repeats"] == 1)
check("replay: device failures aren't scored", rep["device"] == 1)
check("replay: with an interval", rep["agree_90"] is not None)


async def broken(text):
    if text == "fan to 40":
        raise RuntimeError("model down")
    return {"entity_id": "light.other", "action": "on"}

rep2 = run(replay.replay(R, broken))
check("replay: a failing case is reported, not fatal", rep2["errors"] == 1 and rep2["agree"] == 0)
check("compare: no clear difference with few cases", replay.compare(rep2, rep) in ("worse", "no clear difference"))
check("same_action: on vs brightness is a different action",
      not replay.same_action({"entity_id": "l", "action": "brightness", "value": 30}, {"entity_id": "l", "action": "on"}))

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
