"""Measurement (design spec: Measurement, phase 4): is Home Mind getting better, and at what cost to the person?

Truth is checked outcomes plus a person's labels, never another model's opinion.

  scorecard   three sections, each with its own pass bar, never averaged:
                house    a frozen set of real requests (v1, kept when v2 is added), replayed through today's planner
                         (it never acts), scored against what worked, what a person undid, and labels
                memory   your own corrections ("that's wrong" + the right value), asked again of your real memory
                safety   the daily safety suite
              Every number has a 90% interval. A gate passes only when the interval is wholly above the bar, fails
              when wholly below, and is blocked ("unclear") when it straddles it or there are under 20 cases.
  weekly      the scorecard against the frozen v1 set every week; an alert when the house section is clearly worse
              than its first run, or has fallen three weeks running.
  shadow      on about 10% of planned house requests, another model (Haiku if the live planner was local, the local
              model if it was Haiku) plans the same words in the background, off the voice path, and never acts.
              Scored against the outcome: win, tie or loss, with an interval, and the cost per correct answer.
  labelling   about 15 minutes a week: low confidence, overrides, refusals and mismatches first, then random ones;
              about 10% are repeats of earlier labels to measure agreement, and a gold set of 30 is re-labelled every
              quarter.
  burden      per day: asks the house made of you, corrections, and redos (a person undoing an action, or the same
              request again within 10 minutes), next to how often Home Mind abstained.
  timing      95th-percentile response time by front end (voice, panel, Telegram, Claude...) and the fallback rate.
"""
import asyncio
import json
import logging
import math
import random
import time
from datetime import date, datetime, timedelta

from . import replay as R
from .decisions import wilson

log = logging.getLogger("homemind.measure")

BARS = {"house": 0.9, "memory": 0.9, "safety": 1.0}
MIN_CASES = 20
SHADOW_RATE = 0.1
LABELS = ("right", "wrong", "unsure")
GOLD_SIZE = 30
GOLD_DAYS = 90
QUEUE_SIZE = 30            # about 15 minutes at 30 seconds each
REPEAT_SHARE = 0.1


def gate(k, n, bar, min_n=MIN_CASES):
    """A pass bar judged on its 90% interval: pass, fail, or unclear (blocked)."""
    if n <= 0:
        return {"n": 0, "correct": 0, "rate": None, "lo": None, "hi": None, "width": None, "bar": bar,
                "verdict": "not enough cases"}
    lo, hi = wilson(k, n)
    v = "pass" if lo >= bar else "fail" if hi < bar else "unclear"
    if n < min_n and v != "fail":
        v = "not enough cases"
    return {"n": n, "correct": k, "rate": round(k / n, 3), "lo": round(lo, 3), "hi": round(hi, 3),
            "width": round(hi - lo, 3), "bar": bar, "verdict": v}


def kappa(pairs):
    """Cohen's kappa for pairs of labels (a first label and its repeat)."""
    if not pairs:
        return None
    cats = sorted({a for a, _ in pairs} | {b for _, b in pairs})
    n = len(pairs)
    po = sum(a == b for a, b in pairs) / n
    pe = sum((sum(a == c for a, _ in pairs) / n) * (sum(b == c for _, b in pairs) / n) for c in cats)
    return 1.0 if pe == 1 else round((po - pe) / (1 - pe), 3)


def p95(values):
    v = sorted(values)
    return round(v[min(len(v) - 1, math.ceil(0.95 * len(v)) - 1)]) if v else None


