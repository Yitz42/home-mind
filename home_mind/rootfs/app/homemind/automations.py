"""Home Mind runs your Home Assistant automations itself.

How it works
  - The automations stay written in Home Assistant (edit them in its automation editor as before). Home Mind reads
    their configs from Home Assistant, and for the ones it has taken over it does the work: it listens to Home
    Assistant's events, checks the triggers and conditions, and calls the services.
  - Taking one over turns the Home Assistant copy off and gives it the label "Run by Home Mind". Home Mind never
    runs an automation whose Home Assistant copy is on, so the two never both act.
  - The breaking point: Home Mind updates sensor.home_mind_heartbeat every minute. The "Home Mind watchdog"
    automation (kept in Home Assistant) turns every labelled automation back on if the heartbeat stops for 10
    minutes, so if Home Mind is stopped or removed, Home Assistant takes over again by itself. When Home Mind is back
    it takes them over again.
  - Templates are rendered by Home Assistant itself (its /api/template), so every template function works exactly
    as it does there.
  - Every run is filed in memory (home/automations), and each automation's description is kept as a note, so the
    brain can answer "what does my wake-up do?" and "when did bedtime last run?".

Supported: triggers state (to/from/not_to/not_from/attribute/for), numeric_state, time (fixed, helper or timestamp
sensor, with offset), zone enter/leave, homeassistant start; conditions state, numeric_state, time (after/before,
helpers, weekday), template, trigger, and/or/not; actions service calls (data/target templates, response
variables, continue_on_error), choose/default, if/then/else, delay, repeat count, variables, wait_for_trigger
(state and template), stop. Modes single, restart, queued. Anything else makes the automation "not supported" and
it is left to Home Assistant.
"""
import ast
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta

import aiohttp

log = logging.getLogger("homemind.automations")
SUPERVISOR = "http://supervisor"
LABEL = "run_by_home_mind"
TRIGGERS = {"state", "numeric_state", "time", "zone", "homeassistant"}
CONDITIONS = {"state", "numeric_state", "time", "template", "trigger", "and", "or", "not"}
ACTIONS = {"action", "service", "choose", "if", "delay", "repeat", "variables", "wait_for_trigger", "stop"}
KEEP_IN_HA = {"automation.home_mind_comfort_driver", "automation.home_mind_comfort_voice",
              "automation.home_mind_watchdog"}


def _list(x):
    return [] if x is None else x if isinstance(x, list) else [x]


def _secs(d):
    if d is None:
        return 0
    if isinstance(d, (int, float)):
        return float(d)
    if isinstance(d, str):
        p = [float(x) for x in d.split(":")]
        while len(p) < 3:
            p.insert(0, 0)
        return p[0] * 3600 + p[1] * 60 + p[2]
    return d.get("days", 0) * 86400 + d.get("hours", 0) * 3600 + d.get("minutes", 0) * 60 + d.get("seconds", 0) + \
        d.get("milliseconds", 0) / 1000


def _native(s):
    """Like Home Assistant's native types: '5' -> 5, "['a']" -> ['a'], 'abc' stays."""
    if not isinstance(s, str):
        return s
    t = s.strip()
    for f in (json.loads, ast.literal_eval):
        try:
            v = f(t)
            if isinstance(v, (int, float, list, dict, bool)) or v is None:
                return v
        except Exception:  # noqa: BLE001
            pass
    return s


