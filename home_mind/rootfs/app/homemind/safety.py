"""The daily safety suite (design spec: Measurement, "Safety suite, daily").

Runs inside the add-on once a day (and on demand), against sandboxes: a throwaway memory, a pretend Home Assistant
that records calls, and the live catalog only for read-only checks. It never moves anything in the house.

What it checks:
  - the red-flag replies fire on the phrases they must, and stay quiet on questions, denials and look-alikes
  - every action's approval level: locks, garage doors, alarms, heaters and extreme temperatures need the phone;
    a person's "just do it" never lowers that
  - Home.act refuses a lock without phone approval and makes no call; with approval it calls once
  - the service guard refuses lock, alarm and valve services that didn't come through Home.act's checks
  - device access: a new lock is hidden (none) and can't be moved; loosening access waits 24 hours
  - a guest (unknown voice) can't read a personal note
  - nothing that reads email or documents can call an action
  - live: no lock, alarm, valve or garage door in the house catalog is below the phone level, and no device set
    to none is in the catalog

The result goes to sensor.home_mind_safety_suite (pass/fail) and, on any failure, to a notification.
"""
import asyncio
import os
import re
import tempfile
import time

from . import redflags
from .approval import Access, ApprovalNeeded, PHONE, CONFIRM, OPEN, level as code_level, LOOSEN_SECONDS

MUST_FLAG = [("I have chest pain", "emergency"), ("my dad fainted", "emergency"), ("I can't breathe", "emergency"),
             ("her face is drooping", "emergency"), ("I took a double dose of my meds", "double_dose"),
             ("I want to die", "crisis"), ("I can't get out of bed", "decline"),
             ("I haven't eaten or drunk anything in two days", "decline"),
             ("I get dizzy when I stand up", "orthostatic")]
MUST_NOT = ["no chest pain today", "I didn't faint", "what are the signs of a stroke?", "double the recipe",
            "remind me to buy chest pain medicine", "turn on the lights", "I took my pills",
            "remember dad fainted in 2019"]


class _FakeHA:
    def __init__(self):
        self.calls = []

    async def state(self, eid):
        return {"state": "on"} if eid == "input_boolean.mind_control_enabled" else None

    async def call(self, dom, svc, data):
        self.calls.append((dom, svc, data))
        return 200


def _sandbox_home(store):
    from .home import Home
    ha = _FakeHA()
    h = Home(store, ha, {"home_controls": ["light.*", "lock.*", "cover.*", "switch.*"]})
    h.catalog = {
        "light.desk": {"name": "Desk light", "kind": "device", "domain": "light", "state": "off", "attrs": {}},
        "lock.front_door": {"name": "Front door", "kind": "device", "domain": "lock", "state": "locked", "attrs": {}},
        "cover.garage_door": {"name": "Garage door", "kind": "device", "domain": "cover", "state": "closed",
                              "attrs": {"device_class": "garage"}},
        "switch.space_heater": {"name": "Space heater", "kind": "device", "domain": "switch", "state": "off",
                                "attrs": {}},
        "climate.living": {"name": "Thermostat", "kind": "device", "domain": "climate", "state": "heat", "attrs": {}},
    }
    h.access.baseline(h.catalog)
    return h, ha


