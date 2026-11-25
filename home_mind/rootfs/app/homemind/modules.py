"""House modules (0.37): anything in Home Assistant on the House tab, picked from a list, beside the fixed room map.

  modules    the tiles you added (kept in the database as "house_modules"): an entity and a name each. Each tile is
             drawn by its domain: lights, switches, fans, covers, climate and humidifiers get their controls; media
             players get play/pause, skip and volume; sensors show their value; images and cameras show a picture;
             to-do lists show their open items. A first load fills in a few defaults (the scale, the speakers, the
             Ideas lists, the printer camera); after that the list is yours.
  catalog    every Home Assistant entity of a domain a tile can draw, for the picker
  embeds     Home Assistant dashboard views shown inside the House tab ("house_embeds"), each a path such as
             /lovelace/0, opened with ?kiosk (the kiosk-mode card hides HA's header) in a frame. Works when the panel
             is opened through Home Assistant (the frame is on the same address); not from the direct port.

Safety: a tile can only switch what is on it, with the same per-domain allowlist as the room controls (home_ctrl).
Locks, alarms, valves, garage doors and gates are never controllable from a tile; they show their state only.
"""
import logging
import re

log = logging.getLogger("homemind.modules")

KV = "house_modules"
KV_EMBEDS = "house_embeds"
MAX_MODULES = 40
MAX_EMBEDS = 8

# domains a tile can draw; the ones in CONTROL can also be switched from the tile
DRAW = ("light", "switch", "input_boolean", "fan", "cover", "climate", "humidifier", "media_player", "sensor",
        "binary_sensor", "image", "camera", "todo", "script", "scene", "button", "input_number", "number", "lock",
        "weather", "person", "device_tracker", "input_select", "select")
CONTROL = ("light", "switch", "input_boolean", "fan", "cover", "climate", "humidifier", "media_player", "script",
           "scene", "button")
NEVER = ("lock", "alarm_control_panel", "valve")
NEVER_COVER = ("garage", "gate", "door")          # cover device classes that open the house

DEFAULTS = [
    {"entity": "sensor.yitz_weight", "name": "Weight"},
    {"entity": "sensor.yitz_body_fat", "name": "Body fat"},
    {"entity": "media_player.my_bedroom_speaker", "name": "Bedroom speaker"},
    {"entity": "media_player.kitchen_speaker", "name": "Kitchen speaker"},
    {"entity": "image.a1_camera", "name": "Printer camera"},
    {"entity": "todo.ideas_projects", "name": "Ideas: projects"},
    {"entity": "todo.ideas_random", "name": "Ideas: random"},
    {"entity": "todo.ideas_book_and_framework", "name": "Ideas: book"},
]
DEFAULT_EMBEDS = [{"name": "Overview", "path": "/lovelace/0"}]

ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
PATH = re.compile(r"^/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)?$")
DEAD = (None, "unavailable", "unknown")


def _store(A):
    return A.mind.store


def _slug(entity):
    return entity.replace(".", "_")


def load(A):
    """The modules list; the defaults the first time."""
    s = _store(A)
    mods = s.kv_get(KV)
    if mods is None:
        mods = [dict(d, id=_slug(d["entity"])) for d in DEFAULTS]
        s.kv_set(KV, mods)
    return mods


def embeds(A):
    e = _store(A).kv_get(KV_EMBEDS)
    if e is None:
        e = [dict(x) for x in DEFAULT_EMBEDS]
        _store(A).kv_set(KV_EMBEDS, e)
    return e


def controllable(A):
    """Entities on tiles that a tile may switch (home_ctrl.check() adds these to the room controls)."""
    try:
        mods = (_store(A).kv_get(KV) or []) if getattr(A, "mind", None) else []
    except Exception:  # noqa: BLE001
        return set()
    return {m["entity"] for m in mods if m["entity"].split(".")[0] in CONTROL}


