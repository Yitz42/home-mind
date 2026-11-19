"""Phase 9, keeping things sound: the outbox (sent once, retried, expired), the forget list surviving a restore, the
cross-check, the test restore, weekly saves and rollback of learned records with the change log and daily cap, and
records back on trial after a model change. No Home Assistant or model needed.

  python tests/test_soundness.py
"""
import asyncio
import os
import shutil
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
from homemind import dbguard, upkeep as U  # noqa: E402
from homemind.ha import HA  # noqa: E402
from homemind.learned import Learned  # noqa: E402
from homemind.learned_answers import LearnedAnswers  # noqa: E402
from homemind.outbox import Outbox  # noqa: E402
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


# ---- the outbox ---------------------------------------------------------------------------------------------------
st = Store(os.path.join(tempfile.mkdtemp(), "mind.db"))
clk = Clock(time.time())
ha = HA(None)
ha.token = "x"
calls, answer = [], {"status": 200}


async def call(dom, svc, data, approved=False):
    calls.append((dom, svc, data))
    if answer["status"] == "boom":
        raise OSError("Home Assistant is restarting")
    return answer["status"]
ha.call = call
ha.outbox = Outbox(st, clock=clk)
msg = {"title": "Home Mind", "message": "The bins go out tonight."}
check("outbox: sent, and recorded as sent", run(ha.push("mobile_app_phone", msg)) == "sent" and
      ha.outbox.stats() == {"sent": 1}, ha.outbox.stats())
check("outbox: the same message again within 10 minutes isn't sent twice",
      run(ha.push("mobile_app_phone", msg)) == "duplicate" and len(calls) == 1)
answer["status"] = "boom"
check("outbox: Home Assistant down: kept as failed", run(ha.push("mobile_app_phone", {"message": "Leave by 8:10"},
                                                                  tier="important")) == "failed")
answer["status"] = 503
run(ha.retry_failed())
check("outbox: retried while it fails", ha.outbox.stats().get("failed") == 1)
answer["status"] = 200
check("outbox: retried until it goes", run(ha.retry_failed()) == 1 and ha.outbox.stats().get("sent") == 2,
      ha.outbox.stats())
answer["status"] = 500
run(ha.push("mobile_app_phone", {"message": "Routine thing"}))
clk.t += 13 * 3600
check("outbox: given up on once too late to be useful", ha.outbox.take("failed") == [] and
      ha.outbox.stats().get("expired") == 1, ha.outbox.stats())
answer["status"] = 200
ha.quiet = ((0, 0), (23, 59))
check("outbox: quiet hours: held in the database (a restart doesn't lose it)",
      run(ha.push("mobile_app_phone", {"message": "Filter needs changing"})) == "held" and
      len(ha.outbox.take("held")) == 1)
ha.quiet = None
check("outbox: after quiet hours, sent", run(ha.flush_held()) == 1 and ha.outbox.take("held") == [])

# ---- the forget list ----------------------------------------------------------------------------------------------
folder = tempfile.mkdtemp()
st = Store(os.path.join(folder, "mind.db"))
fl = U.ForgetList(st, folder)
st.on_forget = lambda rows, vals: fl.record_notes(rows, vals)
st.on_forget_name = fl.record_name
a = st.remember("Locker 4471 at the gym.", title="Gym locker")["id"]
b = st.remember("Sam's birthday is 3 May.", title="Sam")["id"]
c = st.remember("Milk is in the fridge.", title="Milk")["id"]
os.makedirs(dbguard.snapdir(st.path), exist_ok=True)
old = os.path.join(folder, "before.db")
dbguard.snapshot(st.db, st.path)
shutil.copy2(dbguard.snapshots(st.path)[0], old)
st.purge([a])
st.purge_about("Sam")
check("forget: deleted for good", not st.get(a) and not st.get(b) and st.get(c))
text = open(fl.file).read()
check("forget: only fingerprints are kept, never the text", "4471" not in text and "Sam" not in text and
      "locker" not in text.lower())
check("forget: nothing to do while nothing came back", fl.matches() == [])
# a restore from before the purge: the deleted notes are back, and the table copy of the list too is from before
st.db.close()
shutil.copy2(old, st.path)
st2 = Store(st.path)
fl2 = U.ForgetList(st2, folder)
check("forget: after a restore the old notes are back (and the list's table copy is empty)",
      st2.get(a) and st2.get(b) and not st2.db.execute("SELECT COUNT(*) FROM forget_list").fetchone()[0])