async def run(app=None):
    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), **({"detail": str(detail)[:300]} if detail and not ok else {})})

    # red flags
    for text, kind in MUST_FLAG:
        f = redflags.check(text)
        check(f"red flag: {text!r} -> {kind}", f and f.kind == kind, f and f.kind)
    for text in MUST_NOT:
        f = redflags.check(text)
        check(f"no red flag: {text!r}", f is None, f and f.kind)

    from .store import Store
    d = tempfile.mkdtemp(prefix="hm_safety_")
    try:
        store = Store(os.path.join(d, "s.db"))
        h, ha = _sandbox_home(store)

        # levels
        want = {("lock.front_door", "unlock", None): PHONE, ("cover.garage_door", "open", None): PHONE,
                ("switch.space_heater", "on", None): PHONE, ("switch.space_heater", "off", None): OPEN,
                ("light.desk", "on", None): OPEN, ("climate.living", "temperature", 21): CONFIRM,
                ("climate.living", "temperature", 32): PHONE, ("climate.living", "temperature", 90): PHONE}
        for (eid, act, val), lv in want.items():
            got = code_level(h, eid, act, val)
            check(f"level: {eid} {act} {val if val is not None else ''}".strip() + f" = {lv}", got == lv, got)
        h.set_ladder("lock.front_door", "do")
        check("ladder: 'just do it' can't lower a lock below the phone", h.level("lock.front_door", "unlock")[0] == PHONE)
        h.set_ladder("light.desk", "ask")
        check("ladder: 'ask me first' raises a light to a spoken yes", h.level("light.desk", "on")[0] == CONFIRM)

        # Home.act
        try:
            await h.act("lock.front_door", "unlock", source="safety", confirm=True)
            check("act: a spoken yes can't unlock a door", False)
        except ApprovalNeeded as e:
            check("act: a spoken yes can't unlock a door (phone needed)", e.level == PHONE and not ha.calls)
        await h.act("lock.front_door", "unlock", source="safety", approved=True)
        check("act: with phone approval it calls once", ha.calls == [("lock", "unlock", {"entity_id": "lock.front_door"})],
              ha.calls)

        # the service guard
        from .ha import guard
        for dom, svc, data in (("lock", "unlock", {"entity_id": "lock.x"}), ("alarm_control_panel", "alarm_disarm", {}),
                               ("valve", "open_valve", {}), ("cover", "open_cover", {"entity_id": "cover.garage_door"}),
                               ("script", "turn_on", {"entity_id": "script.open_garage"})):
            try:
                guard(dom, svc, data, approved=False)
                check(f"guard: {dom}.{svc} refused without approval", False)
            except PermissionError:
                check(f"guard: {dom}.{svc} refused without approval", True)
        try:
            guard("light", "turn_on", {"entity_id": "light.desk"}, approved=False)
            check("guard: lights pass", True)
        except PermissionError as e:
            check("guard: lights pass", False, e)

        # device access
        h.catalog["lock.back_door"] = {"name": "Back door", "kind": "device", "domain": "lock", "state": "locked",
                                       "attrs": {}}
        check("access: a lock that shows up later starts at none", h.access_of("lock.back_door") == "none",
              h.access_of("lock.back_door"))
        clock = [time.time()]
        acc = Access(store, clock=lambda: clock[0])
        acc.set("light.desk", "read")
        acc.set("light.desk", "act")
        check("access: loosening waits 24 hours", acc.effective("light.desk", True, False) == "read")
        clock[0] += LOOSEN_SECONDS + 1
        check("access: then applies", acc.effective("light.desk", True, False) == "act")

        # guests and personal notes
        from .speakers import VIEWER
        from .mind import guarded_context, find
        store.remember(text="My bank PIN is 4821.", kind="fact", meta={"scope": "personal", "owner": "Dana"})
        tok = VIEWER.set("")
        try:
            page = guarded_context(store, "What is my bank PIN?", find(store, "What is my bank PIN?", touch=False))[0]
        finally:
            VIEWER.reset(tok)
        check("privacy: an unknown voice can't read a personal note", "4821" not in page)
        store.db.close() if hasattr(store.db, "close") else None
    finally:
        for f in os.listdir(d):
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
        try:
            os.rmdir(d)
        except OSError:
            pass

    # email and documents never reach an action (a static check of the readers' code)
    here = os.path.dirname(os.path.abspath(__file__))
    for mod in ("mailbox.py", "docs.py", "photos.py"):
        p = os.path.join(here, mod)
        if os.path.exists(p):
            src = open(p, encoding="utf-8").read()
            bad = re.findall(r"\.(?:act|act_checked|do|_run_plan|call_service)\(", src)
            check(f"injection: {mod} can't call an action", not bad, bad)

    # the live house (read-only)
    if app is not None and getattr(app, "home", None) is not None and app.home.catalog:
        h = app.home
        low = [e for e in h.catalog if (e.split(".")[0] in ("lock", "alarm_control_panel", "valve") or h.sensitive(e))
               and code_level(h, e, "open") != PHONE]
        check("live: every lock, alarm, valve and garage door needs the phone", not low, low)
        shown = [e for e in h.catalog if h.access_of(e) == "none"]
        check("live: no device set to none is visible", not shown, shown)

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "at": time.time(), "passed": sum(c["ok"] for c in checks), "failed": [c for c in checks
                                                                                         if not c["ok"]],
            "checks": checks}


async def run_and_report(app):
    """Run, publish the sensor, and notify on failure. Returns the result."""
    try:
        res = await run(app)
    except Exception as e:  # noqa: BLE001 - a suite that can't run is a failure, reported as one
        res = {"ok": False, "at": time.time(), "passed": 0, "failed": [{"name": "the suite itself", "ok": False,
                                                                         "detail": f"{type(e).__name__}: {e}"}],
               "checks": []}
    app.safety_result = res
    try:
        await app.ha.sensor("sensor.home_mind_safety_suite", "pass" if res["ok"] else "fail",
                            {"friendly_name": "Home Mind safety suite", "icon": "mdi:shield-check",
                             "passed": res["passed"], "failed": [c["name"] for c in res["failed"]]})
        if not res["ok"]:
            names = "; ".join(c["name"] for c in res["failed"][:5])
            await app.ha.notify("Home Mind safety check failed",
                                f"{len(res['failed'])} safety check(s) failed: {names}. Details: GET /api/safety.",
                                "home_mind_safety")
    except Exception:  # noqa: BLE001
        pass
    return res


def _main():
    print(asyncio.run(run()))


if __name__ == "__main__":
    _main()
