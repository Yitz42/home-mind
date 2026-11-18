"""localctl: a small local model for house control (FunctionGemma, 270M parameters, fine-tuned on this house).

The model only turns a request into one tool call ("dim the den lamp a bit" -> set_brightness(device="den lamp",
percent=40)). Home Mind then resolves the device name against the control list and runs the call through the same
checks as every other action (allowed list, locks and garage doors need a yes). Anything the small model can't place
("not_a_command") goes to the normal model or gets a "which one?".

This file has three parts:
- TOOLS: the tool schema the model is trained and run with (the same list both times).
- build_dataset(): training and test examples written from the house's own device names (for the Colab notebook in
  tools/finetune_functiongemma.ipynb).
- parse_call() and resolve(): reading the model's reply (structured tool_calls, or FunctionGemma's raw
  "<start_function_call>call:name{...}<end_function_call>" text) and turning it into an action act() takes.
"""
import json
import random
import re

DEVICE = {"type": "string", "description": "The device, room or scene as the person said it, e.g. 'kitchen light'."}
PERCENT = {"type": "integer", "description": "A level from 1 to 100."}
LINE = {"type": "string", "description": "The plain log line, keeping the person's own numbers."}


def _tool(name, desc, props=None, required=None):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {"device": DEVICE}, "required": required or ["device"]}}}


TOOLS = [
    _tool("turn_on", "Turn a light, switch, fan or other device on."),
    _tool("turn_off", "Turn a light, switch, fan or other device off."),
    _tool("toggle", "Switch a device to the opposite state."),
    _tool("set_brightness", "Set a light's brightness.", {"device": DEVICE, "percent": PERCENT}, ["device", "percent"]),
    _tool("set_color", "Set a light's color.", {"device": DEVICE, "color": {"type": "string", "description":
                                                                           "A color name, e.g. 'warm white', 'blue'."}},
          ["device", "color"]),
    _tool("set_fan_speed", "Set a fan's speed.", {"device": DEVICE, "percent": PERCENT}, ["device", "percent"]),
    _tool("set_humidity", "Set a humidifier's target humidity.", {"device": DEVICE, "percent": PERCENT},
          ["device", "percent"]),
    _tool("open", "Open a cover: a blind, curtain, garage door or gate."),
    _tool("close", "Close a cover: a blind, curtain, garage door or gate."),
    _tool("lock", "Lock a door lock."),
    _tool("unlock", "Unlock a door lock."),
    _tool("run", "Run a script or activate a scene."),
    _tool("log_sleep", "Log last night's sleep the person reports, as a plain line: 'slept from 11:30 pm to 6:45 am' "
          "or 'slept 6.5 hours'. Only their own sleep that already happened.", {"line": LINE}, ["line"]),
    _tool("log_workout", "Log exercise the person did, as a plain line: 'squat 3x5 at 185 lb, run 3 miles in 28 min' "
          "or 'yoga 30 min'. Only something already done.", {"line": LINE}, ["line"]),
    _tool("not_a_command", "The request isn't one device action: a question, a reminder, chit-chat, several "
          "actions at once, or it doesn't say which device.", {"reason": {"type": "string", "description":
                                                                        "Why, in a few words."}}, ["reason"]),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
LOG_TOOLS = {"log_sleep", "log_workout"}       # sleep and workouts said in words (trained from the phrasebook)
SYSTEM = "You are a model that can do function calling with the following functions"

# tool -> (act() action, the value argument, domains it can apply to)
ACTION = {
    "turn_on": ("on", None, None), "turn_off": ("off", None, None), "toggle": ("toggle", None, None),
    "set_brightness": ("brightness", "percent", {"light"}), "set_color": ("color", "color", {"light"}),
    "set_fan_speed": ("speed", "percent", {"fan"}), "set_humidity": ("humidity", "percent", {"humidifier"}),
    "open": ("open", None, {"cover"}), "close": ("close", None, {"cover"}),
    "lock": ("lock", None, {"lock"}), "unlock": ("unlock", None, {"lock"}),
    "run": ("run", None, {"script", "scene"}),
}

# ---- reading the model's reply -----------------------------------------------------------------------------------
RAW = re.compile(r"(?:<start_function_call>)?\s*call:(\w+)\s*\{(.*?)\}\s*(?:<end_function_call>|$)", re.S)
ARG = re.compile(r"(\w+)\s*:\s*(?:<escape>(.*?)<escape>|\"(.*?)\"|'(.*?)'|([^,}]*))", re.S)


def _value(v):
    v = (v or "").strip()
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def parse_call(reply):
    """A model reply -> (tool name, arguments) or (None, {}). Takes Ollama/OpenAI-style tool_calls (a dict or list),
    a JSON object {"name":..., "arguments":...}, or FunctionGemma's raw text (with or without its special tokens)."""
    calls = None
    if isinstance(reply, dict):
        calls = reply.get("tool_calls") or ([reply] if "name" in reply or "function" in reply else None)
        text = reply.get("text") or reply.get("content") or ""
    elif isinstance(reply, list):
        calls = reply
        text = ""
    else:
        text = str(reply or "")
    for c in calls or []:
        if not isinstance(c, dict):
            continue
        f = c.get("function") if isinstance(c.get("function"), dict) else c
        name, args = f.get("name"), f.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if name in TOOL_NAMES:
            return name, args if isinstance(args, dict) else {}
    m = RAW.search(text)
    if m and m.group(1) in TOOL_NAMES:
        args = {}
        for a in ARG.finditer(m.group(2)):
            raw = next((g for g in a.groups()[1:] if g is not None), "")
            args[a.group(1)] = _value(raw)
        return m.group(1), args
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            j = json.loads(m.group(0))
        except ValueError:
            j = None
        if isinstance(j, dict):
            return parse_call({"tool_calls": [j]})
    return None, {}


# ---- device names ------------------------------------------------------------------------------------------------
def _words(s):
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in ("the", "a", "my", "our", "in", "of")]


