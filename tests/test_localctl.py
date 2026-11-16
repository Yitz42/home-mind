"""The small local control model (localctl, 0.33): training data written from the house's control list, reading
FunctionGemma's tool calls, resolving device names, and /api/do using it first with the general model as fallback.
No network, no model: fakes stand in for llama-server.

  python tests/test_localctl.py
"""
import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "home_mind", "rootfs", "app"))


def _fake_aiohttp():
    """Always the stand-in (also where aiohttp is installed), so main.build() gives routes we can call directly."""
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: None
    m.ClientError = Exception
    m.ClientSession = object
    m.BasicAuth = lambda *a: a
    web = types.ModuleType("aiohttp.web")
    web.middleware = lambda f: f

    class Response:
        def __init__(s, text=None, body=None, status=200, headers=None, content_type=None, **k):
            s.text, s.status, s.headers, s.content_type, s.data = text, status, headers or {}, content_type, None

    class HTTPException(Exception):
        status = 500

        def __init__(s, **k):
            super().__init__(s.status)

    class HTTPNotFound(HTTPException):
        status = 404

    def json_response(data, status=200, dumps=json.dumps, **k):
        r = Response(status=status)
        r.data = json.loads(dumps(data))
        return r

    class RouteTableDef:
        def __init__(s):
            s.routes = []

        def _m(s, meth):
            return lambda path: (lambda f: (s.routes.append((meth, path, f)), f)[1])

        def __getattr__(s, n):
            if n in ("get", "post", "delete", "put"):
                return s._m(n.upper())
            raise AttributeError(n)

    class Application:
        def __init__(s, middlewares=(), **k):
            s.middlewares, s.on_startup, s.on_cleanup, s.routes = middlewares, [], [], []

        def add_routes(s, r):
            s.routes = r.routes
    for n, v in dict(Response=Response, FileResponse=Response, HTTPException=HTTPException, HTTPNotFound=HTTPNotFound,
                     json_response=json_response, RouteTableDef=RouteTableDef, Application=Application).items():
        setattr(web, n, v)
    m.web = web
    sys.modules["aiohttp"], sys.modules["aiohttp.web"] = m, web



_fake_aiohttp()
from homemind import main as M, localctl as L, tokens as T  # noqa: E402
from homemind.home import Home  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []
logging.disable(logging.CRITICAL)


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


THINGS = [("light.kitchen", "Kitchen Light"), ("light.den_lamp", "Den Lamp"), ("light.porch", "Porch Light"),
          ("fan.bedroom", "Bedroom Fan"), ("cover.garage_door", "Garage Door"), ("lock.front_door", "Front Door"),
          ("script.goodnight", "Goodnight"), ("switch.kettle", "Kettle"), ("cover.living_blinds", "Living Room Blinds"),
          ("humidifier.nursery", "Nursery Humidifier"), ("sensor.temp", "Temperature")]


def dataset():
    d = L.build_dataset([{"entity_id": e, "name": n} for e, n in THINGS])
    tr, te = d["train"], d["test"]
    check("training and test examples are written", len(tr) > 200 and len(te) > 50, d["counts"])
    names = {t["function"]["name"] for t in L.TOOLS}
    ok = all(r["messages"][0]["role"] == "developer" and r["messages"][-2]["role"] == "user" and
             r["messages"][-1]["tool_calls"][0]["function"]["name"] in names and r["tools"] == L.TOOLS for r in tr + te)
    check("every example is in the notebook's format (developer, user, assistant tool call, the tool list)", ok)
    check("sensors aren't in the data (only things that can be controlled)",
          not any("temperature" in r["messages"][-2]["content"].lower() and
                  r["messages"][-1]["tool_calls"][0]["function"]["name"] != "not_a_command" for r in tr))
    held = {p[-1] for p in L.PHRASES.values()}
    def phrasing(r):
        return r["messages"][-2]["content"]
    check("held-out wordings never appear in training", not any(
        re.fullmatch(re.escape(h).replace(re.escape("{d}"), ".+").replace(re.escape("{p}"), r"\d+").replace(
            re.escape("{c}"), ".+"), phrasing(r), re.I) for r in tr for h in held))
    unseen = {r["messages"][-1]["tool_calls"][0]["function"]["arguments"].get("device") for r in te} - \
        {r["messages"][-1]["tool_calls"][0]["function"]["arguments"].get("device") for r in tr}
    check("some devices appear only in the test set", len(unseen - {None}) >= 1, unseen)
    check("questions and multi-action requests are labelled not_a_command",
          sum(r["messages"][-1]["tool_calls"][0]["function"]["name"] == "not_a_command" for r in tr) >= 30)
    for r in tr:
        f = r["messages"][-1]["tool_calls"][0]["function"]
        if f["name"] == "set_brightness":
            check("a brightness example names its level in the request", str(f["arguments"]["percent"]) in
                  r["messages"][-2]["content"])
            break
    json.loads(L.jsonl(tr[:3]).splitlines()[0])
    check("the same seed gives the same data", L.build_dataset([{"entity_id": e, "name": n} for e, n in THINGS])["train"]
          == tr)
    nb = json.load(open(os.path.join(ROOT, "tools", "finetune_functiongemma.ipynb")))
    src = "".join(c["source"] if isinstance(c["source"], str) else "".join(c["source"]) for c in nb["cells"])
    check("the notebook loads the files the route writes", "home_mind_control_train.jsonl" in src and
          "home_mind_control_test.jsonl" in src and "google/functiongemma-270m-it" in src)


