"""Expert review regressions (round 5, MCP): protocol conformance of /mcp and the tool list.

Every tool's inputSchema is valid JSON Schema of the kind MCP clients accept, every tool runs once with minimal valid
arguments through the real middlewares (aiohttp stand-in from test_review_sec.py, via test_review_api.py) and answers
with a well-formed result, and the JSON-RPC edges (handshake, ping, notifications, client responses, batches,
unknown methods, scopes) behave as the spec says.

  python tests/test_review_mcp.py
"""
import asyncio
import importlib.util
import json
import os
import re
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("review_api", os.path.join(_here, "test_review_api.py"))
API = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(API)           # sets up the temp data dir and the aiohttp stand-in

from homemind import mcp as MCPM  # noqa: E402
from homemind import suites  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({str(extra)[:300]})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


TYPES = {"string", "integer", "number", "boolean", "object", "array", "null"}
KEYWORDS = {"type", "properties", "required", "items", "enum", "description", "default", "minimum", "maximum",
            "additionalProperties"}


def schema_problems(s, where):
    out = []
    if not isinstance(s, dict):
        return [f"{where}: not an object"]
    for k in s:
        if k not in KEYWORDS:
            out.append(f"{where}: keyword {k} clients may not know")
    if s.get("type") not in TYPES:
        out.append(f"{where}: type {s.get('type')!r}")
    if "enum" in s and not (isinstance(s["enum"], list) and s["enum"] and
                            all(isinstance(e, str) for e in s["enum"]) and s.get("type") == "string"):
        out.append(f"{where}: enum")
    if s.get("type") == "array":
        out += schema_problems(s.get("items"), where + ".items")
    for k, v in (s.get("properties") or {}).items():
        out += schema_problems(v, f"{where}.{k}")
    return out


def tool_list():
    probs, names = [], set()
    for name, desc, schema in MCPM.TOOLS:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", name) or name in names:
            probs.append(f"{name}: bad or repeated name")
        names.add(name)
        if not isinstance(desc, str) or len(desc) < 20:
            probs.append(f"{name}: description too short")
        if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
            probs.append(f"{name}: inputSchema must be an object with properties")
        req = schema.get("required", [])
        if not isinstance(req, list) or not set(req) <= set(schema.get("properties", {})):
            probs.append(f"{name}: required names a property that isn't there: {req}")
        probs += schema_problems(schema, name)
        a = MCPM.annotations(name)
        if not all(isinstance(a[k], bool) for k in ("readOnlyHint", "destructiveHint", "idempotentHint",
                                                       "openWorldHint")):
            probs.append(f"{name}: annotations")
    check(f"every tool schema is valid ({len(MCPM.TOOLS)} tools)", not probs, probs)
    from homemind.tokens import MCP_READ
    wrong = [n for n in MCPM._READ_ONLY if n not in MCP_READ]
    check("tools marked read-only all need only the read scope", not wrong, wrong)
    check("tools that delete, act on the house or install are marked destructive",
          {"mind_purge", "mind_purge_about", "mind_forget_about", "mind_control", "mind_do", "mind_install_automation",
           "mind_updates"} <= MCPM._DESTRUCTIVE)
    check("server instructions don't name one household's person", "Yitz" not in MCPM.INSTRUCTIONS)


def minimal_args(A, nid):
    models = [m["name"] for m in A.models.list()] or ["none"]
    sample = {"text": "the blue bike is in the shed", "id": nid, "name": "zqxname", "confirm": False,
              "query": "bike", "q": "SELECT count(*) AS n FROM notes", "request": "turn the porch light on at dusk",
              "config": {"alias": "x", "trigger": [], "action": []}, "helpful": True, "question": "where is the bike?",
              "model": models[0], "suite": list(suites.SUITE_INFO)[0], "entity_id": "light.porch", "action": "on"}
    out = {}
    for name, _, schema in MCPM.TOOLS:
        out[name] = {k: sample[k] for k in schema["required"]}
    return out


def run_every_tool(app, A):
    st = A.mind.store
    nid = st.remember(text="the blue bike is in the shed", title="bike")["id"]
    crashes, shape = [], []
    for name, args in minimal_args(A, nid).items():
        s, d = API.call(app, "POST", "/mcp", {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                              "params": {"name": name, "arguments": args}})
        r = d.get("result") if isinstance(d, dict) else None
        if s != 200 or not r or not isinstance(r.get("content"), list) or not isinstance(r.get("isError"), bool) or \
                not all(c.get("type") == "text" and isinstance(c.get("text"), str) for c in r["content"]) or \
                d.get("id") != 7 or d.get("jsonrpc") != "2.0":
            shape.append((name, s, d))
            continue
        t = r["content"][0]["text"]
        if r["isError"] and re.match(r"(KeyError|TypeError|AttributeError|NameError|IndexError|UnboundLocalError)", t):
            crashes.append((name, t[:120]))
    check("every tool answers minimal valid arguments with a well-formed result", not shape, shape)
    check("no tool crashes on minimal valid arguments (KeyError/TypeError...)", not crashes, crashes)
    check("the bike note survived (minimal mind_purge without confirm deleted nothing)", st.get(nid) is not None)


def rpc(A, msg, scope="admin"):
    return asyncio.run(A.mcp.handle(msg, scope=scope))


