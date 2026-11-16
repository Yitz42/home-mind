"""Expert review (round 6, first run and docs): the documentation keeps up with the code. Fails when a route, MCP tool
or option is added without being documented, when DOCS lists one that no longer exists, or when a documented scope is
not the one tokens.py enforces. Also checks the panel's first-run hints and the clearer error messages.

  python tests/test_review_docs_api.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
APP = os.path.join(ROOT, "home_mind")
sys.path.insert(0, os.path.join(APP, "rootfs", "app"))
from homemind import tokens as T  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


DOCS = open(os.path.join(APP, "DOCS.md"), encoding="utf-8").read()
MAIN = open(os.path.join(APP, "rootfs", "app", "homemind", "main.py"), encoding="utf-8").read()


def section(title):
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", DOCS, re.M | re.S)
    return m.group(1) if m else ""


def table_rows(text, first_col_code=False):
    rows = []
    for ln in text.splitlines():
        if ln.startswith("|") and not ln.startswith("|---"):
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            rows.append(cells)
    return rows


# ---------- routes ----------
routes = set(re.findall(r'@r\.(get|post|delete|put)\("([^"]+)"\)', MAIN))
routes = {(m.upper(), p) for m, p in routes}
api = section("API")
documented = {}
for cells in table_rows(api):
    if len(cells) == 4 and cells[0] in ("GET", "POST", "DELETE", "PUT"):
        documented[(cells[0], cells[1].strip("`"))] = cells[2]
check("there are routes to document (parser works)", len(routes) > 90, len(routes))
missing = sorted(routes - set(documented))
check("every route in main.py is in DOCS.md › API (method, path, scope, what)", not missing, missing)
stale = sorted(set(documented) - routes)
check("DOCS.md › API lists no route that doesn't exist", not stale, stale)
wrong = []
for (meth, path), scope in documented.items():
    sample = re.sub(r"\{\w+\}", "x1", path)
    if scope.split(";")[0].strip() != T.needed(meth, sample):
        wrong.append((meth, path, scope, T.needed(meth, sample)))
check("each documented scope is the one tokens.needed() enforces", not wrong, wrong)
check("purge's admin scope is documented", "purge: admin" in documented.get(("POST", "/api/item/{id}/{op}"), ""))
check("purge really needs admin", T.needed("POST", "/api/item/7/purge") == "admin")

# ---------- MCP tools ----------
from homemind.mcp import TOOLS  # noqa: E402
tools = {t[0] for t in TOOLS}
doc_tools = {}
for cells in table_rows(api):
    if len(cells) == 3 and cells[0].startswith("`mind_"):
        doc_tools[cells[0].strip("`")] = cells[1]
check("every MCP tool is in DOCS.md › API", not (tools - set(doc_tools)), sorted(tools - set(doc_tools)))
check("no documented MCP tool is gone", not (set(doc_tools) - tools), sorted(set(doc_tools) - tools))
bad = [(n, s, T.tool_needs(n)) for n, s in doc_tools.items() if s.split(";")[0].strip() != T.tool_needs(n)]
check("each documented MCP scope is the one tool_needs() enforces", not bad, bad)

# ---------- options ----------
def keys_under(text, top):
    """Two-space keys under a top-level key, with their four-space fields (no PyYAML on CI)."""
    out, cur, inside = {}, None, False
    for ln in text.splitlines():
        if re.match(r"^\S", ln):
            inside = ln.startswith(top + ":")
            continue
        if not inside:
            continue
        m = re.match(r"^  (\w+):", ln)
        if m:
            cur = out.setdefault(m.group(1), {})
            continue
        m = re.match(r"^    (\w+):\s*(.*)", ln)
        if m and cur is not None:
            field = m.group(1)
            cur[field] = m.group(2)
        elif ln.startswith("      ") and cur is not None and cur:
            cur[field] += " " + ln.strip()      # a folded (>-) value's next line
    return out


CFG = open(os.path.join(APP, "config.yaml"), encoding="utf-8").read()
EN = open(os.path.join(APP, "translations", "en.yaml"), encoding="utf-8").read()
schema, options, tr = keys_under(CFG, "schema"), keys_under(CFG, "options"), keys_under(EN, "configuration")
if True:
    check("parser found the options", len(schema) > 70 and "home_controls" in tr, len(schema))
    cfg = {"options": options, "schema": schema}
    check("options and schema name the same keys", set(cfg["options"]) == set(cfg["schema"]),
          set(cfg["options"]) ^ set(cfg["schema"]))
    no_name = [k for k in cfg["schema"] if not (tr.get(k) or {}).get("name")]
    no_desc = [k for k in cfg["schema"] if not (tr.get(k) or {}).get("description")]
    check("every option has a name in translations/en.yaml", not no_name, no_name)
    check("every option has a description in translations/en.yaml", not no_desc, no_desc)
    check("translations name no option that doesn't exist", not (set(tr) - set(cfg["schema"])), set(tr) - set(cfg["schema"]))
    opts = section("Options")
    undocumented = [k for k in cfg["schema"] if f"`{k}`" not in opts]
    check("every option is in DOCS.md › Options", not undocumented, undocumented)
    risks = opts.split("Before you turn these on")[-1] if "Before you turn these on" in opts else ""
    for k in ("home_controls", "unknown_caller", "auto_update_packs", "web_search", "telegram_token", "email_senders",
              "encrypt_secrets", "automation_pin"):
        check(f"risky option {k} is explained under 'Before you turn these on'", f"`{k}" in risks)
    check("home_controls' description says empty means every script and scene",
          "script" in tr["home_controls"]["description"] and "mind_control_enabled" in tr["home_controls"]["description"])

# ---------- clearer messages ----------
HOME = open(os.path.join(APP, "rootfs", "app", "homemind", "home.py"), encoding="utf-8").read()
check("controls-off error says how to turn controls on (create the Toggle helper)",
      "Toggle helper" in HOME and "controls are off: turn on" in HOME)
check("401 without a token says which header to send", "as header X-Home-Mind-Token" in MAIN)

# ---------- panel first run (node, fake DOM as in test_review_web.py) ----------
NODE = shutil.which("node")
PAGE = open(os.path.join(APP, "rootfs", "app", "homemind", "web", "index.html"), encoding="utf-8").read()
SCRIPT = PAGE.split("<script>")[1].split("</script>")[0]
HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const [script, scenario] = [fs.readFileSync(process.argv[2], 'utf8'), process.argv[3]];
const els = {};
function el(sel) { if (!els[sel]) { const e = {sel, innerHTML: '', textContent: '', value: '', checked: false, dataset: {},
  _c: new Set(), style: {}}; e.classList = {add: c => e._c.add(c), remove: c => e._c.delete(c), contains: c => e._c.has(c),
  toggle: (c, on) => (on === undefined ? !e._c.has(c) : on) ? e._c.add(c) : e._c.delete(c)};
  e.addEventListener = () => {}; els[sel] = e; } return els[sel]; }
const S = JSON.parse(scenario), routes = S.routes;
const ctx = {console, JSON, Promise, Date, Math, Object, Array, String, Number, RegExp, encodeURIComponent,
  setTimeout, setInterval: () => 0, alert: () => {}, prompt: () => null, confirm: () => true,
  location: {search: '', origin: 'http://x', pathname: '/'},
  document: {querySelector: el, querySelectorAll: () => [], getElementById: id => el('#' + id)},
  fetch: async (path) => { const k = Object.keys(routes).find(k => path.startsWith(k));
    const v = k === undefined ? {status: 200, body: []} : routes[k];
    const b = typeof v.body === 'string' ? v.body : JSON.stringify(v.body);
    return {ok: (v.status || 200) < 400, status: v.status || 200, text: async () => b}; }};
ctx.window = ctx; vm.createContext(ctx); vm.runInContext(script, ctx);
(async () => { let crash = null;
  for (const st of S.steps) { try { await vm.runInContext(st, ctx); } catch (e) { crash = String(e); } }
  const out = {crash}; for (const k of S.read) out[k] = {html: el(k).innerHTML || el(k).textContent, hide: el(k)._c.has('hide')};
  process.stdout.write(JSON.stringify(out)); })();
"""