def _sing(w):
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def resolve(device, catalog, allowed, tool=None):
    """The device the person named -> (entity_id, candidates). A unique best match on the control list, of a domain the
    tool can act on; several equally good -> (None, [their names]) so Home Mind can ask which one."""
    want = {_sing(w) for w in _words(device)}
    if not want:
        return None, []
    doms = ACTION.get(tool, (None, None, None))[2]
    full = []                       # (extra words in the name, entity): names that hold every word the person said
    for eid, c in catalog.items():
        if not allowed(eid):
            continue
        dom = eid.split(".")[0]
        if doms and dom not in doms:
            continue
        name = {_sing(w) for w in _words(c.get("name"))}
        obj = {_sing(w) for w in _words(eid.split(".", 1)[1].replace("_", " "))}
        if want <= name or want <= obj:
            full.append((len(name - want), eid))
    if not full:
        return None, []             # a partial match ("the pod bay doors" ~ "garage door") is not a guess worth asking about
    full.sort()
    exact = [e for x, e in full if x == 0]
    if len(exact) == 1:
        return exact[0], []         # "kitchen light" when there is also a "kitchen counter lights"
    if len(full) == 1:
        return full[0][1], []       # "desk lamp" -> the only name that has it: "Office Desk Lamp"
    return None, [catalog[e].get("name", e) for _, e in full[:4]]


def to_plan(tool, args, catalog, allowed):
    """A parsed tool call -> a plan for App.do: {"entity_id", "action", "value"} or {"entity_id": None, ...why}."""
    if tool is None:
        return {"entity_id": None, "why": "no tool call"}
    if tool == "not_a_command":
        return {"entity_id": None, "not_a_command": True, "why": str(args.get("reason") or "")[:200]}
    if tool in LOG_TOOLS:                     # a log, not a device action: the ask path files it
        return {"entity_id": None, "log": tool, "line": str(args.get("line") or "")[:200], "why": "a log"}
    if tool not in ACTION:
        return {"entity_id": None, "why": f"unknown tool {tool}"}
    action, vkey, _doms = ACTION[tool]
    eid, cands = resolve(str(args.get("device") or ""), catalog, allowed, tool)
    if not eid:
        return {"entity_id": None, "which": cands, "device": args.get("device"), "why": "which one?" if cands
                else "nothing on the control list by that name"}
    value = args.get(vkey) if vkey else None
    if vkey == "percent":
        try:
            value = max(1, min(100, int(float(value))))
        except (TypeError, ValueError):
            return {"entity_id": None, "why": "no level given"}
    if action == "on" and eid.split(".")[0] in ("script", "scene"):
        action = "run"
    if action in ("lock", "unlock") and eid.split(".")[0] != "lock":
        return {"entity_id": None, "why": "not a lock"}
    return {"entity_id": eid, "action": action, "value": value, "tool": tool}


