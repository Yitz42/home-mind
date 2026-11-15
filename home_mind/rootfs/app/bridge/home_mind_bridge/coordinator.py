"""Reads Home Mind's controls every 30 seconds (GET /api/control) and sends changes back. Every entity of the Home
Mind device reads from here, so one request keeps all of them current."""
import logging
from datetime import timedelta

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import call

_LOGGER = logging.getLogger(__name__)


class HomeMindCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        super().__init__(hass, _LOGGER, name="Home Mind", update_interval=timedelta(seconds=30), config_entry=entry)

    async def _async_update_data(self):
        try:
            data = await call(self.hass, "GET", "/api/control", timeout=15)
        except Exception as e:  # noqa: BLE001  (app stopped, restarting, or older than the controls)
            raise UpdateFailed(f"Home Mind app unreachable: {e}") from e
        if not isinstance(data, dict) or "settings" not in data:
            raise UpdateFailed(f"unexpected answer from Home Mind: {data}")
        return data

    async def _send(self, method, path, body=None):
        try:
            r = await call(self.hass, method, path, body, timeout=20)
        except Exception as e:  # noqa: BLE001
            raise HomeAssistantError(f"Home Mind app unreachable: {e}") from e
        if isinstance(r, dict) and r.get("error"):
            raise HomeAssistantError(f"Home Mind: {r['error']}")
        return r

    async def async_set(self, key, value):
        self.async_set_updated_data(await self._send("POST", "/api/control/set", {"key": key, "value": value}))

    async def async_run(self, action):
        r = await self._send("POST", f"/api/control/run/{action}")
        if isinstance(r, dict) and isinstance(r.get("control"), dict):
            self.async_set_updated_data(r["control"])
        return r
