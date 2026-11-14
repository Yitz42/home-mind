"""The mind: storage + models + procedures, and the test runner that measures it.

ask(question, model): the brain finds what it holds that bears on the question (and any procedures that apply),
hands that to the model, and logs the episode. organize(text, model): the model decides title, shelf, tags and key
for something new; the brain stores it (so later finding shows how well it organized).
"""
import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime

from . import suites
from .store import Store

ANSWER_SYSTEM = ("You answer questions using the memory notes given to you. Each note has a date. When notes disagree, "
                 "the newest note is the one that is true now. If the notes do not hold the answer, say you don't know. "
                 "Answer in one short sentence and include the exact value, name or title asked for.")
CONTEXT_SYSTEM = ("You answer questions using the log of things you were told, oldest first. Later entries replace "
                  "earlier ones about the same thing. If the log does not hold the answer, say you don't know. Answer in "
                  "one short sentence and include the exact value, name or title asked for.")
ALONE_SYSTEM = "Answer the question in one short sentence. If you don't know, say you don't know."
ORGANIZE_SYSTEM = (
    "You file notes into a personal memory. Reply with JSON only, no other text: "
    '{"title": short title, "shelf": a path like "area/topic" (reuse an existing shelf if one fits), '
    '"tags": [up to 4 words], "key": "subject|attribute" if the note states one value of one thing that could change '
    'later (e.g. "the garage door|code"), else null}.')


def _ts(t):
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")


def note_line(h):
    line = f"[{_ts(h['created'])} | {h['shelf']} | #{h['id']}] {h['title']}"
    if h.get("body") and h["body"] != h["title"]:
        line += f" — {h['body']}"
    if h.get("newer"):
        line += f"  (older note: #{h['newer']} is newer on the same thing)"
    return line


EXPAND_SYSTEM = ("You help search a personal memory that matches words, not meaning. Reply with JSON only: "
                 '{"words": [4 to 8 search words]}: the most specific words from the question, plus other common words '
                 "for the same things (e.g. sensor for detector). No sentences.")


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


NUM_WORDS = {"zero": "0", "none": "0", "no": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
             "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12"}


def contains(answer, gold):
    a, g = f" {norm(answer)} ", f" {norm(gold)} "
    if g in a:
        return True
    if g.strip().isdigit():
        a2 = " " + " ".join(NUM_WORDS.get(w, w) for w in a.split()) + " "
        return g in a2
    return False


