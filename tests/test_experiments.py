"""Phase 14: experiments (only three kinds, never medicines, written plan, ABAB with random order inside pairs,
washout, crash episodes per block, exact randomisation test, a new medicine ends the pair, a safety stop) and gaps
turned into proposals (cheapest first; Opus only with the schema and made-up data, only when due).

  python tests/test_experiments.py
"""
import json
import os
import random
import sys
import tempfile
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind import experiments as X, proposals as P  # noqa: E402
from homemind.day import Day  # noqa: E402
from homemind.rooms import Surprises  # noqa: E402
from homemind.store import Store  # noqa: E402
import asyncio  # noqa: E402

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


# ---- crash episodes (open decision 9's starting value) ------------------------------------------------------------
days = [("d1", "good"), ("d2", "low"), ("d3", "rough"), ("d4", "rough"), ("d5", "good"), ("d6", "rough"),
        ("d7", "good"), ("d8", "low"), ("d9", "unknown"), ("d10", "rough"), ("d11", "good"), ("d12", "good"),
        ("d13", "rough")]
check("crash: starts after 2 days that weren't rough; one episode however long; unknown breaks the run",
      X.crash_starts(days) == ["d3", "d13"], X.crash_starts(days))

# ---- the exact test -----------------------------------------------------------------------------------------------
check("exact test: all differences the same way is as extreme as it gets (1/8 for 3... two-sided 2/16 for 4)",
      abs(X.exact_p([1, 1, 1, 1]) - 2 / 16) < 1e-9, X.exact_p([1, 1, 1, 1]))
check("exact test: no difference: p = 1", X.exact_p([0, 0, 0, 0]) == 1.0)
check("detectable effect: smaller with more pairs", X.detectable(1.0, 8) < X.detectable(1.0, 4))

# ---- planning -----------------------------------------------------------------------------------------------------
st = Store(os.path.join(tempfile.mkdtemp(), "mind.db"))
start_day = date(2026, 11, 18)
clk = Clock(datetime.combine(start_day, datetime.min.time()).replace(hour=12).timestamp())
day = Day(st, clock=clk)


class Pat:
    def __init__(self):
        self.rs = {}

    def rows(self, n):
        return [dict(self.rs[d], day=d) for d in sorted(self.rs)]


pat = Pat()
for i in range(30):
    pat.rs[(start_day - timedelta(days=30 - i)).isoformat()] = {"fog": 2 + (i % 3)}
ex = X.Experiments(st, day, pat, None, clock=clk, rng=random.Random(1))
for bad, why in ((("Does magnesium help?", "take magnesium at night", "sleep_timing"), "supplement"),
                 (("Fewer pills?", "skip a dose", "reduce_load"), "medicine"),
                 (("Exercise more?", "run every day", "more_load"), "kind")):
    try:
        ex.plan(*bad)
        check(f"plan: refused ({why})", False)
    except ValueError:
        check(f"plan: refused ({why})", True)
try:
    ex.plan("q", "lie down after lunch", "reduce_load", pairs=3)
    check("plan: at least 4 pairs", False)
except ValueError:
    check("plan: at least 4 pairs", True)
plan = ex.plan("Does lying down after lunch help my fog?", "lie down for 20 minutes after lunch", "reduce_load")
check("plan: starts the next Monday (the calendar, not how you feel)", plan["start"] == "2026-11-23", plan["start"])
pairs = {}
for b in plan["blocks"]:
    pairs.setdefault(b["pair"], []).append(b["arm"])
check("plan: 6 pairs of 7-day blocks by default, each pair A and B in a random order",
      len(plan["blocks"]) == 12 and all(sorted(v) == ["A", "B"] for v in pairs.values()) and
      len({tuple(v) for v in pairs.values()}) == 2, pairs)
check("plan: says the smallest effect it could see, and the crash definition, before starting",
      plan["smallest_effect"] and "rough" in plan["crash_definition"], plan)
p4 = ex.plan("q", "lie down after lunch", "reduce_load", pairs=4)
check("plan: 4 pairs allowed, but it says it can't reach the 5% line", p4["power_note"] and p4["smallest_p"] == 0.125)
asyncio.run(ex.start(plan))
check("one at a time", asyncio.run(ex.start(plan)).get("error"))

# ---- running: outcomes, crashes, a new medicine, the analysis ------------------------------------------------------
blocks = plan["blocks"]
allx = {}
for b in blocks:
    s = date.fromisoformat(b["start"])
    for i in range(7):
        d = (s + timedelta(days=i)).isoformat()
        pat.rs[d] = {"fog": 1.0 if b["arm"] == "B" else 3.0}
        allx[d] = {"_": {"state": "good", "at": 0, "how": "said"}}
st.kv_set(day.KV, allx)
day._all = lambda: allx          # the real one keeps two weeks; the experiment reads the whole span
end = date.fromisoformat(blocks[-1]["end"])
res = ex.analyse(end)
check("analysis: every pair done, B lower by 2, an exact p", res["pairs_complete"] == 6 and res["difference"] == -2.0
      and res["p"] == round(2 / 64, 3) and "lower" in res["text"], res)
check("analysis: washout days don't count", all(b["days"] == 5 for b in res["blocks"]), [b["days"] for b in res["blocks"]])
b2 = next(b for b in blocks if b["pair"] == 2)
ex.medicine_changed("sertraline", date.fromisoformat(b2["start"]) + timedelta(days=1))
res = ex.analyse(end)
check("a new medicine ends that pair, flagged", res["pairs_complete"] == 5 and res["flags"], res["flags"])
# a safety stop: two crash episodes more with the change in one pair
bb = next(b for b in blocks if b["pair"] == 0 and b["arm"] == "B")
s = date.fromisoformat(bb["start"])
for i, st_ in enumerate(["good", "good", "good", "rough", "good", "good", "rough"]):
    allx[(s + timedelta(days=i)).isoformat()] = {"_": {"state": st_, "at": 0, "how": "said"}}
res = ex.analyse(end)
check("safety: 2 more crash episodes with the change stops it", res["safety_stop"] and res["text"].startswith(
    "Stopped for safety"), res["text"])

# ---- proposals ----------------------------------------------------------------------------------------------------
st2 = Store(os.path.join(tempfile.mkdtemp(), "mind.db"))
sp = Surprises(st2)
for i, w in enumerate(["how much did I spend on groceries", "what's my bank balance", "how much was the gas bill",
                       "make me a coffee", "what's Dana's birthday"]):
    sp.add("dont_know" if i != 3 else "cant_do", w, f"x{i}")
pr = P.Proposals(st2, sp)
check("proposals: none until a topic is seen 3 times", [g["topic"] for g in pr.gaps()] == ["finance"], pr.gaps())
ps = pr.build()
fin = ps[0]
check("proposals: cheapest first, Firefly III before any new module",
      [s_["fix"] for s_ in fin["steps"]] == ["app", "module"] and "Firefly III" in fin["steps"][0]["what"], fin)
req = pr.opus_request(fin)
text = json.dumps(req)
check("proposals: Opus sees the schema and made-up data only, never your notes",
      "Boiler" in text and "groceries" not in text and "bank balance" not in text and "Batch" not in text, text[:200])
check("proposals: batch only when a gap is queued or a gate failed", pr.batch() is not None)
pr.set_status("finance", "dismissed")
check("proposals: nothing queued: no batch", pr.batch() is None and pr.batch(gate_failed=True) is None)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
