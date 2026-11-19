"""Home Mind runs your Home Assistant automations itself.

How it works
  - The automations stay written in Home Assistant (edit them in its automation editor as before). Home Mind reads
    their configs from Home Assistant, and for the ones it has taken over it does the work: it listens to Home
    Assistant's events, checks the triggers and conditions, and calls the services.
  - Taking one over turns the Home Assistant copy off and gives it the label "Run by Home Mind". Home Mind never
    runs an automation whose Home Assistant copy is on, so the two never both act.
  - The breaking point: Home Mind updates sensor.home_mind_heartbeat every minute. The "Home Mind watchdog"
    automation (kept in Home Assistant; Home Mind installs and updates it) turns every labelled automation back on
    after 3 missed heartbeats, so if Home Mind is stopped or removed, Home Assistant takes over by itself.
  - Each automation has one owner at a time: whichever side has it switched on. Home Mind checks before every run,
    and never takes one back by itself: when it finds Home Assistant running some (the watchdog, or a person
    switching one on), it says so and waits for a person to hand them back (take_back).
  - Before each run of a clock trigger, Home Mind writes when it fired to disk. After a stop, a time it missed is
    caught up once, only if it is under CATCH_UP_SECONDS late and the automation is safe to repeat (lights, media,
    fans, scenes, helpers, notifications: nothing that opens, locks, heats or runs a script).
  - Templates are rendered by Home Assistant itself (its /api/template), so every template function works exactly
    as it does there.
  - Every run is filed in memory (home/automations), and each automation's description is kept as a note, so the
    brain can answer "what does my wake-up do?" and "when did bedtime last run?".

Supported: triggers state (to/from/not_to/not_from/attribute/for), numeric_state, time (fixed, helper or timestamp
sensor, with offset), time_pattern (hours/minutes/seconds, "/5" and "*"), sun (sunrise/sunset with offset), event
(event_type, event_data), template (fires when it turns true), zone enter/leave, homeassistant start; conditions state, numeric_state, time (after/before,
helpers, weekday), template, trigger, and/or/not; actions service calls (data/target templates, response
variables, continue_on_error), choose/default, if/then/else, delay, repeat count, variables, wait_for_trigger
(state and template), stop, event (fire an event). Modes single, restart, queued. Anything else makes the automation "not supported" and
it is left to Home Assistant.
"""
import ast
import asyncio
import contextvars
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta

import aiohttp

log = logging.getLogger("homemind.automations")
from .ha import SUPERVISOR  # noqa: E402  (overridable for local testing)
LABEL = "run_by_home_mind"
TRIGGERS = {"state", "numeric_state", "time", "zone", "homeassistant", "time_pattern", "sun", "event", "template"}
CONDITIONS = {"state", "numeric_state", "time", "template", "trigger", "and", "or", "not"}
ACTIONS = {"action", "service", "choose", "if", "delay", "repeat", "variables", "wait_for_trigger", "stop", "event"}
KEEP_IN_HA = {"automation.home_mind_comfort_driver", "automation.home_mind_comfort_voice",
              "automation.home_mind_watchdog"}
CATCH_UP_SECONDS = 1800
WATCHDOG_ID = "home_mind_watchdog"
WATCHDOG = {
    "alias": "Home Mind watchdog",
    "description": "Installed by Home Mind. When Home Mind misses 3 heartbeats, Home Assistant runs Home Mind's everyday "
                   "automations again. Home Mind won't take them back by itself: hand them back from Home Mind.",
    "mode": "single",
    "triggers": [{"trigger": "template", "for": "00:01:00",
                  "value_template": "{% set hb = as_datetime(states('sensor.home_mind_heartbeat'), none) %}"
                                    "{{ hb is none or (now() - hb).total_seconds() > 150 }}"}],
    "conditions": [],
    "actions": [
        {"action": "automation.turn_on", "target": {"label_id": LABEL}},
        {"action": "persistent_notification.create",
         "data": {"notification_id": "home_mind_watchdog", "title": "Home Mind stopped",
                  "message": "Home Mind missed 3 heartbeats, so Home Assistant is running its everyday automations "
                             "again. When Home Mind is back, use its \"Take automations back\" button to hand them "
                             "back."}},
    ],
}
SAFE_DOMAINS = {"light", "fan", "media_player", "humidifier", "input_boolean", "input_number", "input_select",
                "input_text", "input_datetime", "notify", "persistent_notification", "tts", "todo", "switch", "scene"}


