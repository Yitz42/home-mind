"""The decision log: what Home Mind decided, what it expected to happen, and what actually happened.

Design spec steps 21-23 ("Log the decision", "Outcome, who did it", "Expected versus seen"). Everything later in the
design reads from here: "why" answers, replay before shipping, trust earned from checked outcomes, the scorecard.

One row per decision, in its own file (log.db, next to the main database; old rows are dropped after RETENTION_DAYS
unless kept as test cases):
  - who and how: person, channel (voice, telegram, claude, panel, automation...), source, the words asked, the route
    and model that planned it, confidence, cost, time taken.
  - what: entity, action, value, the Home Assistant service and data, and the context ID Home Assistant gave the
    call, so every state change it caused can be tied back to it. Refused requests are logged too (status refused,
    with the reason), so the log shows what was asked and not done.
  - expected: what the entity should look like if the action worked (state, and an attribute within a tolerance),
    and by when (the device's profile: the usual delay for its kind, longer for a device that is slow in practice).
  - seen: match, mismatch, unknown (unavailable or unreadable: never counted as a failure) or unchecked (nothing to
    check, e.g. a script), with when, by whom (Home Mind's own call, a person, an automation, the device), and the
    delay.
  - override: a change that undoes a matched action within OVERRIDE_SECONDS, made by a person or a device rather
    than by Home Mind. Changes during the quiet period after a restart, and the first report of a device rejoining
    (from unavailable), never count.

Device profiles keep, per entity, how often outcomes matched and how long they took. A device whose outcomes are
mostly unknown (a flaky radio, a cloud integration that doesn't report back) is marked not trusted for learning, so
later steps don't learn from it.
"""
import json
import logging
import math
import sqlite3
import threading
import time

from . import migrations

log = logging.getLogger("homemind.decisions")

RETENTION_DAYS = 180
QUIET_SECONDS = 180          # after a start: state changes are not read as overrides
OVERRIDE_SECONDS = 600       # a person undoing a matched action within this long is an override
GONE = ("unavailable", "unknown", "", None)
# how long an action normally takes to show, by domain (seconds); a device's own history can only lengthen it
WITHIN = {"light": 15, "switch": 15, "input_boolean": 10, "input_number": 10, "input_select": 10, "fan": 30,
          "humidifier": 30, "media_player": 30, "climate": 60, "water_heater": 60, "cover": 120, "lock": 60,
          "valve": 60, "vacuum": 60, "siren": 15, "alarm_control_panel": 60}
WITHIN_DEFAULT, WITHIN_MAX = 30, 300

STEPS = [
    """
    CREATE TABLE decisions (
      id INTEGER PRIMARY KEY,
      ts REAL NOT NULL,
      context_id TEXT,
      person TEXT,
      channel TEXT,
      source TEXT,
      conversation_id TEXT,
      request TEXT,
      route TEXT,
      model TEXT,
      records TEXT NOT NULL DEFAULT '[]',
      confidence REAL,
      cost REAL,
      latency_ms REAL,
      entity_id TEXT,
      action TEXT,
      value TEXT,
      service TEXT,
      data TEXT NOT NULL DEFAULT '{}',
      prior TEXT,
      status TEXT NOT NULL,
      reason TEXT,
      expected TEXT,
      due REAL,
      seen TEXT,
      seen_at REAL,
      seen_state TEXT,
      seen_by TEXT,
      delay REAL,
      override TEXT,
      keep INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX decisions_ts ON decisions(ts);
    CREATE INDEX decisions_entity ON decisions(entity_id, ts);
    CREATE INDEX decisions_context ON decisions(context_id);
    CREATE INDEX decisions_pending ON decisions(seen, due);
    CREATE TABLE profiles (
      entity_id TEXT PRIMARY KEY,
      domain TEXT,
      checks INTEGER NOT NULL DEFAULT 0,
      matches INTEGER NOT NULL DEFAULT 0,
      mismatches INTEGER NOT NULL DEFAULT 0,
      unknowns INTEGER NOT NULL DEFAULT 0,
      overrides INTEGER NOT NULL DEFAULT 0,
      delays TEXT NOT NULL DEFAULT '[]',
      learn INTEGER,
      updated REAL
    );
    CREATE TABLE frozen (name TEXT NOT NULL, decision_id INTEGER NOT NULL, PRIMARY KEY (name, decision_id));
    """,
    # 2 (phase 3): what the device looked like before (brightness, speed...), so "undo" can put it back exactly,
    # and which later decision undid this one
    """
    ALTER TABLE decisions ADD COLUMN prior_attrs TEXT;
    ALTER TABLE decisions ADD COLUMN undone_by INTEGER;
    """,
    # 3 (phase 4, measurement): a person's labels, shadow plans, response times, and counts per day
    """
    CREATE TABLE labels (id INTEGER PRIMARY KEY, decision_id INTEGER NOT NULL, label TEXT NOT NULL, correct TEXT,
                         labeller TEXT, at REAL NOT NULL, is_repeat INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX labels_decision ON labels(decision_id, at);
    CREATE TABLE shadow (id INTEGER PRIMARY KEY, decision_id INTEGER, at REAL NOT NULL, live_model TEXT,
                         model TEXT NOT NULL, plan TEXT, same INTEGER, ms REAL, error TEXT);
    CREATE INDEX shadow_decision ON shadow(decision_id);
    CREATE TABLE timings (id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, front_end TEXT,
                          ms REAL NOT NULL, model TEXT, fallback INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX timings_at ON timings(at);
    CREATE TABLE daily (day TEXT NOT NULL, name TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, name));
    CREATE TABLE reports (id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, name TEXT, report TEXT NOT NULL);
    """,
]


