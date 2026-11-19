"""Which model answers, and how sure Home Mind is (design spec steps 12 and 13).

Model modes, per person (option model_mode for everyone; each person can set their own):
  cloud     the cloud model (Haiku) plans and answers; the local model runs only as a shadow
  balanced  the local model plans the kinds of house request it has earned (below); everything else goes to the cloud
            model, and the local model's plan for it is kept beside it as a comparison
  private   local only: nothing leaves the house. If the local computer is asleep, Home Mind says so instead of
            using the cloud

Earning a kind of request (domain.action, e.g. light.on): at least 20 local plans judged by what happened, across at
least 10 different wordings, at least 95% right, never a lock, alarm, valve, garage door or heater. Then a person says
yes (a proposal waits for it), and a replay of those requests through the local model must pass too. Two checked
failures in its last 20 demote a kind again.

The local model is used only when it is warm: it answered a ping in the last 90 seconds (checked every 30 s off the
voice path), so a sleeping laptop never makes the person wait.

Confidence (0 to 1), worked out in code for every house plan: how well the words name the device, whether the action
matches what was said, a "don't" or "not" in the request, and how this kind of request has gone before. Below the
line (the dial, option confidence_dial) Home Mind asks the cloud model when the local model planned it, else asks
back ("Just to check: turn on the desk lamp?"). The line is checked against outcomes in /api/calibration.
"""
import asyncio
import json
import logging
import re
import time

log = logging.getLogger("homemind.modes")

MODES = ("cloud", "balanced", "private")
PROMOTE_N, PROMOTE_WORDINGS, PROMOTE_RATE = 20, 10, 0.95
EARN_LO = 0.90          # the lower end of the replay's interval needed to earn a kind (not the point estimate)
DEMOTE_FAILS, DEMOTE_WINDOW = 2, 20
WARM_SECONDS, PING_EVERY = 90, 30
DIAL = 0.55              # default confidence line
NEGATION = re.compile(r"\b(?:don'?t|do not|not|never|no longer|stop\s+(?:turning|switching))\b", re.I)
ON_WORDS = re.compile(r"\b(?:on|start|open|up|brighten|activate|run|play|resume)\b", re.I)
OFF_WORDS = re.compile(r"\b(?:off|stop|close|shut|down|deactivate|pause|kill)\b", re.I)
NUMBER = re.compile(r"\d+\s*%?|\b(?:half|full|max(?:imum)?|min(?:imum)?)\b", re.I)
SAFETY_DOMAINS = ("lock", "alarm_control_panel", "valve", "cover", "climate", "water_heater")


def kind_of(plan):
    a = (plan.get("action") or "").lower()
    a = {"turn_on": "on", "turn_off": "off", "open": "on", "close": "off"}.get(a, a)
    return f"{(plan.get('entity_id') or '').split('.')[0]}.{a}"


def confidence(home, text, plan, dlog=None):
    """(0..1, reasons). Code only; no model is asked."""
    from .store import words
    eid = plan.get("entity_id")
    if not eid or eid not in home.catalog:
        return 0.0, ["no device"]
    reasons = []
    tw = set(words(text)) | {w[:-1] for w in words(text) if w.endswith("s")}
    nw = set(words(home.catalog[eid].get("name") or "")) - {"the", "s"}
    name = len(tw & nw) / len(nw) if nw else 0.0
    if name == 0:
        name = 0.25
        reasons.append("the device isn't named")
    a = (plan.get("action") or "").lower()
    if a in ("on", "open", "run", "turn_on"):
        action = 0.2 if OFF_WORDS.search(text) and not ON_WORDS.search(text) else 1.0
    elif a in ("off", "close", "turn_off"):
        action = 0.2 if ON_WORDS.search(text) and not OFF_WORDS.search(text) else 1.0
    elif a in ("brightness", "speed", "humidity", "temperature"):
        action = 1.0 if NUMBER.search(text) or re.search(r"\b(?:dim|bright|low|high|fast|slow)", text, re.I) else 0.5
    else:
        action = 0.8
    if action < 0.5:
        reasons.append("the action is the opposite of what was said")
    record = 0.8
    if dlog is not None:
        try:
            past = [d for d in dlog.recent(40, entity_id=eid, status="done")
                    if (d.get("action") or "").lower() == a and d.get("seen") in ("match", "mismatch")]
            if len(past) >= 5:
                ok = sum(1 for d in past if d["seen"] == "match" and not d.get("override"))
                record = ok / len(past)
                if record < 0.7:
                    reasons.append("this hasn't gone well before")
        except Exception:  # noqa: BLE001
            pass
    # naming and the track record add up; a wrong-looking action or a "not" can only take away
    c = (0.6 * name + 0.4 * record) * action
    if NEGATION.search(text):
        c *= 0.3
        reasons.append("the request has a 'not' in it")
    return round(max(0.0, min(1.0, c)), 3), reasons


