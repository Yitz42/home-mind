"""Home controls (#61): the Home tab's room map and controls. One call gathers the state of every device on the map
(lights, heater, humidifier, purifier, blind, plugs, media, the 3D printer), the room readings, the weather and
forecast, the routines Home Mind runs, and a suggestion from the learned habits. Actions go only to the devices
listed here, with only the services each kind of device needs (an allowlist, so the panel can't be used to call
anything else in Home Assistant).
"""
import logging
import re
import time
from datetime import datetime

log = logging.getLogger("homemind.home")

# where things are in the bedroom (x, y: percent of the map, window wall at the top), from Yitz's room
DEVICES = [
    {"id": "all", "name": "All lights", "kind": "light", "entity": "light.bedroom_lights", "x": None, "y": None},
    {"id": "wall", "name": "Wall light (neon rope)", "kind": "light", "entity": "light.rgbic_neon_rope_lights", "x": 84, "y": 3.5},
    {"id": "under", "name": "Under-bed light", "kind": "light", "entity": "light.rgbic_pro_strip_lights", "x": 74, "y": 31},
    {"id": "desk", "name": "Desk light", "kind": "light", "entity": "light.rgbic_tv_light_bars_2", "x": 29, "y": 10.5},
    {"id": "door", "name": "Door light", "kind": "light", "entity": "light.rgbic_tv_light_bars", "x": 48.5, "y": 83},
    {"id": "storage", "name": "Storage light", "kind": "light", "entity": "light.dreamview_p1", "x": 90, "y": 70},
    {"id": "heater", "name": "Heater", "kind": "climate", "entity": "climate.bedroom_temp", "x": 48.5, "y": 92},
    {"id": "purifier", "name": "Air purifier", "kind": "fan", "entity": "fan.dyson_c2a_us_mdb9996a", "x": 9, "y": 10},
    # the humidifier is in the Comfort card; no spot on the map until Yitz says where it sits (x, y stay None)
    {"id": "humid", "name": "Humidifier", "kind": "humidifier", "entity": "humidifier.humidifier_bedroom", "x": 12, "y": 40},
    {"id": "blind", "name": "Blind", "kind": "cover", "entity": "cover.blind_tilt_47d0", "x": 50, "y": 3.5},
    {"id": "printer", "name": "3D printer (A1)", "kind": "printer", "entity": "sensor.a1_print_status", "x": 90, "y": 61},
]
PLUGS = [
    {"id": "desk_plug", "name": "Desk", "entity": "switch.tp_link_power_strip_a849_led_2"},
    {"id": "printer_plug", "name": "Printer", "entity": "switch.tp_link_power_strip_a849_plug_2"},
    {"id": "speaker", "name": "Speaker", "entity": "switch.tp_link_power_strip_a849_speaker"},
    {"id": "tv", "name": "Fire TV", "entity": "media_player.aftka_211_airplay"},
    {"id": "adaptive", "name": "Adaptive light", "entity": "switch.adaptive_lighting_bedroom"},
    {"id": "sleepmode", "name": "Light sleep mode", "entity": "switch.adaptive_lighting_sleep_mode_bedroom"},
    {"id": "halloween", "name": "Halloween", "entity": "input_boolean.halloween_mode"},
]
PRESETS = [  # the Console's light presets (Home Assistant scripts)
    {"id": "white", "name": "White", "color": "#f4f7ff", "script": "script.all_lights_white"},
    {"id": "warm", "name": "Warm", "color": "#ffb35c", "script": "script.bedroom_warm"},
    {"id": "dim", "name": "Dim", "color": "#c96f1c", "script": "script.bedroom_dim"},
    {"id": "night", "name": "Night", "color": "#ff3b3b", "script": "script.nightlight_mode"},
    {"id": "rope", "name": "Rope night", "color": "#ff890e", "script": "script.nightlight_rope"},
    {"id": "reading", "name": "Reading", "color": "#ff8c00", "script": "script.reading_light"},
]
PRINTER = {k: f"sensor.a1_{k}" for k in ("print_status", "task_name", "current_layer", "total_layer_count",
                                          "print_progress", "remaining_time", "current_stage", "nozzle_temperature",
                                          "bed_temperature", "print_weight")}
