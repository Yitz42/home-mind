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

from .speakers import FIRST_PERSON

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


class Unreadable(ValueError):
    """The model's reply has no facts list in it: the turns are tried again another night."""


def parse_facts(text, strict=False):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        if strict:
            raise Unreadable("no JSON in the reply")
        return []
    try:
        d = json.loads(m.group(0))
    except ValueError:
        if strict:
            raise Unreadable("the reply's JSON doesn't parse") from None
        return []
    facts = d.get("facts") if isinstance(d, dict) else None
    if not isinstance(facts, list):
        # {"facts": "nope"} would otherwise file one note per letter (bug-hunt set)
        if strict and not (isinstance(d, dict) and facts is None and not d):
            raise Unreadable("no facts list in the reply")
        return []
    out = []
    for f in facts:
        if isinstance(f, dict) and str(f.get("text") or "").strip():
            out.append({"text": str(f["text"]).strip(), "turn": f.get("turn")})
        elif isinstance(f, str) and f.strip():
            out.append({"text": f.strip(), "turn": None})
    return out


def safe_for(model, text):
    """Notes read by a model in the cloud (option overnight_model) go with their secret values hidden: tagging,
    linking, reading structure and adding search keys never need the value itself."""
    from .privacy import is_cloud, mask_all
    return mask_all(text) if is_cloud(model) else text


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
    facts = parse_facts(r["text"], strict=True)
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