check("forget: the file beside it still knows", len(fl2.entries()) >= 2)
d = st2.remember("Gym bag: towel, lock code 4471", title="Gym bag")["id"]
removed = fl2.apply()
check("forget: applied again: the restored notes go, a note written since (holding a forgotten value) stays",
      removed >= 2 and not st2.get(a) and not st2.get(b) and st2.get(d) and st2.get(c), removed)


# ---- the check and the test restore -------------------------------------------------------------------------------
class App:
    pass


app = App()
app.mind = types.SimpleNamespace(store=st2)
app.forget = fl2
rep = U.check(app)
check("check: all well", rep["ok"] and rep["checks"]["database"] == "ok" and rep["checks"]["forgotten_back"] == 0,
      rep)
st2.db.execute("INSERT INTO items (title, body, created, updated) VALUES ('Sam', 'Sam likes tea', ?, ?)",
               (1000.0, 1000.0))         # an old note back from somewhere (written long before the purge)
st2.db.commit()
rep = U.check(app)
check("check: a forgotten name back in an old note is a problem", not rep["ok"] and
      rep["checks"]["forgotten_back"] == 1, rep)
fl2.apply()
dbguard.snapshot(st2.db, st2.path, now=time.time() + 86400)
rt = U.restore_test(st2, fl2)
check("test restore: the newest snapshot opens and checks out", rt["ok"] and rt["integrity"] == "ok", rt)
snap = dbguard.snapshots(st2.path)[0]
with open(snap, "r+b") as f:
    f.seek(100)
    f.write(b"\x00" * 4000)
rt = U.restore_test(st2, fl2)
check("test restore: a damaged snapshot fails, and the check says so", not rt["ok"] and not U.check(app)["ok"], rt)

# ---- learned records: change log, daily cap, weekly saves, rollback ------------------------------------------------
st3 = Store(os.path.join(tempfile.mkdtemp(), "mind.db"))
lc = Clock(time.time())
hist = U.LearnedHistory(st3, clock=lc)
lr = Learned(st3, None, clock=lc)
lr.history = hist
rid = lr._new("light.desk", "on", None, "turn on the desk lamp", "test")
lr.set_status(rid, "trusted")
ch = hist.changes()
check("change log: every status change", ch and ch[0]["id"] == rid and ch[0]["status"] == "trusted", ch)
w = hist.save_week()
check("weekly save: once a week", w and hist.save_week() is None and hist.weeks()[0]["trusted"] == 1)
lr.set_status(rid, "dropped", "a bad week")
lr._new("light.hall", "on", None, "hall light on", "test")
out = hist.rollback(w)
check("rollback: the learned records as they were that week", lr.get(rid)["status"] == "trusted" and
      len(lr.all()) == 1, (out, [(x["id"], x["status"]) for x in lr.all()]))
for i in range(U.DAILY_CAP):
    lr._new(f"light.l{i}", "on", None, f"light {i} on", "test")
check("daily cap: no room once today's new records reach the cap", not hist.room_today("template"))
la = LearnedAnswers(st3, clock=lc)
check("daily cap: counted per kind", hist.room_today("answer"))

# ---- a new model ------------------------------------------------------------------------------------------------
st4 = Store(os.path.join(tempfile.mkdtemp(), "mind.db"))
lr4 = Learned(st4, None)
t1 = lr4._new("light.desk", "on", None, "turn on the desk lamp", "test")
lr4.set_status(t1, "trusted")
models = {"fg": types.SimpleNamespace(cfg={"model": "functiongemma-270m"}), "haiku": types.SimpleNamespace(cfg={})}
ap = types.SimpleNamespace(mind=types.SimpleNamespace(store=st4), opts={"control_model": "fg"},
                           models=types.SimpleNamespace(models=models), learned=lr4,
                           modes=types.SimpleNamespace(EARNED="earned_kinds", cloud=lambda: "haiku"))
st4.kv_set("earned_kinds", ["light.on"])
check("model: the first look just remembers the models", U.retest_if_swapped(ap) is None and
      lr4.get(t1)["status"] == "trusted")
check("model: unchanged: nothing happens", U.retest_if_swapped(ap) is None)
models["fg"] = types.SimpleNamespace(cfg={"model": "functiongemma-1b"})
r = U.retest_if_swapped(ap)
check("model: changed: trusted records back on trial and earned kinds taken away",
      r and r["changed"] == ["control"] and lr4.get(t1)["status"] == "provisional" and
      st4.kv_get("earned_kinds") == [] and r["kinds_unearned"] == ["light.on"], r)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
