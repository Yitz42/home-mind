"""Gaps become proposals, cheapest first (design spec step 32; phase 14).

What Home Mind couldn't do (the surprises queue's "can't do" and "don't know", step 24) is grouped by topic. A topic
seen at least 3 times in two weeks becomes a proposal, and the cheapest fix comes first:
  1. records from what already exists: a note to tell Home Mind once, a tracker, a scene, an automation draft;
  2. an existing app or integration (finance: Firefly III before any new module; things you own: Homebox; recipes:
     Mealie; pantry and chores: Grocy; documents: Paperless; a device: its Home Assistant integration);
  3. only then a new module, written as a spec and a pull request with tests and a permission manifest.
For 3, Opus is asked through the Batch API (about half price) and only on nights when a gate failed or a gap is
queued. It sees only the schema and made-up data, never your notes, and what it writes is a proposal for a person to
review: new code needs review and a sandbox, and new dependencies need a manual yes. A new module would run as its
own process (JSON over stdin and stdout, no network, no vault key, time limits held by the parent).
"""
import json
import re
import time
from collections import Counter

MIN_SEEN = 3
DAYS = 14
TOPICS = [  # (topic, words, app, record suggestion)
    ("finance", r"\b(?:spend\w*|budget|bank|money|bill|bills|paid|cost|expens\w*|salary|account balance)\b",
     "Firefly III", None),
    ("inventory", r"\b(?:own|warranty|serial|receipt|bought|inventory|where did i buy)\b", "Homebox", None),
    ("recipes", r"\b(?:recipe|cook|bake|ingredients?|dinner idea|meal plan)\b", "Mealie", None),
    ("pantry", r"\b(?:pantry|groceries|grocery|shopping list|expire\w*|chores?)\b", "Grocy", None),
    ("documents", r"\b(?:document|letter|contract|policy|pdf|scan\w*|form)\b", "Paperless-ngx", None),
    ("device", r"\b(?:turn|switch|set|start|open|close|play|vacuum|robot|tv|speaker|oven|kettle|coffee)\b",
     "the device's Home Assistant integration", "an automation draft or a scene"),
    ("health", r"\b(?:symptom|pain|fog|tired|energy|sleep|headache|mood)\b", None, "a tracker"),
    ("people", r"\b(?:birthday|phone number|address|email of|who is)\b", None, "a note: tell me once"),
]
TOPIC_RX = [(t, re.compile(rx, re.I), app, rec) for t, rx, app, rec in TOPICS]
SCHEMA = {"note": {"id": "int", "title": "str", "body": "str", "shelf": "str", "created": "float", "meta": "object"},
          "decision": {"entity_id": "str", "action": "str", "request": "str", "seen": "match|mismatch|unknown"},
          "module protocol": "a subprocess; one JSON object on stdin, one on stdout; no network; no vault key; the "
                             "parent holds every capability and the time limit"}
FAKE = [{"id": 1, "title": "Boiler", "body": "The boiler was serviced in March.", "shelf": "home/appliances",
         "created": 1700000000.0, "meta": {}},
        {"id": 2, "title": "Bins", "body": "Bins go out Tuesday night.", "shelf": "home/routines",
         "created": 1700000100.0, "meta": {}}]


def topic_of(text):
    for t, rx, app, rec in TOPIC_RX:
        if rx.search(text or ""):
            return t, app, rec
    return "other", None, None


class Proposals:
    KV = "proposals"

    def __init__(self, store, surprises=None, clock=None):
        self.store = store
        self.surprises = surprises
        self.clock = clock or time.time

    def gaps(self):
        """Gap topics seen at least MIN_SEEN times in DAYS days."""
        since = self.clock() - DAYS * 86400
        rows = self.store.db.execute("SELECT kind, what FROM surprises WHERE ts>=? AND kind IN ('cant_do', 'dont_know')",
                                     (since,)).fetchall() if self.surprises is not None else []
        c, examples = Counter(), {}
        for r in rows:
            t, _, _ = topic_of(r["what"])
            c[t] += 1
            examples.setdefault(t, []).append(r["what"])
        return [{"topic": t, "seen": n, "examples": examples[t][:5]} for t, n in c.most_common() if n >= MIN_SEEN]

    def build(self):
        """A proposal per gap, cheapest fix first. Kept in kv; returns them."""
        old = {p["topic"]: p for p in (self.store.kv_get(self.KV) or [])}
        out = []
        for g in self.gaps():
            t = g["topic"]
            _, app, rec = next(((x[0], x[2], x[3]) for x in TOPIC_RX if x[0] == t), (t, None, None))
            steps = []
            if rec:
                steps.append({"rank": 1, "fix": "records", "what": f"Use {rec} for this."})
            if app:
                steps.append({"rank": 2, "fix": "app", "what": f"Connect {app} (read-only, its own token)."})
            steps.append({"rank": 3, "fix": "module", "what": "Draft a small module: a spec and a pull request with "
                                                              "tests and a permission manifest, for review."})
            p = {"topic": t, "seen": g["seen"], "examples": g["examples"], "steps": steps,
                 "status": (old.get(t) or {}).get("status", "open"), "at": self.clock()}
            out.append(p)
        self.store.kv_set(self.KV, out)
        return out

    def set_status(self, topic, status):
        ps = self.store.kv_get(self.KV) or []
        for p in ps:
            if p["topic"] == topic:
                p["status"] = status
        self.store.kv_set(self.KV, ps)

    def opus_request(self, proposal, model="claude-opus"):
        """The one batch request for a module draft: the schema, made-up data and the gap's description only."""
        prompt = ("Propose the cheapest fix for a gap in a home assistant's memory system. Prefer records from the "
                  "existing types, then an existing app, and only then a new module. If a module, write a spec, a "
                  "test list and a permission manifest (what it reads, what it may write; never network, never the "
                  "vault key), for a person to review. You see only the schema and made-up data.\n\n"
                  f"Gap topic: {proposal['topic']} (seen {proposal['seen']} times in two weeks).\n"
                  f"Schema: {json.dumps(SCHEMA)}\nMade-up data: {json.dumps(FAKE)}")
        return {"custom_id": f"gap-{proposal['topic']}-{int(self.clock())}",
                "params": {"model": model, "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]}}

    def batch_due(self, gate_failed=False):
        """Only on a night when a gate failed or a gap is queued (an open proposal that reached a module)."""
        open_ = [p for p in (self.store.kv_get(self.KV) or []) if p["status"] == "open"]
        return gate_failed or bool(open_)

    def batch(self, gate_failed=False, model="claude-opus"):
        if not self.batch_due(gate_failed):
            return None
        reqs = [self.opus_request(p, model) for p in (self.store.kv_get(self.KV) or []) if p["status"] == "open"]
        return {"requests": reqs} if reqs else None
