"""Talking to Home Assistant from inside the add-on: sensors with the test numbers, notifications, and installing
the Home Mind Bridge integration (which gives the mind a webhook address Claude can reach through HA Cloud)."""
import json
import os
import re
import shutil
import socket

import aiohttp

SUPERVISOR = os.environ.get("HOME_MIND_SUPERVISOR", "http://supervisor")   # overridable for local testing
HA_CONFIG = "/homeassistant"
BRIDGE_SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bridge", "home_mind_bridge")


# services that open, unlock, disarm or open a valve: refused here unless the caller says the person approved it
GUARDED_DOMAINS = ("lock", "alarm_control_panel", "valve")


def guard(domain, service, data, approved):
    """The last check before a service call leaves Home Mind: a lock, alarm or valve service, a garage door, gate or
    door cover, or a script/scene named like an opener, needs approved=True (Home.act sets it once its own checks,
    phone approval included, have passed). Raises PermissionError."""
    if approved:
        return
    from .home import OPENS_RUN, SENSITIVE_COVERS
    ents = (data or {}).get("entity_id")
    ents = " ".join(ents) if isinstance(ents, (list, tuple)) else str(ents or "")
    if domain in GUARDED_DOMAINS or (domain == "cover" and SENSITIVE_COVERS.search(ents)) or \
            (domain in ("script", "scene") and OPENS_RUN.search(ents)) or \
            (domain == "homeassistant" and service in ("turn_on", "toggle") and
             re.search(r"\b(?:lock|alarm_control_panel|valve)\.", ents)):
        raise PermissionError(f"{domain}.{service} needs the person's approval (refused by the service guard)")


