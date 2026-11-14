"""Offline test of the automation engine with real configs and a fake Home Assistant (jinja2 stands in for HA's
template engine)."""
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
    print("why not:", {e: a["why_not"] for e, a in eng.autos.items() if a["why_not"]})
    r = await eng.takeover(list(CFG)); print("takeover:", r["taken_over"].__len__(), r["skipped"])
    def change(e, **kw):
        old = json.loads(json.dumps(S[e])); S[e] = dict(S[e], **kw); S[e]["attributes"] = dict(old["attributes"], **kw.get("attributes", {}))
        eng.on_state({"entity_id": e, "old_state": old, "new_state": S[e]})
    async def settle(t=0.2): await asyncio.sleep(t)
    # 1. 22:00 time trigger fires bedtime (phone not charging -> nightlight + heat)
    import time as _t
    eng.last_tick = CLOCK[0].timestamp(); CLOCK[0] += timedelta(seconds=5)
    real = _t.time; _t.time = lambda: CLOCK[0].timestamp()
    for auto in list(eng.owned):
        for i, t in enumerate(eng._triggers(auto)):
            if t.get("trigger") == "time":
                for p in eng._time_points(t, CLOCK[0].date()):
                    if eng.last_tick < p.timestamp() <= CLOCK[0].timestamp(): eng.fire(auto, {"platform": "time", "id": t.get("id", str(i))})
    await settle(); print("1 bedtime:", [c[0] for c in CALLS]); CALLS.clear()
    # 2. charging -> lights off
    change("sensor.yitzs_iphone_battery_state", state="Charging"); await settle(); print("2 charge:", CALLS); CALLS.clear()
    # 3. night light restart: press twice quickly -> only one pending off
    change("input_button.steady_nightlight", state="t1") if "input_button.steady_nightlight" in S else None
    S["input_button.steady_nightlight"] = {"entity_id": "input_button.steady_nightlight", "state": "t0", "attributes": {}}
    change("input_button.steady_nightlight", state="t1"); await settle(0.3); change("input_button.steady_nightlight", state="t2"); await settle(1.5)
    print("3 nightlight x2:", [c[0] for c in CALLS]); CALLS.clear()
    # 4. wake: time points tomorrow
    tm = CLOCK[0].date() + timedelta(days=1)
    print("4 wake points:", [p.strftime('%H:%M') for t in eng._triggers('automation.wake_up_mon_sat') for p in eng._time_points(t, tm)])
    CLOCK[0] = datetime(2026, 11, 15, 6, 15, tzinfo=TZ)
    eng.fire("automation.wake_up_mon_sat", {"platform": "time", "id": "lights"}); await settle()
    print("4 wake ramp:", [c[1].get("brightness_pct") for c in CALLS]); CALLS.clear()
    # 5. learn: wait_for_trigger template; room warms
    S["climate.bedroom_temp"]["attributes"]["current_temperature"] = 66
    change("climate.bedroom_temp", attributes={"temperature": 72}); await settle(0.3)
    S["climate.bedroom_temp"]["attributes"]["current_temperature"] = 72
    await settle(21); print("5 learn:", CALLS); CALLS.clear()
    # 6. journal with response variable
    eng.fire("automation.journal", {"platform": "time", "id": "0"}); await settle(); print("6 journal:", CALLS); CALLS.clear()
    # 7. zone enter at 16:00
    CLOCK[0] = datetime(2026, 11, 15, 16, 0, tzinfo=TZ)
    change("device_tracker.yitzs_iphone", state="not_home", attributes={"latitude": 9, "longitude": 9})
    change("device_tracker.yitzs_iphone", state="Near Home", attributes={"latitude": 5, "longitude": 5}); await settle()
    print("7 arrival:", [c[0] for c in CALLS]); CALLS.clear()
    # 8. numeric crossing
    change("sensor.laptop_battery", state="19"); change("sensor.laptop_battery", state="18"); await settle()
    print("8 battery:", CALLS); CALLS.clear()
    # 9. for: press then wait
    S["input_button.steady_wind_down"] = {"entity_id": "input_button.steady_wind_down", "state": "a", "attributes": {}}
    change("input_button.steady_wind_down", state="b"); await settle(1.4); print("9 for:", [c[0] for c in CALLS]); CALLS.clear()
    # 10. HA copy turned back on (watchdog) -> engine must not run
    change("automation.arrival", state="on")
    change("device_tracker.yitzs_iphone", state="not_home", attributes={"latitude": 9, "longitude": 9})
    change("device_tracker.yitzs_iphone", state="Near Home", attributes={"latitude": 5, "longitude": 5}); await settle()
    print("10 HA on -> engine quiet:", CALLS)
    print("log:", [(e["name"], e["result"]) for e in eng.log])
    print("memory:", st.recall("when did bedtime last run", limit=3)[0]["body"][:160])
asyncio.run(main())
