"""Home Mind server: web UI (through HA ingress), REST API, MCP endpoint, and background upkeep."""
import asyncio
import json
import logging
import math
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
from .approval import Approvals
from .automations import Engine
from .journal import Journal
from .calendars import Calendars
from .morning import Morning
from .evening import Evening
from .overnight import ConvLog, Overnight
from .pantry import Pantry
from .fitness import Fitness
from .phrasebook import Phrasebook
from .persona import Persona
from . import nutrition
from .patterns import FACTOR, Patterns
from .knowledge import Knowledge
from .ha import HA
from .mcp import MCP
from .mind import Mind
from .models import ModelError, Models
from .suites import SUITE_INFO
from .updater import Updater
from .usage import Usage
from . import models as models_mod
from .version import VERSION

DATA = os.environ.get("HOME_MIND_DATA", "/data")


_NOUNS = {"light": "lights", "fan": "fans", "cover": "blinds", "switch": "switches", "lock": "locks",
          "media_player": "speakers", "humidifier": "humidifiers", "climate": "heaters"}   # "6 lights off"


def meal_time(when, now=None):
    """'8am', '8:30', '7:45 pm' or an ISO time -> a timestamp today (yesterday if that's still in the future);
    None for nothing or something unreadable."""
    if not when:
        return None
    now = now or datetime.now()
    w = str(when).strip().lower().replace(".", "")
    try:
        return datetime.fromisoformat(w).timestamp()
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", w)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    t = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if t > now:
        t -= timedelta(days=1)
    return t.timestamp()
