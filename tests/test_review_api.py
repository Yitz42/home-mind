"""Expert review regressions (round 3, API): every HTTP route and MCP tool answers bad input with a 4xx and an
{"error": ...} (or isError), never a 500 or a crash. Table-driven: each route is listed here by hand, so a new route
fails the "every route is listed" check until it is added.

Runs a real App (temp data dir, no Home Assistant) through the real middlewares with the aiohttp stand-in from
test_review_sec.py.

  python tests/test_review_api.py
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
os.environ["HOME_MIND_DATA"] = tempfile.mkdtemp()
os.environ["HOME_MIND_OPTIONS"] = os.path.join(os.environ["HOME_MIND_DATA"], "options.json")
for k in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
    os.environ.pop(k, None)

# the aiohttp stand-in, shared with the security review's tests
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_review_sec.py")).read()
_ns = {"__file__": os.path.abspath(__file__), "__name__": "stub"}
exec(_src.split("\n_fake_aiohttp()\n")[0], _ns)
_ns["_fake_aiohttp"]()

from homemind import main as M  # noqa: E402

logging.disable(logging.CRITICAL)
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


NOBODY, NOTJSON = object(), object()


class Req(dict):
    """Enough of aiohttp's Request: json() raises ValueError for an empty or non-JSON body (as aiohttp does), and
    multipart() fails on a non-multipart body (aiohttp asserts)."""

    def __init__(self, method, path, body=NOBODY, ctype="application/json"):
        super().__init__()
        from urllib.parse import urlsplit, parse_qsl
        u = urlsplit(path)
        self.method, self.path, self.query = method, u.path, dict(parse_qsl(u.query, keep_blank_values=True))
        self.headers = {"X-Home-Mind-Token": "main-token"}
        self.remote, self._body, self.match_info = "10.0.0.9", body, {}
        self.can_read_body, self.content_type = body is not NOBODY, ctype

    async def json(self):
        if self._body is NOBODY or self._body is NOTJSON:
            return json.loads("" if self._body is NOBODY else "{not json")
        return json.loads(json.dumps(self._body))

    async def multipart(self):
        assert self.content_type.startswith("multipart/"), "multipart/* content type expected"
        raise AssertionError("no multipart in this stand-in")


def make_app():
    A = M.App()
    A.token = "main-token"

    async def start():
        await A.on_start(None)
        for t in A.tasks:
            t.cancel()

    from homemind.models import ModelError

    async def no_llm(*a, **k):                  # nothing here reaches a model or the network
        raise ModelError("no model in this test")

    async def no_net(*a, **k):
        raise ConnectionError("no network in this test")

    class NoHA:                                 # Home Assistant unreachable: every request fails at once
        def __getattr__(self, n):
            raise ConnectionError("no Home Assistant in this test")
    asyncio.run(start())
    for m in A.models.list():
        A.models.get(m["name"]).chat = no_llm
    A.updater.check = A.updater.apply = no_net
    A.ha.session = NoHA()
    A._spawn = lambda name, f: True             # long jobs (tests, compare) aren't started
    return A


def call(app, method, path, body=NOBODY, ctype="application/json", timeout=20):
    req = Req(method, path, body, ctype)
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
            return await asyncio.wait_for(auth(req, inner), timeout)
        except asyncio.TimeoutError:
            return types.SimpleNamespace(status="hang", data=None)
        except Exception as e:  # noqa: BLE001 - an HTTPException from a handler, or a crash past the middleware
            return types.SimpleNamespace(status=getattr(e, "status", 500), data=f"{type(e).__name__}: {e}")
    r = asyncio.run(go())
    return r.status, r.data


# ---- the table: every route, with the body fields / query parameters it reads ----------------------------------------
# (method, path, fields): for POST/PUT/DELETE fields are JSON body keys, for GET query parameters. Paths with
# {placeholders} are fuzzed with bad path values too.
ROUTES = [
    ("GET", "/", []),
    ("GET", "/api/status", []),
    ("GET", "/api/recall", ["q", "limit", "shelf", "kind", "old", "touch", "raw"]),
    ("POST", "/api/remember", ["text", "title", "kind", "shelf", "tags", "key", "meta", "scope", "owner", "inferred",
                               "source", "at"]),
    ("GET", "/api/review", []),
    ("GET", "/api/browse", ["shelf"]),
    ("GET", "/api/item/{id}", []),
    ("GET", "/api/recent", ["hours", "limit", "filter"]),
    ("POST", "/api/item/{id}/{op}", ["title", "text", "shelf", "tags", "key", "meta", "value", "helpful", "note",
                                     "allow_inbox", "scope", "owner", "confirm"]),
    ("POST", "/api/purge_about", ["name", "confirm"]),
    ("GET", "/api/tasks", ["project", "status"]),
    ("GET", "/api/models", []),
    ("POST", "/api/models/check", []),
    ("POST", "/api/ask", ["question", "model", "use_brain", "expand", "voice", "deadline", "fallback",
                          "conversation_id", "speaker"]),
    ("POST", "/api/organize", ["text", "model"]),
    ("GET", "/api/suites", []),
    ("POST", "/api/tests", ["suite", "arm", "model", "seed", "n", "ingest", "keyed", "expand", "order", "notes_first"]),
    ("GET", "/api/runs", ["limit", "suite"]),
    ("GET", "/api/runs/{id}", []),
    ("GET", "/api/trend", ["suite", "name", "arm"]),
    ("GET", "/api/structure", []),
    ("GET", "/api/profile", []),
    ("GET", "/api/tidy", ["now"]),
    ("GET", "/api/cases", []),
    ("POST", "/api/cases", ["question", "right", "wrong"]),
    ("POST", "/api/cases/{id}/remove", []),
    ("GET", "/api/usage", ["days"]),
    ("GET", "/api/events", ["limit"]),
    ("GET", "/api/updates", []),
    ("POST", "/api/updates/apply", []),
    ("GET", "/api/home", ["q"]),
    ("POST", "/api/home/act", ["entity_id", "action", "value", "source", "confirm"]),
    ("POST", "/api/do", ["text", "model", "source"]),
    ("GET", "/api/automations", []),
    ("POST", "/api/automations/{op}", ["ids", "all", "id", "trigger"]),
    ("GET", "/api/pantry", ["status"]),
    ("POST", "/api/pantry/{op}", ["text", "name", "amount", "location", "use_by", "source", "id", "meal", "model", "n",
                                  "request", "items", "image_b64", "filename"]),
    ("GET", "/api/overnight", []),
    ("POST", "/api/overnight/run", []),
    ("GET", "/api/evening", []),
    ("POST", "/api/evening/send", []),
    ("GET", "/api/backup", []),
    ("POST", "/api/backup/now", []),
    ("GET", "/api/export", ["secrets", "home"]),
    ("POST", "/api/import", ["format", "items", "links"]),
    ("POST", "/api/markdown/sync", []),
    ("POST", "/api/sql", ["q", "sql"]),
    ("GET", "/api/photo/{name}", []),
    ("DELETE", "/api/item/{id}/photo/{name}", []),
    ("GET", "/api/reshelve", ["run"]),
    ("GET", "/api/connections", []),
    ("POST", "/api/email/check", []),
    ("GET", "/api/preferences", []),
    ("GET", "/api/control_model/dataset", ["part", "paraphrase"]),
    ("GET", "/api/thing", ["name"]),
    ("GET", "/api/label", ["text", "name", "base", "scale"]),
    ("GET", "/api/views", []),
    ("POST", "/api/views", ["name", "query", "shelf", "kind", "tags", "days", "sort", "limit", "sql"]),
    ("GET", "/api/views/{name}", []),
    ("DELETE", "/api/views/{name}", []),
    ("GET", "/api/compare", []),
    ("POST", "/api/compare/run", ["seed"]),
    ("GET", "/api/docs", ["q"]),
    ("POST", "/api/docs", ["name", "text"]),
    ("DELETE", "/api/docs/{did}", []),
    ("GET", "/api/inbox", []),
    ("GET", "/api/vault", []),
    ("GET", "/api/vault/key", []),
    ("GET", "/api/tokens", []),
    ("POST", "/api/tokens", ["name", "scope", "person"]),
    ("DELETE", "/api/tokens/{tid}", []),
    ("GET", "/api/people", []),
    ("GET", "/api/house", []),
    ("POST", "/api/house/learn", []),
    ("POST", "/api/house/suggestion/{sid}/{op}", ["enable"]),
    ("POST", "/api/automations/draft", ["request", "model"]),
    ("POST", "/api/automations/install", ["config", "enable", "pin"]),
    ("GET", "/api/morning", ["hours"]),
    ("POST", "/api/morning/send", []),
    ("GET", "/api/knowledge", []),
    ("GET", "/api/knowledge/food", ["upc", "q", "n"]),
    ("GET", "/api/knowledge/search", ["q", "passages", "n"]),
    ("POST", "/api/knowledge/{op}", []),
    ("GET", "/api/patterns", ["days"]),
    ("POST", "/api/patterns/{op}", ["days", "stop", "factor", "what", "action"]),
    ("POST", "/api/journal/summarize", ["day", "model"]),
    ("GET", "/api/journal", ["day"]),
    ("GET", "/api/calendar", ["q", "day"]),
    ("POST", "/api/calendar/sync", []),
    ("POST", "/api/body/backfill", []),
    ("GET", "/api/comfort", ["room"]),
    ("POST", "/api/comfort/vote", ["room", "feeling", "source"]),
    ("POST", "/api/comfort/cycle", ["room"]),
    ("GET", "/api/comfort/check", ["room"]),
    ("GET", "/api/control", []),
    ("POST", "/api/control/set", ["key", "value"]),
    ("POST", "/api/control/reset", ["key"]),
    ("POST", "/api/control/run/{action}", []),
    ("POST", "/mcp", ["jsonrpc", "id", "method", "params"]),
    ("GET", "/mcp", []),
    ("DELETE", "/mcp", []),
]
# routes that talk to the outside world (models, Home Assistant, mail, the update server, backups): only their
# input handling is fuzzed, with the outside calls failing fast
OPS = {"op": ["forget", "restore", "revise", "sensitive", "feedback", "confirm", "photo", "keep", "move", "scope",
              "purge", "nope"],
       "automations_op": ["takeover", "release", "run", "retire", "reload", "nope"],
       "pantry_op": ["add", "ate", "used", "tossed", "remove", "suggest", "shop", "goals", "today", "photo", "nope"],
       "patterns_op": ["refresh", "send", "experiment", "ask", "answer", "weather", "nope"],
       "knowledge_op": ["nope"], "suggestion_op": ["enable", "dismiss", "nope"], "action": ["nope", ""]}
BAD_VALUES = [None, "", 0, -1, 10 ** 30, 1.5, float("1e308"), "abc", "x" * 20000, [], [1, "a", None], {}, {"a": [1]},
              True]
BAD_QUERY = ["", "abc", "-1", "0", "1e999", "nan", "inf", "99999999999999999999", "x" * 5000, "\x00", "1.5"]
BAD_PATH = {"id": ["abc", "-1", "0", "99999999999999999999", "1.5", "%00"], "name": ["..", "abc", "1-x.jpg", "-"],
            "did": ["abc", "-1"], "tid": ["abc"], "sid": ["abc", "-1"]}
BAD_BODIES = [NOBODY, NOTJSON, [], [1, 2], "text", 42, None, True, {}]


def paths_for(path):
    """The route's path filled in: with good placeholder values and each op, then with bad ones."""
    fills = {"id": ["1"], "name": ["1-ab.jpg"], "did": ["1"], "tid": ["1"], "sid": ["1"]}
    if path == "/api/item/{id}/{op}":
        return [f"/api/item/1/{op}" for op in OPS["op"]] + [f"/api/item/{i}/forget" for i in BAD_PATH["id"]]
    for key, opk in (("/api/automations/{op}", "automations_op"), ("/api/pantry/{op}", "pantry_op"),
                     ("/api/patterns/{op}", "patterns_op"), ("/api/knowledge/{op}", "knowledge_op")):
        if path == key:
            return [key.replace("{op}", o) for o in OPS[opk]]
    if path == "/api/house/suggestion/{sid}/{op}":
        return [f"/api/house/suggestion/{s}/{o}" for s in ["1"] + BAD_PATH["sid"] for o in OPS["suggestion_op"]]
    if path == "/api/control/run/{action}":
        return ["/api/control/run/nope", "/api/control/run/x"]
    names = re.findall(r"\{(\w+)\}", path)
    if not names:
        return [path]
    out = []
    for n in names:
        for v in fills[n] + BAD_PATH.get(n, []):
            p = path
            for m in names:
                p = p.replace("{" + m + "}", v if m == n else fills[m][0])
            out.append(p)
    return out


