"""The gaps after 0.18: lookups from outside (Jeeves, MCP) carry the worked-out lines and the profile; notes filed
before 0.19 get their structure read once; due chores and repeating things are in the morning summary; secret
values right after the secret word are hidden; a model can fill in what the patterns miss, and only what it copies
from the note is kept.

  python tests/test_holes.py
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    sys.modules["aiohttp"] = types.ModuleType("aiohttp")
from homemind import mind as M, privacy, structure as S  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.overnight import Overnight  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class Oracle:
    """Answers the structure question the way a good model would, for the paraphrase phrasings."""
    type, cfg = "fake", {}

    async def chat(self, system, messages, max_tokens=800, temperature=0.2):
        t = messages[-1]["content"]
        j = {}
        m = re.match(r"You'll find the (.+) inside the (.+)\.", t) or re.match(r"The (.+) now lives in the (.+)\.", t)
        if m:
            j["place"] = {"thing": m.group(1), "in": m.group(2)}
        m = re.match(r"Bin day for (.+) is (\w+)\.", t)
        if m:
            j["repeats"] = {"weekday": m.group(2).lower(), "every_weeks": 1}
        m = re.match(r"The (.+) should be (swapped) out every three months\.", t)
        if m:
            j["chore"] = {"verb": "swapped", "thing": m.group(1), "every_days": 90}
        m = re.match(r"Serial on the .+: (\w+)", t)
        if m:
            j["fields"] = {"serial": m.group(1)}
        m = re.match(r"I'm married to (\w+)\.", t)
        if m:
            j["alias"] = {"role": "partner", "name": m.group(1)}
        return {"text": json.dumps(j), "ms": 1, "tokens": 0}


class Junk:
    """Makes things up: places and serials that aren't in the note."""
    type, cfg = "fake", {}

    async def chat(self, system, messages, max_tokens=800, temperature=0.2):
        return {"text": json.dumps({"place": {"thing": "drill", "in": "attic"}, "fields": {"serial": "ZZ999"},
                                    "alias": {"role": "partner", "name": "Mallory"}, "days_ago": 3}), "ms": 1}


class Models:
    def __init__(self, **ms):
        self.models = ms

    def get(self, n):
        return self.models[n]