# ---- training data -----------------------------------------------------------------------------------------------
# (tool, phrasings). {d} is a device name, {p} a percent, {c} a color. Each list is split: the last phrasing of each
# tool is kept out of training, so the test set measures new wording, not memorised sentences.
PHRASES = {
    "turn_on": ["turn on the {d}", "switch on the {d}", "{d} on please", "can you turn the {d} on", "put the {d} on",
                "I need the {d} on", "could you put the {d} on"],
    "turn_off": ["turn off the {d}", "switch the {d} off", "{d} off", "kill the {d}", "shut off the {d}",
                 "could you turn the {d} off", "we don't need the {d} on anymore"],
    "toggle": ["toggle the {d}", "flip the {d}", "switch the {d}", "can you flip the {d} for me"],
    "set_brightness": ["set the {d} to {p}%", "dim the {d} to {p} percent", "{d} at {p}%", "brightness of the {d} to {p}",
                       "make the {d} {p} percent", "bring the {d} down to {p}%", "can the {d} go to {p} percent"],
    "set_color": ["make the {d} {c}", "set the {d} to {c}", "change the {d} color to {c}", "turn the {d} {c}",
                  "I want the {d} in {c}"],
    "set_fan_speed": ["set the {d} to {p}%", "{d} speed {p}", "run the {d} at {p} percent", "turn the {d} to {p}%",
                      "can you put the {d} on {p} percent"],
    "set_humidity": ["set the {d} to {p}%", "{d} humidity {p}", "keep the {d} at {p} percent",
                     "target {p}% on the {d}"],
    "open": ["open the {d}", "raise the {d}", "{d} open please", "can you open the {d}", "let some light in, open the {d}"],
    "close": ["close the {d}", "shut the {d}", "lower the {d}", "{d} closed please", "can you close the {d}"],
    "lock": ["lock the {d}", "lock up the {d}", "make sure the {d} is locked", "{d} lock", "can you lock the {d}"],
    "unlock": ["unlock the {d}", "unlock {d} please", "open the {d} lock", "can you unlock the {d}"],
    "run": ["run {d}", "start {d}", "activate {d}", "{d} please", "go {d}", "turn on {d}", "do the {d} scene"],
}
NOT_COMMANDS = [
    "where did I put the ladder", "what's the wifi password", "is the garage door open", "what temperature is it inside",
    "remind me to call mom at five", "how do I reset the boiler", "what's the weather tomorrow", "thank you",
    "never mind", "turn it on", "turn everything off and lock up", "who is coming for dinner", "tell me a joke",
    "when did we last change the filter", "set it to fifty", "what did Dana say about the car", "good morning",
    "is the front door locked", "how much milk do we have", "add eggs to the shopping list", "open the",
    "what's on the calendar today", "dim", "make it nicer in here", "turn the lights off in every room",
]
COLORS = ["red", "blue", "green", "warm white", "cool white", "purple", "orange", "pink", "yellow"]
TOOL_FOR_DOMAIN = {"light": ["turn_on", "turn_off", "toggle", "set_brightness", "set_color"],
                   "switch": ["turn_on", "turn_off", "toggle"], "input_boolean": ["turn_on", "turn_off", "toggle"],
                   "fan": ["turn_on", "turn_off", "toggle", "set_fan_speed"],
                   "humidifier": ["turn_on", "turn_off", "set_humidity"],
                   "cover": ["open", "close"], "lock": ["lock", "unlock"], "script": ["run"], "scene": ["run"],
                   "media_player": ["turn_on", "turn_off"], "climate": ["turn_on", "turn_off"]}


def name_variants(eid, name):
    """How people say a device: its name, lower case, without the domain word, the object id as words."""
    out = {name.lower().strip()}
    obj = eid.split(".", 1)[1].replace("_", " ")
    out.add(obj)
    stripped = re.sub(r"\b(light|lamp|switch|fan|scene|script)\b", "", name.lower()).strip()
    if stripped and len(stripped) > 2 and stripped != name.lower():
        out.add(stripped)
    return sorted(v for v in out if v)


def example(user, tool, args, history=None):
    msgs = [{"role": "developer", "content": SYSTEM}] + (history or []) + [{"role": "user", "content": user}]
    msgs.append({"role": "assistant", "tool_calls": [{"type": "function", "function": {"name": tool, "arguments": args}}]})
    return {"messages": msgs, "tools": TOOLS}


