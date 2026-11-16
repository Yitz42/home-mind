"""Expert review regressions (round 1, security): what a scoped or person-bound token can reach over HTTP and MCP.
The HTTP routes run through the real middlewares with a small stand-in for aiohttp.web, so no server is needed.

  python tests/test_review.py
"""
import asyncio
import json
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
from homemind import main as M, tokens as T  # noqa: E402
from homemind.mcp import MCP  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.views import Views  # noqa: E402
from homemind.speakers import VIEWER  # noqa: E402

FAILS = []


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


def setup():
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    ids = {"bob": st.remember(text="my bank pin is 4455", title="Bob's pin",
                              meta={"scope": "personal", "owner": "Bob"})["id"],
           "task": st.remember(text="buy the surprise gift", title="Bob's gift", kind="task",
                               meta={"scope": "personal", "owner": "Bob"})["id"],
           "house": st.remember(text="the wifi password is on the fridge", title="wifi")["id"]}

    class Mind:
        store = st

        async def ask(self, q, model=None, speaker=None, **k):
            return {"hits": [], "answered_as": speaker if speaker is not None else VIEWER.get()}
    A = types.SimpleNamespace(on_start=None, on_stop=None, token="main-token", tokens=T.Tokens(st), mind=Mind(),
                              opts={}, home=types.SimpleNamespace(catalog={}), views=Views(st))
    A.speaker_for = M.App.speaker_for.__get__(A)
    A.has_pending = lambda cid: False
    A.scope_meta = M.App.scope_meta.__get__(A)
    A.people = lambda: []
    return st, ids, A, M.build(A)


def http_person_bound():
    st, ids, A, app = setup()
    alice = A.tokens.make("alice phone", "write", "Alice")["token"]
    alice_r = A.tokens.make("alice tablet", "read", "Alice")["token"]
    bob = ids["bob"]
    s, d = call(app, "GET", f"/api/item/{bob}", token=alice)
    check("person-bound token: /api/item of someone else's personal note shows nothing", s == 200 and d is None, d)
    s, d = call(app, "GET", f"/api/item/{bob}", token="main-token")
    check("main token still reads it", d and d["id"] == bob, d)
    s, d = call(app, "GET", f"/api/item/{ids['house']}", token=alice)
    check("person-bound token still reads household notes", d and d["id"] == ids["house"], d)
    s, d = call(app, "GET", "/api/tasks", token=alice)
    check("/api/tasks leaves out someone else's personal task", s == 200 and not any(t["id"] == ids["task"] for t in d), d)
    s, d = call(app, "GET", "/api/recent?filter=all", token=alice)
    check("/api/recent leaves out someone else's personal note", s == 200 and {x["id"] for x in d} == {ids["house"]}, d)
    s, d = call(app, "GET", "/api/browse?shelf=inbox", token=alice)
    check("/api/browse leaves out someone else's personal note", s == 200 and bob not in [x["id"] for x in d["items"]], d)
    s, d = call(app, "GET", "/api/inbox", token=alice)
    check("/api/inbox leaves out someone else's personal note", bob not in [x["id"] for x in d["items"]], d)
    s, d = call(app, "POST", "/api/ask", {"question": "what is my pin?", "speaker": "Bob"}, alice)
    check("/api/ask: a person-bound token can't answer as someone else by naming a speaker",
          d.get("answered_as") == "Alice", d)
    s, d = call(app, "POST", "/api/ask", {"question": "pin?", "user": "bob"}, alice_r)
    check("/api/ask: nor by naming a user", d.get("answered_as") == "Alice", d)
    s, d = call(app, "POST", f"/api/item/{bob}/forget", {}, alice)
    check("person-bound token can't archive someone else's personal note", s == 404 and st.get(bob)["status"] == "active",
          (s, d))
    s, d = call(app, "POST", f"/api/item/{bob}/revise", {"text": "my bank pin is 0000"}, alice)
    check("nor revise it", s == 404, (s, d))
    s, d = call(app, "POST", "/api/remember", {"text": "my locker is 12", "scope": "personal", "owner": "Bob"}, alice)
    check("a person-bound token files personal notes as its own person",
          st.get(d["id"])["meta"].get("owner") == "Alice", st.get(d["id"])["meta"])
    st.kv_set("tidy_pairs", [[bob, ids["house"]]])
    s, d = call(app, "GET", "/api/tidy", token=alice)
    check("/api/tidy leaves out pairs with someone else's personal note", d == {"pairs": []}, d)
    st.log("ask", None, question="what is my bank pin", answer="4455")
    s, d = call(app, "GET", "/api/events", token=alice)
    check("/api/events: a person-bound token doesn't see what others asked", "4455" not in json.dumps(d), d)
    s, d = call(app, "GET", "/api/events", token="main-token")
    check("the main token still sees the log", "4455" in json.dumps(d))
    A.views.save("everything", None, "inbox", None, None, None, "newest", 20, None)
    s, d = call(app, "GET", "/api/views/everything", token=alice)
    check("a saved view leaves out someone else's personal note", bob not in [x["id"] for x in d["items"]], d)