def unsupported(cfg):
    """Why this config can't be run here, or None."""
    def walk_actions(acts):
        for a in _list(acts):
            if not isinstance(a, dict):
                return "odd action"
            keys = set(a) - {"alias", "continue_on_error", "enabled", "note", "data", "target", "response_variable",
                            "default", "else", "then", "conditions", "sequence", "timeout", "continue_on_timeout",
                            "set_conversation_response", "metadata"}
            kind = next((k for k in ("action", "service", "choose", "if", "delay", "repeat", "variables",
                                     "wait_for_trigger", "stop", "set_conversation_response") if k in a), None)
            if kind is None:
                return f"action {sorted(keys)}"
            if kind == "choose":
                for c in _list(a["choose"]):
                    w = walk_conds(c.get("conditions")) or walk_actions(c.get("sequence"))
                    if w:
                        return w
                w = walk_actions(a.get("default"))
            elif kind == "if":
                w = walk_conds(a["if"]) or walk_actions(a.get("then")) or walk_actions(a.get("else"))
            elif kind == "repeat":
                if "count" not in a["repeat"]:
                    return "repeat other than count"
                w = walk_actions(a["repeat"].get("sequence"))
            elif kind == "wait_for_trigger":
                w = next((f"wait trigger {t.get('trigger')}" for t in _list(a["wait_for_trigger"])
                          if t.get("trigger", t.get("platform")) not in ("state", "template")), None)
            else:
                w = None
            if w:
                return w
        return None

    def walk_conds(conds):
        for c in _list(conds):
            if isinstance(c, str):
                continue        # template shorthand
            k = c.get("condition")
            if k not in CONDITIONS:
                return f"condition {k}"
            if k in ("and", "or", "not"):
                w = walk_conds(c.get("conditions"))
                if w:
                    return w
        return None

    for t in _list(cfg.get("triggers") or cfg.get("trigger")):
        k = t.get("trigger", t.get("platform"))
        if k not in TRIGGERS:
            return f"trigger {k}"
    return walk_conds(cfg.get("conditions") or cfg.get("condition")) or \
        walk_actions(cfg.get("actions") or cfg.get("action"))


class Stop(Exception):
    pass


