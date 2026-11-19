"""Learned records, house requests (phase 7): templates learned from what worked, earning trust in shadow, promoted
only through the nightly replay gate, then acting as reflexes without a model call. No Home Assistant or model.

  python tests/test_learned.py
"""
import asyncio
import json
import os
import random
import sys
import tempfile
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
from homemind import learned as L, main as M  # noqa: E402
from homemind.decisions import DecisionLog  # noqa: E402
from homemind.home import Home  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []
LOOP = asyncio.new_event_loop()
DAY = 86400


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(c):
    return LOOP.run_until_complete(c)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeHA:
    def __init__(self):
        self.calls = []

    async def state(self, eid):
        return {"state": "on"} if eid == "input_boolean.mind_control_enabled" else None

    async def call(self, dom, svc, data):
        self.calls.append((dom, svc, data))
        return 200


class Rng:
    def __init__(self, v):
        self.v = v

    def random(self):
        return self.v


def store():
    return Store(os.path.join(tempfile.mkdtemp(), "m.db"))


def home():
    h = Home(store(), FakeHA(), {"home_controls": ["light.*", "fan.*", "lock.*"]})
    h.catalog = {"light.desk": {"name": "desk lamp", "kind": "device", "domain": "light", "state": "off", "attrs": {}},
                 "light.hall": {"name": "hall light", "kind": "device", "domain": "light", "state": "off",
                                "attrs": {}},
                 "fan.bed": {"name": "bedroom fan", "kind": "device", "domain": "fan", "state": "off", "attrs": {}},
                 "lock.front": {"name": "front door", "kind": "device", "domain": "lock", "state": "locked",
                                "attrs": {}}}
    return h


def decide(dl, eid, action, request, seen="match", override=None, ago=2000, records=None, value=None):
    """One settled decision, as the log would hold it after its check."""
    did = dl.record(eid, action, value, f"{eid.split('.')[0]}.turn_{action}", {"entity_id": eid},
                    context_id=f"c{random.random()}", request=request, records=records)
    with dl.lock:
        dl.db.execute("UPDATE decisions SET seen=?, override=?, ts=? WHERE id=?",
                      (seen, json.dumps(override) if override else None, dl.clock() - ago, did))
        dl.db.commit()
    return did


# ---- shapes -------------------------------------------------------------------------------------------------------
b, n, neg = L.shape("dim the desk lamp to 30%")
check("shape: the number comes out as a slot", "{n}" in b and n == ["30"] and not neg, (b, n))
check("shape: a 'don't' is seen", L.shape("don't turn on the desk lamp")[2])
check("shape: a curly apostrophe too", L.shape("don’t turn on the desk lamp")[2])
check("slot: brightness takes a number, on doesn't",
      L.slot_of({"action": "brightness"}) == "n" and L.slot_of({"action": "on"}) is None)
check("trust: the prior is sceptical (low end well under the bar)", L.lower_bound(L.PRIOR_A, L.PRIOR_B) < 0.2)

# ---- step 16: what a template may never be ------------------------------------------------------------------------
h = home()
lr = L.Learned(h.store, h, clock=Clock(1e6))
check("check: an open light action is allowed", lr.check({"entity_id": "light.desk", "action": "on"}) is None)
check("check: a lock never", lr.check({"entity_id": "lock.front", "action": "off"}) is not None)
check("check: an action off the menu never", "reflex" in (lr.check({"entity_id": "light.desk", "action": "unlock"})
                                                         or ""))
check("check: not on the control list", lr.check({"entity_id": "switch.kettle", "action": "on"})
      == "not on the control list")
check("check: not a device", lr.check({"entity_id": "rm -rf", "action": "on"}) == "not a device")
h.set_ladder("light.hall", "ask")
check("check: a device set to 'ask me first' needs a yes: never a template",
      lr.check({"entity_id": "light.hall", "action": "on"}) is not None,
      lr.check({"entity_id": "light.hall", "action": "on"}))

# ---- learning from the log ----------------------------------------------------------------------------------------
clk = Clock(1e6)
h = home()
lr = L.Learned(h.store, h, clock=clk)
dl = DecisionLog(":memory:", clock=clk, quiet_seconds=0)
decide(dl, "light.desk", "on", "turn on the desk lamp")
decide(dl, "lock.front", "off", "unlock the front door")
decide(dl, "light.hall", "on", "hall light on", seen="mismatch")          # the device didn't respond
decide(dl, "fan.bed", "on", "bedroom fan on", override={"at": clk() - 1900})  # undone
decide(dl, "light.hall", "off", "hall light off", seen="unknown")
decide(dl, "light.desk", "off", "desk lamp off", ago=60)                  # not settled yet
got = lr.learn_from(dl)
ts = lr.all()
check("learn: one shadow template, from the one request that worked and stood",
      got["created"] == 1 and len(ts) == 1 and ts[0]["entity_id"] == "light.desk" and ts[0]["status"] == "shadow",
      (got, [(t["entity_id"], t["status"]) for t in ts]))
