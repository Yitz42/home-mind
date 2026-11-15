"""Controls for Home Assistant: settings change the running app, survive a restart, give way to the Configuration
tab, refuse bad values; jobs run in the background and keep their last result.

  python tests/test_control.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.control import ACTIONS, SETTINGS, Control  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class Store:
    """The two things Control needs from the real store: a key-value table and the event log."""
    def __init__(self, kv=None):
        self.kv, self.events = dict(kv or {}), []

    def kv_get(self, k, default=None):
        return self.kv.get(k, default)

    def kv_set(self, k, v):
        import json
        self.kv[k] = json.loads(json.dumps(v))       # stored as JSON, like the real one

    def log(self, type_, item_id=None, **data):
        self.events.append((type_, data))

    def stats(self):
        return {"items": 42, "versions": 3, "faded": 1, "shelves": 5}


def app(store, **opts):
    o = {"comfort": True, "home": True, "overnight_enabled": True, "morning_enabled": True, "patterns_checkins": True,
         "auto_update_packs": True, "voice_seconds": 8, "fade_days": 60, "auto_test_hours": 0,
         "morning_time": "07:30", "overnight_time": "03:00"}
    o.update(opts)
    home = object()
    calls = []

    async def slow_run():
        await asyncio.sleep(0.05)
        calls.append("overnight")
        return {"filed": 2, "turns": 5}

    async def boom():
        raise RuntimeError("model computer is off")

    A = NS(opts=o, home=home, calls=calls,
           mind=NS(store=store, home=home, VOICE_SECONDS=8.0,
                   start_test=lambda s, arm, seed=None: (calls.append(("test", s)), {"id": f"run-{s}"})[1]),
           overnight=NS(at=(3, 0), done_for=None, enabled=True, last={"at": 1_700_000_000, "filed": 2, "turns": 5,
                                                                        "ids": [1, 2]}, run=slow_run),
           morning=NS(at=(7, 30), done_for=None, enabled=True, send=boom),
           patterns=NS(checkins_on=True), convlog=NS(pending=lambda: [1, 2, 3]),
           engine=NS(owned={"automation.a", "automation.b"}))
    return A


async def main():
    st = Store()
    A = app(st)
    c = Control(A)
    c.load()
    d = c.describe()
    check("every setting described", set(d["settings"]) == set(SETTINGS))
    check("every job described", set(d["actions"]) == set(ACTIONS))
    check("switch shows the option", d["settings"]["comfort"]["value"] is True and not d["settings"]["comfort"]["overridden"])
    check("number has its range", d["settings"]["voice_seconds"]["min"] == 2 and d["settings"]["voice_seconds"]["max"] == 60)
    check("readings", d["sensors"]["items"]["value"] == 42 and d["sensors"]["pending_turns"]["value"] == 3
          and d["sensors"]["automations_owned"]["value"] == 2 and d["sensors"]["overnight_filed"]["value"] == 2)
    check("timestamp reading has a zone", (d["sensors"]["overnight_last"]["value"] or "")[-6] in "+-",
          d["sensors"]["overnight_last"]["value"])
    check("ids left out of attributes", "ids" not in d["sensors"]["overnight_last"]["attrs"])

    # settings change the running app
    c.set("home", False)
    check("home off unplugs the house", A.mind.home is None and A.opts["home"] is False)
    c.set("home", True)
    check("home on plugs it back", A.mind.home is A.home)
    check("back to the Configuration value drops the override", "home" not in st.kv.get("control_overrides", {}))
    c.set("overnight_enabled", "off")
    check("overnight switch (text value)", A.overnight.enabled is False)
    c.set("patterns_checkins", False)
    check("check-ins switch", A.patterns.checkins_on is False)
    c.set("voice_seconds", 12)
    check("voice limit", A.mind.VOICE_SECONDS == 12.0 and A.opts["voice_seconds"] == 12)
    c.set("comfort", False)
    check("comfort reads opts", A.opts["comfort"] is False)
    d = c.describe()["settings"]
    check("override marked, with the Configuration value", d["comfort"]["overridden"] and d["comfort"]["configuration"] is True)

    # bad values are refused, nothing changes
    for key, val in (("voice_seconds", 99), ("fade_days", -1), ("morning_time", "25:00"), ("morning_time", "noon")):
        try:
            c.set(key, val)
            check(f"refuses {key}={val}", False)
        except ValueError:
            check(f"refuses {key}={val}", True)
    try:
        c.set("models", [])
        check("refuses unknown setting", False)
    except KeyError:
        check("refuses unknown setting", True)
    check("refused value left the app alone", A.mind.VOICE_SECONDS == 12.0)

    # moving a daily job: later today runs today, earlier waits for tomorrow
    now = datetime.now()
    later, earlier = now + timedelta(minutes=2), now - timedelta(minutes=2)
    if later.date() == now.date() and earlier.date() == now.date():
        A.morning.done_for = now.date()
        c.set("morning_time", later.strftime("%H:%M"))
        check("moved later: runs today", A.morning.done_for is None and A.morning.at == (later.hour, later.minute))
        c.set("morning_time", earlier.strftime("%H:%M"))
        check("moved earlier: waits for tomorrow", A.morning.done_for == now.date())
    c.set("overnight_time", "4:5")
    check("time written HH:MM", A.opts["overnight_time"] == "04:05" and A.overnight.at == (4, 5))

    # a restart keeps the overrides...
    A2 = app(Store(st.kv))
    Control(A2).load()
    check("restart keeps overrides", A2.mind.VOICE_SECONDS == 12.0 and A2.overnight.enabled is False
          and A2.opts["comfort"] is False and A2.overnight.at == (4, 5))
    # ...unless the Configuration tab changed that option since
    A3 = app(Store(st.kv), voice_seconds=20)
    c3 = Control(A3)
    c3.load()
    check("Configuration change wins", A3.mind.VOICE_SECONDS == 8.0 and A3.opts["voice_seconds"] == 20,
          A3.mind.VOICE_SECONDS)
    check("its override is dropped", "voice_seconds" not in A3.mind.store.kv["control_overrides"]
          and "comfort" in A3.mind.store.kv["control_overrides"])

    # reset one, then all
    c.reset("comfort")
    check("reset one", A.opts["comfort"] is True and "comfort" not in st.kv["control_overrides"])
    c.reset()
    check("reset all", st.kv["control_overrides"] == {} and A.overnight.enabled is True and A.mind.VOICE_SECONDS == 8.0)

    # jobs run in the background; a second press while running is ignored; errors are kept, not raised
    r1 = c.run("overnight")
    r2 = c.run("overnight")
    check("job starts", r1["started"] is True)
    check("second press while running ignored", r2["started"] is False and r2["running"])
    check("running shows", c.describe()["actions"]["overnight"]["running"])
    await asyncio.sleep(0.15)
    last = c.describe()["actions"]["overnight"]["last"]
    check("job result kept", last and last["ok"] and last["result"]["filed"] == 2 and A.calls.count("overnight") == 1, last)
    c.run("morning")
    await asyncio.sleep(0.02)
    last = c.describe()["actions"]["morning"]["last"]
    check("job error kept", last and not last["ok"] and "computer is off" in last["error"], last)
    c.run("tests")
    await asyncio.sleep(0.02)
    check("tests start four suites", [x for x in A.calls if isinstance(x, tuple)] ==
          [("test", s) for s in ("recall", "update", "locate", "tasks")], A.calls)
    try:
        c.run("reboot")
        check("refuses unknown job", False)
    except KeyError:
        check("refuses unknown job", True)
    check("changes and runs logged", any(t == "control" for t, _ in st.events) and
          any(t == "control_run" for t, _ in st.events))

    print("CONTROL OK" if not FAILS else f"CONTROL FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