class Engine:
    def __init__(self, store, ha, data_dir):
        self.store, self.ha = store, ha
        self.path = os.path.join(data_dir, "automations.json")
        self.owned = set()
        self.autos = {}         # entity_id -> {"id", "config", "name", "ha_state", "why_not"}
        self.states = {}        # entity_id -> state dict (kept current from events)
        self.running = {}       # entity_id -> task
        self.locks = {}
        self.pending_for = {}   # (auto, trigger index) -> task
        self.waiters = []       # (predicate, future)
        self.last_tick = time.time()
        self.tz = None
        self.log = []           # recent runs
        self.dry = False
        try:
            with open(self.path) as f:
                d = json.load(f)
            self.owned = set(d.get("owned", []))
        except (OSError, ValueError):
            pass

    # ---- Home Assistant plumbing -------------------------------------------------------------------------------
    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.ha.token}"}

    async def _get(self, path):
        async with self.ha.session.get(f"{SUPERVISOR}/core/api/{path}", headers=self.headers,
                                       timeout=aiohttp.ClientTimeout(total=20)) as r:
            return await r.json() if r.status == 200 else None

    async def _post(self, path, payload):
        async with self.ha.session.post(f"{SUPERVISOR}/core/api/{path}", json=payload, headers=self.headers,
                                        timeout=aiohttp.ClientTimeout(total=60)) as r:
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"{path}: {r.status} {text[:200]}")
            try:
                return json.loads(text)
            except ValueError:
                return text

    async def render(self, value, variables):
        """Render templates in a value (str, list, dict) with Home Assistant's own template engine."""
        if isinstance(value, str):
            if "{{" not in value and "{%" not in value:
                return value
            out = await self._post("template", {"template": value, "variables": variables})
            return _native(out)
        if isinstance(value, list):
            return [await self.render(v, variables) for v in value]
        if isinstance(value, dict):
            return {k: await self.render(v, variables) for k, v in value.items()}
        return value

    def save(self):
        with open(self.path + ".tmp", "w") as f:
            json.dump({"owned": sorted(self.owned)}, f)
        os.replace(self.path + ".tmp", self.path)

    async def load(self):
        """Read every automation and its config from Home Assistant."""
        conf = await self._get("config")
        if conf and conf.get("time_zone"):
            from zoneinfo import ZoneInfo
            self.tz = ZoneInfo(conf["time_zone"])
        states = await self._get("states") or []
        self.states = {s["entity_id"]: s for s in states}
        autos = {}
        for s in states:
            if not s["entity_id"].startswith("automation."):
                continue
            aid = (s.get("attributes") or {}).get("id")
            cfg = await self._get(f"config/automation/config/{aid}") if aid else None
            autos[s["entity_id"]] = {"id": aid, "config": cfg, "ha_state": s["state"],
                                     "name": (s.get("attributes") or {}).get("friendly_name") or s["entity_id"],
                                     "why_not": "no config" if not cfg else unsupported(cfg)}
        self.autos = autos
        return autos

    def now(self):
        return datetime.now(self.tz) if self.tz else datetime.now().astimezone()

    # ---- taking over and handing back --------------------------------------------------------------------------
    async def takeover(self, ids):
        done, skipped = [], {}
        for eid in ids:
            a = self.autos.get(eid)
            if not a:
                skipped[eid] = "not found"
                continue
            if eid in KEEP_IN_HA:
                skipped[eid] = "kept in Home Assistant (it is Home Mind's own link to the house)"
                continue
            if a["why_not"]:
                skipped[eid] = f"not supported: {a['why_not']}"
                continue
            if a["ha_state"] == "unavailable":
                skipped[eid] = "unavailable in Home Assistant"
                continue
            await self._label(eid, True)
            await self._post("services/automation/turn_off", {"entity_id": eid, "stop_actions": False})
            a["ha_state"] = "off"
            self.owned.add(eid)
            done.append(eid)
            self._file_description(eid)
        self.save()
        return {"taken_over": done, "skipped": skipped}

    async def release(self, ids):
        out = []
        for eid in ids:
            if eid in self.owned:
                self.owned.discard(eid)
                t = self.running.pop(eid, None)
                if t:
                    t.cancel()
                await self._post("services/automation/turn_on", {"entity_id": eid})
                await self._label(eid, False)
                out.append(eid)
        self.save()
        return {"handed_back": out}

    async def retire(self, ids):
        """Stop running these, and leave the Home Assistant copy off and unlabelled (so the watchdog won't bring it
        back either): for automations whose job has moved into Home Mind itself."""
        out = []
        for eid in ids:
            self.owned.discard(eid)
            t = self.running.pop(eid, None)
            if t:
                t.cancel()
            await self._post("services/automation/turn_off", {"entity_id": eid, "stop_actions": True})
            await self._label(eid, False)
            out.append(eid)
        self.save()
        return {"retired": out}

    async def _label(self, eid, add):
        """Labels need the websocket API; best effort."""
        try:
            await self.ws_call({"type": "config/label_registry/create", "name": "Run by Home Mind",
                                "icon": "mdi:brain"}, quiet=True)
            ent = await self.ws_call({"type": "config/entity_registry/get", "entity_id": eid})
            labels = set((ent or {}).get("labels") or [])
            labels = labels | {LABEL} if add else labels - {LABEL}
            await self.ws_call({"type": "config/entity_registry/update", "entity_id": eid, "labels": sorted(labels)})
        except Exception as e:  # noqa: BLE001
            log.warning("label %s: %s", eid, e)

    def _file_description(self, eid):
        a = self.autos[eid]
        d = (a["config"] or {}).get("description") or ""
        if not d:
            return
        exists = self.store.db.execute("SELECT 1 FROM items WHERE shelf='home/automations' AND title=?",
                                       (f"Automation: {a['name']}",)).fetchone()
        if not exists:
            self.store.remember(text=f"My automation \"{a['name']}\" ({eid}): {d}", title=f"Automation: {a['name']}",
                                kind="note", shelf="home/automations", tags=["automation"], source="automations",
                                meta={"entity_id": eid})

    # ---- websocket: events in, registry calls out --------------------------------------------------------------
    async def ws_call(self, msg, quiet=False):
        if not getattr(self, "_ws", None):
            raise RuntimeError("websocket not connected")
        self._msg_id += 1
        mid = self._msg_id
        fut = asyncio.get_running_loop().create_future()
        self._calls[mid] = fut
        await self._ws.send_json(dict(msg, id=mid))
        r = await asyncio.wait_for(fut, 20)
        if not r.get("success") and not quiet:
            raise RuntimeError(str(r.get("error")))
        return r.get("result")

    async def run_forever(self):
        """Connect to Home Assistant's websocket, follow state changes, tick the clock. Reconnects on its own."""
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("automation engine")
            self._ws = None
            await asyncio.sleep(10)

    async def _session(self):
        self._msg_id, self._calls = 0, {}
        async with self.ha.session.ws_connect(f"{SUPERVISOR}/core/websocket", heartbeat=30) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": self.ha.token})
            ok = await ws.receive_json()
            if ok.get("type") != "auth_ok":
                raise RuntimeError(f"websocket auth: {ok}")
            self._ws = ws
            await self.load()
            await self.reclaim()
            self._msg_id += 1
            await ws.send_json({"id": self._msg_id, "type": "subscribe_events", "event_type": "state_changed"})
            ticker = asyncio.create_task(self._ticker())
            # a (re)connection counts as Home Assistant starting, for "homeassistant start" triggers
            for eid in list(self.owned):
                for i, t in enumerate(self._triggers(eid)):
                    if t.get("trigger", t.get("platform")) == "homeassistant" and t.get("event") == "start":
                        self.fire(eid, dict(t, idx=i, platform="homeassistant", event="start"))
            try:
                async for m in ws:
                    if m.type != aiohttp.WSMsgType.TEXT:
                        break
                    d = json.loads(m.data)
                    if d.get("type") == "result" and d.get("id") in self._calls:
                        self._calls.pop(d["id"]).set_result(d)
                    elif d.get("type") == "event":
                        ev = d["event"]
                        if ev.get("event_type") == "state_changed":
                            self.on_state(ev["data"])
            finally:
                ticker.cancel()

    async def reclaim(self):
        """After a restart (or the watchdog handing things back to Home Assistant), take the owned ones back."""
        for eid in list(self.owned):
            a = self.autos.get(eid)
            if not a:
                continue
            if a["ha_state"] == "on":
                await self._post("services/automation/turn_off", {"entity_id": eid, "stop_actions": False})
                a["ha_state"] = "off"
                log.info("took %s back from Home Assistant", eid)

    # ---- triggers -------------------------------------------------------------------------------------------------
    def _triggers(self, eid):
        cfg = self.autos.get(eid, {}).get("config") or {}
        return _list(cfg.get("triggers") or cfg.get("trigger"))

    def on_state(self, data):
        eid, old, new = data.get("entity_id"), data.get("old_state"), data.get("new_state")
        if new is not None:
            self.states[eid] = new
        if eid and eid.startswith("automation.") and eid in self.autos and new:
            self.autos[eid]["ha_state"] = new["state"]
        for pred, fut in list(self.waiters):
            if not fut.done() and pred(eid, old, new):
                fut.set_result({"entity_id": eid, "to_state": new, "from_state": old})
        # a change cancels a pending "for" that no longer holds
        for key, (ent, task, want) in list(self.pending_for.items()):
            if ent == eid and new and new.get("state") != want:
                task.cancel()
                self.pending_for.pop(key, None)
        for auto in list(self.owned):
            for i, t in enumerate(self._triggers(auto)):
                if self._state_matches(t, eid, old, new):
                    trig = {"platform": t.get("trigger", t.get("platform")), "id": t.get("id", str(i)), "idx": i,
                            "entity_id": eid, "from_state": old, "to_state": new}
                    if t.get("for"):
                        self._fire_later(auto, i, t, trig, eid, (new or {}).get("state"))
                    else:
                        self.fire(auto, trig)

    def _state_matches(self, t, eid, old, new):
        kind = t.get("trigger", t.get("platform"))
        if kind not in ("state", "numeric_state", "zone") or new is None:
            return False
        if kind == "zone":
            if eid not in _list(t.get("entity_id")):
                return False
            zone = self.states.get(t.get("zone"), {})
            zname = (zone.get("attributes") or {}).get("friendly_name")
            was = (old or {}).get("state") == zname or self._in_zone(old, zone)
            now_in = new.get("state") == zname or self._in_zone(new, zone)
            return (t.get("event", "enter") == "enter" and now_in and not was) or \
                   (t.get("event") == "leave" and was and not now_in)
        if eid not in _list(t.get("entity_id")):
            return False
        if kind == "numeric_state":
            def val(s):
                if s is None:
                    return None
                v = (s.get("attributes") or {}).get(t["attribute"]) if t.get("attribute") else s.get("state")
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
            def ok(v):
                return v is not None and (t.get("above") is None or v > float(t["above"])) and \
                    (t.get("below") is None or v < float(t["below"]))
            return ok(val(new)) and not ok(val(old))
        attr = t.get("attribute")
        if attr:
            ov = ((old or {}).get("attributes") or {}).get(attr)
            nv = (new.get("attributes") or {}).get(attr)
            if ov == nv:
                return False
            o, n = ov, nv
        else:
            o, n = (old or {}).get("state"), new.get("state")
            if o == n:
                return False
        if "to" in t and t["to"] is not None and str(n) not in [str(x) for x in _list(t["to"])]:
            return False
        if "from" in t and t["from"] is not None and str(o) not in [str(x) for x in _list(t["from"])]:
            return False
        if t.get("not_to") and str(n) in [str(x) for x in _list(t["not_to"])]:
            return False
        if t.get("not_from") and str(o) in [str(x) for x in _list(t["not_from"])]:
            return False
        return True

    @staticmethod
    def _in_zone(s, zone):
        try:
            a, z = s["attributes"], zone["attributes"]
            from math import radians, sin, cos, asin, sqrt
            la1, lo1, la2, lo2 = map(radians, (a["latitude"], a["longitude"], z["latitude"], z["longitude"]))
            d = 2 * 6371000 * asin(sqrt(sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2))
            return d <= float(z.get("radius", 100))
        except (KeyError, TypeError, ValueError):
            return False

    def _fire_later(self, auto, i, t, trig, eid, want):
        key = (auto, i)
        old = self.pending_for.pop(key, None)
        if old:
            old[1].cancel()

        async def later():
            await asyncio.sleep(_secs(t["for"]))
            self.pending_for.pop(key, None)
            if (self.states.get(eid) or {}).get("state") == want:
                self.fire(auto, trig)
        self.pending_for[key] = (eid, asyncio.create_task(later()), want)

    def _time_points(self, t, day):
        """The moments a time trigger fires on a given day."""
        out = []
        for at in _list(t.get("at")):
            off = 0
            if isinstance(at, dict):
                off = _secs(at.get("offset", 0)) * (-1 if str(at.get("offset", "")).startswith("-") else 1)
                at = at.get("entity_id")
            if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", str(at)):
                h, m, *s = [int(x) for x in str(at).split(":")]
                out.append(datetime.combine(day, datetime.min.time(), self.now().tzinfo).replace(
                    hour=h, minute=m, second=s[0] if s else 0) + timedelta(seconds=off))
                continue
            st = self.states.get(at)
            if not st or st["state"] in ("unknown", "unavailable"):
                continue
            a = st.get("attributes") or {}
            if at.startswith("input_datetime."):
                if a.get("has_date") and a.get("has_time"):
                    v = datetime.fromisoformat(st["state"]).replace(tzinfo=self.now().tzinfo)
                    if v.date() != day:
                        continue
                elif a.get("has_time"):
                    h, m, s = [int(x) for x in st["state"].split(":")]
                    v = datetime.combine(day, datetime.min.time(), self.now().tzinfo).replace(hour=h, minute=m, second=s)
                else:
                    continue
            else:
                try:
                    v = datetime.fromisoformat(st["state"].replace("Z", "+00:00")).astimezone(self.now().tzinfo)
                except ValueError:
                    continue
            out.append(v + timedelta(seconds=off))
        return out

    async def _ticker(self):
        while True:
            await asyncio.sleep(5)
            now = time.time()
            last, self.last_tick = self.last_tick, now
            if now - last > 600:
                last = now - 600      # don't replay a long gap
            for auto in list(self.owned):
                for i, t in enumerate(self._triggers(auto)):
                    if t.get("trigger", t.get("platform")) != "time":
                        continue
                    for day in {datetime.fromtimestamp(last, self.tz).date(), self.now().date()}:
                        for p in self._time_points(t, day):
                            if last < p.timestamp() <= now:
                                self.fire(auto, {"platform": "time", "id": t.get("id", str(i)), "idx": i,
                                                 "now": p.isoformat()})

    # ---- running ----------------------------------------------------------------------------------------------------
    def fire(self, auto, trig):
        a = self.autos.get(auto)
        if not a or auto not in self.owned or a["ha_state"] == "on":
            return          # Home Assistant is running it (watchdog handed it back): never both
        mode = (a["config"] or {}).get("mode", "single")
        cur = self.running.get(auto)
        if mode == "single" and cur and not cur.done():
            return
        if mode == "restart" and cur and not cur.done():
            cur.cancel()
        lock = self.locks.setdefault(auto, asyncio.Lock()) if mode in ("queued", "parallel") else None
        self.running[auto] = asyncio.create_task(self._run(auto, trig, lock))

    async def _run(self, auto, trig, lock):
        if lock:
            async with lock:
                return await self._run(auto, trig, None)
        a = self.autos[auto]
        cfg = a["config"]
        trig = dict(trig, id=str(trig.get("id")))
        v = {"trigger": {k: val for k, val in trig.items() if k not in ("idx",)}, "this": self.states.get(auto)}
        try:
            if not await self.conds(cfg.get("conditions") or cfg.get("condition"), v):
                return
            t0 = self.now()
            steps = []
            try:
                await self.acts(cfg.get("actions") or cfg.get("action"), v, steps)
                result = "done"
            except Stop:
                result = "stopped"
            self._record(auto, trig, t0, result, steps)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("%s", auto)
            self._record(auto, trig, self.now(), f"error: {e}", [])

    def _record(self, auto, trig, t0, result, steps):
        a = self.autos[auto]
        entry = {"automation": auto, "name": a["name"], "trigger": trig.get("id"), "at": t0.isoformat(),
                 "result": result, "steps": steps[:20]}
        self.log = (self.log + [entry])[-200:]
        when = t0.strftime("%a %b %d %Y %I:%M %p").replace(" 0", " ")
        self.store.remember(text=f"{when}: Home Mind ran my automation \"{a['name']}\" (trigger {trig.get('id')}): "
                                 f"{result}; " + ("; ".join(steps[:8]) or "nothing to do"),
                            title=f"Ran {a['name']}", kind="fact", shelf="home/automations", tags=["automation", "run"],
                            source="automations", meta={"series": "auto:" + auto, "entity_id": auto,
                                                        "trigger": trig.get("id"), "result": result})

    async def conds(self, conds, v):
        for c in _list(conds):
            if not await self.cond(c, v):
                return False
        return True

    async def cond(self, c, v):
        if isinstance(c, str):
            r = await self.render(c, v)
            return r is True or str(r).strip().lower() == "true"
        k = c.get("condition")
        if k == "and":
            return await self.conds(c.get("conditions"), v)
        if k == "or":
            for x in _list(c.get("conditions")):
                if await self.cond(x, v):
                    return True
            return False
        if k == "not":
            for x in _list(c.get("conditions")):
                if await self.cond(x, v):
                    return False
            return True
        if k == "trigger":
            return v["trigger"].get("id") in [str(x) for x in _list(c.get("id"))]
        if k == "template":
            r = await self.render(c["value_template"], v)
            return r is True or str(r).strip().lower() == "true"
        if k == "state":
            want = [str(x) for x in _list(c.get("state"))]
            for eid in _list(c.get("entity_id")):
                s = self.states.get(eid) or {}
                val = (s.get("attributes") or {}).get(c["attribute"]) if c.get("attribute") else s.get("state")
                if str(val) not in want:
                    return False
            return True
        if k == "numeric_state":
            for eid in _list(c.get("entity_id")):
                s = self.states.get(eid) or {}
                try:
                    x = float((s.get("attributes") or {}).get(c["attribute"]) if c.get("attribute") else s.get("state"))
                except (TypeError, ValueError):
                    return False
                if c.get("above") is not None and not x > float(c["above"]):
                    return False
                if c.get("below") is not None and not x < float(c["below"]):
                    return False
            return True
        if k == "time":
            now = self.now()
            if c.get("weekday") and now.strftime("%a").lower() not in [w.lower() for w in _list(c["weekday"])]:
                return False
            def t_of(x):
                if x is None:
                    return None
                if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", str(x)):
                    p = [int(y) for y in str(x).split(":")] + [0]
                    return p[0] * 3600 + p[1] * 60 + p[2]
                s = self.states.get(x) or {}
                st = s.get("state", "")
                m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", st)
                if not m:
                    return None
                if "T" in st or "+" in st[10:]:
                    d = datetime.fromisoformat(st.replace("Z", "+00:00")).astimezone(now.tzinfo)
                    return d.hour * 3600 + d.minute * 60 + d.second
                return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            n = now.hour * 3600 + now.minute * 60 + now.second
            after, before = t_of(c.get("after")), t_of(c.get("before"))
            if after is not None and before is not None and after > before:
                return n >= after or n < before
            return (after is None or n >= after) and (before is None or n < before)
        raise ValueError(f"condition {k}")

    async def acts(self, acts, v, steps):
        for a in _list(acts):
            if a.get("enabled") is False:
                continue
            try:
                await self.act(a, v, steps)
            except (Stop, asyncio.CancelledError):
                raise
            except Exception as e:  # noqa: BLE001
                if a.get("continue_on_error"):
                    steps.append(f"error ignored: {e}")
                    continue
                raise

    async def act(self, a, v, steps):
        if "choose" in a:
            for opt in _list(a["choose"]):
                if await self.conds(opt.get("conditions"), v):
                    return await self.acts(opt.get("sequence"), v, steps)
            return await self.acts(a.get("default"), v, steps)
        if "if" in a:
            branch = a.get("then") if await self.conds(a["if"], v) else a.get("else")
            return await self.acts(branch, v, steps)
        if "variables" in a:
            for k, val in a["variables"].items():
                v[k] = await self.render(val, v)
            return None
        if "delay" in a:
            d = await self.render(a["delay"], v)
            steps.append(f"waited {int(_secs(d))}s")
            await asyncio.sleep(_secs(d))
            return None
        if "repeat" in a:
            n = int(await self.render(a["repeat"]["count"], v))
            for i in range(1, n + 1):
                v["repeat"] = {"index": i, "first": i == 1, "last": i == n}
                await self.acts(a["repeat"].get("sequence"), v, steps)
            v.pop("repeat", None)
            return None
        if "wait_for_trigger" in a:
            return await self._wait(a, v, steps)
        if "stop" in a:
            steps.append(f"stopped: {a['stop']}")
            raise Stop()
        if "set_conversation_response" in a:
            return None
        svc = a.get("action") or a.get("service")
        dom, name = svc.split(".", 1)
        data = dict(await self.render(a.get("data") or {}, v))
        target = await self.render(a.get("target") or {}, v)
        for k in ("entity_id", "device_id", "area_id", "label_id"):
            if k in target:
                data[k] = target[k]
        rv = a.get("response_variable")
        if self.dry:
            steps.append(f"(dry) {svc} {json.dumps(data, default=str)[:120]}")
            return None
        path = f"services/{dom}/{name}" + ("?return_response" if rv else "")
        r = await self._post(path, data)
        if rv:
            v[rv] = (r or {}).get("service_response", r) if isinstance(r, dict) else r
        steps.append(f"{svc} {json.dumps({k: x for k, x in data.items() if k != 'instructions'}, default=str)[:120]}")
        return None

    async def _wait(self, a, v, steps):
        trigs = _list(a["wait_for_trigger"])
        fut = asyncio.get_running_loop().create_future()
        polls = []

        def pred(eid, old, new):
            return any(t.get("trigger", t.get("platform")) == "state" and self._state_matches(t, eid, old, new)
                       for t in trigs)
        self.waiters.append((pred, fut))
        for t in trigs:
            if t.get("trigger", t.get("platform")) == "template":
                async def poll(t=t):
                    prev = True     # a template trigger fires when it turns true
                    while not fut.done():
                        r = await self.render(t["value_template"], v)
                        now_true = r is True or str(r).strip().lower() == "true"
                        if now_true and not prev and not fut.done():
                            fut.set_result({"platform": "template"})
                        prev = now_true
                        await asyncio.sleep(20)
                polls.append(asyncio.create_task(poll()))
        timeout = _secs(await self.render(a.get("timeout"), v)) if a.get("timeout") else None
        try:
            r = await asyncio.wait_for(fut, timeout)
            v["wait"] = {"trigger": r, "remaining": None}
            steps.append("wait ended by a trigger")
        except asyncio.TimeoutError:
            v["wait"] = {"trigger": None, "remaining": 0}
            steps.append("wait timed out")
            if a.get("continue_on_timeout") is False:
                raise Stop()
        finally:
            self.waiters = [w for w in self.waiters if w[1] is not fut]
            for p in polls:
                p.cancel()

    # ---- for people ---------------------------------------------------------------------------------------------------
    def summary(self):
        out = []
        for eid, a in sorted(self.autos.items()):
            last = next((e for e in reversed(self.log) if e["automation"] == eid), None)
            out.append({"entity_id": eid, "name": a["name"], "run_by": "Home Mind" if eid in self.owned and
                        a["ha_state"] != "on" else "Home Assistant" if a["ha_state"] == "on" else "nobody (off)",
                        "ha_state": a["ha_state"], "supported": not a["why_not"], "why_not": a["why_not"],
                        "kept_in_ha": eid in KEEP_IN_HA, "last_run": last})
        return out

    async def run_now(self, eid, trigger_id=None):
        if eid not in self.owned:
            raise ValueError(f"{eid} is not run by Home Mind")
        a = self.autos[eid]
        self.running[eid] = asyncio.create_task(self._run(eid, {"platform": "manual", "id": trigger_id or "manual"},
                                                         None))
        await asyncio.sleep(0)
        return {"started": eid, "name": a["name"]}
