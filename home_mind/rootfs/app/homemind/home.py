"""The rest of the house, for the brain: what Home Assistant reports, and gated controls.

Reading
  - Every 5 minutes the useful entities are found in Home Assistant (lights, fan, humidifier, heater, switches,
    scripts, scenes, room/air/watch/phone readings, weather). Noise is skipped: light segments, music modes,
    connection/IP/firmware sensors, printer diagnostics while it is off, test sensors.
  - Changes go into memory as dated notes, one series per thing, so "is the desk light on?", "what was the
    bedroom humidity this morning?" and "when did the fan last run?" work like any other question:
      devices: every on/off (or mode) change;  readings: when they move enough (1°, 3% humidity, ...) and at most
      every 30 minutes each.
  - Questions that name something in the house also get its live state ("HOME NOW") in the model's context.

Controls
  - Only through act(), and only when input_boolean.mind_control_enabled is on: that switch is the breaking point,
    turning it off stops every control at once without touching anything else.
  - Only things on the control list (option home_controls; default: the main lights, the Dyson fan, the
    humidifier, the desk outlets, scripts and scenes). The heater and climate stay with the comfort driver.
  - Limits: brightness 1-100%, fan speed up to option home_fan_max, humidity between input_number.mind_humidity_min
    and max, at most 20 actions a minute. Every action is filed in memory (home/actions).
"""
import json
import logging
import re
import time
from datetime import datetime

log = logging.getLogger("homemind.home")

SKIP = re.compile(r"segment|music mode|music sensitivity|dreamview|\bled\b|connectivity|cloud connection|connection "
                  r"(mode|status)|ip address|wi-?fi|firmware|version|refresh|reconnect|purge|request platform|favorite "
                  r"current song|estimated distance|serial|mqtt|backup|^sun |home mind|location permission|app version|"
                  r"kiosk|assist microphone|calibration|fault|filter type|scheduled events|^a1 |a1_|makerworld|"
                  r"gcode|nozzle|spool|printable|skipped", re.I)
DEVICE_DOMAINS = ("light", "fan", "humidifier", "climate", "switch", "cover", "media_player", "lock", "vacuum")
RUN_DOMAINS = ("script", "scene")
DEFAULT_CONTROLS = ["light.bedroom_lights", "light.rgbic_tv_light_bars", "light.rgbic_tv_light_bars_2",
                    "light.dreamview_p1", "light.rgbic_neon_rope_lights", "light.rgbic_pro_strip_lights",
                    "light.h6056_520c", "fan.dyson_c2a_us_mdb9996a", "humidifier.humidifier_bedroom",
                    "switch.tp_link_power_strip_a849_led_2", "switch.tp_link_power_strip_a849_speaker",
                    "script.*", "scene.*"]
