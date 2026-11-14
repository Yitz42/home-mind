"""The live comfort controller against a fake Home Assistant: votes store snapshots, variables get written within
limits, nothing is written while disabled, and the breaking point switches it off."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "home_mind", "rootfs", "app"))
from homemind.comfort import VARS, Comfort  # noqa: E402
from homemind.mind import Mind  # noqa: E402


class FakeHA:
    def __init__(self):
        self.s = {"climate.bedroom_temp": {"state": "heat", "attributes": {"current_temperature": 69, "temperature": 70, "hvac_action": "idle"}},
                  "humidifier.humidifier_bedroom": {"state": "off", "attributes": {"current_humidity": 41}},
                  "fan.dyson_c2a_us_mdb9996a": {"state": "off", "attributes": {}},
                  "input_datetime.alarm": {"state": "07:00:00"}, "input_number.steady_sleep_hours": {"state": "5.5"},
                  "input_number.steady_hrv": {"state": "38"}, "sensor.yitz_weight": {"state": "167.8"},
                  "input_boolean.mind_comfort_enabled": {"state": "on"}, "input_number.mind_temp_min": {"state": "64"},
                  "input_number.mind_temp_max": {"state": "76"}, "input_number.mind_humidity_min": {"state": "40"},
                  "input_number.mind_humidity_max": {"state": "50"}, "input_button.comfort_cold": {"state": "2026-11-14T10:00:00"},
                  "input_button.comfort_ok": {"state": "unknown"}}
        self.calls, self.notes = [], []

    async def state(self, e):
        return self.s.get(e)

    async def call(self, d, sv, data):
        self.calls.append((d, sv, data))
        if d == "input_number":
            self.s[data["entity_id"]] = {"state": str(data["value"])}
        if d == "input_boolean" and sv == "turn_off":
            self.s[data["entity_id"]] = {"state": "off"}

    async def notify(self, *a):
        self.notes.append(a)

    async def sensor(self, *a):
        pass


FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  ({extra})"))
    if not cond:
        FAILS.append(name)


async def main():
    t = {"now": datetime(2026, 11, 14, 15, 0).timestamp()}
    ha, mind = FakeHA(), Mind(tempfile.mkdtemp(), None)
    c = Comfort(mind, ha, clock=lambda: t["now"])
    r = await c.vote("cold", source="test")
    snap = r["snapshot"]
    check("vote stores room, situation and signals", snap["room"] == 69 and snap["situation"] == "day"
          and snap["signals"]["sleep_hours"] == 5.5 and snap["signals"]["humidity"] == 41, snap)
    tgt = float(ha.s[VARS["target"]]["state"])
    check("cold vote raises the target above the room", tgt >= 70.5, tgt)
    check("writes go to variables only, never devices", all(d in ("input_number", "input_boolean") for d, _, _ in ha.calls),
          ha.calls)
    check("humidity target is the middle of its limits", float(ha.s[VARS["humidity"]]["state"]) == 45)
    check("sleep and wake temperatures written", VARS["sleep"] in ha.s and VARS["wake"] in ha.s)
    # limits hold
    ha.s["input_number.mind_temp_max"] = {"state": "70"}
    t["now"] += 60
    await c.vote("cold", source="test")
    check("target never above the max limit", float(ha.s[VARS["target"]]["state"]) <= 70, ha.s[VARS["target"]])
    # disabled: nothing written
    ha.s["input_boolean.mind_comfort_enabled"] = {"state": "off"}
    n = len(ha.calls)
    t["now"] += 400
    st = await c.cycle()
    check("nothing written while disabled", len(ha.calls) == n and "off" in st["action"], st)
    # button press counts as a vote (first sight is only remembered)
    ha.s["input_boolean.mind_comfort_enabled"] = {"state": "on"}
    await c.poll_buttons()
    ha.s["input_button.comfort_cold"] = {"state": "2026-11-14T15:20:00"}
    out = await c.poll_buttons()
    check("a new button press is a vote", len(out) == 1 and out[0]["snapshot"]["feeling"] == "cold")
    check("old press at first sight is not a vote", len(c.votes()) == 3, len(c.votes()))
    ha.s["input_button.comfort_ok"] = {"state": "2026-11-14T15:21:00"}
    out = await c.poll_buttons()
    check("first ever press of a never-pressed button is a vote", len(out) == 1 and out[0]["snapshot"]["feeling"] == "ok")
    # breaking point
    ha.s["climate.bedroom_temp"]["attributes"]["current_temperature"] = 58
    t["now"] += 7200      # past the vote nudge
    await c.cycle()
    t["now"] += 31 * 60
    st = await c.cycle()
    check("breaking point turns control off and says why", ha.s[VARS["enabled"]]["state"] == "off" and ha.notes
          and "paused" in st["action"], st)
    print("\nALL PASSED" if not FAILS else f"\n{len(FAILS)} FAILED")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
