"""Reaching the Home Mind app: its address and token sit in connection.json, written by the app next to this file."""
import json
import os

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

CONN = os.path.join(os.path.dirname(__file__), "connection.json")


def _read_conn():
    with open(CONN) as f:
        return json.load(f)


async def conn(hass):
    return await hass.async_add_executor_job(_read_conn)


async def call(hass, method, path, body=None, timeout=120):
    c = await conn(hass)
    s = async_get_clientsession(hass)
    async with s.request(method, c["url"] + path, json=body, headers={"X-Home-Mind-Token": c["token"]},
                         timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        return await r.json()
