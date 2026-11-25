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
            "input_datetime.alarm": {"entity_id": "input_datetime.alarm", "state": "07:00:00",
                                     "attributes": {"has_time": True, "has_date": False}},
            "select.desk_scene": {"entity_id": "select.desk_scene", "state": "None",
                                  "attributes": {"options": ["None", "Sunrise", "Movie"]}},
            "update.core": {"entity_id": "update.core", "state": "on",
                            "attributes": {"title": "Home Assistant Core", "latest_version": "2026.12.0"}},
            "update.zigbee": {"entity_id": "update.zigbee", "state": "off", "attributes": {"title": "Zigbee"}},
        }

    async def states(self):
        return list(self.st.values())

    async def state(self, eid):
        return self.st.get(eid)

    async def get_json(self, path, params=None):
        self.hist = (path, params)
        t0 = path.split("/")[-1].replace("Z", "+00:00")
        from datetime import datetime, timedelta
        base = datetime.fromisoformat(t0)
        return [[{"entity_id": "sensor.yitz_weight", "state": "183", "last_changed": (base + timedelta(days=1)).isoformat()},
                 {"state": "unavailable", "last_changed": (base + timedelta(days=2)).isoformat()},
                 {"state": "181.5", "last_changed": (base + timedelta(days=5)).isoformat()}]]

    async def post_json(self, path, payload, timeout=10):
        return {"service_response": {"todo.ideas_projects": {"items": [{"summary": "Forklift"}, {"summary": "Pebble"}]}}}


A = NS(mind=NS(store=Store()), ha=FakeHA())

mods = modules.load(A)
sleep = modules.ADDED_LATER["sleep_buttons"]
later = [d for k in modules.ADDED_LATER for d in modules.ADDED_LATER[k]]
check("first load seeds the defaults, the sleep buttons, the alarm and updates",
      len(mods) == len(modules.DEFAULTS) + len(later) and mods[0]["entity"] == "sensor.yitz_weight")
B = NS(mind=NS(store=Store()), ha=FakeHA())
B.mind.store.kv_set(modules.KV, [{"id": "light_desk", "entity": "light.desk", "name": ""}])
got = modules.load(B)
check("an existing list gets the sleep buttons once, at the end",
      got[0]["entity"] == "light.desk" and [m["entity"] for m in got[1:]] == [d["entity"] for d in later])
modules.remove(B, "input_button_comfort_hot")
check("a removed sleep button stays removed", "input_button.comfort_hot" not in {m["entity"] for m in modules.load(B)})
check("a sleep button can be pressed from its tile",
      home_ctrl.check("input_button.steady_bedtime", "press", {}, modules.controllable(A))[0] == "input_button")
check("but not given anything else", raises(lambda: home_ctrl.check("input_button.steady_bedtime", "turn_on", {}, modules.controllable(A))))
check("the overview dashboard is there by default", modules.embeds(A)[0]["path"] == "/lovelace/0")

out = asyncio.run(modules.build(A))
by = {t["entity"]: t for t in out["modules"]}
check("a sensor shows its value", by["sensor.yitz_weight"]["value"] == 181.5 and by["sensor.yitz_weight"]["unit"] == "lb")
check("a speaker shows volume 0-100", by["media_player.kitchen_speaker"]["volume"] == 35 and by["media_player.kitchen_speaker"]["control"])
check("a to-do list shows its open items", by["todo.ideas_projects"]["items"] == ["Forklift", "Pebble"])
check("a missing entity says so", by["image.a1_camera"]["found"] is False)

cat = asyncio.run(modules.catalog(A, "desk"))
check("the picker finds by name", [e["entity"] for e in cat["entities"]][0] == "light.desk")
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

# 0.39: alarm, scenes, trends, updates
by = {t["entity"]: t for t in asyncio.run(modules.build(A))["modules"]}
check("the alarm tile shows its time and can be set", by["input_datetime.alarm"]["time"] == "07:00" and by["input_datetime.alarm"]["control"])
ok = home_ctrl.check("input_datetime.alarm", "set_datetime", {"time": "6:45"}, modules.controllable(A))[1]
check("an alarm time is cleaned to HH:MM:00", ok["time"] == "06:45:00")
for bad in ("25:00", "7", "07:61", "tomorrow"):
    check(f"a bad alarm time is refused: {bad}",
          raises(lambda b=bad: home_ctrl.check("input_datetime.alarm", "set_datetime", {"time": b}, modules.controllable(A))))
check("the updates tile counts what's waiting", by["homemind.updates"]["open"] == 1
      and by["homemind.updates"]["items"] == ["Home Assistant Core 2026.12.0"] and not by["homemind.updates"]["control"])
check("the updates tile can't be switched", "homemind.updates" not in modules.controllable(A))
check("only Home Mind's own virtual tiles can be added", raises(lambda: modules.add(A, "homemind.anything")))
w = by["sensor.yitz_weight"]["trend"]
check("a number tile gets a 7-day line", len(w) == modules.TREND_POINTS and w[-1] == 181.5 and 183 in w and w[0] is None)
check("the history call asks for numbers only", "minimal_response" in A.ha.hist[1] and "no_attributes" in A.ha.hist[1])
check("scene list names follow the light", modules.scene_select("light.rgbic_tv_light_bars_2") == "select.rgbic_tv_light_bars_scene_2"
      and modules.scene_select("light.dreamview_p1") == "select.dreamview_p1_scene")
check("the room map's lights can pick a scene", "select.rgbic_neon_rope_lights_scene" in home_ctrl.controllable())
check("but no other select", raises(lambda: home_ctrl.check("select.other", "select_option", {"option": "x"}, modules.controllable(A))))
modules.add(A, "light.desk")
by = {t["entity"]: t for t in asyncio.run(modules.build(A))["modules"]}
check("a light tile shows its scene list", by["light.desk"]["scenes"] == {"entity": "select.desk_scene", "current": None, "count": 3})
sc = asyncio.run(modules.scenes(A, "light.desk"))
check("its scene names, without None", sc["options"] == ["Sunrise", "Movie"])
check("scenes of a light that isn't on the House tab are refused", raises(lambda: asyncio.run(modules.scenes(A, "light.other"))))
check("a scene from its own list is allowed", modules.scene_ok(A.ha.st["select.desk_scene"], "Movie"))
check("a made-up scene isn't", not modules.scene_ok(A.ha.st["select.desk_scene"], "Hack") and not modules.scene_ok(A.ha.st["select.desk_scene"], "None"))
modules.remove(A, "light_desk")

# pictures only for tiles
check("no picture for an entity that isn't a tile", asyncio.run(modules.picture(A, "camera.someone_elses")) is None)

print("\nPASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
