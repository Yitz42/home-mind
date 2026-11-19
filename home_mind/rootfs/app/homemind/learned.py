"""Learned records: what Home Mind keeps from what worked, so the next time needs no model (design spec: Records ›
Learned; steps 7, 12, 16, 23 and 26). This first phase learns house requests only.

The store: a thin lifecycle table (learned: id, type, status, version, trust, owner, scope, source, schema_version)
plus one typed table per kind; here the only kind is a template:

  template   a kind of house request and how to do it: one device, one action, and where its number goes
             ("dim the desk lamp to {n}"), with the wordings it was asked in

Statuses:
  shadow     learned, never acts; every house request is checked against it as if it had, and that is its evidence
  provisional its evidence clears the bar; waiting for the nightly gate
  trusted    acts as a reflex: no model call, in milliseconds
  dropped    its evidence says it is wrong more often than right
  retired    its device is gone

Trust: a sceptical prior (as if it had already been wrong 3 times out of 4: Beta(1, 3)), raised only by checked
outcomes (a decision that worked and nobody undid), never by silence. The lower bound of a 90% interval is what is
compared with the bar, so a few lucky matches are never enough. Evidence fades: every 90 days without use halves it.
A trusted template goes back on trial (to shadow) after 2 failures in its last 20 uses, and about 1 in 20 of its
reflexes is re-planned by the model as an audit; a disagreement counts as a failure.

What a template can never do: add a device, an action, a time window or a permission. It is made only from
decisions that already went through every check, only for actions at the open level (no yes needed), and a reflex
still goes through Home.act's checks (control list, device access, approval level, rate limit).

Matching: the request's numbers and any "not" are taken out by rules first (embeddings miss them; a "not" means no
reflex). The rest is compared with each template's wordings by word overlap, and by meaning too when an embedding
model is set (option embed_model). A reflex needs the device named, a close match, and a clear margin over the next
template.

Learning runs every 10 minutes over settled requests: evidence for every template that would have matched, and new
shadow templates (which never act, so this is harmless). The nightly pass makes the one kind of change that can act:
promotions. They are checked by replaying the scorecard's house requests with and without them; if the score drops
they are undone.
"""
import json
import math
import random
import re
import time

from .decisions import wilson

PRIOR_A, PRIOR_B = 1.0, 3.0          # sceptical: as if wrong 3 times out of 4 before any evidence
PROVISIONAL_LO, MIN_EVIDENCE = 0.85, 10
DROP_HI = 0.5
TRIAL_FAILS, TRIAL_WINDOW = 2, 20
HALF_LIFE_DAYS = 90
AUDIT_RATE = 0.05
REFLEX_SIM, REFLEX_MARGIN = 0.6, 0.15
MAX_EXAMPLES = 20
SETTLE_SECONDS = 900                  # an outcome is final after the override window (10 min) and its check
NEG = re.compile(r"\b(?:don'?t|do not|not|never|no longer|stop\s+(?:turning|switching))\b", re.I)
NUM = re.compile(r"\b\d+(?:\.\d+)?\s*%?")
STOP = {"the", "a", "an", "please", "can", "could", "you", "would", "will", "to", "my", "me", "for", "in", "of",
        "it", "and", "now", "just", "hey", "ok", "okay", "at"}
OPEN_ACTIONS = ("on", "off", "toggle", "brightness", "color", "speed", "run")

SCHEMA = """
CREATE TABLE IF NOT EXISTS learned (
  id INTEGER PRIMARY KEY, type TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  trust_a REAL NOT NULL, trust_b REAL NOT NULL, owner TEXT, scope TEXT, source TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, updated REAL NOT NULL, last_used REAL,
  uses INTEGER NOT NULL DEFAULT 0, recent TEXT NOT NULL DEFAULT '[]', note TEXT);
CREATE INDEX IF NOT EXISTS learned_type ON learned(type, status);
CREATE TABLE IF NOT EXISTS learned_templates (
  record_id INTEGER PRIMARY KEY, entity_id TEXT NOT NULL, action TEXT NOT NULL, slot TEXT,
  examples TEXT NOT NULL DEFAULT '[]', vector TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS learned_templates_key ON learned_templates(entity_id, action, slot);
"""