def safe_to_repeat(cfg):
    """An automation whose catch-up can't hurt: only lights, media, fans, scenes, helpers and notifications (no
    lock, cover, climate, valve, script, event, and nothing named like an opener or a heater)."""
    from .home import OPENS_RUN, OPENS_SWITCH
    from .approval import HEATER

    def ok_service(a):
        svc = a.get("action") or a.get("service") or ""
        if "{{" in svc or "." not in svc:
            return False
        dom = svc.split(".")[0]
        if dom not in SAFE_DOMAINS:
            return False
        target = json.dumps([a.get("target"), a.get("data"), a.get("entity_id")], default=str)
        return not (OPENS_RUN.search(target) or OPENS_SWITCH.search(target) or HEATER.search(target))

    def walk(acts):
        for a in _on(acts):
            if not isinstance(a, dict):
                return False
            if "choose" in a:
                if not all(walk(o.get("sequence")) for o in _list(a["choose"])) or not walk(a.get("default")):
                    return False
            elif "if" in a:
                if not walk(a.get("then")) or not walk(a.get("else")):
                    return False
            elif "repeat" in a:
                if not walk((a["repeat"] or {}).get("sequence")):
                    return False
            elif any(k in a for k in ("variables", "delay", "stop", "condition", "wait_for_trigger")):
                continue
            elif "action" in a or "service" in a:
                if not ok_service(a):
                    return False
            else:
                return False
        return True
    return walk((cfg or {}).get("actions") or (cfg or {}).get("action"))


def _list(x):
    return [] if x is None else x if isinstance(x, list) else [x]


def _on(items):
    """Without the ones switched off in the editor (enabled: false): Home Assistant skips those."""
    return [c for c in _list(items) if not (isinstance(c, dict) and c.get("enabled") is False)]


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
                            "set_conversation_response", "metadata", "event_data"}
            kind = next((k for k in ("action", "service", "choose", "if", "delay", "repeat", "variables",
                                     "wait_for_trigger", "stop", "event", "set_conversation_response") if k in a), None)
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
            if k in ("state", "numeric_state") and c.get("for"):
                return f"{k} condition with for"
            if k == "numeric_state" and c.get("value_template"):
                return "numeric_state with value_template"
            if k in ("and", "or", "not"):
                w = walk_conds(c.get("conditions"))
                if w:
                    return w
        return None

    for t in _list(cfg.get("triggers") or cfg.get("trigger")):
        k = t.get("trigger", t.get("platform"))
        if k not in TRIGGERS:
            return f"trigger {k}"
        if k == "sun" and t.get("event") not in ("sunrise", "sunset"):
            return f"sun trigger {t.get('event')}"
        if k == "template" and t.get("for"):
            return "template trigger with for"
        if k == "numeric_state" and t.get("value_template"):
            return "numeric_state with value_template"
    return walk_conds(cfg.get("conditions") or cfg.get("condition")) or \
        walk_actions(cfg.get("actions") or cfg.get("action"))


class Stop(Exception):
    pass


RUNNING = contextvars.ContextVar("homemind_automation", default=None)    # (automation id, trigger id) of this run


