"""Control accuracy: does "dim the den a bit" hit the right device? A realistic house (names that overlap: kitchen
light / kitchen counter lights, living room lamp / living room ceiling light, two door locks) and labelled requests.

Each request expects one of:
- a set of actions [(entity_id, action, value)]: what should happen;
- "which": too ambiguous, it should ask;
- None: not a plain command (a question, too broad, unclear): the rules must leave it to a model.

Scores:
- right: did exactly the expected thing (or asked, or left it to a model, as expected);
- wrong: acted, but not as expected (the dangerous kind: the wrong device or the wrong action);
- handled: share of plain commands answered without any model.

  python tests/control_bench.py                  # the rule planner (no model); floors checked, exit 1 below them
  python tests/control_bench.py --llama URL      # also score a local control model (llama-server /v1), e.g.
                                                 #   --llama http://HOSTNAME:8081/v1
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind import planner, localctl  # noqa: E402

FLOOR_RIGHT = 0.95      # rules: right on at least this share of all cases
FLOOR_WRONG = 0.0       # rules: never act wrongly
FLOOR_HANDLED = 0.85    # rules: answer at least this share of plain commands without a model
HELD_RIGHT = 0.85       # first run scored 0.90 (0 wrong); never tune the rules to HELD_OUT itself
HELD_WRONG = 0.0

L, S, F, C, K, H, SC = "light.", "switch.", "fan.", "cover.", "lock.", "humidifier.", "script."
HOUSE = {
    "light.kitchen": ("Kitchen Light", "on", {"brightness": 255}),
    "light.kitchen_counter": ("Kitchen Counter Lights", "on", {"brightness": 128}),
    "light.dining_room": ("Dining Room Light", "off", {}),
    "light.living_room_lamp": ("Living Room Lamp", "on", {"brightness": 102}),
    "light.living_room_ceiling": ("Living Room Ceiling Light", "off", {}),
    "light.den_lamp": ("Den Lamp", "on", {"brightness": 128}),
    "light.hall": ("Hall Light", "off", {}),
    "light.bedroom": ("Bedroom Light", "on", {"brightness": 200}),
    "light.nightstand": ("Bedroom Nightstand Lamp", "off", {}),
    "light.bathroom": ("Bathroom Light", "off", {}),
    "light.porch": ("Porch Light", "off", {}),
    "light.office_desk": ("Office Desk Lamp", "on", {"brightness": 255}),
    "fan.bedroom": ("Bedroom Fan", "off", {}),
    "fan.office": ("Office Fan", "on", {"percentage": 50}),
    "switch.kettle": ("Kettle", "off", {}),
    "switch.coffee_maker": ("Coffee Maker", "off", {}),
    "switch.christmas_tree": ("Christmas Tree", "off", {}),
    "cover.living_room_blinds": ("Living Room Blinds", "open", {}),
    "cover.bedroom_blinds": ("Bedroom Blinds", "open", {}),
    "cover.garage_door": ("Garage Door", "closed", {"device_class": "garage"}),
    "lock.front_door": ("Front Door", "locked", {}),
    "lock.back_door": ("Back Door", "locked", {}),
    "humidifier.nursery": ("Nursery Humidifier", "on", {}),
    "script.goodnight": ("Goodnight", "off", {}),
    "script.leaving_home": ("Leaving Home", "off", {}),
    "scene.movie_night": ("Movie Night", "off", {}),
    "scene.dinner": ("Dinner", "off", {}),
    "sensor.living_room_temperature": ("Living Room Temperature", "21", {}),
}
CATALOG = {e: {"name": n, "state": s, "attrs": a, "domain": e.split(".")[0]} for e, (n, s, a) in HOUSE.items()}
ALLOWED = lambda e: not e.startswith("sensor.")          # noqa: E731
LIGHTS = sorted(e for e in HOUSE if e.startswith("light."))
BLINDS = ["cover.bedroom_blinds", "cover.living_room_blinds"]


def on(*e):
    return [(x, "on", None) for x in e]


def off(*e):
    return [(x, "off", None) for x in e]


CASES = [
    # one device, on / off
    ("turn on the kitchen light", on("light.kitchen")),
    ("Turn off the kitchen light.", off("light.kitchen")),
    ("kitchen light off", off("light.kitchen")),
    ("switch on the porch light", on("light.porch")),
    ("porch light on please", on("light.porch")),
    ("could you turn the hall light on", on("light.hall")),
    ("Can you please switch the kettle on?", on("switch.kettle")),
    ("turn the coffee maker on", on("switch.coffee_maker")),
    ("kill the den lamp", off("light.den_lamp")),
    ("den lamp off", off("light.den_lamp")),
    ("bathroom light on", on("light.bathroom")),
    ("turn on the christmas tree", on("switch.christmas_tree")),
    ("shut off the office fan", off("fan.office")),
    ("turn on the bedroom fan", on("fan.bedroom")),
    ("put the dining room light on", on("light.dining_room")),
    ("turn on the nightstand lamp", on("light.nightstand")),
    ("living room lamp on", on("light.living_room_lamp")),
    ("turn off the living room ceiling light", off("light.living_room_ceiling")),
    ("toggle the porch light", [("light.porch", "toggle", None)]),
    ("flip the kettle", [("switch.kettle", "toggle", None)]),
    ("turn on the desk lamp", on("light.office_desk")),
    ("hey turn off the kettle thanks", off("switch.kettle")),
    # levels
    ("set the den lamp to 40%", [("light.den_lamp", "brightness", 40)]),
    ("den lamp to 40 percent", [("light.den_lamp", "brightness", 40)]),
    ("dim the kitchen light to 30", [("light.kitchen", "brightness", 30)]),
    ("bedroom light 10%", [("light.bedroom", "brightness", 10)]),
    ("bring the living room lamp down to 20%", [("light.living_room_lamp", "brightness", 20)]),
    ("set the office fan to 75", [("fan.office", "speed", 75)]),
    ("bedroom fan speed to 30", [("fan.bedroom", "speed", 30)]),
    ("set the nursery humidifier to 45%", [("humidifier.nursery", "humidity", 45)]),
    ("hall light to 0", off("light.hall")),
    ("set the den lamp to 150%", None),
    # relative
    ("dim the den lamp a bit", [("light.den_lamp", "brightness", 25)]),
    ("dim the bedroom light", [("light.bedroom", "brightness", 53)]),
    ("brighten the living room lamp", [("light.living_room_lamp", "brightness", 65)]),
    ("brighten the hall light", [("light.hall", "brightness", 50)]),
    ("turn up the office fan", [("fan.office", "speed", 75)]),
    # colors
    ("make the den lamp blue", [("light.den_lamp", "color", "blue")]),
    ("turn the bedroom light warm white", [("light.bedroom", "color", "warm white")]),
    ("set the living room lamp to red", [("light.living_room_lamp", "color", "red")]),
    # covers, locks
    ("open the living room blinds", [("cover.living_room_blinds", "open", None)]),
    ("close the bedroom blinds", [("cover.bedroom_blinds", "close", None)]),
    ("lower the bedroom blinds", [("cover.bedroom_blinds", "close", None)]),
    ("close all the blinds", [(e, "close", None) for e in BLINDS]),
    ("open the blinds", [(e, "open", None) for e in BLINDS]),
    ("open the garage", [("cover.garage_door", "open", None)]),
    ("close the garage door", [("cover.garage_door", "close", None)]),
    ("lock the front door", [("lock.front_door", "lock", None)]),
    ("unlock the back door", [("lock.back_door", "unlock", None)]),
    ("lock up", [("lock.back_door", "lock", None), ("lock.front_door", "lock", None)]),
    ("lock the door", "which"),
    ("unlock the door", "which"),
    # scripts, scenes
    ("run goodnight", [("script.goodnight", "run", None)]),
    ("goodnight", [("script.goodnight", "run", None)]),
    ("movie night", [("scene.movie_night", "run", None)]),
    ("movie night scene", [("scene.movie_night", "run", None)]),
    ("activate dinner", [("scene.dinner", "run", None)]),
    ("start leaving home", [("script.leaving_home", "run", None)]),
    ("leaving home", [("script.leaving_home", "run", None)]),
    # several devices at once
    ("turn off the kitchen lights", off("light.kitchen", "light.kitchen_counter")),
    ("kitchen lights off", off("light.kitchen", "light.kitchen_counter")),
    ("turn off all the lights", off(*LIGHTS)),
    ("lights off", off(*LIGHTS)),
    ("turn on the living room lights", on("light.living_room_ceiling", "light.living_room_lamp")),
    ("turn off the bedroom lights", off("light.bedroom", "light.nightstand")),
    ("set the kitchen lights to 50%", [("light.kitchen", "brightness", 50), ("light.kitchen_counter", "brightness", 50)]),
    # several steps
    ("lights off and lock up", off(*LIGHTS) + [("lock.back_door", "lock", None), ("lock.front_door", "lock", None)]),
    ("turn off the kitchen light and turn on the porch light", off("light.kitchen") + on("light.porch")),
    ("kettle on and coffee maker on", on("switch.kettle", "switch.coffee_maker")),
    ("den lamp to 20% then close the living room blinds",
     [("light.den_lamp", "brightness", 20), ("cover.living_room_blinds", "close", None)]),
    ("kitchen light off, porch light on, then run goodnight",
     off("light.kitchen") + on("light.porch") + [("script.goodnight", "run", None)]),
    ("turn off all the lights but leave the hall light on", off(*[e for e in LIGHTS if e != "light.hall"])),
    ("lights off except the porch light", off(*[e for e in LIGHTS if e != "light.porch"])),
    ("close the blinds and turn on the living room lamp",
     [(e, "close", None) for e in BLINDS] + on("light.living_room_lamp")),
    # too ambiguous: ask
    ("turn off the light", "which"),
    ("turn on the lamp", "which"),
    ("dim the kitchen", "which"),
    ("turn off the fan", "which"),
    ("set the fan to 40", "which"),
    # not plain commands: leave to a model
    ("what's the temperature in the living room", None),
    ("is the garage door open", None),
    ("is the front door locked?", None),
    ("where did I leave the ladder", None),
    ("turn everything off", None),
    ("all off", None),
    ("make it cozy in here", None),
    ("it's too dark in the den", None),
    ("turn it on", None),
    ("turn on the toaster", None),
    ("set an alarm for 7", None),
    ("remind me to turn off the oven", None),
    ("open the pod bay doors", None),
    ("turn the heat up", None),
    ("play some music in the kitchen", None),
    ("when did I last close the garage", None),
    ("what lights are on", None),
    ("turn off the lights in an hour", None),
    ("lock the front door at 10pm", None),
    ("turn on the kitchen light and tell me a joke", None),
]


# Written after the rules were tuned on CASES and never tuned on: a check on wording the rules weren't fitted to.
HELD_OUT = [
    ("please turn the porch light off", off("light.porch")),
    ("switch off the bathroom light now", off("light.bathroom")),
    ("I'd like the kettle on", None),
    ("get the coffee maker going", None),
    ("hall light, on", on("light.hall")),
    ("turn the dining room light on, please", on("light.dining_room")),
    ("office desk lamp off", off("light.office_desk")),
    ("set office desk lamp brightness to 60", [("light.office_desk", "brightness", 60)]),
    ("dining room light at 35 percent", [("light.dining_room", "brightness", 35)]),
    ("make the nightstand lamp 15%", [("light.nightstand", "brightness", 15)]),
    ("dim the office desk lamp a little", [("light.office_desk", "brightness", 75)]),
    ("brighten up the den lamp", None),
    ("change the dining room light to green", [("light.dining_room", "color", "green")]),
    ("make the porch light purple", [("light.porch", "color", "purple")]),
    ("raise the living room blinds", [("cover.living_room_blinds", "open", None)]),
    ("shut the bedroom blinds", [("cover.bedroom_blinds", "close", None)]),
    ("close the garage", [("cover.garage_door", "close", None)]),
    ("lock the back door", [("lock.back_door", "lock", None)]),
    ("unlock front door", [("lock.front_door", "unlock", None)]),
    ("trigger movie night", [("scene.movie_night", "run", None)]),
    ("dinner scene", [("scene.dinner", "run", None)]),
    ("run the goodnight routine", [("script.goodnight", "run", None)]),
    ("living room lights on", on("light.living_room_ceiling", "light.living_room_lamp")),
    ("turn off every light", off(*LIGHTS)),
    ("all the lights off please", off(*LIGHTS)),
    ("porch light on and front door locked", None),
    ("turn on the kettle, then the coffee maker", None),
    ("bedroom fan off and bedroom light off", off("fan.bedroom", "light.bedroom")),
    ("turn off the kitchen counter lights", off("light.kitchen_counter")),
    ("kitchen counter lights to 25%", [("light.kitchen_counter", "brightness", 25)]),
    ("lights", None),
    ("turn the lights down", None),
    ("is the kettle on", None),
    ("did I lock the back door", None),
    ("open the front door", None),
    ("turn off the TV", None),
    ("good night", None),
    ("turn off the bedroom", None),
    ("set the temperature to 70", None),
    ("turn on the porch light at sunset", None),
]


def judge(got, want):
    """-> "right", "wrong" (acted, not as expected) or "missed" (asked or left to a model when it should have acted,
    or acted when it should have left it)."""
    if want is None:
        if got is None:
            return "right"
        return "wrong" if any(s.get("entity_id") for s in got) else "missed"
    if want == "which":
        if got and any("which" in s for s in got):
            return "right"
        return "wrong" if got and any(s.get("entity_id") for s in got) else "missed"
    if got is None or any("which" in s for s in got):
        return "missed"
    acts = {(s["entity_id"], s["action"], s.get("value")) for s in got}
    return "right" if acts == set(want) else "wrong"


def score(predict, show=8, cases=None):
    cases = CASES if cases is None else cases
    tally = {"right": 0, "wrong": 0, "missed": 0}
    plain, handled, misses = 0, 0, []
    for text, want in cases:
        got = predict(text)
        v = judge(got, want)
        tally[v] += 1
        if want is not None:
            plain += 1
            handled += v == "right"
        if v != "right":
            misses.append((v, text, want, got))
    n = len(cases)
    res = {"cases": n, "right": round(tally["right"] / n, 3), "wrong": round(tally["wrong"] / n, 3),
           "handled": round(handled / max(1, plain), 3), "wrong_n": tally["wrong"], "missed_n": tally["missed"]}
    for v, text, want, got in misses[:show]:
        g = [(s.get("entity_id"), s.get("action"), s.get("value")) if s.get("entity_id") else ("which", s.get("which"))
             for s in got] if got else got
        print(f"  {v.upper():6} {text!r}: want {want}, got {g}")
    return res


def rules(text):
    return planner.plan(text, CATALOG, ALLOWED)


def llama(url):
    """A local control model behind llama-server: one tool call, resolved the way Home Mind does it."""
    def predict(text):
        body = json.dumps({"messages": [{"role": "developer", "content": localctl.SYSTEM},
                                        {"role": "user", "content": text}],
                           "tools": localctl.TOOLS, "temperature": 0, "max_tokens": 96}).encode()
        u = url.rstrip("/") + ("/chat/completions" if url.rstrip("/").endswith("/v1") else "/v1/chat/completions")
        req = urllib.request.Request(u, body, {"Content-Type": "application/json"})
        m = json.loads(urllib.request.urlopen(req, timeout=30).read())["choices"][0]["message"]
        tool, args = localctl.parse_call({"tool_calls": m.get("tool_calls"), "text": m.get("content") or ""})
        p = localctl.to_plan(tool, args, CATALOG, ALLOWED)
        if p.get("not_a_command") or tool is None:
            return None
        if p.get("which"):
            return [{"which": p["which"], "device": args.get("device")}]
        return [p] if p.get("entity_id") else None
    return predict


if __name__ == "__main__":
    r = score(rules)
    print("rules:", r)
    h = score(rules, cases=HELD_OUT)
    print("rules, held-out wording:", h)
    ok = r["right"] >= FLOOR_RIGHT and r["wrong"] <= FLOOR_WRONG and r["handled"] >= FLOOR_HANDLED and \
        h["wrong"] <= HELD_WRONG and h["right"] >= HELD_RIGHT
    if "--llama" in sys.argv:
        url = sys.argv[sys.argv.index("--llama") + 1]
        print("local model (one action per request, so several-step requests count against it):", score(llama(url)))
    print("PASS" if ok else f"FAIL (floors: right >= {FLOOR_RIGHT}, wrong <= {FLOOR_WRONG}, handled >= {FLOOR_HANDLED})")
    sys.exit(0 if ok else 1)
