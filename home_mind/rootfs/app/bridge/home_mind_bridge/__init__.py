"""Home Mind Bridge: gives the Home Mind app an address Claude can reach (a Home Assistant webhook, so it works
through Home Assistant Cloud), and services so automations and voice (Jeeves) can remember and recall."""
import json
import logging
import os

import aiohttp
import voluptuous as vol
from aiohttp import web
from homeassistant.components import persistent_notification, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import CONF_WEBHOOK_ID, DOMAIN, NOTIFY_ID

_LOGGER = logging.getLogger(__name__)
CONN = os.path.join(os.path.dirname(__file__), "connection.json")
PASS_IN = ("Content-Type", "Accept", "Mcp-Session-Id", "Mcp-Protocol-Version", "Last-Event-ID")
PASS_OUT = ("Content-Type", "Mcp-Session-Id")


def _read_conn():
    with open(CONN) as f:
        return json.load(f)


async def _conn(hass):
    return await hass.async_add_executor_job(_read_conn)


async def _call(hass, method, path, body=None):
    conn = await _conn(hass)
    s = async_get_clientsession(hass)
    async with s.request(method, conn["url"] + path, json=body, headers={"X-Home-Mind-Token": conn["token"]},
                         timeout=aiohttp.ClientTimeout(total=120)) as r:
        return await r.json()


async def _links(hass, webhook_id):
    out = {}
    try:
        from homeassistant.components import cloud
        if cloud.async_active_subscription(hass):
            out["cloudhook"] = await cloud.async_get_or_create_cloudhook(hass, webhook_id)
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("no cloudhook: %s", e)
    try:
        out["remote"] = get_url(hass, allow_internal=False, allow_ip=False, prefer_cloud=True) + f"/api/webhook/{webhook_id}"
    except NoURLAvailableError:
        pass
    try:
        out["local"] = get_url(hass, allow_external=False) + f"/api/webhook/{webhook_id}"
    except NoURLAvailableError:
        pass
    return out


async def _show_links(hass, webhook_id):
    links = await _links(hass, webhook_id)
    lines = ["Link Claude to Home Mind: in Claude, Settings > Connectors > Add custom connector, and paste one of:", ""]
    if "remote" in links:
        lines.append(f"- **Remote (Home Assistant Cloud):** `{links['remote']}`")
    if "cloudhook" in links:
        lines.append(f"- Cloudhook (backup): `{links['cloudhook']}`")
    if "local" in links:
        lines.append(f"- On your home network only: `{links['local']}`")
    lines += ["", "Treat these like passwords: anyone with the address can use your Home Mind. "
              "Removing and re-adding the integration makes a new address."]
    persistent_notification.async_create(hass, "\n".join(lines), "Home Mind link", NOTIFY_ID)
    return links


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    wid = entry.data[CONF_WEBHOOK_ID]

    async def handle(hass, webhook_id, request: web.Request):
        try:
            conn = await _conn(hass)
        except OSError:
            return web.json_response({"error": "Home Mind app not found"}, status=503)
        if request.method == "GET":
            return web.Response(status=405, headers={"Allow": "POST"})
        headers = {k: request.headers[k] for k in PASS_IN if k in request.headers}
        headers["X-Home-Mind-Token"] = conn["token"]
        body = await request.read()
        s = async_get_clientsession(hass)
        try:
            async with s.request(request.method, conn["url"] + "/mcp", data=body or None, headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=300)) as r:
                data = await r.read()
                out = {k: r.headers[k] for k in PASS_OUT if k in r.headers}
                return web.Response(status=r.status, body=data, headers=out)
        except (aiohttp.ClientError, TimeoutError) as e:
            return web.json_response({"error": f"Home Mind app unreachable: {e}"}, status=502)

    webhook.async_register(hass, DOMAIN, "Home Mind", wid, handle, local_only=False,
                           allowed_methods=["POST", "GET", "DELETE"])
    entry.async_on_unload(lambda: webhook.async_unregister(hass, wid))

    async def remember(call: ServiceCall):
        d = dict(call.data)
        return await _call(hass, "POST", "/api/remember", {"text": d["text"], "title": d.get("title", ""),
                                                           "shelf": d.get("shelf", "inbox"), "tags": d.get("tags", []),
                                                           "key": d.get("key"), "kind": d.get("kind", "note"),
                                                           "source": "home_assistant"})

    async def recall(call: ServiceCall):
        from urllib.parse import urlencode
        d = dict(call.data)
        q = {"q": d["query"], "limit": d.get("limit", 5)}
        if d.get("shelf"):
            q["shelf"] = d["shelf"]
        items = await _call(hass, "GET", "/api/recall?" + urlencode(q))
        return {"items": [{"id": i["id"], "title": i["title"], "text": i["body"], "shelf": i["shelf"],
                           "updated": i["updated"]} for i in items]}

    async def show_link(call: ServiceCall):
        await _show_links(hass, wid)

    hass.services.async_register(DOMAIN, "remember", remember, vol.Schema({
        vol.Required("text"): cv.string, vol.Optional("title"): cv.string, vol.Optional("shelf"): cv.string,
        vol.Optional("tags"): vol.All(cv.ensure_list, [cv.string]), vol.Optional("key"): cv.string,
        vol.Optional("kind"): cv.string}), supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "recall", recall, vol.Schema({
        vol.Required("query"): cv.string, vol.Optional("shelf"): cv.string,
        vol.Optional("limit", default=5): vol.All(int, vol.Range(1, 25))}), supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "show_link", show_link)

    if not entry.data.get("shown"):
        await _show_links(hass, wid)
        hass.config_entries.async_update_entry(entry, data={**entry.data, "shown": True})
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    for s in ("remember", "recall", "show_link"):
        hass.services.async_remove(DOMAIN, s)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    try:
        from homeassistant.components import cloud
        await cloud.async_delete_cloudhook(hass, entry.data[CONF_WEBHOOK_ID])
    except Exception:  # noqa: BLE001
        pass
