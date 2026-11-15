"""Talking to Home Assistant from inside the add-on: sensors with the test numbers, notifications, and installing
the Home Mind Bridge integration (which gives the mind a webhook address Claude can reach through HA Cloud)."""
import json
import os
import shutil
import socket

import aiohttp

SUPERVISOR = "http://supervisor"
HA_CONFIG = "/homeassistant"
BRIDGE_SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bridge", "home_mind_bridge")


class HA:
    def __init__(self, session):
        self.session = session
        self.token = os.environ.get("SUPERVISOR_TOKEN")

    @property
    def available(self):
        return bool(self.token)

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

    async def state(self, entity_id):
        if not self.token:
            return None
        try:
            async with self.session.get(f"{SUPERVISOR}/core/api/states/{entity_id}",
                                        headers={"Authorization": f"Bearer {self.token}"},
                                        timeout=aiohttp.ClientTimeout(total=10)) as r:
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
        if not self.token:
            return None
        try:
            async with self.session.get(f"{SUPERVISOR}/core/api/states",
                                        headers={"Authorization": f"Bearer {self.token}"},
                                        timeout=aiohttp.ClientTimeout(total=20)) as r:
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

    async def call(self, domain, service, data):
        return await self._post(f"services/{domain}/{service}", data)

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