class HA:
    def __init__(self, session):
        self.session = session
        self.token = os.environ.get("SUPERVISOR_TOKEN")
        self.user_token = ""    # option ha_user_token: a non-admin Home Assistant user for control and readings
        self.user_url = "http://homeassistant:8123"

    @property
    def available(self):
        return bool(self.token or self.user_token)

    def _ctl(self, path):
        """(url, headers) for service calls and state reads: the limited user when set, else the Supervisor."""
        if self.user_token:
            return f"{self.user_url.rstrip('/')}/api/{path}", {"Authorization": f"Bearer {self.user_token}"}
        return f"{SUPERVISOR}/core/api/{path}", {"Authorization": f"Bearer {self.token}"}

    async def _post(self, path, payload):
        if not self.token:
            return None
        async with self.session.post(f"{SUPERVISOR}/core/api/{path}", json=payload,
                                     headers={"Authorization": f"Bearer {self.token}"},
                                     timeout=aiohttp.ClientTimeout(total=15)) as r:
            return r.status

    async def post_json(self, path, payload, timeout=30):
        """POST to Home Assistant and return its JSON answer (for services that respond)."""
        if not self.token:
            return None
        async with self.session.post(f"{SUPERVISOR}/core/api/{path}", json=payload,
                                     headers={"Authorization": f"Bearer {self.token}"},
                                     timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status >= 400:
                raise RuntimeError(f"{path}: {r.status} {(await r.text())[:200]}")
            return await r.json()

    async def render(self, template, timeout=20):
        """A Home Assistant template rendered to text (POST /api/template), or None."""
        if not self.token:
            return None
        try:
            async with self.session.post(f"{SUPERVISOR}/core/api/template", json={"template": template},
                                         headers={"Authorization": f"Bearer {self.token}"},
                                         timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return await r.text() if r.status == 200 else None
        except Exception:  # noqa: BLE001
            return None

    async def state(self, entity_id):
        if not self.available:
            return None
        url, headers = self._ctl(f"states/{entity_id}")
        try:
            async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json() if r.status == 200 else None
        except Exception:  # noqa: BLE001
            return None

    async def get_json(self, path, params=None):
        if not self.token:
            return None
        try:
            async with self.session.get(f"{SUPERVISOR}/core/api/{path}", params=params or {},
                                        headers={"Authorization": f"Bearer {self.token}"},
                                        timeout=aiohttp.ClientTimeout(total=30)) as r:
                return await r.json() if r.status == 200 else None
        except Exception:  # noqa: BLE001
            return None

    async def states(self):
        if not self.available:
            return None
        url, headers = self._ctl("states")
        try:
            async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
                return await r.json() if r.status == 200 else None
        except Exception:  # noqa: BLE001
            return None

    async def history(self, entity_ids, start):
        """State history since start (ISO time), one list per entity, with attributes."""
        if not self.token:
            return None
        from datetime import datetime, timezone
        end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            async with self.session.get(f"{SUPERVISOR}/core/api/history/period/{start}",
                                        # without end_time Home Assistant returns only one day after start
                                        params={"filter_entity_id": ",".join(entity_ids), "end_time": end},
                                        headers={"Authorization": f"Bearer {self.token}"},
                                        timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    self.last_error = f"history {r.status}: {(await r.text())[:200]}"
                    return None
                return await r.json()
        except Exception as e:  # noqa: BLE001
            self.last_error = f"history: {e!r}"
            return None

    async def call(self, domain, service, data, approved=False):
        status, _, _ = await self.call_ctx(domain, service, data, approved)
        return status

    async def call_ctx(self, domain, service, data, approved=False):
        """A service call that also returns its context id. Home Assistant answers with the states the call
        changed, each carrying the call's context (an empty list when nothing changed, e.g. already on).
        Returns (status, context id or None, changed states or None). Goes through guard() first."""
        guard(domain, service, data, approved)
        if not self.available:
            return None, None, None
        url, headers = self._ctl(f"services/{domain}/{service}")
        async with self.session.post(url, json=data, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
            try:
                body = await r.json(content_type=None)
            except Exception:  # noqa: BLE001 - an answer without JSON still has its status
                body = None
            from .decisions import context_of
            ctx, states = context_of(body)
            return r.status, ctx, states

    # ---- phone notifications: quiet hours and a daily limit -----------------------------------------------------
    quiet = None        # ((h, m), (h, m)): no phone pushes from start to end (may run past midnight); None = off
    cap = 0             # most phone pushes a day; 0 = no limit

    def in_quiet(self, now=None):
        if not self.quiet:
            return False
        from datetime import datetime
        now = now or datetime.now()
        t, (a, b) = (now.hour, now.minute), self.quiet
        return a <= t < b if a < b else (t >= a or t < b) if a != b else False

    TIERS = ("safety", "important", "routine")

    async def push(self, target, data, tier="routine", now=False, batch=False):
        """Send a phone notification (notify.mobile_app_...). By tier (design spec step 5, interrupt priority):
          safety     always, at once: ignores quiet hours and the daily limit, and isn't counted
          important  ignores the daily limit (counted); waits for quiet hours to end unless now=True (time-bound,
                     e.g. a phone approval the person just asked for)
          routine    waits for quiet hours to end, skipped past today's limit
        The Home Assistant notification list always gets it."""
        from datetime import date
        if tier not in self.TIERS:
            raise ValueError(f"tier is one of {', '.join(self.TIERS)}")
        batcher = getattr(self, "batcher", None)
        if batch and tier == "routine" and batcher is not None:
            batcher.add(data.get("title"), data.get("message"))
            return "batched"                     # it comes with the morning check-in
        held = self.__dict__.setdefault("held", [])
        sent = self.__dict__.setdefault("sent", {})
        day = getattr(self, "day", None)         # the check-in (day.py): a rough day keeps every device quiet
        state = day.get() if day is not None else None
        if state == "rough" and tier == "routine":
            self.__dict__["quieted"] = self.__dict__.get("quieted", 0) + 1
            return "quiet"                       # routine asks expire quietly on a rough day
        if tier != "safety" and not (tier == "important" and now) and (self.in_quiet() or state == "rough"):
            held.append((target, data, tier))
            return "held"
        today = date.today().isoformat()
        if tier == "routine":
            # the daily limit counts every counted push; the day's budget (good 4, low 1, rough 0) routine ones only
            over_cap = bool(self.cap) and sent.get(today, 0) >= self.cap
            over_budget = day is not None and self.__dict__.setdefault("routine_sent", {}).get(today, 0) >= day.budget()
            if over_cap or over_budget:
                self.__dict__["capped"] = self.__dict__.get("capped", 0) + 1
                return "capped"
        if "." not in target:
            target = "notify." + target        # option written as "mobile_app_phone"
        dom, svc = target.split(".", 1)
        await self.call(dom, svc, data)
        hook = getattr(self, "on_push", None)
        if hook is not None:
            try:
                hook(tier)              # burden per day (measure.py): asks the house made of you
            except Exception:  # noqa: BLE001
                pass
        if tier != "safety":
            self.sent = {today: sent.get(today, 0) + 1}
        if tier == "routine":
            rs = self.__dict__.setdefault("routine_sent", {})
            self.routine_sent = {today: rs.get(today, 0) + 1}
        return "sent"

    async def flush_held(self):
        """After quiet hours: send what waited, oldest first (within the daily limit)."""
        held = self.__dict__.get("held") or []
        if not held or self.in_quiet():
            return 0
        self.held, n = [], 0
        for item in held:
            target, data, tier = item if len(item) == 3 else (*item, "routine")
            try:
                if await self.push(target, data, tier) == "sent":
                    n += 1
            except Exception:  # noqa: BLE001
                pass
        return n

    async def sensor(self, entity_id, state, attrs=None):
        try:
            return await self._post(f"states/{entity_id}", {"state": state, "attributes": attrs or {}})
        except Exception:  # noqa: BLE001 - sensors are best effort
            return None

    async def notify(self, title, message, nid="home_mind"):
        try:
            return await self._post("services/persistent_notification/create",
                                    {"title": title, "message": message, "notification_id": nid})
        except Exception:  # noqa: BLE001
            return None

    def install_bridge(self, token, port=8099):
        """Copy the bridge integration into HA's custom_components when it is missing or older, and write how to
        reach this add-on next to it. Returns 'installed', 'updated', 'current' or None (not in HA)."""
        if not os.path.isdir(HA_CONFIG):
            return None
        dst = os.path.join(HA_CONFIG, "custom_components", "home_mind_bridge")
        with open(os.path.join(BRIDGE_SRC, "manifest.json")) as f:
            want = json.load(f)["version"]
        have = None
        try:
            with open(os.path.join(dst, "manifest.json")) as f:
                have = json.load(f)["version"]
        except (OSError, ValueError, KeyError):
            pass
        result = "current"
        if have != want:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copytree(BRIDGE_SRC, dst, dirs_exist_ok=True)
            result = "installed" if have is None else "updated"
        conn = {"url": f"http://{socket.gethostname()}:{port}", "token": token}
        path = os.path.join(dst, "connection.json")
        try:
            with open(path) as f:
                same = json.load(f) == conn
        except (OSError, ValueError):
            same = False
        if not same:
            with open(path, "w") as f:
                json.dump(conn, f)
        return result
