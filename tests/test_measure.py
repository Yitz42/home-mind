"""Phase 4 of the design (0.27): measurement. Gates on 90% intervals, labels as truth, the labelling queue with
repeats and the gold set, the scorecard (three sections, never averaged) and its weekly trend against v1, the shadow
baseline, burden per day, and response times by front end. No Home Assistant or model needed.

  python tests/test_measure.py
"""
import asyncio
import json
import os
import random
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
    sys.modules["aiohttp"] = m
from homemind import measure as ME  # noqa: E402
from homemind.decisions import DecisionLog  # noqa: E402
from homemind.ha import HA  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []
LOOP = asyncio.new_event_loop()


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


# ---- gates and kappa -----------------------------------------------------------------------------------------------
g = ME.gate(19, 20, 0.9)
check("gate: 19/20 against 0.9 is unclear (the interval straddles the bar)", g["verdict"] == "unclear" and
      g["lo"] < 0.9 < g["hi"] and g["width"] > 0.1, g)
check("gate: 200/200 passes", ME.gate(200, 200, 0.9)["verdict"] == "pass")
check("gate: 10/40 fails", ME.gate(10, 40, 0.9)["verdict"] == "fail")
check("gate: under 20 cases is not enough, even if perfect", ME.gate(15, 15, 0.9)["verdict"] == "not enough cases")
check("gate: nothing", ME.gate(0, 0, 0.9)["verdict"] == "not enough cases")
check("kappa: perfect, chance", ME.kappa([("right", "right"), ("wrong", "wrong")]) == 1.0 and
      ME.kappa([("right", "wrong"), ("wrong", "right")]) < 0)
check("p95", ME.p95(list(range(1, 101))) == 95 and ME.p95([]) is None)

# ---- a log with real-looking decisions -------------------------------------------------------------------------
clk = Clock(time.time() - 3 * 86400)
L = DecisionLog(":memory:", clock=clk, quiet_seconds=0)
st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
app = types.SimpleNamespace(decisions=L, mind=types.SimpleNamespace(store=st), models=types.SimpleNamespace(models={}),
                            opts={}, _bg={})
M = ME.Measure(app, rate=0, clock=clk, rng=random.Random(1))


def st_(state, cid):
    return {"state": state, "attributes": {}, "context": {"id": cid}}


def act(req, eid, action, outcome="match", channel="voice", route="cloud", model="haiku", **kw):
    """A decision with its outcome: match, override (a person undid it), mismatch, or none."""
    svc = {"on": "light.turn_on", "off": "light.turn_off"}[action]
    i = L.record(eid, action, None, svc, {}, context_id=f"c{clk.t}", request=req, channel=channel, route=route,
                 model=model, prior="off" if action == "on" else "on", **kw)
    want = "on" if action == "on" else "off"
    if outcome in ("match", "override"):
        L.on_state(eid, st_(want, f"c{clk.t}"))
    if outcome == "override":
        clk.t += 30
        L.on_state(eid, st_("off" if want == "on" else "on", "zz") | {"context": {"id": "zz", "user_id": "u"}},
                   st_(want, "x"))
    if outcome == "mismatch":
        clk.t += 100
        run(L.sweep(lambda e: asyncio.sleep(0, result=st_("off" if want == "on" else "on", "q"))))
    clk.t += 60
    return i


ids = [act(f"lamp {n} on", f"light.l{n}", "on", channel="voice" if n % 2 else "telegram") for n in range(22)]
over = act("bedroom off", "light.bed", "off", "override")
mis = act("porch on", "light.porch", "on", "mismatch")
low = act("desk on", "light.desk", "on", confidence=0.4)
ref = L.record("lock.front", "unlock", status="refused", reason="needs the phone", request="unlock the door")

# ---- labels and truth ------------------------------------------------------------------------------------------
try:
    M.label(ids[0], "maybe")
    check("label: only right, wrong, unsure", False)
except ValueError:
    check("label: only right, wrong, unsure", True)
try:
    M.label(999999, "right")
    check("label: an unknown decision is refused", False)
except ValueError:
    check("label: an unknown decision is refused", True)
r1 = M.label(ids[0], "right", labeller="Dana")
r2 = M.label(ids[0], "right", labeller="Dana")
check("label: a second label is a repeat", not r1["repeat"] and r2["repeat"])
check("label: a labelled decision is kept past retention", L.get(ids[0])["keep"] == 1)
M.label(ids[1], "wrong", correct={"entity_id": "light.l1b", "action": "on"})
M.label(ids[2], "wrong")
M.label(ids[3], "unsure")
cs = {c["id"]: c for c in M.cases()}
check("truth: right label -> match", cs[ids[0]]["kind"] == "match")
check("truth: wrong with the right action -> that action is the answer", cs[ids[1]]["kind"] == "match" and
      cs[ids[1]]["entity_id"] == "light.l1b" and cs[ids[1]].get("corrected"))
