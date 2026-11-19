"""Learned answers: questions Home Mind has answered well before, answered again without a model (design spec:
Records > Learned; steps 7, 12, 16, 23 and 26). Phase 8, after house requests (learned.py).

An answer template is: the note that answered it, the answer that was given (with the note's version at the time),
and the wordings it was asked in. It lives in the same lifecycle table as house templates (type 'answer') and earns
trust the same way: shadow first, a sceptical prior, the lower bound of a 90% interval, fading, 1 in 20 audited, two
failures in 20 back on trial, promoted only at night and only if the replay doesn't get worse.

What counts as a good outcome: a model answered from exactly one note (cited it), the match wasn't weak, the answer
wasn't "I don't know", and nobody said "that's wrong" about it in the next 15 minutes (a correction becomes a test
case in cases.py; that's what is looked for).

A reflex can never be stale: it answers only while its note is still the current version, unchanged since the answer
was given. A changed note sends the question to the model again (and the template back to shadow to learn the new
answer). Never for a sealed (sensitive) note, never for a note this viewer may not see, never with a number in the
question (the note might hold several) or a "not" in it. Every number in the answer must be in the note.

Tie-break (step 12): when the top two trusted templates are close, a small model may pick between them; how often
that happens is counted (the design's line: under 10% of requests).
"""
import json
import re

from .learned import (Learned, overlap, cosine, shape, PRIOR_A, PRIOR_B, REFLEX_MARGIN, AUDIT_RATE, MAX_EXAMPLES,
                      words)

