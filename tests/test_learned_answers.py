"""Learned answers (phase 8): questions answered well before are answered again from the same note with no model,
only while that note is unchanged; trust earned as for house templates; the small-model tie-break. No model needed.

  python tests/test_learned_answers.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind import learned_answers as LA  # noqa: E402
from homemind.cases import Cases  # noqa: E402
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


class Rng:
    def __init__(self, v):
        self.v = v

    def random(self):
        return self.v


st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
gate = st.remember("The boiler was last serviced in March 2025.", title="Boiler service", key="boiler service")
bins = st.remember("Bins go out on Tuesday night.", title="Bins")
secret = st.remember("Safe combination 1234", title="Safe", meta={"sensitive": True})
mine = st.remember("Dana's dentist is Dr Lee.", title="Dentist", meta={"scope": "personal", "owner": "Dana"})
cases = Cases(st)
clk = Clock(time.time() + 10_000)
la = LA.LearnedAnswers(st, clock=clk, rng=Rng(0.9), cases=cases)


def ask(q, src, ans, model="haiku", ts=None, **kw):
    st.db.execute("INSERT INTO events(ts,type,item_id,data) VALUES(?,?,?,?)",
                  (ts if ts is not None else clk() - 2000, "ask", None,
                   json.dumps(dict(question=q, model=model, ids=[src], sources=[src] if src else [], answer=ans, **kw))))
    st.db.commit()


def gid(x):
    return x if isinstance(x, int) else x["id"]


G, B, S, D = gid(gate), gid(bins), gid(secret), gid(mine)

# ---- step 16 ------------------------------------------------------------------------------------------------------
check("check: an answer from the note is fine", la.check_answer(st.get(G), "It was serviced in March 2025.") is None)
check("check: a number not in the note never", "numbers not in the note" in
      (la.check_answer(st.get(G), "It was serviced in March 2024.") or ""))
check("check: a sealed note never", "sealed" in (la.check_answer(st.get(S), "Safe combination 1234") or ""))
check("check: 'I don't know' never", la.check_answer(st.get(G), "I don't know when the boiler was serviced.") is not None)

# ---- learning -----------------------------------------------------------------------------------------------------
ask("when was the boiler serviced?", G, "It was serviced in March 2025.")
ask("when do the bins go out?", B, "Tuesday night.", weak=True)            # weak: no evidence either way
ask("what's the safe combination?", S, "1234")                               # sealed
ask("when was the boiler serviced", G, "I don't know.")                             # don't know
ask("where are my keys?", None, "I don't know.")
got = la.learn_from()
ts = la.all()
check("learn: one shadow answer, from the clear single-note answer", got["created"] == 1 and len(ts) == 1 and
      ts[0]["item_id"] == G and ts[0]["status"] == "shadow", (got, ts))
check("learn: a shadow answer never says itself", run(la.reflex("when was the boiler serviced?")) is None)
ask("when was our boiler serviced?", G, "It was serviced in March 2025.")
la.learn_from()
r = la.all()[0]
check("learn: another wording joins it", len(la.all()) == 1 and len(r["examples"]) == 2, r["examples"])
# corrected: a "that's wrong" case right after
t0 = clk() - 2000
ask("when was the boiler serviced?", G, "It was serviced in March 2025.", ts=t0)
cases.add("when was the boiler serviced?", "October", "March")
st.db.execute("UPDATE cases SET ts=? WHERE question=?", (t0 + 30, "when was the boiler serviced?"))
st.db.commit()
before = la.get(r["id"])["trust_b"]
la.learn_from()
check("evidence: an answer corrected within 15 minutes counts against it", la.get(r["id"])["trust_b"] > before,
      (before, la.get(r["id"])["trust_b"]))
st.db.execute("DELETE FROM cases")
st.db.commit()

# ---- trust and the reflex -----------------------------------------------------------------------------------------
st.db.execute("UPDATE learned SET trust_a=60, trust_b=3, status='trusted', recent='[]' WHERE id=?", (r["id"],))
st.db.commit()
m = run(la.reflex("when was the boiler serviced?"))
check("reflex: trusted and close: the learned answer, with its note", m and m["answer"] == "It was serviced in March 2025." and
      m["item"]["id"] == G, m)
check("reflex: not for a loose match", run(la.reflex("boiler?")) is None)
check("reflex: never with a number in the question", run(la.reflex("when was boiler 2 serviced?")) is None)
check("reflex: never with a 'not'", run(la.reflex("when was the boiler not serviced?")) is None)
la.rng = Rng(0.01)
check("reflex: about 1 in 20 is an audit", run(la.reflex("when was the boiler serviced?")).get("audit"))
la.rng = Rng(0.9)

# privacy
pr = la._new_answer(st.get(D), "Dana's dentist is Dr Lee.", "who is dana's dentist", "test")
la.set_status(pr, "trusted")
VIEWER.set("Sam")
check("reflex: a personal note isn't said to someone else", run(la.reflex("who is dana's dentist")) is None)
VIEWER.set("Dana")
check("reflex: it is to its owner", (run(la.reflex("who is dana's dentist")) or {}).get("answer") ==
      "Dana's dentist is Dr Lee.")
VIEWER.set(None)

# never stale
newer = st.revise(G, body="The boiler was last serviced in October 2026.")
check("stale: a changed note: no reflex, and the answer goes back on trial",
      run(la.reflex("when was the boiler serviced?")) is None and la.get(r["id"])["status"] == "shadow",
      la.get(r["id"])["status"])
ask("when was the boiler serviced?", gid(newer) if newer else G, "It was serviced in October 2026.")
la.learn_from()
r2 = la.get(r["id"])
check("relearn: the next good answer from the new version updates it", r2["answer"] == "It was serviced in October 2026." and
      r2["item_id"] == (gid(newer) if newer else G), (r2["answer"], r2["item_id"]))

# ---- tie-break ----------------------------------------------------------------------------------------------------
a1 = la._new_answer(st.get(B), "Bins go out on Tuesday night.", "when do the bins go out", "test")
recyc = st.remember("Recycling goes out on Thursday night.", title="Recycling")
a2 = la._new_answer(st.get(gid(recyc)), "Recycling goes out on Thursday night.", "when do the recycling bins go out",
                    "test")
la.set_status(a1, "trusted")
la.set_status(a2, "trusted")
q = "when do the bins go out tonight"
mm = run(la.match(q))
check("tie: two close answers aren't a reflex on their own", mm["margin"] < LA.REFLEX_MARGIN and
      run(la.reflex(q)) is None, mm and (mm["sim"], mm["margin"]))


async def pick_bins(text, answers):
    return 0 if "Tuesday" in answers[0] else 1
m = run(la.reflex(q, tiebreak=pick_bins))
check("tie: the small model picks between them", m and m["answer"].startswith("Bins") and m.get("tiebreak"), m)
rate = la.tiebreak_rate()
check("tie: how often it was needed is counted", rate["asked"] >= 1 and rate["seen"] >= rate["asked"], rate)
check("tie: prompt and reply", LA.tie_pick("2") == 1 and LA.tie_pick("Answer: 1") == 0 and LA.tie_pick("0") is None
      and "Question:" in LA.tie_prompt("x", ["a", "b"]))

# ---- the nightly gate ---------------------------------------------------------------------------------------------
st.db.execute("UPDATE learned SET status='provisional' WHERE id=?", (a1,))
st.db.commit()
p = run(la.nightly())
check("gate: no corrections yet: nothing can get worse: promoted", a1 in p.get("promoted", []), p)
st.db.execute("UPDATE learned SET status='provisional' WHERE id=?", (a1,))
st.db.commit()
cases.add("when do the bins go out tonight", "Wednesday", "Tuesday")


async def worse(inc):
    return 0.0 if inc else 1.0
p = run(la.promote(worse))
check("gate: the replay got worse: back to shadow", p.get("undone") == [a1] and la.get(a1)["status"] == "shadow", p)

# retire
st.set_status(gid(recyc), "archived") if hasattr(st, "set_status") else st.db.execute(
    "UPDATE items SET status='archived' WHERE id=?", (gid(recyc),))
st.db.commit()
check("retire: an answer whose note is gone", la.retire_missing() >= 1 and la.get(a2)["status"] == "retired")

# ---- in Mind.ask: no model, and the audit ---------------------------------------------------------------------------
from homemind import mind as MI  # noqa: E402
mi = MI.Mind(tempfile.mkdtemp(), None)
n = mi.store.remember("The wifi password is on the fridge.", title="Wifi")
la2 = LA.LearnedAnswers(mi.store, rng=Rng(0.9), cases=Cases(mi.store))
x = la2._new_answer(mi.store.get(gid(n)), "It's on the fridge.", "where is the wifi password", "test")
la2.set_status(x, "trusted")
mi.learned_answers = la2
res = run(mi.ask("where is the wifi password?"))
check("ask: a trusted answer is said with no model, its note as the source", res["answer"] == "It's on the fridge." and
      res["model"] is None and res.get("reflex", {}).get("id") == x and res["hits"][0]["id"] == gid(n), res)
ev = mi.store.db.execute("SELECT data FROM events WHERE type='ask' ORDER BY id DESC LIMIT 1").fetchone()
check("ask: logged as a reflex (judged later by whether it was corrected)", json.loads(ev["data"]).get("reflex") == x)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