check("truth: wrong without it -> must not be repeated", cs[ids[2]]["kind"] == "override")
check("truth: unsure is left out", ids[3] not in cs)
check("truth: an override -> must not be repeated; a device mismatch says nothing about the plan",
      cs[over]["kind"] == "override" and cs[mis]["kind"] == "device")
M.label(ids[1], "right")          # a repeat that disagrees
ag = M.agreement()
check("agreement: repeats against first labels", ag["pairs"] == 2 and ag["agree"] == 1 and ag["rate"] == 0.5, ag)

# ---- the labelling queue ---------------------------------------------------------------------------------------
q = M.queue(size=30)
whys = {x["why"] for x in q}
check("queue: strata first", {"override", "mismatch", "low confidence", "refused or held"} <= whys, whys)
check("queue: no decision twice, nothing already labelled (except repeats)",
      len({x["id"] for x in q}) == len(q) and
      not any(x["id"] in (ids[0], ids[1], ids[2], ids[3]) for x in q if x["why"] != "repeat (agreement)"))
check("queue: about 15 minutes", len(q) <= 30)
clk.t += 4 * 86400
q = M.queue(size=30)
check("queue: some repeats of older labels, for agreement", any(x["why"] == "repeat (agreement)" for x in q),
      {x["why"] for x in q})

# ---- the gold set ----------------------------------------------------------------------------------------------
try:
    M.make_gold()
    check("gold: needs 30 labelled cases", False)
except ValueError:
    check("gold: needs 30 labelled cases", True)
L2 = DecisionLog(":memory:", clock=Clock(time.time()), quiet_seconds=0)
M2 = ME.Measure(types.SimpleNamespace(decisions=L2), rate=0, clock=L2.clock, rng=random.Random(2))
gids = [L2.record("light.x", "on", None, "light.turn_on", {}, request=f"x{n}") for n in range(35)]
for gid in gids:
    M2.label(gid, "right")
check("gold: 30 kept for good", M2.make_gold() == {"gold": 30, "made": True} and len(L2.frozen("gold")) == 30)
check("gold: made once", M2.make_gold()["made"] is False)
L2.clock.t += 91 * 86400
check("gold: back in the queue after a quarter", sum(x["why"] == "gold (quarterly)" for x in M2.queue(30)) >= 1)

# ---- the scorecard -----------------------------------------------------------------------------------------------
from homemind.cases import Cases  # noqa: E402
st.remember(text="The gate code is 5512.", key="gate|code")
Cases(st).add("What is the gate code?", "5512", "4417")
app.safety_result = {"checks": [{"name": "a", "ok": True}, {"name": "b", "ok": True}]}


async def good_planner(text):
    n = text.split()[1] if text.startswith("lamp") else None
    if n == "2":
        return {"entity_id": "light.l2_other", "action": "on"}     # lamp 2 was labelled wrong: don't repeat it
    if n:
        return {"entity_id": f"light.l{n}" if n != "1" else "light.l1b", "action": "on"}
    if text == "desk on":
        return {"entity_id": "light.desk", "action": "on"}
    if text == "bedroom off":
        return {"entity_id": "light.lounge", "action": "off"}       # not the undone action
    return {"entity_id": None}

sc = run(M.scorecard(good_planner))
S = sc["sections"]
check("scorecard: v1 frozen from real cases on first run", len(L.frozen("v1")) >= 20)
check("scorecard: three sections, no average", set(S) == {"house", "memory", "safety"} and "average" not in sc and
      "total" not in sc)
check("scorecard: house scored against truth (labels, overrides)", S["house"]["n"] >= 20 and
      S["house"]["correct"] == S["house"]["n"], S["house"])
check("scorecard: house by front end", set(S["house"]["by_front_end"]) >= {"voice", "telegram"},
      S["house"].get("by_front_end"))
check("scorecard: memory from your corrections", S["memory"]["n"] == 1 and S["memory"]["correct"] == 1)
check("scorecard: safety from the daily suite, bar 100%", S["safety"]["bar"] == 1.0 and S["safety"]["n"] == 2)
check("scorecard: every section has an interval and a verdict", all("verdict" in v for v in S.values()))
check("scorecard: kept as a report", M.reports()[0]["sections"]["house"]["n"] == S["house"]["n"])


async def bad_planner(text):
    return {"entity_id": "light.wrong", "action": "on"}
notes = []


async def notify(t, msg):
    notes.append(t)