class Warm:
    """Is a local model awake? A cheap ping every PING_EVERY seconds, off the voice path."""

    def __init__(self, models, clock=None):
        self.models = models
        self.clock = clock or time.time
        self.last_ok = {}

    def warm(self, name):
        return bool(name) and self.clock() - self.last_ok.get(name, 0) <= WARM_SECONDS

    def saw(self, name):
        """A real call just worked: as good as a ping."""
        self.last_ok[name] = self.clock()

    async def ping(self, name):
        m = self.models.models.get(name) if hasattr(self.models, "models") else None
        if m is None:
            return False
        url = (getattr(m, "cfg", {}) or {}).get("url") or ""
        session = getattr(self.models, "session", None)
        if not url or session is None:
            return False
        base = url.rstrip("/")
        for path in ("/health", "/models", "/api/tags"):
            try:
                import aiohttp
                async with session.get(base.removesuffix("/v1") + path if path == "/health" else base + path,
                                       timeout=aiohttp.ClientTimeout(total=2)) as r:
                    if r.status < 500:
                        self.saw(name)
                        return True
            except Exception:  # noqa: BLE001 - asleep or unreachable
                continue
        return False

    async def run_forever(self, names):
        while True:
            for n in [x for x in names() if x]:
                try:
                    await self.ping(n)
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(PING_EVERY)