PORT = int(os.environ.get("HOME_MIND_PORT", "8099"))
STOP_GRACE = 30                 # config.yaml timeout: seconds between SIGTERM and SIGKILL
REQUESTS_GRACE, TASKS_GRACE = 10, 10
INGRESS_IPS = {"172.30.32.2"}
# events that carry what someone asked or wrote: not for a token tied to another person (#48)
PRIVATE_EVENTS = {"ask", "recall", "mdsync_edit", "forget_about", "feedback", "email", "revise", "remember", "browse"}
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
            "paperless_url": "", "paperless_token": "", "rerank_model": "", "librarian_model": "", "archivist_model": "", "control_model": "", "trainer_url": "", "trainer_token": "", "rule_planner": True,
            "markdown_folder": "", "markdown_minutes": 10, "compare_models": [], "compare_days": 0,
            "compare_suites": ["recall", "tricky", "hard"], "use_preferences": True, "label_url": "",
            "telegram_token": "", "telegram_chats": [], "web_search": "off", "web_search_url": "", "web_search_key": "",
            "email_host": "", "email_user": "", "email_password": "", "email_folder": "INBOX", "email_senders": [],
            "email_minutes": 10,
            "answer_edges": False, "answer_notes_first": False,
            "events_keep_days": 90, "quiet_hours": True, "quiet_start": "22:00", "quiet_end": "07:00",
            "notify_daily_cap": 0, "checkin_old_facts": True, "checkin_after_days": 180,
            "overnight_structure": False, "phrasebook_grow": True, "persona_model": "", "away_alert_hours": 3, "router": True, "router_local": [], "router_cloud": [], "db_snapshots": 7,
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
        from . import dbguard
        self.db_report = dbguard.guard(os.path.join(DATA, "mind.db"))   # a damaged database: the newest good snapshot
        self.mind = Mind(DATA, self.models)
        self.usage = Usage(self.mind.store, {m["name"]: (float(m["price_in"]), float(m["price_out"]))
                                             for m in (self.opts.get("models") or [])
                                             if m.get("price_in") is not None and m.get("price_out") is not None})
        models_mod.USAGE = self.usage.record
        self.updater = Updater(self.mind.store, self.opts.get("update_url"))
        self.decisions = None       # the decision log (decisions.py), started with the house in setup
        self.measure = None         # scorecard, shadow, labelling, burden (measure.py), started with the log
        self.mcp = MCP(self)
        self.ha = None
        self.comfort = None
        self.bridge = None
        self.control = None
        self.started = time.time()
        self.tasks = []
        self._bg = {}

    def status(self):
        return {"version": VERSION, "uptime_s": int(time.time() - self.started), **self.mind.store.stats(),
                "packs": self.updater.installed(), "models": [m["name"] for m in self.models.list()],
                "bridge": self.bridge, "in_home_assistant": bool(self.ha and self.ha.available),
                "time": __import__("homemind.tz", fromlist=["status"]).status(),
                "database": dict(__import__("homemind.dbguard", fromlist=["status"]).status(self.mind.store.path),
                                 restored=self.db_report and {k: v for k, v in self.db_report.items() if k != "told"}),
                "updates": self.updater.last and {k: v for k, v in self.updater.last.items() if k != "manifest"}}

    # ---- lifecycle --------------------------------------------------------------------------------------------
    async def on_start(self, _app):
        await self.models.start()
        self.mind.rerank_model = self.opts.get("rerank_model") or None
        self.mind.librarian_model = self.opts.get("librarian_model") or None
        if self.opts.get("embed_model"):
            from .embeddings import Embeddings
            try:
                self.mind.embeddings = Embeddings(self.mind.store, self.models.get(self.opts["embed_model"]))
            except Exception as e:  # noqa: BLE001 - a missing model name: keyword search only
                log.warning("embed_model: %s", e)
        self.ha = HA(self.models.session)
        self.models.on_probe = self.away_alert
        from . import tz
        tz.apply(os.environ.get("TZ"))
        if self.ha.available:
            try:        # Home Assistant's own time zone wins: "today" and "last night" end when the house's day does
                conf = await asyncio.wait_for(self.ha.get_json("config"), 10)
                tz.apply((conf or {}).get("time_zone"))
            except Exception as e:  # noqa: BLE001
                log.warning("time zone from Home Assistant: %s", e)
        st = tz.status()
        (log.info if st["ok"] else log.warning)("time zone %s (UTC%s)%s", st["time_zone"], st["utc_offset"],
                                                f": {st['problem']}" if st["problem"] else "")
        self.ha.user_token = (self.opts.get("ha_user_token") or "").strip()
        self.ha.user_url = (self.opts.get("ha_url") or "").strip() or "http://homeassistant:8123"
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
        from .prefs import Prefs
        from .photos import Photos
        self.prefs = Prefs(self.mind.store)
        self.photos = Photos(self.mind.store, os.path.join(DATA, "photos"))
        self.mind.store.on_purge = self.photos.sweep      # a purged note's photos go with it
        from .weblook import Web
        from .messaging import Telegram
        from .mailbox import Mailbox
        self.mind.web = Web(self.opts, lambda: self.models.session)
        self.telegram = Telegram(self, self.opts.get("telegram_token"), self.opts.get("telegram_chats"))
        self.mailbox = Mailbox(self, self.opts)
        self.compare = Compare(self.mind, self.opts.get("compare_models") or [], self.opts.get("compare_days") or 0,
                               self.opts.get("compare_suites") or None)
        self.comforts = self.comfort_rooms()
        self.comfort = self.comforts[0]
        self.body = Body(self.mind.store, self.ha)
        self.home = Home(self.mind.store, self.ha, self.opts)
        self.mind.home = self.home if self.opts.get("home", True) else None
        if self.opts.get("use_preferences", True) is not False:
            self.home.prefs = self.prefs
        self.engine = Engine(self.mind.store, self.ha, DATA)
        self._start_decisions()
        self.approvals = Approvals()
        from .modes import Modes, Warm
        from .nowboard import NowBoard
        from .worsening import Worsening
        self.warm = Warm(self.models)
        self.modes = Modes(self, self.mind.store, self.warm)
        self.board = NowBoard(self.mind.store)
        self.mind.board = self.board
        self.worsening = Worsening(self.mind.store)
        from .learned import Learned
        em = self.opts.get("embed_model") or ""
        self.learned = Learned(self.mind.store, self.home, embed=(
            (lambda texts: self.models.get(em).embed(texts)) if em else None))
        from .learned_answers import LearnedAnswers
        from .cases import Cases
        self.learned_answers = LearnedAnswers(self.mind.store, cases=Cases(self.mind.store), embed=(
            (lambda texts: self.models.get(em).embed(texts)) if em else None))
        self.mind.learned_answers = self.learned_answers
        # phase 9: the forget list, the outbox, weekly saves of learned records
        from .upkeep import ForgetList, LearnedHistory
        from .outbox import Outbox
        st9 = self.mind.store
        self.forget = ForgetList(st9, DATA)
        def _forgotten(rows, vals):
            from .upkeep import scrub
            self.forget.record_notes(rows, vals)
            scrub(st9, [r["id"] for r in rows if "id" in r.keys()] if rows and hasattr(rows[0], "keys") else
                  [r.get("id") for r in rows or []], vals)
        st9.on_forget = _forgotten
        st9.on_forget_name = self.forget.record_name
        self.ha.outbox = Outbox(st9)
        self.learned_history = LearnedHistory(st9)
        self.learned.history = self.learned_answers.history = self.learned_history
        # phase 10: limits per channel, the spend cap, consent on a clear day
        from .channels import Limits, over_cap, priced
        from .consent import Consent
        self.limits = Limits()
        self.mind.blocked_models = lambda: (priced(self.usage) if over_cap(self.usage, self.opts.get("monthly_spend_cap"))
                                            else set())
        self.consent = Consent(st9, getattr(self, "day", None))
        self.consent.appliers["automation_on"] = self._consent_automation_on
        # phase 11: the room model (in memory only), backing off, the surprises queue
        from .rooms import RoomModel, BackOff, Surprises
        self.rooms = RoomModel(self.home)
        self.backoff = BackOff(st9)
        # wired here: the decision log and the engine were set up earlier (_start_decisions), before back-off existed
        if getattr(self, "decisions", None) is not None:
            self.decisions.on_override = lambda eid: self.backoff.mark(eid, "a person undid what Home Mind did")
        if getattr(self, "engine", None) is not None:
            self.engine.backoff = self.backoff
        self.surprises = Surprises(st9)
        # phase 12: things, and scenes drafted from a description
        from .things import Things, Scenes
        self.things = Things(st9)
        self.mind.things = self.things
        self.scenes = Scenes(self)
        # phase 13: trackers, the medicines log, events, nudges, exertion zones, travel mode
        from .tracking import Tracking, Zones
        self.tracking = Tracking(st9, None, folder=DATA)
        self.zones = Zones(st9, self.opts, None, folder=DATA)
        self.ha.travelling = self.tracking.travelling
        # connected answers: the symptom log, your own known issues, the brief (connect.py)
        from .connect import Symptoms, KnownIssues, Brief
        self.symptoms = Symptoms(st9)
        self.known_issues = KnownIssues(st9)
        self.brief = Brief(self)
        from .memgraph import MemGraph, Archivist
        self.memgraph = MemGraph(st9)
        self.mind.memgraph = self.memgraph
        self.archivist = Archivist(
            self.memgraph, self.models,
            model_name=self.opts.get("archivist_model") or None,
            personal_rule=self.opts.get("personal_rule") or "first_person",
        )
        self.mind.archivist = self.archivist
        if self.archivist.model_name:
            self.tasks.append(self.archivist.start())
        from .talk import Talk
        self.talk = Talk(self)          # the panel's conversation: by voice or typing, with choices (talk.py)
        _forget_name = st9.on_forget_name

        def _forget_name_too(name):
            if _forget_name:
                _forget_name(name)
            self.symptoms.purge_owner(name)
            self.memgraph.purge_owner(name)
        st9.on_forget_name = _forget_name_too
        # phase 14: experiments and proposals
        from .experiments import Experiments
        from .proposals import Proposals
        self.experiments = Experiments(st9, None, None, self.tracking)
        self.proposals = Proposals(st9, self.surprises)
        self.consent.appliers["experiment_start"] = self.experiments.start
        self.tracking.on_new_medicine = lambda name: self.experiments.medicine_changed(name, date.today())
        self.mind.tiebreak = self.tiebreak
        from .day import Day
        self.day = Day(self.mind.store)
        from .checkins import Checkins
        self.checkins = Checkins(self.mind.store)    # check-ins by time, water and coffee (0.31)
        self.consent.day = self.day             # a clear day reads the check-in (consent.py)
        self.tracking.day = self.zones.day = self.day
        self.experiments.day = self.day
        self.engine.event_handlers.setdefault("mobile_app_notification_action", []).append(
            lambda d: self.on_nudge_action(d.get("action")))
        self.ha.day = self.day           # the day's ask budget and rough-day quiet (ha.push)
        from .day import AskBatch
        self.ha.batcher = AskBatch(self.mind.store)      # routine notices wait for the morning check-in
        self.engine.event_handlers.setdefault("mobile_app_notification_action", []).append(
            lambda d: self.on_day_tap(d.get("action")))
        self.home.on_tell = self.tell_after
        self.mind.on_red_flag = self.on_red_flag
        self.engine.event_handlers.setdefault("mobile_app_notification_action", []).append(
            lambda d: self.on_approval(d.get("action")))
        self.mind.FALLBACK = list(self.opts.get("fallback") or [])
        self.mind.CONTROL_MODEL = self.opts.get("control_model") or None
        self.mind.ROUTER = self.opts.get("router", True) is not False
        self.mind.ROUTE_LOCAL = list(self.opts.get("router_local") or [])
        self.mind.ROUTE_CLOUD = list(self.opts.get("router_cloud") or [])
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
        if self.mind.store.kv_get("structure_unread_system") != 1:
            from . import structure
            n = structure.unread_system(self.mind.store)
            self.mind.store.kv_set("structure_unread_system", 1)
            if n:
                log.info("structure taken off %d notes Home Mind wrote itself", n)
        self._fix_kg_backfill()
        self.convlog = ConvLog(self.mind.store)
        self.mind.convlog = self.convlog
        self.overnight = Overnight(self.mind.store, self.models, self.convlog, self.opts.get("overnight_time") or "03:00",
                                   self.opts.get("overnight_model") or None)
        self.overnight.enabled = self.opts.get("overnight_enabled", True) is not False
        self.overnight.structure_on = bool(self.opts.get("overnight_structure"))
        self.overnight.memgraph = self.memgraph
        self.overnight.live_archivist = bool(self.archivist.model_name)
        self.overnight.personal_rule = self.opts.get("personal_rule") or "first_person"
        self.morning = Morning(self.mind.store, self.ha, self.calendars, self.opts.get("morning_time") or "07:30",
                               self.opts.get("morning_notify") or "")
        self.morning.enabled = self.opts.get("morning_enabled", True) is not False
        self.morning.day = getattr(self, "day", None)
        self.morning.batch = getattr(self.ha, "batcher", None)
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
        self.pantry.memgraph = self.memgraph
        self.pantry.calendars = self.calendars
        self.mind.pantry = self.pantry
        self.knowledge = Knowledge(DATA, self.opts)
        self.mind.knowledge = self.knowledge
        self.pantry.knowledge = self.knowledge
        self.morning.pantry = self.pantry
        self.patterns = Patterns(self.mind.store, self.ha, self.calendars, self.opts, phone=self.morning._phone)
        self.fitness = Fitness(self.mind.store, self.patterns, self.pantry)
        self.mind.patterns = self.patterns
        _ppl = self.people() or []
        mg_owner = _ppl[0]["name"] if len(_ppl) == 1 else None
        self.home.memgraph = self.memgraph
        self.patterns.memgraph = self.memgraph
        self.patterns.memgraph_owner = mg_owner
        self.fitness.memgraph = self.memgraph
        self.overnight.patterns = self.patterns
        self.overnight.memgraph_owner = mg_owner
        from .trainer import Trainer
        self.trainer = Trainer(self, self.opts)
        try:
            synced = self.memgraph.sync_patterns(self.patterns, days=60, owner=mg_owner)
            if synced.get("ingested"):
                log.info("memgraph synced %d sensor readings from %d days", synced["ingested"], synced["days"])
        except Exception as e:  # noqa: BLE001
            log.warning("memgraph sensor sync: %s", e)
        if getattr(self, "experiments", None) is not None:
            self.experiments.patterns = self.patterns
        from .datafacts import DataFacts
        self.mind.datafacts = DataFacts(self.mind.store, self.fitness, self.patterns)
        from .health import Health
        self.mind.health = Health(self.patterns, getattr(self, "day", None), self.ha)   # "how's my health today?"
        self.persona = Persona(self.mind.store)
        for brief in (self.morning, self.evening):
            brief.persona, brief.tired, brief.people = self.persona, self.tired, self.people
        self.phrasebook = Phrasebook(self.mind.store)
        self.mind.phrasebook = self.overnight.phrasebook = self.phrasebook
        self.overnight.grow_phrasebook = self.opts.get("phrasebook_grow", True) is not False
        self.mind.fitness = self.morning.fitness = self.evening.fitness = self.fitness
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
        # started after 23:30: the journal's tick summarizes today unless a summary for today is already filed
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
        if self.telegram.on:
            self.tasks.append(asyncio.create_task(self.telegram.run_forever()))      # #52
        if self.ha.available:
            self.tasks.append(asyncio.create_task(self.engine.run_forever()))
        if getattr(self, "warm", None) is not None:
            self.tasks.append(asyncio.create_task(self.warm.run_forever(
                lambda: [self.modes.local_planner(), self.modes.local_chat()])))
        log.info("Home Mind %s up on port %s (bridge: %s)", VERSION, PORT, self.bridge)

    async def on_stop(self, _app):
        live = self.tasks + list(self._bg.values())
        for t in live:
            t.cancel()
        if live:
            # let them finish before the session closes (never hang). Home Assistant kills the add-on STOP_GRACE
            # seconds after SIGTERM (config.yaml timeout), so the whole shutdown has to fit inside it.
            await asyncio.wait(live, timeout=TASKS_GRACE)
        await self.models.stop()

    def _start_decisions(self):
        """The decision log (decisions.py, its own file log.db): every action, refused ones too, with what should
        happen; outcomes are checked from the engine's event stream and by the upkeep loop's sweep."""
        from .decisions import DecisionLog
        try:
            self.decisions = DecisionLog(os.path.join(DATA, "log.db"))
        except Exception:  # noqa: BLE001 - without the log the house still works
            log.exception("decision log not started")
            self.decisions = None
        if self.decisions is not None and getattr(self, "backoff", None) is not None:
            self.decisions.on_override = lambda eid: self.backoff.mark(eid, "a person undid what Home Mind did")
        if getattr(self, "backoff", None) is not None:
            self.engine.backoff = self.backoff
        self.home.call_service = self._call_service
        self.home.live = lambda e: self.engine.states.get(e) if getattr(self.engine, "_ws", None) else None
        if self.decisions is None:
            return
        self.home.decisions = self.decisions
        self.engine.decisions = self.decisions
        self.ha.on_push = lambda tier: self.decisions.count("asks") if tier != "safety" else None
        from .measure import Measure
        rate = self.opts.get("shadow_rate")
        self.measure = Measure(self, rate=0.1 if rate in (None, "") else rate)
        self.engine.state_listeners.append(self.decisions.on_state)

    async def _call_service(self, domain, service, data):
        """Service calls for the decision log: over the websocket when it is connected (its answer always carries
        the call's context id), else REST (whose answer carries it when a state changed)."""
        from .ha import guard
        guard(domain, service, data, approved=True)      # Home.act's checks (and any phone approval) have passed
        if getattr(self.engine, "_ws", None) and not self.ha.user_token:
            try:
                res = await self.engine.ws_call({"type": "call_service", "domain": domain, "service": service,
                                                 "service_data": data})
            except RuntimeError as e:
                if "not connected" not in str(e):
                    return 400, None, None          # Home Assistant refused it (act() reports the failure)
            else:
                return 200, ((res or {}).get("context") or {}).get("id"), None
        return await self.ha.call_ctx(domain, service, data, approved=True)

    async def _state_for_check(self, eid):
        s = self.engine.states.get(eid) if getattr(self.engine, "_ws", None) else None
        return s if s is not None else await self.ha.state(eid)

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

    HANDOFF_SECONDS = 6          # by voice, an answer not ready by then goes to the person's phone instead
    HANDOFF_LATE = 60            # ... and is given this long to finish
    # a lock, alarm or garage door asked for in words waits for a spoken "yes" in the same conversation for this long
    PENDING_SECONDS = 30
    YES = re.compile(r"^\s*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|confirm(?:ed)?|do it|go ahead|please do|affirmative)\b"
                     r"(?![\s,.!]*(?:but|wait|no|don'?t|hold)\b)", re.I)
    NO = re.compile(r"^\s*(?:no|nope|don'?t|do not|stop|cancel|never ?mind|nevermind|forget it|wait|hold on|abort)\b",
                    re.I)

    def has_pending(self, conversation_id):
        return bool(conversation_id) and conversation_id in (self.__dict__.get("_pending") or {})

    async def pending_reply(self, conversation_id, text):
        """The answer to "Do you want me to open the garage door? Say yes": a yes within PENDING_SECONDS does it, a
        no drops it, and anything else drops it and is handled as a new request (None)."""
        p = (self.__dict__.get("_pending") or {}).pop(conversation_id, None) if conversation_id else None
        if not p:
            return None
        said = (text or "").replace("’", "'")
        if time.time() - p["t"] > self.PENDING_SECONDS:
            if self.YES.match(said):
                return {"answer": f"That was too long ago, so I didn't {p['verb']} the {p['name']}. Ask me again.",
                        "expired": True}
            return None
        if self.YES.match(said):
            plans = p.get("plans") or [p["plan"]]
            whys = p.get("whys") or [p.get("why")] * len(plans)
            done, errs = [], []
            batch = f"yes:{time.time()}"
            for plan, why in zip(plans, whys):
                try:
                    done.append(await self.home.act(plan["entity_id"], plan.get("action"), plan.get("value"),
                                                    p["source"], confirm=True, batch=batch,
                                                    why=dict(why or {}, confirmed=True)))
                except (ValueError, PermissionError) as e:
                    errs.append(str(e))
            if errs and not done:
                return {"plan": plans[0], "error": "; ".join(errs),
                        "answer": f"I couldn't {p['verb']} the {p['name']}: {errs[0]}"}
            r = {"plan": plans[0], "done": done[0] if len(plans) == 1 else done, "confirmed": True,
                 "answer": f"OK, I'll {p['verb']} the {p['name']}."}
            if errs:
                r["error"] = "; ".join(errs)
            return r
        if self.NO.match(said):
            return {"answer": f"OK, I won't {p['verb']} the {p['name']}.", "cancelled": True}
        return None

    async def do(self, text, model=None, source="api", conversation_id=None, deadline=None, area=None):
        """A request in words ("dim the desk light to 30%") -> one checked action, planned by a model. A lock, alarm
        or garage door is never moved on words alone: with a conversation_id it asks, and a "yes" in the same
        conversation within PENDING_SECONDS does it (pending_reply). deadline (seconds; voice): planning that takes
        longer does nothing, so a voice request that already timed out never moves the house afterwards."""
        r = await self.pending_reply(conversation_id, text)
        if r is not None:
            return r
        from . import redflags
        flag = redflags.check(text)
        if flag:
            hook = getattr(self, "on_red_flag", None)
            if hook:
                try:
                    await hook(flag, text, None, conversation_id, source)
                except Exception:  # noqa: BLE001
                    log.exception("red flag notice")
            return {"answer": flag.reply, "red_flag": flag.kind}
        c = self.checkin_reply(text) if hasattr(self, "checkin_reply") else None
        if c:
            return c
        from . import conversation as conv
        if conv.UNDO.match(text or "") and getattr(self, "decisions", None) is not None:
            return await conv.undo(self, conversation_id, source)
        if getattr(self, "decisions", None) is not None:
            w = conv.why(self, text)
            if w:
                return w
        task = conv.help_request(text)
        if task and getattr(self, "mind", None) is not None:
            r = await self.mind.ask(conv.HELP_PROMPT + task, voice=bool(deadline))
            return dict(r, help=True)
        t0 = time.monotonic()
        if not self.home.catalog:
            await self.home.refresh()
        opts = getattr(self, "opts", None) or {}
        if not model and opts.get("rule_planner", True) is not False:
            # plain commands need no model at all: "kitchen light off", "den lamp to 40%", "lights off and lock up"
            from . import planner
            room = self.room_for(text, area) if area else None
            if room:
                # said in a room without naming one: "lights off" means this room's lights, if that's clear
                sub = {e: c for e, c in self.home.catalog.items() if (self.home.areas or {}).get(e) == room}
                steps = planner.plan(text, sub, self.home.allowed) if sub else None
                g = re.search(r"\bthe (light|lamp|fan|blinds?|shades?|curtains?)\b", text, re.I)
                if sub and g and not (steps and all(s.get("entity_id") for s in steps)):
                    # "the light" in a room with one light: that one, by its name
                    dom = {"light": "light", "lamp": "light", "fan": "fan"}.get(g.group(1).lower(), "cover")
                    one = [e for e in sub if e.split(".")[0] == dom]
                    if len(one) == 1:
                        named = text[:g.start()] + (sub[one[0]].get("name") or one[0]) + text[g.end():]
                        steps = planner.plan(named, sub, self.home.allowed)
                if steps and all(s.get("entity_id") for s in steps):
                    res = dict(await self._run_steps(steps, text, source, conversation_id, "rules", t0=t0), room=room)
                    getattr(self, "_measure_do", lambda *a: None)(res, text, source, t0)
                    return res
            steps = planner.plan(text, self.home.catalog, self.home.allowed)
            if steps:
                res = await self._run_steps(steps, text, source, conversation_id, "rules", t0=t0)
                getattr(self, "_measure_do", lambda *a: None)(res, text, source, t0)
                return res
        lr = getattr(self, "learned", None)
        if lr is not None and not model:
            m = await lr.reflex(text, self.tiebreak if hasattr(self, "tiebreak") else None)
            if m and not m.get("audit"):
                # a trusted template: no model call (it still goes through every check in _run_plan / Home.act)
                plan = dict(m["plan"], confidence=m["sim"])
                res = await self._run_plan(plan, text, source, conversation_id, t0=t0)
                getattr(self, "_measure_do", lambda *a: None)(res, text, source, t0)
                return res
            if m and m.get("audit"):
                # about 1 in 20: planned the usual way instead, and the template is judged against it
                plan = await self.plan_only(text, reflex=False)
                from .replay import same_action
                ok = bool(plan.get("entity_id")) and same_action(plan, m["plan"])
                lr.evidence(m["record"], ok, "" if ok else "an audit disagreed")
                res = await self._run_plan(dict(plan, audit_of=m["record"]["id"]), text, source, conversation_id, t0=t0)
                getattr(self, "_measure_do", lambda *a: None)(res, text, source, t0)
                return res
        if getattr(self, "modes", None) is not None and not model:
            return await self._do_by_mode(text, source, conversation_id, deadline, t0)
        names = [n for n in self.models.models if n != "mock"]
        local = opts.get("control_model") or ""
        things = self.home.controllable()
        if local and local in self.models.models and not model:
            # the small local model first (localctl): one tool call, resolved against the control list
            r = await self.local_plan(local, text, deadline)
            if r.get("entity_id") or r.get("which") or r.get("answer"):
                plan = r
                if r.get("which"):
                    return {"plan": r, "answer": "Which one: " + ", ".join(r["which"]) + "?", "which": r["which"]}
                if r.get("answer"):
                    return r
                res = await self._run_plan(plan, text, source, conversation_id, t0=t0)
                getattr(self, "_measure_do", lambda *a: None)(res, text, source, t0)
                return res
            names = [n for n in names if n != local]          # not a command for it: the general model decides
        model = model or ("haiku" if "haiku" in names else names[0] if names else None)
        if not model:
            return {"error": "no model connected"}
        try:
            coro = plan_action(self.models.get(model), text, things)
            plan = await (asyncio.wait_for(coro, deadline) if deadline else coro)
        except asyncio.TimeoutError:
            return {"error": "the model took too long; nothing was done",
                    "answer": "Sorry, that took too long, so I didn't do anything."}
        plan.setdefault("by", model)
        if plan.get("actions"):
            res = await self._run_steps(plan["actions"], text, source, conversation_id, model, t0=t0)
        else:
            res = await self._run_plan(plan, text, source, conversation_id, t0=t0)
        getattr(self, "_measure_do", lambda *a: None)(res, text, source, t0)
        return res

    async def _do_by_mode(self, text, source, conversation_id, deadline, t0):
        """Planning by model mode (modes.py): the local model for the kinds it has earned, the cloud model for the
        rest (the local plan kept beside it as a comparison), private = local only. Then confidence: below the line,
        the cloud model gets a say, else Home Mind asks back."""
        from .modes import confidence, kind_of
        modes = self.modes
        mode = modes.mode(None)
        local, cloud = modes.local_planner(), modes.cloud()
        local_ok = bool(local) and modes.is_warm(local)
        if mode == "private" and not local_ok:
            return {"error": "private mode, and the local model isn't awake",
                    "answer": "In private mode I only use the computer at home, and it's asleep right now, so I "
                              "didn't do anything."}

        async def by_cloud():
            coro = plan_action(self.models.get(cloud), text, self.home.controllable())
            p = await (asyncio.wait_for(coro, deadline) if deadline else coro)
            p.setdefault("by", cloud)
            return p
        lp = {}
        if local_ok and mode != "cloud":
            lp = await self.local_plan(local, text, deadline)
            if lp:
                modes.warm.saw(local) if modes.warm else None
            if lp.get("which"):
                return {"plan": lp, "answer": "Which one: " + ", ".join(lp["which"]) + "?", "which": lp["which"]}
            if lp.get("answer"):
                return lp
        try:
            if lp.get("entity_id") and (mode == "private" or not cloud or modes.earned(kind_of(lp))):
                plan, other = lp, None
            elif cloud and mode != "private":
                plan, other = await by_cloud(), (lp if lp.get("entity_id") else None)
            else:
                return {"error": "nothing on the control list fits", "plan": lp}
            if plan.get("actions"):
                # the cloud model planned several actions ("lights off and lock up"): each one goes through
                # _run_plan's checks and the safety ladder, as the rules' several actions do
                res = await self._run_steps(plan["actions"], text, source, conversation_id, plan.get("by"), t0=t0)
                getattr(self, "_measure_do", lambda *a: None)(res, text, source, t0)
                return res
            conf, why = confidence(self.home, text, plan, getattr(self, "decisions", None))
            if plan.get("entity_id") and conf < modes.dial() and plan.get("by") == local and cloud and \
                    mode != "private":
                other, plan = plan, await by_cloud()        # unsure on the local model: the cloud model decides
                conf, why = confidence(self.home, text, plan, getattr(self, "decisions", None))
        except asyncio.TimeoutError:
            return {"error": "the model took too long; nothing was done",
                    "answer": "Sorry, that took too long, so I didn't do anything."}
        plan["confidence"] = conf
        if plan.get("entity_id") and conf < modes.dial():
            plan["unsure"], plan["unsure_why"] = True, why
        if other is not None:
            plan["compared"] = True
        res = await self._run_plan(plan, text, source, conversation_id, t0=t0)
        m = getattr(self, "measure", None)
        if other is not None and m is not None:
            m.record_pair((res.get("done") or {}).get("decision_id"), plan, other)
        getattr(self, "_measure_do", lambda *a: None)(res, text, source, t0)
        return res

    async def tiebreak(self, text, answers):
        """Step 12: two learned answers too close to call. The small local model picks one (0 or 1), or None. Only
        when it is warm, so the voice never waits on a sleeping laptop."""
        from .learned_answers import tie_prompt, tie_pick
        modes = getattr(self, "modes", None)
        name = (modes.local_planner() or modes.local_chat()) if modes is not None else None
        if not name or (modes is not None and not modes.is_warm(name)):
            return None
        r = await asyncio.wait_for(self.models.get(name).chat("", [{"role": "user", "content": tie_prompt(text, answers)}],
                                                             max_tokens=4, temperature=0), 2.0)
        return tie_pick((r or {}).get("text"))

    async def plan_only(self, text, model=None, reflex=True, include=()):
        """What do() would plan for these words, without acting (replay), in the order do() uses: the rules (option
        rule_planner), a trusted template (and, for the nightly gate, provisional ones in `include`), then the local
        control model when set, then the general model."""
        if not self.home.catalog:
            await self.home.refresh()
        opts = getattr(self, "opts", None) or {}
        if not model and opts.get("rule_planner", True) is not False:
            from . import planner
            steps = planner.plan(text, self.home.catalog, self.home.allowed)
            if steps:
                steps = [dict(s, by="rules") for s in steps]
                return steps[0] if len(steps) == 1 else {"actions": steps, "by": "rules"}
        lr = getattr(self, "learned", None)
        if lr is not None and reflex and not model:
            from .learned import REFLEX_SIM, REFLEX_MARGIN
            m = await lr.match(text, statuses=("trusted", "provisional"))
            if m and m["named"] and m["sim"] >= REFLEX_SIM and m["margin"] >= REFLEX_MARGIN and \
                    (m["record"]["status"] == "trusted" or m["record"]["id"] in include):
                return m["plan"]
        names = [n for n in self.models.models if n != "mock"]
        local = opts.get("control_model") or ""
        if local and local in self.models.models and not model:
            r = await self.local_plan(local, text)
            if r.get("entity_id") or r.get("which") or r.get("answer"):
                return r
            names = [n for n in names if n != local]
        model = model or ("haiku" if "haiku" in names else names[0] if names else None)
        if not model:
            raise RuntimeError("no model connected")
        plan = await plan_action(self.models.get(model), text, self.home.controllable())
        plan.setdefault("by", model)
        return plan

    def room_for(self, text, area):
        """The room to keep a command to: where it was said, unless the words name a room (or the whole house)."""
        if not area or not getattr(self.home, "areas", None):
            return None
        rooms = set(self.home.areas.values())
        t = " " + re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()) + " "
        if any(f" {r} " in t for r in rooms) or re.search(r"\b(?:all|every|everywhere|whole house|house|downstairs|"
                                                         r"upstairs|outside)\b", t):
            return None
        a = area.lower()
        return a if a in rooms else None

    def _summed(self, plans):
        """What was done, short enough to say: three or more of a kind with the same action become one phrase
        ("6 lights off"); one or two keep their names ("Desk Light off, kettle on")."""
        groups = {}
        for p in plans:
            k = (p["entity_id"].split(".")[0], p.get("action"), p.get("value"))
            groups.setdefault(k, []).append(p)
        out = []
        for (dom, _a, _v), ps in groups.items():
            if len(ps) >= 3:
                what = self._said(ps[0])[len((self.home.catalog.get(ps[0]["entity_id"]) or {}).get("name")
                                             or ps[0]["entity_id"]):].strip()
                out.append(f"{len(ps)} {_NOUNS.get(dom, dom.replace('_', ' ') + 's')} {what}")
            else:
                out.extend(self._said(p) for p in ps)
        return ", ".join(out)

    def _said(self, plan):
        name = (self.home.catalog.get(plan.get("entity_id")) or {}).get("name") or plan.get("entity_id")
        a, v = plan.get("action"), plan.get("value")
        what = {"on": "on", "off": "off", "toggle": "switched", "run": "started", "open": "opened", "close": "closed",
                "lock": "locked", "unlock": "unlocked"}.get(a)
        if what is None:
            what = f"{v}%" if a in ("brightness", "speed", "humidity") and v is not None else f"{a} {v}".strip()
        return f"{name} {what}"

    async def _run_steps(self, steps, text, source, conversation_id, by, t0=None):
        """Several actions from one request ("lights off and lock up"). A "which one?" anywhere asks first and does
        nothing. The ordinary ones run; locks, alarms and garage doors among them wait for one yes together."""
        ask = next((s for s in steps if s.get("which")), None)
        if ask:
            return {"steps": steps, "by": by, "which": ask["which"],
                    "answer": f"Which {ask.get('device') or 'one'}: " + ", ".join(ask["which"]) + "?"}
        steps = [dict(s, by=by) for s in steps if s.get("entity_id")]
        # a light group ("Bedroom Lights") already covers its members: switch the group, not each member as well
        covered = set()
        for s in steps:
            members = ((self.home.catalog.get(s["entity_id"]) or {}).get("attrs") or {}).get("entity_id")
            if isinstance(members, list):
                covered |= {(m, s.get("action"), s.get("value")) for m in members if m != s["entity_id"]}
        if covered:
            steps = [s for s in steps if (s["entity_id"], s.get("action"), s.get("value")) not in covered] or steps
        if len(steps) == 1:
            return await self._run_plan(steps[0], text, source, conversation_id, t0=t0)
        def need(s):
            eid = s["entity_id"]
            if not (eid in self.home.catalog and self.home.allowed(eid)):
                return "open"
            if hasattr(self.home, "level"):          # the safety ladder: phone > confirm (a spoken yes) > open
                return self.home.level(eid, s.get("action"), s.get("value"))[0]
            return "confirm" if self.home.sensitive(eid) else "open"
        # the ones needing a spoken yes wait for one yes together; phone-level ones (locks, garage doors, heaters)
        # each go through _run_plan, which sends a phone approval; the rest run now
        sens = [s for s in steps if need(s) == "confirm"]
        results, said, ran = [], [], []
        batch = f"{time.time()}:{id(steps)}"         # one request: counts once against the actions-per-minute limit
        for s in steps:
            if s in sens:
                continue
            r = await self._run_plan(s, text, source, conversation_id, batch=batch, t0=t0)
            results.append(r)
            if r.get("done"):
                said.append(self._said(s))
                ran.append(s)
        errors = [r["error"] for r in results if r.get("error")]
        out = {"steps": steps, "by": by, "results": results, "done": [r["done"] for r in results if r.get("done")]}
        answer = ("Done: " + self._summed(ran) + ".") if said else ""
        sent = [r for r in results if r.get("approval_sent")]
        if sent:
            out["approvals"] = [r["approval_id"] for r in sent]
            names = [(self.home.catalog.get(r["plan"]["entity_id"]) or {}).get("name") or r["plan"]["entity_id"]
                     for r in sent]
            answer += (" " if answer else "") + f"I've sent a request to your phone for the {', '.join(names)}."
        if errors:
            out["error"] = "; ".join(errors)
            answer += (" " if answer else "") + f"{len(errors)} didn't work."
        if sens:
            names = [(self.home.catalog.get(s["entity_id"]) or {}).get("name") or s["entity_id"] for s in sens]
            verbs = sorted({s.get("action") or "change" for s in sens})
            what = f"{' and '.join(verbs)} the {', '.join(names)}"
            if conversation_id:
                pend = self.__dict__.setdefault("_pending", {})
                pend[conversation_id] = {"plans": sens, "plan": sens[0], "t": time.time(), "source": source,
                                         "name": ", ".join(names), "verb": " and ".join(verbs),
                                         "whys": [decision_why(getattr(self, "opts", None), s, text, source,
                                                               conversation_id, t0) for s in sens]}
                out["confirm_needed"] = True
                answer += (" " if answer else "") + f"Do you want me to {what}? Say yes to go ahead."
            else:
                out["error"] = (out.get("error", "") + "; " if out.get("error") else "") + \
                    f"{', '.join(names)} need the person's yes, and this request has no conversation_id"
                answer += (" " if answer else "") + f"I need a yes before I {what}, and I can't take one here."
        out["answer"] = answer.strip()
        return out

    async def _learned_score(self, include):
        """The house scorecard's rate with trusted templates, plus the provisional ones in `include`: the nightly
        gate's replay. Model plans are shared between the two runs, so they cost one call per request."""
        from . import replay as R
        m = self.measure
        ids = m.ensure_v1()
        if not ids:
            return None
        cache = self.__dict__.setdefault("_score_cache", {})

        from .learned import REFLEX_SIM, REFLEX_MARGIN

        async def planner(text):
            mm = await self.learned.match(text, statuses=("trusted", "provisional"))
            if mm and mm["named"] and mm["sim"] >= REFLEX_SIM and mm["margin"] >= REFLEX_MARGIN and \
                    (mm["record"]["status"] == "trusted" or mm["record"]["id"] in include):
                return mm["plan"]
            if text not in cache:
                cache[text] = await self.plan_only(text, reflex=False)
            return cache[text]
        cs = [c for c in m.cases(ids=ids) if c["kind"] != "device"]
        rep = await R.replay(self.decisions, planner, cases=cs)
        n = rep["match"] + rep["override"]
        if include == () and len(cache) > 500:
            cache.clear()
        return (rep["agree"] + rep["override"] - rep["repeats"]) / n if n else None

    async def shadow_plan(self, text, model):
        """A plan from one named model, for the shadow baseline (measure.py). Never acts."""
        local = (getattr(self, "opts", None) or {}).get("control_model") or ""
        if model == local:
            r = await self.local_plan(local, text)
            return {k: r.get(k) for k in ("entity_id", "action", "value")} if r else {}
        plan = await plan_action(self.models.get(model), text, self.home.controllable())
        return {k: plan.get(k) for k in ("entity_id", "action", "value")}

    async def local_plan(self, name, text, deadline=None):
        """The small local control model (option control_model; localctl): one tool call -> a plan, a "which one?",
        or {} when it isn't a single device action (then the general model decides). Never acts by itself."""
        from . import localctl
        catalog = {e: c for e, c in self.home.catalog.items()}
        try:
            coro = self.models.get(name).call_tool([{"role": "user", "content": text}], localctl.TOOLS,
                                                    system=localctl.SYSTEM)
            r = await (asyncio.wait_for(coro, deadline) if deadline else coro)
        except Exception as e:  # noqa: BLE001 - the general model is the fallback
            log.warning("control model %s: %s", name, e)
            return {}
        tool, args = localctl.parse_call(r)
        plan = localctl.to_plan(tool, args, catalog, self.home.allowed)
        plan["by"] = name
        if plan.get("not_a_command") or plan.get("log") or tool is None:
            return {}
        if not plan.get("entity_id") and not plan.get("which"):
            return {}          # a name it couldn't place: let the general model try with the whole list
        return plan

    async def _run_plan(self, plan, text, source, conversation_id, batch=None, t0=None):
        """The checks every planned action goes through, whichever model planned it."""
        why = decision_why(getattr(self, "opts", None), plan, text, source, conversation_id, t0)
        if not plan.get("entity_id"):
            try:
                self.mind.store.log("cant_do", None, request=text)        # the surprises queue (step 24)
            except Exception:  # noqa: BLE001
                pass
            return {"error": "nothing on the control list fits", "plan": plan}
        eid = plan["entity_id"]
        need = self.home.level(eid, plan.get("action"), plan.get("value"))[0] \
            if eid in self.home.catalog and self.home.allowed(eid) and hasattr(self.home, "level") else "open"
        if str(source or "").startswith("other:") and need != "open":
            # another assistant's speaker: open-level actions only; nothing that needs a yes or the phone
            if hasattr(self.home, "_log_decision"):
                self.home._log_decision(eid, plan.get("action"), plan.get("value"), source, why, status="refused",
                                        reason="another assistant: open-level actions only")
            return {"plan": plan, "error": "open-level actions only from another assistant",
                    "answer": "Please use Home Mind for that."}
        if need == "open" and plan.get("unsure") and eid in self.home.catalog:
            need = "confirm"            # low confidence (modes.confidence): ask back before acting
        if need == "phone":
            return await self.ask_phone(plan, why, source)
        if need == "confirm":
            name = self.home.catalog[eid].get("name") or eid
            a = (plan.get("action") or "").lower()
            verb = self._verb(plan) if hasattr(self, "_verb") else \
                {"on": "open", "off": "close", "toggle": "open or close"}.get(a, a or "change")
            if conversation_id:
                pend = self.__dict__.setdefault("_pending", {})
                for k in [k for k, v in pend.items() if time.time() - v["t"] > 10 * self.PENDING_SECONDS]:
                    pend.pop(k)
                pend[conversation_id] = {
                    "plan": plan, "t": time.time(), "source": source, "name": name, "verb": verb, "why": why}
                return {"plan": plan, "confirm_needed": True,
                        "answer": (f"Just to check: {verb} the {name}? Say yes to go ahead." if plan.get("unsure")
                                   else f"Do you want me to {verb} the {name}? Say yes to go ahead.")}
            if hasattr(self.home, "_log_decision"):
                self.home._log_decision(eid, plan.get("action"), plan.get("value"), source, why, status="refused",
                                        reason="needs a yes and there is no conversation to take one")
            return {"plan": plan, "error": f"{eid} is a lock or door: it needs the person's yes, and this request has "
                                           "no conversation_id to take one (or use mind_control with confirm: true)",
                    "answer": f"I need a yes before I {verb} the {name}, and I can't take one here. "
                              "Please use the Home Assistant app for that."}
        try:
            return {"plan": plan, "done": await self.home.act(plan["entity_id"], plan.get("action"), plan.get("value"),
                                                              source, batch=batch, why=why)}
        except (ValueError, PermissionError) as e:
            return {"plan": plan, "error": str(e)}

    async def answer(self, b, scope="admin"):
        """self question (or a remark) answered: check-in, undo and why first, a "yes" to a pending action, then the
        brain; by voice, privacy, the 6-second handoff and the check-in question. Shared by /api/ask and the voice
        agent (/api/converse). b: the request body ({"question", "conversation_id", "voice", "user"...})."""
        from .tokens import allows
        t_ask = time.monotonic()
        if not (b.get("question") or "").strip():
            raise ValueError("give a question")
        from . import frontends as FE
        oa = FE.other_assistant(b, self.opts)
        if oa:
            # another assistant's speaker: a guest. Open-level only, nothing private, safety replies to the phone
            from . import redflags as _rf
            if FE.assistants_off(self.mind.store):
                return {"answer": f"{oa['name']} is turned off for Home Mind.", "hits": [], "model": None,
                        "other": oa["name"]}
            flag = _rf.check(b["question"])
            if flag:
                hook = getattr(self, "on_red_flag", None)
                if hook is not None:
                    try:
                        await hook(flag, b["question"], None, b.get("conversation_id"), f"other:{oa['name']}")
                    except Exception:  # noqa: BLE001
                        log.exception("red flag via another assistant")
                # the fixed reply itself is said: it holds nothing private, and "check Home Mind" helps nobody in a
                # crisis or an emergency
                return {"answer": flag.reply, "hits": [], "model": None, "red_flag": flag.kind, "other": oa["name"]}
            if FE.other_refuses(b["question"]):
                return {"answer": FE.OTHER_ANSWER, "hits": [], "model": None, "other": oa["name"], "private": True}
            from .speakers import VIEWER as _VW
            _VW.set("")                      # it can't say who is speaking: a guest sees no personal notes
            b = dict(b, speaker="", front_end=f"other:{oa['name']}")
        who, how = self.speaker_for(b)
        from . import redflags, conversation as conv
        urgent = redflags.check(b["question"])       # before every shortcut: "where's my wife, I have chest pain"
        sy = getattr(self, "symptoms", None)
        if sy is not None and who != "" and not oa and allows(scope, "write"):
            try:        # "why am I so gassy lately?" also says: gassy, today (connect.py's symptom log)
                ki = getattr(self, "known_issues", None)
                sy.mention(b["question"], who or None, ki.of(who or None) if ki is not None else None)
            except Exception:  # noqa: BLE001 - a log line is never a reason to fail an answer
                log.exception("symptom log")
        fly = self.mind.store.kv_get("firefly") if not urgent and FE.MONEY_Q.search(b["question"]) else None
        if fly is not None:
            owner = (self.opts.get("firefly_owner") or "").lower()
            if not owner or (who or "").lower() != owner:     # its owner only (firefly_owner)
                return {"answer": "That's private.", "hits": [], "model": None, "private": True}
            said = FE.say_money(fly.get("totals"), fly.get("at") or 0)
            if said:                         # money is said by code from the totals: never through a model
                return {"answer": said, "hits": [], "model": None, "personal": True}
        rm = getattr(self, "rooms", None)
        if rm is not None:
            if who and b.get("area"):
                rm.heard_in(b["area"], who)          # the room model: who was last heard where (memory only)
            from .rooms import where_question, WHERE_ANSWER
            try:
                names = [p["name"] for p in self.people()]
            except Exception:  # noqa: BLE001
                names = []
            if not urgent and where_question(b["question"], names):
                return {"answer": WHERE_ANSWER, "hits": [], "model": None, "where_refused": True}
        if not urgent:
            tk = getattr(self, "tracking", None)
            if tk is not None and allows(scope, "write"):
                med = tk.med_said(b["question"], who or None)       # "took ibuprofen 400": the medicines log
                if med:
                    # "took my meds, feeling rough": the dose is logged, and the check-in in the same breath too
                    c = self.checkin_reply(b["question"], who or None) if hasattr(self, "checkin_reply") else None
                    if c:
                        med = dict(med, answer=f"{med['answer']} {c['answer']}", checkin=c["checkin"])
                    return dict(med, hits=[], model=None, personal=True)
                lg = tk.log_said(b["question"], who or None)        # "log headache", "fog 2": one word, timed
                if lg:
                    return {"answer": lg, "hits": [], "model": None, "logged": True, "personal": True}
            c = self.checkin_reply(b["question"], who or None) if hasattr(self, "checkin_reply") else None
            if c:
                return (dict(c, hits=[], model=None))
            if getattr(self, "decisions", None) is not None:
                if conv.UNDO.match(b["question"]) and allows(scope, "admin"):
                    return (dict(await conv.undo(self, b.get("conversation_id"), "voice" if b.get("voice") else "ask"),
                                  hits=[], model=None))
                w = conv.why(self, b["question"], who)
                if w:
                    return (dict(w, hits=[], model=None))
            task = conv.help_request(b["question"])
            if task:
                b["question"] = conv.HELP_PROMPT + task
        if self.has_pending(b.get("conversation_id")) and allows(scope, "admin"):
            # "yes" right after "Do you want me to open the garage door?" (asked by /api/do in this conversation)
            done = await self.pending_reply(b["conversation_id"], b["question"])
            if done is not None:
                return await self._style(dict(done, hits=[], model=None), b["question"], who, bool(b.get("voice")),
                                         "do")
        if not urgent:
            from . import ask_actions
            act = await ask_actions.dispatch(self, b, who, oa, scope)
            if act is not None:
                if b.get("conversation_id") and hasattr(self.mind, "_record_turn"):
                    self.mind._record_turn(b["conversation_id"], b["question"], act.get("answer") or "")
                return await self._style(act, b["question"], who, bool(b.get("voice")))
        phone = self.phone_of(who) if b.get("voice") and who and hasattr(self, "phone_of") else None
        ak = getattr(self, "automation_ok", None)
        ar = ak(b, who, oa, scope) if not urgent and ak is not None else None
        mk = getattr(self, "meal_ok", None)
        mr = mk(b, who, oa) if not ar and not urgent and mk is not None else None
        dk = getattr(self, "deep_ok", None)
        use_deep = bool(not ar and not mr and not urgent and dk is not None and dk(b, who, oa))
        raw_on_sent = b.get("on_sentence") if not ar and not mr and not use_deep else None
        stream_state = {"started": False, "handed_off": False}

        def on_sent(s):
            if stream_state["handed_off"]:
                return
            stream_state["started"] = True
            raw_on_sent(s)

        ask = self.mind.ask(b["question"], b.get("model"), use_brain=b.get("use_brain", True),
                         expand=bool(b.get("expand")), voice=bool(b.get("voice")),
                         deadline=(b.get("deadline") or self.HANDOFF_LATE) if phone else b.get("deadline"),
                         fallback=b.get("fallback", True), conversation_id=b.get("conversation_id"), speaker=who,
                         **({"on_sentence": on_sent} if raw_on_sent is not None else {}))
        if ar:
            ask = self.automation_answer(b, who, ar, ask)  # "set up an automation to...": autodraft.draft()
        elif mr:
            ask = self.meal_answer(b, who, mr, ask)  # "what can I make with the large eggs?": Pantry.suggest()
        elif use_deep:
            ask = self.deep_answer(b, who, ask)      # why / lately / how am I: the brief, connected (connect.py)
        if phone:
            # past HANDOFF_SECONDS by voice: say so, and send the whole answer to the person's phone when it's ready
            # (design step 19); without a phone the voice deadline and its best-note fallback stand as before
            task = asyncio.ensure_future(ask)
            done, _ = await asyncio.wait({task}, timeout=self.HANDOFF_SECONDS)
            if not done and not stream_state["started"]:
                stream_state["handed_off"] = True
                async def finish():
                    try:
                        r = await task
                        await self.ha.push(phone, {"title": "Home Mind", "message": r.get("answer") or
                                                "Sorry, I couldn't find an answer."}, tier="important", now=True)
                    except Exception:  # noqa: BLE001
                        log.exception("voice handoff")
                key = f"handoff-{id(task)}"
                self._bg[key] = asyncio.ensure_future(finish())
                self._bg[key].add_done_callback(lambda _t, k=key: self._bg.pop(k, None))
                return ({"answer": "That's taking a moment. I'll send it to your phone.", "handoff": phone,
                          "hits": [], "model": None})
            res = task.result() if done else await task
        else:
            res = await ask
        if who is not None:
            res["speaker_how"] = how
        if getattr(self, "decisions", None) is not None:
            try:
                fe = b.get("front_end") or ("voice" if b.get("voice") else "api")
                self.decisions.count("requests")
                self.decisions.timing("ask", (time.monotonic() - t_ask) * 1000, fe, res.get("model"),
                                   bool(res.get("fallback")) or res.get("model") in (self.mind.FALLBACK or []))
                if not res.get("answer") or res.get("weak"):
                    self.decisions.count("abstains")
            except Exception:  # noqa: BLE001
                log.exception("measure")
        if b.get("voice") and who and not res.get("red_flag") and hasattr(self, "private_by_voice"):
            res = await self.private_by_voice(res, who)
        if b.get("voice") and not res.get("red_flag") and not res.get("deep") and getattr(self, "day", None) is not None:
            from .day import QUESTION, DAY_STARTS
            # the check-in starts when the person first speaks, never as an alarm: once, after what they asked
            if self.day.get(who or None) == "unknown" and datetime.now().hour >= DAY_STARTS and \
                    not self.day.asked(who or None):
                res["answer"] = f"{(res.get('answer') or '').rstrip()} {QUESTION}".strip()
                res["checkin_asked"] = True
        res["hits"] = [{"id": h["id"], "title": h["title"], "shelf": h["shelf"], "score": h["score"]} for h in res["hits"]]
        return await self._style(res, b["question"], who, bool(b.get("voice")))

    DEEP_VOICE_SECONDS = 6.0

    def automation_ok(self, b, who, other=None, scope="admin"):
        """An automation-creation request ('set up an automation to...'): drafted & validated via autodraft."""
        from .tokens import allows
        from .autodraft import automation_request
        if other or who == "" or not allows(scope, "write") or b.get("use_brain") is False:
            return None
        return automation_request(b.get("question") or "")

    async def automation_answer(self, b, who, req_text, quick):
        """Draft & check a Home Assistant automation using real HA entities/services; falls back cleanly on error."""
        from .autodraft import draft_summary, format_draft
        voice = bool(b.get("voice"))
        chosen = b.get("model") if b.get("model") not in (None, "", "auto") else None
        md = getattr(self, "modes", None)
        try:
            private = md is not None and md.mode(who or None) == "private"
        except Exception:  # noqa: BLE001
            private = False
        if chosen and b.get("fallback") is False:
            chain = [chosen]
        else:
            chain = self._deep_chain(who) if hasattr(self, "_deep_chain") else []
            if chosen and chosen not in chain and not private:
                chain = [chosen] + chain
            if not chain and not private:
                chain = [x for x in (getattr(getattr(self, "models", None), "models", None) or {})
                         if x != "mock"][:2]
        chain = [m for m in chain if m and m != "mock"] or ([chosen] if chosen == "mock" else [])

        async def run():
            last_err = None
            for m in chain:
                try:
                    res = await self.draft_automation(req_text, model=m)
                    info = draft_summary(res)
                    text = format_draft(res, voice=voice)
                    return {"answer": text, "hits": [], "model": m,
                            "automation_draft": info, "via": "autodraft"}
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    log.warning("automation draft with %s: %s", m, e)
            if last_err is not None:
                return {"answer": f"I couldn't reach Home Assistant or a model to draft that automation: {str(last_err)[:180]}",
                        "hits": [], "model": None}
            return None

        try:
            res = await (asyncio.wait_for(run(), self.DEEP_VOICE_SECONDS) if voice else run())
        except asyncio.TimeoutError:
            res = None
        if res is None:
            return await quick
        quick.close()
        if b.get("conversation_id") and hasattr(self.mind, "_record_turn"):
            self.mind._record_turn(b["conversation_id"], b["question"], res["answer"])
        return res

    def meal_ok(self, b, who, other=None):
        """A meal/recipe idea or weekly meal plan request from what's at home: handled by Pantry."""
        if other or who == "" or b.get("use_brain") is False or getattr(self, "pantry", None) is None:
            return None
        from .pantry import meal_request, week_plan_request
        q = b.get("question") or ""
        w = week_plan_request(q)
        if w:
            return dict(w, kind="week")
        m = meal_request(q)
        return dict(m, kind="single") if m else None

    async def meal_answer(self, b, who, req, quick):
        """Meal ideas or a weekly meal plan + shopping list from Pantry; falls back to the usual ask path if no
        model returns a usable plan."""
        from .pantry import format_plan, format_week_plan
        voice = bool(b.get("voice"))
        chosen = b.get("model") if b.get("model") not in (None, "", "auto") else None
        md = getattr(self, "modes", None)
        try:
            private = md is not None and md.mode(who or None) == "private"
        except Exception:  # noqa: BLE001
            private = False
        if chosen and b.get("fallback") is False:
            chain = [chosen]
        else:
            chain = self._deep_chain(who)
            if chosen and chosen not in chain and not private:
                chain = [chosen] + chain
            if not chain and not private:
                chain = [x for x in (getattr(getattr(self, "models", None), "models", None) or {})
                         if x != "mock"][:2]
        chain = [m for m in chain if m and m != "mock"] or ([chosen] if chosen == "mock" else [])

        async def run():
            for m in chain:
                try:
                    if req.get("kind") == "week":
                        plan = await self.pantry.plan_week(
                            days=req.get("days", 7), meals=req.get("meals"),
                            request=req.get("request") or "", model=m, viewer=who,
                        )
                        text = format_week_plan(plan, voice=voice)
                        if text:
                            added_shop = []
                            if req.get("auto_shop") and plan.get("shopping_items"):
                                try:
                                    added_shop = await self.pantry.add_to_shopping(plan["shopping_items"])
                                    if added_shop:
                                        text += (f" I also added {len(added_shop)} items to your shopping list."
                                                 if voice else
                                                 f"\n\nAdded {len(added_shop)} items to your Home Assistant shopping list.")
                                except Exception as e:  # noqa: BLE001
                                    log.warning("auto-shop failed: %s", e)
                            return {"answer": text, "hits": [], "model": plan.get("model") or m,
                                    "week_plan": plan, "missing": [] if added_shop else (plan.get("shopping_items") or []),
                                    "pantry": True}
                    else:
                        plan = await self.pantry.suggest(req["meal"], model=m, n=2 if voice else 3, request=req["request"], viewer=who)
                        text = format_plan(plan, voice=voice)
                        if text:
                            return {"answer": text, "hits": [], "model": plan.get("model") or m,
                                    "meals": plan.get("meals"), "pantry": True}
                except Exception as e:  # noqa: BLE001 - try next model, then fall back to quick
                    log.warning("meal answer with %s: %s", m, e)
            return None

        try:
            res = await (asyncio.wait_for(run(), self.DEEP_VOICE_SECONDS) if voice else run())
        except asyncio.TimeoutError:
            res = None
        if res is None:
            return await quick
        quick.close()
        if b.get("conversation_id") and hasattr(self.mind, "_record_turn"):
            self.mind._record_turn(b["conversation_id"], b["question"], res["answer"])
        return res

    def deep_ok(self, b, who, other=None):
        """A question for the connected path: why, lately, how am I, really, busy (connect.wants_deep), asked by
        someone who may see personal data (not a guest, not another assistant), with the option on."""
        from . import connect as CN
        chosen = b.get("model") not in (None, "", "auto") and not b.get("mode_model")    # a model picked by hand
        if other or who == "" or self.opts.get("connected_answers") is False or chosen or \
                b.get("use_brain") is False or getattr(self, "brief", None) is None:
            return False
        return bool(b.get("force_deep")) or CN.wants_deep(b.get("question") or "")

    def _deep_chain(self, who):
        name = self.opts.get("deep_model") or None
        chain = self.mind._chain(name)
        md = getattr(self, "modes", None)
        try:
            private = md is not None and md.mode(who or None) == "private"
        except Exception:  # noqa: BLE001
            private = False
        if private:     # private mode: nothing leaves the house
            local, _ = self.mind.route_lists()
            chain = [m for m in chain if m in local]
        return chain[:2]

    async def deep_answer(self, b, who, quick):
        """The connected answer: the brief (code), a model to connect it, every number checked against the brief.
        quick is the usual path's coroutine: it runs alongside for the side-by-side, and answers if this can't."""
        from . import connect as CN
        from .mind import cloud_safe
        q, voice = b["question"], bool(b.get("voice"))
        conv_id = b.get("conversation_id")
        compare = self.opts.get("compare_answers", True) is not False and not voice and b.get("compare", True) is not False
        qt = asyncio.ensure_future(quick) if compare else None      # side by side: both at once

        async def deep():
            facts = self.brief.build(q, who or None)
            st = self.mind.store
            try:
                hits, _p, ctx = await self.mind.context_for(st, q, k=12, voice=voice)
            except Exception:  # noqa: BLE001
                hits, ctx = [], ""
            prior = self.mind._history(conv_id, st) if conv_id and hasattr(self.mind, "_history") else []
            system = CN.DEEP_SYSTEM + (CN.VOICE_DEEP if voice else "")
            for m in self._deep_chain(who):
                try:
                    m_obj = self.models.get(m)
                    safe_ctx = cloud_safe(ctx, q, m_obj, st) if ctx else ""
                    content = f"QUESTION: {q}\n\nBRIEF (their own data, gathered by code):\n{facts['text']}\n\n" \
                              f"NOTES AND OTHER SOURCES:\n{safe_ctx or '(none)'}"
                    msgs = []
                    for pq, pa in prior:
                        msgs.append({"role": "user", "content": cloud_safe(pq, q, m_obj, st)})
                        msgs.append({"role": "assistant", "content": cloud_safe(pa, q, m_obj, st)})
                    msgs.append({"role": "user", "content": content})
                    r = await m_obj.chat(system, msgs, max_tokens=500 if not voice else 200, temperature=0.2)
                    text = (r.get("text") or "").strip()
                    bad = CN.numbers_ok(text, facts["text"] + "\n" + (ctx or ""), q)
                    if bad:     # once more, told which numbers aren't in the brief
                        r = await m_obj.chat(system, msgs + [
                            {"role": "assistant", "content": text},
                            {"role": "user", "content": f"These numbers aren't in the brief: {', '.join(bad)}. "
                                                        "Answer again using only numbers from the brief."}],
                            max_tokens=500 if not voice else 200, temperature=0)
                        text = (r.get("text") or "").strip()
                        if CN.numbers_ok(text, facts["text"] + "\n" + (ctx or ""), q):
                            log.info("deep answer from %s used numbers not in the brief: quick path instead", m)
                            continue
                    if text:
                        return {"answer": text, "hits": hits, "model": m, "deep": True, "personal": True,
                                "issues": facts.get("issues"), "sources": facts.get("sources") or []}
                except Exception as e:  # noqa: BLE001 - the next model, then the quick path
                    log.warning("deep answer with %s: %s", m, e)
            return None

        try:
            res = await (asyncio.wait_for(deep(), self.DEEP_VOICE_SECONDS) if voice else deep())
        except asyncio.TimeoutError:
            res = None
        if res is None:
            return await (qt if qt is not None else quick)
        if compare:
            try:
                quick_res = await asyncio.wait_for(asyncio.shield(qt), 20)
            except Exception:  # noqa: BLE001
                quick_res = None
            if quick_res and quick_res.get("answer"):
                import secrets as _s
                cid = _s.token_hex(6)
                picks = list(self.mind.store.kv_get("answer_picks") or [])[-199:]
                picks.append({"id": cid, "at": time.time(), "deep_model": res.get("model"),
                              "quick_model": quick_res.get("model"), "issues": res.get("issues") or [], "pick": None})
                self.mind.store.kv_set("answer_picks", picks)
                res = dict(res, quick={"answer": quick_res["answer"], "model": quick_res.get("model")}, compare_id=cid)
        else:
            quick.close()           # never started
        if conv_id and hasattr(self.mind, "_record_turn"):
            self.mind._record_turn(conv_id, q, res["answer"])
        return res

    def answer_pick(self, cid, pick):
        """Which was better, the connected answer or the quick one ("deep", "quick", "same")."""
        if pick not in ("deep", "quick", "same"):
            raise ValueError("pick is deep, quick or same")
        picks = list(self.mind.store.kv_get("answer_picks") or [])
        for p in picks:
            if p["id"] == cid:
                p["pick"], p["picked"] = pick, time.time()
                self.mind.store.kv_set("answer_picks", picks)
                done = [x for x in picks if x.get("pick")]
                return {"ok": True, "so_far": {k: sum(1 for x in done if x["pick"] == k)
                                               for k in ("deep", "quick", "same")}}
        raise KeyError(f"no answer {cid}")

    async def _style(self, res, *a):
        """The persona (Jeeves) on an answer, when there is one; an app without it answers plainly."""
        f = getattr(self, "styled", None)
        return await f(res, *a) if f is not None and getattr(self, "persona", None) is not None else res

    async def push_counts(self):
        s = self.mind.store.stats()
        await self.ha.sensor("sensor.home_mind_items", s["items"],
                             {"friendly_name": "Home Mind items", "icon": "mdi:brain", "unit_of_measurement": "items",
                              "state_class": "measurement", "shelves": s["shelves"], "faded": s["faded"],
                              "versions": s["versions"], "by_kind": s["by_kind"]})

    def _verb(self, plan):
        a = (plan.get("action") or "").lower()
        eid = plan.get("entity_id") or ""
        if eid.split(".")[0] in ("cover",):
            return {"on": "open", "off": "close", "toggle": "open or close"}.get(a, a or "change")
        return {"on": "turn on", "off": "turn off"}.get(a, a or "change")

    async def ask_phone(self, plan, why=None, source="api"):
        """The riskiest actions (locks, garage doors, alarms, heaters): an approval request to the owner's phone.
        Approve there (Face ID or the passcode) within 2 minutes and it is done; nothing else can do it."""
        eid = plan["entity_id"]
        name = (self.home.catalog.get(eid) or {}).get("name") or eid
        verb = self._verb(plan)
        target = (self.opts.get("approval_phone") or "").strip() or await self.morning._phone()
        if not target:
            self.home._log_decision(eid, plan.get("action"), plan.get("value"), source, why, status="refused",
                                    reason="needs phone approval and no phone is set up")
            return {"plan": plan, "error": "phone approval needed and no phone is set up (option approval_phone)",
                    "answer": f"I need an OK on your phone before I {verb} the {name}, and I can't reach a phone. "
                              "Please use the Home Assistant app."}
        aid = self.approvals.new(plan, why, source, name, verb)
        who = (why or {}).get("person") or source
        await self.ha.push(target, Approvals.message(aid, name, verb, who), tier="important", now=True)
        self.home._log_decision(eid, plan.get("action"), plan.get("value"), source, why, status="awaiting",
                                reason=f"phone approval {aid} sent to {target}")
        return {"plan": plan, "approval_sent": True, "approval_id": aid,
                "answer": f"I've sent a request to your phone. Approve it there and I'll {verb} the {name}."}

    async def on_approval(self, action):
        """An answer from the phone (mobile_app_notification_action): HM_APPROVE_<id> does it, HM_DENY_<id> drops it.
        An unknown, used or expired id does nothing."""
        m = re.fullmatch(r"HM_(APPROVE|DENY)_([0-9a-f]{12})", str(action or ""))
        if not m:
            return None
        p = self.approvals.take(m.group(2))
        if not p:
            return {"expired": True}
        plan = p["plan"]
        if m.group(1) == "DENY":
            self.home._log_decision(plan["entity_id"], plan.get("action"), plan.get("value"), p["source"], p["why"],
                                    status="refused", reason="denied on the phone")
            return {"denied": True}
        try:
            done = await self.home.act(plan["entity_id"], plan.get("action"), plan.get("value"), p["source"],
                                       why=dict(p["why"] or {}, channel=(p["why"] or {}).get("channel") or p["source"]),
                                       approved=True)
        except (ValueError, PermissionError) as e:
            await self.ha.notify("Home Mind", f"I couldn't {p['verb']} the {p['name']}: {e}", "home_mind_approval")
            return {"error": str(e)}
        await self.ha.notify("Home Mind", f"Done: {p['verb']} the {p['name']}.", "home_mind_approval")
        return {"done": done}

    async def act_checked(self, entity_id, action, value=None, source="api", confirm=False, why=None):
        """For callers that name the device themselves (/api/home/act, MCP mind_control): an action that needs the
        phone sends the request instead of failing."""
        from .approval import ApprovalNeeded, PHONE
        try:
            return await self.home.act(entity_id, action, value, source, confirm=confirm, why=why)
        except ApprovalNeeded as e:
            if e.level != PHONE:
                raise
            eid = entity_id if entity_id in self.home.catalog else (self.home.matching(entity_id, 1) or [entity_id])[0]
            return await self.ask_phone({"entity_id": eid, "action": action, "value": value}, why, source)

    async def on_red_flag(self, flag, text, speaker=None, conversation_id=None, source="ask"):
        """A red flag was answered: logged (an event, with its kind only, never the words), and the safety contact
        is told for emergency, double dose and decline if the person chose that (option safety_contact)."""
        self.mind.store.log("red_flag", kind=flag.kind, speaker=speaker or "", source=source)
        target = (self.opts.get("safety_contact") or "").strip()
        sent = self.__dict__.setdefault("_flag_sent", {})
        key = (flag.kind, speaker or "")
        if target and flag.notify and time.time() - sent.get(key, 0) < 6 * 3600:
            return              # the contact was told about this in the last 6 hours: once is enough
        if target and flag.notify:
            sent[key] = time.time()
            who = speaker or "Someone at home"
            what = {"emergency": "may need emergency help", "double_dose": "may have taken a double dose",
                    "decline": "described a big change in how they're managing"}[flag.kind]
            await self.ha.push(target, {"title": "Home Mind: please check in", "message": f"{who} {what}. "
                                        "Home Mind told them where to get help. Please check on them."}, tier="safety")

    def checkin_reply(self, text, person=None):
        """A check-in answer ("rough", "having a low day", or "good" right after the question): recorded, with the
        day's reply. None if it isn't one."""
        day = getattr(self, "day", None)
        if day is None:
            return None
        from .day import parse, REPLIES
        state = parse(text, expecting=day.expecting(person))
        if not state:
            return None
        day.set(state, person, how="said")
        return {"answer": REPLIES[state], "checkin": state}

    async def on_day_tap(self, action):
        """Good / Low / Rough tapped on the morning notification."""
        import re as _re
        m = _re.fullmatch(r"HM_DAY_(GOOD|LOW|ROUGH)", str(action or ""))
        if not m or getattr(self, "day", None) is None:
            return None
        return self.day.set(m.group(1).lower(), None, how="tap")

    def _measure_do(self, res, text, source, t0):
        """Phase 4: count the request, its time, an abstain ("nothing fits"), and maybe shadow it."""
        dlog = getattr(self, "decisions", None)
        if dlog is None:
            return
        try:
            dlog.count("requests")
            if "steps" in res:      # several actions, or a "which one?" (the rules / multi-action path): no shadow
                dlog.timing("do", (time.monotonic() - t0) * 1000, source, res.get("by"))
                if not res.get("which") and not any(s.get("entity_id") for s in res.get("steps") or []):
                    dlog.count("abstains")
                return
            plan = res.get("plan") or {}
            dlog.timing("do", (time.monotonic() - t0) * 1000, source, plan.get("by"))
            if not plan.get("entity_id"):
                dlog.count("abstains")
            m = getattr(self, "measure", None)
            if m is not None and plan.get("entity_id") and not plan.get("compared"):
                did = (res.get("done") or {}).get("decision_id")
                m.maybe_shadow(text, plan, did)
        except Exception:  # noqa: BLE001 - measuring never gets in the way of the answer
            log.exception("measure")

    def phone_of(self, person):
        """A person's phone (option people: notify), or None."""
        for p in self.people():
            if person and p["name"].lower() == str(person).lower() and p.get("notify"):
                t = p["notify"]
                return t if "." in t else "notify." + t
        return None

    def alone(self, person):
        """Is the person on their own? The house: the only one home, from person entities; with none known, not
        alone (be careful). The room model (rooms.py) only ever makes it less alone (someone else heard in their room
        lately): it hears voices, not the people who stay silent, so it can't say a room is empty."""
        rm = getattr(self, "rooms", None)
        if rm is not None and rm.where(person) and not rm.alone(person):
            return False
        persons = {e: (c.get("state") or "") for e, c in (self.home.catalog or {}).items() if e.startswith("person.")}
        if not persons:
            return False
        return sum(1 for s in persons.values() if s == "home") <= 1

    async def private_by_voice(self, res, person):
        """Who may hear it (design step 18): an answer drawn from someone's personal notes is spoken only when they
        were recognised and are alone; otherwise "you have a private note", sent to their phone if there is one."""
        st = self.mind.store
        private = bool(res.get("personal"))      # health readings (health.py) are personal too
        for h in res.get("hits") or []:
            d = st.get(h["id"]) if h.get("id") else None
            if d and (d.get("meta") or {}).get("scope") == "personal":
                private = True
                break
        if not private or self.alone(person):
            return res
        phone = self.phone_of(person)
        if phone:
            await self.ha.push(phone, {"title": "Home Mind (private)", "message": res.get("answer") or ""},
                               tier="important", now=True)
            return dict(res, answer="You have a private note. I've sent it to your phone.", private=True,
                        sent_to=phone)
        return dict(res, answer="That's in your private notes, and you're not on your own. Ask me when you are, "
                                "or look in Home Mind on your phone.", private=True)

    async def tell_after(self, r):
        """"Tell me after": a short notice once it's done."""
        target = (self.opts.get("approval_phone") or "").strip() or await self.morning._phone()
        did = {"turn_on": "turned on", "turn_off": "turned off", "toggle": "switched", "open_cover": "opened",
               "close_cover": "closed", "set_percentage": "set the speed of", "set_humidity": "set the humidity of",
               "lock": "locked", "unlock": "unlocked"}.get(r["service"].split(".")[1], "changed")
        msg = f"I {did} the {r['name']}."
        if target:
            await self.ha.push(target, {"title": "Home Mind", "message": msg}, tier="routine")
        else:
            await self.ha.notify("Home Mind", msg, "home_mind_tell")

    async def test_done(self, run):
        if run["status"] != "done":
            return
        s = run["summary"]
        await self.ha.sensor(f"sensor.home_mind_test_{run['suite']}", round(100 * s.get("accuracy", 0), 1),
                             {"friendly_name": f"Home Mind test {run['suite']}", "unit_of_measurement": "%",
                              "state_class": "measurement", "icon": "mdi:test-tube", "arm": run["arm"],
                              "model": run["model"], "run": run["id"], "seed": run["seed"], **s})

    async def upkeep(self):
        last = {"fade": 0, "packs": 0, "test": time.time(), "counts": 0, "comfort": 0, "body": 0, "home": 0,
                "home_first": True}
        if self.ha.available:
            try:
                log.info("weigh-ins filed from history: %s", await self.body.backfill())
            except Exception:  # noqa: BLE001
                log.exception("body backfill")
        while True:
            try:
                await self._upkeep_step(last)
            except Exception:  # noqa: BLE001 - one bad step must never stop the loop for good
                log.exception("upkeep")
            await asyncio.sleep(20)

    async def _upkeep9(self, now, last):
        """Phase 9: retries from the outbox, the forget list after a restore and every night, the check, the weekly
        test restore and save of learned records, and records back on trial after a model change."""
        from . import upkeep as U
        if now - last.get("outbox", 0) > 60:
            last["outbox"] = now
            try:
                await self.ha.retry_failed()
            except Exception:  # noqa: BLE001
                log.exception("outbox")
        fl = getattr(self, "forget", None)
        if fl is None:
            return
        if not last.get("check_first"):
            last["check_first"] = True       # at startup: after a restore the list goes first, then the check
            try:
                n = fl.apply()
                if n:
                    log.warning("forget list: %d note(s) you had deleted for good were back and are deleted again", n)
                U.retest_if_swapped(self)
            except Exception:  # noqa: BLE001
                log.exception("forget list / model check")
            self.store_check = U.check(self)
        if datetime.now().hour == 4 and now - last.get("check_night", 0) > 20 * 3600:
            last["check_night"] = now
            try:
                fl.apply()
                self.store_check = U.check(self)
                if not self.store_check["ok"] and self.ha.available:
                    await self.ha.notify("Home Mind: a check found something", "\n".join(self.store_check["problems"]),
                                         "home_mind_check")
                U.retest_if_swapped(self)
                if self.learned_history.save_week():
                    log.info("learned records: this week saved")
                if datetime.now().weekday() == 6:
                    rt = U.restore_test(self.mind.store, fl)
                    log.info("weekly test restore: %s", "ok" if rt["ok"] else rt.get("why"))
                self.ha.outbox.prune()
            except Exception:  # noqa: BLE001
                log.exception("nightly check")

    async def _consent_automation_on(self, payload):
        """An automation agreed on a clear day, 24 hours on: switched on."""
        eid = payload.get("entity_id")
        if not eid:
            return {"error": "no automation"}
        await self.ha.call("automation", "turn_on", {"entity_id": eid}, approved=True)
        return {"on": eid}

    async def _upkeep12(self, now, last):
        """Phase 12: things from Home Assistant's devices (hourly), notes linked to things (10 min), scene previews
        nobody kept put back (every pass)."""
        th = getattr(self, "things", None)
        if th is None:
            return
        try:
            await self.scenes.undo_due()
        except Exception:  # noqa: BLE001
            log.exception("scene previews")
        if now - last.get("things_dev", 0) > 3600 and self.home.catalog:
            last["things_dev"] = now
            try:
                devs = {d["entity_id"] for d in self.home.controllable()}
                th.from_devices({e: c for e, c in self.home.catalog.items() if e in devs})
            except Exception:  # noqa: BLE001
                log.exception("things from devices")
        if now - last.get("things_link", 0) > 600:
            last["things_link"] = now
            try:
                th.link_notes()
            except Exception:  # noqa: BLE001
                log.exception("things links")

    async def on_nudge_action(self, action):
        import re as _re
        m = _re.fullmatch(r"HM_NUDGE_DONE_(\d+)", str(action or ""))
        if m and getattr(self, "tracking", None) is not None:
            self.tracking.nudge_done(int(m.group(1)))
            return {"done": int(m.group(1))}
        return None

    async def _upkeep13(self, now, last):
        """Phase 13: trackers and the weekly events checklist as routine asks, nudges, and exertion zones."""
        tk = getattr(self, "tracking", None)
        if tk is None or not self.ha.available:
            return
        if now - last.get("trackers", 0) > 300:
            last["trackers"] = now
            try:
                people = [p["name"] for p in self.people()] or [None]
                for who in people:
                    phone = self.phone_of(who) if who else await self.morning._phone()
                    if not phone:
                        continue
                    for t in tk.due(who):
                        await self.ha.push(phone, {"title": "Home Mind", "message": t["question"] + " (one word or a "
                                                   "number is plenty)"}, tier="routine", batch=True)
                        tk.asked(t["id"])
                    wk = tk.week()
                    if datetime.now().weekday() == 6 and self.mind.store.kv_get(f"events_asked:{who}") != wk \
                            and not tk.paused(who):
                        self.mind.store.kv_set(f"events_asked:{who}", wk)
                        await self.ha.push(phone, {"title": "Home Mind: this week", "message":
                                                   "Any of these this week: a cold, a trip, a hard week, a new "
                                                   "medicine, poor sleep, a big event? (Unanswered stays unknown.)"},
                                           tier="routine", batch=True)
                # arrive / leave nudges: where each person's state changed, and when
                pc = self.__dict__.setdefault("_place_changes", {})
                was = self.__dict__.setdefault("_places", {})
                for p in self.people():
                    for e in p.get("persons") or []:
                        s = ((self.home.catalog or {}).get(e) or {}).get("state")
                        if s and was.get(p["name"]) and was[p["name"]] != s:
                            pc[p["name"]] = {"from": was[p["name"]], "to": s, "at": now}
                        if s:
                            was[p["name"]] = s
                for n in tk.nudges_due({"states": {e: c.get("state") for e, c in (self.home.catalog or {}).items()},
                                        "place_changes": pc}):
                    phone = self.phone_of(n["owner"]) if n["owner"] else await self.morning._phone()
                    if phone:
                        await self.ha.push(phone, {"title": "Home Mind", "message": tk.nudge_message(n), "data": {
                            "actions": [{"action": f"HM_NUDGE_DONE_{n['id']}", "title": "Done"}]}}, tier="routine")
                        tk.sent(n["id"])
            except Exception:  # noqa: BLE001
                log.exception("trackers and nudges")
        z = getattr(self, "zones", None)
        if z is not None and z.on() and now - last.get("zones", 0) > 5:
            last["zones"] = now
            try:
                live = await self.mind.health.live_heart_rate() if getattr(self.mind, "health", None) else None
                rows = self.patterns.rows(14) if getattr(self, "patterns", None) else []
                resting = next((r.get("resting_hr") for r in reversed(rows) if r.get("resting_hr")), None)
                if live:
                    ch = z.reading(live[0], now - live[2] * 60, resting)
                    if ch == "above":
                        msg = z.notice()
                        if msg:
                            phone = await self.morning._phone()
                            if phone:
                                await self.ha.push(phone, {"title": "Home Mind", "message": msg}, tier="routine")
            except Exception:  # noqa: BLE001
                log.exception("exertion zones")

    async def _upkeep14(self, now, last):
        """Phase 14, once a day: the experiment's checks (travel days, a safety stop, the end), and gaps turned into
        proposals; a batch to Opus only when a gate failed or a gap is queued, and only with proposals_model set."""
        ex = getattr(self, "experiments", None)
        if ex is None or datetime.now().hour != 5 or now - last.get("exp_day", 0) < 20 * 3600:
            return
        last["exp_day"] = now
        try:
            r = ex.check()
            if r and self.ha.available:
                await self.ha.notify("Home Mind: experiment", r.get("text") or "The experiment ended.", "home_mind_exp")
        except Exception:  # noqa: BLE001
            log.exception("experiment check")
        try:
            self.proposals.build()
            await self._proposal_batch()
        except Exception:  # noqa: BLE001
            log.exception("proposals")

    async def _proposal_batch(self):
        name = self.opts.get("proposals_model") or ""
        m = (getattr(self.models, "models", {}) or {}).get(name)
        cfg = getattr(m, "cfg", None) or {}
        st = self.mind.store
        # models are configured with "type" (models.py); "provider" was never set, so the batch never ran
        if not m or (cfg.get("type") or cfg.get("provider")) != "anthropic" or not cfg.get("api_key"):
            return None
        base = (cfg.get("url") or "https://api.anthropic.com").rstrip("/")
        hdr = {"x-api-key": cfg["api_key"], "anthropic-version": "2023-06-01"}
        import aiohttp
        pending = st.kv_get("proposals_batch")
        if pending:
            async with self.models.session.get(f"{base}/v1/messages/batches/{pending['id']}", headers=hdr,
                                               timeout=aiohttp.ClientTimeout(total=30)) as r:
                j = await r.json(content_type=None)
            if j.get("type") == "error" or j.get("processing_status") in ("canceled", "expired") or \
                    time.time() - (pending.get("at") or 0) > 3 * 86400:
                st.kv_set("proposals_batch", None)      # gone or stuck: try again next night, never wait forever
                return pending
            if j.get("processing_status") == "ended" and j.get("results_url"):
                async with self.models.session.get(j["results_url"], headers=hdr,
                                                   timeout=aiohttp.ClientTimeout(total=60)) as r:
                    lines = (await r.text()).splitlines()
                drafts = st.kv_get("proposal_drafts") or {}
                for ln in lines:
                    try:
                        x = json.loads(ln)
                        msg = (x.get("result") or {}).get("message") or {}
                        drafts[x["custom_id"]] = "".join(b.get("text", "") for b in msg.get("content") or [])
                    except (ValueError, KeyError):
                        continue
                st.kv_set("proposal_drafts", drafts)
                st.kv_set("proposals_batch", None)
            return pending
        gate_failed = any("replay got worse" in (c.get("note") or "") for c in self.learned_history.changes(1)) \
            if getattr(self, "learned_history", None) else False
        req = self.proposals.batch(gate_failed, cfg.get("model") or "claude-opus-5-5")
        if not req:
            return None
        import hashlib
        sig = hashlib.sha256(json.dumps(req, sort_keys=True, default=str).encode()).hexdigest()
        if st.kv_get("proposals_batch_sent") == sig:
            return None             # the same proposals were drafted already: nothing new to pay for
        st.kv_set("proposals_batch_sent", sig)
        async with self.models.session.post(f"{base}/v1/messages/batches", json=req, headers=hdr,
                                            timeout=aiohttp.ClientTimeout(total=60)) as r:
            j = await r.json(content_type=None)
        if j.get("id"):
            st.kv_set("proposals_batch", {"id": j["id"], "at": time.time()})
        return j

    async def _upkeep15(self, now, last):
        """Phase 15: Firefly III totals every 15 minutes (never while someone is speaking: off the voice path)."""
        url, tok = self.opts.get("firefly_url") or "", self.opts.get("firefly_token") or ""
        if not (url and tok) or now - last.get("firefly", 0) < 900:
            return
        last["firefly"] = now
        from . import frontends as FE
        try:
            t = await FE.firefly_totals(self.models.session, url, tok)
            self.mind.store.kv_set("firefly", {"totals": t, "at": now})
            b = getattr(self, "board", None)
            owner = self.opts.get("firefly_owner") or None
            if b is not None and t and owner:
                b.set("app:firefly", FE.say_money(t, now), "note", 1800, source="firefly", owner=owner)
        except Exception as e:  # noqa: BLE001
            log.warning("firefly: %s", e)

    async def _upkeep11(self, now, last):
        """Phase 11: the surprises queue from the decision log, corrections and asks, every 10 minutes."""
        sp = getattr(self, "surprises", None)
        if sp is not None and now - last.get("surprises", 0) > 600:
            last["surprises"] = now
            try:
                from .cases import Cases
                sp.collect(getattr(self, "decisions", None), Cases(self.mind.store))
            except Exception:  # noqa: BLE001
                log.exception("surprises")

    async def _upkeep10(self, now, last):
        """Phase 10: agreed changes whose 24 hours are up, and the spend cap said once."""
        c = getattr(self, "consent", None)
        if c is not None and now - last.get("consent", 0) > 60:
            last["consent"] = now
            try:
                for r in await c.due():
                    log.info("agreed change took effect: %s", r)
            except Exception:  # noqa: BLE001
                log.exception("consent")
        if now - last.get("spend", 0) > 3600:
            last["spend"] = now
            from .channels import over_cap, month_spend
            st = self.mind.store
            month = datetime.now().strftime("%Y-%m")
            if over_cap(self.usage, self.opts.get("monthly_spend_cap")) and st.kv_get("spend_told") != month:
                st.kv_set("spend_told", month)
                await self.ha.notify("Home Mind: spend cap reached",
                                     f"This month's cloud spend is ${month_spend(self.usage):.2f}, at the cap of "
                                     f"${float(self.opts.get('monthly_spend_cap')):.2f}. Until next month, answers come "
                                     "from local models or your notes.", "home_mind_spend")

    async def _upkeep_step(self, last):
        now = time.time()
        if now - last.get("snapshot", 0) > 3600:
            last["snapshot"] = now
            from . import dbguard
            try:
                st = self.mind.store
                made = dbguard.snapshot(st.db, st.path, int(self.opts.get("db_snapshots", 7) or 7))
                if made:
                    log.info("database snapshot: %s", made)
            except Exception as e:  # noqa: BLE001
                log.warning("database snapshot: %s", e)
        await self._upkeep9(now, last)
        await self._upkeep10(now, last)
        await self._upkeep11(now, last)
        await self._upkeep12(now, last)
        await self._upkeep13(now, last)
        await self._upkeep14(now, last)
        await self._upkeep15(now, last)
        rep = getattr(self, "db_report", None)
        if rep and self.ha.available and not rep.get("told"):
            rep["told"] = True
            await self.ha.notify("Home Mind: database restored", rep["message"], "home_mind_db")
        if now - last.setdefault("safety", now - 86400 + 300) > 86400:
            last["safety"] = now          # the daily safety suite: first 5 minutes after start, then every day
            from . import safety
            res = await safety.run_and_report(self)
            log.info("safety suite: %s (%d passed, %d failed)", "pass" if res["ok"] else "FAIL", res["passed"],
                     len(res["failed"]))
        board = getattr(self, "board", None)
        if board is not None:
            try:
                board.refresh(self)
            except Exception:  # noqa: BLE001
                log.exception("now board")
        wo = getattr(self, "worsening", None)
        if wo is not None and now - last.setdefault("worsening", 0) > 86400 and datetime.now().hour >= 10:
            last["worsening"] = now      # once a day, mid-morning: yesterday's values, then the trend
            try:
                wo.collect(self)
                r = wo.check()
                if r.get("alarm"):
                    from .worsening import MESSAGE
                    target = (self.opts.get("approval_phone") or "").strip() or await self.morning._phone()
                    await self.ha.notify("Home Mind", MESSAGE, "home_mind_worsening")
                    if target:
                        await self.ha.push(target, {"title": "Home Mind", "message": MESSAGE}, tier="important")
                    log.info("worsening alarm: %s", {k: r[k] for k in ("rough", "fog")})
            except Exception:  # noqa: BLE001
                log.exception("worsening")
        lr = getattr(self, "learned", None)
        la = getattr(self, "learned_answers", None)
        if lr is not None and getattr(self, "decisions", None) is not None and now - last.get("learn", 0) > 600:
            last["learn"] = now          # every 10 minutes: settled requests -> evidence and shadow templates
            for name, rec in (("learned", lr), ("learned answers", la)):
                if rec is None:
                    continue
                try:
                    got = rec.learn_from(self.decisions)
                    if got["created"] or got["evidence"]:
                        log.info("%s: %s", name, got)
                except Exception:  # noqa: BLE001
                    log.exception(name)
        if lr is not None and getattr(self, "measure", None) is not None and \
                datetime.now().hour == 3 and now - last.get("learned_night", 0) > 20 * 3600:
            last["learned_night"] = now  # the nightly gate: promotions checked by replay
            # one kind of record change a night (design step 26): house templates and answers take turns
            answers_night = la is not None and datetime.now().toordinal() % 2 == 1
            try:
                r = await (la.nightly() if answers_night else lr.nightly(self.decisions, self._learned_score))
                log.info("learned (%s), nightly: %s", "answers" if answers_night else "house",
                         {k: v for k, v in r.items() if k != "waiting"})
            except Exception:  # noqa: BLE001
                log.exception("learned nightly")
        mo = getattr(self, "modes", None)
        if mo is not None and getattr(self, "measure", None) is not None and \
                now - last.setdefault("modes", now - 86400 + 3600) > 86400:
            last["modes"] = now          # once a day: propose kinds the local model has earned, demote failures
            try:
                ch = mo.review(self.measure)
                if ch["proposed"]:
                    self.ha.batcher.add("The home computer could handle more", "It got " + ", ".join(
                        ch["proposed"]) + " right nearly every time lately. You can let it take these over "
                        "(Home Mind: model modes).") if getattr(self.ha, "batcher", None) else None
                if ch["proposed"] or ch["demoted"]:
                    log.info("model modes: %s", ch)
            except Exception:  # noqa: BLE001
                log.exception("model modes")
        m = getattr(self, "measure", None)
        if m is not None and now - last.setdefault("weekly", now - 7 * 86400 + 1800) > 7 * 86400:
            last["weekly"] = now        # the weekly replay against frozen v1: first 30 minutes after start
            try:
                sc = await m.weekly(lambda text: self.plan_only(text),
                                    notify=lambda t, msg: self.ha.notify(t, msg, "home_mind_scorecard"))
                log.info("weekly scorecard: %s", {k: v.get("verdict") for k, v in sc["sections"].items()})
            except Exception:  # noqa: BLE001
                log.exception("weekly scorecard")
        if getattr(self, "decisions", None) is not None:
            try:
                await self.decisions.sweep(self._state_for_check)      # outcomes that are due (every 20 s)
                if now - last.get("prune", 0) > 86400:
                    last["prune"] = now
                    n = self.decisions.prune()
                    if n:
                        log.info("decision log: %d old decisions dropped", n)
            except Exception:  # noqa: BLE001
                log.exception("decision log sweep")
        if self.ha.available and self.mind.home and now - last["home"] > 300:
            last["home"] = now
            if now - last.get("areas", 0) > 1800:
                last["areas"] = now
                try:
                    await self.home.load_areas()          # rooms, for "lights off" said in a room
                except Exception as e:  # noqa: BLE001
                    log.warning("areas: %s", e)
            try:
                await self.home.refresh()
                n = self.home.log_changes(first=last["home_first"])
                try:
                    self.prefs.observe(self.home.catalog, getattr(self.home, "own", {}), now)    # #30
                    if now - last.get("prefs", 0) > 6 * 3600:
                        last["prefs"] = now
                        self.prefs.file_notes({e: c["name"] for e, c in self.home.catalog.items()})
                        self.photos.sweep()
                except Exception:  # noqa: BLE001
                    log.exception("preferences")
                if last["home_first"]:
                    log.info("home: %d things, %d filed", len(self.home.catalog), n)
                last["home_first"] = False
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
        self._spawn("overnight", self.overnight.tick)      # can take a long while: the loop keeps ticking
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
                            await self.ha.push(target, {"title": title, "message": msg}, batch=True)
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
        if self.mailbox.on and now - last.get("mail", 0) > 60 * self.mailbox.minutes:
            last["mail"] = now
            try:
                r = await asyncio.to_thread(self.mailbox.run)       # #64
                if r.get("read"):
                    log.info("email: %s", r)
            except Exception as e:  # noqa: BLE001
                log.warning("email: %s", e)
                self.mailbox.last = {"error": str(e)[:200], "at": now}
        if self.compare.due(now):
            self._spawn("compare", self._compare)          # many suites per model: in the background
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
                n = self.mind.store.roll_up_house_log(int(self.opts.get("events_keep_days") or 0))
                if n:
                    log.info("house log: rolled %d notes older than %s days into daily notes", n,
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
                self._spawn("auto_test", self._auto_tests)
        except Exception:  # noqa: BLE001
            log.exception("upkeep")


    def _spawn(self, name, job):
        """Run a long job (a comparison, the night's learning, test suites) beside the upkeep loop, one of each name at
        a time, keeping a reference so it isn't collected mid-run and its failure is logged, not lost."""
        t = self._bg.get(name)
        if t is not None and not t.done():
            return None
        t = asyncio.create_task(job())
        self._bg[name] = t

        def done(t):
            if not t.cancelled() and t.exception() is not None:
                log.error("%s failed", name, exc_info=t.exception())
            if name.startswith("estimate:") and self._bg.get(name) is t:
                self._bg.pop(name, None)        # one per logged meal: don't keep them once finished
        t.add_done_callback(done)
        return t

    async def _compare(self):
        try:
            r = await self.compare.run()
            if r.get("error"):
                return
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

    async def _auto_tests(self):
        for suite in ("recall", "update", "locate", "tasks"):
            await self.mind.run_test_now(suite, "brain", seed=random.randint(1, 99999))

    def setup_vault(self):
        """Option encrypt_secrets (#66): seal secret notes with a key kept out of backups; turned off, open them."""
        from . import vault
        self.vault_status = vault.setup(self.mind.store, os.path.join(DATA, "vault.key"),
                                        self.opts.get("encrypt_secrets"))
        if self.vault_status.get("error"):
            log.warning("vault: %s", self.vault_status["error"])

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

    async def away_alert(self, now=None):
        """A model computer away longer than away_alert_hours: one notification, and one when it's back."""
        hours = float(self.opts.get("away_alert_hours", 3) or 0)
        if hours <= 0 or not self.ha:
            return []
        sent = self.__dict__.setdefault("_away_sent", {})
        out = self.models.away_changes(sent, hours, now)
        for what, key, since in out:
            mine = self.models.on_host(key)
            names = ", ".join(mine) or key
            if what == "away":
                when = datetime.fromtimestamp(since).strftime("%a %I:%M %p").replace(" 0", " ")
                others = [m for m in (self.mind.FALLBACK or []) if m not in mine]
                msg = (f"The computer at {key} ({names}) hasn't answered since {when}. Answers are coming from "
                       f"{', '.join(others) or 'the best matching note'} meanwhile; if it runs Voicebox, the house "
                       "voice is Piper until it's back. It may be asleep, off, or stuck on an update.")
                await self.ha.notify("Home Mind: a model computer is away", msg, "home_mind_away")
            else:
                await self.ha.notify("Home Mind: it's back", f"The computer at {key} ({names}) is answering again.",
                                     "home_mind_away")
        return out

    async def tired(self):
        """Is today a tired day (readiness, sleep, fog)? Cached 10 minutes; False when it can't be told quickly."""
        c = getattr(self, "_tired_cache", None)
        if c and time.time() - c[0] < 600:
            return c[1]
        try:
            t = bool((await asyncio.wait_for(self.pantry.today(), 3)).get("tired"))
        except Exception:  # noqa: BLE001
            t = False
        self._tired_cache = (time.time(), t)
        return t

    async def styled(self, res, question="", who=None, voice=False, kind="ask"):
        """The persona's voice and emotion marker on an answer (never on errors; facts checked, see persona.py)."""
        try:
            name = (who or "").strip().title() or None
            if not isinstance(res, dict) or not isinstance(res.get("answer"), str) or res.get("error"):
                return res
            if res.get("red_flag"):
                return res          # a red flag's fixed words are said exactly: no persona, closer or emotion
            if res.get("_streamed"):
                # already streamed live sentence-by-sentence: keep the spoken text intact, just record the emotion
                emotion = self.persona.emotion(res, question, name, await self.tired(), kind)
                res["emotion"] = emotion
                said = self.persona.__dict__.setdefault("_said", {})
                said[self.persona._key(res["answer"])] = emotion
                return res
            from . import frontends as FE
            from .mind import DONT_KNOW
            failed = bool(res.get("error")) or bool(DONT_KNOW.search(res.get("answer") or ""))
            unsure = bool(voice) and not who
            reg = FE.register(self.mind.store, name, getattr(self, "day", None), failed, unsure)
            if reg == "plain":
                return dict(res, register="plain")          # the plain voice: no persona at all
            emotion = self.persona.emotion(res, question, name, await self.tired(), kind)
            if reg == "quiet":
                return dict(self.persona.finish(res, "neutral", voice, name, None), register="quiet")
            by_model = None
            pm = self.opts.get("persona_model")
            if pm and pm in self.models.models and self.models.up(pm) is not False:
                # Jeeves's words from a model (on the GPU computer); slow, asleep or a changed fact: the rules instead
                by_model = await self.persona.style_model(self.models.get(pm), res["answer"], emotion, who=name,
                                                          voice=voice, timeout=1.5 if voice else 4.0)
            return self.persona.finish(res, emotion, voice, name, by_model)
        except Exception as e:  # noqa: BLE001 - styling never costs an answer
            log.warning("persona: %s", e)
            return res

    def panel_person(self, user_id):
        """The person signed in to Home Assistant who opened the panel (ingress sends X-Remote-User-Id), or None."""
        from .speakers import resolve
        try:
            name, _ = resolve(self.people(), user=user_id)
        except Exception:  # noqa: BLE001
            return None
        return name

    def speaker_for(self, b):
        """From a request body: (viewer, how). viewer None = no speaker information given (sees everything) unless
        option unknown_caller is "guest"; "" = unknown; else a name."""
        from .speakers import resolve, VIEWER
        if VIEWER.get() is not None:
            # a token tied to a person (#48): always that person, whatever the body says; a guest token ("") stays a
            # guest: it can't claim a speaker in the body
            return VIEWER.get(), "token" if VIEWER.get() else "guest token"
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
        return await autodraft.draft(self.models.get(name), request, states, services,
                                     pin=str(self.opts.get("automation_pin") or ""))

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
_KINDS = {str: "text", dict: "an object", list: "a list of text", float: "a number"}
TAGS = (list, str)              # tags: a list, or "a, b" (store.clean_tags splits it)


def as_who(b, field):
    """Who a request acts as: a token's person always; a guest token ("") can't name anyone in the body; with no
    token information (admin, the panel without a person) the body's field."""
    from .speakers import VIEWER
    v = VIEWER.get()
    return v if v is not None else (b.get(field) or None)


def check_types(b, types):
    """ValueError (a 400) when a field that is there has the wrong JSON type: str, dict, list (of text) or float (any
    finite number), or a tuple of these. A missing field or null is left to the handler's default."""
    def fits(v, t):
        return (isinstance(v, str) if t is str else isinstance(v, dict) if t is dict else
                isinstance(v, list) and all(isinstance(x, str) for x in v) if t is list else
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v))
    for k, t in types.items():
        v, ts = b.get(k), t if isinstance(t, tuple) else (t,)
        if v is not None and not any(fits(v, x) for x in ts):
            raise ValueError(f"{k} should be {' or '.join(_KINDS[x] for x in ts)}")
    return b