GATE = "input_boolean.mind_control_enabled"
STEP = {"temperature": 1.0, "humidity": 3.0, "pm25": 5.0, "aqi": 5.0, "battery": 10.0}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class Home:
    def __init__(self, store, ha, opts=None):
        self.store, self.ha = store, ha
        o = opts or {}
        self.controls = o.get("home_controls") or DEFAULT_CONTROLS
        self.fan_max = int(o.get("home_fan_max") or 100)
        self.catalog = {}          # entity_id -> {"name", "kind", "domain", "unit", "state"}
        self.seen = {}             # entity_id -> (value, time filed)
        self.actions = []

    # ---- what is in the house -----------------------------------------------------------------------------------
    def _keep(self, s):
        eid, a = s["entity_id"], s.get("attributes") or {}
        dom = eid.split(".")[0]
        name = a.get("friendly_name") or eid
        if SKIP.search(name) or SKIP.search(eid):
            return None
        if dom in DEVICE_DOMAINS or dom in RUN_DOMAINS:
            if dom == "switch" and re.search(r"adaptive lighting|auto update|continuous monitoring", name, re.I):
                return None
            return {"kind": "run" if dom in RUN_DOMAINS else "device"}
        if dom == "input_number" and eid.startswith(("input_number.steady_", "input_number.mind_",
                                                     "input_number.sleep_", "input_number.wake_")):
            return {"kind": "reading"}
        if dom == "input_boolean" or dom == "weather" or dom == "person":
            return {"kind": "device" if dom == "input_boolean" else "reading"}
        if dom == "sensor":
            if s["state"] in ("unavailable", "unknown"):
                return None
            if a.get("unit_of_measurement") or a.get("device_class") in ("temperature", "humidity", "battery"):
                return {"kind": "reading"}
            if re.search(r"air quality category|dominant pollutant|battery state|activity", name, re.I):
                return {"kind": "reading"}
        return None

    async def refresh(self):
        states = await self.ha.states()
        if not states:
            return 0
        cat = {}
        for s in states:
            k = self._keep(s)
            if not k:
                continue
            a = s.get("attributes") or {}
            name = re.sub(r"^Dyson C2A-US-MDB9996A", "Dyson", a.get("friendly_name") or s["entity_id"]).strip()
            name = re.sub(r"^Yitz \(Wyze scale\) ", "", name)
            cat[s["entity_id"]] = dict(k, name=name, domain=s["entity_id"].split(".")[0], state=s["state"],
                                       unit=a.get("unit_of_measurement") or "", attrs=a)
        self.catalog = cat
        return len(cat)

    def describe(self, eid):
        c = self.catalog[eid]
        st, a = c["state"], c["attrs"]
        extra = []
        if c["domain"] == "light" and st == "on" and a.get("brightness") is not None:
            extra.append(f"brightness {round(a['brightness'] / 2.55)}%")
        if c["domain"] == "fan" and st == "on" and a.get("percentage") is not None:
            extra.append(f"speed {a['percentage']}%")
        if c["domain"] == "climate":
            if a.get("current_temperature") is not None:
                extra.append(f"room {a['current_temperature']}°")
            if a.get("temperature") is not None:
                extra.append(f"set to {a['temperature']}°")
        if c["domain"] == "humidifier" and a.get("humidity") is not None:
            extra.append(f"target {a['humidity']}%")
        if c["domain"] == "weather":
            for k in ("temperature", "humidity", "wind_speed"):
                if a.get(k) is not None:
                    extra.append(f"{k.replace('_', ' ')} {a[k]}")
        v = _f(st)
        val = (f"{v:.1f}".rstrip("0").rstrip(".") if v is not None else st) + (f" {c['unit']}" if c["unit"] else "")
        return f"{c['name']}: {val}" + (f" ({', '.join(extra)})" if extra else "")

    # ---- into memory ----------------------------------------------------------------------------------------------
    def _step(self, c):
        n = (c["name"] + " " + c["unit"]).lower()
        for k, s in STEP.items():
            if k in n or (k == "temperature" and "°" in n):
                return s
        return 0.0

    def log_changes(self, first=False):
        """File what changed since last time. On the first pass, file the current state of everything once (so the
        brain knows how things stand) unless it was already filed today."""
        now = time.time()
        filed = 0
        for eid, c in self.catalog.items():
            if c["kind"] == "run":
                continue
            st = c["state"]
            if st in ("unavailable", "unknown"):
                continue
            prev = self.seen.get(eid)
            if c["kind"] == "device":
                sig = st + ("" if c["domain"] != "climate" else f"/{c['attrs'].get('temperature')}")
                if prev and prev[0] == sig:
                    continue
                self.seen[eid] = (sig, now)
                if prev is None and not first:
                    continue
            else:
                v = _f(st)
                if prev is not None:
                    pv, pt = prev
                    if now - pt < 1800:
                        continue
                    if v is not None and _f(pv) is not None and abs(v - _f(pv)) < max(self._step(c), 1e-9):
                        continue
                    if v is None and pv == st:
                        continue
                self.seen[eid] = (st, now)
                if prev is None and not first:
                    continue
            if prev is None and self._filed_today(eid):
                continue
            when = datetime.now().strftime("%a %b %d %Y %I:%M %p").replace(" 0", " ")
            text = f"{when}: {self.describe(eid)}."
            self.store.remember(text=text, title=f"{c['name']}: {self.describe(eid).split(': ', 1)[1]}",
                                kind="fact", shelf="home/" + ("devices" if c["kind"] == "device" else "readings"),
                                tags=["home", c["domain"]], source="home",
                                meta={"series": "home:" + eid, "entity_id": eid, "state": st})
            filed += 1
        return filed

    def _filed_today(self, eid):
        start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
        return self.store.db.execute("SELECT 1 FROM items WHERE source='home' AND meta LIKE ? AND created >= ?",
                                     (f'%"entity_id": "{eid}"%', start)).fetchone() is not None

    # ---- live state for questions ----------------------------------------------------------------------------------
    def matching(self, question, limit=8):
        """Things in the house a question names ("desk light", "humidity", "fan", "HRV")."""
        from .store import words
        qw = set(words(question))
        qw |= {w[:-1] for w in qw if w.endswith("s")}
        scored = []
        for eid, c in self.catalog.items():
            nw = set(words(c["name"])) - {"yitz", "s", "iphone", "dyson", "bedroom", "steady"}
            hit = qw & nw
            if hit:
                scored.append((len(hit) / len(nw | hit), eid))
        scored.sort(reverse=True)
        return [e for _, e in scored[:limit]]

    def now_lines(self, question):
        ids = self.matching(question)
        if not ids:
            return ""
        return "HOME NOW (live from Home Assistant, " + datetime.now().strftime("%a %H:%M") + "):\n" + \
            "\n".join("- " + self.describe(e) for e in ids)

    # ---- controls ---------------------------------------------------------------------------------------------------
    def allowed(self, eid):
        dom = eid.split(".")[0]
        return eid in self.controls or f"{dom}.*" in self.controls

    def controllable(self):
        return [{"entity_id": e, "name": c["name"], "state": c["state"]} for e, c in sorted(self.catalog.items())
                if self.allowed(e)]

    async def act(self, entity_id, action, value=None, source="api"):
        """action: on, off, toggle, brightness (1-100), color (name), speed (fan %), humidity (%), run."""
        if entity_id not in self.catalog:
            m = self.matching(entity_id, 1)
            if not m:
                raise ValueError(f"don't know {entity_id!r}")
            entity_id = m[0]
        if not self.allowed(entity_id):
            raise PermissionError(f"{entity_id} is not on the control list")
        gate = await self.ha.state(GATE)
        if not gate or gate.get("state") != "on":
            raise PermissionError(f"controls are off ({GATE} is not on)")
        now = time.time()
        self.actions = [t for t in self.actions if now - t < 60]
        if len(self.actions) >= 20:
            raise PermissionError("too many actions in the last minute")
        dom = entity_id.split(".")[0]
        data = {"entity_id": entity_id}
        a = (action or "").lower()
        if a == "run" or dom in RUN_DOMAINS:
            svc = (dom, "turn_on")
        elif a in ("on", "off", "toggle"):
            svc = (dom if dom != "climate" else "climate", f"turn_{a}" if a != "toggle" else "toggle")
        elif a == "brightness" and dom == "light":
            v = max(1, min(100, int(_f(value) or 0)))
            svc, data["brightness_pct"] = ("light", "turn_on"), v
        elif a == "color" and dom == "light":
            svc, data["color_name"] = ("light", "turn_on"), str(value).lower().strip()
        elif a == "speed" and dom == "fan":
            v = max(0, min(self.fan_max, int(_f(value) or 0)))
            svc, data["percentage"] = ("fan", "set_percentage"), v
        elif a == "humidity" and dom == "humidifier":
            lo = _f((await self.ha.state("input_number.mind_humidity_min") or {}).get("state")) or 30
            hi = _f((await self.ha.state("input_number.mind_humidity_max") or {}).get("state")) or 55
            v = max(lo, min(hi, _f(value) or lo))
            svc, data["humidity"] = ("humidifier", "set_humidity"), v
        else:
            raise ValueError(f"can't {action} {entity_id}")
        status = await self.ha.call(svc[0], svc[1], data)
        self.actions.append(now)
        name = self.catalog[entity_id]["name"]
        said = f"{a}" + (f" {value}" if value not in (None, "") else "")
        when = datetime.now().strftime("%a %b %d %Y %I:%M %p").replace(" 0", " ")
        self.store.remember(text=f"{when}: the brain set {name} to {said} (asked by {source}).",
                            title=f"Action: {name} {said}", kind="note", shelf="home/actions",
                            tags=["home", "action", dom], source="home",
                            meta={"entity_id": entity_id, "service": ".".join(svc), "data": data, "status": status})
        return {"entity_id": entity_id, "name": name, "service": ".".join(svc), "data": data, "status": status}


DO_SYSTEM = ("You turn a request into one smart-home action. Reply with JSON only: "
             '{"entity_id": one id from the list, "action": "on"|"off"|"toggle"|"brightness"|"color"|"speed"|'
             '"humidity"|"run", "value": number or color name or null}. If nothing on the list fits, reply '
             '{"entity_id": null}.')


async def plan_action(model, request, things):
    lst = "\n".join(f"- {t['entity_id']}: {t['name']} (now {t['state']})" for t in things)
    r = await model.chat(DO_SYSTEM, [{"role": "user", "content": f"Things you can control:\n{lst}\n\nRequest: {request}"}],
                         max_tokens=120, temperature=0)
    m = re.search(r"\{.*\}", r["text"], re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}