def words(text):
    return [w for w in re.findall(r"[a-z]+", (text or "").lower()) if w not in STOP]


def shape(text):
    """(the words with numbers taken out, the numbers, a "not" in it?)"""
    t = (text or "").replace("’", "'")
    nums = [n.strip().rstrip("%") for n in NUM.findall(t)]
    return NUM.sub(" {n} ", t), nums, bool(NEG.search(t))


def slot_of(plan):
    """Where the request's number goes: brightness/speed/humidity take one, on/off don't."""
    return "n" if (plan.get("action") or "").lower() in ("brightness", "speed", "humidity", "temperature") else None


def overlap(a, b):
    wa, wb = set(words(a)), set(words(b))
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def cosine(a, b):
    if not a or not b:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / ((math.sqrt(sum(x * x for x in a)) or 1) *
                                                (math.sqrt(sum(y * y for y in b)) or 1))


def lower_bound(a, b):
    """The lower end of a 90% interval on the success rate, prior included (a - 1 successes, b - 3 failures)."""
    n = a + b
    return wilson(a, n)[0] if n > 0 else 0.0


class Learned:
    def __init__(self, store, home=None, clock=None, rng=None, embed=None):
        self.store = store
        self.home = home
        self.clock = clock or time.time
        self.rng = rng or random.Random()
        self.embed = embed                  # async texts -> vectors, or None
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()

    # ---- reading ------------------------------------------------------------------------------------------------
    def _rows(self, where="1=1", args=()):
        with self.store.lock:
            rs = self.store.db.execute(
                "SELECT l.*, t.entity_id, t.action, t.slot, t.examples, t.vector FROM learned l "
                f"JOIN learned_templates t ON t.record_id = l.id WHERE l.type='template' AND {where}", args).fetchall()
        out = []
        for r in rs:
            d = dict(r)
            d["examples"] = json.loads(d["examples"] or "[]")
            d["recent"] = json.loads(d["recent"] or "[]")
            d["vector"] = json.loads(d["vector"]) if d.get("vector") else None
            out.append(d)
        return out

    def get(self, rid):
        r = self._rows("l.id=?", (rid,))
        return r[0] if r else None

    def all(self, status=None):
        return self._rows("l.status=?", (status,)) if status else self._rows()

    def trust(self, r):
        """(a, b) with fading: every HALF_LIFE_DAYS without use halves the evidence above the prior."""
        idle = max(0.0, self.clock() - (r.get("last_used") or r["updated"])) / 86400
        f = 0.5 ** (idle / HALF_LIFE_DAYS)
        return PRIOR_A + (r["trust_a"] - PRIOR_A) * f, PRIOR_B + (r["trust_b"] - PRIOR_B) * f

    def describe(self, r):
        a, b = self.trust(r)
        return {"id": r["id"], "status": r["status"], "version": r["version"], "entity_id": r["entity_id"],
                "action": r["action"], "slot": r["slot"], "examples": r["examples"][:5], "uses": r["uses"],
                "right": round(a - PRIOR_A, 1), "wrong": round(b - PRIOR_B, 1),
                "trust_90_low": round(lower_bound(a, b), 3)}

    # ---- matching -------------------------------------------------------------------------------------------------
    async def match(self, text, statuses=("trusted",)):
        """The best template for these words: {"record", "sim", "margin", "plan", "named"} or None. Never for a
        request with a "not" in it."""
        base, nums, neg = shape(text)
        if neg:
            return None
        cands = [r for r in self.all() if r["status"] in statuses]
        if not cands:
            return None
        vec = None
        if self.embed is not None and any(r.get("vector") for r in cands):
            try:
                vec = (await self.embed([base]))[0]
            except Exception:  # noqa: BLE001 - word overlap alone, then
                vec = None
        scored = []
        for r in cands:
            if (r["slot"] == "n") != bool(nums):
                continue                       # "dim to 30" needs a number; "turn on" mustn't have one
            sim = max((overlap(base, e) for e in r["examples"]), default=0.0)
            if vec is not None and r.get("vector"):
                sim = max(sim, cosine(vec, r["vector"]) - 0.25)      # meaning helps, but never alone
            scored.append((sim, r))
        if not scored:
            return None
        scored.sort(key=lambda x: -x[0])
        sim, r = scored[0]
        # the margin is over the best template that would do something else (not another wording of the same)
        rivals = [(s, x) for s, x in scored[1:] if (x["entity_id"], x["action"]) != (r["entity_id"], r["action"])]
        others = [s for s, _x in rivals]
        margin = sim - (others[0] if others else 0.0)
        name = ((self.home.catalog.get(r["entity_id"]) or {}).get("name") if self.home is not None else "") or ""
        # named = every word of the device's name is in the request: "the lamp" could be another lamp
        tw = set(words(text)) | {w[:-1] for w in words(text) if w.endswith("s")}
        nw = set(words(name)) - {"s"}
        named = bool(nw) and nw <= tw
        plan = {"entity_id": r["entity_id"], "action": r["action"], "value": nums[0] if r["slot"] == "n" else None,
                "by": "reflex", "records": [{"id": r["id"], "version": r["version"], "status": r["status"]}]}
        return {"record": r, "sim": round(sim, 3), "margin": round(margin, 3), "plan": plan, "named": named,
                "runner_up": rivals[0][1] if rivals else None, "runner_sim": rivals[0][0] if rivals else 0.0}

    async def reflex(self, text, tiebreak=None):
        """A plan to act on without a model, or None. Trusted, the device named, a close match with a clear margin;
        about 1 in 20 is held back as an audit (the caller plans it the usual way and reports back). Too close to
        call between two trusted templates: tiebreak(text, [said, said]) -> 0, 1 or None may pick (step 12)."""
        m = await self.match(text)
        if not m or not m["named"] or m["sim"] < REFLEX_SIM:
            return None
        self._tie_count("seen")
        if m["margin"] < REFLEX_MARGIN:
            ru = m.get("runner_up")
            if tiebreak is None or ru is None or m["runner_sim"] < REFLEX_SIM:
                return None
            self._tie_count("asked")
            try:
                pick = await tiebreak(text, [self.said(m["record"]), self.said(ru)])
            except Exception:  # noqa: BLE001
                pick = None
            if pick not in (0, 1):
                return None
            if pick == 1:
                base, nums, _neg = shape(text)
                m = dict(m, record=ru, plan={"entity_id": ru["entity_id"], "action": ru["action"],
                                             "value": nums[0] if ru["slot"] == "n" and nums else None, "by": "reflex",
                                             "records": [{"id": ru["id"], "version": ru["version"],
                                                          "status": ru["status"]}]})
            m = dict(m, tiebreak=True)
        if self.rng.random() < AUDIT_RATE:
            return dict(m, audit=True)
        return m

    def said(self, r):
        name = ((self.home.catalog.get(r["entity_id"]) or {}).get("name") if self.home is not None else None) \
            or r["entity_id"]
        return f"{r['action']} the {name}" + (" to a number" if r["slot"] == "n" else "")

    TIE_KEY = "learned_tiebreak_house"

    def _tie_count(self, k):
        c = dict(self.store.kv_get(self.TIE_KEY) or {})
        c[k] = int(c.get(k, 0)) + 1
        self.store.kv_set(self.TIE_KEY, c)

    # ---- writing -----------------------------------------------------------------------------------------------
    def _new(self, entity_id, action, slot, example, source):
        now = self.clock()
        with self.store.lock:
            cur = self.store.db.execute(
                "INSERT INTO learned (type, status, trust_a, trust_b, source, created, updated) VALUES "
                "('template', 'shadow', ?, ?, ?, ?, ?)", (PRIOR_A, PRIOR_B, source, now, now))
            rid = cur.lastrowid
            self.store.db.execute("INSERT INTO learned_templates (record_id, entity_id, action, slot, examples) VALUES "
                                  "(?,?,?,?,?)", (rid, entity_id, action, slot, json.dumps([example])))
            self.store.db.commit()
        return rid

    def check(self, plan):
        """Step 16: a template proposed from a decision must be typed, from the fixed menu, at the open level, and
        for a device Home Mind may move. Returns a reason it isn't allowed, or None."""
        eid, a = plan.get("entity_id") or "", (plan.get("action") or "").lower()
        if not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", eid):
            return "not a device"
        if a not in OPEN_ACTIONS:
            return f"{a!r} isn't something a reflex may do"
        if self.home is not None:
            if eid not in self.home.catalog or not self.home.allowed(eid):
                return "not on the control list"
            if hasattr(self.home, "access_of") and self.home.access_of(eid) != "act":
                return "device access isn't act"
            if hasattr(self.home, "level") and self.home.level(eid, a)[0] != "open":
                return "needs a yes: never a reflex"
        return None

    def add_example(self, r, example):
        ex = [e for e in r["examples"] if e != example] + [example]
        with self.store.lock:
            self.store.db.execute("UPDATE learned_templates SET examples=?, vector=NULL WHERE record_id=?",
                                  (json.dumps(ex[-MAX_EXAMPLES:]), r["id"]))
            self.store.db.commit()

    def evidence(self, r, ok, why=""):
        """One checked outcome for a template. A trusted one with 2 failures in its last 20 goes back on trial."""
        recent = (r["recent"] + [bool(ok)])[-TRIAL_WINDOW:]
        a, b = self.trust(r)
        a, b = (a + 1, b) if ok else (a, b + 1)
        status = r["status"]
        note = None
        if status == "trusted" and len(recent) - sum(recent) >= TRIAL_FAILS:
            status, note = "shadow", f"back on trial: {why or 'two failures in its last 20'}"
            recent = []
        elif status == "shadow" and a + b - PRIOR_A - PRIOR_B >= MIN_EVIDENCE and wilson(a, a + b)[1] < DROP_HI:
            status, note = "dropped", "wrong more often than right"
        elif status == "shadow" and a + b - PRIOR_A - PRIOR_B >= MIN_EVIDENCE and lower_bound(a, b) >= PROVISIONAL_LO:
            status = "provisional"
        now = self.clock()
        with self.store.lock:
            self.store.db.execute("UPDATE learned SET trust_a=?, trust_b=?, recent=?, status=?, updated=?, "
                                  "last_used=?, uses=uses+?, note=COALESCE(?, note) WHERE id=?",
                                  (a, b, json.dumps(recent), status, now, now if ok else r.get("last_used"),
                                   1, note, r["id"]))
            self.store.db.commit()
        if status != r["status"]:
            self._changed(r["id"], status, note)
        return status

    def set_status(self, rid, status, note=None):
        with self.store.lock:
            self.store.db.execute("UPDATE learned SET status=?, updated=?, note=COALESCE(?, note) WHERE id=?",
                                  (status, self.clock(), note, rid))
            self.store.db.commit()
        self._changed(rid, status, note)

    history = None          # upkeep.LearnedHistory: the daily cap on new records

    def _changed(self, rid, status, note=None):
        """The change log (upkeep.py: roll back a bad week)."""
        try:
            self.store.log("learned_change", None, id=rid, status=status, note=note)
        except Exception:  # noqa: BLE001
            pass

    def _room(self, kind):
        return self.history is None or self.history.room_today(kind)

    def retire_missing(self):
        """Templates for devices that are gone (or no longer movable): retired."""
        n = 0
        if self.home is None or not self.home.catalog:
            return 0
        for r in self.all():
            if r["status"] not in ("retired", "dropped") and r["entity_id"] not in self.home.catalog:
                self.set_status(r["id"], "retired", "its device is gone")
                n += 1
        return n

    # ---- learning from the decision log --------------------------------------------------------------------------
    def learn_from(self, dlog, create=True, limit=500):
        """Every settled house request since last time: evidence for each template that would have matched it (did
        it predict what was done, and did that stand?), and, with create, a new shadow template where none fits.
        Returns counts."""
        cur = int(self.store.kv_get("learned_cursor") or 0)
        settle = self.clock() - SETTLE_SECONDS
        with dlog.lock:
            rows = [dlog._row(r) for r in dlog.db.execute(
                "SELECT * FROM decisions WHERE id>? AND ts<? AND status='done' AND request IS NOT NULL AND "
                "request != '' AND (channel IS NULL OR channel != 'automation') AND request NOT LIKE 'undo%' "
                "ORDER BY id LIMIT ?", (cur, settle, limit))]
        out = {"seen": 0, "evidence": 0, "created": 0, "examples": 0}
        for d in rows:
            cur = d["id"]
            if d.get("seen") not in ("match", "mismatch") and not d.get("override"):
                continue                      # unknown or unchecked: no evidence either way
            if d.get("seen") == "mismatch" and not d.get("override"):
                continue                      # the device didn't respond: says nothing about the request
            out["seen"] += 1
            stood = d.get("seen") == "match" and not d.get("override")
            done = {"entity_id": d["entity_id"], "action": (d.get("action") or "").lower()}
            base, nums, neg = shape(d["request"])
            if neg:
                continue
            slot = slot_of(done)
            for r in self.all():
                if r["status"] in ("dropped", "retired"):
                    continue
                if (r["slot"] == "n") != bool(nums):
                    continue
                if max((overlap(base, e) for e in r["examples"]), default=0.0) < REFLEX_SIM:
                    continue                  # it wouldn't have matched: no evidence
                predicted = r["entity_id"] == done["entity_id"] and r["action"] == done["action"]
                if (d.get("records") or []) and any(x.get("id") == r["id"] for x in d["records"]):
                    predicted = True          # it acted itself
                self.evidence(r, predicted == stood, "" if predicted == stood else f"decision {d['id']}")
                out["evidence"] += 1
            if not (create and stood):
                continue
            if self.check(done):
                continue                      # step 16: never a template for what needs a yes
            same = [r for r in self.all() if r["entity_id"] == done["entity_id"] and r["action"] == done["action"]
                    and r["slot"] == slot]
            if same:
                if base.strip() not in same[0]["examples"]:
                    self.add_example(same[0], base.strip())
                    out["examples"] += 1
            elif self._room("template"):
                self._new(done["entity_id"], done["action"], slot, base.strip(), f"decision {d['id']}")
                out["created"] += 1
        self.store.kv_set("learned_cursor", cur)
        return out

    async def embed_missing(self):
        """Vectors for templates whose wordings changed (only with an embedding model)."""
        if self.embed is None:
            return 0
        todo = [r for r in self.all() if r["status"] not in ("dropped", "retired") and not r.get("vector")]
        for r in todo:
            vs = await self.embed(r["examples"])
            if vs:
                c = [sum(col) / len(vs) for col in zip(*vs)]
                with self.store.lock:
                    self.store.db.execute("UPDATE learned_templates SET vector=? WHERE record_id=?",
                                          (json.dumps([round(x, 5) for x in c]), r["id"]))
                    self.store.db.commit()
        return len(todo)

    # ---- the nightly gate ---------------------------------------------------------------------------------------
    async def promote(self, scorecard):
        """Provisional templates become trusted, checked by replay: scorecard(include_ids) -> house rate or None.
        If the rate with them is lower than without, they go back to shadow. Returns what happened."""
        cand = [r["id"] for r in self.all("provisional")]
        if not cand:
            return {"promoted": [], "why": "nothing provisional"}
        before = await scorecard(())
        after = await scorecard(tuple(cand))
        if before is None or after is None:
            return {"promoted": [], "why": "no scorecard to check against yet (it needs 20 real requests)",
                    "waiting": cand}
        if after + 1e-9 < before:
            for rid in cand:
                self.set_status(rid, "shadow", f"replay got worse ({before:.0%} -> {after:.0%})")
            return {"promoted": [], "undone": cand, "before": before, "after": after}
        for rid in cand:
            with self.store.lock:
                self.store.db.execute("UPDATE learned SET status='trusted', version=version+1, updated=?, recent='[]' "
                                      "WHERE id=?", (self.clock(), rid))
                self.store.db.commit()
            self._changed(rid, "trusted", "promoted: the replay held")
        return {"promoted": cand, "before": before, "after": after}

    async def nightly(self, dlog, scorecard):
        """The nightly pass: catch up on learning, retire templates whose device is gone, then the one kind of change
        that can act: promotions, gated by replay."""
        r = self.learn_from(dlog, create=True)
        retired = self.retire_missing()
        await self.embed_missing()
        p = await self.promote(scorecard)
        return {"change": "promotions", "retired": retired, **r, **p}