# ---- what should happen --------------------------------------------------------------------------------------------
def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def expect(service, data=None, prior=None):
    """What the entity should look like after service(data) works, or None when nothing can be checked (a script,
    a scene, a toggle from an unknown state, a service this doesn't know).
    {"state_in": [...]} or {"state_not": [...]}, optionally {"attr": name, "value": v, "tol": t} or
    {"number": v, "tol": t} (the state as a number)."""
    if not service or "." not in service:
        return None
    dom, svc = service.split(".", 1)
    data = data or {}
    if dom in ("script", "scene", "automation", "notify", "persistent_notification", "button", "input_button"):
        return None
    if svc == "toggle":
        if dom == "cover":
            flip = {"open": ["closed", "closing"], "closed": ["open", "opening"]}.get(prior)
        else:
            flip = {"on": ["off"], "off": ["on"]}.get(prior)
        return {"state_in": flip} if flip else None
    if dom == "light" and svc == "turn_on":
        e = {"state_in": ["on"]}
        pct = _num(data.get("brightness_pct"))
        if pct is not None:
            if pct <= 0:
                return {"state_in": ["off"]}
            e.update(attr="brightness", value=round(pct * 255 / 100), tol=4)
        return e
    if dom in ("climate", "water_heater") and svc == "turn_on":
        return {"state_not": ["off"]}
    if dom == "media_player":
        if svc == "turn_on":
            return {"state_not": ["off", "standby"]}
        if svc == "turn_off":
            return {"state_in": ["off", "standby"]}
    if svc == "turn_on":
        return {"state_in": ["on"]}
    if svc == "turn_off":
        return {"state_in": ["off"]}
    if dom == "fan" and svc == "set_percentage":
        p = _num(data.get("percentage"))
        if p is None:
            return None
        # fans with a few speeds round to their nearest step (a 3-speed fan shows 33 or 67)
        return {"state_in": ["off"]} if p <= 0 else {"state_in": ["on"], "attr": "percentage", "value": p, "tol": 17}
    if dom == "humidifier" and svc == "set_humidity":
        h = _num(data.get("humidity"))
        return None if h is None else {"attr": "humidity", "value": h, "tol": 1}
    if dom == "climate" and svc == "set_temperature":
        t = _num(data.get("temperature"))
        return None if t is None else {"attr": "temperature", "value": t, "tol": 0.5}
    if dom == "cover":
        return {"open_cover": {"state_in": ["open", "opening"]}, "close_cover": {"state_in": ["closed", "closing"]}
                }.get(svc)
    if dom == "lock":
        return {"lock": {"state_in": ["locked", "locking"]}, "unlock": {"state_in": ["unlocked", "unlocking"]},
                "open": {"state_in": ["open", "opening", "unlocked"]}}.get(svc)
    if dom in ("input_number", "number") and svc == "set_value":
        v = _num(data.get("value"))
        return None if v is None else {"number": v, "tol": 1e-6}
    if dom in ("input_select", "select") and svc == "select_option" and data.get("option") is not None:
        return {"state_in": [str(data["option"])]}
    return None


