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
import fnmatch
import json
import logging
import math
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
# doors and locks: never moved on a model's say-so alone; act() needs confirm=True (the person said yes)
SENSITIVE_DOMAINS = ("lock", "alarm_control_panel", "valve")
SENSITIVE_COVERS = re.compile(r"garage|gate|door", re.I)
# a script or scene that opens the house ("script.open_garage", "Disarm alarm"), or a relay/button wired to a garage or
# gate opener: running one opens the house as surely as the lock or cover itself
OPENS_RUN = re.compile(r"garage|gate|door|unlock|disarm", re.I)
OPENS_SWITCH = re.compile(r"garage|gate|unlock|disarm|opener|door ?(?:release|strike|buzzer|lock)", re.I)
STEP = {"temperature": 1.0, "humidity": 3.0, "pm25": 5.0, "aqi": 5.0, "battery": 10.0}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _num(x):
    """A finite number from 40, "40", "40%" or "about 40 percent"; None otherwise."""
    if isinstance(x, str):
        m = re.search(r"-?\d+(?:\.\d+)?", x)
        x = m.group(0) if m else None
    v = _f(x)
    return v if v is not None and math.isfinite(v) else None


class Home:
    def __init__(self, store, ha, opts=None):
        self.store, self.ha = store, ha
        o = opts or {}
        self.controls = o.get("home_controls") or DEFAULT_CONTROLS
        fm = o.get("home_fan_max")
        self.fan_max = max(0, min(100, int(fm))) if fm not in (None, "") else 100     # 0 is a real limit
        self.catalog = {}          # entity_id -> {"name", "kind", "domain", "unit", "state"}
        self.seen = {}             # entity_id -> (value, time filed)
        self.actions = []
        self.model = store.kv_get("house_model") or {}    # habits, baselines, counts (house.learn, nightly)
        self.odd = {}              # entity_id -> unusual readings in a row
        self.notices = []          # (title, message) for the phone: very few (house.NOTIFY), sent by the main loop
        self.noticed = {}          # entity_id -> date last noticed (one a day each)

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
                                       unit=a.get("unit_of_measurement") or "", attrs=a,
                                       ctx=s.get("context") or {})
        self.catalog = cat
        return len(cat)

    def describe(self, eid, state=None, attrs=None):
        c = self.catalog[eid]
        st, a = (state, attrs or {}) if state is not None else (c["state"], c["attrs"])
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
            meta = {"series": "home:" + eid, "entity_id": eid, "state": st}
            if not first:
                meta.update(self._salience(eid, c, st, now))
            self.store.remember(text=text, title=f"{c['name']}: {self.describe(eid).split(': ', 1)[1]}",
                                kind="fact", shelf="home/" + ("devices" if c["kind"] == "device" else "readings"),
                                tags=["home", c["domain"]], source="home", meta=meta)
            filed += 1
        return filed

    def _salience(self, eid, c, st, now):
        """How notable this change is (#40), and an unusual reading (#39): two in a row, then one phone notice a day
        for that thing if it scores high enough. Every change is still filed; routine ones rank lower."""
        from . import house
        odd = None
        if c["kind"] == "reading":
            odd = house.unusual(self.model.get("baselines"), eid, st, now)
            self.odd[eid] = self.odd.get(eid, 0) + 1 if odd else 0
            if odd and self.odd[eid] < 2:
                odd = None      # one odd reading can be a glitch
        s, why = house.score(self.model, eid, st, now, anomaly=odd)
        out = {"salience": s}
        if why:
            out["why"] = why
        if s <= 0 and c["kind"] == "device":
            out["routine"] = True
        if odd:
            out["unusual"] = house.unusual_text(c["name"], st, c["unit"], odd[0], odd[1])
        if s >= house.NOTIFY and self.noticed.get(eid) != datetime.now().date():
            self.noticed[eid] = datetime.now().date()
            msg = out.get("unusual") or f"{self.describe(eid)} ({', '.join(why)})."
            self.notices.append(("Home Mind: something unusual", msg))
        return out

    def _filed_today(self, eid):
        start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
        return self.store.db.execute("SELECT 1 FROM items WHERE source='home' AND meta LIKE ? AND created >= ?",
                                     (f'%"entity_id": "{eid}"%', start)).fetchone() is not None

    # ---- history from before Home Mind was watching ------------------------------------------------------------
    async def backfill(self, ws_call=None, days=10, path=None):
        """File what Home Assistant still remembers: the last `days` of state changes (same rules as live: every
        device change, readings when they move enough and at most every 30 minutes), then, for sensors with long-term
        statistics, one note per day before that (average, low, high). Runs once per entity."""
        import json as _json
        import os as _os
        done = {}
        if path and _os.path.exists(path):
            try:
                done = _json.load(open(path))
            except ValueError:
                done = {}
        if not self.catalog:
            await self.refresh()
        first_live = {}
        for r in self.store.db.execute("SELECT meta, MIN(created) c FROM items WHERE source='home' AND "
                                       "meta NOT LIKE '%\"backfill\"%' GROUP BY json_extract(meta, '$.entity_id')"):
            m = _json.loads(r["meta"] or "{}")
            if m.get("entity_id"):
                first_live[m["entity_id"]] = r["c"]
        todo = [e for e, c in self.catalog.items() if c["kind"] != "run" and not done.get(e, {}).get("history")]
        start = (datetime.utcnow() - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        filed = 0
        for i in range(0, len(todo), 15):
            chunk = todo[i:i + 15]
            hist = await self.ha.history(chunk, start) or []
            for series in hist:
                if not series:
                    continue
                eid = series[0]["entity_id"]
                c = self.catalog.get(eid)
                if not c:
                    continue
                stop = first_live.get(eid, time.time())
                prev = None
                for r in series:
                    st, a = r.get("state"), r.get("attributes") or {}
                    t = datetime.fromisoformat(r["last_changed"].replace("Z", "+00:00")).timestamp()
                    if t >= stop - 60 or st in ("unavailable", "unknown", None):
                        continue
                    if c["kind"] == "device":
                        sig = st + ("" if c["domain"] != "climate" else f"/{a.get('temperature')}")
                        if prev and prev[0] == sig:
                            continue
                        prev = (sig, t)
                    else:
                        v = _f(st)
                        if prev is not None:
                            pv, pt = prev
                            if t - pt < 1800:
                                continue
                            if v is not None and _f(pv) is not None and abs(v - _f(pv)) < max(self._step(c), 1e-9):
                                continue
                            if v is None and pv == st:
                                continue
                        prev = (st, t)
                    when = datetime.fromtimestamp(t).strftime("%a %b %d %Y %I:%M %p").replace(" 0", " ")
                    unit_then = a.get("unit_of_measurement")
                    if unit_then == "kg" and c["unit"] == "lb" and _f(st) is not None:
                        st = f"{_f(st) * 2.20462:.2f}"      # the scale reported kg before the unit was switched
                    desc = self.describe(eid, st, a)
                    self.store.remember(text=f"{when}: {desc}.", title=f"{c['name']}: {desc.split(': ', 1)[1]}",
                                        kind="fact", shelf="home/" + ("devices" if c["kind"] == "device" else "readings"),
                                        tags=["home", c["domain"]], source="home", at=t,
                                        meta={"series": "home:" + eid, "entity_id": eid, "state": st, "backfill": True})
                    filed += 1
            for e in chunk:
                done.setdefault(e, {})["history"] = True
        daily = 0
        if ws_call:
            sensors = [e for e, c in self.catalog.items() if c["domain"] == "sensor" and c["kind"] == "reading"
                       and not done.get(e, {}).get("daily")]
            end = (datetime.utcnow() - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
            begin = (datetime.utcnow() - __import__("datetime").timedelta(days=365)).strftime("%Y-%m-%dT00:00:00Z")
            for i in range(0, len(sensors), 20):
                chunk = sensors[i:i + 20]
                try:
                    res = await ws_call({"type": "recorder/statistics_during_period", "start_time": begin,
                                         "end_time": end, "statistic_ids": chunk, "period": "day",
                                         "types": ["mean", "min", "max"]}) or {}
                except Exception as e:  # noqa: BLE001
                    log.warning("statistics: %s", e)
                    res = {}
                for eid, rows in res.items():
                    c = self.catalog.get(eid)
                    if not c:
                        continue
                    for r in rows:
                        if r.get("mean") is None:
                            continue
                        s = r["start"] / 1000 if isinstance(r["start"], (int, float)) else \
                            datetime.fromisoformat(str(r["start"]).replace("Z", "+00:00")).timestamp()
                        day = datetime.fromtimestamp(s)
                        u = f" {c['unit']}" if c["unit"] else ""
                        fmt = lambda x: f"{x:.1f}".rstrip("0").rstrip(".")
                        text = (f"{day:%A %b %d %Y}".replace(" 0", " ") + f": {c['name']} averaged {fmt(r['mean'])}{u}"
                                + (f" (low {fmt(r['min'])}, high {fmt(r['max'])})" if r.get("min") is not None else "")
                                + ".")
                        self.store.remember(text=text, title=f"{c['name']} on {day:%a %b %d}: avg {fmt(r['mean'])}{u}",
                                            kind="fact", shelf="home/daily", tags=["home", "daily"], source="home",
                                            at=s + 86399, meta={"series": "home:" + eid, "entity_id": eid,
                                                                "daily": True, "backfill": True})
                        daily += 1
                for e in chunk:
                    done.setdefault(e, {})["daily"] = True
        if path:
            with open(path, "w") as f:
                _json.dump(done, f)
        return {"history_notes": filed, "daily_notes": daily}

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

    RANGE = re.compile(r"\b(last|past|over|this|few|recent|lately|trend|history|since|coldest|warmest|hottest|"
                       r"highest|lowest|most|least|average|min|max|peak|days?|week|nights?|changed?)\b", re.I)

    def series_lines(self, question, days=14, limit=3):
        """For a question about how a reading went over time ("last few days", "coldest", "this week"): per-day low,
        high and average from the dated notes, plus the lowest and highest moments."""
        if not self.RANGE.search(question or ""):
            return ""
        import json as _json
        from collections import defaultdict
        ids = [e for e in self.matching(question) if self.catalog[e]["kind"] == "reading"][:limit]
        out = []
        since = time.time() - days * 86400
        for eid in ids:
            c = self.catalog[eid]
            rows = self.store.db.execute(
                "SELECT created, meta, body FROM items WHERE source='home' AND status IN ('active','faded') AND "
                "created >= ? AND meta LIKE ?", (since, f'%"entity_id": "{eid}"%')).fetchall()
            pts = []
            for r in rows:
                m = _json.loads(r["meta"] or "{}")
                if m.get("daily"):
                    continue
                v = _f(m.get("state"))
                if v is not None:
                    pts.append((r["created"], v))
            cur = _f(c["state"])
            if cur is not None:
                pts.append((time.time(), cur))
            if len(pts) < 2:
                continue
            pts.sort()
            byday = defaultdict(list)
            for t, v in pts:
                byday[datetime.fromtimestamp(t).date()].append((t, v))
            u = f" {c['unit']}" if c["unit"] else ""
            fmt = lambda x: f"{x:.1f}".rstrip("0").rstrip(".")
            lines = [f"{c['name']} by day (low / high / average of the readings filed):"]
            for d in sorted(byday)[-10:]:
                vs = [v for _, v in byday[d]]
                lo_t = min(byday[d], key=lambda x: x[1])[0]
                lines.append(f"- {d:%a %b %d}: low {fmt(min(vs))}{u} (at {datetime.fromtimestamp(lo_t):%I:%M %p}), "
                             f"high {fmt(max(vs))}{u}, average {fmt(sum(vs) / len(vs))}{u}")
            lo = min(pts, key=lambda x: x[1])
            hi = max(pts, key=lambda x: x[1])
            lines.append(f"Lowest: {fmt(lo[1])}{u} on {datetime.fromtimestamp(lo[0]):%a %b %d at %I:%M %p}. "
                         f"Highest: {fmt(hi[1])}{u} on {datetime.fromtimestamp(hi[0]):%a %b %d at %I:%M %p}.")
            out.append("\n".join(lines))
        return ("HISTORY (from the dated readings in memory):\n" + "\n\n".join(out)) if out else ""

    def now_lines(self, question):
        ids = self.matching(question)
        if not ids:
            return ""
        return "HOME NOW (live from Home Assistant, " + datetime.now().strftime("%a %H:%M") + "):\n" + \
            "\n".join("- " + self.describe(e) for e in ids)

    # ---- controls ---------------------------------------------------------------------------------------------------
    def allowed(self, eid):
        return any(eid == p or fnmatch.fnmatchcase(eid, p) for p in self.controls)

    def sensitive(self, eid):
        """Locks, alarms, valves, and garage doors / gates / doors: opening one needs the person's yes."""
        dom = eid.split(".")[0]
        if dom in SENSITIVE_DOMAINS:
            return True
        if dom == "cover":
            c = self.catalog.get(eid) or {}
            dc = str((c.get("attrs") or {}).get("device_class") or "")
            return bool(SENSITIVE_COVERS.search(dc) or SENSITIVE_COVERS.search(eid)
                        or SENSITIVE_COVERS.search(c.get("name") or ""))
        if dom in ("script", "scene", "switch", "button", "input_button"):
            c = self.catalog.get(eid) or {}
            rx = OPENS_RUN if dom in ("script", "scene") else OPENS_SWITCH
            if rx.search(eid) or rx.search(str(c.get("name") or "")):
                return True
            # a scene that sets a lock, an alarm or a garage door (Home Assistant lists them in its entity_id)
            members = (c.get("attrs") or {}).get("entity_id") if dom == "scene" else None
            return any(isinstance(m, str) and m != eid and m.split(".")[0] != "scene" and self.sensitive(m)
                       for m in (members if isinstance(members, list) else []))
        return False

    def controllable(self):
        return [{"entity_id": e, "name": c["name"], "state": c["state"]} for e, c in sorted(self.catalog.items())
                if self.allowed(e)]

    async def act(self, entity_id, action, value=None, source="api", confirm=False):
        """action: on, off, toggle, brightness (1-100), color (name), speed (fan %), humidity (%), run; covers also
        open, close; locks lock, unlock. Locks, alarms and garage doors need confirm=True."""
        if entity_id not in self.catalog:
            m = self.matching(entity_id, 1)
            if not m:
                raise ValueError(f"don't know {entity_id!r}")
            entity_id = m[0]
        if not self.allowed(entity_id):
            raise PermissionError(f"{entity_id} is not on the control list")
        if self.sensitive(entity_id) and confirm is not True:
            raise PermissionError(f"{entity_id} is a lock or door: it needs the person's confirmation (confirm: true)")
        if (self.catalog[entity_id].get("state") or "") in ("unavailable", "unknown"):
            raise ValueError(f"{entity_id} is {self.catalog[entity_id]['state']} in Home Assistant")
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
        prefs = getattr(self, "prefs", None)
        if a == "on" and value in (None, "") and prefs is not None and dom in ("light", "fan"):
            p = prefs.preferred(entity_id)          # #30: "turn on the desk lamp" -> how you usually like it
            if p:
                a, value = ("brightness" if dom == "light" else "speed"), p["value"]
        self.own = getattr(self, "own", {})
        self.own[entity_id] = now
        if dom in RUN_DOMAINS:
            svc = (dom, "turn_on")
        elif dom == "cover" and a in ("on", "open", "off", "close", "toggle"):
            svc = ("cover", {"on": "open_cover", "open": "open_cover", "off": "close_cover",
                             "close": "close_cover", "toggle": "toggle"}[a])
        elif dom == "lock" and a in ("lock", "unlock"):
            svc = ("lock", a)
        elif dom in ("lock", "alarm_control_panel", "valve"):
            raise ValueError(f"can't {action} {entity_id}")
        elif a == "run":
            svc = (dom, "turn_on")
        elif a in ("on", "off", "toggle"):
            svc = (dom if dom != "climate" else "climate", f"turn_{a}" if a != "toggle" else "toggle")
        elif a == "brightness" and dom == "light":
            v = _num(value)
            if v is None:
                raise ValueError(f"brightness needs a number from 1 to 100, not {value!r}")
            v = max(1, min(100, int(round(v))))
            svc, data["brightness_pct"] = ("light", "turn_on"), v
        elif a == "color" and dom == "light":
            svc, data["color_name"] = ("light", "turn_on"), str(value).lower().strip()
        elif a == "speed" and dom == "fan":
            v = _num(value)
            if v is None:
                raise ValueError(f"speed needs a number from 0 to {self.fan_max}, not {value!r}")
            v = max(0, min(self.fan_max, int(round(v))))
            svc, data["percentage"] = ("fan", "set_percentage"), v
        elif a == "humidity" and dom == "humidifier":
            lo = _f((await self.ha.state("input_number.mind_humidity_min") or {}).get("state")) or 30
            hi = _f((await self.ha.state("input_number.mind_humidity_max") or {}).get("state")) or 55
            v = max(lo, min(hi, _num(value) or lo))
            svc, data["humidity"] = ("humidifier", "set_humidity"), v
        else:
            raise ValueError(f"can't {action} {entity_id}")
        status = await self.ha.call(svc[0], svc[1], data)
        self.actions.append(now)
        if isinstance(status, int) and status >= 400:
            raise ValueError(f"Home Assistant refused {'.'.join(svc)} for {entity_id} (HTTP {status})")
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
        plan = json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}
    # only the shapes act() takes: an entity id and an action as text, a value as a number or text
    if not isinstance(plan.get("entity_id"), str) or not isinstance(plan.get("action"), (str, type(None))):
        return {"entity_id": None}
    if not isinstance(plan.get("value"), (str, int, float, type(None))) or isinstance(plan.get("value"), bool):
        plan["value"] = None
    return plan