class Engine:
    def __init__(self, store, ha, data_dir):
        self.event_handlers = {}           # event type -> [async fn(data)]
        self.state_listeners = []          # fn(entity_id, new_state, old_state) for every state change (decision log)
        self.decisions = None              # the decision log: every service call a run makes is logged there
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
        self.last_fired = {}    # automation -> when a clock trigger last fired it (written before the run)
        self.held_by_ha = []    # owned automations Home Assistant is running now: wait for a person
        self.down_since = None  # the last tick saved before a stop: catch up from here
        try:
            with open(self.path) as f:
                d = json.load(f)
            self.owned = set(d.get("owned", []))
            self.last_fired = {k: float(v) for k, v in (d.get("last_fired") or {}).items()}
            self.down_since = float(d["last_tick"]) if d.get("last_tick") else None
        except (OSError, ValueError, TypeError):
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
            json.dump({"owned": sorted(self.owned), "last_fired": self.last_fired, "last_tick": self.last_tick}, f)
            f.flush()
            os.fsync(f.fileno())
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
            await self.ensure_watchdog()
            await self.reclaim()
            self.catch_up()
            self._msg_id += 1
            await ws.send_json({"id": self._msg_id, "type": "subscribe_events", "event_type": "state_changed"})
            for et in set(self.event_handlers) | self._event_types():   # other events (tapped notifications, triggers)
                self._msg_id += 1
                await ws.send_json({"id": self._msg_id, "type": "subscribe_events", "event_type": et})
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
                        else:
                            for fn in self.event_handlers.get(ev.get("event_type"), ()):
                                asyncio.create_task(_safe(fn, ev.get("data") or {}))
                            self.on_event(ev.get("event_type"), ev.get("data") or {})
            finally:
                ticker.cancel()

    async def reclaim(self):
        """After a restart: owned automations that Home Assistant is running now (the watchdog handed them over, or a
        person switched one on) stay with Home Assistant until a person hands them back (take_back). Never both."""
        held = sorted(e for e in self.owned if (self.autos.get(e) or {}).get("ha_state") == "on")
        new = [e for e in held if e not in self.held_by_ha]
        self.held_by_ha = held
        if new:
            names = ", ".join((self.autos.get(e) or {}).get("name") or e for e in held)
            log.info("Home Assistant is running %d of my automations: %s", len(held), names)
            try:
                await self.ha.notify("Home Mind is back",
                                     f"Home Assistant is running {len(held)} of Home Mind's automations ({names}). "
                                     "They stay there until you hand them back: use Home Mind's \"Take automations "
                                     "back\" button.", "home_mind_watchdog")
            except Exception:  # noqa: BLE001
                pass
        return held

    async def take_back(self, ids=None):
        """A person handing automations back to Home Mind (all that Home Assistant holds, or these)."""
        ids = [e for e in (ids or self.held_by_ha) if e in self.owned]
        r = await self.takeover(ids)
        self.held_by_ha = [e for e in self.held_by_ha if e not in r["taken_over"]]
        return r

    async def ensure_watchdog(self):
        """Install the watchdog automation in Home Assistant, or bring an older one up to date."""
        cur = self.autos.get("automation." + WATCHDOG_ID)
        try:
            if cur is None:
                await self._post(f"config/automation/config/{WATCHDOG_ID}", dict(WATCHDOG, id=WATCHDOG_ID))
                return "installed"
            have = {k: (cur.get("config") or {}).get(k) for k in ("triggers", "actions")}
            if cur.get("id") and have != {k: WATCHDOG[k] for k in ("triggers", "actions")}:
                await self._post(f"config/automation/config/{cur['id']}", dict(WATCHDOG, id=cur["id"]))
                return "updated"
            return "current"
        except Exception as e:  # noqa: BLE001 - without it Home Assistant just won't take over by itself
            log.warning("watchdog: %s", e)
            return f"error: {e}"

    def catch_up(self, now=None):
        """Clock triggers missed while Home Mind was stopped: once each, if under CATCH_UP_SECONDS late and safe to
        repeat, and not already fired (last_fired). Returns the automations fired."""
        now = now or time.time()
        since, self.down_since = self.down_since, None
        if not since or now - since < 10:
            return []
        start = max(since, now - CATCH_UP_SECONDS)
        fired = []
        self._tick(start, now, catchup=fired)
        if fired:
            log.info("caught up %s", ", ".join(fired))
        return fired

    # ---- triggers -------------------------------------------------------------------------------------------------
    def _triggers(self, eid):
        cfg = self.autos.get(eid, {}).get("config") or {}
        # a trigger switched off in the editor never fires (kept as a placeholder so the others keep their index)
        return [{"trigger": "disabled"} if isinstance(t, dict) and t.get("enabled") is False else t
                for t in _list(cfg.get("triggers") or cfg.get("trigger"))]

    def _limit(self, x):
        """A numeric_state above/below: a number, or an entity (input_number.x) whose state is one. None = unknown."""
        if x is None:
            return None
        try:
            return float(x)
        except (TypeError, ValueError):
            pass
        try:
            return float((self.states.get(str(x)) or {}).get("state"))
        except (TypeError, ValueError):
            return None

    def _event_types(self):
        return {et for auto in self.owned for t in self._triggers(auto) if t.get("trigger", t.get("platform")) == "event"
                for et in _list(t.get("event_type"))}

    def on_event(self, event_type, data):
        """Event triggers: the type matches and every key of event_data is in the event's data with that value."""
        for auto in list(self.owned):
            for i, t in enumerate(self._triggers(auto)):
                if t.get("trigger", t.get("platform")) != "event" or event_type not in _list(t.get("event_type")):
                    continue
                want = t.get("event_data") or {}
                if all(data.get(k) == v for k, v in want.items()):
                    self.fire(auto, {"platform": "event", "id": t.get("id", str(i)), "idx": i,
                                     "event": {"event_type": event_type, "data": data}})

    async def _templates(self, eid=None):
        """Template triggers fire when the template turns true (false before, or first seen false). Re-rendered when
        an entity named in it changes, and every minute."""
        truth = self.__dict__.setdefault("_truth", {})
        for auto in list(self.owned):
            for i, t in enumerate(self._triggers(auto)):
                if t.get("trigger", t.get("platform")) != "template":
                    continue
                tpl = t.get("value_template") or ""
                if eid and eid not in tpl:
                    continue
                try:
                    v = await self.render(tpl, {})
                except Exception:  # noqa: BLE001
                    continue
                now_true = str(v).strip().lower() in ("true", "1", "on", "yes")
                was = truth.get((auto, i))
                truth[(auto, i)] = now_true
                if now_true and was is False:
                    self.fire(auto, {"platform": "template", "id": t.get("id", str(i)), "idx": i})

    @staticmethod
    def _pattern(spec, v):
        """time_pattern field: None or "*" = any, "/n" = every n, a number = exactly."""
        if spec is None or str(spec) == "*":
            return True
        s = str(spec)
        if s.startswith("/"):
            return v % int(s[1:]) == 0
        return v == int(s)

    def _pattern_hits(self, t, last, now):
        """Seconds in (last, now] matching a time_pattern trigger (HA defaults: seconds 0 when only larger units
        are given)."""
        hrs, mins, secs = t.get("hours"), t.get("minutes"), t.get("seconds")
        if secs is None and (mins is not None or hrs is not None):
            secs = 0
        if mins is None and hrs is not None:
            mins = 0
        out = []
        for s in range(int(last) + 1, int(now) + 1):
            d = datetime.fromtimestamp(s, self.tz)
            if self._pattern(hrs, d.hour) and self._pattern(mins, d.minute) and self._pattern(secs, d.second):
                out.append(d)
        return out

    def on_state(self, data):
        eid, old, new = data.get("entity_id"), data.get("old_state"), data.get("new_state")
        if new is not None:
            self.states[eid] = new
        for fn in self.state_listeners:
            try:
                fn(eid, new, old)
            except Exception:  # noqa: BLE001 - a listener must never stop the automations
                log.exception("state listener")
        if eid and any(t.get("trigger", t.get("platform")) == "template" for a in self.owned for t in self._triggers(a)):
            asyncio.create_task(_safe(lambda _: self._templates(eid), {}))
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
                try:
                    hit = self._state_matches(t, eid, old, new)
                except Exception:  # noqa: BLE001 - one odd trigger must not drop the connection for every automation
                    log.exception("%s trigger %d", auto, i)
                    continue
                if hit:
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
            above, below = self._limit(t.get("above")), self._limit(t.get("below"))
            if (t.get("above") is not None and above is None) or (t.get("below") is not None and below is None):
                return False        # a limit helper that is unavailable: nothing crosses it

            def ok(v):
                return v is not None and (above is None or v > above) and (below is None or v < below)
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
            self._tick(last, now)
            if now - self.__dict__.get("_saved_at", 0) >= 60:
                self._saved_at = now
                try:
                    self.save()       # the last tick on disk: after a stop, catch_up() starts from it
                except OSError:
                    log.exception("saving the clock")
            if now - self.__dict__.get("_tpl_at", 0) >= 60:
                self._tpl_at = now
                await self._templates()

    def _sun_points(self, t):
        """Sunrise or sunset (with offset) from sun.sun: the next one, and the one just passed (its old value)."""
        attr = "next_rising" if t.get("event") == "sunrise" else "next_setting"
        prev = self.__dict__.setdefault("_sun_prev", {})
        cur = ((self.states.get("sun.sun") or {}).get("attributes") or {}).get(attr)
        out = []
        for v in {cur, prev.get(attr)} - {None}:
            try:
                p = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except ValueError:
                continue
            off = t.get("offset", 0)
            sign = -1 if str(off).startswith("-") else 1
            out.append(p + timedelta(seconds=sign * _secs(str(off).lstrip("-+") if isinstance(off, str) else off)))
        return out

    def _tick(self, last, now, catchup=None):
        """Fire the clock triggers due in (last, now]: time, time_pattern and sun."""
        # sun.sun's next_rising moves to tomorrow at sunrise: keep the one just passed, so it can still fire
        sa = ((self.states.get("sun.sun") or {}).get("attributes") or {})
        prev = self.__dict__.setdefault("_sun_prev", {})
        for k in ("next_rising", "next_setting"):
            if sa.get(k) and sa.get(k) != prev.get(k + "_cur"):
                prev[k], prev[k + "_cur"] = prev.get(k + "_cur"), sa.get(k)
        for auto in list(self.owned):
            for i, t in enumerate(self._triggers(auto)):
                kind = t.get("trigger", t.get("platform"))
                if kind == "time":
                    for day in {datetime.fromtimestamp(last, self.tz).date(), self.now().date()}:
                        for p in self._time_points(t, day):
                            if last < p.timestamp() <= now:
                                self.fire(auto, {"platform": "time", "id": t.get("id", str(i)), "idx": i,
                                                 "now": p.isoformat()}, catchup)
                elif kind == "time_pattern":
                    for p in self._pattern_hits(t, last, now)[:1]:
                        self.fire(auto, {"platform": "time_pattern", "id": t.get("id", str(i)), "idx": i,
                                         "now": p.isoformat()}, catchup)
                elif kind == "sun":
                    for p in self._sun_points(t):
                        if last < p.timestamp() <= now:
                            self.fire(auto, {"platform": "sun", "id": t.get("id", str(i)), "idx": i,
                                             "event": t.get("event"), "offset": t.get("offset", 0),
                                             "now": p.isoformat()}, catchup)


    # ---- creating (suggested and requested automations) -------------------------------------------------------------
    async def create(self, cfg, enable=False):
        """Create an automation in Home Assistant from a checked config; switched off unless enable. Returns its
        entity ID (once Home Assistant has loaded it) and config ID."""
        from .autodraft import slug
        base = slug(cfg.get("alias"))
        aid, n = base, 2
        while any(a.get("id") == aid for a in self.autos.values()):
            aid, n = f"{base}_{n}", n + 1
        body = dict(cfg, id=aid)
        await self._post(f"config/automation/config/{aid}", body)
        eid = None
        for _ in range(20):          # Home Assistant reloads automations after a config write
            await asyncio.sleep(0.5)
            states = await self._get("states") or []
            eid = next((s["entity_id"] for s in states if s["entity_id"].startswith("automation.")
                        and (s.get("attributes") or {}).get("id") == aid), None)
            if eid:
                break
        if eid and not enable:
            await self._post("services/automation/turn_off", {"entity_id": eid, "stop_actions": True})
        if eid:
            self.autos[eid] = {"id": aid, "config": body, "ha_state": "on" if enable else "off",
                               "name": cfg.get("alias") or eid, "why_not": unsupported(body)}
        return {"entity_id": eid, "id": aid, "enabled": bool(enable and eid)}

    # ---- running ----------------------------------------------------------------------------------------------------
    def fire(self, auto, trig, catchup=None):
        a = self.autos.get(auto)
        if not a or auto not in self.owned or a["ha_state"] == "on":
            return          # Home Assistant is running it (watchdog handed it back): never both
        at = trig.get("now")
        if at:
            try:
                ts = datetime.fromisoformat(str(at)).timestamp()
            except ValueError:
                ts = None
            if ts is not None:
                if ts <= self.last_fired.get(auto, 0):
                    return      # this time already fired (before a restart)
                if catchup is not None:
                    if not safe_to_repeat(a.get("config")):
                        return
                    trig = dict(trig, catch_up=True)
                    catchup.append(auto)
                self.last_fired[auto] = ts
                try:
                    self.save()  # on disk before acting: a restart now can't run it twice
                except OSError:
                    log.exception("saving last fired")
        elif catchup is not None:
            return          # only clock times are caught up
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
        RUNNING.set((auto, trig.get("id")))
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
        for c in _on(conds):
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
            for x in _on(c.get("conditions")):
                if await self.cond(x, v):
                    return True
            return False
        if k == "not":
            for x in _on(c.get("conditions")):
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
                above, below = self._limit(c.get("above")), self._limit(c.get("below"))
                if c.get("above") is not None and (above is None or not x > above):
                    return False
                if c.get("below") is not None and (below is None or not x < below):
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
        if "event" in a and "action" not in a and "service" not in a:
            data = await self.render(a.get("event_data") or {}, v)
            if self.dry:
                steps.append(f"(dry) event {a['event']}")
                return None
            await self._post(f"events/{a['event']}", data)
            steps.append(f"fired event {a['event']}")
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
        ents = data.get("entity_id")
        ents = [e.strip() for e in ents.split(",")] if isinstance(ents, str) else list(ents or []) \
            if isinstance(ents, (list, tuple)) else []
        bo = getattr(self, "backoff", None)
        if bo is not None and ents:
            kept = [e for e in ents if not bo.active(e)]
            if not kept:
                steps.append(f"backed off: {', '.join(ents)} changed by hand lately")
                return None
            if len(kept) < len(ents):
                data["entity_id"] = kept
                ents = kept
        before = {e: (self.states.get(e) or {}).get("state") for e in ents}
        if getattr(self.ha, "user_token", "") and not rv:
            # the limited Home Assistant user (option ha_user_token) for device calls; your own automations count as
            # approved (you wrote them in Home Assistant)
            status, _, states = await self.ha.call_ctx(dom, name, data, approved=True)
            if isinstance(status, int) and status >= 400:
                raise RuntimeError(f"{path}: {status}")
            r = states or []
        else:
            r = await self._post(path, data)
        self._log_call(svc, data, r, before)
        if rv:
            v[rv] = (r or {}).get("service_response", r) if isinstance(r, dict) else r
        steps.append(f"{svc} {json.dumps({k: x for k, x in data.items() if k != 'instructions'}, default=str)[:120]}")
        return None

    def _log_call(self, svc, data, response, before=None):
        """Log a service call this run made, once per entity it targets, so its outcome is checked like any
        other decision (channel "automation", source the automation)."""
        dlog = self.decisions
        if dlog is None:
            return
        try:
            from .decisions import context_of
            ctx, states = context_of(response)
            auto, trig = RUNNING.get() or (None, None)
            ents = data.get("entity_id")
            ents = [e.strip() for e in ents.split(",")] if isinstance(ents, str) else list(ents or [None])
            name = (self.autos.get(auto) or {}).get("name")
            for e in ents:
                if e == "all":
                    e = None
                prior = (before or {}).get(e) if e else None
                dlog.record(entity_id=e, action=svc.split(".", 1)[1], service=svc,
                            data={k: x for k, x in data.items() if k != "entity_id"}, context_id=ctx, states=states,
                            prior=prior, channel="automation", source=auto,
                            route="automation", model=None, request=None,
                            records=[{"automation": auto, "name": name, "trigger": trig}] if auto else [])
        except Exception:  # noqa: BLE001 - the log must never stop a run
            log.exception("decision log")

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


async def _safe(fn, data):
    try:
        await fn(data)
    except Exception:  # noqa: BLE001
        log.exception("event handler")