def blocked(entity, state=None):
    """Why a tile may not switch this entity, or None."""
    dom = entity.split(".")[0]
    if dom in NEVER:
        return f"{dom} can't be switched from a tile"
    if dom == "cover" and ((state or {}).get("attributes") or {}).get("device_class") in NEVER_COVER:
        return "garage doors, gates and doors can't be opened from a tile"
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def view(m, s):
    """One tile, from its module and its Home Assistant state."""
    s = s or {}
    a = s.get("attributes") or {}
    ent, state = m["entity"], s.get("state")
    dom = ent.split(".")[0]
    out = {"id": m["id"], "entity": ent, "domain": dom, "name": m.get("name") or a.get("friendly_name") or ent,
           "state": state, "available": state not in DEAD or dom in ("image", "camera", "todo"),
           "unit": a.get("unit_of_measurement"), "icon": a.get("icon"), "found": bool(s),
           "control": dom in CONTROL and not blocked(ent, s)}
    if dom == "light":
        b = a.get("brightness")
        out.update(on=state == "on", level=round(b / 255 * 100) if isinstance(b, (int, float)) else (100 if state == "on" else 0))
    elif dom in ("switch", "input_boolean", "binary_sensor"):
        out.update(on=state == "on")
    elif dom == "fan":
        out.update(on=state == "on", level=a.get("percentage") or 0)
    elif dom == "cover":
        out.update(on=state == "open", level=a.get("current_tilt_position", a.get("current_position")),
                   tilt="current_tilt_position" in a)
    elif dom == "climate":
        out.update(on=state not in ("off",) + DEAD, level=a.get("temperature"), current=a.get("current_temperature"),
                   min=a.get("min_temp", 50), max=a.get("max_temp", 86))
    elif dom == "humidifier":
        out.update(on=state == "on", level=a.get("humidity"), current=a.get("current_humidity"),
                   min=a.get("min_humidity", 30), max=a.get("max_humidity", 80))
    elif dom == "media_player":
        out.update(on=state in ("playing", "on"), title=a.get("media_title"), artist=a.get("media_artist"),
                   volume=None if a.get("volume_level") is None else round(a["volume_level"] * 100),
                   picture=bool(a.get("entity_picture")))
    elif dom in ("sensor", "input_number", "number"):
        v = _num(state)
        out.update(value=state if v is None else (round(v, 1) if abs(v) < 1000 else round(v)))
    elif dom in ("image", "camera"):
        out.update(version=a.get("access_token", "")[:8] + str(state or ""), picture=bool(a.get("entity_picture")))
    elif dom == "todo":
        out.update(open=int(_num(state) or 0))
    return out


async def build(A):
    ha = A.ha if getattr(A, "ha", None) and A.ha.available else None
    mods = load(A)
    out = {"modules": [], "embeds": embeds(A), "available": bool(ha)}
    if not ha:
        out["modules"] = [view(m, None) for m in mods]
        return out
    st = {s["entity_id"]: s for s in (await ha.states() or [])}
    tiles = [view(m, st.get(m["entity"])) for m in mods]
    todos = [t["entity"] for t in tiles if t["domain"] == "todo" and t["found"]]
    if todos:
        items = await todo_items(ha, todos)
        for t in tiles:
            if t["domain"] == "todo":
                t["items"] = items.get(t["entity"], [])
    out["modules"] = tiles
    return out


async def todo_items(ha, entities):
    """Open items of each to-do list ({entity: [summary, ...]}), at most 6 each."""
    try:
        r = await ha.post_json("services/todo/get_items?return_response",
                               {"entity_id": entities, "status": ["needs_action"]}, timeout=15)
    except Exception as e:  # noqa: BLE001
        log.info("todo items: %s", e)
        return {}
    resp = (r or {}).get("service_response") or {}
    return {e: [i.get("summary") for i in (resp.get(e) or {}).get("items", [])][:6] for e in entities}