UPSTREAM = []      # 500s because Home Assistant or the network is down here (not input handling): counted, not failed


def broken(s, d):
    if s == "hang":
        return True
    if s != 500:
        return False
    if "in this test" in str(d):
        UPSTREAM.append(d)
        return False
    return True


def fuzz_http(app):
    """Every route, every bad input: never a 500 or a hang. Returns the failures as (method, path, input, status)."""
    bad = []
    for meth, path, fields in ROUTES:
        for p in paths_for(path):
            if meth == "GET":
                tries = [p] + [f"{p}?{f}={v}" for f in fields for v in BAD_QUERY]
                for t in tries:
                    from urllib.parse import quote
                    url = t if "?" not in t else t.split("?")[0] + "?" + t.split("?")[1].split("=")[0] + "=" + \
                        quote(t.split("=", 1)[1])
                    s, d = call(app, meth, url)
                    if broken(s, d):
                        bad.append((meth, url[:120], None, s, str(d)[:160]))
                continue
            bodies = list(BAD_BODIES) + [{f: v} for f in fields for v in BAD_VALUES]
            if meth == "DELETE":
                bodies = [NOBODY]
            for b in bodies:
                s, d = call(app, meth, p, b)
                if broken(s, d):
                    bad.append((meth, p, repr(b)[:60] if b not in (NOBODY, NOTJSON) else
                                ("no body" if b is NOBODY else "not json"), s, str(d)[:160]))
            if meth == "POST" and p in ("/api/item/1/photo", "/api/pantry/photo", "/api/import", "/api/docs"):
                s, d = call(app, meth, p, "x", ctype="text/plain")
                if broken(s, d):
                    bad.append((meth, p, "text/plain body", s, str(d)[:160]))
    return bad


