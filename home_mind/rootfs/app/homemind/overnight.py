"""Learning from conversations overnight.

1. Conversation log. Every minute the Assist pipelines' recent runs (what was said to Jeeves, by voice or text, and
   what Jeeves answered) are copied into a raw `conversations` table. Search never reads it. Questions asked through
   Home Mind's own ask() are logged there too. Raw turns are deleted 30 days after they're processed (60 if never).
2. Nightly extraction (option overnight_time, default 03:00). A large local model (option overnight_model; default
   the biggest local qwen that is configured) reads the day's conversations and lists only what you stated as true:
   no questions, jokes, hypotheticals, things you took back, or things only the assistant said. Each fact is compared
   with memory (the same fact already there is skipped) and filed with source "overnight" and the time it was said.
   The morning summary lists what was learned so a wrong one can be forgotten in one tap from the Memory page.
3. If the model's computer is off, the night is skipped and the backlog is processed the next night.
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime

log = logging.getLogger("homemind.overnight")

EXTRACT_SYSTEM = (
    "You read a conversation between a person (U) and an assistant (A) and list the facts the PERSON stated as true "
    "about their own life, home, belongings, plans, schedule, people or preferences.\n"
    "Rules:\n"
    "- Only what U says. Never take facts from A's lines.\n"
    "- Skip questions, requests and commands (\"what's the code?\", \"turn on the light\").\n"
    "- Skip jokes and sarcasm (\"lol\", \"jk\", \"just kidding\"), hypotheticals (\"if I...\", \"would it...\", "
    "\"maybe I should...\"), wishes and guesses.\n"
    "- If U corrects themselves (\"no wait\", \"actually\", \"I mean\"), keep only the corrected version.\n"
    "- Skip general knowledge and small talk (weather, feelings of the moment).\n"
    "- Skip anything A confirms it already saved (A's reply starts with \"Saved\" or \"Journaled\").\n"
    "- Skip facts that say nothing on their own (\"I want you to know this\", \"that is for my family\").\n"
    "- Write each fact in the first person, as U would say it (\"I keep the Kosk toolbox in the garage\"), never "
    "\"the user\" or \"the person\". One standalone sentence naming the thing it is about, with names spelled "
    "exactly as U said them and any date or time U gave. Keep numbers, codes and names exact.\n"
    "Reply with JSON only: {\"facts\": [{\"text\": \"...\", \"turn\": <number of the U line>}]}. If there are none, "
    "reply {\"facts\": []}.")


def parse_facts(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return []
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for f in d.get("facts") or []:
        if isinstance(f, dict) and str(f.get("text") or "").strip():
            out.append({"text": str(f["text"]).strip(), "turn": f.get("turn")})
        elif isinstance(f, str) and f.strip():
            out.append({"text": f.strip(), "turn": None})
    return out


async def extract(model, turns, max_tokens=900):
    """turns: [{"who": "user"|"assistant", "text": ..., "ts": ...}] -> facts with the time they were said."""
    keep = []
    for i, t in enumerate(turns):     # what Jeeves already saved ("Saved: ...") is in memory: leave it out
        nxt = turns[i + 1] if i + 1 < len(turns) else None
        if t["who"] == "user" and nxt and nxt["who"] == "assistant" and \
                re.match(r"\s*(saved|journaled)\b", nxt["text"] or "", re.I):
            continue
        if t["who"] == "assistant" and re.match(r"\s*(saved|journaled)\b", t["text"] or "", re.I):
            continue
        keep.append(t)
    turns = keep
    if not any(t["who"] == "user" for t in turns):
        return []
    lines, idx = [], {}
    for i, t in enumerate(turns, 1):
        lines.append(f"{i}. {'U' if t['who'] == 'user' else 'A'}: {t['text']}")
        idx[i] = t
    r = await model.chat(EXTRACT_SYSTEM, [{"role": "user", "content": "\n".join(lines)}], max_tokens=max_tokens,
                         temperature=0)
    facts = parse_facts(r["text"])
    for f in facts:
        t = idx.get(f["turn"]) if isinstance(f["turn"], int) else None
        if t is None or t["who"] != "user":
            # no usable turn number: pin it to the last thing the person said
            t = next((x for x in reversed(turns) if x["who"] == "user"), turns[-1] if turns else None)
        f["ts"] = t["ts"] if t else time.time()
    return facts


FILLER = {"i", "my", "me", "the", "a", "an", "is", "are", "was", "it", "its", "to", "from", "of", "in", "on", "at",
          "and", "user", "person", "they", "their", "them", "he", "she", "his", "her", "now", "this", "that", "am",
          "pm", "s"}


def _content(text):
    from .store import words
    return {w for w in words(text) if w not in FILLER}


def _overlap(a, b):
    """How much of the shorter fact's content the other one covers ("I slept from 1 to 7 am" vs "The person slept
    from 1 to 7 am": 1.0)."""
    wa, wb = _content(a), _content(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def file_facts(store, facts, source="overnight", meta=None):
    """File extracted facts; skip ones memory already has in nearly the same words. Returns (filed ids, skipped)."""
    filed, skipped, batch = [], 0, []
    for f in facts:
        hits = store.recall(f["text"], limit=5, touch=False)
        if any(_overlap(f["text"], h["body"]) >= 0.8 for h in hits) or \
                any(_overlap(f["text"], b) >= 0.8 for b in batch) or len(_content(f["text"])) < 2:
            skipped += 1
            continue
        batch.append(f["text"])
        r = store.remember(text=f["text"], kind="fact", shelf="inbox", source=source, at=f.get("ts"),
                           meta=dict(meta or {}, learned_overnight=True))
        filed.append(r["id"])
    return filed, skipped


class ConvLog:
    KEEP_DONE_DAYS, KEEP_RAW_DAYS = 30, 60

    def __init__(self, store):
        self.store = store
        store.db.execute("""CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY, ts REAL, conversation_id TEXT, run_id TEXT UNIQUE, pipeline TEXT,
            user_text TEXT, reply TEXT, processed INTEGER DEFAULT 0)""")
        store.db.commit()

    def add(self, ts, user_text, reply, conversation_id=None, run_id=None, pipeline=None):
        if not (user_text or "").strip():
            return False
        try:
            self.store.db.execute("INSERT INTO conversations(ts,conversation_id,run_id,pipeline,user_text,reply) "
                                  "VALUES(?,?,?,?,?,?)", (ts, conversation_id, run_id, pipeline, user_text, reply))
            self.store.db.commit()
            return True
        except Exception:  # noqa: BLE001 - duplicate run id
            return False

    def known(self, run_id):
        return self.store.db.execute("SELECT 1 FROM conversations WHERE run_id=?", (run_id,)).fetchone() is not None

    async def poll(self, ws_call):
        """Copy new Assist pipeline runs (the last few per pipeline are kept by Home Assistant)."""
        pipes = await ws_call({"type": "assist_pipeline/pipeline/list"}) or {}
        added = 0
        for p in pipes.get("pipelines", []):
            runs = await ws_call({"type": "assist_pipeline/pipeline_debug/list", "pipeline_id": p["id"]}) or {}
            for run in runs.get("pipeline_runs", []):
                rid = run.get("pipeline_run_id")
                if not rid or self.known(rid):
                    continue
                d = await ws_call({"type": "assist_pipeline/pipeline_debug/get", "pipeline_id": p["id"],
                                   "pipeline_run_id": rid}) or {}
                said, reply, conv, ts = None, None, None, None
                for ev in d.get("events", []):
                    data = ev.get("data") or {}
                    if ev.get("type") == "intent-start":
                        said = data.get("intent_input")
                        conv = data.get("conversation_id") or conv
                    elif ev.get("type") == "stt-end":
                        said = said or (data.get("stt_output") or {}).get("text")
                    elif ev.get("type") == "intent-end":
                        out = data.get("intent_output") or {}
                        conv = out.get("conversation_id") or conv
                        reply = ((out.get("response") or {}).get("speech") or {}).get("plain", {}).get("speech")
                    if ev.get("timestamp") and ts is None:
                        try:
                            ts = datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00")).timestamp()
                        except ValueError:
                            pass
                if self.add(ts or time.time(), said, reply, conv, rid, p.get("name")):
                    added += 1
        return added

    def pending(self):
        rows = self.store.db.execute("SELECT * FROM conversations WHERE processed=0 ORDER BY ts").fetchall()
        return [dict(r) for r in rows]

    def mark(self, ids):
        self.store.db.executemany("UPDATE conversations SET processed=1 WHERE id=?", [(i,) for i in ids])
        self.store.db.commit()

    def prune(self):
        now = time.time()
        self.store.db.execute("DELETE FROM conversations WHERE (processed=1 AND ts < ?) OR ts < ?",
                              (now - self.KEEP_DONE_DAYS * 86400, now - self.KEEP_RAW_DAYS * 86400))
        self.store.db.commit()


class Overnight:
    def __init__(self, store, models, convlog, at="03:00", model=None):
        self.store, self.models, self.log = store, models, convlog
        self.at = tuple(int(x) for x in (at or "03:00").split(":"))
        self.model = model
        self.done_for = None
        self.last = None

    def pick_model(self):
        names = list(self.models.models)
        if self.model and self.model in names:
            return self.model
        for pref in ("qwen35b", "qwen9b"):
            if pref in names:
                return pref
        local = [n for n, m in self.models.models.items() if getattr(m, "type", "") == "ollama"]
        return local[0] if local else None

    async def run(self, limit_turns=24):
        name = self.pick_model()
        if not name:
            self.last = {"at": time.time(), "skipped": "no local model configured"}
            return self.last
        model = self.models.get(name)
        try:
            await asyncio.wait_for(model.chat("Reply with OK.", [{"role": "user", "content": "OK?"}], max_tokens=5), 90)
        except Exception as e:  # noqa: BLE001
            self.last = {"at": time.time(), "model": name, "skipped": f"model not reachable ({type(e).__name__}); "
                                                                       "will retry next night"}
            return self.last
        rows = self.log.pending()
        convs = {}
        for r in rows:
            convs.setdefault(r["conversation_id"] or f"run-{r['id']}", []).append(r)
        filed, skipped, done = [], 0, []
        for cid, rs in convs.items():
            for i in range(0, len(rs), limit_turns):
                chunk = rs[i:i + limit_turns]
                turns = []
                for r in chunk:
                    if re.match(r"\s*(saved|journaled)\b", r.get("reply") or "", re.I):
                        continue        # Jeeves already saved this one to memory
                    turns.append({"who": "user", "text": r["user_text"], "ts": r["ts"]})
                    if r.get("reply"):
                        turns.append({"who": "assistant", "text": r["reply"], "ts": r["ts"]})
                if not any(t["who"] == "user" for t in turns):
                    done += [r["id"] for r in chunk]
                    continue
                try:
                    facts = await extract(model, turns)
                except Exception as e:  # noqa: BLE001
                    log.warning("extract %s: %s", cid, e)
                    continue
                f, s = file_facts(self.store, facts, meta={"conversation_id": cid})
                filed += f
                skipped += s
                done += [r["id"] for r in chunk]
        self.log.mark(done)
        self.log.prune()
        self.last = {"at": time.time(), "model": name, "turns": len(done), "filed": len(filed), "already_known": skipped,
                     "ids": filed}
        return self.last

    async def tick(self):
        now = datetime.now()
        if (now.hour, now.minute) >= self.at and (now.hour, now.minute) < (self.at[0] + 3, self.at[1]) \
                and self.done_for != now.date():
            self.done_for = now.date()
            try:
                log.info("overnight: %s", await self.run())
            except Exception:  # noqa: BLE001
                log.exception("overnight")