check("learn: never a template for a lock", not any(t["entity_id"] == "lock.front" for t in ts))
check("learn: not from an undone or unchecked request",
      not any(t["entity_id"] in ("fan.bed", "light.hall") for t in ts))
check("learn: a shadow template never acts", run(lr.reflex("turn on the desk lamp")) is None)
clk.t += 2000
got = lr.learn_from(dl)
check("learn: the cursor waits for unsettled requests (a later pass picks it up)",
      any(t["entity_id"] == "light.desk" and t["action"] == "off" for t in lr.all()), got)
check("learn: nothing is learned twice", lr.learn_from(dl)["created"] == 0)
decide(dl, "light.desk", "on", "switch on the desk lamp")
lr.learn_from(dl)
t = [t for t in lr.all() if t["entity_id"] == "light.desk" and t["action"] == "on"]
check("learn: another wording joins the same template",
      len(t) == 1 and "switch on the desk lamp" in t[0]["examples"], [x["examples"] for x in t])
decide(dl, "light.desk", "on", "don't turn on the desk lamp")
before = len(lr.all())
lr.learn_from(dl)
check("learn: a request with 'not' teaches nothing", len(lr.all()) == before)

# ---- evidence: shadow -> provisional, or dropped ------------------------------------------------------------------
desk_on = t[0]["id"]
for _ in range(9):
    decide(dl, "light.desk", "on", "turn on the desk lamp")
lr.learn_from(dl)
r = lr.get(desk_on)
check("evidence: 10 right outcomes aren't enough with the sceptical prior", r["status"] == "shadow",
      lr.describe(r))
for _ in range(13):
    decide(dl, "light.desk", "on", "turn on the desk lamp")
lr.learn_from(dl)
r = lr.get(desk_on)
check("evidence: 30 right, none wrong, still isn't enough", r["status"] == "shadow", lr.describe(r))
for _ in range(25):
    decide(dl, "light.desk", "on", "turn on the desk lamp")
lr.learn_from(dl)
r = lr.get(desk_on)
check("evidence: enough right outcomes make it provisional (not trusted: that's the nightly gate's)",
      r["status"] == "provisional", lr.describe(r))

h2 = home()
lr2 = L.Learned(h2.store, h2, clock=clk)
rid = lr2._new("light.hall", "on", None, "hall light", "test")
for _ in range(12):
    lr2.evidence(lr2.get(rid), False)
check("evidence: wrong more often than right is dropped", lr2.get(rid)["status"] == "dropped")
rid2 = lr2._new("light.desk", "on", None, "desk lamp on", "test")
for _ in range(6):
    lr2.evidence(lr2.get(rid2), True)
a0, b0 = lr2.trust(lr2.get(rid2))
clk.t += 90 * DAY
a1, b1 = lr2.trust(lr2.get(rid2))
check("fading: 90 days unused halves the evidence above the prior",
      abs((a1 - L.PRIOR_A) - (a0 - L.PRIOR_A) / 2) < 1e-6, (a0, a1))
clk.t -= 90 * DAY

# ---- the nightly gate ---------------------------------------------------------------------------------------------


async def better(inc):
    return 0.95 if inc else 0.9


async def worse(inc):
    return 0.8 if inc else 0.9


async def none_yet(inc):
    return None
p = run(lr.promote(none_yet))
check("gate: no scorecard yet: nothing promoted, it waits", p["promoted"] == [] and desk_on in p.get("waiting", []), p)
p = run(lr.promote(worse))
check("gate: the replay got worse: back to shadow", p["undone"] == [desk_on] and
      lr.get(desk_on)["status"] == "shadow", p)
lr.set_status(desk_on, "provisional")
p = run(lr.promote(better))
r = lr.get(desk_on)
check("gate: the replay held: trusted, a new version", p["promoted"] == [desk_on] and r["status"] == "trusted" and
      r["version"] == 2, (p, r["status"], r["version"]))

# ---- reflexes -----------------------------------------------------------------------------------------------------
lr.rng = Rng(0.9)
m = run(lr.reflex("turn on the desk lamp"))
check("reflex: trusted, named, close: a plan with no model", m and m["plan"]["entity_id"] == "light.desk" and
      m["plan"]["action"] == "on" and m["plan"]["by"] == "reflex" and
      m["plan"]["records"] == [{"id": desk_on, "version": 2, "status": "trusted"}], m)
check("reflex: never with a 'not'", run(lr.reflex("do not turn on the desk lamp")) is None)
check("reflex: not with the device unnamed", run(lr.reflex("turn on the lamp")) is None)
check("reflex: not for a loose match", run(lr.reflex("desk lamp")) is None)
check("reflex: not when a number is in it (that's another template)", run(lr.reflex("turn on the desk lamp 40"))
      is None)