MCP_BAD = [None, "", 0, -1, 10 ** 30, 1.5, "abc", "x" * 20000, [], [1, None], {}, True]


def fuzz_mcp(app, web_app):
    """Every MCP tool with each argument set to each bad value, and malformed JSON-RPC messages: tools answer with
    isError (or a result), the server with a JSON-RPC error, never a crash."""
    from homemind.mcp import TOOLS
    bad = []

    def rpc(msg):
        return asyncio.run(asyncio.wait_for(app.mcp.handle(msg, scope="admin"), 20))
    for name, _, schema in TOOLS:
        argsets = [None, [], "x", 5, {}] + [{k: v} for k in schema["properties"] for v in MCP_BAD]
        for a in argsets:
            try:
                out = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": a}})
                if "result" not in out or "isError" not in out["result"]:
                    bad.append((name, repr(a)[:60], out))
            except Exception as e:  # noqa: BLE001
                bad.append((name, repr(a)[:60], f"{type(e).__name__}: {e}"))
    for msg in ["x", 5, None, [1, "a"], {"id": 1, "method": 5}, {"id": 1, "method": "tools/call", "params": []},
                {"id": 1, "method": "tools/call", "params": {"name": 5}}, {"id": 1, "method": "initialize", "params": "x"},
                {"id": 1, "method": "tools/call", "params": {"name": "mind_recall", "arguments": [1]}}, []]:
        s, d = call(web_app, "POST", "/mcp", msg)
        if s == 500 or s == "hang" or not (s == 202 or isinstance(d, (dict, list)) and d):
            bad.append(("POST /mcp", repr(msg)[:60], (s, d)))
    return bad