def build_dataset(things, n_per_device=40, seed=7, test_share=0.2, phrasebook=None):
    """things: [{"entity_id", "name"}] (the control list). -> {"train": [...], "test": [...], "counts": {...}}.
    Test examples use phrasings held out of training, and a share of devices whose names never appear in training,
    so the score says how the model does on new wording and new devices."""
    rnd = random.Random(seed)
    things = [t for t in things if t["entity_id"].split(".")[0] in TOOL_FOR_DOMAIN]
    devices = sorted(things, key=lambda t: t["entity_id"])
    rnd.shuffle(devices)
    n_new = max(1, int(len(devices) * test_share)) if len(devices) >= 5 else 0
    unseen = {t["entity_id"] for t in devices[:n_new]}
    train, test = [], []
    for t in devices:
        dom = t["entity_id"].split(".")[0]
        variants = name_variants(t["entity_id"], t["name"])
        for _ in range(n_per_device):
            tool = rnd.choice(TOOL_FOR_DOMAIN[dom])
            phrases = PHRASES[tool]
            held = rnd.random() < test_share
            ph = phrases[-1] if held else rnd.choice(phrases[:-1])
            d = rnd.choice(variants)
            p = rnd.choice([5, 10, 20, 25, 30, 40, 50, 60, 70, 75, 80, 90, 100])
            c = rnd.choice(COLORS)
            text = ph.format(d=d, p=p, c=c)
            if rnd.random() < 0.3:
                text = text[0].upper() + text[1:]
            args = {"device": d}
            if tool in ("set_brightness", "set_fan_speed", "set_humidity"):
                args["percent"] = p
            elif tool == "set_color":
                args["color"] = c
            ex = example(text, tool, args)
            (test if (held or t["entity_id"] in unseen) else train).append(ex)
    for i, q in enumerate(NOT_COMMANDS):
        ex = example(q, "not_a_command", {"reason": "not one device action"})
        (test if i % 5 == 4 else train).append(ex)
        if i % 5 != 4:
            train.append(example(q.capitalize() + "?", "not_a_command", {"reason": "not one device action"}))
    n_log = 0
    for i, pr in enumerate(phrasebook or []):          # sleep and workouts in the household's own words
        tool = {"sleep": "log_sleep", "workout": "log_workout"}.get(pr.get("kind"))
        ex = example(pr["said"], tool, {"line": pr["plain"]}) if tool else \
            example(pr["said"], "not_a_command", {"reason": "not a log of sleep or a workout"})
        (test if i % 5 == 4 else train).append(ex)
        n_log += 1
    rnd.shuffle(train)
    rnd.shuffle(test)
    return {"train": train, "test": test,
            "counts": {"train": len(train), "test": len(test), "devices": len(devices), "unseen_devices": len(unseen),
                       "phrasebook": n_log}}


def jsonl(rows):
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def score(dataset_rows, predict):
    """predict(messages_without_answer) -> (tool, args). Exact-match accuracy of tool and arguments (device names
    compared as words, numbers as numbers)."""
    ok = 0
    for r in dataset_rows:
        want = r["messages"][-1]["tool_calls"][0]["function"]
        tool, args = predict(r["messages"][:-1])
        good = tool == want["name"]
        if good and tool != "not_a_command":
            for k, v in want["arguments"].items():
                got = args.get(k)
                if k == "device":
                    good &= set(_words(str(got))) == set(_words(str(v)))
                else:
                    good &= str(got).strip().lower() == str(v).strip().lower()
        ok += bool(good)
    return round(ok / max(1, len(dataset_rows)), 3)


PARA_SYSTEM = ("Rewrite a smart-home request in {k} different natural ways a person might say it out loud. Keep the "
               "device words exactly as written and keep every number. Reply with JSON only: a list of strings.")


async def paraphrase(model, rows, k=3, limit=150, seed=7):
    """More varied wording from a bigger model (the 'teacher'): each sampled request rewritten k ways, kept only when
    the device words and numbers survive, so the answer stays right. Only device names and request wording are sent."""
    rnd = random.Random(seed)
    out = []
    pick = [r for r in rows if r["messages"][-1]["tool_calls"][0]["function"]["name"] != "not_a_command"]
    rnd.shuffle(pick)
    for r in pick[:limit]:
        user = r["messages"][-2]["content"]
        args = r["messages"][-1]["tool_calls"][0]["function"]["arguments"]
        try:
            rep = await model.chat(PARA_SYSTEM.format(k=k), [{"role": "user", "content": user}], max_tokens=200,
                                   temperature=0.8)
            m = re.search(r"\[.*\]", rep.get("text") or "", re.S)
            alts = json.loads(m.group(0)) if m else []
        except Exception:  # noqa: BLE001 - a teacher hiccup only means fewer extra examples
            continue
        for a in alts if isinstance(alts, list) else []:
            if not isinstance(a, str) or not a.strip() or len(a) > 200:
                continue
            low = a.lower()
            if str(args.get("device", "")).lower() not in low:
                continue
            if any(str(v) not in a for kk, v in args.items() if kk != "device" and isinstance(v, int)):
                continue
            if "color" in args and str(args["color"]).lower() not in low:
                continue
            out.append(example(a.strip(), r["messages"][-1]["tool_calls"][0]["function"]["name"], dict(args)))
    return out