ANSWER_SIM = 0.7              # questions have no device name to anchor them, so the bar is higher than a house reflex
SETTLE = 900
MAX_ANSWER = 300
NUM = re.compile(r"\d+(?:[.:]\d+)?")
DONT_KNOW = re.compile(r"\b(don'?t know|do not know|not sure|no (?:record|note|information)|nothing in memory|"
                       r"can'?t find|couldn'?t find)\b", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS learned_answers (
  record_id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL, item_key TEXT, item_updated REAL, answer TEXT NOT NULL,
  examples TEXT NOT NULL DEFAULT '[]', vector TEXT);
CREATE INDEX IF NOT EXISTS learned_answers_item ON learned_answers(item_id);
"""


class LearnedAnswers(Learned):
    CURSOR = "learned_answers_cursor"
    TIEBREAK = "learned_tiebreak"

    def __init__(self, store, clock=None, rng=None, embed=None, cases=None):
        super().__init__(store, None, clock=clock, rng=rng, embed=embed)
        self.cases = cases
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()

    # ---- reading ------------------------------------------------------------------------------------------------
    def _rows(self, where="1=1", args=()):
        with self.store.lock:
            rs = self.store.db.execute(
                "SELECT l.*, a.item_id, a.item_key, a.item_updated, a.answer, a.examples, a.vector FROM learned l "
                f"JOIN learned_answers a ON a.record_id = l.id WHERE l.type='answer' AND {where}", args).fetchall()
        out = []
        for r in rs:
            d = dict(r)
            d["examples"] = json.loads(d["examples"] or "[]")
            d["recent"] = json.loads(d["recent"] or "[]")
            d["vector"] = json.loads(d["vector"]) if d.get("vector") else None
            out.append(d)
        return out

    def describe(self, r):
        a, b = self.trust(r)
        from .learned import lower_bound
        return {"id": r["id"], "type": "answer", "status": r["status"], "version": r["version"],
                "item_id": r["item_id"], "answer": r["answer"], "examples": r["examples"][:5], "uses": r["uses"],
                "right": round(a - PRIOR_A, 1), "wrong": round(b - PRIOR_B, 1),
                "trust_90_low": round(lower_bound(a, b), 3), "note": r.get("note")}

    def _item(self, item_id):
        try:
            return self.store.get(item_id)
        except Exception:  # noqa: BLE001
            return None

    def _superseded(self, item_id):
        return self.store.db.execute("SELECT 1 FROM items WHERE supersedes=? LIMIT 1", (item_id,)).fetchone() is not None

    def current(self, r):
        """The template's note if it is still the version the answer came from, else None."""
        it = self._item(r["item_id"])
        if not it or it.get("status") != "active" or self._superseded(r["item_id"]):
            return None
        if r.get("item_updated") is not None and abs(float(it.get("updated") or it.get("created") or 0)
                                                     - float(r["item_updated"])) > 1e-6:
            return None
        return it

    # ---- step 16 --------------------------------------------------------------------------------------------------
    def check_answer(self, item, answer):
        """A reason this can't be a template, or None."""
        if not item:
            return "no note"
        meta = item.get("meta") or {}
        if meta.get("sensitive"):
            return "a sealed note: always through the model and its masking"
        if not answer or len(answer) > MAX_ANSWER:
            return "no answer, or too long"
        if DONT_KNOW.search(answer):
            return "the answer was 'I don't know'"
        have = set(NUM.findall(f"{item.get('title') or ''} {item.get('body') or ''}"))
        extra = [n for n in NUM.findall(answer) if n not in have]
        if extra:
            return f"numbers not in the note: {', '.join(extra)}"
        return None

    # ---- matching -----------------------------------------------------------------------------------------------
    async def match(self, text, statuses=("trusted",), viewer=None):
        base, nums, neg = shape(text)
        if neg or nums:
            return None
        cands = [r for r in self.all() if r["status"] in statuses]
        if not cands:
            return None
        vec = None
        if self.embed is not None and any(r.get("vector") for r in cands):
            try:
                vec = (await self.embed([base]))[0]
            except Exception:  # noqa: BLE001
                vec = None
        scored = []
        for r in cands:
            sim = max((overlap(base, e) for e in r["examples"]), default=0.0)
            if vec is not None and r.get("vector"):
                sim = max(sim, cosine(vec, r["vector"]) - 0.25)
            scored.append((sim, r))
        scored.sort(key=lambda x: -x[0])
        sim, r = scored[0]
        others = [(s, x) for s, x in scored[1:] if x["item_id"] != r["item_id"]]
        margin = sim - (others[0][0] if others else 0.0)
        return {"record": r, "sim": round(sim, 3), "margin": round(margin, 3),
                "runner_up": others[0][1] if others else None, "runner_sim": others[0][0] if others else 0.0}

    async def reflex(self, text, viewer=None, tiebreak=None):
        """{"answer", "item", "record", ...} to say without a model, or None. About 1 in 20 comes back as an audit."""
        from .speakers import visible
        m = await self.match(text)
        if not m or m["sim"] < ANSWER_SIM:
            return None
        self._count("seen")
        r = m["record"]
        if m["margin"] < REFLEX_MARGIN:
            ru = m["runner_up"]
            if tiebreak is None or ru is None or m["runner_sim"] < ANSWER_SIM:
                return None
            self._count("asked")
            try:
                pick = await tiebreak(text, [r["answer"], ru["answer"]])
            except Exception:  # noqa: BLE001
                pick = None
            if pick not in (0, 1):
                return None
            r = (r, ru)[pick]
            m = dict(m, record=r, tiebreak=True)
        it = self.current(r)
        if it is None:
            if r["status"] == "trusted":
                self.set_status(r["id"], "shadow", "its note changed: the next answer is learned afresh")
            return None
        if not visible(it.get("meta"), viewer) or self.check_answer(it, r["answer"]):
            return None
        out = dict(m, answer=r["answer"], item=it)
        if self.rng.random() < AUDIT_RATE:
            out["audit"] = True
        return out

    def _count(self, k):
        c = dict(self.store.kv_get(self.TIEBREAK) or {})
        c[k] = int(c.get(k, 0)) + 1
        self.store.kv_set(self.TIEBREAK, c)

    def tiebreak_rate(self):
        c = self.store.kv_get(self.TIEBREAK) or {}
        seen = int(c.get("seen", 0))
        return {"seen": seen, "asked": int(c.get("asked", 0)),
                "rate": round(int(c.get("asked", 0)) / seen, 3) if seen else None}

    # ---- writing ------------------------------------------------------------------------------------------------
    def _new_answer(self, item, answer, example, source):
        now = self.clock()
        with self.store.lock:
            cur = self.store.db.execute(
                "INSERT INTO learned (type, status, trust_a, trust_b, source, created, updated) VALUES "
                "('answer', 'shadow', ?, ?, ?, ?, ?)", (PRIOR_A, PRIOR_B, source, now, now))
            rid = cur.lastrowid
            self.store.db.execute(
                "INSERT INTO learned_answers (record_id, item_id, item_key, item_updated, answer, examples) VALUES "
                "(?,?,?,?,?,?)", (rid, item["id"], item.get("key"), item.get("updated") or item.get("created"),
                                  answer, json.dumps([example])))
            self.store.db.commit()
        return rid

    def add_example(self, r, example, answer=None, item=None):
        ex = [e for e in r["examples"] if e != example] + [example]
        with self.store.lock:
            if answer is not None and item is not None:
                self.store.db.execute("UPDATE learned_answers SET examples=?, vector=NULL, answer=?, item_id=?, "
                                      "item_updated=? WHERE record_id=?",
                                      (json.dumps(ex[-MAX_EXAMPLES:]), answer, item["id"],
                                       item.get("updated") or item.get("created"), r["id"]))
            else:
                self.store.db.execute("UPDATE learned_answers SET examples=?, vector=NULL WHERE record_id=?",
                                      (json.dumps(ex[-MAX_EXAMPLES:]), r["id"]))
            self.store.db.commit()

    def _corrected(self, question, ts):
        """Did someone say "that's wrong" about this question within SETTLE seconds? (cases.py keeps each one.)"""
        if self.cases is None:
            return False
        for c in self.cases.all():
            if ts <= (c.get("ts") or 0) <= ts + SETTLE and overlap(question, c.get("question") or "") >= 0.6:
                return True
        return False

    def learn_from(self, dlog=None, create=True, limit=500):
        """Settled asks since last time: evidence for templates that would have matched, and new shadow ones."""
        cur = int(self.store.kv_get(self.CURSOR) or 0)
        settle = self.clock() - SETTLE
        rows = self.store.db.execute("SELECT id, ts, data FROM events WHERE type='ask' AND id>? AND ts<? ORDER BY id "
                                     "LIMIT ?", (cur, settle, limit)).fetchall()
        out = {"seen": 0, "evidence": 0, "created": 0, "examples": 0}
        for ev in rows:
            cur = ev["id"]
            try:
                d = json.loads(ev["data"] or "{}")
            except ValueError:
                continue
            q, srcs, ans = d.get("question") or "", d.get("sources") or [], d.get("answer") or ""
            if d.get("reflex"):
                r = self.get(d["reflex"])       # a learned answer said itself: judged by whether it was corrected
                if r is not None and r["status"] == "trusted":
                    ok = not self._corrected(q, ev["ts"])
                    self.evidence(r, ok, "" if ok else "corrected after it answered")
                    out["evidence"] += 1
                continue
            if not d.get("model") or d.get("weak") or d.get("errors") or len(srcs) != 1 or DONT_KNOW.search(ans):
                continue                     # no clear single-note answer: says nothing either way
            base, nums, neg = shape(q)
            if neg or nums:
                continue
            out["seen"] += 1
            stood = not self._corrected(q, ev["ts"])
            src = srcs[0]
            item = self._item(src)
            key = (item or {}).get("key")

            def same_note(r):          # the same note, or a newer version of it (same key)
                return r["item_id"] == src or (bool(key) and r.get("item_key") == key)
            for r in self.all():
                if r["status"] in ("dropped", "retired"):
                    continue
                if max((overlap(base, e) for e in r["examples"]), default=0.0) < ANSWER_SIM:
                    continue
                predicted = same_note(r)
                self.evidence(r, predicted == stood, "" if predicted == stood else f"ask {ev['id']}")
                out["evidence"] += 1
            if not (create and stood):
                continue
            if self.check_answer(item, ans):
                continue
            same = [r for r in self.all() if same_note(r) and r["status"] not in ("retired",)
                    and max((overlap(base, e) for e in r["examples"]), default=0.0) >= ANSWER_SIM]
            if same:
                if base.strip() not in same[0]["examples"] or same[0]["answer"] != ans:
                    self.add_example(same[0], base.strip(), ans, item)
                    out["examples"] += 1
            else:
                self._new_answer(item, ans, base.strip(), f"ask {ev['id']}")
                out["created"] += 1
        self.store.kv_set(self.CURSOR, cur)
        return out

    def retire_missing(self):
        n = 0
        for r in self.all():
            if r["status"] in ("retired", "dropped"):
                continue
            it = self._item(r["item_id"])
            if not it or it.get("status") != "active":
                self.set_status(r["id"], "retired", "its note is gone")
                n += 1
        return n

    async def score(self, include=()):
        """The nightly gate's replay: every correction test case, answered by a template where one would act (trusted,
        or provisional in include), else by the top note. The share right; 1.0 when there are no cases yet."""
        from .cases import check
        from .mind import find
        cs = self.cases.all() if self.cases is not None else []
        if not cs:
            return 1.0
        k = 0
        for c in cs:
            m = await self.match(c["question"], statuses=("trusted", "provisional"))
            if m and m["sim"] >= ANSWER_SIM and m["margin"] >= REFLEX_MARGIN and \
                    (m["record"]["status"] == "trusted" or m["record"]["id"] in include) and self.current(m["record"]):
                text = m["record"]["answer"]
            else:
                hits = find(self.store, c["question"], 8, touch=False)
                top = next((h for h in hits if not h.get("newer")), None)
                text = (top["title"] + " " + top["body"]) if top else ""
            k += bool(check(c, text))
        return k / len(cs)

    async def nightly(self, dlog=None, scorecard=None):
        r = self.learn_from(dlog, create=True)
        retired = self.retire_missing()
        await self.embed_missing()
        p = await self.promote(scorecard or self.score)
        return {"change": "promotions", "retired": retired, **r, **p}


def tie_prompt(text, answers):
    """The small model's whole task: which of two answers fits the question. Reply 1, 2 or 0."""
    return ("Which answer fits the question? Reply with only 1, 2, or 0 if neither.\n"
            f"Question: {text}\n1: {answers[0]}\n2: {answers[1]}")


def tie_pick(reply):
    m = re.search(r"\b([012])\b", reply or "")
    return {"1": 0, "2": 1}.get(m.group(1)) if m else None


__all__ = ["LearnedAnswers", "ANSWER_SIM", "tie_prompt", "tie_pick", "words"]