def decision_why(opts, plan, text, source, conversation_id, t0=None):
    """How a planned action was decided, for the decision log: the words, the channel, which model planned it
    (the local control model or a general one) and how long planning took."""
    local = (opts or {}).get("control_model") or ""
    by = plan.get("by")
    return {"request": text, "channel": source, "conversation_id": conversation_id, "model": by,
            "route": "reflex" if by == "reflex" else "local" if local and by == local else "cloud" if by else None,
            **({"records": plan["records"]} if plan.get("records") else {}),
            "latency_ms": round((time.monotonic() - t0) * 1000) if t0 else None,
            **({"confidence": plan["confidence"]} if plan.get("confidence") is not None else {})}


async def read_body(req, optional=False, **types):
    """The request's JSON body, which must be an object (a list, a string or no JSON at all is a 400). optional: no
    body (or null) is {}. types: field=str/dict/list/float, see check_types."""
    if optional and not req.can_read_body:
        b = {}
    else:
        try:
            b = await req.json()
        except ValueError:
            raise ValueError("send a JSON object as the body") from None
        if b is None and optional:
            b = {}
    if not isinstance(b, dict):
        raise ValueError("send a JSON object as the body")
    return check_types(b, types)


def num(v, name, default, lo, hi, cast=int):
    """A number from a query parameter or body field, clamped to [lo, hi]: missing or empty is the default; text that
    isn't a number, nan and inf are a 400."""
    if v is None or v == "":
        return default
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} should be a number") from None
    if not math.isfinite(x):
        raise ValueError(f"{name} should be a number")
    return cast(min(hi, max(lo, x)))