def parsing():
    cases = [
        ("<start_function_call>call:set_brightness{device:<escape>den lamp<escape>,percent:40}<end_function_call>",
         ("set_brightness", {"device": "den lamp", "percent": 40})),
        ("call:turn_on{device:kitchen light}", ("turn_on", {"device": "kitchen light"})),
        ("call:set_color{device:<escape>porch, front<escape>,color:<escape>warm white<escape>}",
         ("set_color", {"device": "porch, front", "color": "warm white"})),
        ({"tool_calls": [{"function": {"name": "lock", "arguments": {"device": "front door"}}}]},
         ("lock", {"device": "front door"})),
        ({"tool_calls": [{"function": {"name": "open", "arguments": '{"device": "garage"}'}}]},
         ("open", {"device": "garage"})),
        ('{"name": "close", "arguments": {"device": "blinds"}}', ("close", {"device": "blinds"})),
        ("call:delete_everything{x:1}", (None, {})),
        ("I can't help with that", (None, {})),
        ({"tool_calls": "garbage", "text": ""}, (None, {})),
        (None, (None, {})),
    ]
    for reply, want in cases:
        check(f"parse {str(reply)[:50]!r}", L.parse_call(reply) == want, L.parse_call(reply))


def resolving():
    cat = {e: {"name": n} for e, n in THINGS}
    allow = lambda e: not e.startswith("sensor.")
    p = L.to_plan("set_brightness", {"device": "den lamp", "percent": "40"}, cat, allow)
    check("a named light and level become a plan", p == {"entity_id": "light.den_lamp", "action": "brightness",
                                                         "value": 40, "tool": "set_brightness"}, p)
    check("turning on a script runs it", L.to_plan("turn_on", {"device": "goodnight"}, cat, allow)["action"] == "run")
    check("open goes to a cover even when a lock shares the word",
          L.to_plan("open", {"device": "garage"}, cat, allow)["entity_id"] == "cover.garage_door")
    p = L.to_plan("turn_off", {"device": "light"}, cat, allow)
    check("several matches ask which one", p["entity_id"] is None and len(p["which"]) >= 2, p)
    check("an unknown device is not guessed", L.to_plan("turn_on", {"device": "toaster"}, cat, allow)["entity_id"] is None)
    check("a level out of range is clamped", L.to_plan("set_fan_speed", {"device": "bedroom fan", "percent": 250}, cat,
                                                       allow)["value"] == 100)
    check("a level that isn't a number is refused", L.to_plan("set_brightness", {"device": "den lamp", "percent": "lots"},
                                                              cat, allow)["entity_id"] is None)
    check("things off the control list can't be picked", L.to_plan("turn_on", {"device": "temperature"}, cat,
                                                                   allow)["entity_id"] is None)
    check("unlock only applies to locks", L.to_plan("unlock", {"device": "garage door"}, cat, allow)["entity_id"] is None)


class FakeHA:
    def __init__(self):
        self.calls = []

    async def state(self, eid):
        return {"state": "on"} if eid == "input_boolean.mind_control_enabled" else None

    async def call(self, dom, svc, data):
        self.calls.append((dom, svc, data))
        return 200


class Small:
    """Stands in for llama-server running the fine-tuned model."""
    type, cfg = "openai", {}

    def __init__(self, replies, fail=False):
        self.replies, self.fail, self.seen = replies, fail, []

    async def call_tool(self, messages, tools, system=None, **k):
        self.seen.append((messages[-1]["content"], tools, system))
        if self.fail:
            raise RuntimeError("laptop asleep")
        text = messages[-1]["content"].lower()
        return {"text": next((r for w, r in self.replies.items() if w in text), "call:not_a_command{reason:?}"),
                "tool_calls": None}


class Big:
    type, cfg = "anthropic", {}

    def __init__(self):
        self.asked = []

    async def chat(self, system, messages, **k):
        self.asked.append(messages[-1]["content"])
        return {"text": json.dumps({"entity_id": "light.kitchen", "action": "off"}), "ms": 1}


def app(small, big=None, opts=None):
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    ha = FakeHA()
    h = Home(st, ha, {"home_controls": ["light.*", "fan.*", "cover.*", "lock.*", "script.*", "switch.*"]})
    h.catalog = {e: {"name": n, "domain": e.split(".")[0], "state": "off", "attrs": {}, "kind": "device"}
                 for e, n in THINGS}
    models = {"control": small}
    if big:
        models["haiku"] = big
    A = types.SimpleNamespace(home=h, opts=dict({"control_model": "control", "rule_planner": False}, **(opts or {})),
                              models=types.SimpleNamespace(models=models, get=lambda n: models[n]))
    for n in ("YES", "NO", "PENDING_SECONDS"):
        setattr(A, n, getattr(M.App, n))
    for n in ("do", "pending_reply", "has_pending", "local_plan", "_run_plan", "_run_steps", "_said"):
        setattr(A, n, getattr(M.App, n).__get__(A))
    return A, ha


