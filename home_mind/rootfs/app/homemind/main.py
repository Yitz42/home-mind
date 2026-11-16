"""Home Mind server: web UI (through HA ingress), REST API, MCP endpoint, and background upkeep."""
import asyncio
import json
import logging
import os
import random
import re
import secrets
import time
from datetime import date, datetime, timedelta

from aiohttp import web

from .comfort import Comfort
from .control import Control, quiet_window
from .body import Body
from .home import Home, plan_action
from .automations import Engine
from .journal import Journal
from .calendars import Calendars
from .morning import Morning
from .evening import Evening
from .overnight import ConvLog, Overnight
from .pantry import Pantry
from . import nutrition
from .patterns import FACTOR, Patterns
from .knowledge import Knowledge
from .ha import HA
from .mcp import MCP
from .mind import Mind
from .models import Models
from .suites import SUITE_INFO
from .updater import Updater
from .usage import Usage
from . import models as models_mod
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
            "fallback": ["haiku"], "voice_seconds": 8, "morning_time": "07:30", "morning_notify": "", "overnight_time": "03:00", "overnight_model": "",
            "overnight_enabled": True, "morning_enabled": True, "evening_enabled": True, "evening_time": "21:00",
            "evening_notify": "", "house_notices": True, "automation_pin": "", "people": [],
            "personal_rule": "first_person", "unknown_caller": "admin", "embed_model": "",
            "encrypt_secrets": False, "backup_target": "", "backup_url": "", "backup_user": "", "backup_password": "",
            "backup_passphrase": "", "backup_every_hours": 6, "backup_keep": 14, "backup_region": "",
            "learn_filing": True, "docs_folder": "/share/home-mind/docs", "homebox_url": "", "homebox_token": "",
            "paperless_url": "", "paperless_token": "", "rerank_model": "",
            "markdown_folder": "", "markdown_minutes": 10, "compare_models": [], "compare_days": 0,
            "compare_suites": ["recall", "tricky", "hard"],
            "answer_edges": False, "answer_notes_first": False,
            "events_keep_days": 90, "quiet_hours": True, "quiet_start": "22:00", "quiet_end": "07:00",
            "notify_daily_cap": 0, "checkin_old_facts": True, "checkin_after_days": 180,
            "overnight_structure": False,
            "calendar_quiet": ["calendar.hebcal_baltimore", "calendar.hebcal_baltimore_2", "calendar.hebcal_baltimore_3",
                               "calendar.jewish_holidays"]}


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
        self.usage = Usage(self.mind.store, {m["name"]: (float(m["price_in"]), float(m["price_out"]))
                                             for m in (self.opts.get("models") or [])
                                             if m.get("price_in") is not None and m.get("price_out") is not None})
        models_mod.USAGE = self.usage.record
        self.updater = Updater(self.mind.store, self.opts.get("update_url"))
        self.mcp = MCP(self)
        self.ha = None
        self.comfort = None
        self.bridge = None
        self.control = None
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
        self.mind.rerank_model = self.opts.get("rerank_model") or None
        if self.opts.get("embed_model"):
            from .embeddings import Embeddings
            try:
                self.mind.embeddings = Embeddings(self.mind.store, self.models.get(self.opts["embed_model"]))
            except Exception as e:  # noqa: BLE001 - a missing model name: keyword search only
                log.warning("embed_model: %s", e)
        self.ha = HA(self.models.session)
        self.ha.quiet = quiet_window(self.opts)
        self.ha.cap = int(self.opts.get("notify_daily_cap") or 0)
        from .tokens import Tokens
        self.tokens = Tokens(self.mind.store)
        self.setup_vault()
        from .docs import Docs
        from .apps import Apps
        self.mind.docs = self.docs = Docs(self.mind.store, self.opts.get("docs_folder") or None)
        self.mind.apps = Apps(self.opts, lambda: self.models.session)
        if self.opts.get("learn_filing", True) is not False:
            from .filing import Filer
            self.mind.store.filer = Filer(self.mind.store)
        from .offsite import Offsite
        self.offsite = Offsite(self.mind.store, self.opts, lambda: self.models.session)
        from .mdsync import MdSync
        from .views import Views
        from .compare import Compare
        self.mdsync = MdSync(self.mind.store, self.opts.get("markdown_folder") or None)
        self.mind.views = self.views = Views(self.mind.store)
        self.compare = Compare(self.mind, self.opts.get("compare_models") or [], self.opts.get("compare_days") or 0,
                               self.opts.get("compare_suites") or None)
        self.comforts = self.comfort_rooms()
        self.comfort = self.comforts[0]
        self.body = Body(self.mind.store, self.ha)
        self.home = Home(self.mind.store, self.ha, self.opts)
        self.mind.home = self.home if self.opts.get("home", True) else None
        self.engine = Engine(self.mind.store, self.ha, DATA)
        self.mind.FALLBACK = list(self.opts.get("fallback") or [])
        self.mind.VOICE_SECONDS = float(self.opts.get("voice_seconds") or 8)
        self.mind.ORDER = "edges" if self.opts.get("answer_edges") else "default"
        self.mind.NOTES_FIRST = bool(self.opts.get("answer_notes_first"))
        self.journal = Journal(self.mind.store, self.models)
        self.calendars = Calendars(self.mind.store, self.ha, self.opts.get("calendar_quiet"))
        self._mark_secrets()
        if self.mind.store.kv_get("structure_backfill") != 1:
            from . import structure
            n = structure.backfill(self.mind.store)
            self.mind.store.kv_set("structure_backfill", 1)
            log.info("structure read from %d notes filed before 0.19", n)
        self._fix_kg_backfill()
        self.convlog = ConvLog(self.mind.store)
        self.mind.convlog = self.convlog
        self.overnight = Overnight(self.mind.store, self.models, self.convlog, self.opts.get("overnight_time") or "03:00",
                                   self.opts.get("overnight_model") or None)
        self.overnight.enabled = self.opts.get("overnight_enabled", True) is not False
        self.overnight.structure_on = bool(self.opts.get("overnight_structure"))
        self.morning = Morning(self.mind.store, self.ha, self.calendars, self.opts.get("morning_time") or "07:30",
                               self.opts.get("morning_notify") or "")
        self.morning.enabled = self.opts.get("morning_enabled", True) is not False
        self.morning.checkin_on = self.opts.get("checkin_old_facts", True) is not False
        self.morning.checkin_days = int(self.opts.get("checkin_after_days") or 180)
        self.evening = Evening(self.mind.store, self.ha, self.opts.get("evening_time") or "21:00",
                               self.opts.get("evening_notify") or self.opts.get("morning_notify") or "",
                               phone=self.morning._phone)
        self.evening.enabled = self.opts.get("evening_enabled", True) is not False
        if (datetime.now().hour, datetime.now().minute) >= self.evening.at:
            self.evening.done_for = datetime.now().date()      # started after the time: wait for tomorrow
        self.house_notices = self.opts.get("house_notices", True) is not False
        self.overnight.known_only = bool(self.opts.get("people"))
        self.convlog.who = self.who_by_device
        self.pantry = Pantry(self.mind.store, self.ha, self.models, DATA)
        self.knowledge = Knowledge(DATA, self.opts)
        self.mind.knowledge = self.knowledge
        self.pantry.knowledge = self.knowledge
        self.morning.pantry = self.pantry
        self.patterns = Patterns(self.mind.store, self.ha, self.calendars, self.opts, phone=self.morning._phone)
        self.engine.event_handlers.setdefault("mobile_app_notification_action", []).append(
            lambda d: self.patterns.on_action(d.get("action")))
        self.engine.event_handlers["mobile_app_notification_action"].append(
            lambda d: self.morning.on_action(d.get("action")))
        try:
            n = nutrition.file_into(self.mind.store)
            if n:
                log.info("filed %d nutrition studies", n)
        except Exception as e:                       # the studies are a nice-to-have, never block start-up
            log.warning("nutrition studies not filed: %s", e)
        mt = self.morning.at
        if (datetime.now().hour, datetime.now().minute) >= mt:
            self.morning.done_for = datetime.now().date()      # started after the time: wait for tomorrow
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
        # switches, numbers and buttons for Home Assistant (through the bridge); put back changes made there
        self.control = Control(self)
        self.control.load()
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

    def _fix_kg_backfill(self):
        """0.8.x filed the scale's early kilogram readings with 'lb'. Rewrite them once, converted."""
        st = self.mind.store
        if st.kv_get("fixed_kg_backfill"):
            return
        cutoff = datetime.fromisoformat("2026-11-11T19:26:55-05:00").timestamp()
        n = 0
        for r in st.db.execute("SELECT id FROM items WHERE source='home' AND created < ? AND status='active' AND "
                               "meta LIKE '%\"backfill\": true%' AND meta LIKE '%sensor.yitz_%'", (cutoff,)).fetchall():
            d = st.get(r["id"])
            eid, v = d["meta"].get("entity_id", ""), d["meta"].get("state")
            if not re.search(r"weight|mass", eid) or v is None:
                continue
            try:
                lb = float(v) * 2.20462
            except ValueError:
                continue
            body = re.sub(r": [\d.]+ lb\.$", f": {lb:.1f} lb.", d["body"])
            title = re.sub(r": [\d.]+ lb$", f": {lb:.1f} lb", d["title"])
            st.db.execute("UPDATE items SET body=?, title=? WHERE id=?", (body, title, r["id"]))
            n += 1
        st.db.commit()
        st.kv_set("fixed_kg_backfill", True)
        if n:
            log.info("converted %d early kilogram readings to lb", n)

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
            await self.overnight.tick()
            if self.ha.available and now - last.get("shop", 0) > 300:
                last["shop"] = now
                try:
                    n = await self.pantry.sync_shopping()
                    if n:
                        log.info("pantry: %d items from the shopping list", n)
                except Exception:  # noqa: BLE001
                    log.exception("shopping list sync")
            if self.ha.available and now - last.get("conv", 0) > 60 and getattr(self.engine, "_ws", None):
                last["conv"] = now
                try:
                    n = await self.convlog.poll(self.engine.ws_call)
                    if n:
                        log.info("conversation log: %d new turns", n)
                except Exception as e:  # noqa: BLE001
                    log.debug("conversation log: %s", e)
            if self.ha.available:
                await self.morning.tick()
                await self.evening.tick()
            if self.ha.available and self.home.notices:
                notes, self.home.notices = self.home.notices[:2], []      # very few (#40): at most two at a time
                if self.house_notices:
                    target = self.evening.notify or await self.morning._phone()
                    for title, msg in notes:
                        try:
                            await self.ha.notify(title, msg, "home_mind_house")
                            if target:
                                await self.ha.push(target, {"title": title, "message": msg})
                        except Exception as e:  # noqa: BLE001
                            log.warning("house notice: %s", e)
            if now - last.get("docs", 0) > 3600:
                last["docs"] = now
                try:
                    r = await asyncio.to_thread(self.docs.scan)
                    if r.get("added") or r.get("removed"):
                        log.info("documents: %s", r)
                except Exception as e:  # noqa: BLE001
                    log.warning("documents folder: %s", e)
            if self.mdsync.folder and now - last.get("md", 0) > 60 * float(self.opts.get("markdown_minutes") or 10):
                last["md"] = now
                try:
                    r = await asyncio.to_thread(self.mdsync.run)
                    if any(r.get(k) for k in ("updated_notes", "new_notes", "archived", "conflicts")):
                        log.info("markdown sync: %s", r)
                except Exception as e:  # noqa: BLE001
                    log.warning("markdown sync: %s", e)
            if self.compare.due(now):
                try:
                    r = await self.compare.run()
                    log.info("model comparison: best %s, dropped %s", r.get("best"), r.get("dropped"))
                    for m, row in (r.get("models") or {}).items():
                        if row.get("accuracy") is not None:
                            await self.ha.sensor(f"sensor.home_mind_compare_{re.sub(r'[^a-z0-9]+', '_', m.lower())}",
                                                 round(100 * row["accuracy"], 1),
                                                 {"friendly_name": f"Home Mind model {m}", "unit_of_measurement": "%",
                                                  "state_class": "measurement", "icon": "mdi:scale-balance",
                                                  "ms_avg": row.get("ms_avg"), "suites": row.get("suites")})
                except Exception as e:  # noqa: BLE001
                    log.warning("model comparison: %s", e)
            if self.offsite.due(now):
                try:
                    r = await self.offsite.run(now)
                    log.info("off-site backup: %s", r.get("file") or r.get("error"))
                except Exception as e:  # noqa: BLE001
                    log.warning("off-site backup: %s", e)
                    self.offsite.last["at"] = now      # try again next period, not every few seconds
            emb = getattr(self.mind, "embeddings", None)
            if emb and now - last.get("embed", 0) > 30:
                last["embed"] = now
                try:
                    await emb.index(32)         # new and changed notes, a batch at a time (#10)
                except Exception as e:  # noqa: BLE001
                    log.warning("embedding notes: %s", e)
                    last["embed"] = now + 600   # the model is down: try again in ten minutes
            if now - last.get("house", 0) > 86400 and self.home.catalog:
                last["house"] = now
                try:
                    r = self.learn_house()
                    log.info("house: %d habits, %d suggestions, %d baselines", *r)
                except Exception:  # noqa: BLE001
                    log.exception("house learning")
            if now - last.get("knowledge", 0) > 60:
                last["knowledge"] = now
                try:
                    await self.knowledge.tick()
                except Exception:  # noqa: BLE001
                    log.exception("knowledge")
            if self.ha.available and now - last.get("patterns", 0) > 240:
                last["patterns"] = now
                try:
                    if self.mind.store.kv_get("patterns_backfilled") != 3:      # 3: + naps, air, time away, button times
                        self.mind.store.db.execute("DELETE FROM days")
                        n = await self.patterns.refresh(10)
                        for i in range(1, 11):
                            self.patterns.file_weather(date.today() - timedelta(days=i))
                        self.mind.store.kv_set("patterns_backfilled", 3)
                        log.info("patterns: %d days from history", n)
                    await self.patterns.tick()
                except Exception:  # noqa: BLE001
                    log.exception("patterns")
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
                    for c in self.comforts:
                        await c.poll_buttons()
                    if now - last["comfort"] > 300:
                        last["comfort"] = now
                        for c in self.comforts:
                            await c.cycle()
                except Exception:  # noqa: BLE001
                    log.exception("comfort")
            try:
                if now - last["counts"] > 600:
                    last["counts"] = now
                    await self.push_counts()
                if now - last["fade"] > 86400:
                    last["fade"] = now
                    self.mind.store.fade(int(self.opts.get("fade_days") or 0))
                    from . import tidy
                    pairs = tidy.run(self.mind.store)
                    if pairs:
                        log.info("tidy: %d possible duplicate pairs flagged", len(pairs))
                    n = self.mind.store.prune_events(int(self.opts.get("events_keep_days") or 0))
                    if n:
                        log.info("event log: rolled up and removed %d rows older than %s days", n,
                                 self.opts.get("events_keep_days"))
                if self.ha.available:
                    n = await self.ha.flush_held()
                    if n:
                        log.info("quiet hours over: sent %d held phone notifications", n)
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


    def setup_vault(self):
        """Option encrypt_secrets (#66): seal secret notes with a key kept out of backups; turned off, open them."""
        from . import vault
        st = self.mind.store
        path = os.path.join(DATA, "vault.key")
        self.vault_status = {"enabled": False}
        if not vault.available():
            self.vault_status["error"] = "the cryptography library isn't installed"
            return
        if self.opts.get("encrypt_secrets"):
            st.vault_key = vault.load_key(path)
            n = st.seal_all()
            self.vault_status = {"enabled": True, "key": vault.fingerprint(st.vault_key), "sealed_now": n}
        elif os.path.exists(path):
            st.vault_key = vault.load_key(path, create=False)
            n = st.unseal_all()         # turned off: back to plain text, so nothing is stranded
            st.vault_key = None
            self.vault_status = {"enabled": False, "opened_now": n}
        self.vault_status["sealed"] = st.db.execute("SELECT COUNT(*) FROM items WHERE secret IS NOT NULL").fetchone()[0]

    def comfort_rooms(self):
        """Option comfort_rooms (#29): each room's entities, person and helpers; empty = the original bedroom."""
        def pairs(text):         # "hrv=input_number.x, steps=sensor.y" -> dict
            out = {}
            for part in (text or "").split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    out[k.strip()] = v.strip()
            return out
        rooms = []
        for k, r in enumerate(self.opts.get("comfort_rooms") or []):
            ents = {x: r[x] for x in ("climate", "temp_sensor", "humidifier", "fan", "phone_battery", "alarm") if r.get(x)}
            sig = pairs(r.get("signals")) if r.get("signals") is not None else None
            rooms.append(Comfort(self.mind, self.ha, room=ents, signals=sig, vars_=pairs(r.get("helpers")),
                                 person=r.get("person") or "Yitz", name=r.get("name") or f"room{k + 1}", primary=k == 0))
        return rooms or [Comfort(self.mind, self.ha)]

    def comfort_for(self, name=None):
        return next((c for c in self.comforts if c.name == name), None) if name else self.comfort

    def people(self):
        """Option people plus Home Assistant's person entities (#3)."""
        from .speakers import people_from
        persons = [{"entity_id": e, "attributes": dict(c.get("attrs") or {}, friendly_name=c.get("name"))}
                   for e, c in (self.home.catalog or {}).items() if e.startswith("person.")]
        return people_from(self.opts.get("people") or [], persons)

    def who_by_device(self, device_id):
        from .speakers import resolve
        home = {e: c.get("state") for e, c in (self.home.catalog or {}).items() if e.startswith("person.")}
        return resolve(self.people(), device=device_id, home=home)

    def speaker_for(self, b):
        """From a request body: (viewer, how). viewer None = no speaker information given (sees everything) unless
        option unknown_caller is "guest"; "" = unknown; else a name."""
        from .speakers import resolve
        if b.get("speaker") is not None:
            return str(b["speaker"]).strip(), "given"
        clues = {k: b.get(k) for k in ("user", "user_id", "device_id", "area")}
        if not any(clues.values()):
            return (None, "no speaker information") if self.opts.get("unknown_caller") != "guest" else ("", "unknown")
        home = {e: c.get("state") for e, c in (self.home.catalog or {}).items() if e.startswith("person.")}
        name, how = resolve(self.people(), user=clues["user"] or clues["user_id"], device=clues["device_id"],
                            area=clues["area"], home=home)
        return (name or ""), how

    def scope_meta(self, meta, text, viewer):
        """A known person filing in the first person: personal (#48)."""
        from .speakers import scope_for
        if viewer and "scope" not in meta:
            scope, owner = scope_for(text, viewer, self.opts.get("personal_rule") or "first_person")
            meta = dict(meta, said_by=viewer, scope=scope, **({"owner": owner} if owner else {}))
        return meta

    def learn_house(self):
        """The nightly house model (#19, #39, #40, #41): habits filed as notes, suggestions kept for the panel."""
        from . import house
        st = self.mind.store
        cfgs = [a.get("config") for a in self.engine.autos.values() if a.get("config")]
        model = house.learn(st, time.time(), cfgs, st.kv_get("suggestions_done") or [])
        st.kv_set("house_model", model)
        self.home.model = model
        house.file_habits(st, model)
        return len(model["habits"]), len(model["suggestions"]), len(model["baselines"])

    async def house_lists(self):
        states = await self.engine._get("states") or []
        services = await self.engine._get("services") or []
        return states, services

    def house_overview(self):
        from . import house
        st = self.mind.store
        m = st.kv_get("house_model") or {}
        done = st.kv_get("suggestions_done") or []
        notable = []
        for r in st.db.execute("SELECT id, created, title, meta FROM items WHERE source='home' AND created >= ? AND "
                               "meta LIKE '%salience%' ORDER BY created DESC LIMIT 300", (time.time() - 86400,)):
            mm = json.loads(r["meta"] or "{}")
            if (mm.get("salience") or 0) >= house.NOTABLE:
                notable.append({"id": r["id"], "created": r["created"], "title": r["title"],
                                "salience": mm["salience"], "why": mm.get("why"), "unusual": mm.get("unusual")})
        base = [{"entity_id": e, "name": b["name"], "low": round(b["all"]["lo"], 1), "high": round(b["all"]["hi"], 1),
                 "middle": round(b["all"]["med"], 1), "readings": b["all"]["n"]}
                for e, b in sorted((m.get("baselines") or {}).items(), key=lambda kv: kv[1]["name"])]
        return {"at": m.get("at"), "habits": [dict(h, text=house.habit_text(h)) for h in m.get("habits") or []],
                "suggestions": [x for x in m.get("suggestions") or [] if x["id"] not in done],
                "baselines": base, "notable": notable}

    async def suggestion(self, sid, op, enable=False):
        """Create (switched off unless enable) or dismiss a suggested automation. None if there's no such one."""
        from . import autodraft
        st = self.mind.store
        sug = next((x for x in (st.kv_get("house_model") or {}).get("suggestions") or [] if x["id"] == sid), None)
        if not sug or op not in ("create", "dismiss"):
            return None
        done = st.kv_get("suggestions_done") or []
        if op == "dismiss":
            st.kv_set("suggestions_done", done + [sid])
            return {"dismissed": sid}
        states, services = await self.house_lists()
        ents, svcs, names, _, _ = autodraft.house_lists(states, services)
        problems = autodraft.check(sug["config"], ents, svcs, names=names)
        if problems:
            return {"problems": problems}
        r = await self.engine.create(sug["config"], enable=enable)
        st.kv_set("suggestions_done", done + [sid])
        return r

    async def draft_automation(self, request, model=None):
        from . import autodraft
        if not request.strip():
            raise ValueError("say what the automation should do")
        states, services = await self.house_lists()
        name = model or self.opts.get("overnight_model") or (self.models.list() or [{}])[0].get("name")
        return await autodraft.draft(self.models.get(name), request, states, services)

    async def install_automation(self, cfg, enable=False, pin=""):
        """Install a drafted automation, checked again here: never trust a config because it was drafted."""
        from . import autodraft
        states, services = await self.house_lists()
        ents, svcs, names, _, _ = autodraft.house_lists(states, services)
        problems = autodraft.check(cfg, ents, svcs, pin=str(self.opts.get("automation_pin") or ""),
                                   given_pin=str(pin or ""), names=names)
        if problems:
            return {"installed": False, "problems": problems}
        return dict(await self.engine.create(cfg, enable=enable), installed=True)


