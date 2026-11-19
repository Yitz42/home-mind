"""Phase 2 of the design (0.27): approval levels and phone approval, the ladder, device access, red flags, phone
notification tiers, the service guard, automation ownership with the watchdog and catch-up, and the daily safety
suite. No Home Assistant needed.

  python tests/test_safety.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: None
    m.WSMsgType = types.SimpleNamespace(TEXT=1)
    m.ClientSession, m.ClientError, m.BasicAuth = object, Exception, (lambda *a: a)
    m.web = types.ModuleType("aiohttp.web")          # main is imported only for App.do here
    sys.modules["aiohttp"] = m
    sys.modules["aiohttp.web"] = m.web
from homemind import approval as AP, redflags, safety  # noqa: E402
from homemind import automations as AU  # noqa: E402
from homemind.approval import Access, Approvals, ApprovalNeeded, PHONE, CONFIRM, OPEN  # noqa: E402
from homemind.ha import HA, guard  # noqa: E402
from homemind.home import Home  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []
LOOP = asyncio.new_event_loop()


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(c):
    return LOOP.run_until_complete(c)


class FakeHA:
    def __init__(self):
        self.calls = []

    async def state(self, eid):
        return {"state": "on"} if eid == "input_boolean.mind_control_enabled" else None

    async def call(self, dom, svc, data):
        self.calls.append((dom, svc, data))
        return 200


def store():
    return Store(os.path.join(tempfile.mkdtemp(), "m.db"))


def home(catalog, controls=("light.*", "lock.*", "cover.*", "switch.*", "climate.*", "fan.*")):
    h = Home(store(), FakeHA(), {"home_controls": list(controls)})
    h.catalog = {e: dict({"name": e, "kind": "device", "domain": e.split(".")[0], "state": "off", "attrs": {}}, **c)
                 for e, c in catalog.items()}
    return h


# ---- approval levels and the ladder ------------------------------------------------------------------------------
h = home({"light.desk": {}, "lock.front": {}, "cover.garage": {"attrs": {"device_class": "garage"}},
          "cover.blinds": {"attrs": {"device_class": "blind"}}, "switch.oven": {"name": "Oven"},
          "switch.kettle": {"name": "Kettle"}, "climate.den": {"attrs": {"temperature_unit": "°F"}}})
L = lambda e, a=None, v=None: AP.level(h, e, a, v)  # noqa: E731
check("level: a light is open", L("light.desk", "on") == OPEN)
check("level: a lock is phone", L("lock.front", "lock") == PHONE)
check("level: a garage door is phone, blinds are open", L("cover.garage", "open") == PHONE and
      L("cover.blinds", "open") == OPEN)
check("level: switching an oven on is phone, off is open", L("switch.oven", "on") == PHONE and
      L("switch.oven", "off") == OPEN)
check("level: a kettle is open", L("switch.kettle", "on") == OPEN)
check("level: heating within limits needs a yes", L("climate.den", "temperature", 70) == CONFIRM)
check("level: 85°F needs the phone", L("climate.den", "temperature", 85) == PHONE)
check("extreme: by unit or by size", AP.extreme(30) and not AP.extreme(21) and AP.extreme(55, "°F") and
      not AP.extreme(72))
h.set_ladder("lock.front", "do")
check("ladder: 'just do it' can't lower a lock", h.level("lock.front", "unlock")[0] == PHONE and
      h.rung("lock.front") == "ask")
h.set_ladder("light.desk", "ask")
check("ladder: 'ask me first' raises a light to a spoken yes", h.level("light.desk", "on")[0] == CONFIRM)
h.set_ladder("light.desk", "tell")
check("ladder: 'tell me after' keeps it open and tells", h.level("light.desk", "on") == (OPEN, True))
try:
    h.set_ladder("light.desk", "maybe")
    check("ladder: unknown rung refused", False)
except ValueError:
    check("ladder: unknown rung refused", True)

# ---- Home.act with levels -------------------------------------------------------------------------------------------
told = []


async def on_tell(r):
    told.append(r["entity_id"])
h.on_tell = on_tell
run(h.act("light.desk", "on"))
check("act: 'tell me after' acts, then tells", h.ha.calls[-1][:2] == ("light", "turn_on") and told == ["light.desk"])
n = len(h.ha.calls)
for kw in ({}, {"confirm": True}):
    try:
        run(h.act("lock.front", "unlock", **kw))
        check(f"act: lock refused with {kw or 'nothing'}", False)
    except ApprovalNeeded as e:
        check(f"act: lock refused with {kw or 'nothing'} (phone)", e.level == PHONE and len(h.ha.calls) == n)
run(h.act("lock.front", "unlock", approved=True))
check("act: phone-approved unlock calls lock.unlock", h.ha.calls[-1][:2] == ("lock", "unlock"))
h.set_ladder("light.desk", "ask")
try:
    run(h.act("light.desk", "off"))
    check("act: 'ask me first' needs a yes", False)
except ApprovalNeeded as e:
    check("act: 'ask me first' needs a yes", e.level == CONFIRM)
run(h.act("light.desk", "off", confirm=True))
check("act: with the yes it's done", h.ha.calls[-1][:2] == ("light", "turn_off"))

# ---- device access ------------------------------------------------------------------------------------------------
clock = [1_000_000.0]
st = store()
acc = Access(st, clock=lambda: clock[0])
check("access: before a baseline, nothing is new", acc.effective("lock.x", True, True) == "act")
acc.baseline(["light.a", "lock.old"])
check("access: a device that was there keeps what it had", acc.effective("lock.old", True, True) == "act" and
      acc.effective("light.a", False, False) == "read")
check("access: a new lock starts at none", acc.effective("lock.new", True, True) == "none")
check("access: a new light starts at act if on the control list", acc.effective("light.new", True, False) == "act")
ch = acc.set("lock.old", "read")
check("access: stricter applies now", ch["when"] == "now" and acc.effective("lock.old", True, True) == "read")
ch = acc.set("lock.old", "act")
check("access: looser waits 24 hours", ch["when"] == "in 24 hours" and acc.effective("lock.old", True, True) == "read")
clock[0] += 24 * 3600 + 1
check("access: then it applies", acc.effective("lock.old", True, True) == "act")
acc.set("lock.new", "act")
clock[0] += 24 * 3600 + 1
check("access: act still needs the control list", acc.effective("lock.new", False, True) == "read")
try:
    acc.set("light.a", "admin")
    check("access: unknown level refused", False)
except ValueError:
    check("access: unknown level refused", True)


class StatesHA(FakeHA):
    def __init__(self, states):
        super().__init__()
        self._s = states

    async def states(self):
        return self._s


sts = [{"entity_id": "light.desk", "state": "on", "attributes": {"friendly_name": "Desk"}},
       {"entity_id": "lock.front", "state": "locked", "attributes": {"friendly_name": "Front door"}}]
h2 = Home(store(), StatesHA(sts), {"home_controls": ["light.*", "lock.*"]})
run(h2.refresh())
check("refresh: the first time, everything there is kept", set(h2.catalog) == {"light.desk", "lock.front"})
sts.append({"entity_id": "lock.back", "state": "locked", "attributes": {"friendly_name": "Back door"}})
sts.append({"entity_id": "climate.new", "state": "heat", "attributes": {"friendly_name": "New thermostat"}})
run(h2.refresh())
check("refresh: a new lock and new heating are hidden (none)", "lock.back" not in h2.catalog and
      "climate.new" not in h2.catalog and {"lock.back", "climate.new"} <= h2.hidden, (sorted(h2.catalog), h2.hidden))
try:
    run(h2.act("lock.back", "unlock", approved=True))
    check("act: a hidden device can't be moved", False)
except (ValueError, PermissionError):
    check("act: a hidden device can't be moved, even approved", not h2.ha.calls)
h2.access.set("light.desk", "read")
try:
    run(h2.act("light.desk", "off"))
    check("act: a read-only device can't be moved", False)
except PermissionError as e:
    check("act: a read-only device can't be moved", "read" in str(e))

# ---- phone approvals ------------------------------------------------------------------------------------------------
ap = Approvals(clock=lambda: clock[0])
aid = ap.new({"entity_id": "lock.front", "action": "unlock"}, name="Front door", verb="unlock")
msg = Approvals.message(aid, "Front door", "unlock", "voice")
check("approval: Approve needs Face ID / passcode, Deny doesn't",
      msg["data"]["actions"][0]["authenticationRequired"] is True and msg["data"]["actions"][0]["action"] ==
      f"HM_APPROVE_{aid}" and "authenticationRequired" not in msg["data"]["actions"][1])
check("approval: used once", ap.take(aid) is not None and ap.take(aid) is None)
aid = ap.new({"entity_id": "lock.front"})
clock[0] += 121
check("approval: expires after 2 minutes", ap.take(aid) is None)

# ---- the service guard -------------------------------------------------------------------------------------------
for dom, svc, data in (("lock", "unlock", {"entity_id": "lock.f"}), ("alarm_control_panel", "alarm_disarm", {}),
                       ("valve", "open_valve", {}), ("cover", "open_cover", {"entity_id": ["cover.garage_door"]}),
                       ("script", "turn_on", {"entity_id": "script.unlock_door"}),
                       ("homeassistant", "turn_on", {"entity_id": "lock.front"})):
    try:
        guard(dom, svc, data, False)
        check(f"guard: {dom}.{svc} refused", False)
    except PermissionError:
        check(f"guard: {dom}.{svc} refused without approval", True)
guard("lock", "unlock", {"entity_id": "lock.f"}, True)
guard("cover", "open_cover", {"entity_id": "cover.blinds"}, False)
check("guard: approved calls and blinds pass", True)
ha = HA(None)
ha.token = ""
try:
    run(ha.call("lock", "unlock", {"entity_id": "lock.f"}))
    check("guard: HA.call refuses a lock even with no Home Assistant", False)
except PermissionError:
    check("guard: HA.call refuses a lock before anything leaves", True)

# ---- notification tiers ------------------------------------------------------------------------------------------
ha = HA(None)
sent = []


async def call(dom, svc, data, approved=False):
    sent.append(data.get("message"))
    return 200
ha.call = call
ha.cap = 1
ha.quiet = ((0, 0), (23, 59))     # quiet almost all day
check("tier: routine waits in quiet hours", run(ha.push("notify.p", {"message": "r1"})) == "held")
check("tier: important waits in quiet hours too", run(ha.push("notify.p", {"message": "i1"}, "important")) == "held")
check("tier: important and time-bound goes now", run(ha.push("notify.p", {"message": "a1"}, "important", now=True))
      == "sent")
check("tier: safety goes now, in quiet hours", run(ha.push("notify.p", {"message": "s1"}, "safety")) == "sent")
ha.quiet = None
check("tier: routine stops at the limit", run(ha.push("notify.p", {"message": "r2"})) == "capped")
check("tier: important ignores the limit", run(ha.push("notify.p", {"message": "i2"}, "important")) == "sent")
check("tier: safety isn't counted", run(ha.push("notify.p", {"message": "s2"}, "safety")) == "sent" and
      ha.sent[max(ha.sent)] == 2, ha.sent)
check("tier: held ones keep their tier when flushed", run(ha.flush_held()) == 1 and "i1" in sent and "r1" not in sent,
      sent)
try:
    run(ha.push("notify.p", {}, "urgent"))
    check("tier: unknown tier refused", False)
except ValueError:
    check("tier: unknown tier refused", True)

# ---- red flags in do() --------------------------------------------------------------------------------------------
from homemind import main as M  # noqa: E402
flags = []


async def on_flag(flag, text, speaker, cid, source):
    flags.append(flag.kind)
A = types.SimpleNamespace(home=home({"light.desk": {}}), opts={}, on_red_flag=on_flag,
                          pending_reply=lambda cid, t: asyncio.sleep(0, result=None))
r = run(M.App.do(A, "I took a double dose of my pills"))
check("do(): a red flag answers in code, before any plan", r.get("red_flag") == "double_dose" and "1-800-222-1222"
      in r["answer"] and flags == ["double_dose"])
check("red flags: replies never advise a dose or a treatment",
      all(not __import__("re").search(r"\b(?:take|stop taking|mg|aspirin|dose of)\b", t, __import__("re").I)
          for t in redflags.REPLIES.values()))
check("red flags: crisis never told to the safety contact", "crisis" not in redflags.NOTIFY)

# ---- automations: ownership, watchdog, catch-up -----------------------------------------------------------------
d = tempfile.mkdtemp()
posts, notes = [], []


class EngHA:
    token, session = "t", None

    async def notify(self, title, message, nid="home_mind"):
        notes.append(message)


eng = AU.Engine(store(), EngHA(), d)


async def post(path, data):
    posts.append((path, data))
    return []
eng._post = post
eng.owned = {"automation.lights", "automation.door"}
eng.autos = {"automation.lights": {"id": "l", "name": "Evening lights", "ha_state": "on", "why_not": None,
                                   "config": {"triggers": [{"trigger": "time", "at": "07:00:00"}],
                                              "actions": [{"action": "light.turn_on",
                                                           "target": {"entity_id": "light.hall"}}]}},
             "automation.door": {"id": "d", "name": "Lock up", "ha_state": "off", "why_not": None,
                                 "config": {"triggers": [{"trigger": "time", "at": "07:00:00"}],
                                            "actions": [{"action": "lock.lock", "target": {"entity_id": "lock.f"}}]}}}
held = run(eng.reclaim())
check("reclaim: one Home Assistant is running stays there", held == ["automation.lights"] and
      not any("turn_off" in p for p, _ in posts) and notes and "hand them back" in notes[-1], (posts, notes))
n = len(notes)
run(eng.reclaim())
check("reclaim: the notice isn't repeated", len(notes) == n)
eng._label = lambda eid, add: asyncio.sleep(0)
r = run(eng.take_back())
check("take_back: a person hands it back", r["taken_over"] == ["automation.lights"] and not eng.held_by_ha and
      eng.autos["automation.lights"]["ha_state"] == "off", r)

posts.clear()
eng.autos.pop("automation.home_mind_watchdog", None)
check("watchdog: installed when missing", run(eng.ensure_watchdog()) == "installed" and
      posts[-1][0] == "config/automation/config/home_mind_watchdog")
cfg = posts[-1][1]
check("watchdog: 3 missed beats, hands over by label, never takes back",
      "150" in cfg["triggers"][0]["value_template"] and cfg["actions"][0]["target"] == {"label_id": AU.LABEL})
eng.autos["automation.home_mind_watchdog"] = {"id": "wd_old", "config": {"triggers": [{"trigger": "old"}]}}
check("watchdog: an older one is updated in place", run(eng.ensure_watchdog()) == "updated" and
      posts[-1][0] == "config/automation/config/wd_old")
eng.autos["automation.home_mind_watchdog"]["config"] = dict(AU.WATCHDOG)
check("watchdog: a current one is left alone", run(eng.ensure_watchdog()) == "current")

check("safe to repeat: lights yes, a lock no",
      AU.safe_to_repeat(eng.autos["automation.lights"]["config"]) and
      not AU.safe_to_repeat(eng.autos["automation.door"]["config"]))
check("safe to repeat: a script or an opener no",
      not AU.safe_to_repeat({"actions": [{"action": "script.turn_on", "target": {"entity_id": "script.x"}}]}) and
      not AU.safe_to_repeat({"actions": [{"action": "switch.turn_on", "target": {"entity_id": "switch.garage_opener"}}]}))
check("safe to repeat: a template service no", not AU.safe_to_repeat({"actions": [{"action": "{{ x }}"}]}))

fired = []
eng.fire_orig = eng.fire
eng.running = {}
eng._run = lambda auto, trig, lock: asyncio.sleep(0, result=fired.append((auto, trig)))
eng.autos["automation.lights"]["ha_state"] = "off"
eng.autos["automation.door"]["ha_state"] = "off"
eng.tz = None
today7 = datetime.now().astimezone().replace(hour=7, minute=0, second=0, microsecond=0)
now = today7.timestamp() + 600          # 10 minutes late
eng.last_fired = {}
eng.down_since = today7.timestamp() - 3600


async def catch():
    got = eng.catch_up(now)
    await asyncio.sleep(0)
    return got
got = run(catch())
check("catch-up: a missed 7:00 is run once if safe, under 30 minutes late",
      got == ["automation.lights"] and [a for a, _ in fired] == ["automation.lights"] and fired[0][1].get("catch_up"),
      (got, fired))
check("catch-up: when it fired is on disk before it runs",
      json.load(open(os.path.join(d, "automations.json")))["last_fired"].get("automation.lights") == today7.timestamp())
fired.clear()
eng.down_since = today7.timestamp() - 3600
run(catch())
check("catch-up: never twice for the same time", fired == [], fired)
eng.last_fired = {}
eng.down_since = today7.timestamp() - 3600
fired.clear()
eng.catch_up(today7.timestamp() + 3600)            # an hour late
check("catch-up: over 30 minutes late is skipped", fired == [], fired)
eng.save()
eng2 = AU.Engine(store(), EngHA(), d)
check("restart: owned, last fired and the clock come back from disk",
      eng2.owned == eng.owned and eng2.last_fired == eng.last_fired and eng2.down_since == eng.last_tick)
eng.last_fired = {"automation.lights": today7.timestamp()}


async def fire_again():
    eng.fire("automation.lights", {"platform": "time", "id": "0", "now": today7.isoformat()})
    await asyncio.sleep(0)
run(fire_again())
check("fire: a time already fired doesn't run again", fired == [])

# ---- the safety suite -------------------------------------------------------------------------------------------
res = run(safety.run())
check("safety suite: passes in a sandbox", res["ok"] and res["passed"] >= 35, res["failed"])


class LiveHome:
    catalog = {"lock.front": {"name": "Front"}}

    def sensitive(self, e):
        return True

    def access_of(self, e):
        return "none"
res = run(safety.run(types.SimpleNamespace(home=LiveHome())))
check("safety suite: catches a hidden device that is still visible",
      not res["ok"] and any("none" in c["name"] for c in res["failed"]), [c["name"] for c in res["failed"]])

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
