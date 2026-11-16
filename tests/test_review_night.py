"""Expert review regressions: what Home Mind learns by itself (round 3, learning loops). Fake models and clocks; no Home
Assistant or network needed.

  python tests/test_review_night.py
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda total=None, **k: types.SimpleNamespace(total=total)
    m.ClientError = type("ClientError", (Exception,), {})
    sys.modules["aiohttp"] = m
from homemind import filing, house, journal as J, prefs as P  # noqa: E402
from homemind.filing import Filer  # noqa: E402
from homemind.mind import Mind  # noqa: E402
from homemind.overnight import ConvLog, Overnight  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def new(clock=None):
    return Store(os.path.join(tempfile.mkdtemp(), "m.db"), clock=clock)


class Fake:
    type, cfg = "ollama", {"url": "http://192.168.1.5:11434"}

    def __init__(self, reply):
        self.reply, self.sent = reply, []

    async def chat(self, system, messages, **k):
        self.sent.append(messages[-1]["content"])
        return {"text": self.reply(messages[-1]["content"]) if callable(self.reply) else self.reply, "ms": 1}


class Models:
    def __init__(self, **m):
        self.models = m

    def get(self, n):
        return self.models[n]

    def list(self):
        return [{"name": n} for n in self.models]


# ---- filing must not learn from its own guesses ------------------------------------------------------------------------
def filing_self_training():
    st = new()
    for k in range(8):
        st.remember(text=f"Bake the bread with flour and yeast, batch {k}.", shelf="kitchen")
    for k in range(6):
        st.remember(text=f"Tomato seeds and compost go in garden bed {k}.", shelf="garden")
    st.filer = Filer(st, rebuild_seconds=0)
    probe = "Water the tomato plants in the garden with the hose"
    before = st.filer.guess(probe)
    placed = []
    for k in range(12):      # a run of notes the model files on kitchen by itself; no one looks at them
        r = st.remember(text=f"Water and flour for the bread dough with the hose plants {k}")
        placed.append(st.get(r["id"]))
    st.filer._model = None
    after = st.filer.guess(probe)
    check("filing: the model's own placements happen (setup)", all(p["meta"].get("filed_by") == "learned" for p in placed))
    check("filing: its own placements don't teach it (a garden note stays a garden note)",
          before[0] == "garden" and after[0] == "garden" and after[1] > 0.9, (before, after))
    check("filing: the model counts only notes a person placed",
          st.filer.model()["docs"]["kitchen"] == 8, dict(st.filer.model()["docs"]))
    # a note the person moved there does teach it, even though the model first filed it
    i = placed[0]["id"]
    m = dict(st.get(i)["meta"], moved_from="kitchen")
    st.db.execute("UPDATE items SET shelf='garden', meta=? WHERE id=?", (json.dumps(m), i))
    st.db.commit()
    st.filer._model = None
    check("filing: a note the person moved teaches it", st.filer.model()["docs"]["garden"] == 7)
    check("filing: trains() reads meta text or dicts", filing.trains('{"filed_by": "learned"}') is False and
          filing.trains({"filed_by": "learned", "keep_shelf": True}) and filing.trains(None) and filing.trains("{bad"))


def reshelve_no_flipflop():
    """Notes the model moved don't pull more notes after them, and moving runs settle (nothing moves twice)."""
    import time
    now = [time.time() - 30 * 86400]
    st = new(clock=lambda: now[0])
    for k in range(8):
        st.remember(text=f"Bake the sourdough bread with rye flour and yeast, batch {k}.", shelf="kitchen/recipes")
        st.remember(text=f"Car oil change and tire pressure check at the garage, visit {k}.", shelf="car")
    ids = [st.remember(text=f"Rye flour sourdough bread {k} with yeast and the garage oven.", shelf="car",
                       meta={"filed_by": "learned"})["id"] for k in range(6)]
    now[0] = time.time()
    fl = Filer(st)
    first = filing.reshelve(fl)
    moves = {}
    for _ in range(4):
        for x in filing.reshelve(fl)["moved"]:
            moves[x["id"]] = moves.get(x["id"], 0) + 1
    check("reshelve: the model's misfiled notes go to the recipes", all(st.get(i)["shelf"] == "kitchen/recipes"
                                                                         for i in ids), first)
    check("reshelve: nothing moves again on later nights", not moves, moves)


