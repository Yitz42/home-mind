"""Phase 13: trackers (3 at most, paused on low/rough days and in travel), one-word logs, the medicines log (tracking
only, the repeat answered with the fact), significant events (unknown, never none), nudges (one a day, stop after 3
ignored), exertion zones (off by default, hold, data checks, blocked by rate-lowering medicines, per-minute summaries
only, one notice on a low day), and travel mode holding routine pushes.

  python tests/test_tracking.py
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
from homemind import tracking as TR  # noqa: E402
from homemind.day import Day  # noqa: E402
from homemind.ha import HA  # noqa: E402
from homemind.store import Store  # noqa: E402

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


noon = datetime.combine(date.today(), datetime.min.time()).replace(hour=12).timestamp()
clk = Clock(noon)
folder = tempfile.mkdtemp()
st = Store(os.path.join(folder, "mind.db"))
day = Day(st, clock=clk)
tk = TR.Tracking(st, day, clock=clk, folder=folder)

# ---- trackers -----------------------------------------------------------------------------------------------------
for n in ("fog", "headache", "pain"):
    tk.add_tracker(n, "Dana")
try:
    tk.add_tracker("nausea", "Dana")
    check("trackers: at most 3", False)
except ValueError:
    check("trackers: at most 3", True)
check("trackers: someone else has their own 3", tk.add_tracker("mood", "Sam") > 0)
check("trackers: weekly: not due on day one", tk.due("Dana") == [])
clk.t += 7 * 86400 + 1
day.set("good", "Dana")
check("trackers: due after a week", len(tk.due("Dana")) == 3)
day.set("low", "Dana")
check("trackers: paused on a low day", tk.due("Dana") == [] and tk.paused("Dana") == "a low day")
day.set("good", "Dana")
check("logs: 'fog 2' for a tracker: logged with the time", tk.log_said("fog 2", "Dana").startswith("Logged fog 2 at"))
check("logs: 'log headache'", tk.log_said("log headache", "Dana").startswith("Logged headache"))
check("logs: not a tracker: left alone ('log a run' is fitness's)", tk.log_said("log a run", "Dana") is None and
      tk.log_said("bins 2", "Dana") is None)
check("logs: details only asked on a good day, never as a follow-up", len(tk.to_confirm("Dana")) == 2)
day.set("rough", "Dana")
check("logs: not on a rough day", tk.to_confirm("Dana") == [])
day.set("good", "Dana")

# ---- the medicines log ----------------------------------------------------------------------------------------------
r = tk.med_said("took ibuprofen 400mg", "Dana")
check("meds: logged, and the reply says nobody reviews it", r and "Nobody reviews this. No interaction checks." in
      r["answer"] and "400 mg" in r["answer"], r)
clk.t += 2 * 3600
r = tk.med_said("I took ibuprofen", "Dana")
check("meds: again inside its window: the fact only", r and r.get("repeat") and r["answer"].startswith(
    "You logged ibuprofen at") and "should" not in r["answer"].lower(), r)
clk.t += 5 * 3600
check("meds: after the window: logged as usual", not tk.med_said("took ibuprofen", "Dana").get("repeat"))
for not_med in ("I took the dog out", "took a nap", "took the bins out", "took a photo"):
    check(f"meds: not a medicine: {not_med!r}", tk.med_said(not_med, "Dana") is None)
check("meds: tracking only, its owner's", len(tk.meds("Dana")) == 3 and tk.meds("Sam") == [])

# ---- significant events -------------------------------------------------------------------------------------------
ev = tk.events_answer("Dana", {"a trip": "yes", "a cold or infection": "no"})
check("events: answered kinds kept, the rest unknown (never 'none')", ev["a trip"] == "yes" and
      ev["a cold or infection"] == "no" and ev["a hard week"] == "unknown", ev)

# ---- nudges -------------------------------------------------------------------------------------------------------
try:
    tk.add_nudge("", {"type": "time", "at": "09:00"}, "Drink some water")
    check("nudges: only for a goal the person wrote", False)
except ValueError:
    check("nudges: only for a goal the person wrote", True)
nid = tk.add_nudge("drink more water", {"type": "time", "at": "09:00"}, "Water?", "Dana")
clk.t = datetime.combine(date.fromtimestamp(clk.t), datetime.min.time()).replace(hour=9, minute=30).timestamp()
day.set("good", "Dana")
due = tk.nudges_due()
check("nudges: due at its time", [n["id"] for n in due] == [nid] and "(Your goal: drink more water.)" in
      tk.nudge_message(due[0]), due)
tk.sent(nid)
check("nudges: one a day", tk.nudges_due() == [])
for i in range(3):
    clk.t += 86400
    day.set("good", "Dana")
    due = tk.nudges_due()
    if due:
        tk.sent(nid)
clk.t += 86400
day.set("good", "Dana")
tk.nudges_due()
check("nudges: stop after 3 ignored", tk.nudges(active=True) == [] and tk.nudges(active=False)[0]["ignored"] >= 3,
      tk.nudges(active=False))
n2 = tk.add_nudge("log fog", {"type": "nothing_logged_by", "at": "20:00", "tracker": "fog"}, "Fog today?", "Dana")
clk.t = datetime.combine(date.fromtimestamp(clk.t), datetime.min.time()).replace(hour=20, minute=30).timestamp()
day.set("good", "Dana")
check("nudges: 'nothing logged by 8 pm' fires when nothing was", any(n["id"] == n2 for n in tk.nudges_due()))
tk.log_said("fog 1", "Dana")
check("nudges: and not once something is", all(n["id"] != n2 for n in tk.nudges_due()))
tk.set_travel(True)
check("travel: trackers and nudges pause", tk.paused("Dana") == "travel mode" and tk.due("Dana") == [])
tk.set_travel(False)

# ---- exertion zones -----------------------------------------------------------------------------------------------
zc = Clock(noon)
z = TR.Zones(st, {}, day, clock=zc, folder=folder)
check("zones: off by default", not z.on() and "off" in z.why_off() and z.reading(150, resting=60) is None)
z = TR.Zones(st, {"exertion_zones": True}, day, clock=zc, folder=folder)
check("zones: one high reading isn't enough (must hold)", z.reading(80, resting=60) is None)
zc.t += 5
check("zones: held for two readings: above", z.reading(82, resting=60) == "above" and z.zone == "above")
zc.t += 5
check("zones: readings that fail the data checks don't count", z.reading(400, resting=60) is None and
      z.reading("n/a", resting=60) is None and z.reading(70, at=zc.t - 600, resting=60) is None)
z2 = TR.Zones(st, {"exertion_zones": True, "exertion_threshold": 100}, day, clock=zc, folder=folder)
z2.reading(90)
zc.t += 5
check("zones: the clinician's line wins over resting + 15", z2.reading(95) is None)
zc.t += 120
z.reading(84, resting=60)
lines = open(os.path.join(folder, "zones.jsonl")).read().splitlines()
check("zones: only per-minute summaries are kept, never the raw readings",
      lines and all(set(json.loads(x)) == {"minute", "n", "mean", "max", "above"} for x in lines), lines)
day.set("low")
check("zones: on a low day one neutral notice", z.notice() == "Heart's up. Rest when you can." and z.notice() is None)
day.set("rough")
st.kv_set("zones_notice", {})
check("zones: none on a rough day", z.notice() is None)
day.set("low")
z.mute_today()
check("zones: 'not today' mutes it", z.notice() is None)
tk.med_said("took bisoprolol 2.5mg", "Dana")
check("zones: switch themselves off when a beta-blocker is logged", not z.on() and "bisoprolol" in z.why_off())

# ---- travel holds routine pushes ----------------------------------------------------------------------------------
ha = HA(None)
ha.token = "x"
calls = []


async def call(dom, svc, data, approved=False):
    calls.append(svc)
    return 200
ha.call = call
ha.travelling = lambda: True
check("travel: routine pushes wait", asyncio.run(ha.push("mobile_app_phone", {"message": "x"})) == "travel" and not calls)
check("travel: safety still goes", asyncio.run(ha.push("mobile_app_phone", {"message": "smoke"}, tier="safety")) == "sent")

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