sc2 = run(M.weekly(bad_planner, notify))
check("weekly: clearly worse than v1's first run raises the alarm", sc2["trend"]["alarm"] and notes, sc2["trend"])
check("trend: not enough runs at first", ME.Measure(types.SimpleNamespace(decisions=DecisionLog(":memory:")),
                                                    rate=0).trend()["verdict"] == "not enough runs")

# ---- the shadow baseline ----------------------------------------------------------------------------------------
plans = {}


async def shadow_plan(text, model):
    return plans.get(text, {"entity_id": None})
app.shadow_plan = shadow_plan
app.models = types.SimpleNamespace(models={"haiku": 1, "fg": 1})
app.opts = {"control_model": "fg"}
M.rate = 1.0
check("shadow: Haiku shadows the local model, the local model shadows Haiku",
      M.shadow_model("fg") == "haiku" and M.shadow_model("haiku") == "fg")
plans["lamp 5 on"] = {"entity_id": "light.l5", "action": "on"}          # same as live, live right: tie
plans["lamp 6 on"] = {"entity_id": "light.other", "action": "on"}       # differs, live right: live wins
plans["bedroom off"] = {"entity_id": "light.lounge", "action": "off"}   # live undone, shadow differs: shadow wins


async def shadow_all():
    for req, did in (("lamp 5 on", ids[5]), ("lamp 6 on", ids[6]), ("bedroom off", over)):
        d = L.get(did)
        await M.maybe_shadow(req, {"entity_id": d["entity_id"], "action": d["action"], "by": "fg"}, did)
run(shadow_all())
rep = M.shadow_report(30)
p = rep["pairs"][0]
check("shadow: win, tie and loss judged by the outcome", p["pairs"] == 3 and p["ties"] == 1 and p["live_wins"] == 1
      and p["shadow_wins"] == 1, p)
check("shadow: with an interval", p["live_win_90"] is not None)
M.rate = 0
check("shadow: rate 0 never shadows", M.maybe_shadow("lamp 5 on", {"by": "fg"}) is None)
with L.lock:
    n_before = L.db.execute("SELECT COUNT(*) FROM decisions WHERE status='done'").fetchone()[0]
check("shadow: never acts (no new decisions)", n_before == len(ids) + 3)
app.usage = types.SimpleNamespace(rows=lambda days: [{"model": "haiku", "calls": 10, "cost": 0.02}])
cpc = M.cost_per_correct(30)
check("cost per correct answer from usage", cpc["haiku"]["per_call"] == 0.002 and cpc["haiku"]["per_correct"]
      is not None, cpc)

# ---- burden and timing ----------------------------------------------------------------------------------------
clk2 = Clock(time.time())
L3 = DecisionLog(":memory:", clock=clk2, quiet_seconds=0)
st3 = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
M3 = ME.Measure(types.SimpleNamespace(decisions=L3, mind=types.SimpleNamespace(store=st3)), rate=0, clock=clk2)
L3.count("asks", 3)
L3.count("requests", 10)
L3.count("abstains", 2)
a1 = L3.record("light.k", "on", None, "light.turn_on", {}, request="kitchen on", context_id="k1")
clk2.t += 120
a2 = L3.record("light.k", "on", None, "light.turn_on", {}, request="kitchen light on!", context_id="k2")
L3.on_state("light.k", st_("on", "k2"))
clk2.t += 30
L3.on_state("light.k", {"state": "off", "attributes": {}, "context": {"id": "p", "user_id": "u"}}, st_("on", "k2"))
nid = st3.remember(text="The bins go out Monday.")["id"]
st3.feedback(nid, False, "wrong")
b = M3.burden(1)[0]
check("burden: asks + corrections + redos (an undo, a repeat within 10 minutes)",
      b["asks"] == 3 and b["corrections"] == 1 and b["redos"] == 2 and b["burden"] == 6, b)
check("burden: abstain rate beside it", b["abstain_rate"] == 0.2)
for ms in range(100, 2100, 100):
    L3.timing("ask", ms, "voice", "fg", fallback=ms > 1800)
L3.timing("ask", 400, "telegram", "haiku")
tm = {(t["kind"], t["front_end"]): t for t in M3.timing(7)}
check("timing: p95 by front end with the voice target", tm[("ask", "voice")]["p95_ms"] == 1900 and
      tm[("ask", "voice")]["target_ms"] == 2500 and tm[("ask", "voice")]["fallback_rate"] == 0.1, tm)
check("timing: other front ends separately", tm[("ask", "telegram")]["n"] == 1)

ha = HA(None)
pushed = []
ha.on_push = pushed.append


async def call(dom, svc, data, approved=False):
    return 200
ha.call = call
run(ha.push("notify.p", {"message": "x"}))
run(ha.push("notify.p", {"message": "y"}, "safety"))
check("asks are counted as they go out (every tier reported)", pushed == ["routine", "safety"])

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