def http_scopes():
    st, ids, A, app = setup()
    w = A.tokens.make("assistant", "write")["token"]
    r = A.tokens.make("reader", "read")["token"]
    for path in ("/api/home/act", "/api/do"):
        s, d = call(app, "POST", path, {"entity_id": "lock.front_door", "action": "off", "text": "unlock"}, w)
        check(f"{path} (house control) needs admin, as mind_control/mind_do do over MCP", s == 403, (s, d))
    s, d = call(app, "GET", "/api/tidy?now=1", token=r)
    check("GET /api/tidy?now=1 (archives old logs) needs write", s == 403, (s, d))
    s, d = call(app, "GET", "/api/tidy", token=r)
    check("GET /api/tidy (just the list) stays read", s == 200, (s, d))
    s, d = call(app, "GET", "/api/status", token="café")
    check("a token with non-ASCII characters is refused (401), not a crash", s == 401, (s, d))


def mcp_scopes():
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    bob = st.remember(text="my bank pin is 4455", title="Bob's pin", meta={"scope": "personal", "owner": "Bob"})["id"]
    ran = []

    class Eng:
        autos, owned, log = {}, set(), []

        def summary(self):
            return []

        async def run_now(self, i, t=None):
            ran.append(i)
            return {"ran": i}

    class Upd:
        async def apply(self):
            ran.append("update")
            return {}

        async def check(self):
            return {}
    m = MCP(types.SimpleNamespace(mind=types.SimpleNamespace(store=st), engine=Eng(), updater=Upd()))

    def tool(name, args, scope):
        return asyncio.run(m.handle({"id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
                                    scope))["result"]
    r = tool("mind_automations", {"action": "run", "id": "automation.unlock"}, "read")
    check("MCP: a read token can't run an automation (HTTP /api/automations/run is admin)", r["isError"] and not ran, r)
    r = tool("mind_updates", {"apply": True}, "read")
    check("MCP: a read token can't install updates (HTTP /api/updates/apply is admin)", r["isError"] and not ran, r)
    check("MCP: listing automations stays read", not tool("mind_automations", {}, "read")["isError"])
    check("MCP: an admin token still runs one", not tool("mind_automations", {"action": "run", "id": "a.b"}, "admin")[
        "isError"] and ran == ["a.b"])
    t = VIEWER.set("Alice")
    try:
        r = tool("mind_get", {"id": bob}, "read")
        check("MCP mind_get: a person-bound token doesn't get someone else's personal note",
              "4455" not in r["content"][0]["text"], r)
        r = tool("mind_forget", {"id": bob}, "write")
        check("MCP mind_forget: nor archive it", r["isError"] and st.get(bob)["status"] == "active", r)
        r = tool("mind_forget_about", {"name": "Bob"}, "write")
        check("MCP mind_forget_about leaves someone else's personal notes alone", st.get(bob)["status"] == "active", r)
        r = tool("mind_remember", {"text": "my code is 1", "scope": "personal", "owner": "Bob"}, "write")
        nid = json.loads(r["content"][0]["text"])["id"]
        check("MCP mind_remember files personal notes as the token's person", st.get(nid)["meta"]["owner"] == "Alice")
    finally:
        VIEWER.reset(t)
    check("scopes: tool_needs looks at the action", T.tool_needs("mind_automations", {"action": "takeover"}) == "admin"
          and T.tool_needs("mind_automations", {"action": "list"}) == "read" and T.tool_needs("mind_recall") == "read")


http_person_bound()
http_scopes()
mcp_scopes()
print(f"\n{len(FAILS)} failed" if FAILS else "\nall ok")
sys.exit(1 if FAILS else 0)
