"""Plain commands without a model (planner.py) and several actions in one request, through App.do: what runs, what
waits for a yes, what asks "which one?", and what is left to a model. Fakes for Home Assistant and the models.

  python tests/test_planner.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
import types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "home_mind", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


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
from homemind import main as M, planner  # noqa: E402
from homemind.home import Home, plan_action  # noqa: E402
from homemind.store import Store  # noqa: E402
import control_bench as B  # noqa: E402

FAILS = []
logging.disable(logging.CRITICAL)


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeHA:
    def __init__(self):
        self.calls = []

    async def state(self, eid):
        return {"state": "on"} if eid == "input_boolean.mind_control_enabled" else None

    async def call(self, dom, svc, data):
        self.calls.append((dom, svc, data))
        return 200


class Big:
    """The general model: answers with a fixed reply, and counts how often it's asked."""
    type, cfg = "anthropic", {}

    def __init__(self, reply=None, delay=0):
        self.reply, self.delay, self.asked = reply, delay, []

    async def chat(self, system, messages, **k):
        self.asked.append(messages[-1]["content"])
        await asyncio.sleep(self.delay)
        return {"text": json.dumps(self.reply or {"entity_id": None}), "ms": 1}


def app(big=None, opts=None):
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    ha = FakeHA()
    h = Home(st, ha, {"home_controls": ["light.*", "fan.*", "cover.*", "lock.*", "script.*", "scene.*", "switch.*",
                                        "humidifier.*"]})
    h.catalog = {e: dict(c, kind="device") for e, c in B.CATALOG.items() if not e.startswith("sensor.")}
    big = big or Big()
    models = {"haiku": big}
    A = types.SimpleNamespace(home=h, opts=dict(opts or {}), models=types.SimpleNamespace(models=models,
                                                                                         get=lambda n: models[n]))
    for n in ("YES", "NO", "PENDING_SECONDS"):
        setattr(A, n, getattr(M.App, n))
    for n in ("do", "pending_reply", "has_pending", "local_plan", "_run_plan", "_run_steps", "_said", "room_for",
              "_verb"):
        setattr(A, n, getattr(M.App, n).__get__(A))
    A.phone_requests = []

    async def ask_phone(plan, why=None, source="api"):      # stands in for the phone approval (tested in test_safety)
        A.phone_requests.append(plan)
        return {"plan": plan, "approval_sent": True, "approval_id": f"{len(A.phone_requests):012x}",
                "answer": "I've sent a request to your phone."}
    A.ask_phone = ask_phone
    return A, ha, big


def run(A, text, cid=None, **k):
    return asyncio.run(A.do(text, conversation_id=cid, **k))


def single():
    A, ha, big = app()
    r = run(A, "den lamp to 40%")
    check("a plain command runs with no model", ha.calls == [("light", "turn_on", {"entity_id": "light.den_lamp",
                                                                                  "brightness_pct": 40})]
          and not big.asked and r["plan"]["by"] == "rules", (r, ha.calls))
    A2, ha2, big2 = app(big=Big(delay=2))
    t0 = time.time()
    run(A2, "turn on the porch light", deadline=0.2)
    check("and doesn't wait for a slow model", time.time() - t0 < 0.5 and ha2.calls)
    r = run(A, "turn off the light")
    check("too ambiguous: it asks which one and does nothing", r.get("which") and "Which" in r["answer"] and
          len(ha.calls) == 1, r)
    r = run(A, "open the garage", cid="c1")
    check("a garage door from the rules needs a phone approval and isn't moved", r.get("approval_sent") and
          len(ha.calls) == 1 and A.phone_requests[-1]["entity_id"].startswith("cover."), r)
    r = run(A, "yes", cid="c1")
    check("... and a spoken yes doesn't open it", not r.get("confirmed") and ha.calls[-1][1] != "open_cover", r)


def multi():
    A, ha, big = app()
    r = run(A, "lights off and lock up", cid="v1")
    lights = [c for c in ha.calls if c[0] == "light"]
    check("several steps: the lights go off at once", len(lights) == len(B.LIGHTS) and
          all(c[1] == "turn_off" for c in lights), ha.calls)
    check("... the locks each get a phone approval and don't move, and the answer says both",
          len(A.phone_requests) == 2 and all(p["entity_id"].startswith("lock.") for p in A.phone_requests) and
          not any(c[0] == "lock" for c in ha.calls) and r["answer"].startswith("Done:") and "phone" in r["answer"]
          and len(r.get("approvals") or []) == 2, r)
    r = run(A, "yes", cid="v1")
    check("a spoken yes after that doesn't lock them", not any(c[0] == "lock" for c in ha.calls), r)
    ha.calls.clear()
    A.phone_requests.clear()
    r = run(A, "lights off and lock up")
    check("no conversation needed for a phone approval: the lights go off, the locks are sent to the phone",
          ha.calls and not any(c[0] == "lock" for c in ha.calls) and len(A.phone_requests) == 2, r)
    ha.calls.clear()
    r = run(A, "kitchen light off and turn off the fan")
    check("one ambiguous part: it asks and does nothing at all", r.get("which") and not ha.calls, (r, ha.calls))
    r = run(A, "turn off all the lights but leave the hall light on")
    check("'but leave ... on' is respected", ha.calls and not any(c[2]["entity_id"] == "light.hall" for c in ha.calls))
    check("several results are summed up in words", "Done:" in r.get("answer", ""), r)