async def main():
    # ---- lookups from outside get the worked-out lines and the profile ----------------------------------------------
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    st.remember(text="Dana is my partner.")
    st.remember(text="The drill is in the red toolbox.")
    st.remember(text="I moved the red toolbox to the shed.")
    res = M.lookup(st, "Where is the drill?", 8, touch=False)
    check("worked-out lines come first", res[0]["id"] == 0 and "red toolbox" in res[0]["body"] and "shed" in res[0]["body"],
          res[0])
    check("profile comes with it", any(r["shelf"] == "profile" and r["id"] == 0 and "Dana" in r["body"] for r in res))
    check("then the notes", any(r["id"] and "drill" in r["body"] for r in res))
    check("a shelf filter gets notes only", all(r["id"] for r in M.lookup(st, "drill", 8, shelf="inbox", touch=False)))
    check("the panel's own search gets notes only", all(r["id"] for r in M.lookup(st, "drill", 8, touch=False, extras=False)))

    # ---- notes filed before 0.19 ------------------------------------------------------------------------------------
    st2 = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    Store.STRUCTURE = False
    a = st2.remember(text="Sam is my brother.")
    b = st2.remember(text="Change the furnace filter every 3 months.")
    c = st2.remember(text="The passports are in the desk drawer.")
    st2.remember(text="Living room light turned on.", source="home")
    Store.STRUCTURE = True
    check("old notes have no structure", not st2.get(c["id"])["meta"].get("place"))
    n = S.backfill(st2)
    check("backfill reads them", n == 3 and st2.get(c["id"])["meta"]["place"] == {"thing": "passports", "in": "desk drawer"}
          and st2.get(b["id"])["meta"]["chore"]["every_days"] == 90, n)
    check("who-is-who moves to the profile shelf", st2.get(a["id"])["shelf"] == "profile")
    check("backfill runs once per note", S.backfill(st2) == 0)

    # ---- due things in the morning summary -------------------------------------------------------------------------
    st3 = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    long_ago = time.time() - 100 * 86400
    st3.clock = lambda: long_ago
    st3.remember(text="Change the furnace filter every 3 months.")
    st3.clock = time.time
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%A")
    st3.remember(text=f"The trash goes out every {tomorrow}.")
    st3.remember(text="Clean the gutters every 6 months.")
    due = S.due_lines(st3, time.time())
    check("overdue chore listed", any("furnace filter" in x and "overdue" in x for x in due), due)
    check("tomorrow's repeating thing listed", any("trash" in x.lower() and "tomorrow" in x for x in due), due)
    check("a chore not due is left out", not any("gutters" in x for x in due), due)

    class HA:
        async def notify(self, *a, **k):
            pass
    from homemind.morning import Morning
    text = Morning(st3, HA(), None).build()
    check("morning summary has a Due line", "Due:" in text and "furnace filter" in text, text)
    check("'when did I last' gets no next-date line",
          not S.recurring_lines(st3, "When did I last take the trash out?", st3.recall("trash", touch=False), time.time()))
    r = S.recurrence("Trash collection happens weekly, on Tuesday.", time.time())
    check("'weekly, on Tuesday' is every Tuesday", r and r["kind"] == "weekly" and r["dow"] == 1, r)

    # ---- secrets right after the secret word -------------------------------------------------------------------------
    m = lambda t: privacy.mask(t, "", lambda x: [], lambda x: set())
    check("'password sunflower88' is a secret", privacy.is_sensitive("Guest wifi password sunflower88"))
    check("... and hidden", m("Guest wifi password sunflower88") == "Guest wifi password [hidden]")
    check("'code: K9x!pq' hidden", "K9x" not in m("Garage code: K9x!pq"))
    check("'password reset done' is not a secret", not privacy.is_sensitive("Password reset done for the router."))

    # ---- what the patterns miss: the model's reading, checked against the note -----------------------------------------
    now = time.time()
    add = S.from_model('{"place": {"thing": "the drill", "in": "the red toolbox"}}',
                       "You'll find the drill inside the red toolbox.", now)
    check("a place copied from the note is kept", add.get("place") == {"thing": "drill", "in": "red toolbox"}, add)
    add = S.from_model('{"place": {"thing": "drill", "in": "attic"}, "fields": {"serial": "ZZ999"}, '
                       '"alias": {"role": "partner", "name": "Mallory"}}', "You'll find the drill inside the red toolbox.", now)
    check("made-up place, serial and name are dropped", add == {}, add)
    check("not JSON: nothing", S.from_model("Sure! The drill is in the toolbox.", "x", now) == {})
    check("a bad role is dropped", S.from_model('{"alias": {"role": "overlord", "name": "Dana"}}', "Dana rules.", now) == {})

    mind = M.Mind(tempfile.mkdtemp(), Models(oracle=Oracle(), junk=Junk()))
    acc = {}
    for ingest, model in (("raw", None), ("structure", "oracle"), ("structure", "junk")):
        xs = []
        for seed in range(1001, 1006):
            r = await mind.run_test_now("paraphrase", "brain", model=model, seed=seed, ingest=ingest)
            xs.append(r["summary"].get("accuracy", 0) if r["status"] == "done" else -1)
        acc[(ingest, model)] = sum(xs) / len(xs)
    check("patterns alone miss the paraphrases", acc[("raw", None)] == 0.0, acc)
    check("a good model fills them in", acc[("structure", "oracle")] >= 0.9, acc)
    check("a model that makes things up adds nothing", acc[("structure", "junk")] == 0.0, acc)
    for suite in ("update", "places"):
        base = (await mind.run_test_now(suite, "brain", seed=1001))["summary"]["accuracy"]
        junk = (await mind.run_test_now(suite, "brain", model="junk", seed=1001, ingest="structure"))["summary"]["accuracy"]
        check(f"the junk model doesn't hurt {suite}", junk >= base, (base, junk))

    # overnight: reads recent notes the patterns missed, once each
    st4 = mind.store
    x = st4.remember(text="You'll find the glue gun inside the craft bin.")
    st4.remember(text="The drill is in the red toolbox.")            # the patterns read this one: not sent
    ov = Overnight(st4, mind.models, None)
    n = await ov.read_structure(mind.models.get("oracle"))
    check("overnight reads a missed note", n == 1 and st4.get(x["id"])["meta"].get("place", {}).get("in") == "craft bin",
          (n, st4.get(x["id"])["meta"]))
    check("... once", await ov.read_structure(mind.models.get("oracle")) == 0)

    # ---- notes Home Mind writes itself are not read for structure ----------------------------------------------------
    import tempfile as _tf
    from homemind.store import Store as _Store
    from homemind import structure as _S
    sx = _Store(_tf.mktemp())
    study = sx.remember(text="5 servings a day was linked to lower mortality. Takeaway: two fruits and three vegetables "
                             "daily.", title="Nutrition study: 5 a day", shelf="nutrition/evidence",
                        source="nutrition-tracker")
    summ = sx.remember(text="Remembered 5 this week. Automations ran weekly.", title="Morning summary",
                       shelf="summaries/morning", source="morning")
    mine = sx.remember(text="I take my vitamins every day.", title="Vitamins")
    import json as _json
    meta = lambda i: _json.loads(sx.db.execute("SELECT meta FROM items WHERE id=?", (i,)).fetchone()["meta"] or "{}")
    check("a study's 'daily' is not a repeating thing", "recur" not in meta(study["id"]), meta(study["id"]))
    check("a summary's 'weekly' is not either", "recur" not in meta(summ["id"]), meta(summ["id"]))
    check("the user's own 'every day' still is", "recur" in meta(mine["id"]), meta(mine["id"]))
    sx.db.execute("UPDATE items SET meta=? WHERE id=?", (_json.dumps({"recur": {"kind": "interval", "days": 1}}),
                                                       study["id"]))
    check("structure read by mistake is taken off", _S.unread_system(sx) == 1 and "recur" not in meta(study["id"])
          and "recur" in meta(mine["id"]))
    print("HOLES OK" if not FAILS else f"HOLES FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