class Mind:
    def __init__(self, data_dir, models, packs=None):
        self.data_dir = data_dir
        self.store = Store(os.path.join(data_dir, "mind.db"))
        self.models = models
        self.packs = packs
        self.jobs = {}

    # ---- using the brain --------------------------------------------------------------------------------------------
    async def search(self, store, question, k=8, shelf=None, model=None, expand=False):
        """The brain's search, optionally helped by a model that suggests other words for the same things. The two
        result lists are fused by rank (each item scores 1/(rank+20) in each list)."""
        hits = store.recall(question, shelf=shelf, limit=k)
        if not (expand and model):
            return hits, None
        r = await self.models.get(model).chat(EXPAND_SYSTEM, [{"role": "user", "content": question}], max_tokens=80,
                                              temperature=0)
        plan = parse_json(r["text"]) or {}
        extra = " ".join(str(w) for w in (plan.get("words") or []))[:300]
        if not extra.strip():
            return hits, extra
        more = store.recall(extra, shelf=shelf, limit=k, touch=False)
        fused, rows = {}, {}
        for lst in (hits, more):
            for i, h in enumerate(lst):
                fused[h["id"]] = fused.get(h["id"], 0) + 1 / (i + 20)
                rows.setdefault(h["id"], h)
        out = sorted(rows.values(), key=lambda h: (fused[h["id"]], h["created"]), reverse=True)[:k]
        return out, extra

    async def context_for(self, store, question, k=8, shelf=None, model=None, expand=False):
        hits, _ = await self.search(store, question, k, shelf, model, expand)
        procs = [p for p in store.recall(question, kind="procedure", limit=2, touch=False) if p["score"] > 0.5]
        return hits, procs, "\n".join(note_line(h) for h in hits)

    async def ask(self, question, model=None, use_brain=True, k=8, shelf=None, store=None, system=None, expand=False):
        store = store or self.store
        hits, procs, ctx = (await self.context_for(store, question, k, shelf, model, expand)) if use_brain else ([], [], "")
        if model is None:
            return {"answer": hits[0]["body"] if hits else None, "hits": hits, "model": None}
        sysmsg = system or (ANSWER_SYSTEM if use_brain else ALONE_SYSTEM)
        if procs:
            sysmsg += "\n\nProcedures you know:\n" + "\n".join(f"- {p['title']}: {p['body']}" for p in procs)
        content = (f"MEMORY:\n{ctx or '(nothing found)'}\n\nQUESTION: {question}") if use_brain else question
        r = await self.models.get(model).chat(sysmsg, [{"role": "user", "content": content}])
        store.log("ask", None, question=question, model=model, ids=[h["id"] for h in hits], answer=r["text"][:500],
                  ms=r["ms"])
        return {"answer": r["text"], "hits": hits, "procedures": [p["id"] for p in procs], "model": model, "ms": r["ms"]}

    async def organize(self, text, model, store=None, source="organize"):
        store = store or self.store
        shelves = [s["path"] for s in store.browse()["shelves"]]
        deeper = []
        for s in shelves[:20]:
            deeper += [x["path"] for x in store.browse(s)["shelves"]]
        r = await self.models.get(model).chat(
            ORGANIZE_SYSTEM, [{"role": "user", "content": f"Existing shelves: {', '.join(shelves + deeper) or '(none)'}"
                                                         f"\n\nNote: {text}"}], max_tokens=200, temperature=0)
        plan = parse_json(r["text"]) or {}
        out = store.remember(text=text, title=str(plan.get("title") or "")[:120], shelf=plan.get("shelf") or "inbox",
                             tags=plan.get("tags") or [], key=plan.get("key") or None, source=source,
                             kind="note")
        out["plan"] = plan
        out["ms"] = r["ms"]
        return out

    # ---- tests --------------------------------------------------------------------------------------------------------
    def start_test(self, suite, arm="brain", model=None, seed=None, n=None, ingest="raw", keyed=False, expand=False):
        if suite not in suites.SUITES:
            raise ValueError(f"unknown suite {suite}; have {', '.join(suites.SUITES)}")
        if arm not in ("brain", "model+brain", "model+context", "model"):
            raise ValueError("arm must be brain, model+brain, model+context or model")
        if arm != "brain" and not model:
            raise ValueError(f"arm {arm} needs a model")
        if ingest == "model" and not model:
            raise ValueError("ingest=model needs a model")
        seed = int(seed) if seed is not None else int(time.time()) % 100000
        run = {"id": uuid.uuid4().hex[:10], "ts": time.time(), "suite": suite, "arm": arm,
               "model": model if arm != "brain" or ingest == "model" else None, "seed": seed, "status": "running",
               "config": {"n": n, "ingest": ingest, "keyed": keyed, "expand": bool(expand)}, "summary": {}, "details": []}
        self.store.save_run(run)
        task = asyncio.get_running_loop().create_task(self._run_test(run))
        self.jobs[run["id"]] = {"task": task, "run": run}
        return run

    async def run_test_now(self, *a, **kw):
        run = self.start_test(*a, **kw)
        await self.jobs[run["id"]]["task"]
        return self.store.run(run["id"])

    async def _run_test(self, run):
        try:
            await self._play(run)
            run["status"] = "done"
        except Exception as e:  # noqa: BLE001 - recorded on the run
            run["status"] = "error"
            run["summary"]["error"] = f"{type(e).__name__}: {e}"[:500]
        self.store.save_run(run)
        self.store.log("test", None, run=run["id"], suite=run["suite"], arm=run["arm"], model=run["model"],
                       status=run["status"], summary=run["summary"])
        if self.on_test_done:
            try:
                await self.on_test_done(run)
            except Exception:  # noqa: BLE001
                pass

    on_test_done = None

    async def _play(self, run):
        cfg = run["config"]
        gen = suites.SUITES[run["suite"]]
        world = gen(run["seed"], **({"n": cfg["n"]} if cfg.get("n") else {}))
        os.makedirs(os.path.join(self.data_dir, "sandbox"), exist_ok=True)
        path = os.path.join(self.data_dir, "sandbox", f"{run['id']}.db")
        clock = {"t": time.time() - 30 * 86400}
        sb = Store(path, clock=lambda: clock["t"])
        refs, log, t0 = {}, [], time.monotonic()
        arm, model = run["arm"], run["model"]
        try:
            # 1. tell it the world (events an hour apart, so newer means newer)
            for ev in world["events"]:
                clock["t"] += 3600
                log.append(ev["text"])
                if arm in ("model", "model+context"):
                    continue
                if ev["op"] == "remember":
                    if cfg.get("ingest") == "model":
                        r = await self.organize(ev["text"] if not ev.get("title") else f"{ev['title']}. {ev['text']}",
                                                model, store=sb, source="test")
                    else:
                        r = sb.remember(text=ev["text"], title=ev.get("title", ""), kind=ev.get("kind", "note"),
                                        shelf=ev.get("shelf", "inbox"),
                                        key=ev.get("key") if cfg.get("keyed") else None, source="test")
                    refs[ev["ref"]] = r["id"]
                elif ev["op"] == "task_add":
                    r = sb.remember(text=ev["text"], title=ev["title"], kind="task", shelf=f"tasks/{ev['project']}",
                                    meta={"project": ev["project"], "due": ev["due"], "priority": ev["priority"],
                                          "done": False}, source="test")
                    refs[ev["ref"]] = r["id"]
                elif ev["op"] in ("task_done", "task_move"):
                    ch = {"done": True} if ev["op"] == "task_done" else {"due": ev["due"]}
                    r = sb.revise(refs[ev["ref"]], meta=ch)
                    refs[ev["ref"]] = r["id"]
            # 2. ask
            details = []
            for q in world["questions"]:
                clock["t"] += 600
                details.append(await self._question(run, sb, q, refs, log))
            run["details"] = details
            run["summary"] = summarize(details, run, time.monotonic() - t0, len(world["events"]))
        finally:
            sb.close()
            for ext in ("", "-wal", "-shm"):
                try:
                    os.remove(path + ext)
                except OSError:
                    pass

    async def _question(self, run, sb, q, refs, log):
        arm, model = run["arm"], run["model"]
        d = {"q": q["q"], "gold": q["gold"]}
        if run["suite"] == "tasks":
            p = q["params"]
            open_ = sb.tasks(project=p.get("project"), due_before=p.get("due_before"))
            if arm == "brain":
                ans = (open_[0]["title"] if open_ else "none") if p["type"] == "next" else str(len(open_))
                golds = [q["gold"]] + q.get("also", [])
                d.update(answer=ans, correct=any(contains(ans, g) for g in golds) if p["type"] == "next" else ans == q["gold"])
                return d
            if arm == "model+brain":
                allopen = sb.tasks()
                ctx = "\n".join(f"[open task] {t['title']} | project {t['meta']['project']} | due day {t['meta']['due']}"
                                f" | priority {t['meta']['priority']}" for t in allopen)
                content = f"OPEN TASKS (finished ones removed, soonest first):\n{ctx}\n\nQUESTION: {q['q']}"
                sysmsg = ANSWER_SYSTEM
            elif arm == "model+context":
                content, sysmsg = "LOG:\n" + "\n".join(log)[-16000:] + f"\n\nQUESTION: {q['q']}", CONTEXT_SYSTEM
            else:
                content, sysmsg = q["q"], ALONE_SYSTEM
            r = await self.models.get(model).chat(sysmsg, [{"role": "user", "content": content}])
            d.update(answer=r["text"][:400], ms=r["ms"],
                     correct=any(contains(r["text"], g) for g in [q["gold"]] + q.get("also", [])))
            return d
        # recall / update / locate / scale
        gold_id = refs.get(q.get("ref"))
        stale_ids = {refs.get(x) for x in q.get("stale_refs", [])} - {None}
        if arm in ("brain", "model+brain"):
            if arm == "model+brain" and run["config"].get("expand"):
                hits, d["expanded"] = await self.search(sb, q["q"], 8, model=model, expand=True)
            else:
                hits = sb.recall(q["q"], limit=8, include_old=False)
            ids = [h["id"] for h in hits]
            # with model-organized storage the gold item has its own id; it may have superseded the old ones
            rank = ids.index(gold_id) + 1 if gold_id in ids else None
            d.update(rank=rank, hit1=rank == 1, hit5=bool(rank and rank <= 5), mrr=1.0 / rank if rank else 0.0)
            stale_above = any(i in stale_ids for i in ids[:(rank - 1) if rank else len(ids)])
            d["stale_above"] = stale_above
            if q.get("shelf") and hits:
                d["shelf_ok"] = hits[0]["shelf"] == q["shelf"]
            if arm == "brain":
                top = hits[0] if hits else None
                text = (top["title"] + " " + top["body"]) if top else ""
                d.update(answer=text[:300], correct=bool(top) and contains(text, q["gold"])
                         and not any(contains(text, s) for s in q.get("stale", [])))
                return d
            ctx = "\n".join(note_line(h) for h in hits)
            content, sysmsg = f"MEMORY:\n{ctx or '(nothing found)'}\n\nQUESTION: {q['q']}", ANSWER_SYSTEM
        elif arm == "model+context":
            content, sysmsg = "LOG:\n" + "\n".join(log)[-16000:] + f"\n\nQUESTION: {q['q']}", CONTEXT_SYSTEM
        else:
            content, sysmsg = q["q"], ALONE_SYSTEM
        r = await self.models.get(model).chat(sysmsg, [{"role": "user", "content": content}])
        stale = [s for s in q.get("stale", []) if contains(r["text"], s) and not contains(s, q["gold"])]
        d.update(answer=r["text"][:400], ms=r["ms"], correct=contains(r["text"], q["gold"]) and not stale,
                 stale_answer=bool(stale))
        return d


def summarize(details, run, secs, n_events):
    n = len(details) or 1
    s = {"n": len(details), "events": n_events, "seconds": round(secs, 1),
         "accuracy": round(sum(1 for d in details if d.get("correct")) / n, 3)}
    if any("rank" in d for d in details):
        s["hit1"] = round(sum(1 for d in details if d.get("hit1")) / n, 3)
        s["hit5"] = round(sum(1 for d in details if d.get("hit5")) / n, 3)
        s["mrr"] = round(sum(d.get("mrr", 0) for d in details) / n, 3)
        s["stale_above"] = round(sum(1 for d in details if d.get("stale_above")) / n, 3)
    if any("shelf_ok" in d for d in details):
        xs = [d["shelf_ok"] for d in details if "shelf_ok" in d]
        s["shelf_ok"] = round(sum(xs) / len(xs), 3)
    if any("stale_answer" in d for d in details):
        s["stale_answers"] = round(sum(1 for d in details if d.get("stale_answer")) / n, 3)
    ms = [d["ms"] for d in details if d.get("ms")]
    if ms:
        s["ms_avg"] = int(sum(ms) / len(ms))
    return s


def parse_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None
