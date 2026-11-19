"""Expert review regressions: the Home Assistant side (house controls, the automation engine, notifications, the
add-on options). A fake Home Assistant client stands in; no network needed.

  python tests/test_review_ha.py
"""
import asyncio
import os
import re
import sys
import tempfile
import types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: None
    m.WSMsgType = types.SimpleNamespace(TEXT=1)
    sys.modules["aiohttp"] = m
from homemind.home import Home  # noqa: E402
from homemind.ha import HA  # noqa: E402
from homemind import automations as A  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeHA:
    """Records service calls; the control switch is on; answers with an HTTP status like HA._post."""
    def __init__(self, status=200):
        self.calls, self.status = [], status

    async def state(self, eid):
        return {"state": "on"} if eid == "input_boolean.mind_control_enabled" else None

    async def call(self, dom, svc, data):
        self.calls.append((dom, svc, data))
        return self.status


def home(controls, catalog, ha=None, **opts):
    st = Store(tempfile.mktemp())
    h = Home(st, ha or FakeHA(), dict(opts, home_controls=controls))
    h.catalog = {e: dict({"name": e, "domain": e.split(".")[0], "state": "off", "attrs": {}, "kind": "device"}, **c)
                 for e, c in catalog.items()}
    return h


async def err(coro):
    try:
        return await coro
    except (ValueError, PermissionError) as e:
        return e


async def controls():
    # option home_fan_max 0 is a real limit (the schema allows 0), not "unset"
    h = home(["fan.*"], {"fan.bed": {}}, home_fan_max=0)
    await h.act("fan.bed", "speed", 80)
    check("home_fan_max 0 keeps the fan at 0%", h.ha.calls[-1][2].get("percentage") == 0, h.ha.calls)

    # a model saying "50%" must not set the light to 1%
    h = home(["light.*"], {"light.desk": {}})
    await h.act("light.desk", "brightness", "50%")
    check("brightness '50%' sets 50", h.ha.calls[-1][2].get("brightness_pct") == 50, h.ha.calls)
    await h.act("light.desk", "brightness", 250)
    check("brightness above 100 is clamped", h.ha.calls[-1][2].get("brightness_pct") == 100)
    e = await err(h.act("light.desk", "brightness", None))
    check("brightness with no number is refused (not 1%)", isinstance(e, ValueError), e)
    e = await err(h.act("light.desk", "brightness", "inf"))
    check("brightness inf is refused cleanly", isinstance(e, ValueError), e)
    h2 = home(["fan.*"], {"fan.bed": {}})
    e = await err(h2.act("fan.bed", "speed", None))
    check("fan speed with no number doesn't switch the fan off", isinstance(e, ValueError) and not h2.ha.calls, e)

    # glob patterns on the control list
    h = home(["light.kitchen_*"], {"light.kitchen_main": {}, "light.bedroom": {}})
    check("glob pattern on the control list", h.allowed("light.kitchen_main") and not h.allowed("light.bedroom"))
    check("domain.* still works", home(["script.*"], {}).allowed("script.bedtime"))

    # locks, alarms and garage doors need the person's yes
    h = home(["cover.*", "lock.*"], {"cover.garage_door": {"attrs": {"device_class": "garage"}},
                                     "cover.blinds": {"attrs": {"device_class": "blind"}},
                                     "lock.front_door": {}})
    e = await err(h.act("cover.garage_door", "toggle"))
    check("garage door won't move without confirm", isinstance(e, PermissionError) and not h.ha.calls, e)
    e = await err(h.act("lock.front_door", "unlock"))
    check("lock won't unlock without confirm", isinstance(e, PermissionError) and not h.ha.calls, e)
    # 0.27: a spoken yes isn't enough for these any more: they need a fresh approval on the phone
    e = await err(h.act("cover.garage_door", "open", confirm=True))
    check("a spoken yes can't open the garage (phone approval)", isinstance(e, PermissionError) and not h.ha.calls, e)
    await h.act("cover.garage_door", "open", approved=True)
    check("approved garage open uses cover.open_cover", h.ha.calls[-1][:2] == ("cover", "open_cover"), h.ha.calls)
    await h.act("lock.front_door", "lock", approved=True)
    check("approved lock uses lock.lock", h.ha.calls[-1][:2] == ("lock", "lock"), h.ha.calls)
    await h.act("cover.blinds", "off")
    check("blinds 'off' closes them (cover has no turn_off)", h.ha.calls[-1][:2] == ("cover", "close_cover"),
          h.ha.calls)
    e = await err(h.act("lock.front_door", "on", approved=True))
    check("lock.turn_on (no such service) is refused", isinstance(e, ValueError), e)

    # the planner (/api/do) never confirms: a model can't open the garage
    src = open(os.path.join(ROOT, "home_mind", "rootfs", "app", "homemind", "main.py")).read()
    do = src[src.index("async def do(self"):src.index("async def push_counts")]
    # (r6: a spoken "yes" in the same conversation confirms it, in App.pending_reply; do() itself never does)
    check("/api/do acts without confirm", not re.search(r"confirm\s*=", do), re.findall(r"confirm\s*=.*", do))

    # unavailable things and HA refusing a call
    h = home(["light.*"], {"light.porch": {"state": "unavailable"}})
    e = await err(h.act("light.porch", "on"))
    check("an unavailable light is refused, not filed as done", isinstance(e, ValueError) and not h.ha.calls, e)
    h = home(["light.*"], {"light.desk": {}}, ha=FakeHA(status=400))
    e = await err(h.act("light.desk", "on"))
    n = h.store.db.execute("SELECT COUNT(*) FROM items WHERE shelf='home/actions'").fetchone()[0]
    check("HA refusing the call is an error, not a filed action", isinstance(e, ValueError) and n == 0, (e, n))


