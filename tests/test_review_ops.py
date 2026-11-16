"""Review round 6 (ops): add-on packaging - config/schema/translations, Dockerfile vs CI, shutdown timing, backups,
packs and the self-updater.

  python tests/test_review_ops.py
"""
import asyncio
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import types

import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
ADDON = os.path.join(ROOT, "home_mind")
APP = os.path.join(ADDON, "rootfs", "app")
sys.path.insert(0, APP)
try:
    import aiohttp  # noqa: F401
except ImportError:
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: None
    m.ClientSession = object
    m.BasicAuth = lambda *a: a
    sys.modules["aiohttp"] = m
from homemind import updater  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def read(*p):
    with open(os.path.join(*p), encoding="utf-8") as f:
        return f.read()


CFG = yaml.safe_load(read(ADDON, "config.yaml"))
TR = yaml.safe_load(read(ADDON, "translations", "en.yaml"))
MAIN = read(APP, "homemind", "main.py")


def config_schema():
    o, s = set(CFG["options"]), set(CFG["schema"])
    check("every option has a schema entry and vice versa", o == s, (o ^ s))
    tr = set(TR.get("configuration") or {})
    check("every schema option has a translation (and no stale ones)", tr == s, (tr ^ s))
    ports = CFG.get("ports") or {}
    check("ports are NNNN/tcp|udp", all(re.fullmatch(r"\d{1,5}/(tcp|udp)", p) for p in ports), ports)
    check("the direct API port isn't exposed on the host by default", all(v is None for v in ports.values()), ports)
    check("every port has a description in translations", set(ports) <= set(TR.get("network") or {}))
    check("ingress port is the app port", CFG.get("ingress_port") == int(re.search(
        r'HOME_MIND_PORT", "(\d+)"', MAIN).group(1)))
    check("arch list is valid", set(CFG["arch"]) <= {"amd64", "aarch64", "armv7", "armhf", "i386"} and CFG["arch"])
    v = re.search(r'VERSION = "([^"]+)"', read(APP, "homemind", "version.py")).group(1)
    check("config.yaml version matches homemind/version.py", str(CFG["version"]) == v, (CFG["version"], v))
    bad = []
    simple = re.compile(r"^(str|bool|email|url|password|port|int(\(-?\d*,-?\d*\))?|float(\([\d.-]*,[\d.-]*\))?|"
                        r"list\([^)]+\)|match\(.+\))\??$")

    def walk(k, t):
        if isinstance(t, list):
            for x in t:
                walk(k, x)
        elif isinstance(t, dict):
            for kk, tt in t.items():
                walk(f"{k}.{kk}", tt)
        elif not simple.match(str(t)):
            bad.append((k, t))
    for k, t in CFG["schema"].items():
        walk(k, t)
    check("schema types are valid Home Assistant types", not bad, bad)


def dockerfile_vs_ci():
    df = read(ADDON, "Dockerfile")
    ci = read(ROOT, ".github", "workflows", "tests.yml")

    def specs(line):
        return {x for x in shlex.split(line) if re.match(r"^[A-Za-z]", x) and x not in ("pip", "install", "RUN", "run")
                and not x.startswith("-")}
    d = specs(re.search(r"pip install (.+)", df).group(1))
    c = specs(re.search(r"pip install (.+)", ci).group(1)) - {"jinja2", "pyyaml"}     # test-only extras
    check("CI installs exactly the Dockerfile's package versions", d == c, d ^ c)
    ci_py = re.search(r'python-version: "([\d.]+)"', ci).group(1)
    img_py = re.search(r"FROM python:([\d.]+)", df).group(1)
    check("CI Python matches the image's Python", ci_py == img_py, (ci_py, img_py))


def shutdown_fits_timeout():
    g = re.search(r"REQUESTS_GRACE, TASKS_GRACE = (\d+), (\d+)", MAIN)
    stop = int(re.search(r"STOP_GRACE = (\d+)", MAIN).group(1))
    check("main.STOP_GRACE is config.yaml timeout", CFG.get("timeout") == stop, (CFG.get("timeout"), stop))
    check("request + task grace fit inside the add-on's stop timeout (on_stop runs before SIGKILL)",
          g and int(g.group(1)) + int(g.group(2)) < (CFG.get("timeout") or 10))
    check("run_app is given shutdown_timeout (aiohttp default 60 s > HA's kill)",
          re.search(r"run_app\([^\n]*shutdown_timeout=REQUESTS_GRACE", MAIN) is not None)
    check("on_stop waits TASKS_GRACE", "asyncio.wait(live, timeout=TASKS_GRACE)" in MAIN)
    check("no init: python is PID 1 and aiohttp handles SIGTERM", CFG.get("init") is False and
          re.search(r'CMD \["python", "-m", "homemind"\]', read(ADDON, "Dockerfile")))