def contracts(app, A):
    """Specific answers: the right status and an {"error"} message."""
    st = A.mind.store
    nid = st.remember(text="the spare key is under the mat", title="spare key")["id"]
    s, d = call(app, "POST", "/api/remember", [1, 2])
    check("a JSON list instead of an object: 400 with an error", s == 400 and "error" in d, (s, d))
    s, d = call(app, "POST", "/api/remember", NOTJSON)
    check("a body that isn't JSON: 400", s == 400 and "error" in d, (s, d))
    s, d = call(app, "POST", "/api/ask", {})
    check("/api/ask without a question: 400 naming it", s == 400 and "question" in d["error"], (s, d))
    s, d = call(app, "POST", "/api/ask", {"question": ["a"]})
    check("/api/ask with a question that isn't text: 400", s == 400, (s, d))
    s, d = call(app, "GET", "/api/recall?q=key&limit=abc")
    check("/api/recall?limit=abc: 400, not 500", s == 400 and "limit" in d["error"], (s, d))
    s, d = call(app, "GET", "/api/recall?q=spare+key&limit=-5")
    check("/api/recall?limit=-5 is clamped (answers, no crash)", s == 200 and isinstance(d, list), (s, d))
    s, d = call(app, "GET", "/api/events?limit=-1")
    check("/api/events?limit=-1 doesn't mean 'every row'", s == 200 and len(d) <= 1000, (s, len(d or [])))
    s, d = call(app, "GET", "/api/events?limit=99999999999999999999999")
    check("/api/events with a huge limit: clamped, not an SQLite overflow", s == 200, (s, d))
    s, d = call(app, "GET", "/api/recent?hours=nan")
    check("/api/recent?hours=nan: 400", s == 400, (s, d))
    s, d = call(app, "GET", "/api/item/abc")
    check("/api/item/abc: 400 or 404, not 500", s in (400, 404), (s, d))
    s, d = call(app, "POST", "/api/item/99999/forget", {})
    check("forgetting a missing item: 404", s == 404, (s, d))
    s, d = call(app, "POST", f"/api/item/{nid}/revise", {"text": 5})
    check("revise with text that isn't a string: 400", s == 400, (s, d))
    s, d = call(app, "POST", f"/api/item/{nid}/photo", {"x": 1})
    check("item photo with a JSON body: 400 asking for multipart", s == 400 and "multipart" in d["error"], (s, d))
    s, d = call(app, "POST", "/api/tokens", {"name": "x", "scope": "root"})
    check("/api/tokens with an unknown scope: 400", s == 400, (s, d))
    s, d = call(app, "POST", "/api/journal/summarize", {"day": "not a day"})
    check("/api/journal/summarize with a bad day: 400", s == 400, (s, d))
    s, d = call(app, "GET", "/api/journal?day=2026-13-45")
    check("/api/journal with a bad day: 400", s == 400, (s, d))
    s, d = call(app, "POST", "/mcp", "just a string")
    check("/mcp with a JSON string: a JSON-RPC error, not a 500", s == 400 and d.get("error"), (s, d))
    s, d = call(app, "POST", "/mcp", [1])
    check("/mcp batch with a non-object entry: an error entry, not a 500", s in (200, 400) and d, (s, d))
    s, d = call(app, "POST", f"/api/item/{nid}.5/forget", {})
    check("an id like 7.5 is a 400, never rounded to item 7", s == 400 and st.get(nid)["status"] == "active", (s, d))
    s, d = call(app, "POST", f"/api/item/{nid}/revise", {})
    check("revise with nothing to change: 400, not an empty new version", s == 400, (s, d))
    s, d = call(app, "GET", "/api/trend")
    check("a missing field reads 'missing suite', not \"'suite'\"", s == 400 and d["error"] == "missing suite", (s, d))
    s, d = call(app, "POST", "/api/organize", {"text": "the key is under the mat", "model": "haiku"})
    check("a model that fails is a 502 (upstream), not a 500", s == 502 and "error" in d, (s, d))
    s, d = call(app, "POST", "/api/pantry/photo", {"image_b64": "@@not base64@@"})
    check("pantry photo with bad base64: 400", s == 400, (s, d))
    s, d = call(app, "POST", "/api/remember", {"text": "garage code is in the drawer", "tags": "garage, codes"})
    check("tags as 'a, b' text still work", s == 200 and "garage" in st.get(d["id"])["tags"], (s, d))
    s, d = call(app, "POST", "/api/pantry/add", {"name": "eggs", "amount": 12})
    check("a pantry amount given as a number still works (Home Assistant scripts send them)", s == 200, (s, d))
    out = asyncio.run(A.mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "mind_forget", "arguments": {"id": 987654}}}))
    check("MCP mind_forget of a missing item: isError, not 'archived'", out["result"]["isError"], out)
    out = asyncio.run(A.mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "mind_recall", "arguments": [1]}}))
    check("MCP arguments that aren't an object: isError", out["result"]["isError"], out)
    out = asyncio.run(A.mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "mind_recall", "arguments": {"query": "key", "limit": -3}}}))
    check("MCP mind_recall with limit -3: answers (clamped)", not out["result"]["isError"], out)
    out = asyncio.run(A.mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "mind_get", "arguments": {}}}))
    check("MCP mind_get without id: isError naming the argument", out["result"]["isError"] and
          "id" in out["result"]["content"][0]["text"], out)


