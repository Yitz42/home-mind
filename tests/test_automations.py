"""Offline test of the automation engine with real configs and a fake Home Assistant (jinja2 stands in for HA's
template engine). Exits 1 if any check fails."""
import asyncio, sys, tempfile, json, re
from datetime import datetime, timedelta
import os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'home_mind', 'rootfs', 'app'))
import jinja2
from zoneinfo import ZoneInfo
from homemind.store import Store
from homemind import automations as A

TZ = ZoneInfo("America/New_York")
CLOCK = [datetime(2026, 11, 14, 21, 59, 58, tzinfo=TZ)]
CFG = {
 "automation.bedtime_lights_and_heat": {"alias": "Bedtime lights and heat", "description": "bed", "triggers": [{"at": "22:00:00", "id": "bedtime", "trigger": "time"}, {"entity_id": "device_tracker.yitzs_iphone", "id": "arrived", "to": "home", "trigger": "state"}, {"entity_id": "sensor.yitzs_iphone_battery_state", "id": "charge", "not_from": ["unavailable", "unknown"], "not_to": ["unavailable", "unknown"], "trigger": "state"}], "conditions": [{"condition": "state", "entity_id": "device_tracker.yitzs_iphone", "state": "home"}, {"after": "22:00:00", "before": "05:45:00", "condition": "time"}], "actions": [{"if": [{"condition": "trigger", "id": ["bedtime", "arrived"]}], "then": [{"action": "climate.set_temperature", "data": {"hvac_mode": "heat", "temperature": "{{ states('input_number.sleep_temperature') | float(68) }}"}, "target": {"entity_id": "climate.bedroom_temp"}}]}, {"choose": [{"conditions": [{"condition": "state", "entity_id": "sensor.yitzs_iphone_battery_state", "state": ["Charging", "Full"]}], "sequence": [{"action": "light.turn_off", "target": {"entity_id": "light.bedroom_lights"}}]}, {"conditions": [{"condition": "state", "entity_id": "sensor.yitzs_iphone_battery_state", "state": "Not Charging"}], "sequence": [{"action": "script.nightlight_mode"}]}]}], "mode": "queued"},
 "automation.steady_night_light": {"alias": "Steady night light", "triggers": [{"entity_id": "input_button.steady_nightlight", "id": "light", "not_from": ["unavailable", "unknown"], "not_to": ["unavailable", "unknown"], "trigger": "state"}, {"entity_id": "input_button.steady_lights_off", "id": "off", "not_from": ["unavailable", "unknown"], "not_to": ["unavailable", "unknown"], "trigger": "state"}], "conditions": [{"condition": "state", "entity_id": "device_tracker.yitzs_iphone", "state": "home"}], "actions": [{"choose": [{"conditions": [{"condition": "trigger", "id": "light"}], "sequence": [{"action": "script.nightlight_mode", "continue_on_error": True}, {"delay": {"seconds": 1}}, {"action": "light.turn_off", "continue_on_error": True, "target": {"entity_id": "light.bedroom_lights"}}]}, {"conditions": [{"condition": "trigger", "id": "off"}], "sequence": [{"action": "light.turn_off", "target": {"entity_id": "light.bedroom_lights"}}]}]}], "mode": "restart"},
 "automation.wake_up_mon_sat": {"alias": "Wake up", "triggers": [{"at": "sensor.wake_heat_start", "id": "heat", "trigger": "time"}, {"at": {"entity_id": "input_datetime.alarm", "offset": "-00:30:00"}, "id": "heat", "trigger": "time"}, {"at": {"entity_id": "input_datetime.alarm", "offset": "-00:15:00"}, "id": "lights", "trigger": "time"}], "conditions": [{"condition": "or", "conditions": [{"condition": "and", "conditions": [{"condition": "template", "value_template": "{{ states('input_datetime.steady_wake_date') == now().date() | string }}"}, {"condition": "state", "entity_id": "input_boolean.steady_wake_on", "state": "on"}]}]}, {"condition": "state", "entity_id": "device_tracker.yitzs_iphone", "state": "home"}], "actions": [{"choose": [{"conditions": [{"condition": "trigger", "id": "heat"}], "sequence": [{"action": "climate.set_temperature", "data": {"hvac_mode": "heat", "temperature": "{{ states('input_number.wake_temperature') | float(72) }}"}, "target": {"entity_id": "climate.bedroom_temp"}}]}, {"conditions": [{"condition": "trigger", "id": "lights"}], "sequence": [{"repeat": {"count": 3, "sequence": [{"action": "light.turn_on", "data": {"brightness_pct": "{{ [1, (100 * (repeat.index / 3) ** 2) | round(0) | int] | max }}"}, "target": {"entity_id": ["light.a", "light.b"]}}]}}]}]}], "mode": "single"},
 "automation.learn": {"alias": "Learn", "triggers": [{"attribute": "temperature", "entity_id": "climate.bedroom_temp", "trigger": "state"}], "conditions": [], "actions": [{"variables": {"start_temp": "{{ state_attr('climate.bedroom_temp', 'current_temperature') | float(0) }}", "target": "{{ states('input_number.wake_temperature') | float(72) }}"}}, {"continue_on_timeout": True, "timeout": {"seconds": 30}, "wait_for_trigger": [{"trigger": "template", "value_template": "{{ state_attr('climate.bedroom_temp', 'current_temperature') | float(0) >= target - 0.5 }}"}, {"entity_id": "climate.bedroom_temp", "to": "off", "trigger": "state"}]}, {"variables": {"gained": "{{ state_attr('climate.bedroom_temp', 'current_temperature') | float(0) - start_temp }}"}}, {"if": [{"condition": "template", "value_template": "{{ gained >= 2 }}"}], "then": [{"action": "input_number.set_value", "data": {"value": "{{ gained }}"}, "target": {"entity_id": "input_number.x"}}]}], "mode": "single"},
 "automation.journal": {"alias": "Journal", "triggers": [{"at": "23:30:00", "trigger": "time"}], "actions": [{"action": "calendar.get_events", "data": {"end_date_time": "x"}, "response_variable": "journal", "target": {"entity_id": "calendar.mind_journal"}}, {"variables": {"entries": "{{ journal['calendar.mind_journal'].events | rejectattr('summary', 'match', 'Daily summary') | list }}"}}, {"if": [{"condition": "template", "value_template": "{{ entries | length > 0 }}"}], "then": [{"action": "calendar.create_event", "data": {"summary": "Daily summary ({{ entries | length }})"}, "target": {"entity_id": "calendar.mind_journal"}}]}], "mode": "single"},
 "automation.arrival": {"alias": "Arrival", "triggers": [{"entity_id": "device_tracker.yitzs_iphone", "event": "enter", "trigger": "zone", "zone": "zone.near_home"}], "conditions": [{"after": "05:45:00", "before": "22:00:00", "condition": "time"}], "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.bedroom_lights"}}], "mode": "single"},
 "automation.battery": {"alias": "Battery", "triggers": [{"below": 20, "entity_id": "sensor.laptop_battery", "id": "low", "trigger": "numeric_state"}], "actions": [{"action": "notify.mobile_app_yitzs_iphone", "data": {"message": "Battery {{ states('sensor.laptop_battery') }}%"}}], "mode": "single"},
 "automation.winddown": {"alias": "Wind", "triggers": [{"entity_id": "input_button.steady_wind_down", "for": {"seconds": 1}, "id": "winddown", "trigger": "state"}], "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.bedroom_lights"}}], "mode": "single"},
 "automation.voice": {"alias": "Voice", "triggers": [{"command": ["I'm cold"], "trigger": "conversation"}], "actions": []},
}
S = {
 "device_tracker.yitzs_iphone": {"state": "home", "attributes": {"latitude": 1, "longitude": 1}},
 "sensor.yitzs_iphone_battery_state": {"state": "Not Charging", "attributes": {}},
 "input_number.sleep_temperature": {"state": "68.0", "attributes": {}},
 "input_number.wake_temperature": {"state": "72.0", "attributes": {}},
 "climate.bedroom_temp": {"state": "heat", "attributes": {"current_temperature": 66, "temperature": 70}},
 "input_datetime.alarm": {"state": "06:30:00", "attributes": {"has_time": True, "has_date": False}},
 "sensor.wake_heat_start": {"state": "2026-11-15T10:50:00+00:00", "attributes": {}},
 "input_datetime.steady_wake_date": {"state": "2026-11-15", "attributes": {}},
 "input_boolean.steady_wake_on": {"state": "on", "attributes": {}},
 "zone.near_home": {"state": "0", "attributes": {"friendly_name": "Near Home", "latitude": 5, "longitude": 5, "radius": 300}},
 "sensor.laptop_battery": {"state": "50", "attributes": {}},
}
for e, c in CFG.items(): S[e] = {"state": "off", "attributes": {"friendly_name": c["alias"], "id": e}}
for e in S: S[e]["entity_id"] = e
CALLS = []
env = jinja2.Environment()
env.tests["match"] = lambda v, p: bool(re.match(p, str(v)))
env.filters["float"] = lambda v, d=0.0: (lambda: float(v))() if re.fullmatch(r"-?[\d.]+", str(v)) else d
def tmpl(t, variables):
    g = dict(variables, states=lambda e: S.get(e, {}).get("state", "unknown"),
             state_attr=lambda e, a: S.get(e, {}).get("attributes", {}).get(a),
             now=lambda: CLOCK[0])
    return env.from_string(t).render(**g)
