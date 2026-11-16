"""Expert review regressions (round 6, voice): the spoken path end to end. A request from a voice satellite arrives
through Home Assistant (an LLM tool script calling /api/ask or /api/do through the bridge, with conversation_id): what
the person hears for "stop", a misheard wake word, a lock or garage door that needs a yes, and a slow model.
Fake models and a fake Home Assistant; no network.

  python tests/test_review_voice.py
"""
import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import time
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
from homemind import main as M, tokens as T  # noqa: E402
from homemind.home import Home, DO_SYSTEM  # noqa: E402
from homemind.mind import Mind, spoken  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.views import Views  # noqa: E402

FAILS = []
logging.disable(logging.CRITICAL)


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class Req(dict):
    def __init__(self, method, path, body=None, token=None):
        super().__init__()
        from urllib.parse import urlsplit, parse_qsl
        u = urlsplit(path)
        self.method, self.path, self.query = method, u.path, dict(parse_qsl(u.query))
        self.headers = {"X-Home-Mind-Token": token} if token else {}
        self.remote, self._body, self.match_info = "10.0.0.9", body, {}
        self.can_read_body, self.content_type = body is not None, "application/json"

    async def json(self):
        return self._body


def call(app, method, path, body=None, token=None):
    """A request through the auth and error middlewares to its route: (status, json or None)."""
    req = Req(method, path, body, token)
    for meth, pat, f in app.routes:
        mm = re.match("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pat) + "$", req.path)
        if meth == method and mm:
            req.match_info = mm.groupdict()
            break
    else:
        raise KeyError(f"no route {method} {path}")
    auth, errors = app.middlewares

    async def go():
        async def inner(r):
            return await errors(r, f)
        try:
            return await auth(req, inner)
        except Exception as e:  # noqa: BLE001 - an HTTPException from a handler
            return types.SimpleNamespace(status=getattr(e, "status", 500), data=None)
    r = asyncio.run(go())
    return r.status, r.data



class FakeHA:
    """Records service calls; the control switch is on."""
    def __init__(self):
        self.calls = []

    async def state(self, eid):
        return {"state": "on"} if eid == "input_boolean.mind_control_enabled" else None

    async def call(self, dom, svc, data):
        self.calls.append((dom, svc, data))
        return 200


class Planner:
    """A model that turns a request into a fixed plan (or takes too long)."""
    type, cfg = "ollama", {}

    def __init__(self, plans, delay=0):
        self.plans, self.delay, self.asked = plans, delay, []

    async def chat(self, system, messages, **k):
        self.asked.append(messages[-1]["content"])
        await asyncio.sleep(self.delay)
        req = messages[-1]["content"].rsplit("Request: ", 1)[-1]
        plan = next((p for w, p in self.plans.items() if w in req.lower()), {"entity_id": None})
        return {"text": json.dumps(plan), "ms": 1}


class NoModels:
    models = {}

    def get(self, n):
        raise KeyError(n)


def app(delay=0):
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    ha = FakeHA()
    h = Home(st, ha, {"home_controls": ["cover.*", "lock.*", "light.*"]})
    h.catalog = {e: {"name": n, "domain": e.split(".")[0], "state": "closed", "attrs": a, "kind": "device"}
                 for e, n, a in (("cover.garage_door", "Garage Door", {"device_class": "garage"}),
                                 ("lock.front_door", "Front Door", {}), ("light.desk", "Desk Lamp", {}))}
    planner = Planner({"garage": {"entity_id": "cover.garage_door", "action": "open"},
                       "front door": {"entity_id": "lock.front_door", "action": "unlock"},
                       "desk": {"entity_id": "light.desk", "action": "on"}}, delay)

    class Mind_:
        store = st
        VOICE_SECONDS = 8

        async def ask(self, q, model=None, speaker=None, **k):
            return {"hits": [], "answer": "searched: " + q}
    A = types.SimpleNamespace(on_start=None, on_stop=None, token="main-token", tokens=T.Tokens(st), mind=Mind_(),
                              opts={}, home=h, views=Views(st), models=types.SimpleNamespace(
                                  models={"haiku": planner}, get=lambda n: planner))
    for n in ("YES", "NO", "PENDING_SECONDS"):
        setattr(A, n, getattr(M.App, n))
    for n in ("do", "pending_reply", "has_pending", "speaker_for", "local_plan", "_run_plan", "_run_steps", "_said"):
        setattr(A, n, getattr(M.App, n).__get__(A))
    A.people = lambda: []
    return A, ha, M.build(A)


def confirm_by_voice():
    A, ha, web_app = app()
    do = lambda text, cid="sat-1", path="/api/do": call(web_app, "POST", path, {"text": text, "conversation_id": cid},
                                                        "main-token")[1]
    r = do("open the garage")
    check("voice: 'open the garage' asks for a yes instead of failing", r.get("confirm_needed") and not ha.calls
          and "Garage Door" in r.get("answer", "") and "yes" in r["answer"].lower(), r)
    r = do("yes")
    check("voice: 'yes' in the same conversation opens it", r.get("confirmed") and
          ha.calls == [("cover", "open_cover", {"entity_id": "cover.garage_door"})], (r, ha.calls))
    r = do("yes")
    check("voice: a second 'yes' does nothing more", len(ha.calls) == 1 and not r.get("confirmed"), (r, ha.calls))

    ha.calls.clear()
    do("unlock the front door")
    r = do("no")
    check("voice: 'no' drops the pending unlock", r.get("cancelled") and not ha.calls and "won't" in r["answer"], r)
    check("voice: and a later 'yes' does nothing", not do("yes").get("confirmed") and not ha.calls)

    do("unlock the front door")
    r = do("turn on the desk lamp")
    check("voice: anything else drops the pending unlock and is done as asked",
          ha.calls == [("light", "turn_on", {"entity_id": "light.desk"})], ha.calls)
    do("yes")
    check("voice: so a yes after it unlocks nothing", all(c[0] != "lock" for c in ha.calls), ha.calls)

    ha.calls.clear()
    do("unlock the front door", cid="sat-2")
    r = do("yes", cid="sat-1")
    check("voice: a yes from another conversation doesn't unlock", not ha.calls and not r.get("confirmed"), r)
    r = do("yes, but wait", cid="sat-2")
    check("voice: 'yes, but wait' is not a yes", not ha.calls, (r, ha.calls))

    do("unlock the front door", cid="sat-3")
    A._pending["sat-3"]["t"] -= 31
    r = do("yes", cid="sat-3")
    check("voice: a yes after 30 seconds does nothing and says so", not ha.calls and r.get("expired")
          and "again" in r.get("answer", ""), r)

    # the yes often comes back as a question to /api/ask (the conversation agent sends what was said)
    do("open the garage", cid="sat-4")
    s, r = call(web_app, "POST", "/api/ask", {"question": "Yes please", "conversation_id": "sat-4"}, "main-token")
    check("voice: 'yes' sent to /api/ask in that conversation does the pending action",
          s == 200 and r.get("confirmed") and ha.calls[-1][1] == "open_cover", (s, r, ha.calls))
    s, r = call(web_app, "POST", "/api/ask", {"question": "where are the keys", "conversation_id": "sat-4"},
                "main-token")
    check("voice: /api/ask without anything pending answers as before", r.get("answer") == "searched: where are the keys",
          r)
    tok = A.tokens.make("kitchen", "read")["token"]
    do("unlock the front door", cid="sat-5")
    n = len(ha.calls)
    s, r = call(web_app, "POST", "/api/ask", {"question": "yes", "conversation_id": "sat-5"}, tok)
    check("voice: a read-only token's 'yes' can't unlock (house control is admin)", len(ha.calls) == n, (r, ha.calls))

    r = do("open the garage", cid=None)
    check("voice: no conversation_id: a clear spoken reason, nothing moved", "error" in r and len(ha.calls) == n and
          "yes" in r.get("answer", ""), r)


def slow_model():
    A, ha, web_app = app(delay=0.5)
    A.opts = {"rule_planner": False}      # a plain command doesn't wait for a model at all: this is the model path
    s, r = call(web_app, "POST", "/api/do", {"text": "turn on the desk lamp", "deadline": 0.1}, "main-token")
    time.sleep(0.6)
    check("voice: a plan that misses the deadline does nothing (no light switching on after the reply)",
          not ha.calls and "answer" in r, (r, ha.calls))
    s, r = call(web_app, "POST", "/api/do", {"text": "turn on the desk lamp", "deadline": 2}, "main-token")
    check("voice: within the deadline it's done", ha.calls and r.get("done"), r)


def do_prompt():
    for a in ("open", "close", "lock", "unlock"):
        check(f"/api/do planner may say {a} (locks and covers were unreachable by words)", f'"{a}"' in DO_SYSTEM)


def dismiss_and_mishear():
    m = Mind(tempfile.mkdtemp(), NoModels())
    for t in ["Mind the step by the back door.", "Stop the water at the valve under the sink.",
              "The wifi password is hunter22.", "Cancel the newspaper when we travel.", "The shed key is on the hook."]:
        m.store.remember(text=t)

    async def go():
        for q in ["never mind", "Stop.", "cancel", "no thanks", "nope", "OK", "thank you"]:
            r = await m.ask(q, None, voice=True, conversation_id="c1")
            check(f"voice: {q!r} reads no note", r.get("dismissed") and not r["hits"] and "hunter" not in r["answer"]
                  and "Mind the step" not in r["answer"], r["answer"])
        for q in ["yes", "what", "um", "the", "hey jarvis", "huh?"]:
            r = await m.ask(q, None, voice=True, conversation_id="c2")
            check(f"voice: a lone {q!r} is not answered with a note", not r["hits"] and "hunter" not in (r["answer"] or "")
                  and "catch" in (r["answer"] or ""), r["answer"])
        r = await m.ask("where is the shed key", None, voice=True)
        check("voice: real questions still answered", "hook" in (r["answer"] or ""), r["answer"])
        r = await m.ask("stop the water", None, voice=True)
        check("voice: 'stop the water' is still a question", "valve" in (r["answer"] or ""), r["answer"])
    asyncio.run(go())


def speech():
    check("spoken: emoji are not read out", spoken("Bins go out Tuesday 🗑️👍") == "Bins go out Tuesday",
          spoken("Bins go out Tuesday 🗑️👍"))


confirm_by_voice()
slow_model()
do_prompt()
dismiss_and_mishear()
speech()
print(f"\n{len(FAILS)} failed" if FAILS else "\nall ok")
sys.exit(1 if FAILS else 0)
