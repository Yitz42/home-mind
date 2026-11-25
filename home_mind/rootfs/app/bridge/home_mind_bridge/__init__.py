"""Home Mind Bridge: gives the Home Mind app an address Claude can reach (a Home Assistant webhook, so it works
through Home Assistant Cloud), services so automations and voice (Jeeves) can remember and recall, and a Home Mind
device whose switches, numbers, times, buttons and sensors control the app from any dashboard."""
import logging

import aiohttp
import voluptuous as vol
from aiohttp import web
from homeassistant.components import persistent_notification, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .api import call as _call
from .api import conn as _conn
from .const import CONF_WEBHOOK_ID, DOMAIN, NOTIFY_ID
from .coordinator import HomeMindCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["switch", "number", "time", "button", "sensor", "conversation"]
PASS_IN = ("Content-Type", "Accept", "Mcp-Session-Id", "Mcp-Protocol-Version", "Last-Event-ID")
PASS_OUT = ("Content-Type", "Mcp-Session-Id")


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
        if request.method in ("GET", "HEAD"):
            return web.Response(status=405, headers={"Allow": "POST"})
        headers = {k: request.headers[k] for k in PASS_IN if k in request.headers}
        # a person's own link (?token=hm_..., or Authorization: Bearer hm_...) is answered as that person, with that
        # token's scope; the plain link uses the app's main token, as before
        own = request.query.get("token") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        headers["X-Home-Mind-Token"] = own if own.startswith("hm_") else conn["token"]
        if not own.startswith("hm_"):
            headers["X-Home-Mind-Via"] = "webhook"      # the app limits what the plain link may do
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
                           allowed_methods=["POST", "GET", "HEAD"])
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

    ALLOWED = ("/api/pantry", "/api/ask", "/api/do", "/api/journal", "/api/morning", "/api/calendar", "/api/home",
               "/api/patterns", "/api/control")

    async def call_api(call: ServiceCall):
        """Reach one of Home Mind's own endpoints from a script (pantry, meals, ask...)."""
        d = dict(call.data)
        path = "/" + d["path"].lstrip("/")
        base = path.split("?", 1)[0]
        if ".." in path or "\\" in path or not any(base == a or base.startswith(a + "/") for a in ALLOWED):
            raise ValueError(f"{path} is not allowed from Home Assistant")
        method = d.get("method", "POST" if d.get("body") is not None else "GET").upper()
        r = await _call(hass, method, path, d.get("body") if method == "POST" else None)
        return r if isinstance(r, dict) else {"result": r}

    async def show_link(call: ServiceCall):
        await _show_links(hass, wid)

    async def person_link(call: ServiceCall):
        """A link for one person's Claude: a token tied to them (their notes, not anyone else's), shown once."""
        d = dict(call.data)
        tok = await _call(hass, "POST", "/api/tokens", {"name": f"Claude ({d['person']})", "person": d["person"],
                                                        "scope": d.get("scope", "write"), "days": d.get("days", 90)})
        if not isinstance(tok, dict) or not tok.get("token"):
            raise ValueError(f"Home Mind didn't make the token: {tok}")
        links = await _links(hass, wid)
        lines = [f"Claude link for **{d['person']}** (answers as them; expires in {d.get('days', 90)} days, renew "
                 "by making a new one). In their Claude: Settings > Connectors > Add custom connector, and paste:", ""]
        for name in ("remote", "local"):
            if name in links:
                lines.append(f"- {name.capitalize()}: `{links[name]}?token={tok['token']}`")
        lines += ["", "This is shown once. Anyone with it can use Home Mind as this person; revoke it on the Models "
                  "tab (Access tokens)."]
        persistent_notification.async_create(hass, "\n".join(lines), f"Home Mind link for {d['person']}",
                                             f"{NOTIFY_ID}_{d['person'].lower()}")
        return {"person": d["person"], "token_id": tok.get("id")}

    hass.services.async_register(DOMAIN, "remember", remember, vol.Schema({
        vol.Required("text"): cv.string, vol.Optional("title"): cv.string, vol.Optional("shelf"): cv.string,
        vol.Optional("tags"): vol.All(cv.ensure_list, [cv.string]), vol.Optional("key"): cv.string,
        vol.Optional("kind"): cv.string}), supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "recall", recall, vol.Schema({
        vol.Required("query"): cv.string, vol.Optional("shelf"): cv.string,
        vol.Optional("limit", default=5): vol.All(int, vol.Range(1, 25))}), supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "show_link", show_link)
    hass.services.async_register(DOMAIN, "person_link", person_link, vol.Schema({
        vol.Required("person"): cv.string, vol.Optional("scope", default="write"): vol.In(["read", "write", "admin"]),
        vol.Optional("days", default=90): vol.All(int, vol.Range(0, 3650))}), supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, "call", call_api, vol.Schema({
        vol.Required("path"): cv.string, vol.Optional("method"): vol.In(["GET", "POST"]),
        vol.Optional("body"): dict}), supports_response=SupportsResponse.ONLY)

    if not entry.data.get("shown"):
        await _show_links(hass, wid)
        hass.config_entries.async_update_entry(entry, data={**entry.data, "shown": True})

    # The Home Mind device. If the app is not up yet, the webhook and services above still work, and the
    # entities appear on the first successful poll.
    coordinator = HomeMindCoordinator(hass, entry)
    await coordinator.async_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    for s in ("remember", "recall", "show_link", "person_link", "call"):
        hass.services.async_remove(DOMAIN, s)
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    try:
        from homeassistant.components import cloud
        await cloud.async_delete_cloudhook(hass, entry.data[CONF_WEBHOOK_ID])
    except Exception:  # noqa: BLE001
        pass
