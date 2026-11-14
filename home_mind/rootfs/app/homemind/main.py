"""Home Mind server: web UI (through HA ingress), REST API, MCP endpoint, and background upkeep."""
import asyncio
import json
import logging
import os
import random
import secrets
import time
from datetime import datetime

from aiohttp import web

from .comfort import Comfort
from .body import Body
from .home import Home, plan_action
from .automations import Engine
from .journal import Journal
from .calendars import Calendars
from .ha import HA
from .mcp import MCP
from .mind import Mind
from .models import Models
from .suites import SUITE_INFO
from .updater import Updater
from .version import VERSION

DATA = os.environ.get("HOME_MIND_DATA", "/data")
PORT = int(os.environ.get("HOME_MIND_PORT", "8099"))
INGRESS_IPS = {"172.30.32.2"}
WEB = os.path.join(os.path.dirname(__file__), "web")
log = logging.getLogger("home_mind")

DEFAULTS = {"models": [{"name": "haiku", "type": "ha_ai_task", "entity_id": "ai_task.claude_ai_task"}],
            "update_url": "https://raw.githubusercontent.com/Yitz42/home-mind/main/packs/manifest.json",
            "auto_update_packs": True, "auto_test_hours": 0, "fade_days": 60, "log_level": "info", "comfort": True,
            "home": True, "home_controls": [], "home_fan_max": 100,
            "fallback": ["haiku"], "voice_seconds": 8}


def load_options():
    path = os.environ.get("HOME_MIND_OPTIONS", os.path.join(DATA, "options.json"))
    opts = dict(DEFAULTS)
    try:
        with open(path) as f:
            opts.update({k: v for k, v in json.load(f).items() if v is not None})
    except (OSError, ValueError):
        pass
    return opts


def load_token():
    path = os.path.join(DATA, "token")
    try:
        with open(path) as f:
            t = f.read().strip()
            if t:
                return t
    except OSError:
        pass
    t = secrets.token_urlsafe(32)
    os.makedirs(DATA, exist_ok=True)
    with open(path, "w") as f:
        f.write(t)
    return t


