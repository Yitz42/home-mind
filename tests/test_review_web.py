"""Review round 2 (web panel): static checks on web/index.html, plus a node run of its script with a tiny fake DOM
for the XSS / error / race cases. The node part is skipped when node is not installed."""
import html
import html.parser
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "home_mind/rootfs/app/homemind/web/index.html")
SRC = open(PAGE, encoding="utf-8").read()
SCRIPT = re.search(r"<script>(.*)</script>", SRC, re.S).group(1)
MARKUP = SRC[:SRC.index("<script>")]
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------- static ----------
defined = set(re.findall(r"\b(?:async\s+)?function\s+(\w+)", SCRIPT)) | set(re.findall(r"\b(?:const|let|var)\s+(\w+)\s*=", SCRIPT))
defined |= set(re.findall(r",\s*(\w+)\s*=\s*(?:s\s*=>|\{\}|\(|\w+\s*=>)", SCRIPT))
builtins = {"document", "this", "return", "alert", "window", "if", "confirm", "prompt", "event", "JSON"}
handlers = re.findall(r"\bon(?:click|change|input|submit|keydown)\s*=\s*\"([^\"]*)\"", SRC)
bad = set()
for h in handlers:
    for name in re.findall(r"(?<![.\w$])([A-Za-z_]\w*)\s*\(", html.unescape(h)):
        if name not in defined and name not in builtins:
            bad.add(name)
check("every on*= handler calls a defined function", not bad, sorted(bad))

ids = set(re.findall(r"\bid=\"([\w-]+)", SRC)) | {i + "x" for i in re.findall(r"\bid=\"([\w-]+)\$\{", SRC)}
used = set(re.findall(r"\$\('#([\w-]+)'\)", SRC)) | set(re.findall(r"getElementById\('([\w-]+)'\)", SRC))
missing = {u for u in used if u not in ids and not any(u.startswith(p) for p in re.findall(r"\bid=\"([\w-]+)\$\{", SRC))}
check("every $('#id') has an element", not missing, sorted(missing))

check("no single-quoted on*= attribute (JSON/text with ' breaks out)", not re.search(r"\bon\w+\s*=\s*'", SRC))
CONST_TERNARY = r"[^{}]*\?\s*'\w*'\s*:\s*'\w*'"    # '${gone ? 'restore' : 'forget'}' is fine
quoted = [m for m in re.findall(r"\bon\w+=\"[^\"]*?'\$\{([^}]*)\}", SRC) if not re.fullmatch(CONST_TERNARY, m)]
check("no '${...}' JS string built inside an on*= attribute (use jsq)", not quoted, quoted[:3])

UNTRUSTED = {"title", "body", "name", "shelf", "path", "caption", "file", "suggested", "owner", "said_by", "model", "error",
             "why", "source", "text", "habit", "alias", "location", "ingest", "arm", "suite", "type", "kind", "status",
             "scope", "protein_g", "calories", "data", "amount", "question", "right", "wrong", "unusual", "person",
             "thing", "verb", "due", "state", "next", "since", "raw", "answer", "focus", "id", "every", "ms", "score",
             "low", "high", "middle", "readings", "seed"}
NUMERIC_OK = {"d.id", "x.id", "r.id", "i.id", "n.id", "v.id", "c.id", "h.id", "d.newer", "d.superseded_by", "d.a.id",
              "d.b.id", "m.failures", "d.pages", "missing.length", "c.latest.seed"}
raw = []
for expr in [x for line in SCRIPT.split("\n") if "innerHTML" in line or "=> `" in line
             for x in re.findall(r"\$\{([^{}]*)\}", line)]:
    e = expr.strip()
    m = re.fullmatch(r"([\w.]+)(\s*(\?\?|\|\|)\s*'[^']*')?", e)
    if m and m.group(1).split(".")[-1] in UNTRUSTED and m.group(1) not in NUMERIC_OK and "." in m.group(1):
        raw.append(e)
check("no unescaped data field interpolated into markup", not raw, raw)

check("esc escapes the single quote too", "\"'\":'&#39;'" in SCRIPT)
check("no absolute URL (ingress needs relative paths)",
      not re.search(r"""(?:fetch|api|jfetch|window\.open)\(\s*['"`]/|\b(?:href|src|action)\s*=\s*["']/""", SRC))