PRINTER_LIGHT = "light.a1_chamber_light"
PRINTER_COVER = "image.a1_cover_image"   # the Bambu integration's picture of the model being printed
_MINUTES = {"d": 1440, "h": 60, "min": 1, "s": 1 / 60}   # duration units Home Assistant reports
PRINTER_BUTTONS = {"pause": "button.a1_pause_printing", "resume": "button.a1_resume_printing",
                   "stop": "button.a1_stop_printing"}
FAN_NIGHT = "switch.dyson_c2a_us_mdb9996a_night_mode"
COMFORT = "input_boolean.mind_comfort_enabled"
ALARM = "input_datetime.alarm"
ROUTINES = [
    {"entity": "automation.wake_up_mon_sat", "name": "Wake up", "sub": "Mon–Sat, follows the alarm", "time": "alarm"},
    {"entity": "automation.bedtime_lights_and_heat", "name": "Bedtime", "sub": "lights and heat", "time": "10:00 PM"},
    {"entity": "automation.bedroom_lights_on_arrival", "name": "Lights on arrival", "sub": "when your phone gets home",
     "time": "arrive"},
    {"entity": "automation.wake_up_lights", "name": "Sunday wake-up lights", "sub": "weekends", "time": "Sun"},
]
BATTERIES = {"phone": "sensor.yitzs_iphone_battery_level", "laptop": "sensor.laptop_battery"}
WEATHER = "weather.forecast_home"
OUTDOOR_AQI = "sensor.bedroom_dyson_c2a_us_mdb9996a_outdoor_aqi"
SUN = "sun.sun"

# which services each kind of thing may get, and which data keys may go with them
ALLOWED = {
    "light": {"turn_on": {"brightness_pct", "rgb_color", "color_temp_kelvin"}, "turn_off": set(), "toggle": set()},
    "climate": {"set_temperature": {"temperature"}, "set_hvac_mode": {"hvac_mode"}, "set_preset_mode": {"preset_mode"}},
    "humidifier": {"turn_on": set(), "turn_off": set(), "toggle": set(), "set_humidity": {"humidity"}},
    "fan": {"turn_on": set(), "turn_off": set(), "toggle": set(), "set_percentage": {"percentage"},
            "set_preset_mode": {"preset_mode"}},
    "cover": {"set_cover_tilt_position": {"tilt_position"}, "open_cover_tilt": set(), "close_cover_tilt": set(),
              "open_cover": set(), "close_cover": set(), "stop_cover": set(), "set_cover_position": {"position"}},
    "switch": {"turn_on": set(), "turn_off": set(), "toggle": set()},
    "input_boolean": {"turn_on": set(), "turn_off": set(), "toggle": set()},
    "media_player": {"media_play_pause": set(), "media_next_track": set(), "media_previous_track": set(),
                     "volume_set": {"volume_level"}},
    "button": {"press": set()},
    "input_button": {"press": set()},
    "script": {"turn_on": set()},
    "scene": {"turn_on": set()},
    "input_datetime": {"set_datetime": {"time"}},
    "select": {"select_option": {"option"}},   # only a light's Govee scene list, and only its own options (act)
}


def controllable():
    """Every entity the panel may act on."""
    ents = {d["entity"] for d in DEVICES if d["kind"] != "printer"} | {p["entity"] for p in PLUGS}
    ents |= {p["script"] for p in PRESETS} | set(PRINTER_BUTTONS.values()) | {PRINTER_LIGHT, FAN_NIGHT, COMFORT, ALARM}
    from . import modules
    ents |= {modules.scene_select(d["entity"]) for d in DEVICES if d["kind"] == "light" and d["id"] != "all"}
    return ents


def check(entity, service, data, extra=()):
    """(domain, clean data) or raise ValueError when the action isn't allowed. extra: entities on House tiles."""
    if entity not in controllable() and entity not in extra:
        raise ValueError(f"{entity} is not on the Home controls")
    domain = entity.split(".")[0]
    keys = ALLOWED.get(domain, {}).get(service)
    if keys is None:
        raise ValueError(f"{domain}.{service} is not allowed here")
    bad = set(data or {}) - keys
    if bad:
        raise ValueError(f"not allowed with {domain}.{service}: {', '.join(sorted(bad))}")
    clean = dict(data or {})
    for k, lo, hi in (("brightness_pct", 1, 100), ("temperature", 50, 86), ("humidity", 30, 80), ("percentage", 0, 100),
                      ("tilt_position", 0, 100), ("position", 0, 100), ("color_temp_kelvin", 2000, 6500)):
        if k in clean:
            clean[k] = max(lo, min(hi, float(clean[k]) if k == "temperature" else int(float(clean[k]))))
    if "volume_level" in clean:
        clean["volume_level"] = round(max(0.0, min(1.0, float(clean["volume_level"]))), 2)
    if "time" in clean:
        m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", str(clean["time"]).strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError("time needs to look like 07:30")
        clean["time"] = f"{int(m.group(1)):02d}:{m.group(2)}:00"
    if "option" in clean:
        clean["option"] = str(clean["option"])[:120]
    if "rgb_color" in clean:
        rgb = [max(0, min(255, int(v))) for v in list(clean["rgb_color"])[:3]]
        if len(rgb) != 3:
            raise ValueError("rgb_color needs three numbers")
        clean["rgb_color"] = rgb
    return domain, clean