lr.rng = Rng(0.01)
check("reflex: about 1 in 20 is an audit", run(lr.reflex("turn on the desk lamp")).get("audit"))
lr.rng = Rng(0.9)
# a close rival that does something else takes the margin away
rv = lr._new("light.desk", "off", None, "turn off the desk lamp", "test")
lr.set_status(rv, "trusted")
mm = run(lr.match("turn the desk lamp"))
check("reflex: no clear margin over another template: no reflex", mm and mm["margin"] < L.REFLEX_MARGIN and
      run(lr.reflex("turn the desk lamp")) is None, mm and (mm["sim"], mm["margin"]))
lr.set_status(rv, "dropped")

# slots
dim = lr._new("light.desk", "brightness", "n", "dim the desk lamp to {n}", "test")
lr.set_status(dim, "trusted")
m = run(lr.reflex("dim the desk lamp to 30%"))
check("reflex: the number goes in its slot", m and m["plan"]["action"] == "brightness" and
      m["plan"]["value"] == "30", m and m["plan"])

# trial
for ok in (True, False, True, False):
    st = lr.evidence(lr.get(desk_on), ok)
check("trial: two failures in its last 20: back to shadow", st == "shadow" and
      "back on trial" in (lr.get(desk_on)["note"] or ""), (st, lr.get(desk_on)["note"]))
lr.set_status(desk_on, "trusted")
lr.store.db.execute("UPDATE learned SET recent='[]' WHERE id=?", (desk_on,))

# a template that acted is judged by its own outcome
did = decide(dl, "light.desk", "on", "turn on the desk lamp", override={"at": clk() - 1500},
             records=[{"id": desk_on, "version": 2, "status": "trusted"}])
lr.learn_from(dl)
check("evidence: a reflex that was undone counts against it", lr.get(desk_on)["recent"][-1:] == [False],
      lr.get(desk_on)["recent"])

# retire
h.catalog.pop("fan.bed")
fb = lr._new("fan.bed", "on", None, "bedroom fan on", "test")
check("retire: a template for a device that's gone", lr.retire_missing() == 1 and lr.get(fb)["status"] == "retired")

# ---- meaning (embeddings) never acts alone ------------------------------------------------------------------------


async def emb(texts):
    return [[1.0, 0.0] for _ in texts]
h3 = home()
lr3 = L.Learned(h3.store, h3, clock=clk, rng=Rng(0.9), embed=emb)
e = lr3._new("light.desk", "on", None, "turn on the desk lamp", "test")
lr3.set_status(e, "trusted")
run(lr3.embed_missing())
check("embed: a vector is kept per template", lr3.get(e)["vector"] == [1.0, 0.0])
m = run(lr3.match("illuminate my workspace"))
check("embed: a same-meaning vector alone stays under the reflex line (and the device isn't named)",
      m and m["sim"] <= 0.75 and run(lr3.reflex("illuminate my workspace")) is None, m and m["sim"])

# ---- in do(): the reflex acts with no model call; the audit asks the model ----------------------------------------
asked = []


async def plan_action(model, text, things):
    asked.append(text)
    return {"entity_id": "light.desk", "action": "on"}
M.plan_action = plan_action


def make_app(learned_rng):
    hh = home()
    a = types.SimpleNamespace(opts={"rule_planner": False}, home=hh, PENDING_SECONDS=30,   # templates, not the rules
                              models=types.SimpleNamespace(models={"haiku": 1}, get=lambda n: n))
    a.decisions = DecisionLog(":memory:", quiet_seconds=0)
    hh.decisions = a.decisions
    a.learned = L.Learned(hh.store, hh, rng=Rng(learned_rng))
    t_ = a.learned._new("light.desk", "on", None, "turn on the desk lamp", "test")
    a.learned.set_status(t_, "trusted")

    async def no_pending(*_a, **_k):
        return None
    a.pending_reply = no_pending
    for n_ in ("do", "_run_plan", "plan_only", "_verb", "_run_steps", "_said", "_summed", "room_for"):
        if hasattr(M.App, n_):
            setattr(a, n_, getattr(M.App, n_).__get__(a))
    return a, t_


A, tid = make_app(0.9)
r = run(A.do("turn on the desk lamp", source="voice"))
dec = A.decisions.recent(1)[0]
check("do: a reflex acts with no model call", r.get("done") and asked == [] and
      A.home.ha.calls[-1][:2] == ("light", "turn_on"), (r, asked))
check("do: the decision says it was a reflex, and which record", dec["route"] == "reflex" and
      (dec["records"] or [{}])[0].get("id") == tid, (dec["route"], dec["records"]))
A, tid = make_app(0.01)
r = run(A.do("turn on the desk lamp", source="voice"))
check("do: an audit asks the model, acts on its plan, and the template gets the evidence",
      asked == ["turn on the desk lamp"] and r.get("done") and A.learned.get(tid)["recent"] == [True],
      (asked, r, A.learned.get(tid)["recent"]))
asked.clear()
A, tid = make_app(0.9)
A.home.set_ladder("light.desk", "ask")
r = run(A.do("turn on the desk lamp", source="voice", conversation_id="c1"))
check("do: a reflex still goes through every check (an 'ask me first' device asks)",
      r.get("confirm_needed") and not A.home.ha.calls, r)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