class App:
    def __init__(self):
        self.opts = load_options()
        logging.basicConfig(level=getattr(logging, str(self.opts.get("log_level", "info")).upper(), logging.INFO),
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.token = load_token()
        self.models = Models(self.opts.get("models"))
        self.mind = Mind(DATA, self.models)
        self.updater = Updater(self.mind.store, self.opts.get("update_url"))
        self.mcp = MCP(self)
        self.ha = None
        self.comfort = None
        self.bridge = None
        self.started = time.time()
        self.tasks = []

    def status(self):
        return {"version": VERSION, "uptime_s": int(time.time() - self.started), **self.mind.store.stats(),
                "packs": self.updater.installed(), "models": [m["name"] for m in self.models.list()],
                "bridge": self.bridge, "in_home_assistant": bool(self.ha and self.ha.available),
                "updates": self.updater.last and {k: v for k, v in self.updater.last.items() if k != "manifest"}}

    # ---- lifecycle --------------------------------------------------------------------------------------------
    async def on_start(self, _app):
        await self.models.start()
        self.ha = HA(self.models.session)
        self.comfort = Comfort(self.mind, self.ha)
        self.body = Body(self.mind.store, self.ha)
        self.home = Home(self.mind.store, self.ha, self.opts)
        self.mind.home = self.home if self.opts.get("home", True) else None
        self.engine = Engine(self.mind.store, self.ha, DATA)
        self.mind.FALLBACK = list(self.opts.get("fallback") or [])
        self.mind.VOICE_SECONDS = float(self.opts.get("voice_seconds") or 8)
        self.journal = Journal(self.mind.store, self.models)
        self.calendars = Calendars(self.mind.store, self.ha)
        self._mark_secrets()
        self.mind.calendars = self.calendars
        if datetime.now().hour * 60 + datetime.now().minute >= 23 * 60 + 30:
            self.journal.done_for = datetime.now().date()     # started after 23:30: don't summarize at once
        self.mind.on_test_done = self.test_done
        for r in self.updater.install_bundled():
            log.info("installed bundled pack %s %s", r["id"], r["version"])
        try:
            self.bridge = self.ha.install_bridge(self.token, PORT)
        except Exception as e:  # noqa: BLE001
            self.bridge = f"error: {e}"
        if self.bridge in ("installed", "updated"):
            await self.ha.notify("Home Mind", "The **Home Mind Bridge** integration was " + self.bridge +
                                 ". Restart Home Assistant, then add it under Settings > Devices & services > Add "
                                 "integration > Home Mind Bridge. It will show the address to link Claude to.",
                                 "home_mind_bridge")
        # build the word list for typo/sound repair now, so the first question doesn't wait for it
        await asyncio.get_running_loop().run_in_executor(None, self.mind.store.vocab)
        self.tasks = [asyncio.create_task(self.upkeep())]
        if self.ha.available:
            self.tasks.append(asyncio.create_task(self.engine.run_forever()))
        log.info("Home Mind %s up on port %s (bridge: %s)", VERSION, PORT, self.bridge)

    async def on_stop(self, _app):
        for t in self.tasks:
            t.cancel()
        await self.models.stop()

    def _mark_secrets(self):
        """Notes filed before secrets were flagged get the flag once (by the same guess as new notes)."""
        from .privacy import is_sensitive
        st = self.mind.store
        n = 0
        for r in st.db.execute("SELECT id, kind, title, body, meta FROM items WHERE meta NOT LIKE '%\"sensitive\"%'").fetchall():
            if r["kind"] != "procedure" and is_sensitive(r["title"] + " " + r["body"]):
                m = json.loads(r["meta"] or "{}")
                m["sensitive"] = True
                st.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(m), r["id"]))
                n += 1
        st.db.commit()
        if n:
            log.info("marked %d older notes as secret", n)

    async def do(self, text, model=None, source="api"):
        """A request in words ("dim the desk light to 30%") -> one checked action, planned by a model."""
        if not self.home.catalog:
            await self.home.refresh()
        names = [n for n in self.models.models if n != "mock"]
        model = model or ("haiku" if "haiku" in names else names[0] if names else None)
        if not model:
            return {"error": "no model connected"}
        things = self.home.controllable()
        plan = await plan_action(self.models.get(model), text, things)
        if not plan.get("entity_id"):
            return {"error": "nothing on the control list fits", "plan": plan}
        try:
            return {"plan": plan, "done": await self.home.act(plan["entity_id"], plan.get("action"), plan.get("value"),
                                                              source)}
        except (ValueError, PermissionError) as e:
            return {"plan": plan, "error": str(e)}

    async def push_counts(self):
        s = self.mind.store.stats()
        await self.ha.sensor("sensor.home_mind_items", s["items"],
                             {"friendly_name": "Home Mind items", "icon": "mdi:brain", "unit_of_measurement": "items",
                              "state_class": "measurement", "shelves": s["shelves"], "faded": s["faded"],
                              "versions": s["versions"], "by_kind": s["by_kind"]})

    async def test_done(self, run):
        if run["status"] != "done":
            return
        s = run["summary"]
        await self.ha.sensor(f"sensor.home_mind_test_{run['suite']}", round(100 * s.get("accuracy", 0), 1),
                             {"friendly_name": f"Home Mind test {run['suite']}", "unit_of_measurement": "%",
                              "state_class": "measurement", "icon": "mdi:test-tube", "arm": run["arm"],
                              "model": run["model"], "run": run["id"], "seed": run["seed"], **s})

    async def upkeep(self):
        last = {"fade": 0, "packs": 0, "test": time.time(), "counts": 0, "comfort": 0, "body": 0, "home": 0}
        home_first = True
        if self.ha.available:
            try:
                log.info("weigh-ins filed from history: %s", await self.body.backfill())
            except Exception:  # noqa: BLE001
                log.exception("body backfill")
        while True:
            now = time.time()
            if self.ha.available and self.mind.home and now - last["home"] > 300:
                last["home"] = now
                try:
                    await self.home.refresh()
                    n = self.home.log_changes(first=home_first)
                    if home_first:
                        log.info("home: %d things, %d filed", len(self.home.catalog), n)
                    home_first = False
                except Exception:  # noqa: BLE001
                    log.exception("home")
            if self.ha.available and now - last.get("beat", 0) > 60:
                last["beat"] = now
                await self.ha.sensor("sensor.home_mind_heartbeat", datetime.now().astimezone().isoformat(timespec="seconds"),
                                     {"friendly_name": "Home Mind heartbeat", "icon": "mdi:heart-pulse",
                                      "device_class": "timestamp",
                                      "automations_run": sorted(e for e in self.engine.owned
                                                                if self.engine.autos.get(e, {}).get("ha_state") != "on")})
            await self.journal.tick()
            if self.ha.available and now - last.get("cal", 0) > 3600:
                last["cal"] = now
                try:
                    log.info("calendars: %s", await self.calendars.sync())
                except Exception:  # noqa: BLE001
                    log.exception("calendars")
            if self.ha.available and not last.get("backfilled") and self.home.catalog and \
                    getattr(self.engine, "_ws", None):
                last["backfilled"] = True
                try:
                    log.info("home history: %s", await self.home.backfill(self.engine.ws_call,
                                                                         path=os.path.join(DATA, "home_backfill.json")))
                except Exception:  # noqa: BLE001
                    log.exception("home backfill")
            if self.ha.available and now - last["body"] > 60:
                last["body"] = now
                try:
                    await self.body.poll()
                except Exception:  # noqa: BLE001
                    log.exception("body")
            if self.opts.get("comfort") and self.ha.available:
                try:
                    await self.comfort.poll_buttons()
                    if now - last["comfort"] > 300:
                        last["comfort"] = now
                        await self.comfort.cycle()
                except Exception:  # noqa: BLE001
                    log.exception("comfort")
            try:
                if now - last["counts"] > 600:
                    last["counts"] = now
                    await self.push_counts()
                if now - last["fade"] > 86400:
                    last["fade"] = now
                    self.mind.store.fade(int(self.opts.get("fade_days") or 0))
                if now - last["packs"] > 12 * 3600:
                    last["packs"] = now
                    await (self.updater.apply() if self.opts.get("auto_update_packs") else self.updater.check())
                h = int(self.opts.get("auto_test_hours") or 0)
                if h and now - last["test"] > h * 3600:
                    last["test"] = now
                    for suite in ("recall", "update", "locate", "tasks"):
                        await self.mind.run_test_now(suite, "brain", seed=random.randint(1, 99999))
            except Exception:  # noqa: BLE001
                log.exception("upkeep")
            await asyncio.sleep(20)