async def catalog(A, q=""):
    """Entities a tile can draw, for the picker, matching q (words in the name or id); at most 60."""
    ha = A.ha if getattr(A, "ha", None) and A.ha.available else None
    if not ha:
        return {"entities": [], "available": False}
    have = {m["entity"] for m in load(A)}
    words = [w for w in re.split(r"\s+", (q or "").lower().strip()) if w]
    out = []
    for s in await ha.states() or []:
        ent = s["entity_id"]
        if ent.split(".")[0] not in DRAW or ent in have:
            continue
        name = (s.get("attributes") or {}).get("friendly_name") or ent
        hay = (name + " " + ent).lower()
        if all(w in hay for w in words):
            out.append({"entity": ent, "name": name, "domain": ent.split(".")[0], "state": s.get("state")})
    out.sort(key=lambda x: (x["state"] in DEAD, x["domain"], x["name"].lower()))
    return {"entities": out[:60], "total": len(out), "available": True}


def add(A, entity, name=""):
    entity = str(entity or "").strip()
    if not ENTITY.match(entity) or entity.split(".")[0] not in DRAW:
        raise ValueError(f"{entity or 'that'} can't be shown as a tile")
    mods = load(A)
    if any(m["entity"] == entity for m in mods):
        raise ValueError(f"{entity} is already on the House tab")
    if len(mods) >= MAX_MODULES:
        raise ValueError(f"at most {MAX_MODULES} tiles")
    mods.append({"id": _slug(entity), "entity": entity, "name": str(name or "").strip()[:40]})
    _store(A).kv_set(KV, mods)
    return {"added": entity, "count": len(mods)}


def remove(A, mid):
    mods = load(A)
    keep = [m for m in mods if m["id"] != mid]
    if len(keep) == len(mods):
        raise ValueError("no such tile")
    _store(A).kv_set(KV, keep)
    return {"removed": mid, "count": len(keep)}


def move(A, mid, step):
    mods = load(A)
    i = next((n for n, m in enumerate(mods) if m["id"] == mid), None)
    if i is None:
        raise ValueError("no such tile")
    j = max(0, min(len(mods) - 1, i + (1 if int(step) > 0 else -1)))
    mods.insert(j, mods.pop(i))
    _store(A).kv_set(KV, mods)
    return {"moved": mid, "to": j}


def rename(A, mid, name):
    mods = load(A)
    for m in mods:
        if m["id"] == mid:
            m["name"] = str(name or "").strip()[:40]
            _store(A).kv_set(KV, mods)
            return {"renamed": mid}
    raise ValueError("no such tile")


def embed_add(A, name, path):
    path = str(path or "").strip().split("?")[0].rstrip("/")
    if not PATH.match(path) or path.split("/")[1] in ("api", "auth", "hassio", "local", "static", "config"):
        raise ValueError("give a dashboard path like /lovelace/0 or /dashboard-bedroom/console")
    e = embeds(A)
    if len(e) >= MAX_EMBEDS:
        raise ValueError(f"at most {MAX_EMBEDS} dashboards")
    e.append({"name": str(name or path).strip()[:30], "path": path})
    _store(A).kv_set(KV_EMBEDS, e)
    return {"embeds": e}


def embed_remove(A, path):
    e = [x for x in embeds(A) if x["path"] != path]
    _store(A).kv_set(KV_EMBEDS, e)
    return {"embeds": e}


async def picture(A, entity):
    """(bytes, content type) of an image or camera tile's picture, through Home Assistant's proxy, or None."""
    if entity not in {m["entity"] for m in load(A)} or entity.split(".")[0] not in ("image", "camera", "media_player"):
        return None
    ha = A.ha if getattr(A, "ha", None) and A.ha.available else None
    if not ha:
        return None
    s = await ha.state(entity) or {}
    tok = (s.get("attributes") or {}).get("access_token")
    if not tok:
        return None
    dom = entity.split(".")[0]
    kind = {"image": "image_proxy", "camera": "camera_proxy", "media_player": "media_player_proxy"}[dom]
    import aiohttp
    url, headers = ha._ctl(f"{kind}/{entity}")
    try:
        async with ha.session.get(url, params={"token": tok}, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return None
            return await r.read(), r.headers.get("Content-Type", "image/jpeg")
    except Exception:  # noqa: BLE001
        log.debug("picture of %s not reachable", entity, exc_info=True)
        return None