async def notify_target():
    ha = HA(None)
    ha.calls = []

    async def call(dom, svc, data):
        ha.calls.append((dom, svc))
        return 200
    ha.call = call
    await ha.push("mobile_app_pixel", {"message": "hi"})
    check("notify target without 'notify.' still pushes", ha.calls == [("notify", "mobile_app_pixel")], ha.calls)


async def engine():
    S = {"sensor.temp": {"entity_id": "sensor.temp", "state": "20", "attributes": {}},
         "input_number.limit": {"entity_id": "input_number.limit", "state": "25", "attributes": {}},
         "input_boolean.x": {"entity_id": "input_boolean.x", "state": "off", "attributes": {}},
         "automation.hot": {"entity_id": "automation.hot", "state": "on", "attributes": {"id": "hot"}},
         "automation.dis": {"entity_id": "automation.dis", "state": "on", "attributes": {"id": "dis"}}}
    CFG = {"hot": {"triggers": [{"trigger": "numeric_state", "entity_id": "sensor.temp", "above": "input_number.limit"}],
                   "conditions": [{"condition": "numeric_state", "entity_id": "sensor.temp",
                                   "above": "input_number.limit"}],
                   "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.a"}}]},
           "dis": {"triggers": [{"trigger": "state", "entity_id": "input_boolean.x", "to": "on", "enabled": False},
                                {"trigger": "state", "entity_id": "input_boolean.x", "to": "off"}],
                   "conditions": [{"condition": "state", "entity_id": "input_boolean.x", "state": "nope",
                                   "enabled": False}],
                   "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.b"}}]}}
    calls = []
    eng = A.Engine(Store(tempfile.mktemp()), types.SimpleNamespace(token="t", session=None), tempfile.mkdtemp())

    async def _get(path):
        if path == "states":
            return list(S.values())
        m = re.match(r"config/automation/config/(.+)", path)
        return CFG.get(m.group(1)) if m else None

    async def _post(path, payload):
        calls.append((path, payload))
        return []

    async def _label(eid, add):
        pass
    eng._get, eng._post, eng._label = _get, _post, _label
    await eng.load()
    await eng.takeover(["automation.hot", "automation.dis"])
    calls.clear()

    def change(e, state):
        old = dict(S[e])
        S[e] = dict(S[e], state=state)
        eng.on_state({"entity_id": e, "old_state": old, "new_state": S[e]})
    crashed = None
    try:
        change("sensor.temp", "30")
    except Exception as e:  # noqa: BLE001
        crashed = e
    await asyncio.sleep(0.1)
    check("numeric_state above an input_number doesn't crash the event loop", crashed is None, crashed)
    check("... and fires when the reading crosses the helper's value",
          any("light/turn_on" in p and d.get("entity_id") == "light.a" for p, d in calls), calls)
    calls.clear()
    change("input_boolean.x", "on")
    await asyncio.sleep(0.1)
    check("a disabled trigger doesn't fire", not calls, calls)
    change("input_boolean.x", "off")
    await asyncio.sleep(0.1)
    check("a disabled condition is skipped", any(d.get("entity_id") == "light.b" for p, d in calls), calls)
    check("state condition with for is not supported (left to HA)",
          A.unsupported({"triggers": [{"trigger": "state", "entity_id": "a.b"}],
                         "conditions": [{"condition": "state", "entity_id": "a.b", "state": "on",
                                         "for": "00:05:00"}], "actions": []}) is not None)


def options():
    import yaml
    t = yaml.safe_load(open(os.path.join(ROOT, "home_mind", "translations", "en.yaml")))
    c = yaml.safe_load(open(os.path.join(ROOT, "home_mind", "config.yaml")))
    check("translations/en.yaml is valid YAML covering every option", set(t["configuration"]) == set(c["schema"]))
    src = open(os.path.join(ROOT, "home_mind", "rootfs", "app", "homemind", "main.py")).read()
    DEFAULTS = eval(re.search(r"DEFAULTS = (\{.*?\n)\n", src, re.S).group(1))  # main.py needs aiohttp.web
    check("every default is in the schema", not set(DEFAULTS) - set(c["schema"]), set(DEFAULTS) - set(c["schema"]))
    bad = []
    for k, v in c["schema"].items():
        if isinstance(v, str) and v.startswith("match("):
            if not re.fullmatch(v[6:-1], str(c["options"][k])):
                bad.append(k)
            if re.fullmatch(v[6:-1], "7:30 AM"):
                bad.append(k + " takes 7:30 AM")
    check("time options: schema accepts the defaults and rejects 7:30 AM", not bad, bad)
    check("time options are checked by the schema",
          str(c["schema"]["morning_time"]).startswith("match(") and str(c["schema"]["quiet_end"]).startswith("match("))


async def main():
    await controls()
    await notify_target()
    await engine()
    try:
        import yaml  # noqa: F401
        options()
    except ImportError:
        print("skip options (no yaml)")


asyncio.run(main())
if FAILS:
    print(f"{len(FAILS)} failed")
    sys.exit(1)