class FakeHA: token = "t"; session = None
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


async def main():
    st = Store(tempfile.mktemp())
    eng = A.Engine(st, FakeHA(), tempfile.mkdtemp())
    async def _get(path):
        if path == "config": return {"time_zone": "America/New_York"}
        if path == "states": return list(S.values())
        m = re.match(r"config/automation/config/(.+)", path)
        return CFG.get(m.group(1)) if m else None
    async def _post(path, payload):
        if path == "template": return tmpl(payload["template"], payload.get("variables", {}))
        CALLS.append((path, payload))
        if "get_events" in path:
            return {"service_response": {"calendar.mind_journal": {"events": [{"summary": "went for a walk"}, {"summary": "Daily summary (2)"}]}}}
        return []
    async def _label(eid, add): pass
    eng._get, eng._post, eng._label = _get, _post, _label
    eng.now = lambda: CLOCK[0]
    await eng.load()
    check("load: only the conversation trigger is unsupported",
          {e: a["why_not"] for e, a in eng.autos.items() if a["why_not"]} == {"automation.voice": "trigger conversation"})
    S["automation.gone"] = {"entity_id": "automation.gone", "state": "unavailable", "attributes": {"friendly_name": "Gone", "id": "automation.gone"}}
    CFG["automation.gone"] = {"alias": "Gone", "triggers": [{"at": "01:00:00", "trigger": "time"}], "actions": []}
    await eng.load()
    r = await eng.takeover(list(CFG))
    offs = [c for c in CALLS if c[0] == "services/automation/turn_off"]
    check("takeover: the 8 supported ones are taken", len(r["taken_over"]) == 8 and set(eng.owned) == set(r["taken_over"]), r)
    check("takeover: unsupported and unavailable ones are skipped, not turned off",
          set(r["skipped"]) == {"automation.voice", "automation.gone"} and "unavailable" in r["skipped"]["automation.gone"]
          and not any(c[1]["entity_id"] in ("automation.voice", "automation.gone") for c in offs), r["skipped"])
    check("takeover: each Home Assistant copy is turned off, its run left to finish",
          sorted(c[1]["entity_id"] for c in offs) == sorted(r["taken_over"]) and all(c[1]["stop_actions"] is False for c in offs))
    CALLS.clear()
    def change(e, **kw):
        old = json.loads(json.dumps(S[e])); S[e] = dict(S[e], **kw); S[e]["attributes"] = dict(old["attributes"], **kw.get("attributes", {}))
        eng.on_state({"entity_id": e, "old_state": old, "new_state": S[e]})
    async def settle(t=0.2): await asyncio.sleep(t)
    # 1. 22:00 time trigger fires bedtime (phone not charging -> nightlight + heat)
    eng.last_tick = CLOCK[0].timestamp(); CLOCK[0] += timedelta(seconds=5)
    for auto in list(eng.owned):
        for i, t in enumerate(eng._triggers(auto)):
            if t.get("trigger") == "time":
                for p in eng._time_points(t, CLOCK[0].date()):
                    if eng.last_tick < p.timestamp() <= CLOCK[0].timestamp(): eng.fire(auto, {"platform": "time", "id": t.get("id", str(i))})
    await settle()
    check("1 bedtime: heat to the sleep temperature, then the night light",
          [c[0] for c in CALLS] == ["services/climate/set_temperature", "services/script/nightlight_mode"]
          and CALLS[0][1].get("temperature") == 68.0 and CALLS[0][1].get("hvac_mode") == "heat", CALLS)
    CALLS.clear()
    # 2. charging -> lights off (choose branch on the new state; the trigger id isn't bedtime, so no heat)
    change("sensor.yitzs_iphone_battery_state", state="Charging"); await settle()
    check("2 charging: only the lights go off", CALLS == [("services/light/turn_off", {"entity_id": "light.bedroom_lights"})], CALLS)
    CALLS.clear()
    # 3. night light restart: press twice quickly -> only one pending off
    S["input_button.steady_nightlight"] = {"entity_id": "input_button.steady_nightlight", "state": "t0", "attributes": {}}
    change("input_button.steady_nightlight", state="t1"); await settle(0.3); change("input_button.steady_nightlight", state="t2"); await settle(1.5)
    check("3 restart mode: two presses, one light-off", [c[0] for c in CALLS] ==
          ["services/script/nightlight_mode", "services/script/nightlight_mode", "services/light/turn_off"], CALLS)
    CALLS.clear()
    # 4. wake: time points tomorrow
    tm = CLOCK[0].date() + timedelta(days=1)
    pts = [p.strftime('%H:%M') for t in eng._triggers('automation.wake_up_mon_sat') for p in eng._time_points(t, tm)]
    check("4 wake: sensor time and alarm offsets", pts == ["05:50", "06:00", "06:15"], pts)
    CLOCK[0] = datetime(2026, 11, 15, 6, 15, tzinfo=TZ)
    eng.fire("automation.wake_up_mon_sat", {"platform": "time", "id": "lights"}); await settle()
    ramp = [c[1].get("brightness_pct") for c in CALLS]
    check("4 wake ramp: repeat with templated brightness", ramp == [11, 44, 100] and
          all(c[1].get("entity_id") == ["light.a", "light.b"] for c in CALLS), CALLS)
    CALLS.clear()
    # 5. learn: wait_for_trigger template (polled every 20 s); the room warms
    S["climate.bedroom_temp"]["attributes"]["current_temperature"] = 66
    change("climate.bedroom_temp", attributes={"temperature": 72}); await settle(0.3)
    check("5 learn: waits while the room is cold", CALLS == [], CALLS)
    S["climate.bedroom_temp"]["attributes"]["current_temperature"] = 72
    await settle(21)
    check("5 learn: the gain is written once the room is warm",
          CALLS == [("services/input_number/set_value", {"value": 6.0, "entity_id": "input_number.x"})], CALLS)
    CALLS.clear()
    # 6. journal with response variable
    eng.fire("automation.journal", {"platform": "time", "id": "0"}); await settle()
    check("6 journal: response variable read, own summaries left out",
          [c[0] for c in CALLS] == ["services/calendar/get_events?return_response", "services/calendar/create_event"]
          and CALLS[1][1]["summary"] == "Daily summary (1)", CALLS)
    CALLS.clear()
    # 7. zone enter at 16:00
    CLOCK[0] = datetime(2026, 11, 15, 16, 0, tzinfo=TZ)
    change("device_tracker.yitzs_iphone", state="not_home", attributes={"latitude": 9, "longitude": 9}); await settle()
    check("7 leaving the zone doesn't fire an enter trigger", CALLS == [], CALLS)
    change("device_tracker.yitzs_iphone", state="Near Home", attributes={"latitude": 5, "longitude": 5}); await settle()
    check("7 arrival: entering the zone turns the light on", [c[0] for c in CALLS] == ["services/light/turn_on"], CALLS)
    CALLS.clear()
    # 8. numeric crossing: fires when it crosses, not again while it stays below
    change("sensor.laptop_battery", state="19"); change("sensor.laptop_battery", state="18"); await settle()
    check("8 battery: one notice on crossing below 20",
          CALLS == [("services/notify/mobile_app_yitzs_iphone", {"message": "Battery 18%"})], CALLS)
    CALLS.clear()
    # 9. for: the state must hold; a change before the time is up cancels it
    S["input_button.steady_wind_down"] = {"entity_id": "input_button.steady_wind_down", "state": "a", "attributes": {}}
    change("input_button.steady_wind_down", state="b"); await settle(0.4)
    change("input_button.steady_wind_down", state="c"); await settle(0.4)
    check("9 for: not yet", CALLS == [], CALLS)
    await settle(1.0)
    check("9 for: fires once, after the last change held", [c[0] for c in CALLS] == ["services/light/turn_off"], CALLS)
    CALLS.clear()
    # 10. HA copy turned back on (watchdog) -> engine must not run
    change("automation.arrival", state="on")
    change("device_tracker.yitzs_iphone", state="not_home", attributes={"latitude": 9, "longitude": 9})
    change("device_tracker.yitzs_iphone", state="Near Home", attributes={"latitude": 5, "longitude": 5}); await settle()
    check("10 HA runs it again (watchdog): the engine stays quiet", CALLS == [], CALLS)
    # 11. after a restart, owned ones Home Assistant is running stay there (0.27: only a person hands them back)
    held = await eng.reclaim()
    check("11 reclaim leaves the Home Assistant copy on and says so",
          CALLS == [] and held == ["automation.arrival"] and eng.autos["automation.arrival"]["ha_state"] == "on", CALLS)
    eng._label = lambda eid, add: asyncio.sleep(0)
    await eng.take_back()
    check("11 a person taking it back turns the Home Assistant copy off",
          ("services/automation/turn_off", {"entity_id": "automation.arrival", "stop_actions": False}) in CALLS
          and eng.autos["automation.arrival"]["ha_state"] == "off", CALLS)
    CALLS.clear()
    CALLS.clear()
    # 12. hand back: a running automation is stopped and Home Assistant's copy turned on
    eng.fire("automation.learn", {"platform": "state", "id": "0"}); await settle()
    running = eng.running.get("automation.learn")
    r = await eng.release(["automation.learn", "automation.voice"]); await settle()
    check("12 release hands back only what it runs", r["handed_back"] == ["automation.learn"] and
          "automation.learn" not in eng.owned, r)
    check("12 release turns the Home Assistant copy on",
          CALLS == [("services/automation/turn_on", {"entity_id": "automation.learn"})], CALLS)
    check("12 release stops the run in progress", running is not None and running.done() and "automation.learn" not in eng.running)
    CALLS.clear()
    log = [(e["name"], e["result"]) for e in eng.log]
    check("log: every finished run recorded as done", log == [(n, "done") for n in (
        "Bedtime lights and heat", "Bedtime lights and heat", "Steady night light", "Wake up", "Learn", "Journal",
        "Arrival", "Battery", "Wind")], log)
    check("memory: each taken-over automation is described in a note",
          "Bedtime lights and heat" in st.recall("when did bedtime last run", limit=3)[0]["body"])
    print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
    sys.exit(1 if FAILS else 0)
asyncio.run(main())