class Modes:
    KV, EARNED, PROPOSED = "model_mode", "earned_kinds", "proposed_kinds"

    def __init__(self, app, store, warm=None, clock=None):
        self.app = app
        self.store = store
        self.warm = warm
        self.clock = clock or time.time

    @property
    def opts(self):
        return getattr(self.app, "opts", None) or {}

    # ---- settings ---------------------------------------------------------------------------------------------
    def mode(self, person=None):
        per = self.store.kv_get(self.KV) or {}
        m = per.get((person or "").lower()) if person else None
        m = m or self.opts.get("model_mode") or "balanced"
        return m if m in MODES else "balanced"

    def set_mode(self, mode, person=None):
        if mode not in MODES:
            raise ValueError(f"mode is one of {', '.join(MODES)}")
        per = dict(self.store.kv_get(self.KV) or {})
        per[(person or "").lower() or "_"] = mode
        self.store.kv_set(self.KV, per)
        return mode

    def dial(self):
        try:
            return float(self.opts.get("confidence_dial") or DIAL)
        except (TypeError, ValueError):
            return DIAL

    def local_planner(self):
        n = self.opts.get("control_model") or ""
        return n if n in getattr(self.app.models, "models", {}) else None

    def local_chat(self):
        n = self.opts.get("local_model") or ""
        return n if n in getattr(self.app.models, "models", {}) else None

    def cloud(self):
        ms = getattr(self.app.models, "models", {}) or {}
        if "haiku" in ms:
            return "haiku"
        local = {self.local_planner(), self.local_chat(), "mock"}
        return next((n for n in ms if n not in local), None)

    def is_warm(self, name):
        return bool(name) and (self.warm is None or self.warm.warm(name))

    def answer_model(self, person=None):
        """The model that writes answers to questions, by mode. None = the notes alone (nothing leaves the house and
        no local model is awake)."""
        mode, lc, cl = self.mode(person), self.local_chat(), self.cloud()
        if mode == "private":
            return lc if self.is_warm(lc) else None
        if mode == "balanced" and lc and self.is_warm(lc) and self.earned("ask"):
            return lc
        return cl or (lc if self.is_warm(lc) else None)

    # ---- earning kinds of request ------------------------------------------------------------------------------
    def earned(self, kind):
        return kind in set(self.store.kv_get(self.EARNED) or [])

    def proposals(self):
        return dict(self.store.kv_get(self.PROPOSED) or {})

    def stats(self, measure):
        """Per kind: the local model's plans judged by what happened (from the comparison pairs)."""
        from .replay import same_action
        dlog = measure.log_
        local = self.local_planner()
        with dlog.lock:
            rows = dlog.db.execute("SELECT * FROM shadow WHERE model=? AND same IS NOT NULL AND decision_id IS NOT "
                                   "NULL ORDER BY at", (local,)).fetchall() if local else []
            acted = dlog.db.execute("SELECT id FROM decisions WHERE route='local' AND status='done' ORDER BY id"
                                    ).fetchall()
        out = {}
        for r in rows:
            plan = json.loads(r["plan"]) if r["plan"] else {}
            cs = measure.cases(ids=[r["decision_id"]])
            if not cs or cs[0]["kind"] == "device":
                continue
            c = cs[0]
            k = kind_of(plan) if plan.get("entity_id") else kind_of(c)
            right = same_action(plan, c) if c["kind"] == "match" else not same_action(plan, c)
            s = out.setdefault(k, {"n": 0, "right": 0, "wordings": set(), "recent": []})
            s["n"] += 1
            s["right"] += int(right)
            s["wordings"].add((c.get("request") or "").strip().lower())
        for (did,) in acted:
            cs = measure.cases(ids=[did])
            if cs and cs[0]["kind"] != "device":
                s = out.setdefault(kind_of(cs[0]), {"n": 0, "right": 0, "wordings": set(), "recent": []})
                s["recent"].append(cs[0]["kind"] == "match" and not cs[0].get("corrected"))
        return out

    def review(self, measure):
        """Propose kinds that meet the bar; demote earned kinds with 2 failures in their last 20. Returns changes."""
        st = self.stats(measure)
        earned = set(self.store.kv_get(self.EARNED) or [])
        prop = self.proposals()
        changes = {"proposed": [], "demoted": []}
        for k, s in st.items():
            dom = k.split(".")[0]
            if k in earned:
                last = s["recent"][-DEMOTE_WINDOW:]
                if len(last) - sum(last) >= DEMOTE_FAILS:
                    earned.discard(k)
                    changes["demoted"].append(k)
                continue
            if dom in SAFETY_DOMAINS or k in prop:
                continue
            if s["n"] >= PROMOTE_N and len(s["wordings"]) >= PROMOTE_WORDINGS and s["right"] / s["n"] >= PROMOTE_RATE:
                prop[k] = {"n": s["n"], "right": s["right"], "wordings": len(s["wordings"]), "at": self.clock()}
                changes["proposed"].append(k)
        self.store.kv_set(self.EARNED, sorted(earned))
        self.store.kv_set(self.PROPOSED, prop)
        return changes

    async def accept(self, kind, measure, planner):
        """A person said yes to a proposal: replay its requests through the local model; earned if it passes."""
        from .measure import gate
        from . import replay as R
        prop = self.proposals()
        if kind not in prop:
            raise ValueError(f"{kind} isn't proposed")
        cs = [c for c in measure.cases() if c["kind"] != "device" and kind_of(c) == kind]
        rep = await R.replay(measure.log_, planner, cases=cs)
        right = rep["agree"] + (rep["override"] - rep["repeats"])
        g = gate(right, rep["match"] + rep["override"], PROMOTE_RATE)
        if g["verdict"] == "fail":
            prop.pop(kind)
            self.store.kv_set(self.PROPOSED, prop)
            return {"kind": kind, "earned": False, "replay": g}
        if not (g["verdict"] == "pass" or (g["rate"] or 0) >= PROMOTE_RATE and (g.get("lo") or 0) >= EARN_LO):
            # a point estimate alone is not enough: the lower end of its interval must clear EARN_LO too (about 25
            # replayed cases, all right); until then it stays proposed
            return {"kind": kind, "earned": False, "replay": g}
        earned = set(self.store.kv_get(self.EARNED) or [])
        earned.add(kind)
        prop.pop(kind)
        self.store.kv_set(self.EARNED, sorted(earned))
        self.store.kv_set(self.PROPOSED, prop)
        return {"kind": kind, "earned": True, "replay": g}