def docs_match(app):
    """Every route DOCS.md names (`/api/...`, with {x} or <x> placeholders) is one the server has."""
    docs = open(os.path.join(ROOT, "home_mind", "DOCS.md")).read()
    served = [(m, re.compile("^" + re.sub(r"\\\{\w+\\\}", "[^/]+", re.escape(p)) + "$")) for m, p, _ in app.routes]
    named = set(re.findall(r"(?:\b(GET|POST|DELETE|PUT)(?:/(?:GET|POST))? )?`?(/(?:api/[\w/{}<>.\-]*|mcp\b))", docs))
    ghost = []
    for meth, path in named:
        path = re.sub(r"[{<]\w+[}>]", "x", path.rstrip(".,/"))
        if not any(rx.match(path) and (not meth or m == meth) for m, rx in served):
            ghost.append((meth, path))
    check(f"DOCS.md names no route the server doesn't have ({len(named)} named)", named and not ghost, ghost)


def main():
    app_state = make_app()
    app = M.build(app_state)
    listed = {(m, p) for m, p, _ in ROUTES}
    served = {(m, p) for m, p, _ in app.routes}
    check("every route the app serves is in this table (add new ones to ROUTES)", served <= listed, served - listed)
    check("every route in this table is served", listed <= served, listed - served)
    bad = fuzz_http(app)
    for b in bad[:60]:
        print("   500:", b)
    if os.environ.get("API_FUZZ_DUMP"):
        json.dump([list(map(str, b)) for b in bad], open(os.environ["API_FUZZ_DUMP"], "w"), indent=0)
    check(f"HTTP fuzz over {len(ROUTES)} routes: no 500s or hangs", not bad, f"{len(bad)} bad")
    print(f"     ({len(UPSTREAM)} answers were 500 only because Home Assistant/the network is down in this test)")
    badm = fuzz_mcp(app_state, app)
    for b in badm[:30]:
        print("   MCP:", b)
    check("MCP fuzz: every tool answers bad arguments with isError, never a crash", not badm, f"{len(badm)} bad")
    contracts(app, app_state)
    docs_match(app)
    return bad, badm


if __name__ == "__main__":
    main()
    print(f"\n{len(FAILS)} failed" if FAILS else "\nall ok")
    sys.exit(1 if FAILS else 0)
