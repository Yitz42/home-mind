"""House and automations (0.22): new trigger types and the event action in the automation engine (#31), creating an
automation switched off, habits and suggestions (#41, #19), unusual readings and how notable a change is (#39, #40),
drafted automations checked before installing (#42), and the evening brief (#51). No Home Assistant needed.

  python tests/test_house.py
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
except ImportError:                      # the engine only needs these names when it talks to Home Assistant
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: None
    m.WSMsgType = types.SimpleNamespace(TEXT=1)
    sys.modules["aiohttp"] = m
import jinja2  # noqa: E402
from homemind import automations as A, autodraft, house  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.evening import Evening  # noqa: E402
from homemind.home import Home  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---- #31 engine ---------------------------------------------------------------------------------------------------
S, CALLS = {}, []
CFG = {
    "automation.on_event": {"alias": "On event", "triggers": [{"trigger": "event", "event_type": "hm_ping",
                                                              "event_data": {"room": "kitchen"}}],
                            "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.k"}}]},
    "automation.every15": {"alias": "Every 15", "triggers": [{"trigger": "time_pattern", "minutes": "/15"}],
                           "actions": [{"action": "switch.toggle", "target": {"entity_id": "switch.p"}}]},
    "automation.sunset": {"alias": "Sunset", "triggers": [{"trigger": "sun", "event": "sunset", "offset": "-00:20:00"}],
                          "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.porch"}}]},
    "automation.sunrise": {"alias": "Sunrise", "triggers": [{"trigger": "sun", "event": "sunrise"}],
                           "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.porch"}}]},
    "automation.hot": {"alias": "Hot", "triggers": [{"trigger": "template",
                                                    "value_template": "{{ states('sensor.t') | float(0) > 30 }}"}],
                       "actions": [{"event": "hm_hot", "event_data": {"t": "{{ states('sensor.t') }}"}}]},
    "automation.dusk": {"alias": "Dusk", "triggers": [{"trigger": "sun", "event": "dusk"}], "actions": []},
}
env = jinja2.Environment()
env.filters["float"] = lambda v, d=0.0: float(v) if re.fullmatch(r"-?[\d.]+", str(v)) else d


class FakeHA:
    token, session = "t", None


async def engine_tests():
    st = Store(tempfile.mktemp())
    eng = A.Engine(st, FakeHA(), tempfile.mkdtemp())
    now = datetime.now().astimezone()
    sunset, sunrise = now + timedelta(hours=1), now + timedelta(hours=10)
    S.update({e: {"entity_id": e, "state": "off", "attributes": {"friendly_name": c["alias"], "id": e.split(".")[1]}}
              for e, c in CFG.items()})
    S["sun.sun"] = {"entity_id": "sun.sun", "state": "above_horizon",
                    "attributes": {"next_setting": sunset.isoformat(), "next_rising": sunrise.isoformat()}}
    S["sensor.t"] = {"entity_id": "sensor.t", "state": "25", "attributes": {}}
    created = {}

    async def _get(path):
        if path == "config":
            return {"time_zone": "America/New_York"}
        if path == "states":
            return list(S.values()) + [{"entity_id": f"automation.{k}", "state": "on", "attributes": {"id": k}}
                                       for k in created]
        m = re.match(r"config/automation/config/(.+)", path)
        return next((c for e, c in CFG.items() if e.split(".")[1] == m.group(1)), None) if m else None

    async def _post(path, payload):
        if path == "template":
            return env.from_string(payload["template"]).render(
                states=lambda e: S.get(e, {}).get("state", "unknown"), **payload.get("variables", {}))
        if path.startswith("config/automation/config/"):
            created[path.rsplit("/", 1)[1]] = payload
        CALLS.append((path, payload))
        return {}
    eng._get, eng._post = _get, _post
    await eng.load()
    check("new trigger types are supported", all(eng.autos[e]["why_not"] is None for e in CFG if e != "automation.dusk"),
          {e: eng.autos[e]["why_not"] for e in CFG})
    check("a sun event other than sunrise/sunset is left to Home Assistant", eng.autos["automation.dusk"]["why_not"] == "sun trigger dusk")
    check("a template trigger with 'for' is left to Home Assistant",
          A.unsupported({"triggers": [{"trigger": "template", "value_template": "x", "for": 5}], "actions": []}) is not None)
    eng.owned = set(CFG) - {"automation.dusk"}

    async def settle():
        await asyncio.sleep(0.05)
        for t in list(eng.running.values()):
            await t

    def calls(svc):
        return [c for c in CALLS if c[0] == svc]

    eng.on_event("hm_ping", {"room": "garage"})
    await settle()
    check("event trigger: other data doesn't fire", not calls("services/light/turn_on"))
    eng.on_event("hm_ping", {"room": "kitchen", "by": "x"})
    await settle()
    check("event trigger: matching data fires", len(calls("services/light/turn_on")) == 1)
    check("event types to subscribe to", eng._event_types() == {"hm_ping"})
    base = datetime.now().replace(minute=14, second=58, microsecond=0).timestamp()
    eng._tick(base, base + 5)
    await settle()
    eng._tick(base + 5, base + 60)
    await settle()
    check("time_pattern /15 fires once at :15", len(calls("services/switch/toggle")) == 1, calls("services/switch/toggle"))
    t = (sunset - timedelta(minutes=20)).timestamp()
    eng._tick(t - 3, t + 2)
    await settle()
    check("sun: sunset with a -20 minute offset", len([c for c in calls("services/light/turn_on")]) == 2)
    eng._tick(sunrise.timestamp() - 30, sunrise.timestamp() - 5)
    S["sun.sun"]["attributes"]["next_rising"] = (sunrise + timedelta(days=1)).isoformat()
    eng.states["sun.sun"] = S["sun.sun"]
    eng._tick(sunrise.timestamp() - 5, sunrise.timestamp() + 2)
    await settle()
    check("sun: sunrise still fires after sun.sun moves on to tomorrow", len(calls("services/light/turn_off")) == 1)
    await eng._templates()
    await settle()
    check("template: false at first, no fire", not calls("events/hm_hot"))
    S["sensor.t"]["state"] = "35"
    await eng._templates("sensor.t")
    await settle()
    check("template turning true fires; the event action renders its data",
          calls("events/hm_hot") == [("events/hm_hot", {"t": 35})], calls("events/hm_hot"))
    await eng._templates("sensor.t")
    await settle()
    check("template: stays true, no second fire", len(calls("events/hm_hot")) == 1)
    r = await eng.create({"alias": "Desk lamp off at 11", "triggers": [{"trigger": "time", "at": "23:00:00"}],
                          "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.desk"}}]})
    check("create: written to Home Assistant and switched off", r["id"] == "home_mind_desk_lamp_off_at_11" and
          r["entity_id"] and calls("services/automation/turn_off")[-1][1]["entity_id"] == r["entity_id"], r)


# ---- #41 #19 habits, #39 baselines, #40 salience -------------------------------------------------------------------
def house_tests():
    t = {"now": time.time()}
    st = Store(tempfile.mktemp(), clock=lambda: t["now"])
    mid = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=28)

    def at(d, h, m):
        return (mid + timedelta(days=d, hours=h, minutes=m)).timestamp()
    import random
    rng = random.Random(4)
    for d in range(28):
        for eid, stt, h, m in (("light.desk_lamp", "off", 23, 5 + rng.randint(-8, 8)), ("lock.front", "locked", 22, rng.randint(0, 20))):
            st.remember(text=f"x: {stt}", title=f"{eid.split('.')[1].replace('_', ' ').title()}: {stt}", kind="fact",
                        shelf="home/devices", source="home", at=at(d, h, m), meta={"entity_id": eid, "state": stt})
        for k in range(48):
            v = 40 + rng.gauss(0, 1)
            st.remember(text=f"h: {v:.1f}", title=f"Bedroom humidity: {v:.1f}", kind="fact", shelf="home/readings",
                        source="home", at=at(d, k // 2, 30 * (k % 2)), meta={"entity_id": "sensor.hum", "state": f"{v:.1f}"})
    t["now"] = at(28, 9, 0)
    model = house.learn(st, t["now"])
    texts = [house.habit_text(h) for h in model["habits"]]
    check("a habit is found and said plainly", any(x.startswith("Desk Lamp usually is turned off around 11:0") for x in texts), texts)
    check("a suggestion is drafted for it", any("light.desk_lamp" in json.dumps(s["config"]) for s in model["suggestions"]))
    check("locks are never suggested", not any("lock.front" in json.dumps(s["config"]) for s in model["suggestions"]))
    sug = next(s for s in model["suggestions"] if "light.desk_lamp" in json.dumps(s["config"]))
    check("the suggestion passes the checker", autodraft.check(sug["config"], {"light.desk_lamp"}, {"light": {"turn_off"}}) == [])
    n = house.file_habits(st, model)
    again = house.file_habits(st, model)
    check("habits are filed as notes once", n == again and st.db.execute(
        "SELECT COUNT(*) FROM items WHERE source='habits' AND status='active'").fetchone()[0] == n)
    # the watcher: routine change, unusual reading (two in a row), a notice
    home = Home(st, None)
    home.model = model
    home.catalog = {"light.desk_lamp": {"name": "Desk lamp", "kind": "device", "domain": "light", "unit": "", "state": "off",
                                        "attrs": {}},
                    "sensor.hum": {"name": "Bedroom humidity", "kind": "reading", "domain": "sensor", "unit": "%",
                                   "state": "40", "attrs": {}},
                    "lock.front": {"name": "Front door", "kind": "device", "domain": "lock", "unit": "", "state": "locked",
                                   "attrs": {}}}
    m = home._salience("light.desk_lamp", home.catalog["light.desk_lamp"], "off", at(28, 23, 4))
    check("a change at its habit time is routine", m.get("routine") and m["salience"] <= 0, m)
    m1 = home._salience("sensor.hum", home.catalog["sensor.hum"], "75", t["now"])
    m2 = home._salience("sensor.hum", home.catalog["sensor.hum"], "76", t["now"])
    check("one odd reading is not enough, two in a row are", "unusual" not in m1 and "higher than usual" in m2.get("unusual", ""), (m1, m2))
    check("an unusual reading makes a phone notice", home.notices and "Bedroom humidity is 76" in home.notices[-1][1], home.notices)
    home._salience("sensor.hum", home.catalog["sensor.hum"], "77", t["now"])
    check("only one notice a day for the same thing", len(home.notices) == 1)
    m = home._salience("lock.front", home.catalog["lock.front"], "unlocked", at(28, 3, 0))
    check("a lock unlocked at 3 am is worth a notice", m["salience"] >= house.NOTIFY and len(home.notices) == 2, m)
    # routine house notes rank lower
    st3 = Store(tempfile.mktemp())
    for x in ["The drill is in the red toolbox.", "Mia waters the ferns on Sunday.", "The boiler was serviced in May."]:
        st3.remember(text=x)
    # kind note, so the two aren't taken as versions of one fact (versions are ordered by time, not weight)
    a = st3.remember(text="The desk lamp: off.", title="Desk lamp: off", kind="note", shelf="home/devices", source="home",
                     meta={"entity_id": "light.desk_lamp", "state": "off", "routine": True})
    b = st3.remember(text="The desk lamp: off.", title="Desk lamp: off", kind="note", shelf="home/devices", source="home",
                     meta={"entity_id": "light.desk_lamp", "state": "off", "salience": 2})
    sc = {h["id"]: h["score"] for h in st3.recall("desk lamp off", limit=40)}
    check("a routine house note ranks below a notable one", sc.get(a["id"], 0) < sc.get(b["id"], 0), (sc.get(a["id"]), sc.get(b["id"])))
    # #51 evening brief
    st.remember(text="Front door: unlocked.", title="Front door: unlocked", kind="fact", shelf="home/devices",
                source="home", at=t["now"] - 60, meta={"entity_id": "lock.front", "state": "unlocked", "salience": 5,
                                                       "why": ["lock unlocked", "unusual time"]})
    st.remember(text="Desk lamp: off.", title="Desk lamp: off", kind="fact", shelf="home/devices", source="home",
                at=t["now"] - 50, meta={"entity_id": "light.desk_lamp", "state": "off", "salience": 0, "routine": True})
    st.remember(text="The spare key is in the blue bowl.", source="api", at=t["now"] - 40)
    st.remember(text="Ate tuna chickpea penne.", title="Ate tuna chickpea penne", shelf="body/meals", source="api",
                at=t["now"] - 30)
    st.remember(text="Fog 3, slept ok.", title="Check-in: fog 3", shelf="body/fog", source="check-in", at=t["now"] - 20)
    st.kv_set("house_model", model)
    text = Evening(st, None).build(t["now"])
    check("evening brief: the notable change, not the routine one", "Front door: unlocked (lock unlocked, unusual time)" in text
          and "Desk lamp: off" not in text, text)
    check("evening brief: what was remembered and the suggestions waiting", "Remembered 1: The spare key" in text and "penne" not in text and
          "Check-in" not in text and
          "Suggested automations" in text, text)
    st2 = Store(tempfile.mktemp())
    check("evening brief: nothing to say, nothing sent", Evening(st2, None).build() == "")


# ---- #42 drafted automations --------------------------------------------------------------------------------------
async def draft_tests():
    class Model:
        def __init__(self, reply):
            self.reply = reply

        async def chat(self, system, messages, **k):
            self.seen = messages[0]["content"]
            return {"text": self.reply, "ms": 5}
    states = [{"entity_id": "light.desk", "attributes": {"friendly_name": "Desk"}},
              {"entity_id": "lock.front_door", "attributes": {"friendly_name": "Front door"}},
              {"entity_id": "cover.garage", "attributes": {"friendly_name": "Garage door"}}]
    svcs = [{"domain": "light", "services": {"turn_off": {}, "turn_on": {}}}, {"domain": "lock", "services": {"unlock": {}, "lock": {}}},
            {"domain": "cover", "services": {"open_cover": {}}}]
    good = '```json\n{"alias": "Desk off", "triggers": [{"trigger": "time", "at": "23:00:00"}], "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.desk"}}]}\n```'
    mdl = Model(good)
    r = await autodraft.draft(mdl, "turn the desk light off at 11 pm", states, svcs)
    check("a draft is parsed out of a code block and passes", r["config"]["alias"] == "Desk off" and r["problems"] == [], r)
    check("the model is shown the house's entities and services", "light.desk (Desk)" in mdl.seen and "light: turn_off, turn_on" in mdl.seen)
    r = await autodraft.draft(Model("I can't do that, sorry"), "x", states, svcs)
    check("a reply with no JSON is refused", r["problems"] == ["the model's reply wasn't JSON"])
    bad = json.dumps({"alias": "Garage", "triggers": [{"trigger": "time", "at": "07:00:00"}],
                      "actions": [{"action": "cover.open_cover", "target": {"entity_id": "cover.garage"}}]})
    r = await autodraft.draft(Model(bad), "open the garage at 7", states, svcs)
    check("opening the garage needs the PIN", any("opens the house" in p for p in r["problems"]), r["problems"])
    ents, sv, names, _, _ = autodraft.house_lists(states, svcs)
    check("with the right PIN it passes", autodraft.check(r["config"], ents, sv, pin="9", given_pin="9", names=names) == [])
    check("a template's entity isn't taken for a missing one", autodraft.check(
        {"triggers": [{"trigger": "template", "value_template": "{{ is_state('sensor.nope', 'on') }}"}],
         "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.desk"}}]}, ents, sv) == [])
    check("an automation's id comes from its name", autodraft.slug("Desk lamp off at 11!") == "home_mind_desk_lamp_off_at_11")
    # Ask / Talk automation-request detection & plain-English summary
    check("detects 'Can you set up an automation to...'",
          bool(autodraft.automation_request("Can you set up an automation to turn off the Bedroom Lights and the Heater Bedroom Temp when I leave?")))
    check("detects 'Create an automation that...'",
          bool(autodraft.automation_request("Create an automation that turns off the desk lamp at 11 pm")))
    check("detects 'Automate turning off...'",
          bool(autodraft.automation_request("Automate turning off the porch light at sunrise")))
    check("ignores questions about existing automations",
          autodraft.automation_request("What automations do I have?") is None
          and autodraft.automation_request("Did the bedroom automation run last night?") is None
          and autodraft.automation_request("Turn off the bedroom light") is None)
    r_ok = await autodraft.draft(Model(good), "turn the desk light off at 11 pm", states, svcs)
    sum_ok = autodraft.draft_summary(r_ok)
    check("draft_summary: clean draft is installable without PIN",
          sum_ok["installable"] and not sum_ok["needs_pin"] and sum_ok["alias"] == "Desk off", sum_ok)
    txt_ok = autodraft.format_draft(r_ok)
    check("format_draft: screen summary shows alias, trigger, action, and install hint",
          "Desk off" in txt_ok and "23:00:00" in txt_ok and "light.turn_off" in txt_ok and "Install switched off" in txt_ok, txt_ok)
    txt_v = autodraft.format_draft(r_ok, voice=True)
    check("format_draft: voice summary is compact", "\n" not in txt_v and "Desk off" in txt_v, txt_v)
    # PIN configured -> needs_pin=True, installable=True (UI prompts for PIN on click)
    r_pin = await autodraft.draft(Model(bad), "open the garage at 7", states, svcs, pin="1234")
    sum_pin = autodraft.draft_summary(r_pin)
    check("draft_summary: opening draft with configured PIN is installable with needs_pin",
          sum_pin["installable"] and sum_pin["needs_pin"], sum_pin)
    # PIN NOT configured -> hard problem, installable=False
    r_nopin = await autodraft.draft(Model(bad), "open the garage at 7", states, svcs, pin="")
    sum_nopin = autodraft.draft_summary(r_nopin)
    check("draft_summary: opening draft without configured PIN is not installable",
          (not sum_nopin["installable"]) and sum_nopin["needs_pin"], sum_nopin)
    # Missing entity -> hard problem, installable=False
    broken = json.dumps({"alias": "Broken", "triggers": [{"trigger": "time", "at": "07:00:00"}],
                         "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.nonexistent"}}]})
    r_bad = await autodraft.draft(Model(broken), "turn off nonexistent light", states, svcs)
    sum_bad = autodraft.draft_summary(r_bad)
    check("draft_summary: unknown entity marks draft not installable",
          not sum_bad["installable"] and any("no such entity" in p for p in sum_bad["problems"]), sum_bad)
    check("format_draft: reports fix needed on broken draft",
          "Needs a fix" in autodraft.format_draft(r_bad), autodraft.format_draft(r_bad))


asyncio.run(engine_tests())
house_tests()
asyncio.run(draft_tests())
print(f"\n{len(FAILS)} failed" if FAILS else "\nALL HOUSE AND AUTOMATION CHECKS PASSED")
sys.exit(1 if FAILS else 0)