def satisfied(expected, state):
    """True / False against a state object ({"state", "attributes"}); None when the entity can't say (unavailable,
    unknown, missing), which is never a failure."""
    if not expected or not isinstance(state, dict):
        return None
    st = state.get("state")
    if st in GONE:
        return None
    if "state_in" in expected and st not in expected["state_in"]:
        return False
    if "state_not" in expected and st in expected["state_not"]:
        return False
    if "number" in expected:
        v = _num(st)
        if v is None or abs(v - expected["number"]) > expected.get("tol", 0):
            return False
    if "attr" in expected:
        v = _num((state.get("attributes") or {}).get(expected["attr"]))
        if v is None or abs(v - expected["value"]) > expected.get("tol", 0):
            return False
    return True


def who(ctx, ours, known=None):
    """Who caused a state change, from its Home Assistant context: "home_mind" (this decision's call, or another
    Home Mind decision), "user:<id>" (a person, through Home Assistant), "automation" (something Home Assistant ran)
    or "device" (the device itself or a physical switch: no user, no parent)."""
    ctx = ctx or {}
    cid, parent, user = ctx.get("id"), ctx.get("parent_id"), ctx.get("user_id")
    if ours and (cid == ours or parent == ours):
        return "home_mind"
    if known and (known(cid) or known(parent)):
        return "home_mind"
    if user:
        return f"user:{user}"
    if parent:
        return "automation"
    return "device"


def context_of(response):
    """The context id from Home Assistant's answer to a service call: a list of changed states, or (with
    return_response) {"changed_states": [...], "service_response": ...}. Returns (context id, states)."""
    states = response.get("changed_states") if isinstance(response, dict) else response
    if not isinstance(states, list):
        return None, None
    for s in states:
        cid = ((s or {}).get("context") or {}).get("id") if isinstance(s, dict) else None
        if cid:
            return cid, states
    return None, states