# ---- HTTP ------------------------------------------------------------------------------------------------------
def build(app_state):
    @web.middleware
    async def auth(request, handler):
        if request.method == "OPTIONS":
            return web.Response(status=204)
        peer = request.remote or ""
        tok = request.headers.get("X-Home-Mind-Token") or request.headers.get("Authorization", "").removeprefix("Bearer ")
        if peer in INGRESS_IPS or (tok and secrets.compare_digest(tok, app_state.token)):
            return await handler(request)
        if os.environ.get("HOME_MIND_OPEN") == "1" and peer in ("127.0.0.1", "::1"):
            return await handler(request)
        return web.json_response({"error": "not allowed"}, status=401)

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except (ValueError, KeyError) as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:  # noqa: BLE001
            log.exception("request failed")
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    A, st = app_state, app_state.mind.store
    J = lambda x, status=200: web.json_response(x, status=status, dumps=lambda o: json.dumps(o, default=str))
    r = web.RouteTableDef()

    @r.get("/")
    async def index(_):
        return web.FileResponse(os.path.join(WEB, "index.html"))

    @r.get("/api/status")
    async def status(_):
        return J(A.status())

    @r.get("/api/recall")
    async def recall(q):
        p = q.query
        res = st.recall(p.get("q", ""), shelf=p.get("shelf"), kind=p.get("kind"), limit=int(p.get("limit", 10)),
                        include_old=p.get("old") == "1", touch=p.get("touch", "1") == "1")
        if p.get("raw") != "1":
            # what leaves through this lookup goes to Jeeves (Claude in the cloud): hide secrets not asked for
            from . import privacy as sec
            from .store import words, names_in
            qq = st.fix_typos(p.get("q", ""))
            res = [dict(h, body=sec.mask(h.get("body") or "", qq, words, names_in),
                        title=sec.mask(h.get("title") or "", qq, words, names_in)) for h in res]
        if not p.get("shelf") and not p.get("kind"):
            # a question about a day ("what do I have Monday?") gets that day's calendar events first
            from .calendars import asked_days
            days = asked_days(p.get("q", ""), datetime.now().date())
            evs = [e for d in days for e in A.calendars.events_on(d)]
            if evs:
                ids = {e["id"] for e in evs}
                res = [dict(e, score=99.0) for e in evs] + [h for h in res if h["id"] not in ids]
            # a question about how a reading went ("coldest this week") gets a summary of its dated readings
            if A.home.catalog:
                hist = A.home.series_lines(p.get("q", ""))
                if hist:
                    res = [{"id": 0, "title": "Reading history", "body": hist, "shelf": "home/history", "kind": "note",
                            "updated": time.time(), "score": 98.0}] + res
        return J(res)

    @r.post("/api/remember")
    async def remember(q):
        b = await q.json()
        return J(st.remember(text=b.get("text", ""), title=b.get("title", ""), kind=b.get("kind", "note"),
                             shelf=b.get("shelf", "inbox"), tags=b.get("tags"), key=b.get("key"), meta=b.get("meta"),
                             source=b.get("source", "api"), at=b.get("at")))

    @r.get("/api/browse")
    async def browse(q):
        return J(st.browse(q.query.get("shelf")))

    @r.get("/api/item/{id}")
    async def item(q):
        i = int(q.match_info["id"])
        d = st.get(i, history=True)
        if d:
            # other versions of the same fact, as the brain groups them when searching
            res = st.recall(d["title"], limit=12, include_old=True, touch=False)
            mine = next((h for h in res if h["id"] == i), {})
            head = mine.get("newer") or i
            d["versions"] = [{"id": h["id"], "title": h["title"], "created": h["created"], "status": h["status"],
                              "current": not h.get("newer")}
                             for h in res if h["id"] != i and (h["id"] == head or h.get("newer") == head)]
            d["newer"] = mine.get("newer")
        return J(d)

    @r.get("/api/recent")
    async def recent(q):
        hours = float(q.query.get("hours", 48))
        lim = min(500, int(q.query.get("limit", 100)))
        f = q.query.get("filter", "told")
        where = {"told": "source NOT IN ('home','calendar','automations') AND source NOT LIKE 'pack:%'", "all": "1=1",
                 "calendar": "source='calendar'", "house": "source='home'", "automations": "source='automations'",
                 "archived": "status='archived'"}.get(f, "1=1")
        col = "updated" if f == "archived" else "created"
        rows = st.db.execute(f"SELECT * FROM items WHERE {where} AND {col} >= ? ORDER BY {col} DESC LIMIT ?",
                             (time.time() - hours * 3600, lim)).fetchall()
        return J([{k: v for k, v in st._row(r).items() if k in ("id", "title", "body", "shelf", "kind", "source",
                                                                "created", "status", "meta")} for r in rows])

    @r.post("/api/item/{id}/{op}")
    async def item_op(q):
        i, op = int(q.match_info["id"]), q.match_info["op"]
        if op == "forget":
            return J(st.set_status(i, "archived"))
        if op == "restore":
            return J(st.set_status(i, "active"))
        if op == "revise":
            b = await q.json()
            return J(st.revise(i, title=b.get("title"), body=b.get("text"), shelf=b.get("shelf"), tags=b.get("tags"),
                               key=b.get("key"), meta=b.get("meta")))
        if op == "sensitive":
            b = await q.json()
            d = st.get(i)
            meta = dict(d["meta"] or {}, sensitive=bool(b.get("value")))
            st.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(meta), i))
            st.db.commit()
            return J({"id": i, "sensitive": meta["sensitive"]})
        if op == "feedback":
            b = await q.json()
            return J(st.feedback(i, bool(b.get("helpful")), b.get("note", "")))
        raise web.HTTPNotFound()

    @r.get("/api/tasks")
    async def tasks(q):
        return J(st.tasks(project=q.query.get("project"), status=q.query.get("status", "open")))

    @r.get("/api/models")
    async def models(_):
        return J(A.models.list())

    @r.post("/api/models/check")
    async def models_check(_):
        return J(await A.models.check_all())

    @r.post("/api/ask")
    async def ask(q):
        b = await q.json()
        res = await A.mind.ask(b["question"], b.get("model"), use_brain=b.get("use_brain", True),
                               expand=bool(b.get("expand")), voice=bool(b.get("voice")),
                               deadline=b.get("deadline"), fallback=b.get("fallback", True))
        res["hits"] = [{"id": h["id"], "title": h["title"], "shelf": h["shelf"], "score": h["score"]} for h in res["hits"]]
        return J(res)

    @r.post("/api/organize")
    async def organize(q):
        b = await q.json()
        return J(await A.mind.organize(b["text"], b["model"], source="api"))

    @r.get("/api/suites")
    async def suites_(_):
        return J(SUITE_INFO)

    @r.post("/api/tests")
    async def tests(q):
        b = await q.json()
        run = A.mind.start_test(b["suite"], b.get("arm", "brain"), b.get("model") or None, b.get("seed"),
                                b.get("n"), b.get("ingest", "raw"), bool(b.get("keyed")),
                                bool(b.get("expand")))
        return J({"id": run["id"], "status": run["status"], "seed": run["seed"]})

    @r.get("/api/runs")
    async def runs(q):
        return J(st.runs(limit=int(q.query.get("limit", 50)), suite=q.query.get("suite")))

    @r.get("/api/runs/{id}")
    async def run(q):
        return J(st.run(q.match_info["id"]))

    @r.get("/api/trend")
    async def trend(q):
        return J(st.trend(q.query["suite"], q.query.get("name", "accuracy"), q.query.get("arm")))

    @r.get("/api/events")
    async def events(q):
        rows = st.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(q.query.get("limit", 100)),))
        return J([dict(x) for x in rows])

    @r.get("/api/updates")
    async def updates(_):
        return J(await A.updater.check())

    @r.post("/api/updates/apply")
    async def updates_apply(_):
        return J(await A.updater.apply())

    @r.get("/api/home")
    async def home_status(q):
        if not A.home.catalog:
            await A.home.refresh()
        qs = q.query.get("q")
        if qs:
            return J({"now": A.home.now_lines(qs)})
        return J({"things": [{"entity_id": e, "name": c["name"], "kind": c["kind"], "now": A.home.describe(e),
                              "controllable": A.home.allowed(e)} for e, c in sorted(A.home.catalog.items())]})

    @r.post("/api/home/act")
    async def home_act(q):
        b = await q.json()
        if not A.home.catalog:
            await A.home.refresh()
        try:
            return J(await A.home.act(b["entity_id"], b["action"], b.get("value"), b.get("source", "api")))
        except (ValueError, PermissionError) as e:
            return J({"error": str(e)}, status=400)

    @r.post("/api/do")
    async def home_do(q):
        b = await q.json()
        return J(await A.do(b.get("text", ""), b.get("model"), b.get("source", "api")))

    @r.get("/api/automations")
    async def autos_list(_):
        return J({"automations": A.engine.summary(), "recent": A.engine.log[-30:]})

    @r.post("/api/automations/{op}")
    async def autos_op(q):
        b = await q.json() if q.can_read_body else {}
        op = q.match_info["op"]
        ids = b.get("ids") or []
        if ids == "all" or b.get("all"):
            ids = [e for e, a in A.engine.autos.items() if a["ha_state"] == "on" or e in A.engine.owned]
        try:
            if op == "takeover":
                return J(await A.engine.takeover(ids))
            if op == "release":
                return J(await A.engine.release(ids or sorted(A.engine.owned)))
            if op == "run":
                return J(await A.engine.run_now(b["id"], b.get("trigger")))
            if op == "retire":
                return J(await A.engine.retire(ids))
            if op == "reload":
                await A.engine.load()
                return J({"loaded": len(A.engine.autos)})
        except (ValueError, RuntimeError) as e:
            return J({"error": str(e)}, status=400)
        return J({"error": f"unknown op {op}"}, status=404)

    @r.post("/api/journal/summarize")
    async def journal_sum(q):
        b = await q.json() if q.can_read_body else {}
        from datetime import date
        day = date.fromisoformat(b["day"]) if b.get("day") else None
        return J(await A.journal.summarize(day, b.get("model")))

    @r.get("/api/journal")
    async def journal_list(q):
        from datetime import date
        day = date.fromisoformat(q.query["day"]) if q.query.get("day") else datetime.now().date()
        return J({"day": str(day), "entries": A.journal.entries(day)})

    @r.get("/api/calendar")
    async def cal_day(q):
        from datetime import date
        if q.query.get("q"):
            return J({"text": A.calendars.lines_for(q.query["q"])})
        day = date.fromisoformat(q.query["day"]) if q.query.get("day") else datetime.now().date()
        return J({"day": str(day), "events": [e["body"] for e in A.calendars.events_on(day)], "last_sync": A.calendars.last})

    @r.post("/api/calendar/sync")
    async def cal_sync(_):
        return J(await A.calendars.sync())

    @r.post("/api/body/backfill")
    async def body_backfill(_):
        n = await A.body.backfill()
        return J({"filed": n, "debug": getattr(A.body, "debug", None)})

    @r.get("/api/comfort")
    async def comfort_status(_):
        return J(A.comfort.status())

    @r.post("/api/comfort/vote")
    async def comfort_vote(q):
        b = await q.json()
        return J(await A.comfort.vote(b["feeling"], b.get("source", "api")))

    @r.post("/api/comfort/cycle")
    async def comfort_cycle(_):
        return J(await A.comfort.cycle(reason="asked"))

    @r.post("/mcp")
    async def mcp_post(q):
        try:
            msg = await q.json()
        except ValueError:
            return J({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, 400)
        out = await A.mcp.handle(msg)
        if out is None:
            return web.Response(status=202)
        return J(out)

    @r.get("/mcp")
    async def mcp_get(_):
        return web.Response(status=405, headers={"Allow": "POST"})

    @r.delete("/mcp")
    async def mcp_delete(_):
        return web.Response(status=204)

    app = web.Application(middlewares=[auth, errors], client_max_size=8 * 1024 * 1024)
    app.add_routes(r)
    app.on_startup.append(A.on_start)
    app.on_cleanup.append(A.on_stop)
    return app


def main():
    state = App()
    web.run_app(build(state), host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