def backups():
    ex = CFG.get("backup_exclude") or []
    check("knowledge downloads and the test sandbox aren't backed up", "knowledge/*" in ex and "sandbox/*" in ex)
    check("vault.key kept out of backups (by design: a stolen backup can't open sealed notes)", "vault.key" in ex)
    pre = CFG.get("backup_pre", "")
    m = re.match(r"python3 (/app/\S+)$", pre)
    script = m and os.path.join(ADDON, "rootfs", m.group(1).lstrip("/"))
    check("backup_pre script is in the image", script and os.path.exists(script), pre)
    if not (script and os.path.exists(script)):
        return
    d = tempfile.mkdtemp()
    st = Store(os.path.join(d, "mind.db"))
    for i in range(30):
        st.remember(text=f"The shed code number {i} is {1000 + i}.", kind="fact")
    wal = os.path.join(d, "mind.db-wal")
    before = os.path.getsize(wal) if os.path.exists(wal) else 0
    p = subprocess.run([sys.executable, script, d], capture_output=True, text=True)
    after = os.path.getsize(wal) if os.path.exists(wal) else 0
    check("backup_pre checkpoints the live database's WAL into mind.db", p.returncode == 0 and before > 0 and
          after == 0 and "checkpointed" in p.stdout, (before, after, p.stdout, p.stderr))
    check("... the notes are in mind.db alone afterwards", sqlite3.connect(
        f"file:{os.path.join(d, 'mind.db')}?mode=ro&immutable=1", uri=True).execute(
        "SELECT count(*) FROM items").fetchone()[0] >= 30)
    r = st.remember(text="The gate code is 4242.", kind="fact")
    check("... and the app keeps writing after it", st.get(r["id"]) is not None)
    p = subprocess.run([sys.executable, script, tempfile.mkdtemp()], capture_output=True, text=True)
    check("backup_pre exits 0 with no database (never aborts HA's backup)", p.returncode == 0, p.stderr)
    junk = tempfile.mkdtemp()
    open(os.path.join(junk, "mind.db"), "w").write("not a database")
    p = subprocess.run([sys.executable, script, junk], capture_output=True, text=True)
    check("backup_pre exits 0 on a broken database", p.returncode == 0 and "skipped" in p.stdout, p.stdout + p.stderr)


def repo_hygiene():
    files = subprocess.run(["git", "ls-files", "-s"], cwd=ROOT, capture_output=True, text=True).stdout.splitlines()
    data_like = [ln for ln in files if re.search(r"(\.db|\.db-wal|vault\.key|/token|options\.json)$", ln)]
    check("no databases, tokens or keys tracked in git", not data_like, data_like)
    noexec = []
    for ln in files:
        mode, _h, _s, path = ln.split(None, 3)
        if path.startswith("home_mind/rootfs/") and not path.endswith(".py"):
            try:
                with open(os.path.join(ROOT, path), "rb") as f:
                    if f.read(2) == b"#!" and mode != "100755":
                        noexec.append(path)
            except OSError:
                pass
    check("every rootfs script with a shebang is executable in git", not noexec, noexec)


def packs():
    man = json.loads(read(ROOT, "packs", "manifest.json"))
    for p in man["packs"]:
        raw = open(os.path.join(ROOT, "packs", p["url"]), "rb").read()
        check(f"manifest sha256 matches packs/{p['url']}", hashlib.sha256(raw).hexdigest() == p["sha256"])
        check(f"manifest version matches packs/{p['url']}", json.loads(raw)["version"] == p["version"])
        bundled = os.path.join(APP, "packs", p["url"])
        check(f"bundled {p['url']} is the published one", os.path.exists(bundled) and open(bundled, "rb").read() == raw)
        try:
            updater.Updater._valid(json.loads(raw))
            ok = True
        except ValueError:
            ok = False
        check(f"{p['url']} passes the pack check (installed at first start)", ok)
    v = re.search(r'VERSION = "([^"]+)"', read(APP, "homemind", "version.py")).group(1)
    check("manifest app_latest isn't behind the app", updater._vt(man.get("app_latest")) <= updater._vt(v))


def fake(files):
    class R:
        def __init__(self, data):
            self.status, self.data = (200, data) if data is not None else (404, b"")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read(self):
            return self.data

    class S:
        def get(self, url, timeout=None):
            return R(files.get(url.rsplit("/", 1)[1]))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False
    return S


def updater_atomic():
    saved = updater.KEYS_FILE
    updater.KEYS_FILE = os.path.join(tempfile.mkdtemp(), "none.txt")
    try:
        st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
        u = updater.Updater(st, "https://x/manifest.json")
        u.install_pack({"id": "p", "version": "1", "procedures": [
            {"id": "a", "title": "Reset the router", "body": "Unplug it for 30 seconds."}]}, "test")
        pack = json.dumps({"id": "p", "version": "2", "procedures": [
            {"id": "a", "title": "Reset the router", "body": "Hold the reset button for 10 seconds."},
            {"id": "b", "title": "Broken"}]}).encode()
        man = json.dumps({"packs": [{"id": "p", "version": "2", "url": "p.json",
                                     "sha256": hashlib.sha256(pack).hexdigest()}]}).encode()
        u.session_factory = fake({"manifest.json": man, "p.json": pack})
        out = asyncio.run(u.apply())
        body = st.db.execute("SELECT body FROM items WHERE key='procedure:p:a' AND status='active'").fetchone()
        check("a pack with a bad procedure installs nothing (no half-updated pack)",
              not out["installed"] and out["errors"] and u.installed()["p"]["version"] == "1"
              and body and "Unplug" in body[0], (out, body and body[0]))
        for bad in (b"[]", b'{"packs": [{"version": "2", "url": "p.json"}]}', b'{"packs": "x"}'):
            u.session_factory = fake({"manifest.json": bad})
            try:
                r = asyncio.run(u.check())
                ok = "error" in r
            except Exception as e:  # noqa: BLE001
                ok, r = False, repr(e)
            check(f"malformed manifest {bad[:30]!r} is an error, not a crash", ok, r)
    finally:
        updater.KEYS_FILE = saved


if __name__ == "__main__":
    config_schema()
    dockerfile_vs_ci()
    shutdown_fits_timeout()
    backups()
    repo_hygiene()
    packs()
    updater_atomic()
    print(f"\n{len(FAILS)} failed" if FAILS else "\nall ok")
    sys.exit(1 if FAILS else 0)