def wilson(k, n, z=1.645):
    """A 90% interval for k successes in n (the scorecard's gates show this, not just the rate)."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def _summary(state):
    if not isinstance(state, dict):
        return None
    a = state.get("attributes") or {}
    keep = {k: a[k] for k in ("brightness", "percentage", "humidity", "temperature") if k in a}
    return json.dumps({"state": state.get("state"), **({"attrs": keep} if keep else {})})


class DecisionLog:
    def __init__(self, path, clock=None, quiet_seconds=QUIET_SECONDS, override_seconds=OVERRIDE_SECONDS):
        self.path = path
        self.clock = clock or time.time
        self.lock = threading.RLock()
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        self.version = migrations.migrate(conn, STEPS, path)
        self.db = conn
        self.started = self.clock()
        self.quiet_until = self.started + quiet_seconds
        self.override_seconds = override_seconds
        self.watch = {}            # entity_id -> {decision id: until}: pending outcomes, then the override window
        with self.lock:
            # outcomes that were still awaited when Home Mind stopped can't be judged now: unknown, never failure
            n = conn.execute("UPDATE decisions SET seen='unknown', seen_by='restart', seen_at=? "
                             "WHERE seen IS NULL AND expected IS NOT NULL", (self.started,)).rowcount
        if n:
            log.info("%d outcomes left unknown by a restart", n)

    # ---- writing --------------------------------------------------------------------------------------------------
    def record(self, entity_id=None, action=None, value=None, service=None, data=None, status="done", reason=None,
               context_id=None, prior=None, states=None, person=None, channel=None, source=None, prior_attrs=None,
               conversation_id=None, request=None, route=None, model=None, records=None, confidence=None, cost=None,
               latency_ms=None):
        """Log one decision. states: the changed states Home Assistant returned with the call (checked at once).
        Returns the decision's id."""
        now = self.clock()
        exp = expect(service, data, prior) if status == "done" and entity_id else None
        due = now + self.within(entity_id) if exp else None
        seen = None if exp else ("unchecked" if status == "done" else None)
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO decisions (ts, context_id, person, channel, source, conversation_id, request, route, "
                "model, records, confidence, cost, latency_ms, entity_id, action, value, service, data, prior, status, "
                "reason, expected, due, seen, prior_attrs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, context_id, person, channel, source, conversation_id, request, route, model,
                 json.dumps(records or []), confidence, cost, latency_ms, entity_id, action,
                 None if value is None else str(value), service, json.dumps(data or {}, default=str), prior, status,
                 reason, json.dumps(exp) if exp else None, due, seen,
                 json.dumps(prior_attrs) if prior_attrs else None))
            did = cur.lastrowid
            if exp:
                self.watch.setdefault(entity_id, {})[did] = due
        for s in states or []:
            if isinstance(s, dict) and s.get("entity_id") == entity_id:
                self.on_state(entity_id, s)
        return did

    def _known(self, cid):
        if not cid:
            return False
        with self.lock:
            return bool(self.db.execute("SELECT 1 FROM decisions WHERE context_id=? LIMIT 1", (cid,)).fetchone())

    def on_state(self, entity_id, new_state, old_state=None, now=None):
        """A state change from Home Assistant (the automation engine's event stream, or a service call's answer)."""
        w = self.watch.get(entity_id)
        if not w or not isinstance(new_state, dict):
            return
        now = now or self.clock()
        with self.lock:
            for did in list(w):
                d = self.db.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
                if d is None:
                    w.pop(did, None)
                    continue
                exp = json.loads(d["expected"]) if d["expected"] else None
                if d["seen"] is None:
                    if satisfied(exp, new_state) is True:
                        by = who(new_state.get("context"), d["context_id"], self._known)
                        self._seen(d, "match", now, new_state, by, now - d["ts"])
                        w[did] = now + self.override_seconds
                    continue
                if d["seen"] != "match" or d["override"] or now > w[did]:
                    w.pop(did, None)
                    continue
                if now < self.quiet_until:
                    continue
                if isinstance(old_state, dict) and old_state.get("state") in GONE:
                    continue        # a device rejoining reports its state: not someone changing it
                by = who(new_state.get("context"), d["context_id"], self._known)
                if by == "home_mind":
                    continue        # Home Mind changing its own mind is not an override
                if satisfied(exp, new_state) is False:
                    ov = {"at": now, "by": by, "to": json.loads(_summary(new_state)), "after": round(now - d["ts"], 1)}
                    self.db.execute("UPDATE decisions SET override=? WHERE id=?", (json.dumps(ov), did))
                    self._profile(d["entity_id"], override=True)
                    w.pop(did, None)
            if not w:
                self.watch.pop(entity_id, None)

    def _seen(self, d, seen, now, state, by, delay):
        self.db.execute("UPDATE decisions SET seen=?, seen_at=?, seen_state=?, seen_by=?, delay=? WHERE id=?",
                        (seen, now, _summary(state), by, None if delay is None else round(delay, 2), d["id"]))
        self._profile(d["entity_id"], seen=seen, delay=delay if seen == "match" and by != "checked" else None)

    async def sweep(self, get_state, now=None):
        """Judge every outcome that is due: read the entity now (get_state: async entity_id -> state or None).
        Returns how many were judged."""
        now = now or self.clock()
        with self.lock:
            due = self.db.execute("SELECT * FROM decisions WHERE seen IS NULL AND due <= ?", (now,)).fetchall()
        n = 0
        for d in due:
            try:
                st = await get_state(d["entity_id"])
            except Exception:  # noqa: BLE001 - can't read it: unknown, not failure
                st = None
            ok = satisfied(json.loads(d["expected"]), st)
            seen = {True: "match", False: "mismatch", None: "unknown"}[ok]
            with self.lock:
                if self.db.execute("SELECT seen FROM decisions WHERE id=?", (d["id"],)).fetchone()["seen"] is not None:
                    continue        # an event judged it while the state was being read
                self._seen(d, seen, now, st, "checked", None)
                w = self.watch.get(d["entity_id"], {})
                if seen == "match":
                    w[d["id"]] = now + self.override_seconds
                else:
                    w.pop(d["id"], None)
            n += 1
        # windows that have closed
        for eid in list(self.watch):
            w = self.watch[eid]
            for did, until in list(w.items()):
                if until is not None and now > until:
                    with self.lock:
                        row = self.db.execute("SELECT seen FROM decisions WHERE id=?", (did,)).fetchone()
                    if row is None or row["seen"] is not None:
                        w.pop(did)
            if not w:
                self.watch.pop(eid)
        return n

    # ---- device profiles ------------------------------------------------------------------------------------------
    def _profile(self, entity_id, seen=None, delay=None, override=False):
        if not entity_id:
            return
        p = self.db.execute("SELECT * FROM profiles WHERE entity_id=?", (entity_id,)).fetchone()
        if p is None:
            self.db.execute("INSERT INTO profiles (entity_id, domain, updated) VALUES (?,?,?)",
                            (entity_id, entity_id.split(".")[0], self.clock()))
            p = self.db.execute("SELECT * FROM profiles WHERE entity_id=?", (entity_id,)).fetchone()
        delays = json.loads(p["delays"])
        if delay is not None:
            delays = (delays + [round(delay, 2)])[-20:]
        col = {"match": "matches", "mismatch": "mismatches", "unknown": "unknowns"}.get(seen)
        counted = f"checks=checks+1, {col}={col}+1, " if col else ""
        self.db.execute(f"UPDATE profiles SET {counted}overrides=overrides+?, delays=?, updated=? WHERE entity_id=?",
                        (1 if override else 0, json.dumps(delays), self.clock(), entity_id))

    def profile(self, entity_id):
        with self.lock:
            p = self.db.execute("SELECT * FROM profiles WHERE entity_id=?", (entity_id,)).fetchone()
        if p is None:
            return None
        d = dict(p)
        d["delays"] = json.loads(d["delays"])
        d["learnable"] = self.learnable(entity_id, d)
        return d

    def learnable(self, entity_id, p=None):
        """Trusted for learning: set by hand (learn 1/0), else yes unless 10+ checks were mostly unknown."""
        p = p or self.profile(entity_id)
        if not p:
            return True
        if p.get("learn") is not None:
            return bool(p["learn"])
        return not (p["checks"] >= 10 and p["unknowns"] / p["checks"] > 0.3)

    def set_learn(self, entity_id, value):
        with self.lock:
            self._profile(entity_id)
            self.db.execute("UPDATE profiles SET learn=? WHERE entity_id=?",
                            (None if value is None else int(bool(value)), entity_id))

    def within(self, entity_id):
        base = WITHIN.get((entity_id or "").split(".")[0], WITHIN_DEFAULT)
        with self.lock:
            p = self.db.execute("SELECT delays FROM profiles WHERE entity_id=?", (entity_id,)).fetchone()
        ds = sorted(json.loads(p["delays"])) if p else []
        if len(ds) >= 5:
            p90 = ds[min(len(ds) - 1, int(math.ceil(0.9 * len(ds))) - 1)]
            base = max(base, p90 * 1.5 + 2)
        return min(base, WITHIN_MAX)

    # ---- reading --------------------------------------------------------------------------------------------------
    @staticmethod
    def _row(r):
        d = dict(r)
        for k in ("records", "data", "expected", "seen_state", "override", "prior_attrs"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    pass
        return d

    def get(self, did):
        with self.lock:
            r = self.db.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
        return self._row(r) if r else None

    def recent(self, limit=50, entity_id=None, since=None, status=None):
        q, args = "SELECT * FROM decisions WHERE 1=1", []
        if entity_id:
            q += " AND entity_id=?"
            args.append(entity_id)
        if since:
            q += " AND ts>=?"
            args.append(since)
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self.lock:
            return [self._row(r) for r in self.db.execute(q, args)]

    def undoable(self, within=600, conversation_id=None):
        """The newest action that can still be undone: done in the last `within` seconds, by a request (not an
        automation), not already undone, and not itself an undo. Same conversation first."""
        q = ("SELECT * FROM decisions WHERE status='done' AND ts>=? AND undone_by IS NULL AND entity_id IS NOT NULL "
             "AND (channel IS NULL OR channel != 'automation') AND (request IS NULL OR request NOT LIKE 'undo%') ")
        args = [self.clock() - within]
        with self.lock:
            if conversation_id:
                r = self.db.execute(q + "AND conversation_id=? ORDER BY id DESC LIMIT 1",
                                    args + [conversation_id]).fetchone()
                if r:
                    return self._row(r)
            r = self.db.execute(q + "ORDER BY id DESC LIMIT 1", args).fetchone()
        return self._row(r) if r else None

    def mark_undone(self, did, by):
        with self.lock:
            self.db.execute("UPDATE decisions SET undone_by=? WHERE id=?", (by, did))

    def last_for(self, entity_id):
        r = self.recent(1, entity_id=entity_id, status="done")
        return r[0] if r else None

    def stats(self, since=None):
        """Expected versus seen, per entity and action: how many matched, missed, couldn't be read, were undone."""
        q = ("SELECT entity_id, action, status, seen, override IS NOT NULL AS ov, delay FROM decisions" +
             (" WHERE ts>=?" if since else ""))
        with self.lock:
            rows = self.db.execute(q, (since,) if since else ()).fetchall()
        out, tot = {}, {"decisions": 0, "refused": 0, "match": 0, "mismatch": 0, "unknown": 0, "unchecked": 0,
                        "pending": 0, "overrides": 0}
        for r in rows:
            key = f"{r['entity_id']} {r['action']}"
            s = out.setdefault(key, {"entity_id": r["entity_id"], "action": r["action"], "decisions": 0, "refused": 0,
                                     "match": 0, "mismatch": 0, "unknown": 0, "unchecked": 0, "pending": 0,
                                     "overrides": 0, "delays": []})
            for t in (s, tot):
                t["decisions"] += 1
                if r["status"] != "done":
                    t["refused"] += 1
                elif r["seen"] is None:
                    t["pending"] += 1
                else:
                    t[r["seen"]] = t.get(r["seen"], 0) + 1
                t["overrides"] += r["ov"]
            if r["delay"] is not None:
                s["delays"].append(r["delay"])
        for s in out.values():
            ds = sorted(s.pop("delays"))
            s["median_delay"] = ds[len(ds) // 2] if ds else None
            checked = s["match"] + s["mismatch"]
            s["match_rate"] = round(s["match"] / checked, 3) if checked else None
            s["match_90"] = [round(x, 3) for x in wilson(s["match"], checked)] if checked else None
        checked = tot["match"] + tot["mismatch"]
        tot["match_rate"] = round(tot["match"] / checked, 3) if checked else None
        tot["match_90"] = [round(x, 3) for x in wilson(tot["match"], checked)] if checked else None
        return {"total": tot, "by_action": sorted(out.values(), key=lambda s: -s["decisions"])}

    # ---- replay cases ---------------------------------------------------------------------------------------------
    def cases(self, limit=500, since=None, ids=None):
        """Decisions that make test cases: asked in words, done, and with a checked outcome. kind: "match" (it
        worked and stood: the action was right), "override" (a person undid it: the action was wrong), "device"
        (the device didn't do it: says nothing about the plan)."""
        q = ("SELECT * FROM decisions WHERE status='done' AND request IS NOT NULL AND request != '' "
             "AND (seen IN ('match', 'mismatch') OR override IS NOT NULL)")
        args = []
        if since:
            q += " AND ts>=?"
            args.append(since)
        if ids is not None:
            ids = [int(i) for i in ids]
            if not ids:
                return []
            q += f" AND id IN ({','.join('?' * len(ids))})"
            args += ids
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self.lock:
            rows = [self._row(r) for r in self.db.execute(q, args)]
        for r in rows:
            r["kind"] = "override" if r.get("override") else "match" if r["seen"] == "match" else "device"
        return rows

    def freeze(self, name, ids=None):
        """Keep a set of cases under a name (the frozen v1 set the weekly replay compares against). The decisions
        in it are kept past the retention limit. Returns how many."""
        ids = [c["id"] for c in self.cases(limit=10_000)] if ids is None else [int(i) for i in ids]
        with self.lock:
            self.db.executemany("INSERT OR IGNORE INTO frozen (name, decision_id) VALUES (?,?)", [(name, i) for i in ids])
            self.db.executemany("UPDATE decisions SET keep=1 WHERE id=?", [(i,) for i in ids])
        return len(ids)

    def frozen(self, name):
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT decision_id FROM frozen WHERE name=? ORDER BY decision_id",
                                                  (name,))]

    def prune(self, days=RETENTION_DAYS):
        cutoff = self.clock() - days * 86400
        with self.lock:
            n = self.db.execute("DELETE FROM decisions WHERE ts < ? AND keep=0", (cutoff,)).rowcount
            self.db.execute("DELETE FROM timings WHERE at < ?", (cutoff,))
            self.db.execute("DELETE FROM shadow WHERE at < ? AND decision_id NOT IN (SELECT id FROM decisions)",
                            (cutoff,))
            return n

    # ---- measurement records (phase 4; measure.py reads them) -------------------------------------------------------
    def count(self, name, n=1, day=None):
        """Add to a per-day count (asks sent, abstains, requests...)."""
        from datetime import date
        d = day or date.fromtimestamp(self.clock()).isoformat()
        with self.lock:
            self.db.execute("INSERT INTO daily (day, name, n) VALUES (?,?,?) ON CONFLICT(day, name) DO UPDATE SET "
                            "n = n + excluded.n", (d, name, n))

    def timing(self, kind, ms, front_end=None, model=None, fallback=False):
        try:
            ms = float(ms)
        except (TypeError, ValueError):
            return
        with self.lock:
            self.db.execute("INSERT INTO timings (at, kind, front_end, ms, model, fallback) VALUES (?,?,?,?,?,?)",
                            (self.clock(), kind, front_end, ms, model, 1 if fallback else 0))