def _num(s, default=None):
    try:
        return float((s or {}).get("state"))
    except (TypeError, ValueError):
        return default


def _minutes(s):
    """A duration sensor as whole minutes, read in its own unit (the A1 reports hours: 2.55 h = 153 min)."""
    v = _num(s)
    if v is None:
        return None
    unit = ((s or {}).get("attributes") or {}).get("unit_of_measurement") or "min"
    return round(v * _MINUTES.get(unit, 1))


def _hex(rgb):
    try:
        return "#%02x%02x%02x" % tuple(int(v) for v in rgb[:3])
    except (TypeError, ValueError):
        return None


def _clock(t):
    """'06:30:00' -> '6:30 AM'."""
    try:
        h, m = (int(x) for x in str(t).split(":")[:2])
        return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"
    except ValueError:
        return None


def device_view(d, st):
    s = st.get(d["entity"]) or {}
    a = s.get("attributes") or {}
    state = s.get("state")
    out = {k: d[k] for k in ("id", "name", "kind", "entity", "x", "y")}
    out["available"] = state not in (None, "unavailable", "unknown") or d["kind"] == "printer"
    if d["kind"] == "light":
        b = a.get("brightness")
        out.update(on=state == "on", level=round(b / 255 * 100) if isinstance(b, (int, float)) else (100 if state == "on" else 0),
                   color=_hex(a.get("rgb_color")) if a.get("rgb_color") else None, kelvin=a.get("color_temp_kelvin"),
                   min=1, max=100)
        if d["id"] != "all":
            from . import modules
            out["scenes"] = modules.scene_info(d["entity"], st)
    elif d["kind"] == "climate":
        out.update(on=state not in ("off", None, "unavailable"), mode=state, level=a.get("temperature"),
                   current=a.get("current_temperature"), action=a.get("hvac_action"), preset=a.get("preset_mode"),
                   presets=a.get("preset_modes") or [], min=a.get("min_temp", 60), max=a.get("max_temp", 80))
    elif d["kind"] == "humidifier":
        out.update(on=state == "on", level=a.get("humidity"), current=a.get("current_humidity"),
                   min=a.get("min_humidity", 30), max=a.get("max_humidity", 70))
    elif d["kind"] == "fan":
        out.update(on=state == "on", level=a.get("percentage") or 0, preset=a.get("preset_mode"),
                   presets=a.get("preset_modes") or [], night=(st.get(FAN_NIGHT) or {}).get("state") == "on", min=0, max=100)
    elif d["kind"] == "cover":
        out.update(on=state == "open", level=a.get("current_tilt_position"), min=0, max=100)
    elif d["kind"] == "printer":
        p = {k: (st.get(e) or {}).get("state") for k, e in PRINTER.items()}
        live = p["print_status"] not in (None, "unavailable", "unknown")
        out.update(on=live and p["print_status"] in ("running", "prepare", "pause"), printer={
            "live": live, "status": p["print_status"] if live else "offline", "task": p["task_name"],
            "layer": p["current_layer"], "layers": p["total_layer_count"], "progress": _num(st.get(PRINTER["print_progress"])),
            "remaining": p["remaining_time"], "remaining_min": _minutes(st.get(PRINTER["remaining_time"])),
            "stage": p["current_stage"], "nozzle": p["nozzle_temperature"],
            "bed": p["bed_temperature"], "weight": p["print_weight"],
            "chamber": (st.get(PRINTER_LIGHT) or {}).get("state") == "on",
            "cover": _cover_version(st)})
    return out