def whole(v, name="id"):
    """A whole number id from a path or body (an int, or digits): 1.5, true or 'abc' are a 400, never rounded to
    another item's id."""
    if isinstance(v, bool) or not isinstance(v, (int, str)) or not re.fullmatch(r"\s*-?\d{1,19}\s*", str(v)):
        raise ValueError(f"{name} should be a whole number")
    return int(v)


def build(app_state):
    @web.middleware
    async def auth(request, handler):
        if request.method == "OPTIONS":
            return web.Response(status=204)
        peer = request.remote or ""
        tok = request.headers.get("X-Home-Mind-Token") or request.headers.get("Authorization", "").removeprefix("Bearer ")
        if peer in INGRESS_IPS or (tok and secrets.compare_digest(tok.encode("utf-8", "surrogateescape"),
                                                                  str(app_state.token).encode())):
            request["scope"] = "admin"
            uid = request.headers.get("X-Remote-User-Id") if peer in INGRESS_IPS else None
            who = app_state.panel_person(uid) if uid and hasattr(app_state, "panel_person") else None
            if who:                 # the panel, signed in to Home Assistant as a known person: answered as them
                from .speakers import VIEWER
                tv = VIEWER.set(who)
                try:
                    return await handler(request)
                finally:
                    VIEWER.reset(tv)
            return await handler(request)
        rec = app_state.tokens.check(tok) if tok else None
        if rec:                     # a scoped token (#27)
            from .tokens import needed, allows
            need = needed(request.method, request.path)
            if not allows(rec["scope"], need):
                return web.json_response({"error": f"this token ({rec['name']}) can {rec['scope']}; this needs {need}"},
                                         status=403)
            request["scope"], request["token"] = rec["scope"], rec
            if rec.get("person") or rec["scope"] != "admin":
                # answered as that person (#48); a read/write token tied to no one is a guest (household notes only)
                from .speakers import VIEWER
                tv = VIEWER.set(rec.get("person") or "")
                try:
                    return await handler(request)
                finally:
                    VIEWER.reset(tv)
            return await handler(request)
        if os.environ.get("HOME_MIND_OPEN") == "1" and peer in ("127.0.0.1", "::1"):
            return await handler(request)
        return web.json_response({"error": "not allowed: send the token from /data/token, or one made on the Models tab "
                                           "(Access tokens), as header X-Home-Mind-Token" if not tok else
                                           "not allowed: that token is unknown or was revoked"}, status=401)

    @web.middleware
    async def errors(request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except KeyError as e:                           # b["question"] with no question: say which field
            msg = str(e.args[0]) if e.args else "missing field"
            return web.json_response({"error": f"missing {msg}" if re.fullmatch(r"\w+", msg) else msg}, status=400)
        except (ValueError, OverflowError) as e:        # bad input (OverflowError: a number too big for SQLite)
            return web.json_response({"error": str(e)}, status=400)
        except PermissionError as e:                    # allowed in general, not this (a guest changing personal data)
            return web.json_response({"error": str(e)}, status=403)
        except ModelError as e:                         # the model behind it failed: not this server's bug
            return web.json_response({"error": str(e)}, status=502)
        except Exception as e:  # noqa: BLE001
            log.exception("request failed")
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=500)

    A, st = app_state, app_state.mind.store
    from .speakers import VIEWER, visible, sql as vis_sql
    from .tokens import allows
    seen_id = lambda i: bool(i) and st.seen(int(i)) is not None      # noqa: E731 - may the viewer see this item (#48)
    J = lambda x, status=200: web.json_response(x, status=status, dumps=lambda o: json.dumps(o, default=str))
    r = web.RouteTableDef()

    def model_ok(b):
        """An unknown model name is the caller's mistake (400), not a model failure."""
        m = b.get("model")
        if m not in (None, "") and (not isinstance(m, str) or m not in A.models.models):
            raise ValueError(f"no model named {m!r}; have: {', '.join(A.models.models)}")
        return b

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
        res = lookup(st, p.get("q", ""), num(p.get("limit"), "limit", 10, 1, 200), p.get("shelf"), p.get("kind"),
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
            from .pantry import PANTRY_Q
            if PANTRY_Q.search(p.get("q", "")):
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
        b = await read_body(q, text=str, title=str, kind=str, shelf=str, tags=TAGS, key=str, meta=dict, scope=str,
                            owner=str, source=str)
        from .speakers import own_meta
        meta = own_meta(dict(b.get("meta") or {}, **({"inferred": True} if b.get("inferred") else {})))
        if b.get("scope") in ("personal", "household"):
            meta["scope"] = b["scope"]
            if b["scope"] == "personal":
                meta["owner"] = as_who(b, "owner") or A.speaker_for(b)[0] or ""
                if not meta["owner"]:
                    raise ValueError("a personal note needs an owner (a guest can't file one)")
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
        i = whole(q.match_info["id"])
        d = st.seen(i, history=True)
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
        hours = num(q.query.get("hours"), "hours", 48.0, 0, 24 * 365 * 100, float)
        lim = num(q.query.get("limit"), "limit", 100, 1, 500)
        f = q.query.get("filter", "told")
        where = {"told": "source NOT IN ('home','calendar','automations','habits','tidy','evening') AND source NOT LIKE 'pack:%'", "all": "1=1",
                 "calendar": "source='calendar'", "house": "source='home'", "automations": "source='automations'",
                 "archived": "status='archived'"}.get(f, "1=1")
        col = "updated" if f == "archived" else "created"
        vs, va = vis_sql()
        rows = st.db.execute(f"SELECT * FROM items WHERE {where} AND {vs} AND {col} >= ? ORDER BY {col} DESC LIMIT ?",
                             (*va, time.time() - hours * 3600, lim)).fetchall()
        return J([{k: v for k, v in st._row(r).items() if k in ("id", "title", "body", "shelf", "kind", "source",
                                                                "created", "status", "meta")} for r in rows])

    @r.post("/api/item/{id}/{op}")
    async def item_op(q):
        i, op = whole(q.match_info["id"]), q.match_info["op"]
        try:
            st.check_seen(i)
        except KeyError:
            raise web.HTTPNotFound()
        if not st.get(i):                       # missing: not a silent "archived" for an item that isn't there
            return J({"error": f"no item {i}"}, status=404)
        if op == "forget":
            return J(st.set_status(i, "archived"))
        if op == "restore":
            return J(st.set_status(i, "active"))
        if op == "revise":
            b = await read_body(q, title=str, text=str, shelf=str, tags=TAGS, key=str, meta=dict)
            if not any(b.get(k) is not None for k in ("title", "text", "shelf", "tags", "key", "meta")):
                raise ValueError("nothing to change: give title, text, shelf, tags, key or meta")
            from .speakers import own_meta
            return J(st.revise(i, title=b.get("title"), body=b.get("text"), shelf=b.get("shelf"), tags=b.get("tags"),
                               key=b.get("key"), meta=own_meta(b.get("meta"))))
        if op == "sensitive":
            b = await read_body(q)
            d = st.get(i)
            meta = dict(d["meta"] or {}, sensitive=bool(b.get("value")))
            st.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(meta), i))
            st.db.commit()
            st.seal_item(i) if meta["sensitive"] else st.unseal_item(i)
            return J({"id": i, "sensitive": meta["sensitive"]})
        if op == "feedback":
            b = await read_body(q, note=str)
            return J(st.feedback(i, bool(b.get("helpful")), b.get("note", "")))
        if op == "confirm":
            return J(st.confirm(i))
        if op == "photo":                       # #36: multipart file (+ caption)
            if not q.content_type.startswith("multipart/"):
                return J({"error": "send the photo as multipart/form-data (a file, and a caption)"}, status=400)
            rd = await q.multipart()
            data, caption = None, ""
            async for part in rd:
                if part.name == "caption":
                    caption = (await part.text())[:200]
                elif part.filename:
                    data = await part.read(decode=False)
            try:
                return J(await asyncio.to_thread(A.photos.add, i, data, caption))
            except (ValueError, KeyError) as e:
                return J({"error": str(e)}, status=400)
        if op == "keep":                        # #32: leave it on its shelf, don't suggest another
            d = st.get(i)
            m = {k: v for k, v in (d["meta"] or {}).items() if k not in ("suggested_shelf", "suggested_p")}
            m["keep_shelf"] = True
            with st.lock:
                st.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(m), i))
                st.db.commit()
            return J({"id": i, "kept": d["shelf"]})
        if op == "move":           # to another shelf; filing learns from where notes are (#58, #59)
            b = await read_body(q, shelf=str)
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
            b = await read_body(q, scope=str, owner=str)
            d = st.get(i)
            if not d or b.get("scope") not in ("personal", "household"):
                raise ValueError("scope is personal or household")
            m = dict(d["meta"], scope=b["scope"])
            if b["scope"] == "personal":
                m["owner"] = as_who(b, "owner") if VIEWER.get() is not None else \
                    (b.get("owner") or m.get("owner") or m.get("said_by") or "")
                if not m["owner"]:
                    raise ValueError("a personal note needs an owner")
            with st.lock:
                st.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(m), i))
                st.db.commit()
            return J({"id": i, "scope": b["scope"], "owner": m.get("owner")})
        if op == "purge":          # delete for good: needs {"confirm": true}
            b = await read_body(q, optional=True)
            if b.get("confirm") is not True:
                return J({"error": "send {\"confirm\": true} to delete for good (this cannot be undone)"}, status=400)
            return J(st.purge([i]))
        raise web.HTTPNotFound()

    @r.post("/api/purge_about")
    async def purge_about(q):
        b = await read_body(q, name=str)
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

    @r.post("/api/converse")
    async def converse(q):
        """One voice turn for the bridge's conversation agent: {"text", "conversation_id"?, "user_id"?, "device_id"?,
        "area"?, "language"?, "front_end"?}. With Accept: application/x-ndjson the answer streams as events (an
        acknowledgement at 1.5 s, then one sentence at a time); otherwise the events come back together."""
        from . import voice
        b = await read_body(q, text=str, conversation_id=str, user_id=str, device_id=str, area=str, language=str,
                            front_end=str)
        scope = q.get("scope", "admin")
        if "ndjson" in (q.headers.get("Accept") or "") and hasattr(web, "StreamResponse"):
            resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson", "Cache-Control": "no-cache"})
            await resp.prepare(q)
            async for ev in voice.converse(A, b, scope):
                await resp.write(voice.ndjson(ev))
            await resp.write_eof()
            return resp
        events = [ev async for ev in voice.converse(A, b, scope)]
        return J(dict(events[-1], events=events[:-1]))

    @r.post("/api/ask")
    async def ask(q):
        b = model_ok(await read_body(q, question=str, conversation_id=str, speaker=str, user=str, deadline=float,
                                     front_end=str))
        return J(await A.answer(b, q.get("scope", "admin")))

    @r.post("/api/organize")
    async def organize(q):
        b = model_ok(await read_body(q, text=str, model=str))
        if not (b.get("text") or "").strip() or not b.get("model"):
            raise ValueError("give text and model")
        return J(await A.mind.organize(b["text"], b["model"], source="api"))

    @r.get("/api/suites")
    async def suites_(_):
        return J(SUITE_INFO)

    @r.post("/api/tests")
    async def tests(q):
        b = model_ok(await read_body(q, suite=str, arm=str, seed=float, n=float, ingest=str, order=str))
        run = A.mind.start_test(b["suite"], b.get("arm", "brain"), b.get("model") or None, b.get("seed"),
                                b.get("n"), b.get("ingest", "raw"), bool(b.get("keyed")),
                                bool(b.get("expand")), b.get("order") or "default", bool(b.get("notes_first")))
        return J({"id": run["id"], "status": run["status"], "seed": run["seed"]})

    @r.get("/api/runs")
    async def runs(q):
        return J(st.runs(limit=num(q.query.get("limit"), "limit", 50, 1, 500), suite=q.query.get("suite")))

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
                             for x, y in ((st.seen(a), st.seen(b)) for a, b in (st.kv_get("tidy_pairs") or []))
                             if x and y and x["status"] == "active" and y["status"] == "active"]
        out["conflicts"] = [{"a": {k: x[k] for k in ("id", "title")}, "b": {k: y[k] for k in ("id", "title")}, "why": w}
                            for x, y, w in ((st.seen(a), st.seen(b), w) for a, b, w in (st.kv_get("tidy_conflicts") or []))
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
        if q.query.get("now") == "1" and not allows(q.get("scope", "admin"), "write"):
            return J({"error": "tidying now rolls up and archives old logs: this needs a write token"}, status=403)
        pairs = tidy.run(st) if q.query.get("now") == "1" else (st.kv_get("tidy_pairs") or [])
        return J({"pairs": [{"a": st.seen(a), "b": st.seen(b)} for a, b in pairs if seen_id(a) and seen_id(b)]})

    @r.get("/api/cases")
    async def cases_get(_):
        from .cases import Cases
        return J({"cases": Cases(st).all()})

    @r.post("/api/cases")
    async def cases_add(q):
        from .cases import Cases
        b = await read_body(q, question=str, right=str, wrong=str)
        return J({"id": Cases(st).add(b["question"], b["right"], b.get("wrong", ""))})

    @r.post("/api/cases/{id}/remove")
    async def cases_remove(q):
        from .cases import Cases
        i = whole(q.match_info["id"])
        Cases(st).remove(i)
        return J({"removed": i})

    @r.get("/api/usage")
    async def usage(q):
        days = num(q.query.get("days"), "days", 30, 1, 3650)
        return J(dict(A.usage.summary(days), rows=A.usage.rows(days)))

    @r.get("/api/events")
    async def events(q):
        rows = [dict(x) for x in st.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?",
                                                (num(q.query.get("limit"), "limit", 100, 1, 1000),))]
        if VIEWER.get() is not None:    # a person-bound token: not what others asked, nor events about their notes
            rows = [x for x in rows if x["type"] not in PRIVATE_EVENTS and (not x["item_id"] or seen_id(x["item_id"]))]
        return J(rows)

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
        b = await read_body(q, entity_id=str, action=str, source=str)
        if not A.home.catalog:
            await A.home.refresh()
        try:
            return J(await A.act_checked(b["entity_id"], b["action"], b.get("value"), b.get("source", "api"),
                                         confirm=b.get("confirm") is True))
        except (ValueError, PermissionError) as e:
            return J({"error": str(e)}, status=400)

    @r.get("/api/decisions")
    async def decisions_list(q):
        """The decision log: newest first. ?entity_id= ?limit= ?status=done|refused"""
        if A.decisions is None:
            return J({"error": "the decision log isn't running"}, status=503)
        limit = num(q.query.get("limit"), "limit", 50, 1, 500)
        return J({"decisions": A.decisions.recent(limit, q.query.get("entity_id"), status=q.query.get("status"))})

    @r.get("/api/decisions/stats")
    async def decisions_stats(q):
        """Expected versus seen: match, mismatch, unknown and overrides per entity and action. ?days="""
        if A.decisions is None:
            return J({"error": "the decision log isn't running"}, status=503)
        days = num(q.query.get("days"), "days", 0, 0, 3650, float)
        return J(A.decisions.stats(time.time() - days * 86400 if days > 0 else None))

    @r.get("/api/decisions/profile")
    async def decisions_profile(q):
        if A.decisions is None:
            return J({"error": "the decision log isn't running"}, status=503)
        return J({"profile": A.decisions.profile(q.query.get("entity_id") or "")})

    @r.post("/api/decisions/freeze")
    async def decisions_freeze(q):
        """Keep today's test cases under a name (e.g. "v1") for later replays to compare against."""
        b = await read_body(q, name=str)
        if A.decisions is None or not b.get("name"):
            return J({"error": "needs a name, and the decision log running"}, status=400)
        return J({"name": b["name"], "cases": A.decisions.freeze(b["name"])})

    @r.get("/api/as_of")
    async def as_of(q):
        """A keyed fact at a time: ?key=&at= (seconds or ISO date) &by=valid|recorded. Answered for the viewer."""
        p = q.query
        at = p.get("at") or ""
        try:
            t = float(at)
        except ValueError:
            try:
                t = datetime.fromisoformat(at).timestamp()
            except ValueError:
                raise ValueError("at is seconds since 1970 or an ISO date") from None
        row = st.as_of(p.get("key", ""), t, p.get("by") or "valid")
        if row and not seen_id(row["id"]):
            row = None              # someone else's personal note
        return J({"fact": row})

    @r.post("/api/today/checkin")
    async def today_checkin(q):
        """{"state": good|low|rough, "person"?}: the morning check-in from the panel or an app."""
        from .speakers import VIEWER
        b = await read_body(q, state=str, person=str)
        person = as_who(b, "person")
        A.day.set(b.get("state") or "", person, how="panel")
        from .day import REPLIES
        return J({"checkin": b["state"], "answer": REPLIES[b["state"]], "budget": A.day.budget(person)})

    @r.get("/api/checkins")
    async def checkins_get(q):
        """Check-ins by time: ?day=YYYY-MM-DD (default today) and the last ?days= (default 7) as a series."""
        from .checkins import TAGS as CK_TAGS, FOG_WORD
        from .day import today as day_of
        day = q.query.get("day") or day_of()
        try:
            datetime.fromisoformat(day)
        except ValueError:
            raise ValueError("day is YYYY-MM-DD") from None
        days = num(q.query.get("days"), "days", 7, 1, 60)
        return J({"day": day, "entries": A.checkins.entries(day=day), "series": A.checkins.series(days),
                  "tags": CK_TAGS, "fog_words": FOG_WORD, "logs": A.checkins.counts()})

    @r.post("/api/checkins")
    async def checkins_add(q):
        """{"fog"?: 0-5, "energy"?: low|ok|high, "tags"?: [...], "note"?, "at"? (seconds) or "minutes_ago"?}: one
        check-in, as often as how you feel changes. Also answers the day's good / low / rough check-in."""
        b = await read_body(q, fog=float, energy=str, tags=list, note=str, at=float, minutes_ago=float, person=str)
        person = as_who(b, "person")
        e = A.checkins.add(b.get("fog"), b.get("energy"), b.get("tags"), b.get("note") or "", b.get("at"),
                           b.get("minutes_ago"), person)
        try:
            A.day.set(e["state"], person, how="panel")
        except ValueError:
            pass
        from .day import today as day_of
        if e["fog"] is not None and A.ha and A.ha.available and e["day"] == day_of():
            try:                    # the fog helper Steady and the patterns read
                from .patterns import E as PE
                await A.ha.call("input_number", "set_value", {"entity_id": PE["fog"], "value": e["fog"]})
                await A.patterns.refresh(1)
            except Exception:  # noqa: BLE001
                log.info("check-in: fog helper not updated", exc_info=True)
        return J(e)

    @r.get("/api/hub")
    async def hub_get(_):
        """Everything the Home screen shows around the ask bar (hub.py); the room and weather are /api/controls."""
        from . import hub
        return J(await hub.build(A))

    @r.post("/api/todo")
    async def todo_op(q):
        """{"op": add|done|undo, "entity": "todo.…", "item"? (text, for add), "uid"? (for done / undo)}: the Home
        screen's reminders and shopping list, kept in Home Assistant's to-do lists."""
        b = await read_body(q, op=str, entity=str, item=str, uid=str)
        ent = b.get("entity") or "todo.shopping_list"
        if not re.fullmatch(r"todo\.[a-z0-9_]+", ent):
            raise ValueError("entity is a todo.… list")
        if not (A.ha and A.ha.available):
            return J({"error": "Home Assistant is not reachable"}, status=503)
        op = b.get("op")
        if op == "add":
            item = (b.get("item") or "").strip()[:200]
            if not item:
                raise ValueError("say what to add")
            await A.ha.call("todo", "add_item", {"entity_id": ent, "item": item})
        elif op in ("done", "undo"):
            if not b.get("uid"):
                raise ValueError("uid is the item to mark")
            await A.ha.call("todo", "update_item", {"entity_id": ent, "item": b["uid"],
                                                    "status": "completed" if op == "done" else "needs_action"})
        else:
            raise ValueError("op is add, done or undo")
        return J({"ok": True, "op": op, "entity": ent})

    @r.post("/api/day_edit")
    async def day_edit(q):
        """{"day": YYYY-MM-DD, "key": sleep_h|wakeups|hrv|steps_prev, "value": number or null to clear}: fix a reading."""
        b = await read_body(q, day=str, key=str, value=float)
        row = A.patterns.edit(b.get("day") or "", b.get("key") or "", b.get("value"))
        return J({"day": row["day"], "row": row})

    @r.post("/api/log")
    async def quick_log(q):
        """{"kind": water|coffee, "n"?: 1..6 or -1 to take the last one back}: today's counts back."""
        b = await read_body(q, kind=str, n=float, person=str)
        return J(A.checkins.log(b.get("kind") or "", b.get("n") if b.get("n") is not None else 1,
                                as_who(b, "person")))

    def need_measure():
        if getattr(A, "measure", None) is None:
            raise ValueError("measurement isn't running (it needs the decision log)")
        return A.measure

    @r.get("/api/learned")
    async def learned_list(q):
        """What Home Mind has learned about house requests: each template, its status and its evidence."""
        st_ = q.query.get("status")
        return J({"templates": [A.learned.describe(r) for r in A.learned.all(st_)]})

    @r.get("/api/learned/match")
    async def learned_match(q):
        """Which template these words would match (?q=), and whether it would act as a reflex. Never acts."""
        mm = await A.learned.match(q.query.get("q", ""), statuses=("trusted", "provisional", "shadow"))
        if not mm:
            return J({"match": None})
        from .learned import REFLEX_SIM, REFLEX_MARGIN
        return J({"match": A.learned.describe(mm["record"]), "sim": mm["sim"], "margin": mm["margin"],
                  "named": mm["named"], "plan": mm["plan"],
                  "would_act": mm["record"]["status"] == "trusted" and mm["named"] and mm["sim"] >= REFLEX_SIM and
                  mm["margin"] >= REFLEX_MARGIN})

    @r.post("/api/learned/{id}")
    async def learned_set(q):
        """A person's say: {"status": "dropped"} (never use it) or "shadow" (back on trial)."""
        b = await read_body(q, status=str)
        if b.get("status") not in ("dropped", "shadow"):
            raise ValueError("status is dropped or shadow")
        rid = num(q.match_info["id"], "id", 0, 1, 1e12)
        rec = next((x for x in (A.learned, getattr(A, "learned_answers", None)) if x is not None and x.get(rid)), None)
        if rec is None:
            raise ValueError("no such template")
        rec.set_status(rid, b["status"], "set by a person")
        return J(rec.describe(rec.get(rid)))

    @r.get("/api/check")
    async def check_get(q):
        """The cross-check of every store (phase 9): database, forget list, learned records, decision log, snapshots,
        vault key, the last test restore. ?run=1 runs it now."""
        from . import upkeep as U
        if q.query.get("run") or not getattr(A, "store_check", None):
            A.store_check = U.check(A)
        return J(A.store_check)

    @r.post("/api/restore_test")
    async def restore_test_post(_):
        """Try restoring the newest snapshot into a scratch copy, and check it."""
        from . import upkeep as U
        return J(U.restore_test(A.mind.store, A.forget))

    @r.get("/api/outbox")
    async def outbox_get(_):
        """Phone messages in the last 7 days by what happened (sent, held, failed, expired, duplicate), and what's
        waiting now."""
        ob = A.ha.outbox
        return J({"week": ob.stats(), "held": len(ob.take("held")), "failed": len(ob.take("failed"))})

    @r.get("/api/learned/weeks")
    async def learned_weeks(_):
        """Saved weeks of learned records, and the change log of the last two weeks."""
        return J({"weeks": A.learned_history.weeks(), "changes": A.learned_history.changes()})

    @r.post("/api/learned/rollback")
    async def learned_rollback(q):
        """Roll the learned records back to a saved week ({"week": "2026-W46", "confirm": true}); the forget list is
        applied after."""
        b = await read_body(q, week=str)
        if b.get("confirm") is not True:
            raise ValueError("rolling back replaces every learned record: send confirm: true")
        return J(A.learned_history.rollback(b["week"], A.forget))

    @r.get("/api/learned/answers")
    async def learned_answers_list(q):
        """What Home Mind has learned about answering questions: each answer, its note, status and evidence, and how
        often the small-model tie-break was needed (the design's line: under 10%)."""
        la = A.learned_answers
        return J({"answers": [la.describe(x) for x in la.all(q.query.get("status"))], "tiebreak": la.tiebreak_rate()})

    @r.get("/api/modes")
    async def modes_get(_):
        """Model mode, the confidence line, which local models are awake, kinds earned and proposed."""
        mo = A.modes
        return J({"mode": mo.mode(None), "dial": mo.dial(), "local_planner": mo.local_planner(),
                  "local_model": mo.local_chat(), "cloud": mo.cloud(),
                  "warm": {n: mo.is_warm(n) for n in (mo.local_planner(), mo.local_chat()) if n},
                  "earned": sorted(A.mind.store.kv_get(mo.EARNED) or []), "proposed": mo.proposals()})

    @r.post("/api/modes")
    async def modes_set(q):
        """{"mode": cloud|balanced|private, "person"?}"""
        b = await read_body(q, mode=str, person=str)
        return J({"mode": A.modes.set_mode(b.get("mode") or "", b.get("person"))})

    @r.post("/api/modes/accept")
    async def modes_accept(q):
        """Yes to a proposed kind ({"kind": "light.on"}): replayed through the local model first; earned if it
        passes."""
        b = await read_body(q, kind=str)
        local = A.modes.local_planner()
        if not local:
            raise ValueError("no local control model is set (option control_model)")
        return J(await A.modes.accept(b.get("kind") or "", need_measure(),
                                      lambda text: A.shadow_plan(text, local)))

    @r.get("/api/calibration")
    async def calibration_get(_):
        """Confidence against what happened: bins, ECE, and the suggested line."""
        return J(dict(need_measure().calibration(), line=A.modes.dial()))

    @r.get("/api/now")
    async def now_get(_):
        """The now board for the viewer (household entries and their own)."""
        from .speakers import VIEWER
        return J({"now": A.board.snapshot(VIEWER.get())})

    @r.post("/api/now/propose")
    async def now_propose(q):
        """A model's proposed entry ({"kind": waiting_on|unsure_which|doing|note, "value", "ttl"?, "owner"?}),
        checked before it is written."""
        b = await read_body(q, kind=str, value=str, owner=str)
        return J({"id": A.board.propose(b.get("kind") or "", b.get("value") or "", b.get("owner"),
                                        num(b.get("ttl"), "ttl", 3600, 60, 86400))})

    @r.get("/api/worsening")
    async def worsening_get(_):
        """The worsening alarm's numbers (rough days and fog against your own baseline); doesn't speak."""
        from .worsening import check_series
        w = A.worsening
        return J({"rough": check_series(w.series("rough")), "fog": check_series(w.series("fog")),
                  "last_alarm": (w._state() or {}).get("last_alarm")})

    @r.get("/api/scorecard")
    async def scorecard_get(_):
        """The last scorecard (three sections, never averaged) and the trend against v1's first run."""
        m = need_measure()
        last = m.reports("scorecard", "v1", 1)
        return J({"last": last[0] if last else None, "trend": m.trend("v1")})

    @r.post("/api/scorecard/run")
    async def scorecard_run(q):
        """Run the scorecard now (replays the frozen set through the planner; never acts). {"set"?: "v1"}"""
        b = await read_body(q, optional=True, set=str)
        return J(await need_measure().scorecard(lambda text: A.plan_only(text), b.get("set") or "v1"))

    @r.get("/api/labels/queue")
    async def labels_queue(q):
        """This week's labelling queue (about 15 minutes)."""
        return J({"queue": need_measure().queue(num(q.query.get("size"), "size", 30, 1, 200))})

    @r.post("/api/labels")
    async def labels_add(q):
        """{"decision_id", "label": right|wrong|unsure, "correct"?: {"entity_id", "action", "value"}}"""
        b = await read_body(q, label=str, correct=dict)
        from .speakers import VIEWER
        return J(need_measure().label(num(b.get("decision_id"), "decision_id", 0, 1, 1e12), b.get("label") or "",
                                      b.get("correct"), VIEWER.get() or "admin"))

    @r.get("/api/labels/agreement")
    async def labels_agreement(_):
        return J(need_measure().agreement())

    @r.post("/api/labels/gold")
    async def labels_gold(_):
        """Make the gold set (30 labelled cases, re-labelled every quarter) if there isn't one."""
        return J(need_measure().make_gold())

    @r.get("/api/shadow")
    async def shadow_get(q):
        """The shadow baseline: live planner against the other model, win/tie/loss by outcome, cost per correct."""
        return J(need_measure().shadow_report(num(q.query.get("days"), "days", 30, 1, 365)))

    @r.get("/api/burden")
    async def burden_get(q):
        """Per day: asks + corrections + redos, with abstains and requests."""
        return J({"days": need_measure().burden(num(q.query.get("days"), "days", 14, 1, 90))})

    @r.get("/api/timing")
    async def timing_get(q):
        """95th-percentile response time and fallback rate by front end."""
        return J({"timing": need_measure().timing(num(q.query.get("days"), "days", 7, 1, 90))})

    @r.get("/api/safety")
    async def safety_get(_):
        """The last daily safety suite: pass/fail and each check."""
        return J(getattr(A, "safety_result", None) or {"ok": None, "note": "not run yet (it runs 5 minutes after start)"})

    @r.post("/api/safety/run")
    async def safety_run(_):
        from . import safety
        return J(await safety.run_and_report(A))

    @r.get("/api/access")
    async def access_get(_):
        """Device access (none/read/act) and the ladder (ask/tell/do) for every device Home Mind knows, hidden ones
        included, with changes waiting their 24 hours."""
        if not A.home.catalog:
            await A.home.refresh()
        table, lad = A.home.access.table(), A.home.ladder()
        out = []
        for e in sorted(set(A.home.catalog) | set(A.home.hidden)):
            row = {"entity_id": e, "access": A.home.access_of(e), "level": A.home.level(e)[0],
                   "ladder": A.home.rung(e)}
            if (table.get(e) or {}).get("pending"):
                row["pending"] = {"to": table[e]["pending"], "at": table[e]["effective_at"]}
            if e in lad:
                row["ladder_set"] = lad[e]
            out.append(row)
        return J({"devices": out})

    @r.post("/api/access")
    async def access_set(q):
        """{"entity_id", "access": none|read|act} and/or {"ladder": ask|tell|do}. Stricter access applies now,
        looser access in 24 hours; every change is announced."""
        b = await read_body(q, entity_id=str, access=str, ladder=str)
        eid = (b.get("entity_id") or "").strip()
        if not re.fullmatch(r"(?:[a-z_]+\.[a-z0-9_]+|room:[\w .'-]+|group:[a-z_]+\.[a-z0-9_]+)", eid):
            raise ValueError("give an entity_id like lock.front_door, or room:<room> / group:<group entity>")
        out = {"entity_id": eid}
        if b.get("access"):
            from .speakers import VIEWER
            try:
                ch = A.home.access.set(eid, b["access"], by=VIEWER.get() or "admin",
                                       room=(getattr(A.home, "areas", None) or {}).get(eid))
            except PermissionError as e:
                return J({"error": str(e)}, 403)
            out["access"] = ch
            await A.ha.notify("Home Mind: device access changed",
                              f"{eid}: {ch['from'] or 'default'} → {ch['to']} ({ch['when']}).", "home_mind_access")
        if b.get("ladder"):
            out["ladder"] = A.home.set_ladder(eid, b["ladder"])
        await A.home.refresh()
        return J(out)

    @r.post("/api/replay")
    async def replay_run(q):
        """Replay logged requests through the planner (never acts). {"model"?, "limit"?, "frozen"?: name}"""
        from . import replay
        b = model_ok(await read_body(q, frozen=str))
        if A.decisions is None:
            return J({"error": "the decision log isn't running"}, status=503)
        ids = A.decisions.frozen(b["frozen"]) if b.get("frozen") else None
        limit = num(b.get("limit"), "limit", 200, 1, 1000)
        rep = await replay.replay(A.decisions, lambda text: A.plan_only(text, b.get("model")), limit=limit, ids=ids)
        return J(rep)

    @r.post("/api/do")
    async def home_do(q):
        b = model_ok(await read_body(q, text=str, source=str, conversation_id=str, deadline=float, area=str,
                                     device_id=str))
        deadline = b.get("deadline") or (A.mind.VOICE_SECONDS if b.get("voice") else None)
        area = b.get("area") or None
        if not area and b.get("device_id") and A.ha and A.ha.available:
            try:
                area = await asyncio.wait_for(A.home.area_of_device(b["device_id"]), 3)
            except Exception:  # noqa: BLE001
                area = None
        res = await A.do(b.get("text", ""), b.get("model"), b.get("source", "api"),
                         conversation_id=b.get("conversation_id"), deadline=deadline, area=area)
        return J(await styled_(res, b.get("text", ""), A.speaker_for(b)[0], bool(b.get("voice")), "do"))

    @r.get("/api/automations")
    async def autos_list(_):
        return J({"automations": A.engine.summary(), "recent": A.engine.log[-30:]})

    @r.post("/api/automations/{op}")
    async def autos_op(q):
        b = await read_body(q, optional=True, id=str, trigger=str)
        op = q.match_info["op"]
        ids = b.get("ids") or []
        if ids != "all":
            check_types(b, {"ids": list})
        if ids == "all" or b.get("all"):
            ids = [e for e, a in A.engine.autos.items() if a["ha_state"] == "on" or e in A.engine.owned]
        try:
            if op == "takeover":
                return J(await A.engine.takeover(ids))
            if op == "release":
                return J(await A.engine.release(ids or sorted(A.engine.owned)))
            if op == "take_back":
                return J(await A.engine.take_back(ids or None))
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

    @r.get("/api/controls")
    async def controls_get(_):
        from . import home_ctrl
        return J(await home_ctrl.build(A))

    @r.post("/api/controls/{op}")
    async def controls_op(q):
        from . import home_ctrl
        b = await q.json() if q.can_read_body else {}
        op = q.match_info["op"]
        if not (A.ha and A.ha.available):
            return J({"error": "Home Assistant is not reachable"}, status=503)
        try:
            if op == "act":
                return J(await home_ctrl.act(A, b.get("entity", ""), b.get("service", ""), b.get("data") or {}))
            if op == "scene":
                return J(await home_ctrl.scene(A, b.get("name", "")))
            if op == "routine":
                ent = b.get("entity", "")
                if ent not in {r["entity"] for r in home_ctrl.ROUTINES}:
                    raise ValueError(f"{ent} is not one of the routines on the Home tab")
                return J(await (A.engine.takeover([ent]) if b.get("on") else A.engine.retire([ent])))
        except ValueError as e:
            return J({"error": str(e)}, status=400)
        return J({"error": f"unknown op {op}"}, status=404)

    @r.get("/api/today")
    async def today_get(_):
        from . import today
        return J(await today.build(A))

    @r.get("/api/fitness")
    async def fitness_get(_):
        return J(A.fitness.status())

    @r.get("/api/fitness/review")
    async def fitness_review(_):
        return J({"lines": A.fitness.review(), "suggestions": A.fitness.suggestions()})

    @r.get("/api/fitness/logs")
    async def fitness_logs(q):
        days = int(num(q.query.get("days"), "days", 30, 1, 3650))
        return J({"logs": A.fitness.logs(q.query.get("kind") or "", days)})

    @r.get("/api/fitness/workouts")
    async def fitness_workouts(q):
        try:
            days = max(1, min(3650, int(q.query.get("days", "90"))))
        except ValueError:
            raise ValueError("days must be a number") from None
        ex = q.query.get("exercise")
        rows = A.fitness.workouts(days)
        if ex:
            from .fitness import exercise_name
            rows = [w for w in rows if w["entry"]["exercise"] == exercise_name(ex)]
        return J({"workouts": rows, "records": A.fitness.records(), "stalls": A.fitness.stalls()})

    @r.post("/api/fitness/{op}")
    async def fitness_op(q):
        op = q.match_info["op"]
        if op == "goals":
            from .fitness import GOALS
            b = await read_body(q, optional=True, **{k: (float, str) for k in GOALS})
            return J({"goals": A.fitness.set_goals(b)})
        if op == "workout":
            b = await read_body(q, text=str, at=float, scope=str, speaker=str)
            who = A.speaker_for(b)[0]
            meta = {"said_by": who, "scope": "personal", "owner": who} if who and b.get("scope") != "household" else {}
            return J(A.fitness.log(b.get("text") or "", when=b.get("at"),
                                   source="api", meta=meta))
        if op in ("edit", "delete"):
            b = await read_body(q, kind=str, id=float, text=str)
            if b.get("id") is None:
                raise ValueError("give id")
            if op == "delete":
                return J(A.fitness.delete_log(b.get("kind") or "", int(b["id"])))
            return J(A.fitness.edit_log(b.get("kind") or "", int(b["id"]), b.get("text") or ""))
        if op == "accept":
            b = await read_body(q, index=float)
            sug = [s for s in A.fitness.suggestions() if s.get("set")]
            i = int(b.get("index") or 0)
            if not 0 <= i < len(sug):
                raise ValueError("no such suggestion (GET /api/fitness/review lists them)")
            return J({"goals": A.fitness.set_goals(sug[i]["set"]), "applied": sug[i]})
        return J({"error": f"unknown {op}"}, status=404)

    async def styled_(res, *a):
        """The persona on an answer, when there is one (an app without it answers plainly)."""
        f = getattr(A, "styled", None)
        return await f(res, *a) if f is not None and getattr(A, "persona", None) is not None else res

    @r.get("/api/persona")
    async def persona_get(_):
        from .persona import EMOTIONS
        names = []
        try:
            names = sorted({(p.get("name") or "").lower() for p in A.people() if p.get("name")})
        except Exception:  # noqa: BLE001
            pass
        return J({"settings": A.persona.settings(), "emotions": EMOTIONS, "prompt": A.persona.prompt(),
                  "examples": A.persona.examples(), "people": A.persona.people(), "known_people": names})

    @r.post("/api/persona")
    async def persona_set(q):
        b = await read_body(q, optional=True, name=str, address=str, wit=str, person=str)
        person = b.pop("person", None)
        return J({"settings": A.persona.set(b, person=person), "people": A.persona.people(),
                  "prompt": A.persona.prompt()})

    @r.get("/api/persona/corpus")
    async def persona_corpus(q):
        """A practice set for training a style model: made-up plain answers per emotion, the persona's
        instructions, examples, and the fact check's rules (for tools/train_jeeves_style.ipynb)."""
        n = int(num(q.query.get("n"), "n", 600, 12, 5000))
        body = {"persona": A.persona.settings()["name"], "prompt": A.persona.prompt(),
                "style_system": A.persona.MODEL_SYSTEM, "examples": A.persona.examples(),
                "items": A.persona.corpus(n)}
        resp = J(body)
        if q.query.get("download"):
            resp.headers["Content-Disposition"] = 'attachment; filename="jeeves_corpus.json"'
        return resp

    @r.get("/api/persona/emotion")
    async def persona_emotion(q):
        t = q.query.get("text") or ""
        if not t.strip():
            raise ValueError("give text")
        emo, how = A.persona.emotion_for(t[:2000])
        return J({"emotion": emo, "from": how})

    @r.post("/api/persona/try")
    async def persona_try(q):
        from .persona import EMOTIONS
        b = await read_body(q, text=str, emotion=str)
        emo = b.get("emotion") or "neutral"
        if emo not in EMOTIONS:
            raise ValueError(f"emotion is one of {', '.join(EMOTIONS)}")
        styled, applied = A.persona.style(b.get("text") or "", emo, who=None, voice=bool(b.get("voice")))
        return J({"plain": b.get("text") or "", "emotion": emo, "styled": styled, "applied": applied,
                  "ssml": A.persona.ssml(styled, emo)})

    @r.get("/api/phrasebook")
    async def phrasebook_get(q):
        st = q.query.get("status") or None
        if st not in (None, "active", "pending", "rejected"):
            raise ValueError("status is active, pending or rejected")
        src = q.query.get("source") or None
        lim = num(q.query.get("limit"), "limit", 100, 1, 5000)
        return J({"stats": A.phrasebook.stats(), "entries": A.phrasebook.entries(st, src, lim)})

    @r.get("/api/phrasebook/export")
    async def phrasebook_export(_):
        return J({"pairs": A.phrasebook.export()})

    def _pb_model(name):
        name = name or A.overnight.pick_model() or (A.mind.FALLBACK[0] if A.mind.FALLBACK else None)
        if not name:
            raise ValueError("no model to use: give model, or set overnight_model")
        return name, A.models.get(name)

    @r.post("/api/phrasebook/{op}")
    async def phrasebook_op(q):
        op, pb = q.match_info["op"], A.phrasebook
        b = await read_body(q, optional=True, said=str, plain=str, id=float, model=str, n=float, limit=float, text=str)
        if op == "add":
            return J(pb.add(b.get("said") or "", b.get("plain") or "", "added"))
        if op in ("approve", "reject", "remove"):
            if b.get("id") is None:
                raise ValueError("give id")
            rid = int(b["id"])
            return J(pb.remove(rid) if op == "remove" else pb.set_status(rid, "active" if op == "approve" else "rejected"))
        if op == "try":
            from .phrasebook import meaning
            t = b.get("text") or ""
            hit = pb.match(t)
            out = {"text": t, "phrasebook": {"entry": hit[0]["id"], "plain": hit[1]} if hit else None,
                   "similar": pb.similar(t, 5)}
            from .sleeplog import parse_sleep
            from .fitness import parse_workout, is_workout_log, describe
            ps = parse_sleep(t)
            out["parsers"] = ps and ps.get("plain") or (f"slept {ps['hours']:g} hours" if ps else None) or \
                (", ".join(describe(e) for e in parse_workout(t)) if is_workout_log(t) else None)
            if b.get("model"):
                name, m = _pb_model(b["model"])
                line = await pb.read(m, t)
                out["model"] = {"name": name, "line": line, "meaning": meaning(line)}
            return J(out)
        if op == "grow":
            name, m = _pb_model(b.get("model"))
            n = int(num(b.get("n"), "n", 10, 1, 30))
            return J(dict(await pb.grow(m, n), model=name))
        if op == "eval":
            name, m = _pb_model(b.get("model"))
            lim = int(num(b.get("limit"), "limit", 40, 1, 500))
            return J(dict(await pb.evaluate(m, lim), model=name))
        return J({"error": f"unknown {op}"}, status=404)

    @r.get("/api/pantry")
    async def pantry_list(q):
        return J({"items": A.pantry.items(q.query.get("status", "in")), "goals": A.pantry.goals(),
                  "week_plan": A.pantry.week_plan(), "week_calendar": A.pantry.week_calendar()})

    @r.post("/api/pantry/{op}")
    async def pantry_op(q):
        op = q.match_info["op"]
        if op == "photo" and q.content_type == "application/json":
            import base64
            b = await read_body(q, image_b64=str, filename=str)   # for Claude forwarding a photo from chat
            if not b.get("image_b64"):
                raise ValueError("give image_b64")
            import binascii
            try:
                base64.b64decode(b["image_b64"], validate=True)
            except binascii.Error:
                raise ValueError("image_b64 isn't base64") from None
            return J(await A.pantry.from_photo(base64.b64decode(b["image_b64"]), b.get("filename") or "photo.jpg"))
        if op == "photo":
            if not q.content_type.startswith("multipart/"):
                return J({"error": "send the photo as multipart/form-data, or JSON with image_b64"}, status=400)
            reader = await q.multipart()
            part = await reader.next()
            data = await part.read(decode=False)
            return J(await A.pantry.from_photo(data, part.filename or "photo.jpg"))
        b = model_ok(await read_body(q, optional=True, text=str, name=str, location=str, source=str, meal=str,
                                     request=str, items=list, slot=str, style=str, kind=str, action=str, grid=dict,
                                     prep_day=str, prep_time=str, include=str, entity=str))
        if not isinstance(b.get("amount", ""), (str, int, float)) or not isinstance(b.get("use_by", ""), (str, int, float)):
            raise ValueError("amount and use_by should be text (or a number)")
        try:
            if op == "add":
                if b.get("text"):
                    return J({"added": [A.pantry.add(n, a, b.get("location"), source=b.get("source", "api"))
                                        for n, a in A.pantry.parse_list(b["text"])]})
                return J(A.pantry.add(b["name"], b.get("amount", ""), b.get("location"), b.get("use_by"),
                                      source=b.get("source", "api")))
            if op == "ate":
                text = b.get("text") or b.get("name") or ""
                out = A.pantry.ate(text, when=meal_time(b.get("when") if isinstance(b.get("when"), str) else None),
                                   source=b.get("source", "api"))
                if out.get("id"):
                    mid = out["id"]
                    A._spawn(f"estimate:{mid}", lambda: A.pantry.estimate(mid, text))   # tracked, cancelled at stop
                return J(out)
            if op == "estimate":     # meals logged today without a protein estimate (or the given ids)
                from . import today
                ids = b.get("ids")
                if ids is not None and not isinstance(ids, list):
                    raise ValueError("ids should be a list of meal ids")
                ids = [whole(i, "ids") for i in ids] if ids else \
                    [r["id"] for r in (await today.build(A))["meals"]
                     if r.get("protein_g") is None or r.get("fiber_g") is None]
                return J({"estimated": [x for x in [await A.pantry.estimate(i) for i in ids] if x]})
            if op in ("used", "tossed"):
                names = [n for n, _ in A.pantry.parse_list(b["text"])] if b.get("text") else [b["name"]]
                return J({"done": [A.pantry.use(n, "used" if op == "used" else "tossed") for n in names]})
            if op == "remove":
                return J(A.pantry.remove(whole(b.get("id"))))
            if op == "suggest":
                return J(await A.pantry.suggest(b.get("meal"), b.get("model"), num(b.get("n"), "n", 3, 1, 10),
                                               b.get("request") or "", viewer=VIEWER.get()))
            if op == "week":
                d_val = whole(b["days"], "days") if b.get("days") is not None else 7
                if not (1 <= d_val <= 7):
                    raise ValueError("days must be between 1 and 7")
                meals_arg = b.get("meals")
                if meals_arg is not None and not isinstance(meals_arg, (str, list)):
                    raise ValueError("meals should be text or a list")
                if isinstance(meals_arg, list) and not all(isinstance(x, str) for x in meals_arg):
                    raise ValueError("meals items should be text")
                return J(await A.pantry.plan_week(days=d_val, meals=meals_arg, request=b.get("request") or "",
                                                  model=b.get("model"), viewer=VIEWER.get(),
                                                  style=b.get("style"), grid=b.get("grid"),
                                                  prep_day=b.get("prep_day"), prep_time=b.get("prep_time")))
            if op == "layout":
                rec = A.pantry.save_week_layout(kind=b.get("kind") or "usual", style=b.get("style") or "twice",
                                                grid=b.get("grid"), prep_day=b.get("prep_day"),
                                                prep_time=b.get("prep_time"))
                return J({"layout": rec, "layouts": A.pantry.week_layouts()})
            if op == "plan_cell":
                if b.get("day_index") is None:
                    raise ValueError("missing day_index")
                d_idx = whole(b["day_index"], "day_index")
                return J(A.pantry.set_plan_cell(d_idx, slot=b.get("slot") or "", action=b.get("action") or "skip"))
            if op == "swap":
                if b.get("day_index") is None:
                    raise ValueError("missing day_index")
                d_idx = whole(b["day_index"], "day_index")
                return J(await A.pantry.swap_meal(d_idx, slot=b.get("slot") or "dinner",
                                                  request=b.get("request") or "", model=b.get("model"),
                                                  viewer=VIEWER.get()))
            if op == "plan_clear":
                return J(A.pantry.clear_week_plan())
            if op == "meal_info":
                if b.get("day_index") is None:
                    raise ValueError("missing day_index")
                d_idx = whole(b["day_index"], "day_index")
                return J(A.pantry.meal_info(d_idx, slot=b.get("slot") or "dinner"))
            if op == "calendar":
                out = await A.pantry.add_to_calendar(include=b.get("include") or "prep",
                                                     entity=b.get("entity"), prep_time=b.get("prep_time"))
                if getattr(A, "calendars", None) is not None and hasattr(A, "_spawn"):
                    A._spawn("cal_sync", A.calendars.sync)
                return J(out)
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
                                       "SELECT * FROM conversations ORDER BY ts DESC LIMIT 10").fetchall()
                                   if VIEWER.get() is None or str(t["speaker"] or "").lower() == VIEWER.get().lower()]})

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
        b = await read_body(q, q=str, sql=str)
        try:
            return J(await asyncio.to_thread(sqlview.query, A.mind.store.path, b.get("q") or b.get("sql")))
        except sqlview.SqlError as e:
            return J({"error": str(e), "views": sqlview.HELP}, status=400)

    @r.get("/api/photo/{name}")
    async def photo_get(q):
        try:
            path, ctype = A.photos.path(q.match_info["name"])
        except (ValueError, KeyError):
            raise web.HTTPNotFound()
        if not seen_id(whole(q.match_info["name"].split("-")[0], "name")):     # a photo of someone else's personal note
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": ctype, "Cache-Control": "private, max-age=86400"})

    @r.delete("/api/item/{id}/photo/{name}")
    async def photo_delete(q):
        return J({"removed": A.photos.remove(st.check_seen(whole(q.match_info["id"])),
                                             q.match_info["name"])})

    @r.get("/api/reshelve")
    async def reshelve_list(q):
        """Notes on a shelf that filing now thinks belong on another (#32); ?run=1 checks now (dry run)."""
        if q.query.get("run") == "1" and st.filer:
            from .filing import reshelve
            res = reshelve(st.filer, apply=False)
            return J({k: [x for x in v if seen_id(x["id"])] for k, v in res.items()})
        vs, va = vis_sql()
        rows = st.db.execute("SELECT * FROM items WHERE status='active' AND shelf != 'inbox' AND "
                             f"meta LIKE '%\"suggested_shelf\"%' AND {vs} ORDER BY id DESC LIMIT 100", va).fetchall()
        return J([{k: v for k, v in st._row(r).items() if k in ("id", "title", "body", "shelf", "meta")} for r in rows])

    @r.get("/api/connections")
    async def connections(_):
        """Telegram (#52), web lookups (#55) and email (#64): on or off, and how the last try went."""
        return J({"telegram": {"on": A.telegram.on, "chats": len(A.telegram.chats), "error": A.telegram.last_error},
                  "web": {"on": A.mind.web.on, "kind": A.mind.web.kind or None},
                  "email": {"on": A.mailbox.on, "folder": A.mailbox.folder, "senders": len(A.mailbox.senders),
                            "last": A.mailbox.last}})

    @r.post("/api/email/check")
    async def email_check(_):
        if not A.mailbox.on:
            return J({"error": "set email_host, email_user and email_password first"}, status=400)
        try:
            return J(await asyncio.to_thread(A.mailbox.run))
        except Exception as e:  # noqa: BLE001
            return J({"error": str(e)[:200]}, status=502)

    @r.get("/api/control_model/dataset")
    async def control_dataset(q):
        """Training data for the small local control model (localctl), written from this house's control list:
        ?part=train|test (JSONL, the format the Colab notebook tools/finetune_functiongemma.ipynb loads),
        ?paraphrase=1 adds wording from the general model (device names and request wording only leave the house)."""
        from . import localctl
        if not A.home.catalog:
            await A.home.refresh()
        d = localctl.build_dataset(A.home.controllable(), phrasebook=A.phrasebook.export())
        part = q.query.get("part") or "train"
        if part not in ("train", "test"):
            return J({"error": "part must be train or test"}, status=400)
        rows = d[part]
        if part == "train" and q.query.get("paraphrase") == "1":
            names = [n for n in A.models.models if n not in ("mock", A.opts.get("control_model"))]
            if names:
                rows = rows + await localctl.paraphrase(A.models.get("haiku" if "haiku" in names else names[0]), rows)
        return web.Response(text=localctl.jsonl(rows), content_type="application/jsonl",
                            headers={"Content-Disposition": f'attachment; filename="home_mind_control_{part}.jsonl"'})

    @r.get("/api/trainer/status")
    async def trainer_status(_):
        """Turnkey small-LLM training status: local GPU worker health, all 3 model targets, Colab links & active jobs."""
        return J(await A.trainer.status())

    @r.get("/api/trainer/dataset/{target}")
    async def trainer_dataset(q):
        """Download a single ready-to-train dataset bundle for target (control | persona | memgraph)."""
        from .trainer import TARGETS
        target = q.match_info["target"]
        if target not in TARGETS:
            return J({"error": "target must be control, persona, or memgraph"}, status=400)
        paraphrase = q.query.get("paraphrase") == "1"
        n = int(num(q.query.get("n"), "n", 600, 12, 5000))
        max_ex = int(num(q.query.get("max_examples"), "max_examples", 500, 20, 5000))
        try:
            bundle = await A.trainer.build_dataset(target, paraphrase=paraphrase, n=n, max_examples=max_ex)
        except ValueError as e:
            return J({"error": str(e)}, status=400)
        if target == "memgraph" and q.query.get("format") == "jsonl":
            text = "\n".join(json.dumps(rec, ensure_ascii=False) for rec in bundle.get("records") or []) + "\n"
            hdrs = {"Content-Disposition": 'attachment; filename="memgraph_train.jsonl"'} if q.query.get("download") else {}
            return web.Response(text=text, content_type="application/jsonl", headers=hdrs)
        resp = J(bundle)
        if q.query.get("download"):
            resp.headers["Content-Disposition"] = f'attachment; filename="{TARGETS[target]["filename"]}"'
        return resp

    @r.post("/api/trainer/start")
    async def trainer_start(q):
        """One-click start: build the dataset and submit a training job to the configured GPU trainer worker."""
        b = await read_body(q, target=str, base_model=str, ollama_name=str)
        target = b.get("target")
        if not target:
            raise ValueError("missing target")
        epochs = whole(b["epochs"], "epochs") if b.get("epochs") is not None else None
        paraphrase = bool(b.get("paraphrase")) if isinstance(b.get("paraphrase"), (bool, int)) else False
        if b.get("paraphrase") is not None and not isinstance(b.get("paraphrase"), bool):
            raise ValueError("paraphrase must be true or false")
        try:
            return J(await A.trainer.start(target, base_model=b.get("base_model"), epochs=epochs,
                                           paraphrase=paraphrase, ollama_name=b.get("ollama_name")))
        except ValueError as e:
            return J({"error": str(e)}, status=400)
        except RuntimeError as e:
            return J({"error": str(e)}, status=502)

    @r.post("/api/trainer/cancel")
    async def trainer_cancel(q):
        """Cancel a running training job on the GPU trainer worker."""
        b = await read_body(q, job_id=str)
        if not b.get("job_id"):
            raise ValueError("missing job_id")
        try:
            return J(await A.trainer.cancel(b["job_id"]))
        except ValueError as e:
            return J({"error": str(e)}, status=400)
        except RuntimeError as e:
            return J({"error": str(e)}, status=502)

    @r.get("/api/preferences")
    async def preferences(_):
        names = {e: c["name"] for e, c in A.home.catalog.items()}
        return J([dict(p, name=names.get(p["entity_id"], p["entity_id"])) for p in A.prefs.learned()])

    @r.get("/api/memgraph/graph")
    async def memgraph_graph(q):
        """Viewer-scoped visual graph of MemGraph nodes, links, live stability, episodes, and optional note links."""
        include_dormant = q.query.get("dormant", "1") not in ("0", "false", "no")
        include_notes = q.query.get("notes", "0") in ("1", "true", "yes")
        cat = (q.query.get("category") or "").strip() or None
        lim = int(num(q.query.get("limit"), "limit", 120, 1, 300))
        return J(A.memgraph.snapshot(viewer=VIEWER.get(), include_dormant=include_dormant,
                                     category=cat, limit=lim, include_notes=include_notes))

    @r.post("/api/memgraph/link/{id}/{op}")
    async def memgraph_link_op(q):
        """Confirm, hide (forget), or delete a single MemGraph link from the Brain Graph visualizer."""
        lid = whole(q.match_info["id"], "id")
        op = q.match_info["op"]
        if op not in ("confirm", "forget", "delete"):
            return J({"error": "op must be confirm, forget, or delete"}, status=400)
        try:
            if op == "confirm":
                return J(A.memgraph.confirm_link(lid, viewer=VIEWER.get()))
            return J(A.memgraph.forget_link(lid, viewer=VIEWER.get(), delete=(op == "delete")))
        except PermissionError as e:
            return J({"error": str(e)}, status=403)
        except KeyError as e:
            return J({"error": str(e)}, status=404)

    @r.get("/api/thing")
    async def thing(q):
        """Everything known about one thing or place (#20), for the page a QR label opens (#63)."""
        from . import profiles
        name = (q.query.get("name") or "").strip()[:80]
        if not name:
            return J({"error": "give a name"}, status=400)
        return J({"name": name, "known": profiles.picture(st, name.lower(), time.time()),
                  "notes": [{k: h.get(k) for k in ("id", "title", "body", "shelf", "meta")}
                            for h in st.recall(name, limit=10, touch=False, quiet=True)]})

    @r.get("/api/label")
    async def label(q):
        """A QR label (SVG) for a thing, place or shelf (#63). url: where scanning it should go (the panel works it
        out; option label_url overrides), text: what's printed under it."""
        from . import qr
        from urllib.parse import quote
        text = (q.query.get("text") or q.query.get("name") or "").strip()[:60]
        base = A.opts.get("label_url") or q.query.get("base") or ""
        if not text or not base:
            return J({"error": "give text and base (or set label_url)"}, status=400)
        url = base + ("&" if "?" in base else "?") + "hm_thing=" + quote(text)
        try:
            return web.Response(text=qr.svg(url, scale=num(q.query.get("scale"), "scale", 6, 1, 40), label=text),
                                content_type="image/svg+xml")
        except ValueError as e:
            return J({"error": str(e)}, status=400)

    @r.get("/api/views")
    async def views_list(_):
        return J(A.views.all())

    @r.post("/api/views")
    async def views_save(q):
        b = await read_body(q, name=str, query=str, shelf=str, kind=str, tags=TAGS, days=float, sort=str, limit=float,
                            sql=str)
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
        b = await read_body(q, optional=True, seed=float)
        if A.compare.running or not A._spawn("compare", lambda: A.compare.run(b.get("seed"))):
            return J({"started": False, "error": "a comparison is already running"})
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
        b = await read_body(q, name=str, text=str)
        if not (b.get("name") or "").strip():
            raise ValueError("give a name")
        return J(A.docs.add(b["name"], (b.get("text") or "").encode()))

    @r.delete("/api/docs/{did}")
    async def docs_remove(q):
        return J({"removed": A.docs.remove(q.match_info["did"])})

    @r.get("/api/inbox")
    async def inbox_get(_):
        """Notes in the inbox, newest first, with the shelf filing would suggest (#59)."""
        st = A.mind.store
        vs, va = vis_sql()
        rows = [st._row(r) for r in st.db.execute(
            f"SELECT * FROM items WHERE shelf='inbox' AND status='active' AND kind != 'procedure' AND {vs} "
            "ORDER BY created DESC LIMIT 100", va)]
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
        b = await read_body(q, name=str, scope=str, person=str)
        return J(A.tokens.make(b.get("name", ""), b.get("scope", "read"), b.get("person"), b.get("days")))

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
        b = await read_body(q, optional=True)
        r = await A.suggestion(q.match_info["sid"], q.match_info["op"], enable=bool(b.get("enable")))
        if r is None:
            raise web.HTTPNotFound()
        return J(r, status=400 if r.get("problems") else 200)

    @r.post("/api/automations/draft")
    async def automation_draft(q):
        b = model_ok(await read_body(q, request=str))
        return J(await A.draft_automation(b.get("request") or "", b.get("model")))

    @r.post("/api/automations/install")
    async def automation_install(q):
        b = await read_body(q, config=dict, pin=str)
        enable = bool(b.get("enable"))
        r = await A.install_automation(b.get("config"), False, b.get("pin") or "")
        if enable and r.get("installed") and r.get("entity_id"):
            # a new behaviour: installed switched off; on 24 hours after a yes on a clear day (consent.py)
            from .speakers import VIEWER
            c = A.consent.propose("automation_on", f"Switch on: {(b.get('config') or {}).get('alias') or r['entity_id']}",
                                  {"entity_id": r["entity_id"]}, VIEWER.get() or None)
            r = dict(r, consent=A.consent.yes(c["id"]))
        return J(r, status=400 if r.get("problems") else 200)

    @r.get("/api/consent")
    async def consent_list(q):
        """Changes waiting for a yes, agreed (taking effect 24 hours on), done or cancelled; and whether today is a
        clear day for whoever is asking."""
        from .speakers import VIEWER
        ok, why = A.consent.clear_day(VIEWER.get() or None)
        return J({"changes": A.consent.list(q.query.get("status")), "clear_day": ok, "why": why or None})

    @r.post("/api/consent/{id}")
    async def consent_answer(q):
        """{"answer": "yes" | "no" | "changed my mind"} for one change."""
        from .speakers import VIEWER
        b = await read_body(q, answer=str)
        cid = num(q.match_info["id"], "id", 0, 1, 1e12)
        a = (b.get("answer") or "").strip().lower()
        if a == "yes":
            return J(A.consent.yes(cid, VIEWER.get() or None))
        if a == "no":
            return J(A.consent.no(cid))
        if a in ("changed my mind", "change my mind", "undo"):
            return J(A.consent.change_mind(cid))
        raise ValueError("answer is yes, no or changed my mind")

    @r.get("/api/surprises")
    async def surprises_get(q):
        """The surprises and gaps queue (step 24): missed predictions, failed actions, corrections, requests nothing
        could do, questions the notes couldn't answer (?kind), with counts for the last week."""
        sp = A.surprises
        return J({"open": sp.open(q.query.get("kind")), "week": sp.counts()})

    @r.post("/api/surprises/{id}/seen")
    async def surprises_seen(q):
        A.surprises.seen(num(q.match_info["id"], "id", 0, 1, 1e12))
        return J({"ok": True})

    @r.get("/api/backoff")
    async def backoff_get(_):
        """Devices Home Mind's own automations are leaving alone because a person changed them (step 25)."""
        return J(A.backoff.table())

    @r.get("/api/spaces")
    async def spaces_get(_):
        """Personal spaces: rooms whose devices only their person can open up (device access)."""
        return J(A.home.access.spaces())

    @r.post("/api/spaces")
    async def spaces_set(q):
        """{"room": "bedroom", "person": "Dana"} (or person empty to clear). Only that person, or the admin while it
        has no owner, can set it."""
        from .speakers import VIEWER
        b = await read_body(q, room=str, person=str)
        room = (b.get("room") or "").strip().lower()
        if not room:
            raise ValueError("give a room")
        owner = A.home.access.spaces().get(room)
        me = VIEWER.get()
        if owner and me and me.lower() != owner.lower():
            return J({"error": f"that's {owner}'s space"}, 403)
        A.home.access.set_space(room, (b.get("person") or "").strip())
        return J(A.home.access.spaces())

    @r.get("/api/things")
    async def things_list(_):
        """Things Home Mind keeps track of, things noticed in notes waiting for a yes, and look-alikes to merge."""
        th = A.things
        return J({"things": th.all(), "noticed": th.noticed()[:30], "duplicates": th.duplicates()})

    @r.get("/api/things/{id}")
    async def thing_get(q):
        """One thing with its links (only to notes you may see) and where it is."""
        from .speakers import VIEWER
        tid = num(q.match_info["id"], "id", 0, 1, 1e12)
        t = A.things.get(tid)
        if not t:
            return J({"error": "no such thing"}, 404)
        return J(dict(t, links=A.things.links(tid, viewer=VIEWER.get()), where=A.things.where(tid, VIEWER.get())))

    @r.post("/api/things")
    async def thing_add(q):
        """Say yes to a thing ({"name"}), say no to one noticed ({"name", "dismiss": true}), merge look-alikes
        ({"keep", "merge"}), or link two ({"a", "b", "relation": about|in|part_of|belongs_to})."""
        b = await read_body(q, name=str, relation=str)
        th = A.things
        if b.get("merge") is not None:
            return J(th.merge(int(b["keep"]), int(b["merge"])))
        if b.get("relation"):
            th.link(int(b["a"]), int(b["b"]), b["relation"])
            return J({"ok": True})
        if not (b.get("name") or "").strip():
            raise ValueError("give a name")
        if b.get("dismiss"):
            th.dismiss(b["name"])
            return J({"dismissed": b["name"]})
        tid = th.create(b["name"])
        th.link_notes()
        return J(th.get(tid))

    @r.get("/api/scenes")
    async def scenes_list(_):
        """Kept scenes, previews running, and how each scene has done (edits; still kept after a week)."""
        sc = A.scenes
        return J({"scenes": sc.saved(), "previews": {k: v["until"] for k, v in sc.previews.items()},
                  "scores": sc.scores()})

    @r.post("/api/scenes/draft")
    async def scene_draft(q):
        """{"text": "make the living room cozy", "room"?, "preview"?: true}: a drafted scene (never heaters, locks,
        doors or covers); with preview, run for 10 minutes and undone unless kept."""
        b = await read_body(q, text=str, room=str)
        if not (b.get("text") or "").strip():
            raise ValueError("describe the scene")
        sc = await A.scenes.draft(b["text"], b.get("room"))
        if b.get("preview") and sc["steps"]:
            sc["preview"] = await A.scenes.preview(sc)
        return J(sc)

    @r.post("/api/scenes/{name}")
    async def scene_do(q):
        """{"do": "keep"} a preview (optionally with edited "steps"), {"do": "run"} a kept scene, or {"do": "remove"}."""
        b = await read_body(q, do=str)
        name = q.match_info["name"]
        what = b.get("do")
        if what == "keep":
            return J(A.scenes.keep(name, b.get("steps") if isinstance(b.get("steps"), list) else None))
        if what == "run":
            return J(await A.scenes.run(name))
        if what == "remove":
            A.scenes.remove(name)
            return J({"removed": name})
        raise ValueError("do is keep, run or remove")

    @r.get("/api/trackers")
    async def trackers_get(_):
        """Your trackers (at most 3), entries waiting for details on a good day, and whether they're paused."""
        from .speakers import VIEWER
        who = VIEWER.get() or None
        tk = A.tracking
        return J({"trackers": [dict(t, entries=tk.entries(t["id"])) for t in tk.trackers(who)],
                  "to_confirm": tk.to_confirm(who), "paused": tk.paused(who)})

    @r.post("/api/trackers")
    async def trackers_add(q):
        """{"name", "kind"?: scale|yesno|word, "cadence"?: weekly|daily} or {"stop": id} or {"confirm": entry id,
        "details"?}."""
        from .speakers import VIEWER
        b = await read_body(q, name=str, kind=str, cadence=str, details=str)
        tk = A.tracking
        if b.get("stop") is not None:
            tk.stop_tracker(int(b["stop"]))
            return J({"stopped": int(b["stop"])})
        if b.get("confirm") is not None:
            tk.confirm(int(b["confirm"]), b.get("details"))
            return J({"confirmed": int(b["confirm"])})
        tid = tk.add_tracker(b.get("name"), VIEWER.get() or None, b.get("kind") or "scale", b.get("cadence") or "weekly")
        return J({"id": tid})

    @r.get("/api/talk")
    async def talk_get(_):
        """This person's conversation in the panel: {"conversation_id", "turns": [{"who", "text", "choices"?}]}."""
        from .speakers import VIEWER
        return J(A.talk.thread(VIEWER.get()))

    @r.post("/api/talk")
    async def talk_post(q):
        """One turn ({"text", "spoken"?}): a house command or a question, answered in this person's conversation,
        with the answers it expects next as choices."""
        from .speakers import VIEWER
        b = await read_body(q, text=str)
        return J(await A.talk.turn(b.get("text") or "", VIEWER.get(), q.get("scope", "admin"),
                                   spoken=b.get("spoken") is True))

    @r.post("/api/talk/new")
    async def talk_new(_):
        from .speakers import VIEWER
        return J(A.talk.new(VIEWER.get()))

    @r.post("/api/answer_pick")
    async def answer_pick(q):
        """{"id", "pick": deep|quick|same}: which was better, the connected answer or the quick one."""
        b = await read_body(q, id=str, pick=str)
        return J(A.answer_pick(b.get("id") or "", b.get("pick") or ""))

    @r.get("/api/answer_picks")
    async def answer_picks(_):
        picks = [p for p in (A.mind.store.kv_get("answer_picks") or []) if p.get("pick")]
        return J({"picked": len(picks), **{k: sum(1 for p in picks if p["pick"] == k) for k in ("deep", "quick", "same")},
                  "recent": picks[-20:]})

    @r.get("/api/issues")
    async def issues_get(_):
        """Known issues: the built-in ones (with their general causes) and your own, with how often each symptom
        was mentioned in the last 30 and 90 days."""
        from .speakers import VIEWER
        from . import connect as CN
        if VIEWER.get() == "":
            return J({"issues": [], "note": "Private: a guest sees none of it."})
        who = VIEWER.get() or None
        mine = A.known_issues.of(who)
        now = time.time()
        out = []
        for k in list(CN.ISSUES) + [x for x in mine if x not in CN.ISSUES]:
            it = CN.ISSUES.get(k, {})
            d90 = A.symptoms.days(k, who, since=now - 90 * 86400)
            d30 = {d: n for d, n in d90.items() if d >= date.fromtimestamp(now - 30 * 86400).isoformat()}
            out.append({"key": k, "name": it.get("name") or k, "general": it.get("general"),
                        "suspects": (mine.get(k) or {}).get("suspects") or [], "yours": k in mine,
                        "days_30": len(d30), "days_90": len(d90)})
        return J({"issues": out})

    @r.post("/api/issues")
    async def issues_post(q):
        """{"name", "words"?, "suspects"?: [...], "source"?: you|doctor}: add an issue, or suspects to one."""
        from .speakers import VIEWER
        if VIEWER.get() == "":
            raise PermissionError("a guest can't add issues")
        b = await read_body(q, name=str, words=str, suspects=list, source=str)
        return J(A.known_issues.add(VIEWER.get() or None, b.get("name") or "", b.get("words"),
                                    b.get("suspects") or [], b.get("source") or "you"))

    @r.delete("/api/issues/{name}")
    async def issues_delete(q):
        from .speakers import VIEWER
        if VIEWER.get() == "":
            raise PermissionError("a guest can't change issues")
        A.known_issues.remove(VIEWER.get() or None, q.match_info["name"])
        return J({"ok": True})

    @r.get("/api/meds")
    async def meds_get(_):
        """Your medicines log (last 30 days). Nobody reviews this. No interaction checks."""
        from .speakers import VIEWER
        from .tracking import MEDS_NOTE
        if VIEWER.get() == "":
            return J({"log": [], "note": "The medicines log is private: a guest sees none of it."})
        return J({"log": A.tracking.meds(VIEWER.get() or None), "note": MEDS_NOTE})

    @r.post("/api/meds/window")
    async def meds_window(q):
        """{"name", "hours"}: how long after a log a second one gets "You logged that at ..."."""
        b = await read_body(q, name=str, hours=float)
        A.tracking.set_window(b["name"], b["hours"])
        return J({"ok": True})

    @r.get("/api/significant")
    async def events_get(_):
        from .speakers import VIEWER
        return J({"week": A.tracking.week(), "events": A.tracking.events(VIEWER.get() or None)})

    @r.post("/api/significant")
    async def events_post(q):
        """This week's checklist: {"a trip": "yes", "a cold or infection": "no", ...}; unanswered stays unknown."""
        from .speakers import VIEWER
        b = await read_body(q)
        return J({"events": A.tracking.events_answer(VIEWER.get() or None, b)})

    @r.get("/api/nudges")
    async def nudges_get(_):
        return J({"nudges": A.tracking.nudges(active=False)})

    @r.post("/api/nudges")
    async def nudges_add(q):
        """{"goal", "trigger": {"type": time|arrive|leave|nothing_logged_by|sensor_on, ...}, "message"}."""
        from .speakers import VIEWER
        b = await read_body(q, goal=str, trigger=dict, message=str)
        return J({"id": A.tracking.add_nudge(b.get("goal"), b.get("trigger"), b.get("message") or "",
                                            VIEWER.get() or None)})

    @r.post("/api/travel")
    async def travel_post(q):
        """{"on": true}: routine asks, trackers, nudges and experiments pause; the house goes to away where it has an
        away switch."""
        b = await read_body(q)
        on = bool(b.get("on"))
        out = A.tracking.set_travel(on)
        ent = A.opts.get("travel_away_entity") or ("input_boolean.away" if "input_boolean.away" in
                                                   (A.home.catalog or {}) else None)
        if ent and ent.split(".")[0] not in ("input_boolean", "switch"):
            out["away_error"] = "travel_away_entity must be an input_boolean or a switch"
            ent = None
        if ent:
            try:
                await A.ha.call(ent.split(".")[0], "turn_on" if on else "turn_off", {"entity_id": ent}, approved=True)
                out["away"] = ent
            except Exception as e:  # noqa: BLE001
                out["away_error"] = str(e)
        return J(out)

    @r.get("/api/zones")
    async def zones_get(_):
        z = A.zones
        return J({"on": z.on(), "why_off": z.why_off(), "zone": z.zone})

    @r.post("/api/zones/mute")
    async def zones_mute(_):
        """"Not today": no exertion notice for the rest of the day."""
        A.zones.mute_today()
        return J({"muted": True})

    @r.post("/api/experiments/plan")
    async def experiment_plan(q):
        """A written plan ({"question", "change", "kind": reduce_load|sleep_timing|meal_timing, "outcome"?, "pairs"?,
        "block_days"?, "washout_days"?}), checked, and put to consent: it starts 24 hours after a yes on a clear day."""
        from .speakers import VIEWER
        from .experiments import DEFAULT_PAIRS
        b = await read_body(q, question=str, change=str, kind=str, outcome=str, pairs=float, block_days=float,
                            washout_days=float)
        plan = A.experiments.plan(b.get("question") or "", b.get("change") or "", b.get("kind") or "",
                                  b.get("outcome") or "fog", b.get("pairs") or DEFAULT_PAIRS, b.get("block_days") or 7,
                                  b.get("washout_days") if b.get("washout_days") is not None else 2,
                                  person=VIEWER.get() or None, underpowered_ok=b.get("underpowered_ok") is True)
        c = A.consent.propose("experiment_start", f"Experiment: {plan['change']}", plan, VIEWER.get() or None)
        return J({"plan": plan, "consent": c})

    @r.get("/api/experiments")
    async def experiment_get(_):
        e = A.experiments.current()
        return J({"experiment": e, "analysis": A.experiments.analyse() if e else None})

    @r.post("/api/experiments/adherence")
    async def experiment_adherence(q):
        """{"yes": true} for today (or "day": "2026-12-03")."""
        b = await read_body(q, day=str)
        d = date.fromisoformat(b["day"]) if b.get("day") else date.today()
        A.experiments.adhered(d, bool(b.get("yes")))
        return J({"ok": True})

    @r.post("/api/experiments/stop")
    async def experiment_stop(_):
        return J(A.experiments.stop("stopped by you"))

    @r.get("/api/proposals")
    async def proposals_get(_):
        """Gaps seen at least 3 times in two weeks, each with the cheapest fixes first, and any module drafts."""
        return J({"proposals": A.proposals.store.kv_get(A.proposals.KV) or A.proposals.build(),
                  "drafts": A.mind.store.kv_get("proposal_drafts") or {}})

    @r.post("/api/proposals/{topic}")
    async def proposals_set(q):
        """{"status": "done" | "dismissed" | "open"}."""
        b = await read_body(q, status=str)
        if b.get("status") not in ("done", "dismissed", "open"):
            raise ValueError("status is done, dismissed or open")
        A.proposals.set_status(q.match_info["topic"], b["status"])
        return J({"ok": True})

    @r.get("/api/plain/{view}")
    async def views_get(q):
        """The four plain views: today, asks, memory (?topic) and rules."""
        from . import frontends as FE
        from .speakers import VIEWER
        v, who = q.match_info["view"], VIEWER.get() or None
        if v == "today":
            return J(await FE.view_today(A, who))
        if v == "asks":
            return J(FE.view_asks(A, who))
        if v == "memory":
            return J(FE.view_memory(A, VIEWER.get(), q.query.get("topic")))
        if v == "rules":
            return J(FE.view_rules(A))
        return J({"error": "the views are today, asks, memory and rules"}, 404)

    @r.get("/api/assistants")
    async def assistants_get(_):
        """Other assistants set up as front ends, and who has turned them off."""
        return J({"assistants": A.opts.get("other_assistants") or [],
                  "off": A.mind.store.kv_get("assistants_off") or {}})

    @r.post("/api/assistants")
    async def assistants_set(q):
        """{"off": true}: other assistants can't reach Home Mind for you (or for anyone, from the admin)."""
        from . import frontends as FE
        from .speakers import VIEWER
        b = await read_body(q)
        return J(FE.set_assistants(A.mind.store, VIEWER.get() or "_", bool(b.get("off"))))

    @r.post("/api/plain_voice")
    async def plain_voice(q):
        """{"on": true}: answers without the persona, for you."""
        from . import frontends as FE
        from .speakers import VIEWER
        b = await read_body(q)
        return J(FE.set_plain(A.mind.store, VIEWER.get() or "_", bool(b.get("on"))))

    @r.get("/api/guest")
    async def guest_get(_):
        from .channels import guest_on
        return J({"on": guest_on(A.mind.store)})

    @r.post("/api/guest")
    async def guest_post(q):
        """Guest mode on or off ({"on": true}): microphones muted where they can be, nothing answered by voice but
        safety phrases, no transcripts kept."""
        from .channels import set_guest
        b = await read_body(q)
        return J(await set_guest(A, bool(b.get("on"))))

    @r.get("/api/spend")
    async def spend_get(_):
        """This month's cloud spend against the cap (models with prices set)."""
        from .channels import month_spend, over_cap
        cap = float(A.opts.get("monthly_spend_cap") or 0)
        return J({"month": month_spend(A.usage), "cap": cap or None, "over": over_cap(A.usage, cap)})

    @r.get("/api/morning")
    async def morning_preview(q):
        return J({"text": A.morning.build(num(q.query.get("hours"), "hours", 24.0, 1, 24 * 14, float))})

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
        return J({"matches": await asyncio.to_thread(A.knowledge.food, q.query.get("q", ""),
                                                            num(q.query.get("n"), "n", 3, 1, 20))})

    @r.get("/api/knowledge/search")
    async def knowledge_search(q):
        qq = q.query.get("q", "")
        if q.query.get("passages"):
            return J({"passages": await asyncio.to_thread(A.knowledge.passages, qq, num(q.query.get("n"), "n", 2, 1, 20))})
        res = await asyncio.to_thread(A.knowledge.search, qq, num(q.query.get("n"), "n", 3, 1, 20))
        return J({"results": [{"title": x["title"], "intro": (x["text"] or [""])[0][:500]} for x in res]})

    @r.post("/api/knowledge/{op}")
    async def knowledge_op(q):
        op = q.match_info["op"]
        if op in ("food", "wiki"):
            return J({"state": A.knowledge.start_now(op)})
        return J({"error": "unknown"}, status=404)

    @r.get("/api/patterns")
    async def patterns_get(q):
        days = num(q.query.get("days"), "days", 30, 1, 3650)
        rep = A.patterns.report()
        return J({"report": rep["text"], "findings": rep["findings"], "fog_days": rep["fog_days"],
                  "experiment": A.patterns.experiment(), "days": A.patterns.rows(days)})

    @r.post("/api/patterns/{op}")
    async def patterns_op(q):
        op = q.match_info["op"]
        b = await read_body(q, optional=True, factor=str, what=str, action=str)
        P = A.patterns
        if op == "refresh":
            return J({"days": await P.refresh(num(b.get("days"), "days", 3, 1, 365))})
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
            return J(P.start_experiment(f, num(b.get("days"), "days", 14, 1, 365)))
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
        b = model_ok(await read_body(q, optional=True, day=str))
        from datetime import date
        day = date.fromisoformat(b["day"]) if b.get("day") else None
        return J(await A.journal.summarize(day, b.get("model")))

    @r.get("/api/journal")
    async def journal_list(q):
        from datetime import date
        day = date.fromisoformat(q.query["day"]) if q.query.get("day") else datetime.now().date()
        return J({"day": str(day), "entries": [e for e in A.journal.entries(day) if visible(e.get("meta"))]})

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
        b = await read_body(q, room=str, feeling=str, source=str)
        return J(await _room(q, b).vote(b["feeling"], b.get("source", "api")))

    @r.post("/api/comfort/cycle")
    async def comfort_cycle(q):
        b = await read_body(q, optional=True, room=str)
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
        b = await read_body(q, key=str)
        return J(A.control.set(b["key"], b["value"]))

    @r.post("/api/control/reset")
    async def control_reset(q):
        b = await read_body(q, optional=True, key=str)
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
        if not isinstance(msg, (dict, list)) or msg == []:
            return J({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}, 400)
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

    app = web.Application(middlewares=[auth, errors], client_max_size=32 * 1024 * 1024)
    app.add_routes(r)
    app.on_startup.append(A.on_start)
    app.on_cleanup.append(A.on_stop)
    return app


def main():
    state = App()
    # aiohttp waits 60 s by default for requests in flight (a slow model answer); Home Assistant would SIGKILL the
    # add-on long before on_stop ran. Requests get REQUESTS_GRACE s, background tasks TASKS_GRACE s.
    web.run_app(build(state), host="0.0.0.0", port=PORT, access_log=None, shutdown_timeout=REQUESTS_GRACE)


if __name__ == "__main__":
    main()