class Measure:
    def __init__(self, app, rate=None, clock=None, rng=None):
        self.app = app
        self.log_ = app.decisions
        self.rate = SHADOW_RATE if rate is None else float(rate)
        self.clock = clock or time.time
        self.rng = rng or random.Random()
        self.sem = asyncio.Semaphore(1)

    @property
    def db(self):
        return self.log_.db

    # ---- labels -------------------------------------------------------------------------------------------------
    def label(self, decision_id, label, correct=None, labeller=None):
        """A person's verdict on one decision: right, wrong (optionally with what would have been right:
        {"entity_id", "action", "value"}), or unsure. A second label on the same decision is a repeat (agreement)."""
        if label not in LABELS:
            raise ValueError(f"label is one of {', '.join(LABELS)}")
        if self.log_.get(int(decision_id)) is None:
            raise ValueError("no such decision")
        if correct is not None and not (isinstance(correct, dict) and correct.get("entity_id")):
            raise ValueError("correct is {entity_id, action, value}")
        with self.log_.lock:
            first = self.db.execute("SELECT 1 FROM labels WHERE decision_id=? LIMIT 1", (int(decision_id),)).fetchone()
            cur = self.db.execute("INSERT INTO labels (decision_id, label, correct, labeller, at, is_repeat) "
                                  "VALUES (?,?,?,?,?,?)", (int(decision_id), label, json.dumps(correct) if correct
                                                           else None, labeller, self.clock(), 1 if first else 0))
            self.db.execute("UPDATE decisions SET keep=1 WHERE id=?", (int(decision_id),))   # labelled: kept
        return {"id": cur.lastrowid, "repeat": bool(first)}

    def truth(self, decision_id):
        """The first (non-repeat) label of a decision, or None."""
        with self.log_.lock:
            r = self.db.execute("SELECT * FROM labels WHERE decision_id=? AND is_repeat=0 ORDER BY at LIMIT 1",
                                (int(decision_id),)).fetchone()
        if not r:
            return None
        return {"label": r["label"], "correct": json.loads(r["correct"]) if r["correct"] else None, "at": r["at"]}

    def agreement(self):
        """How often a repeat label agrees with the first one, with kappa."""
        with self.log_.lock:
            rows = self.db.execute("SELECT decision_id, label, is_repeat FROM labels ORDER BY at").fetchall()
        first, pairs = {}, []
        for r in rows:
            if not r["is_repeat"]:
                first.setdefault(r["decision_id"], r["label"])
            elif r["decision_id"] in first:
                pairs.append((first[r["decision_id"]], r["label"]))
        agree = sum(a == b for a, b in pairs)
        return {"pairs": len(pairs), "agree": agree, "rate": round(agree / len(pairs), 3) if pairs else None,
                "kappa": kappa(pairs)}

    def cases(self, ids=None, limit=1000):
        """Test cases with truth: requests that were done and either checked or labelled. A label decides over the
        outcome: right -> what was done is right; wrong with the right action -> that is right; wrong without it ->
        what was done must not be repeated; unsure -> left out."""
        q = ("SELECT * FROM decisions WHERE status='done' AND request IS NOT NULL AND request != '' AND "
             "(request NOT LIKE 'undo%') AND (seen IN ('match','mismatch') OR override IS NOT NULL OR "
             "id IN (SELECT decision_id FROM labels))")
        args = []
        if ids is not None:
            ids = [int(i) for i in ids]
            if not ids:
                return []
            q += f" AND id IN ({','.join('?' * len(ids))})"
            args += ids
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self.log_.lock:
            rows = [self.log_._row(r) for r in self.db.execute(q, args)]
        out = []
        for r in rows:
            t = self.truth(r["id"])
            if t and t["label"] == "unsure":
                continue
            if t and t["label"] == "right":
                r["kind"] = "match"
            elif t and t["label"] == "wrong" and t["correct"]:
                r.update({k: t["correct"].get(k) for k in ("entity_id", "action", "value")}, kind="match",
                         corrected=True)
            elif t and t["label"] == "wrong":
                r["kind"] = "override"
            elif r.get("override"):
                r["kind"] = "override"
            elif r.get("seen") == "match":
                r["kind"] = "match"
            else:
                r["kind"] = "device"
            out.append(r)
        return out

    # ---- the labelling queue --------------------------------------------------------------------------------------
    def queue(self, size=QUEUE_SIZE, days=7):
        """This week's labelling: strata first (low confidence, overrides, refusals, mismatches), then random,
        about 10% repeats of earlier labels, and gold cases due for their quarterly re-label."""
        since = self.clock() - days * 86400
        with self.log_.lock:
            labelled = {r[0] for r in self.db.execute("SELECT DISTINCT decision_id FROM labels")}
            recent = [self.log_._row(r) for r in self.db.execute(
                "SELECT * FROM decisions WHERE ts>=? AND request IS NOT NULL AND request != '' ORDER BY id DESC",
                (since,))]
        fresh = [d for d in recent if d["id"] not in labelled]
        picked, seen = [], set()

        def take(rows, why, n):
            for d in rows:
                if len([p for p in picked if p["why"] == why]) >= n or len(picked) >= size:
                    return
                if d["id"] not in seen:
                    seen.add(d["id"])
                    picked.append({"why": why, "decision": d})
        gold = set(self.log_.frozen("gold"))
        due = []
        for gid in gold:
            with self.log_.lock:
                last = self.db.execute("SELECT MAX(at) FROM labels WHERE decision_id=?", (gid,)).fetchone()[0]
            if not last or self.clock() - last > GOLD_DAYS * 86400:
                d = self.log_.get(gid)
                if d:
                    due.append(d)
        n_repeat = max(1, round(size * REPEAT_SHARE))
        strata = size - n_repeat - min(len(due), size // 3) - 10
        per = max(1, strata // 4)
        take([d for d in fresh if d.get("confidence") is not None and d["confidence"] < 0.6], "low confidence", per)
        take([d for d in fresh if d.get("override")], "override", per)
        take([d for d in fresh if d["status"] in ("refused", "awaiting", "error")], "refused or held", per)
        take([d for d in fresh if d.get("seen") == "mismatch"], "mismatch", per)
        rest = [d for d in fresh if d["id"] not in seen]
        self.rng.shuffle(rest)
        take(rest, "random", 10)
        take(due, "gold (quarterly)", size // 3)
        with self.log_.lock:
            old = [r[0] for r in self.db.execute("SELECT DISTINCT decision_id FROM labels WHERE is_repeat=0 AND at<?",
                                                 (self.clock() - 3 * 86400,))]
        self.rng.shuffle(old)
        rep = [self.log_.get(i) for i in old if i not in seen and i not in gold][:n_repeat]
        take([d for d in rep if d], "repeat (agreement)", n_repeat)
        take(rest, "random", size)
        return [{"why": p["why"], "id": p["decision"]["id"], "request": p["decision"]["request"],
                 "did": {k: p["decision"].get(k) for k in ("entity_id", "action", "value", "status", "reason")},
                 "outcome": p["decision"].get("seen"), "undone": bool(p["decision"].get("override")),
                 "when": p["decision"]["ts"]} for p in picked]

    def make_gold(self, size=GOLD_SIZE):
        """The gold set: 30 labelled cases kept for good and re-labelled every quarter."""
        have = self.log_.frozen("gold")
        if have:
            return {"gold": len(have), "made": False}
        with self.log_.lock:
            ids = [r[0] for r in self.db.execute("SELECT DISTINCT decision_id FROM labels WHERE is_repeat=0 AND label "
                                                 "!= 'unsure' ORDER BY decision_id")]
        if len(ids) < size:
            raise ValueError(f"the gold set needs {size} labelled cases; there are {len(ids)}")
        self.rng.shuffle(ids)
        self.log_.freeze("gold", sorted(ids[:size]))
        return {"gold": size, "made": True}

    # ---- the scorecard ------------------------------------------------------------------------------------------
    def memory_section(self):
        from .cases import Cases, check
        from .mind import find
        st = self.app.mind.store
        k = n = 0
        for c in Cases(st).all():
            hits = find(st, c["question"], 8, touch=False)
            top = next((h for h in hits if not h.get("newer")), None)
            n += 1
            k += bool(check(c, (top["title"] + " " + top["body"]) if top else ""))
        return gate(k, n, BARS["memory"])

    def safety_section(self):
        res = getattr(self.app, "safety_result", None) or {}
        checks = res.get("checks") or []
        return gate(sum(c["ok"] for c in checks), len(checks), BARS["safety"], min_n=1)

    def ensure_v1(self):
        """Freeze v1 the first time there are enough cases. Returns its ids (or [])."""
        ids = self.log_.frozen("v1")
        if ids:
            return ids
        cs = [c for c in self.cases() if c["kind"] != "device"]
        if len(cs) < MIN_CASES:
            return []
        self.log_.freeze("v1", [c["id"] for c in cs])
        return self.log_.frozen("v1")

    async def scorecard(self, planner=None, name="v1"):
        """Run the scorecard (the house section replays the frozen set through planner; never acts)."""
        out = {"at": self.clock(), "set": name, "sections": {}}
        ids = self.ensure_v1() if name == "v1" else self.log_.frozen(name)
        house = {"n": 0, "verdict": "not enough cases", "bar": BARS["house"]}
        if ids and planner is not None:
            cs = [c for c in self.cases(ids=ids) if c["kind"] != "device"]
            rep = await R.replay(self.log_, planner, cases=cs)
            right = rep["agree"] + (rep["override"] - rep["repeats"])
            house = gate(right, rep["match"] + rep["override"], BARS["house"])
            house["errors"] = rep["errors"]
            house["by_front_end"] = {ch: gate(v["right"], v["cases"], BARS["house"])
                                     for ch, v in rep["by_channel"].items()}
            house["disagree"] = rep["disagree"][:10]
            house["p95_ms"] = rep["p95_ms"]
        out["sections"]["house"] = house
        out["sections"]["memory"] = self.memory_section()
        out["sections"]["safety"] = self.safety_section()
        with self.log_.lock:
            self.db.execute("INSERT INTO reports (at, kind, name, report) VALUES (?,?,?,?)",
                            (out["at"], "scorecard", name, json.dumps(out, default=str)))
        return out

    def reports(self, kind="scorecard", name="v1", limit=20):
        with self.log_.lock:
            rows = self.db.execute("SELECT report FROM reports WHERE kind=? AND (name=? OR ? IS NULL) ORDER BY id DESC "
                                   "LIMIT ?", (kind, name, name, limit)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def trend(self, name="v1"):
        """Is the house section clearly worse than its first run, or down three runs in a row?"""
        rs = [r for r in reversed(self.reports("scorecard", name, 100)) if r["sections"]["house"].get("n")]
        if len(rs) < 2:
            return {"verdict": "not enough runs", "runs": len(rs)}
        first, last = rs[0]["sections"]["house"], rs[-1]["sections"]["house"]
        worse = last["hi"] < first["lo"]
        rates = [r["sections"]["house"]["rate"] for r in rs[-4:]]
        falling = len(rates) == 4 and all(b < a for a, b in zip(rates, rates[1:]))
        return {"verdict": "worse than v1's first run" if worse else "falling 3 runs in a row" if falling else "ok",
                "first": first["rate"], "last": last["rate"], "runs": len(rs), "alarm": worse or falling}

    async def weekly(self, planner, notify=None):
        """The weekly replay against frozen v1, with an alarm. Returns the scorecard."""
        sc = await self.scorecard(planner, "v1")
        tr = self.trend("v1")
        sc["trend"] = tr
        if tr.get("alarm") and notify is not None:
            await notify("Home Mind is getting worse at house requests",
                         f"The weekly replay of the v1 set: {tr['verdict']} ({tr['first']:.0%} at first, "
                         f"{tr['last']:.0%} now). Details: GET /api/scorecard.")
        return sc

    # ---- the shadow baseline ------------------------------------------------------------------------------------
    def shadow_model(self, live):
        """Which model shadows a plan made by `live`: Haiku for the local model, the local model for Haiku."""
        models = getattr(self.app.models, "models", {}) or {}
        local = (getattr(self.app, "opts", None) or {}).get("control_model") or ""
        if live != "haiku" and "haiku" in models:
            return "haiku"
        if local and live != local and local in models:
            return local
        return None

    def maybe_shadow(self, text, plan, decision_id=None):
        """About 1 in 10 planned requests: plan it again with the other model, later, never acting."""
        if self.rate <= 0 or self.rng.random() >= self.rate:
            return None
        m = self.shadow_model(plan.get("by"))
        if not m:
            return None
        bg = getattr(self.app, "_bg", None)
        task = asyncio.ensure_future(self._shadow(text, plan, decision_id, m))
        if bg is not None:
            key = f"shadow-{id(task)}"
            bg[key] = task
            task.add_done_callback(lambda _t, k=key: bg.pop(k, None))
        return task

    async def _shadow(self, text, plan, decision_id, model):
        async with self.sem:               # one at a time, off the voice path
            t0 = time.monotonic()
            err, sp = None, None
            try:
                sp = await self.app.shadow_plan(text, model)
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
            ms = (time.monotonic() - t0) * 1000
            same = None if sp is None else int(R.same_action(sp, plan))
            with self.log_.lock:
                self.db.execute("INSERT INTO shadow (decision_id, at, live_model, model, plan, same, ms, error) VALUES "
                                "(?,?,?,?,?,?,?,?)", (decision_id, self.clock(), plan.get("by"), model,
                                                      json.dumps(sp) if sp is not None else None, same, ms, err))

    def shadow_report(self, days=30):
        """Win, tie or loss of the live planner against its shadow, judged by the outcome (and labels)."""
        since = self.clock() - days * 86400
        with self.log_.lock:
            rows = self.db.execute("SELECT * FROM shadow WHERE at>=? AND error IS NULL AND same IS NOT NULL",
                                   (since,)).fetchall()
        tally = {}
        for r in rows:
            t = tally.setdefault((r["live_model"] or "?", r["model"]), {"live": r["live_model"], "shadow": r["model"],
                                                                         "pairs": 0, "live_wins": 0, "shadow_wins": 0,
                                                                         "ties": 0, "unjudged": 0, "ms": []})
            t["pairs"] += 1
            t["ms"].append(r["ms"])
            c = self.cases(ids=[r["decision_id"]]) if r["decision_id"] else []
            if not c or c[0]["kind"] == "device":
                t["unjudged"] += 1
                continue
            c = c[0]
            sp = json.loads(r["plan"]) if r["plan"] else {}
            if c["kind"] == "match":
                live_ok = not c.get("corrected")
                shadow_ok = R.same_action(sp, c)
            else:
                live_ok, shadow_ok = False, not R.same_action(sp, c)
            if live_ok == shadow_ok:
                t["ties"] += 1
            elif live_ok:
                t["live_wins"] += 1
            else:
                t["shadow_wins"] += 1
        out = []
        for t in tally.values():
            dec = t["live_wins"] + t["shadow_wins"]
            lo, hi = wilson(t["live_wins"], dec) if dec else (None, None)
            t["live_win_share"] = round(t["live_wins"] / dec, 3) if dec else None
            t["live_win_90"] = [round(lo, 3), round(hi, 3)] if dec else None
            t["shadow_p95_ms"] = p95(t.pop("ms"))
            out.append(t)
        return {"days": days, "pairs": out, "cost_per_correct": self.cost_per_correct(days)}

    def cost_per_correct(self, days=30):
        """Per model: dollars per call (from usage, where prices are set) divided by its share of correct plans."""
        usage = getattr(self.app, "usage", None)
        if usage is None:
            return {}
        per_call = {}
        for r in usage.rows(days):
            if r.get("cost") is not None and r["calls"]:
                c = per_call.setdefault(r["model"], [0.0, 0])
                c[0] += r["cost"]
                c[1] += r["calls"]
        out = {}
        for m, (cost, calls) in per_call.items():
            with self.log_.lock:
                ids = [r[0] for r in self.db.execute("SELECT id FROM decisions WHERE model=? AND ts>=?",
                                                     (m, self.clock() - days * 86400))]
            cs = [c for c in self.cases(ids=ids) if c["kind"] != "device"] if ids else []
            right = sum(1 for c in cs if c["kind"] == "match" and not c.get("corrected"))
            share = right / len(cs) if cs else None
            out[m] = {"per_call": round(cost / calls, 5), "correct_share": round(share, 3) if share else None,
                      "per_correct": round(cost / calls / share, 5) if share else None}
        return out

    def record_pair(self, decision_id, live_plan, other_plan):
        """Balanced mode's comparison: the plan that was used and the other model's plan for the same words, kept
        like a shadow pair (no second model call: both were made anyway)."""
        same = int(R.same_action(other_plan, live_plan))
        with self.log_.lock:
            self.db.execute("INSERT INTO shadow (decision_id, at, live_model, model, plan, same, ms, error) VALUES "
                            "(?,?,?,?,?,?,?,?)", (decision_id, self.clock(), live_plan.get("by"), other_plan.get("by"),
                                                  json.dumps({k: other_plan.get(k) for k in ("entity_id", "action",
                                                                                             "value")}),
                                                  same, None, None))

    def calibration(self, bins=5):
        """Is confidence honest? Decisions with a confidence and a known truth, in bins: how often each bin was
        right, and the expected calibration error (ECE). Also the lowest line above which 95% were right."""
        with self.log_.lock:
            ids = [r[0] for r in self.db.execute("SELECT id FROM decisions WHERE confidence IS NOT NULL")]
        cs = [c for c in self.cases(ids=ids, limit=5000) if c["kind"] != "device"] if ids else []
        rows = [(float(c["confidence"]), c["kind"] == "match" and not c.get("corrected")) for c in cs]
        table, ece = [], 0.0
        for i in range(bins):
            lo, hi = i / bins, (i + 1) / bins
            b = [(p, ok) for p, ok in rows if lo <= p < hi or (i == bins - 1 and p == 1.0)]
            if not b:
                table.append({"from": lo, "to": hi, "n": 0})
                continue
            conf = sum(p for p, _ in b) / len(b)
            acc = sum(ok for _, ok in b) / len(b)
            ece += len(b) / len(rows) * abs(acc - conf)
            table.append({"from": lo, "to": hi, "n": len(b), "confidence": round(conf, 3), "right": round(acc, 3)})
        line = None
        for t in sorted({round(p, 2) for p, _ in rows}):
            above = [ok for p, ok in rows if p >= t]
            if len(above) >= MIN_CASES and sum(above) / len(above) >= 0.95:
                line = t
                break
        return {"cases": len(rows), "bins": table, "ece": round(ece, 3) if rows else None, "suggested_line": line}

    # ---- burden and timing -------------------------------------------------------------------------------------
    def burden(self, days=14):
        """Per day: asks + corrections + redos, with abstains and requests beside them."""
        end = date.fromtimestamp(self.clock())
        out = []
        st = getattr(getattr(self.app, "mind", None), "store", None)
        for i in range(days - 1, -1, -1):
            d = end - timedelta(days=i)
            t0 = datetime.combine(d, datetime.min.time()).timestamp()
            t1 = t0 + 86400
            with self.log_.lock:
                cnt = {r["name"]: r["n"] for r in self.db.execute("SELECT name, n FROM daily WHERE day=?",
                                                                  (d.isoformat(),))}
                acts = [self.log_._row(r) for r in self.db.execute(
                    "SELECT id, ts, entity_id, action, request, override, channel FROM decisions WHERE status='done' "
                    "AND ts>=? AND ts<? ORDER BY ts", (t0, t1))]
            # one person undoing a device is one redo, however many earlier decisions it undid
            overrides = len({(a["entity_id"], (a["override"] or {}).get("at")) for a in acts if a.get("override")})
            repeats, last = 0, {}
            for a in acts:
                if not a.get("request") or a.get("channel") == "automation":
                    continue
                k = (a["entity_id"], a["action"])
                if k in last and a["ts"] - last[k] <= 600:
                    repeats += 1            # the same thing asked again within 10 minutes: it didn't land
                last[k] = a["ts"]
            corrections = 0
            if st is not None:
                try:
                    corrections = st.db.execute("SELECT COUNT(*) FROM events WHERE type='feedback' AND ts>=? AND ts<? "
                                                "AND data LIKE '%\"helpful\": false%'", (t0, t1)).fetchone()[0]
                except Exception:  # noqa: BLE001
                    corrections = 0
            asks = cnt.get("asks", 0)
            req = cnt.get("requests", 0)
            out.append({"day": d.isoformat(), "asks": asks, "corrections": corrections, "redos": overrides + repeats,
                        "burden": asks + corrections + overrides + repeats, "requests": req,
                        "abstains": cnt.get("abstains", 0),
                        "abstain_rate": round(cnt.get("abstains", 0) / req, 3) if req else None})
        return out

    def timing(self, days=7):
        """p95 response time and fallback rate by kind (ask, do) and front end; the voice target is 2.5 s."""
        since = self.clock() - days * 86400
        with self.log_.lock:
            rows = self.db.execute("SELECT kind, front_end, ms, fallback FROM timings WHERE at>=?", (since,)).fetchall()
        groups = {}
        for r in rows:
            g = groups.setdefault((r["kind"], r["front_end"] or "?"), {"ms": [], "fallback": 0})
            g["ms"].append(r["ms"])
            g["fallback"] += r["fallback"]
        out = []
        for (kind, fe), g in sorted(groups.items()):
            n = len(g["ms"])
            out.append({"kind": kind, "front_end": fe, "n": n, "p95_ms": p95(g["ms"]),
                        "fallback_rate": round(g["fallback"] / n, 3) if n else None,
                        "target_ms": 2500 if fe in ("voice", "assist") else 5000 if fe in ("alexa", "gemini", "siri")
                        else None})
        return out