check("no javascript: URL", "javascript:" not in SRC.lower())
for m in re.finditer(r"window\.top\.", SCRIPT):
    line = SCRIPT[SCRIPT.rfind("\n", 0, m.start()):SCRIPT.find("\n", m.start())]
    check("window.top read inside try (throws outside HA)", "try {" in line and line.index("try {") < line.index("window.top"))
check("no raw fetch().json() (throws on an ingress error page)", ".json()" not in SCRIPT.replace("r.json", ""))


class Ctl(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.bad = []
        self.label_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label":
            self.label_depth += 1
        if tag in ("input", "select", "textarea") and a.get("type") not in ("hidden",):
            if not (self.label_depth or a.get("aria-label") or a.get("placeholder") or a.get("title")):
                self.bad.append(a.get("id"))

    def handle_endtag(self, tag):
        if tag == "label":
            self.label_depth -= 1


p = Ctl()
p.feed(MARKUP)
check("every input/select/textarea has a label, aria-label or placeholder", not p.bad, p.bad)

# ---------- node run ----------
NODE = shutil.which("node")
HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const [script, scenario] = [fs.readFileSync(process.argv[2], 'utf8'), process.argv[3]];
const els = {}, calls = [], fetched = [];
function el(sel) { if (!els[sel]) { const e = {sel, innerHTML: '', textContent: '', value: '', checked: false, files: [], dataset: {},
  _c: new Set(), style: {}}; e.classList = {add: c => e._c.add(c), remove: c => e._c.delete(c), contains: c => e._c.has(c),
  toggle: (c, on) => (on === undefined ? !e._c.has(c) : on) ? e._c.add(c) : e._c.delete(c)};
  e.addEventListener = () => {}; e.scrollIntoView = () => {}; e.click = () => {}; els[sel] = e; } return els[sel]; }
let routes = {};
const ctx = {console, JSON, Promise, Date, Math, Object, Array, String, Number, RegExp, encodeURIComponent, decodeURIComponent,
  setTimeout, setInterval: () => 0, FormData: class { append() {} },
  alert: m => calls.push(['alert', m]), prompt: () => null, confirm: () => true,
  location: {search: '', origin: 'http://x', pathname: '/'},
  document: {querySelector: el, querySelectorAll: () => [], getElementById: id => el('#' + id)},
  fetch: async (path, opts) => { fetched.push(path); const k = Object.keys(routes).find(k => path.startsWith(k));
    const v = k === undefined ? {status: 200, body: '[]'} : routes[k]; if (v.wait) await new Promise(r => setTimeout(r, v.wait));
    const b = typeof v.body === 'string' ? v.body : JSON.stringify(v.body);
    return {ok: (v.status || 200) < 400, status: v.status || 200, text: async () => b, json: async () => JSON.parse(b)}; }};
ctx.window = ctx; Object.defineProperty(ctx, 'top', {get() { throw new Error('SecurityError: cross-origin'); }});
vm.createContext(ctx);
const S = JSON.parse(scenario);
ctx.location.search = S.search || '';
routes = S.routes || {};
vm.runInContext(script, ctx);
(async () => {
  await new Promise(r => setTimeout(r, 20));
  const out = {};
  for (const step of S.steps || []) { await vm.runInContext(step, ctx); }
  await new Promise(r => setTimeout(r, S.settle || 50));
  for (const k of S.read || []) out[k] = el(k).innerHTML || el(k).textContent;
  if (S.eval) {   // run handler code with stubbed functions; report what they were called with
    for (const n of S.stubs) ctx[n] = (...a) => calls.push([n, a]);
    for (const code of S.eval) { try { new vm.Script(code); vm.runInContext(code, ctx); } catch (e) { calls.push(['throw', String(e)]); } }
  }
  out.calls = calls; out.fetched = fetched;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { process.stdout.write(JSON.stringify({crash: String(e.stack)})); });
"""


def run(scenario):
    with tempfile.TemporaryDirectory() as d:
        sp, hp = os.path.join(d, "page.js"), os.path.join(d, "h.js")
        open(sp, "w").write(SCRIPT)
        open(hp, "w").write(HARNESS)
        r = subprocess.run([NODE, hp, sp, json.dumps(scenario)], capture_output=True, text=True, timeout=60)
        try:
            return json.loads(r.stdout)
        except ValueError:
            return {"crash": r.stdout + r.stderr}


class Attrs(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.handlers = [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for k, v in attrs:
            if k.startswith("on"):
                self.handlers.append((k, v))


def parse(markup):
    p = Attrs()
    p.feed(markup)
    return p


BASE = {"api/status": {"body": {"version": "t"}}, "api/runs": {"body": []}, "api/events": {"body": []}}
EVIL = ["');alert(1);('", "\\');alert(1)//", "\"><img src=x onerror=alert(1)>", "x'y\\z\u2028"]

if not NODE:
    print("ok   (node not installed: skipped the run checks)")
else:
    # shelves: browse('...') from shelf paths with quotes and backslashes
    o = run({"routes": dict(BASE, **{"api/browse": {"body": {"shelf": "a", "items": [],
             "shelves": [{"path": s, "name": s, "items": 1} for s in EVIL]}}}),
             "steps": ["browse('a/b')"], "read": ["#shelves"]})
    p = parse(o.get("#shelves", ""))
    check("shelf names make no tags of their own", set(p.tags) <= {"span"}, p.tags)
    o2 = run({"routes": BASE, "eval": [h for _, h in p.handlers], "stubs": ["browse"]})
    got = [c[1][0] for c in o2.get("calls", []) if c[0] == "browse"]
    check("shelf onclick passes the exact shelf path, nothing runs", got == ["a"] + EVIL and not any(c[0] in ("alert", "throw") for c in o2["calls"]), o2.get("calls"))

    # pantry: puse('name') with a backslash used to break out
    o = run({"routes": dict(BASE, **{"api/pantry": {"body": {"goals": {}, "items": [
             {"id": 1, "name": n, "amount": "", "location": "<b>fridge</b>", "days_left": 3} for n in EVIL]}}}),
             "steps": ["pantry()"], "read": ["#p_items"]})
    p = parse(o.get("#p_items", ""))
    check("pantry location and names make no tags", "img" not in p.tags and "b" not in p.tags, p.tags)
    o2 = run({"routes": BASE, "eval": [h for _, h in p.handlers], "stubs": ["puse", "premove"]})
    got = [c[1][0] for c in o2.get("calls", []) if c[0] == "puse"]
    check("pantry used/tossed pass the exact name, nothing runs", got == [n for n in EVIL for _ in (0, 1)] and
          not any(c[0] in ("alert", "throw") for c in o2["calls"]), o2.get("calls"))

    # meals: model output (missing items, protein) into markup and a shop([...]) handler
    o = run({"routes": dict(BASE, **{"api/pantry/suggest": {"body": {"meals": [{"name": "m", "protein_g": "<img src=x onerror=alert(2)>",
             "missing": ["'><img src=x onerror=alert(1)>", "a\\'b"]}]}}}), "steps": ["meals()"], "read": ["#m_out"]})
    p = parse(o.get("#m_out", ""))
    check("meal ideas from a model make no tags", "img" not in p.tags, p.tags)
    o2 = run({"routes": BASE, "eval": [h for _, h in p.handlers], "stubs": ["shop"]})
    got = [c[1][0] for c in o2.get("calls", []) if c[0] == "shop"]
    check("shop button passes the exact missing items", got == [["'><img src=x onerror=alert(1)>", "a\\'b"]], o2.get("calls"))

    # tokens, suggestions, runs, owner: ids/strings into handlers
    o = run({"routes": dict(BASE, **{"api/tokens": {"body": [{"id": "t'1", "name": "n", "scope": "<i>read</i>"}]},
             "api/runs": {"body": [{"id": "r'1", "ts": 0, "suite": "<i>s</i>", "arm": "brain", "status": "<i>done</i>",
                                    "config": {"ingest": "<img src=x onerror=alert(1)>"}, "summary": {}}]}}),
             "steps": ["tokens()", "runs()"], "read": ["#toks", "#runs"]})
    p = parse(o.get("#toks", "") + o.get("#runs", ""))
    check("token scope / run fields make no tags", not ({"i", "img"} & set(p.tags)), p.tags)
    o2 = run({"routes": BASE, "eval": [h for _, h in p.handlers], "stubs": ["revokeToken", "showRun"]})
    check("token/run handlers pass exact ids", [c[1][0] for c in o2.get("calls", []) if c[0] in ("revokeToken", "showRun")] == ["t'1", "r'1"], o2.get("calls"))

    o = run({"routes": dict(BASE, **{"api/item/5": {"body": {"id": 5, "title": "t", "body": "b", "shelf": "s", "kind": "<i>k</i>",
             "status": "active", "created": 0, "meta": {"scope": "personal", "owner": "O'Brien\\", "photos": [{"file": "a b#c.jpg", "caption": "c"}]}}}}),
             "steps": ["item(5)"], "read": ["#itemcard"]})
    p = parse(o.get("#itemcard", ""))
    check("item kind makes no tags", "i" not in p.tags, p.tags)
    check("photo file name is URL-encoded", 'api/photo/a%20b%23c.jpg' in o.get("#itemcard", ""))
    o2 = run({"routes": BASE, "eval": [h for k, h in p.handlers if "scopeItem" in h], "stubs": ["scopeItem"]})
    check("Make personal passes the owner's exact name", [c[1][2] for c in o2.get("calls", []) if c[0] == "scopeItem"] == ["O'Brien\\"], o2.get("calls"))

    # errors: an ingress error page (HTML) and a JSON error become {error}, not a thrown promise
    o = run({"routes": dict(BASE, **{"api/ask": {"status": 502, "body": "<html>Bad gateway</html>"}}), "steps": ["ask()"], "read": ["#a_out"]})
    check("non-JSON error page shows an error, not a stuck '…'", "502" in o.get("#a_out", ""), o)
    o = run({"routes": dict(BASE, **{"api/remember": {"status": 403, "body": {"error": "this token can read"}}}), "steps": ["remember()"], "read": ["#r_out"]})
    check("JSON error is shown", o.get("#r_out") == "this token can read", o)
    o = run({"routes": dict(BASE, **{"api/tokens/": {"status": 403, "body": {"error": "needs admin"}}, "api/tokens": {"body": []}}),
             "steps": ["revokeToken('x')"]})
    check("a failed revoke is reported", ["alert", "needs admin"] in o.get("calls", []), o)

    # race: a slow older recent() must not overwrite the newer one
    o = run({"routes": dict(BASE, **{"api/recent?filter=told": {"wait": 150, "body": [{"id": 1, "title": "OLD", "created": 0, "status": "active", "shelf": "s"}]},
             "api/recent?filter=all": {"body": [{"id": 2, "title": "NEW", "created": 0, "status": "active", "shelf": "s"}]}}),
             "steps": ["$('#rf').value = 'told'; recent(); $('#rf').value = 'all'; recent();"], "settle": 300, "read": ["#recent"]})
    check("stale recent() answer is dropped", "NEW" in o.get("#recent", "") and "OLD" not in o.get("#recent", ""), o.get("#recent"))
    o = run({"routes": dict(BASE, **{"api/item/1": {"wait": 150, "body": {"id": 1, "title": "OLD", "body": "", "created": 0, "status": "active", "meta": {}}},
             "api/item/2": {"body": {"id": 2, "title": "NEW", "body": "", "created": 0, "status": "active", "meta": {}}}}),
             "steps": ["item(1); item(2);"], "settle": 300, "read": ["#itemcard"]})
    check("stale item() answer is dropped", "NEW" in o.get("#itemcard", "") and "OLD" not in o.get("#itemcard", ""))

    # ?hm_thing= outside HA (window.top throws), escaped, and a malformed one doesn't crash
    o = run({"search": "?hm_thing=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E", "settle": 500,
             "routes": dict(BASE, **{"api/thing": {"body": {"name": "<img src=x onerror=alert(1)>", "known": [], "notes": []}}}), "read": ["#itemcard"]})
    check("hm_thing works outside HA and is escaped", "api/thing?name=%3Cimg" in "".join(o.get("fetched", [])) and "img" not in parse(o.get("#itemcard", "")).tags, o)
    o = run({"search": "?hm_thing=%E0%A4%A", "settle": 500, "routes": BASE})
    check("malformed hm_thing does not crash the page", "crash" not in o and not any("api/thing" in f for f in o.get("fetched", [])), o)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
