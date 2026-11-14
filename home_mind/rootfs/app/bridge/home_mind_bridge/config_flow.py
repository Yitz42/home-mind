"""One step: check the Home Mind app is reachable, then make the webhook id."""
import json
import os

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import webhook
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_WEBHOOK_ID, DOMAIN

CONN = os.path.join(os.path.dirname(__file__), "connection.json")


def read_conn():
    with open(CONN) as f:
        return json.load(f)


class HomeMindFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                conn = await self.hass.async_add_executor_job(read_conn)
                s = async_get_clientsession(self.hass)
                async with s.get(conn["url"] + "/api/status", headers={"X-Home-Mind-Token": conn["token"]},
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status != 200:
                        raise RuntimeError(r.status)
            except FileNotFoundError:
                errors["base"] = "no_app"
            except Exception:  # noqa: BLE001
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="Home Mind",
                                               data={CONF_WEBHOOK_ID: webhook.async_generate_id()})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}), errors=errors)