def fallback():
    A, ha, big = app(big=Big({"entity_id": "light.kitchen", "action": "off"}))
    run(A, "make it cozy in here")
    check("not a plain command: the model decides", big.asked == [big.asked[0]] and "cozy" in big.asked[0] and ha.calls)
    A, ha, big = app(big=Big({"entity_id": "light.kitchen", "action": "off"}), opts={"rule_planner": False})
    run(A, "turn off the porch light")
    check("option rule_planner off: everything goes to the model", big.asked and
          ha.calls == [("light", "turn_off", {"entity_id": "light.kitchen"})])


def model_multi():
    A, ha, big = app(big=Big({"actions": [{"entity_id": "light.den_lamp", "action": "off"},
                                          {"entity_id": "switch.kettle", "action": "on"},
                                          {"entity_id": "lock.front_door", "action": "unlock"}]}),
                     opts={"rule_planner": False})
    r = run(A, "wind down: den off, kettle on, let Sam in", cid="m1")
    check("the general model can plan several actions; the unlock goes to the phone",
          {(c[0], c[1]) for c in ha.calls} == {("light", "turn_off"), ("switch", "turn_on")} and
          A.phone_requests and A.phone_requests[-1]["action"] == "unlock", (r, ha.calls))

    class M_:
        def __init__(self, text):
            self.text = text

        async def chat(self, *a, **k):
            return {"text": self.text}
    things = [{"entity_id": "light.den_lamp", "name": "Den Lamp", "state": "on"}]
    for text, want in [('{"actions": [{"entity_id": "light.den_lamp", "action": "off"}]}', "light.den_lamp"),
                       ('{"actions": "nope"}', None), ('{"actions": [5, {"entity_id": 3}]}', None),
                       ('[1, 2]', None)]:
        p = asyncio.run(plan_action(M_(text), "x", things))
        check(f"model reply {text[:40]!r} is read safely", p.get("entity_id") == want and "actions" not in p, p)


def bench():
    r = B.score(B.rules, show=0)
    h = B.score(B.rules, show=0, cases=B.HELD_OUT)
    check(f"control bench: right {r['right']} (floor {B.FLOOR_RIGHT}), wrong {r['wrong']}",
          r["right"] >= B.FLOOR_RIGHT and r["wrong"] <= B.FLOOR_WRONG and r["handled"] >= B.FLOOR_HANDLED, r)
    check(f"control bench, held-out wording: right {h['right']} (floor {B.HELD_RIGHT}), wrong {h['wrong']}",
          h["right"] >= B.HELD_RIGHT and h["wrong"] <= B.HELD_WRONG, h)


def odd():
    for t in ["", "   ", "?", "and and and", "turn on the " + "x" * 300, "lights off " * 30, "🙂 on", "\x00"]:
        try:
            planner.plan(t, B.CATALOG, B.ALLOWED)
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = False
            print("   ", repr(t[:30]), e)
        check(f"odd text {t[:20]!r} doesn't break the planner", ok)


def rooms():
    """Said in a room without naming one: that room's devices, when that's clear; a named room or 'all' wins."""
    A, ha, big = app()
    A.home.areas = {"light.kitchen": "kitchen", "light.kitchen_counter": "kitchen", "light.bedroom": "bedroom",
                    "light.nightstand": "bedroom", "light.den_lamp": "den", "light.office_desk": "office",
                    "light.hall": "hall", "light.porch": "porch"}
    r = run(A, "lights off", area="Bedroom")
    got = sorted(c[2]["entity_id"] for c in ha.calls)
    check("'lights off' in the bedroom: the bedroom's lights", got == ["light.bedroom", "light.nightstand"] and
          r.get("room") == "bedroom", (got, r))
    ha.calls.clear()
    r = run(A, "turn off the light", area="den")
    check("'the light' in the den: the den lamp, no 'which one?'", [c[2]["entity_id"] for c in ha.calls] ==
          ["light.den_lamp"], (ha.calls, r))
    ha.calls.clear()
    run(A, "kitchen lights off", area="bedroom")
    check("a named room wins", sorted(c[2]["entity_id"] for c in ha.calls) == ["light.kitchen", "light.kitchen_counter"],
          ha.calls)
    ha.calls.clear()
    run(A, "turn off all the lights", area="den")
    check("'all the lights' is the whole house", len(ha.calls) == len(B.LIGHTS), len(ha.calls))
    ha.calls.clear()
    r = run(A, "turn off the light", area="garage")
    check("a room with no such device: as before (it asks which)", r.get("which") and not ha.calls, r)
    ha.calls.clear()
    A.home.areas = {}
    r = run(A, "lights off", area="bedroom")
    check("rooms unknown: as before", len(ha.calls) == len(B.LIGHTS) and "room" not in r)


single()
multi()
rooms()
fallback()
model_multi()
bench()
odd()
print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