def _cover_version(st):
    """The cover image's state (when it last changed), so the panel reloads it for a new print; None = no picture."""
    s = st.get(PRINTER_COVER) or {}
    v = s.get("state")
    return v if v not in (None, "unavailable", "unknown") else None


async def printer_cover(A):
    """(bytes, content type) of the print's preview picture, fetched from Home Assistant's image proxy, or None."""
    import aiohttp
    ha = A.ha if getattr(A, "ha", None) and A.ha.available else None
    if not ha:
        return None
    s = await ha.state(PRINTER_COVER) or {}
    tok = (s.get("attributes") or {}).get("access_token")
    if s.get("state") in (None, "unavailable", "unknown") or not tok:
        return None
    url, headers = ha._ctl(f"image_proxy/{PRINTER_COVER}")
    try:
        async with ha.session.get(url, params={"token": tok}, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            return await r.read(), r.headers.get("Content-Type", "image/png")
    except Exception:  # noqa: BLE001
        log.debug("printer cover image not reachable", exc_info=True)
        return None


def suggestion(st, model, now):
    """One nudge from the learned habits: past the usual lights-off time with lights still on."""
    offs = [h for h in (model.get("habits") or []) if h.get("state") == "off" and h.get("entity_id", "").startswith("light.")]
    if not offs:
        return None
    minute = sorted(h.get("minute") or 0 for h in offs)[len(offs) // 2]
    t = datetime.fromtimestamp(now)
    mins = t.hour * 60 + t.minute
    late = mins >= minute or mins < 4 * 60          # after the usual time, until 4 am
    lit = [d for d in DEVICES if d["kind"] == "light" and d["id"] != "all" and (st.get(d["entity"]) or {}).get("state") == "on"]
    if not late or len(lit) < 2:
        return None
    return {"title": f"Past your usual {_clock(f'{minute // 60}:{minute % 60}')} lights-off",
            "text": f"{len(lit)} lights still on. Run Bedtime?", "scene": "bedtime"}


async def build(A, now=None):
    now = now or time.time()
    ha = A.ha if getattr(A, "ha", None) and A.ha.available else None
    out = {"at": now, "devices": [], "plugs": [], "presets": [{k: p[k] for k in ("id", "name", "color")} for p in PRESETS],
           "room": {}, "weather": None, "routines": [], "suggestion": None, "comfort": None, "alarm": None,
           "batteries": {}, "available": bool(ha)}
    states = (await ha.states()) if ha else None
    st = {s["entity_id"]: s for s in (states or [])}
    out["devices"] = [device_view(d, st) for d in DEVICES]
    for p in PLUGS:
        s = st.get(p["entity"]) or {}
        out["plugs"].append({"id": p["id"], "name": p["name"], "entity": p["entity"], "state": s.get("state"),
                             "on": s.get("state") in ("on", "playing")})
    from . import today
    for k, eid in today.ROOM.items():
        out["room"][k] = _num(st.get(eid))
    out["batteries"] = {k: _num(st.get(e)) for k, e in BATTERIES.items()}
    out["comfort"] = (st.get(COMFORT) or {}).get("state") == "on"
    out["alarm"] = _clock((st.get(ALARM) or {}).get("state"))
    w = st.get(WEATHER)
    if w:
        a = w.get("attributes") or {}
        sun = (st.get(SUN) or {}).get("attributes") or {}
        out["weather"] = {"state": w.get("state"), "temp": a.get("temperature"), "humidity": a.get("humidity"),
                          "wind": a.get("wind_speed"), "wind_bearing": a.get("wind_bearing"),
                          "wind_unit": a.get("wind_speed_unit"), "pressure": a.get("pressure"),
                          "aqi": _num(st.get(OUTDOOR_AQI)), "sunrise": sun.get("next_rising"),
                          "sunset": sun.get("next_setting"), "hourly": [], "daily": []}
        out["weather"].update(await forecasts(ha))
    eng = getattr(A, "engine", None)
    summ = {a["entity_id"]: a for a in (eng.summary() if eng else [])}
    for r in ROUTINES:
        a = summ.get(r["entity"])
        if not a:
            continue
        out["routines"].append({"entity": r["entity"], "name": r["name"], "sub": r["sub"],
                                "time": out["alarm"] if r["time"] == "alarm" else r["time"],
                                "on": a["run_by"] in ("Home Mind", "Home Assistant"), "run_by": a["run_by"],
                                "supported": a["supported"]})
    model = (A.mind.store.kv_get("house_model") or {}) if getattr(A, "mind", None) else {}
    out["suggestion"] = suggestion(st, model, now)
    return out


async def forecasts(ha):
    res = {"hourly": [], "daily": []}
    for kind in ("hourly", "daily"):
        try:
            r = await ha.post_json("services/weather/get_forecasts?return_response",
                                   {"entity_id": WEATHER, "type": kind}, timeout=15)
            fc = (((r or {}).get("service_response") or {}).get(WEATHER) or {}).get("forecast") or []
        except Exception as e:  # noqa: BLE001
            log.info("forecast %s: %s", kind, e)
            fc = []
        if kind == "hourly":
            res["hourly"] = [{"t": f["datetime"], "temp": f.get("temperature"), "state": f.get("condition"),
                              "rain": f.get("precipitation_probability")} for f in fc[:24:2][:7]]
        else:
            res["daily"] = [{"t": f["datetime"], "hi": f.get("temperature"), "lo": f.get("templow"),
                             "state": f.get("condition")} for f in fc[:6]]
    return res


async def act(A, entity, service, data=None):
    from . import modules
    extra = set()
    if entity not in controllable():
        extra = modules.controllable(A)
        if entity in extra:
            why = modules.blocked(entity, await A.ha.state(entity))
            if why:
                raise ValueError(why)
    domain, clean = check(entity, service, data, extra)
    if domain == "select" and not modules.scene_ok(await A.ha.state(entity), clean.get("option")):
        raise ValueError("that scene isn't one of this light's scenes")
    dlog = getattr(A, "decisions", None)
    prior = A.home.state_now(entity) if dlog is not None and getattr(A, "home", None) else None
    if dlog is not None and hasattr(A.ha, "call_ctx"):
        status, ctx, states = await A.ha.call_ctx(domain, service, dict(clean, entity_id=entity))
    else:
        status, ctx, states = await A.ha.call(domain, service, dict(clean, entity_id=entity)), None, None
    if dlog is not None:
        try:
            ok = not (isinstance(status, int) and status >= 400)
            dlog.record(entity_id=entity, action=service, service=f"{domain}.{service}", data=clean,
                        status="done" if ok else "refused", reason=None if ok else f"Home Assistant said {status}",
                        context_id=ctx, states=states, prior=prior, channel="panel", source="panel")
        except Exception:  # noqa: BLE001 - the log must never stop the panel's buttons
            log.exception("decision log")
    return {"done": f"{domain}.{service}", "entity": entity, "data": clean}


async def scene(A, name):
    """The quick actions. Returns the actions taken."""
    ha, done = A.ha, []

    async def do(entity, service, data=None):
        try:
            done.append(await act(A, entity, service, data))
        except Exception as e:  # noqa: BLE001
            done.append({"error": str(e), "entity": entity})

    lights = [d["entity"] for d in DEVICES if d["kind"] == "light" and d["id"] != "all"]
    if name == "bedtime":
        for e in lights:
            if e != "light.rgbic_pro_strip_lights":
                await do(e, "turn_off")
        await do("light.rgbic_pro_strip_lights", "turn_on", {"brightness_pct": 5})
        await do("switch.adaptive_lighting_sleep_mode_bedroom", "turn_on")
        night = None
        if getattr(A, "comfort", None):
            try:
                night = A.comfort.status()["preferred"]["night"]
            except Exception:  # noqa: BLE001
                night = None
        if night:
            await do("climate.bedroom_temp", "set_temperature", {"temperature": night})
    elif name == "wake":
        await do("switch.adaptive_lighting_sleep_mode_bedroom", "turn_off")
        await do("light.bedroom_lights", "turn_on", {"brightness_pct": 80})
        await do("cover.blind_tilt_47d0", "open_cover_tilt")
    elif name == "focus":
        await do("switch.adaptive_lighting_sleep_mode_bedroom", "turn_off")
        await do("switch.tp_link_power_strip_a849_led_2", "turn_on")
        await do("light.rgbic_tv_light_bars_2", "turn_on", {"brightness_pct": 100})
    elif name == "alloff":
        await do("light.bedroom_lights", "turn_off")
    else:
        raise ValueError(f"unknown quick action {name}")
    if getattr(A, "mind", None):
        A.mind.store.log("home_scene", None, scene=name, actions=len(done))
    return {"scene": name, "actions": done}