# ---- HTTP ------------------------------------------------------------------------------------------------------
def build(app_state):
    @web.middleware
    async def auth(request, handler):
        if request.method == "OPTIONS":
            return web.Response(status=204)
        peer = request.remote or ""
        tok = request.headers.get("X-Home-Mind-Token") or request.headers.get("Authorization", "").removeprefix("Bearer ")
        if peer in INGRESS_IPS or (tok and secrets.compare_digest(tok, app_state.token)):
            request["scope"] = "admin"
            return await handler(request)
        rec = app_state.tokens.check(tok) if tok else None
        if rec:                     # a scoped token (#27)
            from .tokens import needed, allows
            need = needed(request.method, request.path)
            if not allows(rec["scope"], need):
                return web.json_response({"error": f"this token ({rec['name']}) can {rec['scope']}; this needs {need}"},
                                         status=403)
            request["scope"], request["token"] = rec["scope"], rec
            if rec.get("person"):   # answered as that person (#48)
                from .speakers import VIEWER
                tv = VIEWER.set(rec["person"])
                try:
                    return await handler(request)
                finally:
                    VIEWER.reset(tv)
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
        from .mind import lookup
        res = lookup(st, p.get("q", ""), int(p.get("limit", 10)), p.get("shelf"), p.get("kind"),
                     p.get("old") == "1", p.get("touch", "1") == "1", extras=p.get("raw") != "1")
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
            evs = [e for d in days for e in A.calendars.events_on(d, p.get("q", ""))]
            if evs:
                ids = {e["id"] for e in evs}
                res = [dict(e, score=99.0) for e in evs] + [h for h in res if h["id"] not in ids]
            # a question about food at home gets the pantry
            if re.search(r"\b(pantry|fridge|freezer|groceries|food|eat|cook|make for|dinner|lunch|breakfast|snack|"
                         r"expire|expiring|go bad|going bad|do i have|have any|left in)\b", p.get("q", ""), re.I):
                res = [{"id": 0, "title": "Pantry", "body": A.pantry.lines(), "shelf": "pantry", "kind": "note",
                        "updated": time.time(), "score": 97.0}] + res
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
        meta = dict(b.get("meta") or {}, **({"inferred": True} if b.get("inferred") else {}))
        if b.get("scope") in ("personal", "household"):
            meta["scope"] = b["scope"]
            if b["scope"] == "personal":
                meta["owner"] = b.get("owner") or A.speaker_for(b)[0] or ""
        meta = A.scope_meta(meta, b.get("text", ""), A.speaker_for(b)[0])
        return J(st.remember(text=b.get("text", ""), title=b.get("title", ""), kind=b.get("kind", "note"),
                             shelf=b.get("shelf", "inbox"), tags=b.get("tags"), key=b.get("key"), meta=meta or None,
                             source=b.get("source", "api"), at=b.get("at")))

    @r.get("/api/review")
    async def review(_):
        return J(st.review_queue())

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
        where = {"told": "source NOT IN ('home','calendar','automations','habits','tidy','evening') AND source NOT LIKE 'pack:%'", "all": "1=1",
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
            st.seal_item(i) if meta["sensitive"] else st.unseal_item(i)
            return J({"id": i, "sensitive": meta["sensitive"]})
        if op == "feedback":
            b = await q.json()
            return J(st.feedback(i, bool(b.get("helpful")), b.get("note", "")))
        if op == "confirm":
            return J(st.confirm(i))
        if op == "move":           # to another shelf; filing learns from where notes are (#58, #59)
            b = await q.json()
            from .store import clean_shelf
            shelf = clean_shelf(b.get("shelf") or "")
            if not shelf or shelf == "inbox" and not b.get("allow_inbox"):
                raise ValueError("give a shelf")
            d = st.get(i)
            m = {k: v for k, v in (d["meta"] or {}).items() if k not in ("suggested_shelf", "suggested_p")}
            if d["shelf"] != shelf:
                m["moved_from"] = d["shelf"]
            with st.lock:
                st.db.execute("UPDATE items SET shelf=?, meta=?, updated=? WHERE id=?", (shelf, json.dumps(m), st.clock(), i))
                st.db.commit()
            if st.filer:
                st.filer._model = None
            st.log("move", i, shelf=shelf, was=d["shelf"])
            return J({"id": i, "shelf": shelf})
        if op == "scope":          # personal (one person) or household (everyone), #48
            b = await q.json()
            d = st.get(i)
            if not d or b.get("scope") not in ("personal", "household"):
                raise ValueError("scope is personal or household")
            m = dict(d["meta"], scope=b["scope"])
            if b["scope"] == "personal":
                m["owner"] = b.get("owner") or m.get("owner") or m.get("said_by") or ""
                if not m["owner"]:
                    raise ValueError("a personal note needs an owner")
            with st.lock:
                st.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(m), i))
                st.db.commit()
            return J({"id": i, "scope": b["scope"], "owner": m.get("owner")})
        if op == "purge":          # delete for good: needs {"confirm": true}
            b = await q.json() if q.can_read_body else {}
            if b.get("confirm") is not True:
                return J({"error": "send {\"confirm\": true} to delete for good (this cannot be undone)"}, status=400)
            return J(st.purge([i]))
        raise web.HTTPNotFound()

    @r.post("/api/purge_about")
    async def purge_about(q):
        b = await q.json()
        if b.get("confirm") is not True or not (b.get("name") or "").strip():
            return J({"error": "send {\"name\": ..., \"confirm\": true} to delete for good (this cannot be undone)"},
                     status=400)
        return J(st.purge_about(b["name"]))

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
        who, how = A.speaker_for(b)
        res = await A.mind.ask(b["question"], b.get("model"), use_brain=b.get("use_brain", True),
                               expand=bool(b.get("expand")), voice=bool(b.get("voice")),
                               deadline=b.get("deadline"), fallback=b.get("fallback", True),
                               conversation_id=b.get("conversation_id"), speaker=who)
        if who is not None:
            res["speaker_how"] = how
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
                                bool(b.get("expand")), b.get("order") or "default", bool(b.get("notes_first")))
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

    @r.get("/api/structure")
    async def structure_get(_):
        from . import structure
        from .cases import Cases
        out = structure.overview(st, time.time())
        out["duplicates"] = [{"a": {k: x[k] for k in ("id", "title")}, "b": {k: y[k] for k in ("id", "title")}}
                             for x, y in ((st.get(a), st.get(b)) for a, b in (st.kv_get("tidy_pairs") or []))
                             if x and y and x["status"] == "active" and y["status"] == "active"]
        out["conflicts"] = [{"a": {k: x[k] for k in ("id", "title")}, "b": {k: y[k] for k in ("id", "title")}, "why": w}
                            for x, y, w in ((st.get(a), st.get(b), w) for a, b, w in (st.kv_get("tidy_conflicts") or []))
                            if x and y and x["status"] == "active" and y["status"] == "active"]
        out["cases"] = Cases(st).all()
        return J(out)

    @r.get("/api/profile")
    async def profile_get(_):
        from . import structure
        return J({"lines": structure.profile(st), "max": structure.PROFILE_MAX})

    @r.get("/api/tidy")
    async def tidy_get(q):
        from . import tidy
        pairs = tidy.run(st) if q.query.get("now") == "1" else (st.kv_get("tidy_pairs") or [])
        return J({"pairs": [{"a": st.get(a), "b": st.get(b)} for a, b in pairs]})

    @r.get("/api/cases")
    async def cases_get(_):
        from .cases import Cases
        return J({"cases": Cases(st).all()})

    @r.post("/api/cases")
    async def cases_add(q):
        from .cases import Cases
        b = await q.json()
        return J({"id": Cases(st).add(b["question"], b["right"], b.get("wrong", ""))})

    @r.post("/api/cases/{id}/remove")
    async def cases_remove(q):
        from .cases import Cases
        Cases(st).remove(int(q.match_info["id"]))
        return J({"removed": int(q.match_info["id"])})

    @r.get("/api/usage")
    async def usage(q):
        days = int(q.query.get("days", 30))
        return J(dict(A.usage.summary(days), rows=A.usage.rows(days)))

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

    @r.get("/api/pantry")
    async def pantry_list(q):
        return J({"items": A.pantry.items(q.query.get("status", "in")), "goals": A.pantry.goals()})

    @r.post("/api/pantry/{op}")
    async def pantry_op(q):
        op = q.match_info["op"]
        if op == "photo" and q.content_type == "application/json":
            import base64
            b = await q.json()          # {"image_b64": ..., "filename": ...}: for Claude forwarding a photo from chat
            return J(await A.pantry.from_photo(base64.b64decode(b["image_b64"]), b.get("filename") or "photo.jpg"))
        if op == "photo":
            reader = await q.multipart()
            part = await reader.next()
            data = await part.read(decode=False)
            return J(await A.pantry.from_photo(data, part.filename or "photo.jpg"))
        b = await q.json() if q.can_read_body else {}
        try:
            if op == "add":
                if b.get("text"):
                    return J({"added": [A.pantry.add(n, a, b.get("location"), source=b.get("source", "api"))
                                        for n, a in A.pantry.parse_list(b["text"])]})
                return J(A.pantry.add(b["name"], b.get("amount", ""), b.get("location"), b.get("use_by"),
                                      source=b.get("source", "api")))
            if op == "ate":
                return J(A.pantry.ate(b.get("text") or b.get("name") or "", source=b.get("source", "api")))
            if op in ("used", "tossed"):
                names = [n for n, _ in A.pantry.parse_list(b["text"])] if b.get("text") else [b["name"]]
                return J({"done": [A.pantry.use(n, "used" if op == "used" else "tossed") for n in names]})
            if op == "remove":
                return J(A.pantry.remove(b["id"]))
            if op == "suggest":
                return J(await A.pantry.suggest(b.get("meal"), b.get("model"), int(b.get("n", 3)), b.get("request", "")))
            if op == "shop":
                return J({"added": await A.pantry.add_to_shopping(b.get("items") or [])})
            if op == "goals":
                return J(A.pantry.set_goals(b))
            if op == "today":
                return J(await A.pantry.today())
        except (ValueError, KeyError, RuntimeError) as e:
            return J({"error": str(e)}, status=400)
        raise web.HTTPNotFound()

    @r.get("/api/overnight")
    async def overnight_status(_):
        return J({"last": A.overnight.last, "model": A.overnight.pick_model(), "pending_turns": len(A.convlog.pending()),
                  "recent_turns": [{k: t[k] for k in ("ts", "user_text", "reply", "pipeline", "processed")}
                                   for t in A.mind.store.db.execute(
                                       "SELECT * FROM conversations ORDER BY ts DESC LIMIT 10").fetchall()]})

    @r.post("/api/overnight/run")
    async def overnight_run(_):
        return J(await A.overnight.run())

    @r.get("/api/evening")
    async def evening_preview(_):
        return J({"text": A.evening.build()})

    @r.post("/api/evening/send")
    async def evening_send(_):
        return J(await A.evening.send())

    @r.get("/api/backup")
    async def backup_get(_):
        return J({"target": A.offsite.target or None, "last": A.offsite.last,
                  "files": A.mind.store.kv_get("offsite_files") or []})

    @r.post("/api/backup/now")
    async def backup_now(_):
        return J(await A.offsite.run())

    @r.get("/api/export")
    async def export_get(q):
        from . import portable
        data = portable.export(A.mind.store, secrets=q.query.get("secrets") == "1",
                               include_home=q.query.get("home") == "1", app_version=VERSION)
        name = f"home-mind-{datetime.now().strftime('%Y-%m-%d')}.json"
        return web.Response(text=json.dumps(data, ensure_ascii=False), content_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @r.post("/api/import")
    async def import_post(q):
        from . import portable
        if q.content_type.startswith("multipart/"):
            rd = await q.multipart()
            data = None
            async for part in rd:
                if part.filename:
                    data = json.loads((await part.read(decode=False)).decode("utf-8"))
        else:
            data = await q.json()
        try:
            return J(await asyncio.to_thread(portable.import_, A.mind.store, data, q.query.get("dry_run") == "1"))
        except ValueError as e:
            return J({"error": str(e)}, status=400)

    @r.post("/api/markdown/sync")
    async def markdown_sync(_):
        if not A.mdsync.folder:
            return J({"error": "set markdown_folder in the options first"}, status=400)
        return J(await asyncio.to_thread(A.mdsync.run))

    @r.post("/api/sql")
    async def sql_post(q):
        from . import sqlview
        b = await q.json()
        try:
            return J(await asyncio.to_thread(sqlview.query, A.mind.store.path, b.get("q") or b.get("sql")))
        except sqlview.SqlError as e:
            return J({"error": str(e), "views": sqlview.HELP}, status=400)

    @r.get("/api/views")
    async def views_list(_):
        return J(A.views.all())

    @r.post("/api/views")
    async def views_save(q):
        b = await q.json()
        try:
            return J(A.views.save(b.get("name"), b.get("query"), b.get("shelf"), b.get("kind"), b.get("tags"),
                                  b.get("days"), b.get("sort"), b.get("limit") or 20, b.get("sql")))
        except ValueError as e:
            return J({"error": str(e)}, status=400)

    @r.get("/api/views/{name}")
    async def views_run(q):
        try:
            return J(A.views.run(q.match_info["name"]))
        except KeyError as e:
            return J({"error": str(e)}, status=404)

    @r.delete("/api/views/{name}")
    async def views_delete(q):
        return J({"removed": A.views.remove(q.match_info["name"])})

    @r.get("/api/compare")
    async def compare_get(_):
        return J({**A.compare.table(), "running": A.compare.running, "every_days": A.compare.days,
                  "compares": A.compare.names()})

    @r.post("/api/compare/run")
    async def compare_run(q):
        b = await q.json() if q.can_read_body else {}
        asyncio.create_task(A.compare.run(b.get("seed")))
        return J({"started": True, "models": A.compare.names(), "suites": A.compare.suites})

    @r.get("/api/docs")
    async def docs_list(q):
        if q.query.get("q"):
            return J(A.docs.search(q.query["q"], k=5))
        return J(A.docs.list())

    @r.post("/api/docs")
    async def docs_add(q):
        if q.content_type.startswith("multipart/"):
            rd = await q.multipart()
            out = []
            async for part in rd:
                if part.filename:
                    out.append(await asyncio.to_thread(A.docs.add, part.filename, await part.read(decode=False)))
            return J(out)
        b = await q.json()
        return J(A.docs.add(b["name"], (b.get("text") or "").encode()))

    @r.delete("/api/docs/{did}")
    async def docs_remove(q):
        return J({"removed": A.docs.remove(q.match_info["did"])})

    @r.get("/api/inbox")
    async def inbox_get(_):
        """Notes in the inbox, newest first, with the shelf filing would suggest (#59)."""
        st = A.mind.store
        rows = [st._row(r) for r in st.db.execute(
            "SELECT * FROM items WHERE shelf='inbox' AND status='active' AND kind != 'procedure' ORDER BY created DESC "
            "LIMIT 100")]
        out = []
        for d in rows:
            sug = (d["meta"] or {}).get("suggested_shelf")
            if not sug and st.filer:
                s2, p = st.filer.guess(f"{d['title']} {d['body']}")
                sug = s2 if s2 and p >= 0.5 else None
            out.append({"id": d["id"], "title": d["title"], "created": d["created"], "suggested": sug})
        shelves = [r[0] for r in st.db.execute("SELECT shelf FROM items WHERE status='active' AND shelf != 'inbox' AND "
                                               "shelf NOT LIKE 'home/%' AND shelf NOT LIKE 'comfort/%' GROUP BY shelf "
                                               "ORDER BY COUNT(*) DESC LIMIT 40")]
        return J({"items": out, "shelves": shelves})

    @r.get("/api/vault")
    async def vault_get(_):
        return J(A.vault_status)

    @r.get("/api/vault/key")
    async def vault_key(_):
        """The key that opens sealed secrets: keep a copy somewhere safe (admin only)."""
        k = A.mind.store.vault_key
        return J({"key": k.hex() if k else None, "file": "/data/vault.key (left out of Home Assistant backups)"})

    @r.get("/api/tokens")
    async def tokens_list(_):
        return J(A.tokens.list())

    @r.post("/api/tokens")
    async def tokens_make(q):
        b = await q.json()
        return J(A.tokens.make(b.get("name", ""), b.get("scope", "read"), b.get("person")))

    @r.delete("/api/tokens/{tid}")
    async def tokens_revoke(q):
        return J({"revoked": A.tokens.revoke(q.match_info["tid"])})

    @r.get("/api/people")
    async def people_get(_):
        """Who Home Mind can recognise, and how (#3)."""
        return J({"people": A.people(), "learn_from_unknown_voices": not A.overnight.known_only,
                  "unknown_caller": A.opts.get("unknown_caller") or "admin"})

    @r.get("/api/house")
    async def house_get(_):
        return J(A.house_overview())

    @r.post("/api/house/learn")
    async def house_learn(_):
        h, sg, b = A.learn_house()
        return J({"habits": h, "suggestions": sg, "baselines": b})

    @r.post("/api/house/suggestion/{sid}/{op}")
    async def house_suggestion(q):
        b = await q.json() if q.can_read_body else {}
        r = await A.suggestion(q.match_info["sid"], q.match_info["op"], enable=bool(b.get("enable")))
        if r is None:
            raise web.HTTPNotFound()
        return J(r, status=400 if r.get("problems") else 200)

    @r.post("/api/automations/draft")
    async def automation_draft(q):
        b = await q.json()
        return J(await A.draft_automation(b.get("request") or "", b.get("model")))

    @r.post("/api/automations/install")
    async def automation_install(q):
        b = await q.json()
        r = await A.install_automation(b.get("config"), bool(b.get("enable")), b.get("pin") or "")
        return J(r, status=400 if r.get("problems") else 200)

    @r.get("/api/morning")
    async def morning_preview(q):
        return J({"text": A.morning.build(float(q.query.get("hours", 24)))})

    @r.post("/api/morning/send")
    async def morning_send(_):
        return J(await A.morning.send())

    @r.get("/api/knowledge")
    async def knowledge_get(q):
        return J(A.knowledge.summary())

    @r.get("/api/knowledge/food")
    async def knowledge_food(q):
        if q.query.get("upc"):
            return J({"match": A.knowledge.by_upc(q.query["upc"])})
        return J({"matches": await asyncio.to_thread(A.knowledge.food, q.query.get("q", ""), int(q.query.get("n", 3)))})

    @r.get("/api/knowledge/search")
    async def knowledge_search(q):
        qq = q.query.get("q", "")
        if q.query.get("passages"):
            return J({"passages": await asyncio.to_thread(A.knowledge.passages, qq, int(q.query.get("n", 2)))})
        res = await asyncio.to_thread(A.knowledge.search, qq, int(q.query.get("n", 3)))
        return J({"results": [{"title": x["title"], "intro": (x["text"] or [""])[0][:500]} for x in res]})

    @r.post("/api/knowledge/{op}")
    async def knowledge_op(q):
        op = q.match_info["op"]
        if op in ("food", "wiki"):
            return J({"state": A.knowledge.start_now(op)})
        return J({"error": "unknown"}, status=404)

    @r.get("/api/patterns")
    async def patterns_get(q):
        days = int(q.query.get("days", 30))
        rep = A.patterns.report()
        return J({"report": rep["text"], "findings": rep["findings"], "fog_days": rep["fog_days"],
                  "experiment": A.patterns.experiment(), "days": A.patterns.rows(days)})

    @r.post("/api/patterns/{op}")
    async def patterns_op(q):
        op = q.match_info["op"]
        b = await q.json() if q.can_read_body else {}
        P = A.patterns
        if op == "refresh":
            return J({"days": await P.refresh(int(b.get("days", 3)))})
        if op == "send":
            return J(await P.send_report())
        if op == "experiment":
            if b.get("stop"):
                return J({"result": P.stop_experiment()})
            f = b.get("factor")
            if not f:                       # "start the fog experiment": the one the report suggests
                cand = [x for x in P.report()["findings"] if x["status"] in ("holding", "candidate") and x["experiment"]]
                if not cand:
                    return J({"error": "no experiment suggested yet: not enough days or no clear link",
                              "factors": [k for k, v in FACTOR.items() if v[4]]}, status=400)
                f = sorted(cand, key=lambda x: (x["status"] != "holding", -x["diff"]))[0]["factor"]
            return J(P.start_experiment(f, int(b.get("days", 14))))
        if op == "ask":
            await P.ask(b.get("what", "fog"))
            return J({"asked": b.get("what", "fog")})
        if op == "answer":
            return J(await P.on_action(b.get("action")))
        if op == "weather":
            return J(await P.snapshot_weather())
        return J({"error": "unknown"}, status=404)

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

    def _room(q, b=None):
        c = A.comfort_for((b or {}).get("room") or q.query.get("room"))
        if not c:
            raise web.HTTPNotFound(text=json.dumps({"error": "no such comfort room", "rooms": [x.name for x in A.comforts]}),
                                   content_type="application/json")
        return c

    @r.get("/api/comfort")
    async def comfort_status(q):
        out = _room(q).status()
        out["rooms"] = [c.name for c in A.comforts]
        return J(out)

    @r.post("/api/comfort/vote")
    async def comfort_vote(q):
        b = await q.json()
        return J(await _room(q, b).vote(b["feeling"], b.get("source", "api")))

    @r.post("/api/comfort/cycle")
    async def comfort_cycle(q):
        b = await q.json() if q.can_read_body else {}
        return J(await _room(q, b).cycle(reason="asked"))

    @r.get("/api/comfort/check")
    async def comfort_check(q):
        """Which of a room's configured entities Home Assistant doesn't have (#29)."""
        c = _room(q)
        states = {s["entity_id"] for s in (await A.engine._get("states") or [])}
        want = {**{f"room.{k}": v for k, v in c.room.items()}, **{f"signal.{k}": v for k, v in c.signals.items()},
                **{f"helper.{k}": v for k, v in c.vars.items()}}
        return J({"room": c.name, "person": c.person, "missing": {k: v for k, v in want.items() if v not in states},
                  "found": sum(1 for v in want.values() if v in states), "of": len(want)})

    # ---- controls for Home Assistant (the bridge makes switches, numbers, times, buttons and sensors of these) -------
    @r.get("/api/control")
    async def control_get(_):
        return J(A.control.describe())

    @r.post("/api/control/set")
    async def control_set(q):
        b = await q.json()
        return J(A.control.set(b["key"], b["value"]))

    @r.post("/api/control/reset")
    async def control_reset(q):
        b = await q.json() if q.can_read_body else {}
        return J(A.control.reset(b.get("key")))

    @r.post("/api/control/run/{action}")
    async def control_run(q):
        res = A.control.run(q.match_info["action"])
        return J(dict(res, control=A.control.describe()))

    @r.post("/mcp")
    async def mcp_post(q):
        try:
            msg = await q.json()
        except ValueError:
            return J({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, 400)
        out = await A.mcp.handle(msg, scope=q.get("scope", "admin"))
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