def wiring():
    small = Small({"den": "<start_function_call>call:set_brightness{device:<escape>den lamp<escape>,percent:30}"
                          "<end_function_call>",
                   "garage": "call:open{device:<escape>garage door<escape>}",
                   "lights": "call:turn_off{device:<escape>light<escape>}",
                   "toaster": "call:turn_on{device:<escape>toaster<escape>}"})
    big = Big()
    A, ha = app(small, big)
    r = asyncio.run(A.do("dim the den lamp to 30"))
    check("the local model plans it, the house does it, the big model isn't asked",
          ha.calls == [("light", "turn_on", {"entity_id": "light.den_lamp", "brightness_pct": 30})] and not big.asked,
          (r, ha.calls, big.asked))
    check("it was given the tool list and the training system prompt", small.seen[-1][1] == L.TOOLS and
          small.seen[-1][2] == L.SYSTEM)
    ha.calls.clear()
    r = asyncio.run(A.do("open the garage", conversation_id="c1"))
    check("a garage door planned by the small model still needs a yes", r.get("confirm_needed") and not ha.calls, r)
    r = asyncio.run(A.do("turn off the lights"))
    check("several matches: it asks which one, does nothing", r.get("which") and "Which one" in r["answer"] and
          not ha.calls, r)
    r = asyncio.run(A.do("what's the weather like"))
    check("not a command: the general model decides", big.asked and ha.calls, (r, big.asked, ha.calls))
    big.asked.clear()
    ha.calls.clear()
    asyncio.run(A.do("turn on the toaster"))
    check("a name it can't place: the general model gets the whole list", big.asked, big.asked)
    A2, ha2 = app(Small({}, fail=True), Big())
    r = asyncio.run(A2.do("dim the den lamp to 30"))
    check("the small model's machine is off: the general model still works", ha2.calls, r)
    A3, ha3 = app(small, None)
    r = asyncio.run(A3.do("what's the weather like"))
    check("no general model and not a command: a clear error, nothing done", "error" in r and not ha3.calls, r)
    A4, ha4 = app(small, Big(), opts={"control_model": ""})
    asyncio.run(A4.do("dim the den lamp to 30"))
    check("off by default: the option empty means the general model plans", not small.seen[-1][0] == "x" and
          ha4.calls == [("light", "turn_off", {"entity_id": "light.kitchen"})], ha4.calls)


def scope():
    check("the training data route needs admin", T.needed("GET", "/api/control_model/dataset") == "admin")


async def _para():
    class Teacher:
        async def chat(self, system, messages, **k):
            return {"text": json.dumps(["could you dim the den lamp to 40 percent", "den lamp, 40% please",
                                        "make it darker", "set den lamp to forty", 5])}
    rows = [L.example("set the den lamp to 40%", "set_brightness", {"device": "den lamp", "percent": 40})]
    out = await L.paraphrase(Teacher(), rows, k=4)
    return [r["messages"][-2]["content"] for r in out]


def para():
    got = asyncio.run(_para())
    check("teacher wordings are kept only when the device and number survive",
          got == ["could you dim the den lamp to 40 percent", "den lamp, 40% please"], got)


def addon():
    import subprocess
    import yaml
    base = os.path.join(ROOT, "home_mind_control")
    cfg = yaml.safe_load(open(os.path.join(base, "config.yaml")))
    tr = yaml.safe_load(open(os.path.join(base, "translations", "en.yaml")))["configuration"]
    check("control add-on: options, schema and translations name the same settings",
          set(cfg["options"]) == set(cfg["schema"]) == set(tr), (cfg["options"], cfg["schema"], tr))
    check("control add-on: no port opened on the network", not cfg.get("ports"))
    check("control add-on: the share folder is read-only", cfg["map"] == [{"type": "share", "read_only": True}])
    check("control add-on: run.sh parses as shell", subprocess.run(["sh", "-n", os.path.join(base, "run.sh")]).returncode == 0)
    mode = subprocess.run(["git", "ls-files", "-s", "home_mind_control/run.sh"], cwd=ROOT, capture_output=True,
                          text=True).stdout
    check("control add-on: run.sh is executable in git", not mode or mode.startswith("100755"), mode)
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "options.json"), "w") as f:
        json.dump({"model_file": "/nonexistent/m.gguf", "threads": 6, "context": 4096}, f)
    script = open(os.path.join(base, "run.sh")).read().replace("/data/options.json", os.path.join(d, "options.json"))
    out = subprocess.run(["timeout", "2", "sh", "-c", script], capture_output=True, text=True).stdout
    check("control add-on: waits for a missing model file and says where to put it",
          "Waiting for the model file /nonexistent/m.gguf" in out, out)


dataset()
addon()
parsing()
resolving()
wiring()
scope()
para()
print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