def run(routes, steps, read):
    with tempfile.TemporaryDirectory() as d:
        sp, hp = os.path.join(d, "page.js"), os.path.join(d, "h.js")
        open(sp, "w").write(SCRIPT)
        open(hp, "w").write(HARNESS)
        r = subprocess.run([NODE, hp, sp, json.dumps({"routes": routes, "steps": steps, "read": read})],
                           capture_output=True, text=True, timeout=60)
        try:
            return json.loads(r.stdout)
        except ValueError:
            return {"crash": r.stdout + r.stderr}


if not NODE:
    print("ok   (node not installed: skipped the panel checks)")
else:
    fresh = {"version": "t", "items": 0, "versions": 0, "faded": 0, "shelves": 0, "events": 0, "runs": 0,
             "models": ["mock"], "bridge": None, "in_home_assistant": False}
    o = run({"api/status": {"body": fresh}, "api/runs": {"body": []}, "api/events": {"body": []}},
            ["load('more')"], ["#start", "#latest", "#events"])
    st = o.get("#start", {})
    check("fresh install: the System page shows a Getting started card", not o.get("crash") and not st.get("hide") and
          "Getting started" in st.get("html", ""), o)
    check("... saying no models are set up (mock doesn't count) and where to add one",
          "No models are set up" in st.get("html", "") and "Configuration" in st.get("html", ""), st)
    check("... that it isn't running inside Home Assistant", "Not running inside Home Assistant" in st.get("html", ""))
    check("... and how to file a first note", "Memory is empty" in st.get("html", ""))
    check("empty test results and activity say so", "no test runs yet" in o.get("#latest", {}).get("html", "") and
          "nothing yet" in o.get("#events", {}).get("html", ""), o)
    ready = dict(fresh, items=12, models=["haiku", "mock"], bridge="current", in_home_assistant=True)
    o = run({"api/status": {"body": ready}, "api/runs": {"body": []}, "api/events": {"body": []}},
            ["load('more')"], ["#start"])
    check("set up: no Getting started card", o.get("#start", {}).get("hide") is True, o)
    o = run({"api/status": {"status": 502, "body": "<html>bad gateway</html>"}, "api/runs": {"status": 502, "body": "x"},
             "api/events": {"status": 502, "body": "x"}}, ["load('more')"], ["#start"])
    check("app not answering: the System page says so instead of breaking", not o.get("crash") and
          "isn't answering" in o.get("#start", {}).get("html", ""), o)
    o = run({"api/status": {"body": ready}, "api/runs": {"status": 500, "body": {"error": "database is locked"}},
             "api/events": {"status": 500, "body": {"error": "database is locked"}}}, ["load('more')"],
            ["#latest", "#events"])
    check("runs/events errors are shown, not a TypeError", not o.get("crash") and
          "database is locked" in o.get("#latest", {}).get("html", "") and
          "database is locked" in o.get("#events", {}).get("html", ""), o)
    o = run({"api/ask": {"body": {"answer": "No model answered, and nothing in memory matched.", "model": None,
                                  "hits": [], "errors": ["haiku: ModelError ai_task.claude_ai_task: 400 not found"]}}},
            ["ask()"], ["#a_out"])
    out = o.get("#a_out", {}).get("html", "")
    check("Ask box shows why no model answered and where to fix it",
          "ai_task.claude_ai_task: 400" in out and "Configuration" in out, o)
    o = run({"api/models": {"body": [{"name": "mock", "type": "mock"}]}, "api/usage": {"body": {"models": [],
             "today": {}}}, "api/tokens": {"body": []}}, ["load('models')"], ["#mlist"])
    check("Models tab with only mock says how to add a model", "no models yet" in o.get("#mlist", {}).get("html", ""), o)

if FAILS:
    print(f"\n{len(FAILS)} failed")
    sys.exit(1)
print("\nall passed")
