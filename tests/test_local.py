"""End-to-end check without Home Assistant: starts the server on a spare port with a temporary data folder, a
mock model, and the repo's packs served over local HTTP, then exercises the REST API, MCP, updates and tests.

  python tests/test_local.py
"""
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "home_mind", "rootfs", "app")
FAILS = []


def port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main():
    data = tempfile.mkdtemp()
    pp, sp = port(), port()
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=os.path.join(ROOT, "packs"), **k)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", pp), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with open(os.path.join(data, "options.json"), "w") as f:
        json.dump({"models": [{"name": "mock", "type": "mock"}],
                   "update_url": f"http://127.0.0.1:{pp}/manifest.json", "auto_update_packs": False}, f)
    env = dict(os.environ, HOME_MIND_DATA=data, HOME_MIND_PORT=str(sp), PYTHONPATH=APP)
    proc = subprocess.Popen([sys.executable, "-m", "homemind"], env=env, cwd=APP,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{sp}"
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/status", timeout=1)
            except urllib.error.HTTPError:
                break
            except OSError:
                time.sleep(0.2)
        token = open(os.path.join(data, "token")).read().strip()

        def call(path, body=None, tok=token, method=None):
            req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                         method=method or ("POST" if body is not None else "GET"),
                                         headers={"Content-Type": "application/json", **({"X-Home-Mind-Token": tok} if tok else {})})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read()
                    return r.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as e:
                raw = e.read()
                return e.code, (json.loads(raw) if raw else None)

        # auth
        check("no token is refused", call("/api/status", tok=None)[0] == 401)
        check("wrong token is refused", call("/api/status", tok="nope")[0] == 401)
        code, st = call("/api/status")
        check("status with token", code == 200 and st["version"], st)
        check("bundled core pack installed", st["packs"].get("core", {}).get("version") == "0.1.0", st.get("packs"))

        # remember / update by key / recall / browse / history
        _, a = call("/api/remember", {"text": "The garage door code is 4417.", "shelf": "house/doors",
                                      "key": "garage door|code"})
        _, b = call("/api/remember", {"text": "The garage door code is 9020.", "shelf": "house/doors",
                                      "key": "garage door|code"})
        check("same key, new value supersedes", b["action"] == "updated" and b["previous"]["id"] == a["id"], b)
        _, c = call("/api/remember", {"text": "The garage door code is 9020.", "shelf": "house/doors",
                                      "key": "garage door|code"})
        check("same key, same value reinforces", c["action"] == "reinforced", c)
        _, hits = call("/api/recall?q=garage%20code")
        check("recall finds newest only", hits and hits[0]["id"] == b["id"] and all(h["id"] != a["id"] for h in hits),
              [h["id"] for h in hits])
        _, old = call("/api/recall?q=garage%20code&old=1")
        check("old versions on request", any(h["id"] == a["id"] for h in old))
        _, item = call(f"/api/item/{b['id']}")
        check("history kept", item["history"] and item["history"][0]["id"] == a["id"])
        _, br = call("/api/browse?shelf=house")
        check("browse shelf tree", br["shelves"] and br["shelves"][0]["name"] == "doors", br)
        call(f"/api/item/{b['id']}/forget", {})
        _, hits = call("/api/recall?q=garage%20code")
        check("forgotten item hidden", all(h["id"] != b["id"] for h in hits))
        call(f"/api/item/{b['id']}/restore", {})

        # unkeyed change: newest first, older marked
        call("/api/remember", {"text": "The Vellum shed padlock is set to 3381.", "shelf": "house"})
        call("/api/remember", {"text": "Garden hose is in the shed."})
        _, nw = call("/api/remember", {"text": "Change: Vellum shed padlock now opens with 5512.", "shelf": "house"})
        _, hits = call("/api/recall?q=Vellum%20shed%20padlock")
        check("unkeyed change: newest first", hits and hits[0]["id"] == nw["id"], [h["title"] for h in hits])
        check("older note marked with the newer id", any(h.get("newer") == nw["id"] for h in hits[1:]), hits)

        # ask with the mock model
        _, ans = call("/api/ask", {"question": "What is the garage door code?", "model": "mock"})
        check("ask (mock + brain)", "9020" in ans["answer"], ans)

        # MCP
        code, init = call("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "t", "version": "0"}}})
        check("mcp initialize", init["result"]["protocolVersion"] == "2025-06-18", init)
        code, _ = call("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        check("mcp notification -> 202", code == 202)
        _, tl = call("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in tl["result"]["tools"]]
        check("mcp tools listed", "mind_recall" in names and "mind_run_test" in names, names)
        _, r = call("/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "mind_recall", "arguments": {"query": "garage door"}}})
        check("mcp tools/call recall", not r["result"]["isError"] and "9020" in r["result"]["content"][0]["text"], r)
        _, r = call("/mcp", {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                             "params": {"name": "nope", "arguments": {}}})
        check("mcp unknown tool is a tool error", r["result"]["isError"])
        code, _ = call("/mcp", method="GET")
        check("mcp GET -> 405", code == 405)

        # updates
        _, u = call("/api/updates")
        check("update check reads manifest", u.get("packs") and u["packs"][0]["latest"] == "0.1.0", u)
        _, ua = call("/api/updates/apply", {})
        check("nothing to install when current", ua.get("installed") == [] and not ua.get("errors"), ua)

        # tests: every suite, brain arm; model arms with the mock
        runs = []
        for s in ("recall", "update", "locate", "tasks"):
            runs.append(call("/api/tests", {"suite": s, "arm": "brain", "seed": 11})[1]["id"])
        for arm in ("model+brain", "model+context", "model"):
            runs.append(call("/api/tests", {"suite": "update", "arm": arm, "model": "mock", "seed": 11})[1]["id"])
        runs.append(call("/api/tests", {"suite": "tasks", "arm": "model+brain", "model": "mock", "seed": 11})[1]["id"])
        runs.append(call("/api/tests", {"suite": "update", "arm": "brain", "seed": 11, "keyed": True})[1]["id"])
        done = {}
        for _ in range(100):
            for rid in runs:
                if rid not in done:
                    _, rr = call(f"/api/runs/{rid}")
                    if rr["status"] != "running":
                        done[rid] = rr
            if len(done) == len(runs):
                break
            time.sleep(0.2)
        for rid in runs:
            rr = done.get(rid)
            ok = rr and rr["status"] == "done" and rr["summary"]["n"] > 0
            label = f"test {rr and rr['suite']} {rr and rr['arm']}" + (" keyed" if rr and rr["config"].get("keyed") else "")
            check(label, ok, rr and rr["summary"])
            if ok:
                print("     ", {k: v for k, v in rr["summary"].items() if k in ("accuracy", "hit1", "stale_above", "stale_answers")})
        alone = [r for r in done.values() if r["arm"] == "model"]
        check("model alone knows nothing (sanity floor)", alone and alone[0]["summary"]["accuracy"] == 0.0)
        check("sandbox did not leak into the real mind", call("/api/status")[1]["items"] < 20)
        check("sandbox files cleaned up", not os.listdir(os.path.join(data, "sandbox")))
        _, tr = call("/api/trend?suite=update&name=accuracy")
        check("trend has points", len(tr) >= 4)
        code, page = None, None
        try:
            urllib.request.urlopen(urllib.request.Request(base + "/", headers={"X-Home-Mind-Token": token}))
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        check("web page served", code == 200)
    finally:
        proc.terminate()
        out = proc.communicate(timeout=10)[0].decode()
        srv.shutdown()
        if FAILS:
            print(out[-3000:])
    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