def tool(A, name, args, scope="admin"):
    return rpc(A, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": args}}, scope)["result"]


def protocol(app, A):
    r = rpc(A, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}})["result"]
    check("initialize: a version the client asked for and we have is kept", r["protocolVersion"] == "2025-06-18", r)
    check("initialize: capabilities.tools and serverInfo", "tools" in r["capabilities"] and r["serverInfo"]["name"])
    r = rpc(A, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    check("initialize: an unknown version gets our latest", r["result"]["protocolVersion"] == MCPM.PROTOCOLS[0], r)
    check("ping answers {}", rpc(A, {"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {})
    r = rpc(A, {"jsonrpc": "2.0", "id": 3, "method": "no/such"})
    check("unknown method: -32601", r["error"]["code"] == -32601 and r["id"] == 3, r)
    s, _ = API.call(app, "POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("a notification: 202, no body", s == 202, s)
    s, d = API.call(app, "POST", "/mcp", {"jsonrpc": "2.0", "id": 9, "result": {}})
    check("the client's answer to a request (id + result, no method): 202, not a -32601 reply", s == 202, (s, d))
    s, d = API.call(app, "POST", "/mcp", {"jsonrpc": "2.0", "id": 9, "error": {"code": -1, "message": "no"}})
    check("the client's error answer: 202", s == 202, (s, d))
    s, d = API.call(app, "POST", "/mcp", [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                          {"jsonrpc": "2.0", "method": "notifications/x"},
                                          {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    check("a batch: one answer per request, none for the notification", s == 200 and [x["id"] for x in d] == [1, 2], d)
    s, _ = API.call(app, "POST", "/mcp", [{"jsonrpc": "2.0", "method": "notifications/x"}])
    check("a batch of notifications only: 202", s == 202, s)
    s, d = API.call(app, "POST", "/mcp", API.NOTJSON)
    check("not JSON: -32700", s == 400 and d["error"]["code"] == -32700, (s, d))
    s, _ = API.call(app, "GET", "/mcp")
    check("GET /mcp (no server stream): 405", s == 405, s)
    tl = rpc(A, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    check("tools/list carries annotations and titles",
          all(isinstance(t.get("annotations"), dict) and t.get("title") for t in tl), tl[:1])
    r = tool(A, "nope", {})
    check("unknown tool: isError naming it", r["isError"] and "no tool nope" in r["content"][0]["text"], r)
    r = tool(A, "nope", {}, "read")
    check("unknown tool with a read token: says no such tool (not 'not allowed')", "no tool" in r["content"][0]["text"],
          r)
    r = tool(A, "mind_remember", {})
    check("missing required argument: isError naming it", r["isError"] and "text" in r["content"][0]["text"], r)
    r = tool(A, "mind_purge", {"id": A.mind.store.remember(text="keep me")["id"], "confirm": False})
    check("a refusal ({error: ...}) is isError", r["isError"], r)


def scopes_and_safety(A):
    st = A.mind.store
    read = [t["name"] for t in rpc(A, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "read")["result"]["tools"]]
    check("a read token isn't offered write/admin tools", not {"mind_remember", "mind_purge", "mind_control"} & set(read),
          read)
    check("a read token is offered mind_view (listing and running views is a read over HTTP too)", "mind_view" in read)
    r = tool(A, "mind_view", {"action": "list"}, "read")
    check("mind_view list with a read token works", not r["isError"], r)
    r = tool(A, "mind_view", {"action": "save", "name": "bikes", "query": "bike"}, "read")
    check("mind_view save with a read token is refused", r["isError"] and "not allowed" in r["content"][0]["text"], r)
    r = tool(A, "mind_view", {"action": "save", "name": "bikes", "query": "bike"}, "write")
    check("mind_view save with a write token works", not r["isError"], r)
    r = tool(A, "mind_view", {"action": "run", "name": "bikes"}, "read")
    check("mind_view run with a read token works", not r["isError"], r)

    a = st.remember(text="Dana likes the window seat")["id"]
    b = st.remember(text="the router password is on the fridge")["id"]
    for args in ({"name": " "}, {"name": "  \t"}):
        r = tool(A, "mind_forget_about", args)
        check(f"mind_forget_about {args!r}: refused, nothing archived",
              r["isError"] and st.get(a)["status"] == "active" and st.get(b)["status"] == "active", r)
        r = tool(A, "mind_purge_about", dict(args, confirm=True))
        check(f"mind_purge_about {args!r} confirm=true: refused, nothing deleted",
              r["isError"] and st.get(a) is not None and st.get(b) is not None, r)


def big_output(A):
    st = A.mind.store
    for i in range(260):
        st.remember(text=f"bulk note {i} " + "lorem ipsum dolor sit amet " * 30, title=f"bulk {i}", shelf="bulk")
    r = tool(A, "mind_sql", {"q": "SELECT body FROM notes WHERE shelf = 'bulk'"})
    t = r["content"][0]["text"]
    check("a huge tool result is cut to a size clients accept, saying how to ask for less",
          len(t) < MCPM.MAX_TEXT + 500 and "cut:" in t, len(t))
    r = tool(A, "mind_recall", {"query": "bulk note", "limit": 3})
    check("a small result isn't cut", "cut:" not in r["content"][0]["text"])


def main():
    A = API.make_app()
    app = API.M.build(A)
    tool_list()
    run_every_tool(app, A)
    protocol(app, A)
    scopes_and_safety(A)
    big_output(A)


if __name__ == "__main__":
    main()
    print(f"\n{len(FAILS)} failed" if FAILS else "\nall ok")
    sys.exit(1 if FAILS else 0)