# ---- learned settings ----------------------------------------------------------------------------------------------------
def lamp(b):
    return {"light.desk": {"domain": "light", "state": "on", "attrs": {"brightness": b}, "ctx": {}}}


def prefs_retire():
    st = new()
    pf = P.Prefs(st)
    base = datetime(2026, 11, 2, 19, 15).timestamp()          # a Monday evening
    for d in range(5):
        pf.observe(lamp(255), now=base + d * 86400 - 3600)
        pf.observe(lamp(102), now=base + d * 86400)
    pf.file_notes({"light.desk": "Desk lamp"}, now=base + 5 * 86400)
    key = "preference|light.desk|weekday evening"
    check("prefs: a learned preference is filed (setup)", st.active_by_key(key) is not None)
    later = base + (P.DAYS + 10) * 86400                       # two months with no settings: nothing learned any more
    pf.file_notes({"light.desk": "Desk lamp"}, now=later)
    old = st.active_by_key(key)
    check("prefs: a preference that no longer holds isn't kept as a note", old is None,
          old and (old["status"], old["body"]))


def prefs_night_band():
    fri, sat = datetime(2026, 11, 6, 23, 30), datetime(2026, 11, 7, 0, 30)   # Nov 6 2026 is a Friday
    sun, mon = datetime(2026, 11, 8, 23, 30), datetime(2026, 11, 9, 0, 30)
    check("prefs: Friday 23:30 and Saturday 00:30 are one night (a weekend night)",
          P.band(fri.timestamp()) == P.band(sat.timestamp()) == "weekend night",
          (P.band(fri.timestamp()), P.band(sat.timestamp())))
    check("prefs: Sunday 23:30 and Monday 00:30 are one night (a weekday night)",
          P.band(sun.timestamp()) == P.band(mon.timestamp()) == "weekday night",
          (P.band(sun.timestamp()), P.band(mon.timestamp())))
    check("prefs: other bands unchanged", P.band(datetime(2026, 11, 6, 19).timestamp()) == "weekday evening" and
          P.band(datetime(2026, 11, 7, 9).timestamp()) == "weekend morning")
    # the same late-night setting on six Friday nights, some before midnight, some after: one preference
    st = new()
    pf = P.Prefs(st)
    for w in range(6):
        evening = fri.replace(hour=21) + timedelta(days=7 * w)
        pf.observe(lamp(255), now=evening.timestamp())                # full brightness in the evening
        t = (fri if w % 2 else sat) + timedelta(days=7 * w)
        pf.observe(lamp(51), now=t.timestamp())                        # dimmed to 20% late at night
    got = pf.learned(now=(sat + timedelta(days=40)).timestamp())
    check("prefs: a Friday-night setting either side of midnight is learned", any(
        p["band"] == "weekend night" and p["n"] >= 5 and p["value"] == 20 for p in got), got)


# ---- habits: one version per change, not per night ----------------------------------------------------------------------
def habit_churn():
    st = new()
    h = {"entity_id": "person.yitz", "name": "Yitz", "state": "home", "daytype": "weekdays", "minute": 1170,
         "hits": 10, "days": 14, "by_hand": 10, "in_window": 10}
    house.file_habits(st, {"habits": [h]})
    for n in range(11, 15):          # four more nights, the habit holds on one more day each night
        house.file_habits(st, {"habits": [dict(h, hits=n, days=n + 4)]})
    versions = st.db.execute("SELECT COUNT(*) FROM items WHERE source='habits'").fetchone()[0]
    check("habits: the same habit isn't filed again every night", versions == 1, versions)
    house.file_habits(st, {"habits": [dict(h, minute=1230)]})
    cur = st.active_by_key("habit:person.yitz|home|weekdays")
    check("habits: a new time still replaces the old note", cur and "8:30 PM" in cur["body"], cur and cur["body"])


# ---- restarts don't repeat (or lose) nightly work -----------------------------------------------------------------------
def at_clock(mod, when):
    class D(datetime):
        @classmethod
        def now(cls, tz=None):
            return when
    mod.datetime = D


