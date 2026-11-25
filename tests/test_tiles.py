"""House tiles and dashboards (0.37, modules.py): defaults on first load, add/move/rename/remove, the picker,
per-domain views, the dashboard path guard, and that a tile only unlocks controls for what's on it, never locks,
garage doors or gates. No Home Assistant needed.

  python tests/test_tiles.py
"""
import asyncio
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind import modules, home_ctrl  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


class Store:
    def __init__(self):
        self.kv = {}

    def kv_get(self, k, default=None):
        return self.kv.get(k, default)

    def kv_set(self, k, v):
        self.kv[k] = v


class FakeHA:
    available = True

    def __init__(self):
        self.st = {
            "sensor.yitz_weight": {"entity_id": "sensor.yitz_weight", "state": "181.46", "attributes": {"unit_of_measurement": "lb"}},
            "media_player.kitchen_speaker": {"entity_id": "media_player.kitchen_speaker", "state": "playing",
                                             "attributes": {"volume_level": 0.35, "media_title": "Song", "entity_picture": "/x"}},
            "todo.ideas_projects": {"entity_id": "todo.ideas_projects", "state": "3", "attributes": {}},
            "light.desk": {"entity_id": "light.desk", "state": "on", "attributes": {"brightness": 128, "friendly_name": "Desk lamp"}},
            "lock.front": {"entity_id": "lock.front", "state": "locked", "attributes": {}},
            "cover.garage": {"entity_id": "cover.garage", "state": "closed", "attributes": {"device_class": "garage"}},
            "automation.x": {"entity_id": "automation.x", "state": "on", "attributes": {}},
        }

    async def states(self):
        return list(self.st.values())

    async def state(self, eid):
        return self.st.get(eid)

    async def post_json(self, path, payload, timeout=10):
        return {"service_response": {"todo.ideas_projects": {"items": [{"summary": "Forklift"}, {"summary": "Pebble"}]}}}


A = NS(mind=NS(store=Store()), ha=FakeHA())

mods = modules.load(A)
check("first load seeds the defaults", len(mods) == len(modules.DEFAULTS) and mods[0]["entity"] == "sensor.yitz_weight")
check("the overview dashboard is there by default", modules.embeds(A)[0]["path"] == "/lovelace/0")

out = asyncio.run(modules.build(A))
by = {t["entity"]: t for t in out["modules"]}
check("a sensor shows its value", by["sensor.yitz_weight"]["value"] == 181.5 and by["sensor.yitz_weight"]["unit"] == "lb")
check("a speaker shows volume 0-100", by["media_player.kitchen_speaker"]["volume"] == 35 and by["media_player.kitchen_speaker"]["control"])
check("a to-do list shows its open items", by["todo.ideas_projects"]["items"] == ["Forklift", "Pebble"])
check("a missing entity says so", by["image.a1_camera"]["found"] is False)

cat = asyncio.run(modules.catalog(A, "desk"))
check("the picker finds by name", [e["entity"] for e in cat["entities"]] == ["light.desk"])
cat = asyncio.run(modules.catalog(A, ""))
check("the picker leaves out what's added and what can't be drawn",
      "sensor.yitz_weight" not in [e["entity"] for e in cat["entities"]] and "automation.x" not in [e["entity"] for e in cat["entities"]])

modules.add(A, "light.desk")
check("add puts it last", modules.load(A)[-1]["entity"] == "light.desk")
check("adding twice is refused", raises(lambda: modules.add(A, "light.desk")))
check("an automation can't be a tile", raises(lambda: modules.add(A, "automation.x")))
check("a bad id is refused", raises(lambda: modules.add(A, "light.desk; rm")))
modules.move(A, "light_desk", -1)
check("move steps one place", modules.load(A)[-2]["entity"] == "light.desk")
modules.rename(A, "light_desk", "Lamp")
check("rename sticks", next(m for m in modules.load(A) if m["id"] == "light_desk")["name"] == "Lamp")

# controls: only what's on a tile, same allowlist, never locks or garage doors
check("a light on a tile can be dimmed",
      home_ctrl.check("light.desk", "turn_on", {"brightness_pct": 140}, modules.controllable(A))[1].get("brightness_pct") == 100)
check("a light not on a tile can't", raises(lambda: home_ctrl.check("light.other", "turn_on", {}, modules.controllable(A))))
check("speaker volume is clamped to 0-1",
      home_ctrl.check("media_player.kitchen_speaker", "volume_set", {"volume_level": 3}, modules.controllable(A))[1]["volume_level"] == 1)
modules.add(A, "lock.front")
check("a lock tile never becomes controllable", "lock.front" not in modules.controllable(A) and modules.blocked("lock.front"))
check("a garage door can't be opened from a tile", modules.blocked("cover.garage", FakeHA().st["cover.garage"]) is not None)
check("a plain blind can", modules.blocked("cover.blind", {"attributes": {"device_class": "blind"}}) is None)

modules.remove(A, "light_desk")
check("remove takes it off", "light.desk" not in {m["entity"] for m in modules.load(A)})

# dashboards
modules.embed_add(A, "Bedroom", "/dashboard-bedroom/console?kiosk")
check("a dashboard path is cleaned and kept", modules.embeds(A)[-1] == {"name": "Bedroom", "path": "/dashboard-bedroom/console"})
for bad in ("/api/states", "https://evil.example/x", "/auth/token", "//evil", "/a/b/c", "/config/integrations", "javascript:alert(1)"):
    check(f"not a dashboard: {bad}", raises(lambda b=bad: modules.embed_add(A, "x", b)))
modules.embed_remove(A, "/dashboard-bedroom/console")
check("remove a dashboard", all(e["path"] != "/dashboard-bedroom/console" for e in modules.embeds(A)))

# pictures only for tiles
check("no picture for an entity that isn't a tile", asyncio.run(modules.picture(A, "camera.someone_elses")) is None)

print("\nPASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
