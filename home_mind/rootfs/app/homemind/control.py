"""Controls for Home Assistant: the settings that can change while Home Mind runs, the jobs that can be started by
hand, and a few readings, described so the bridge can make switches, numbers, times, buttons and sensors from them.

A change made here is kept in the database as an override on top of the app's Configuration tab. If the option
itself is changed in the Configuration tab later, the Configuration value wins and the override is dropped, so the
two never fight. Jobs started from a button run in the background; their last result is kept for the sensors.
"""
import asyncio
import logging
import time
from datetime import datetime

from .version import VERSION

log = logging.getLogger("home_mind.control")
KV = "control_overrides"


def _hhmm(v):
    """'7:5' or '07:05:00' -> '07:05'; anything else raises ValueError."""
    parts = str(v).strip().split(":")
    if len(parts) < 2:
        raise ValueError(f"time must be HH:MM, got {v!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"time out of range: {v!r}")
    return f"{h:02d}:{m:02d}"


def _at(v):
    return tuple(int(x) for x in _hhmm(v).split(":"))


def _move_daily(job, value):
    """A daily job (morning summary, overnight) moved to a new time: run today only if that time is still ahead."""
    job.at = _at(value)
    now = datetime.now()
    job.done_for = now.date() if (now.hour, now.minute) >= job.at else None


def quiet_window(opts):
    """Quiet hours from the options, as ((h, m), (h, m)), or None when off."""
    if opts.get("quiet_hours") is False:
        return None
    try:
        a, b = (tuple(int(x) for x in str(opts.get(k)).split(":")[:2]) for k in ("quiet_start", "quiet_end"))
        return (a, b) if a != b else None
    except (TypeError, ValueError):
        return None


def _quiet(A, v=None):
    A.ha.quiet = quiet_window(A.opts)


# Each setting: kind, label, icon, its range (numbers), and how to apply it to the running app.
SETTINGS = {
    "comfort": {"kind": "switch", "label": "Bedroom comfort brain", "icon": "mdi:thermometer-auto",
                "apply": lambda A, v: None},                          # upkeep reads opts["comfort"] each pass
    "home": {"kind": "switch", "label": "Watch the house", "icon": "mdi:home-search",
             "apply": lambda A, v: setattr(A.mind, "home", A.home if v else None)},
    "overnight_enabled": {"kind": "switch", "label": "Overnight learning", "icon": "mdi:weather-night",
                          "apply": lambda A, v: setattr(A.overnight, "enabled", bool(v))},
    "morning_enabled": {"kind": "switch", "label": "Morning summary", "icon": "mdi:weather-sunset-up",
                        "apply": lambda A, v: setattr(A.morning, "enabled", bool(v))},
    "patterns_checkins": {"kind": "switch", "label": "Brain-fog check-ins", "icon": "mdi:head-question",
                          "apply": lambda A, v: setattr(A.patterns, "checkins_on", bool(v))},
    "auto_update_packs": {"kind": "switch", "label": "Install new packs automatically", "icon": "mdi:package-down",
                          "apply": lambda A, v: None},
    "answer_edges": {"kind": "switch", "label": "Repeat the best note before the question", "icon": "mdi:format-list-checks",
                     "apply": lambda A, v: setattr(A.mind, "ORDER", "edges" if v else "default")},
    "answer_notes_first": {"kind": "switch", "label": "Name the notes before answering", "icon": "mdi:note-search",
                           "apply": lambda A, v: setattr(A.mind, "NOTES_FIRST", bool(v))},
    "overnight_structure": {"kind": "switch", "label": "Overnight: read what the patterns missed", "icon": "mdi:text-search",
                            "apply": lambda A, v: setattr(A.overnight, "structure_on", bool(v))},
    "quiet_hours": {"kind": "switch", "label": "Quiet hours for phone notifications", "icon": "mdi:bell-sleep",
                    "apply": _quiet},
    "checkin_old_facts": {"kind": "switch", "label": "Ask about old facts in the morning", "icon": "mdi:calendar-question",
                          "apply": lambda A, v: setattr(A.morning, "checkin_on", bool(v))},
    "voice_seconds": {"kind": "number", "label": "Voice time limit", "icon": "mdi:timer-outline", "min": 2, "max": 60,
                      "step": 1, "unit": "s", "apply": lambda A, v: setattr(A.mind, "VOICE_SECONDS", float(v))},
    "fade_days": {"kind": "number", "label": "Fade unused notes after", "icon": "mdi:archive-clock", "min": 0,
                  "max": 3650, "step": 1, "unit": "d", "apply": lambda A, v: None},
    "auto_test_hours": {"kind": "number", "label": "Brain tests every", "icon": "mdi:test-tube", "min": 0, "max": 168,
                        "step": 1, "unit": "h", "apply": lambda A, v: None},
    "notify_daily_cap": {"kind": "number", "label": "Phone notifications a day (0 = no limit)", "icon": "mdi:bell-badge",
                         "min": 0, "max": 50, "step": 1, "apply": lambda A, v: setattr(A.ha, "cap", int(v))},
    "events_keep_days": {"kind": "number", "label": "Keep the detailed activity log for", "icon": "mdi:history",
                         "min": 0, "max": 3650, "step": 1, "unit": "d", "apply": lambda A, v: None},
    "checkin_after_days": {"kind": "number", "label": "Ask about facts older than", "icon": "mdi:calendar-question",
                           "min": 30, "max": 3650, "step": 1, "unit": "d",
                           "apply": lambda A, v: setattr(A.morning, "checkin_days", int(v))},
    "quiet_start": {"kind": "time", "label": "Quiet hours start", "icon": "mdi:bell-sleep", "apply": _quiet},
    "quiet_end": {"kind": "time", "label": "Quiet hours end", "icon": "mdi:bell-ring", "apply": _quiet},
    "morning_time": {"kind": "time", "label": "Morning summary time", "icon": "mdi:clock-outline",
                     "apply": lambda A, v: _move_daily(A.morning, v)},
    "overnight_time": {"kind": "time", "label": "Overnight learning time", "icon": "mdi:clock-outline",
                       "apply": lambda A, v: _move_daily(A.overnight, v)},
}


async def _tests(A):
    import random
    runs = [A.mind.start_test(s, "brain", seed=random.randint(1, 99999)) for s in ("recall", "update", "locate", "tasks")]
    return {"started": [r["id"] for r in runs]}


async def _release(A):
    return await A.engine.release(sorted(A.engine.owned))


# Each job: label, icon, and what it starts.
ACTIONS = {
    "overnight": {"label": "Run overnight learning now", "icon": "mdi:weather-night", "run": lambda A: A.overnight.run()},
    "morning": {"label": "Send the morning summary", "icon": "mdi:message-text", "run": lambda A: A.morning.send()},
    "calendars": {"label": "Sync calendars", "icon": "mdi:calendar-sync", "run": lambda A: A.calendars.sync()},
    "models": {"label": "Check models", "icon": "mdi:robot", "run": lambda A: A.models.check_all()},
    "packs": {"label": "Install pack updates", "icon": "mdi:package-down", "run": lambda A: A.updater.apply()},
    "comfort": {"label": "Run a comfort cycle", "icon": "mdi:thermometer-auto",
                "run": lambda A: A.comfort.cycle(reason="asked")},
    "patterns": {"label": "Refresh the daily record", "icon": "mdi:chart-timeline-variant",
                 "run": lambda A: A.patterns.refresh(3)},
    "tests": {"label": "Run brain tests", "icon": "mdi:test-tube", "run": _tests},
    "release_automations": {"label": "Hand automations back to Home Assistant", "icon": "mdi:hand-back-left",
                            "run": _release},
}


def _iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None


class Control:
    def __init__(self, app):
        self.A = app
        self.store = app.mind.store
        self.running = {}          # action -> asyncio.Task
        self.last = {}             # action -> {"at", "ok", "result" or "error"}

    # ---- settings ------------------------------------------------------------------------------------------------
    def _overrides(self):
        return dict(self.store.kv_get(KV) or {})

    def _check(self, key, value):
        if key not in SETTINGS:
            raise KeyError(f"unknown setting {key}")
        s = SETTINGS[key]
        if s["kind"] == "switch":
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "on", "yes")
            return bool(value)
        if s["kind"] == "number":
            v = float(value)
            if not s["min"] <= v <= s["max"]:
                raise ValueError(f"{key} must be between {s['min']} and {s['max']}")
            return int(v) if float(s["step"]).is_integer() else v
        return _hhmm(value)

    def load(self):
        """At start-up: put back the overrides whose option has not been changed in the Configuration tab since."""
        kept, dropped = {}, []
        for key, o in self._overrides().items():
            if key in SETTINGS and self.A.opts.get(key) == o.get("base"):
                kept[key] = o
                self._apply(key, o["value"])
            else:
                dropped.append(key)
        if dropped:
            self.store.kv_set(KV, kept)
            log.info("control: Configuration tab changed, dropped overrides for %s", ", ".join(dropped))
        return kept

    def _apply(self, key, value):
        self.A.opts[key] = value
        try:
            SETTINGS[key]["apply"](self.A, value)
        except Exception:  # noqa: BLE001  (a module that did not start, e.g. outside Home Assistant)
            log.exception("control: applying %s", key)

    def set(self, key, value):
        value = self._check(key, value)
        ov = self._overrides()
        base = ov[key]["base"] if key in ov else self.A.opts.get(key)     # the Configuration tab's value
        if value == base:
            ov.pop(key, None)                      # back to what the Configuration tab says: no override needed
        else:
            ov[key] = {"value": value, "base": base, "at": time.time()}
        self.store.kv_set(KV, ov)
        self._apply(key, value)
        self.store.log("control", key=key, value=value)
        return self.describe()

    def reset(self, key=None):
        """Drop one override (or all) and go back to the Configuration tab's value."""
        ov = self._overrides()
        for k in ([key] if key else list(ov)):
            o = ov.pop(k, None)
            if o is not None:
                self._apply(k, o["base"])
        self.store.kv_set(KV, ov)
        return self.describe()

    # ---- jobs ----------------------------------------------------------------------------------------------------
    def run(self, action):
        """Start a job in the background (some take minutes); a second press while it runs is ignored."""
        if action not in ACTIONS:
            raise KeyError(f"unknown action {action}")
        t = self.running.get(action)
        if t and not t.done():
            return {"action": action, "started": False, "running": True}
        self.running[action] = asyncio.create_task(self._run(action))
        return {"action": action, "started": True}

    async def _run(self, action):
        t0 = time.time()
        try:
            res = ACTIONS[action]["run"](self.A)
            if asyncio.iscoroutine(res):
                res = await res
            self.last[action] = {"at": time.time(), "ok": True, "seconds": round(time.time() - t0, 1),
                                 "result": _short(res)}
        except Exception as e:  # noqa: BLE001
            log.exception("control: %s", action)
            self.last[action] = {"at": time.time(), "ok": False, "error": f"{type(e).__name__}: {e}"}
        self.store.log("control_run", action=action, **self.last[action])

    # ---- readings ------------------------------------------------------------------------------------------------
    def sensors(self):
        A = self.A
        out = {}

        def put(key, label, value, icon, unit=None, device_class=None, attrs=None):
            out[key] = {"label": label, "value": value, "icon": icon, "unit": unit, "device_class": device_class,
                        "attrs": attrs or {}}

        try:
            s = self.store.stats()
            put("items", "Memory items", s.get("items"), "mdi:brain", "items",
                attrs={k: s.get(k) for k in ("versions", "faded", "shelves") if k in s})
        except Exception:  # noqa: BLE001
            pass
        conv = getattr(A, "convlog", None)
        if conv is not None:
            try:
                put("pending_turns", "Conversation turns to learn from", len(conv.pending()), "mdi:forum", "turns")
            except Exception:  # noqa: BLE001
                pass
        ov = getattr(A, "overnight", None)
        if ov is not None:
            last = ov.last or {}
            put("overnight_last", "Last overnight run", _iso(last.get("at")), "mdi:weather-night",
                device_class="timestamp", attrs={k: v for k, v in last.items() if k not in ("at", "ids")})
            put("overnight_filed", "Facts filed last night", last.get("filed"), "mdi:file-document-plus", "facts")
        eng = getattr(A, "engine", None)
        if eng is not None:
            put("automations_owned", "Automations run by Home Mind", len(getattr(eng, "owned", ()) or ()),
                "mdi:robot-happy", "automations")
        u = getattr(A, "usage", None)
        if u is not None:
            try:
                t = u.summary(1)["today"]
                put("model_calls_today", "Model calls today", t["calls"], "mdi:robot", "calls")
                put("model_tokens_today", "Model tokens today", t["tokens"], "mdi:counter", "tokens")
                if t["cost"] is not None:
                    put("model_cost_today", "Model cost today", t["cost"], "mdi:currency-usd", "USD")
            except Exception:  # noqa: BLE001
                pass
        ha = getattr(A, "ha", None)
        if ha is not None:
            put("notifications_waiting", "Phone notifications waiting for quiet hours to end",
                len(getattr(ha, "held", []) or []), "mdi:bell-sleep", "notifications")
        put("version", "Version", VERSION, "mdi:information-outline")
        return out

    def describe(self):
        ov = self._overrides()
        settings = {}
        for k, s in SETTINGS.items():
            d = {x: s[x] for x in ("kind", "label", "icon", "min", "max", "step", "unit") if x in s}
            d["value"] = self.A.opts.get(k)
            d["overridden"] = k in ov
            if k in ov:
                d["configuration"] = ov[k]["base"]
            settings[k] = d
        actions = {}
        for k, a in ACTIONS.items():
            t = self.running.get(k)
            last = self.last.get(k)
            actions[k] = {"label": a["label"], "icon": a["icon"], "running": bool(t and not t.done()),
                          "last": last and dict(last, at=_iso(last["at"]))}
        return {"settings": settings, "actions": actions, "sensors": self.sensors()}


def _short(res, n=400):
    """Keep a job's result small enough for a sensor attribute."""
    if isinstance(res, dict):
        return {k: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:120])
                for k, v in list(res.items())[:12]}
    if isinstance(res, list):
        return {"count": len(res)}
    return str(res)[:n] if res is not None else None