async def journal_restart():
    st = new()
    day = datetime.now().replace(hour=23, minute=40, second=0, microsecond=0)
    st.remember(text="Went for a long walk by the river.", shelf="journal", at=day.replace(hour=9).timestamp())
    mdl = Fake("You went for a walk.")
    real = J.datetime
    try:
        at_clock(J, day)
        j = J.Journal(st, Models(haiku=mdl))
        await j.tick()
        j2 = J.Journal(st, Models(haiku=mdl))       # the add-on restarts at 23:45
        at_clock(J, day.replace(minute=45))
        await j2.tick()
    finally:
        J.datetime = real
    n = st.db.execute("SELECT COUNT(*) FROM items WHERE shelf='journal/summaries'").fetchone()[0]
    check("journal: a restart after tonight's summary doesn't file a second one", n == 1 and len(mdl.sent) == 1, n)

    st2 = new()
    st2.remember(text="Planted the tulips.", shelf="journal", at=day.replace(hour=9).timestamp())
    try:
        at_clock(J, day.replace(minute=50))
        j3 = J.Journal(st2, Models(haiku=Fake("You planted tulips.")))      # started after 23:30, none filed yet
        await j3.tick()
    finally:
        J.datetime = real
    n = st2.db.execute("SELECT COUNT(*) FROM items WHERE shelf='journal/summaries'").fetchone()[0]
    check("journal: started after 23:30 with no summary yet, today is still summarized", n == 1, n)


async def overnight_restart():
    from homemind import overnight as O
    st = new()
    runs = []
    mdl = Fake('{"facts": []}')
    o = Overnight(st, Models(big=mdl), ConvLog(st), model="big")
    o.keys_on = False

    async def run(limit_turns=24):
        runs.append(1)
        o.last = {"at": 1, "filed": 3}
        st.kv_set("overnight_last", o.last)
        return o.last
    real = O.datetime
    night = datetime.now().replace(hour=3, minute=5, second=0, microsecond=0)
    try:
        at_clock(O, night)
        o.run = run
        await o.tick()
        o2 = Overnight(st, Models(big=mdl), ConvLog(st), model="big")
        o2.run = run
        at_clock(O, night.replace(hour=4))
        await o2.tick()
    finally:
        O.datetime = real
    check("overnight: a restart inside the window doesn't run the night again", len(runs) == 1, len(runs))
    check("overnight: the last run's report survives a restart", (o2.last or {}).get("filed") == 3, o2.last)
    o3 = Overnight(st, Models(big=mdl), ConvLog(st), model="big")
    o3.keys_on = False
    r = await o3.run()
    check("overnight: run() keeps its report", st.kv_get("overnight_last", {}).get("at") == r["at"])


# ---- a known person's questions are learned as theirs --------------------------------------------------------------------
async def ask_speaker():
    mind = Mind(tempfile.mkdtemp(), Models(m=Fake("Noted.")))
    st = mind.store
    cl = ConvLog(st)
    mind.convlog = cl
    await mind.ask("By the way my locker code is 5150, what's my gym schedule?", "m", fallback=False, speaker="Dana")
    row = st.db.execute("SELECT speaker FROM conversations").fetchone()
    check("ask: the conversation log keeps who asked", row and row["speaker"] == "Dana", row and dict(row))

    def facts(text):
        return json.dumps({"facts": [{"text": "My locker code is 5150.", "turn": 1}]} if "5150" in text else {"facts": []})
    o = Overnight(st, Models(big=Fake(facts)), cl, model="big")
    o.known_only, o.keys_on = True, False          # people are set up: unknown voices aren't learned from
    r = await o.run()
    got = st.db.execute("SELECT meta FROM items WHERE source='overnight'").fetchall()
    check("ask: with people set up, a known person's question is learned from (not dropped as a guest)",
          got and r.get("unknown_voices") == 0, r)
    check("ask: ... and is personal to them", got and '"owner": "Dana"' in got[0]["meta"], got and got[0]["meta"])
    mind2 = Mind(tempfile.mkdtemp(), Models(m=Fake("Ok.")))
    mind2.convlog = ConvLog(mind2.store)
    await mind2.ask("What time is it?", "m", fallback=False)
    row = mind2.store.db.execute("SELECT speaker FROM conversations").fetchone()
    check("ask: no speaker information stays none", row and row["speaker"] is None)


filing_self_training()
reshelve_no_flipflop()
prefs_retire()
prefs_night_band()
habit_churn()
asyncio.run(journal_restart())
asyncio.run(overnight_restart())
asyncio.run(ask_speaker())
print(f"\n{len(FAILS)} failed" if FAILS else "\nall ok")
sys.exit(1 if FAILS else 0)