def _keys(text):
    """Names and numbers: two facts about different things or values are never duplicates."""
    names = {w.lower() for w in re.findall(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-z]+\b", text or "")} - FILLER
    nums = set(re.findall(r"\b\d+\b", text or ""))
    return names, nums


def _overlap(a, b):
    """How much of the shorter fact's content the other one covers ("I slept from 1 to 7 am" vs "The person slept
    from 1 to 7 am": 1.0). Different names or numbers: 0 ("the Logor toolbox is in the laundry room" is not the
    Fidor one)."""
    (na, ua), (nb, ub) = _keys(a), _keys(b)
    if (na or nb) and na != nb and not (na <= nb or nb <= na):
        return 0.0
    if na != nb and na and nb:
        return 0.0
    if ua and ub and not (ua <= ub or ub <= ua):
        return 0.0
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
        for col in ("speaker TEXT", "speaker_how TEXT", "device_id TEXT"):
            try:
                store.db.execute(f"ALTER TABLE conversations ADD COLUMN {col}")
            except Exception:  # noqa: BLE001 - already there
                pass
        store.db.commit()
        self.who = None          # device_id -> (name or None, how); set by the app from option people (#3)

    def add(self, ts, user_text, reply, conversation_id=None, run_id=None, pipeline=None, speaker=None, how=None,
            device_id=None):
        if not (user_text or "").strip():
            return False
        if speaker is None and device_id and self.who:
            speaker, how = self.who(device_id)
        try:
            self.store.db.execute("INSERT INTO conversations(ts,conversation_id,run_id,pipeline,user_text,reply,speaker,"
                                  "speaker_how,device_id) VALUES(?,?,?,?,?,?,?,?,?)",
                                  (ts, conversation_id, run_id, pipeline, user_text, reply, speaker, how, device_id))
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
                said, reply, conv, ts, dev = None, None, None, None, None
                for ev in d.get("events", []):
                    data = ev.get("data") or {}
                    if ev.get("type") == "intent-start":
                        said = data.get("intent_input")
                        conv = data.get("conversation_id") or conv
                        dev = data.get("device_id") or dev
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
                if self.add(ts or time.time(), said, reply, conv, rid, p.get("name"), device_id=dev):
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
        self.keys_on = True          # the model adds extra search keys to recent notes (#71)
        self.known_only = False      # True once option people lists anyone: unknown voices (guests) aren't learned from
        self.at = tuple(int(x) for x in (at or "03:00").split(":"))
        self.model = model
        self.done_for = None
        try:            # the last run survives a restart (the panel and the sensors read it)
            self.last = store.kv_get("overnight_last")
        except Exception:  # noqa: BLE001
            self.last = None
        self.enabled = True          # the Overnight learning switch; off skips the nightly run (not a button press)
        self.structure_on = False    # option overnight_structure: the model reads structure the patterns missed

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
        guests = []
        for r in rows:
            if self.known_only and not r.get("speaker"):
                guests.append(r["id"])      # #79: an unknown voice (a guest, the TV) doesn't become memory
                continue
            convs.setdefault((r["conversation_id"] or f"run-{r['id']}", r.get("speaker")), []).append(r)
        filed, skipped, done = [], 0, list(guests)
        for (cid, who), rs in convs.items():
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
                    # an unreadable reply or a failure: these turns wait for another night, three tries at most
                    log.warning("extract %s: %s", cid, e)
                    tries = self.store.kv_get("extract_tries") or {}
                    k = str(chunk[0]["id"])
                    tries[k] = tries.get(k, 0) + 1
                    if tries[k] >= self.EXTRACT_TRIES:
                        done += [r["id"] for r in chunk]
                        tries.pop(k)
                    self.store.kv_set("extract_tries", dict(list(tries.items())[-500:]))
                    continue
                mine = [r["user_text"] for r in chunk if who and FIRST_PERSON.search(r["user_text"] or "")]
                personal = [x for x in facts if mine and max(_overlap(x["text"], u) for u in mine) >= 0.4]
                rest = [x for x in facts if x not in personal]
                base = {"conversation_id": cid, **({"said_by": who} if who else {})}
                for group, extra in ((rest, {}), (personal, {"scope": "personal", "owner": who})):
                    if group:
                        f, s = file_facts(self.store, group, meta=dict(base, **extra))
                        filed += f
                        skipped += s
                done += [r["id"] for r in chunk]
        self.log.mark(done)
        self.log.prune()
        read = await self.read_structure(model) if self.structure_on else None
        reshelved = {"moved": [], "suggested": []}
        if getattr(self.store, "filer", None):
            try:
                from .filing import reshelve
                reshelved = reshelve(self.store.filer)        # #32: filing has learned more since these were filed
            except Exception as e:  # noqa: BLE001
                log.warning("reshelve: %s", e)
        tagged = 0
        try:
            tagged = await self.tag_languages(model)
        except Exception as e:  # noqa: BLE001
            log.warning("language tags: %s", e)
        relinked = 0
        try:
            relinked = await self.check_links(model)
        except Exception as e:  # noqa: BLE001
            log.warning("pronoun links: %s", e)
        keyed = 0
        if self.keys_on:
            from .keys import model_keys
            try:
                keyed = await model_keys(self.store, model)     # #71: other words for recent notes
            except Exception as e:  # noqa: BLE001
                log.warning("search keys: %s", e)
        self.last = {"at": time.time(), "model": name, "turns": len(done), "filed": len(filed), "already_known": skipped,
                     "unknown_voices": len(guests), "keys_added": keyed, "links_moved": relinked, "tagged_languages": tagged,
                     "reshelved": len(reshelved["moved"]), "reshelve_suggested": len(reshelved["suggested"]),
                     "ids": filed, **({"structure_read": read} if read is not None else {})}
        self.store.kv_set("overnight_last", self.last)
        return self.last

    EXTRACT_TRIES = 3

    TAG_SYSTEM = ("The note may be in any language. Reply with JSON only: {\"hedge\": true if the writer isn't sure "
                  "(a guess), \"correction\": true if it corrects an earlier note, \"speaker\": the name of the person "
                  "it says said it, or null, \"valid_from\": the date it became true as YYYY-MM-DD if it says one, or null}.")

    async def tag_languages(self, model, days=2, limit=30):
        """#21: notes not in English get the tags the English patterns would give (a guess, a correction, who said it,
        when it became true) from the model, so versions, guesses and disagreements work for them too."""
        import json
        from . import langs
        rows = self.store.db.execute(
            "SELECT id, body, meta FROM items WHERE status='active' AND created > ? AND kind != 'procedure' AND source NOT IN "
            "('home','automations','calendar','habits','tidy','evening') ORDER BY id DESC LIMIT 300",
            (time.time() - days * 86400,)).fetchall()
        n = 0
        for r in rows:
            m = json.loads(r["meta"] or "{}")
            if m.get("tagged_by") or langs.english(r["body"]):
                continue
            if n >= limit:
                break
            try:
                out = await model.chat(self.TAG_SYSTEM, [{"role": "user", "content": safe_for(model, r["body"][:600])}],
                                       max_tokens=80,
                                       temperature=0)
                tags = json.loads(re.search(r"\{.*\}", out.get("text") or "", re.S).group(0))
            except Exception:  # noqa: BLE001
                continue
            m["tagged_by"] = "model"
            m["language"] = langs.guess(r["body"])
            if tags.get("hedge") is True:
                m["hedge"] = True
            if tags.get("correction") is True:
                m["correction"] = True
            if isinstance(tags.get("speaker"), str) and tags["speaker"].strip():
                m.setdefault("speaker", tags["speaker"].strip()[:40])
            vf = tags.get("valid_from")
            if isinstance(vf, str) and re.fullmatch(r"\d{4}-\d\d-\d\d", vf) and "valid_from" not in m:
                from datetime import datetime
                m["valid_from"] = datetime.strptime(vf, "%Y-%m-%d").timestamp()
            with self.store.lock:
                self.store.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(m), r["id"]))
                self.store.db.commit()
            n += 1
        return n

    LINK_SYSTEM = ("A short note says 'it', 'its' or 'their' without naming the thing. Which of the numbered earlier notes "
                   "is it about? Reply with the number only, or 0 if none.")

    async def check_links(self, model, days=2, limit=30):
        """#13: re-check pronoun links from the last days with the big model: the note it points to, against the
        notes filed just before it. A different answer moves the link (and the [re: ...] in the title)."""
        import json
        rows = self.store.db.execute(
            "SELECT id, title, body, created, meta FROM items WHERE status='active' AND created > ? AND meta LIKE "
            "'%refers_to%' ORDER BY id DESC LIMIT ?", (time.time() - days * 86400, limit)).fetchall()
        moved = 0
        for r in rows:
            m = json.loads(r["meta"] or "{}")
            if m.get("refers_by") == "model" or not m.get("refers_to"):
                continue
            cands = self.store.db.execute(
                "SELECT id, title, body FROM items WHERE id < ? AND created >= ? AND kind != 'procedure' AND source NOT IN "
                "('home','automations','calendar') ORDER BY id DESC LIMIT 5", (r["id"], r["created"] - 3600)).fetchall()
            if len(cands) < 2:
                continue
            body = re.sub(r"\s*\[re: .*?\]$", "", r["body"] or "")
            msg = safe_for(model, "Earlier notes:\n" + "\n".join(f"{k + 1}. {c['body'][:200]}" for k, c in enumerate(cands)) +
                           f"\n\nNew note: {body[:300]}")
            try:
                out = await model.chat(self.LINK_SYSTEM, [{"role": "user", "content": msg}], max_tokens=5, temperature=0)
                k = int(re.search(r"\d+", out.get("text") or "0").group(0))
            except Exception:  # noqa: BLE001
                continue
            m["refers_by"] = "model"
            if 1 <= k <= len(cands) and cands[k - 1]["id"] != m["refers_to"]:
                new = cands[k - 1]
                m["refers_to"], m["refers_was"] = new["id"], m["refers_to"]
                title = re.sub(r"\s*\[re: .*?\]$", "", r["title"]) + f" [re: {new['title'][:100]}]"
                moved += 1
            else:
                title = r["title"]
            with self.store.lock:
                self.store.db.execute("UPDATE items SET meta=?, title=? WHERE id=?", (json.dumps(m), title, r["id"]))
                self.store.db.commit()
        return moved

    async def read_structure(self, model, days=2, limit=30):
        """Notes from the last `days` where the patterns found nothing but a word hints at structure: the model reads
        them, and only what it copies straight from the note is kept (structure.from_model)."""
        import json
        from . import structure
        rows = self.store.db.execute(
            "SELECT id, title, body, created, meta FROM items WHERE status='active' AND created > ? AND kind NOT IN "
            "('procedure','task') AND source NOT IN ('home','calendar','automations','scale','morning','patterns','habits','tidy','evening') "
            "AND source NOT LIKE 'pack:%' ORDER BY created DESC LIMIT 200", (time.time() - days * 86400,)).fetchall()
        n = 0
        for r in rows:
            meta = json.loads(r["meta"] or "{}")
            if meta.get("structure_by") or not structure.missed(meta, r["body"]):
                continue
            if n >= limit:
                break
            try:
                add = await structure.ask_model(model, safe_for(model, r["body"] or r["title"]), r["created"])
            except Exception as e:  # noqa: BLE001
                log.warning("structure %s: %s", r["id"], e)
                continue
            n += 1
            meta.update(add or {"structure_by": "model-none"})        # marked either way, so it is read once
            shelf_sql = ", shelf='profile'" if add.get("alias") else ""
            self.store.db.execute(f"UPDATE items SET meta=?{shelf_sql} WHERE id=?", (json.dumps(meta), r["id"]))
        self.store.db.commit()
        return n

    async def tick(self):
        now = datetime.now()
        if (now.hour, now.minute) >= self.at and (now.hour, now.minute) < (self.at[0] + 3, self.at[1]) \
                and self.done_for != now.date():
            self.done_for = now.date()
            if self.store.kv_get("overnight_done_for") == str(now.date()):
                return          # a restart inside the window after tonight's run: don't run it again
            self.store.kv_set("overnight_done_for", str(now.date()))
            if not self.enabled:
                self.last = {"at": time.time(), "skipped": "overnight learning switched off"}
                return
            try:
                log.info("overnight: %s", await self.run())
            except Exception:  # noqa: BLE001
                log.exception("overnight")
