"""The phrasebook: odd ways of saying sleep and workouts, each with the plain line the parsers read.

    "zonked at 1, kid woke me at 5:40"          -> slept from 1 am to 5:40 am
    "knocked out 5 sets of 5 on squats w/ 225"  -> squat 5x5 at 225 lb
    "watched TV for 2 hours last night"         -> NONE

It builds itself:
  - learned: when a model reads a message the parsers couldn't, the pair is kept
  - corrections: "no, it was 6 hours" (or "that's wrong") right after a log fixes the log and keeps the right pair
  - grown: overnight, the model writes new hard cases; each is kept only if the parsers can read its plain line, it
    adds no number that wasn't said, and a second blind reading (without the answer) agrees. Disagreements wait for
    review on the Health tab.
  - starter: a built-in set, so it works from the first day

And it is used two ways:
  - a message with the same shape as an entry ("zonked at 12, kid woke me at 6:10") is read with no model at all
  - otherwise the most similar entries go into the model's prompt as examples, which is what lets a small model
    (1-3B, on a laptop CPU) copy the pattern instead of following long instructions

Every line still goes through the same checks as before: it must parse, and a number that wasn't said is refused.
"""
import json
import logging
import random
import re
import time

from .langs import digits

log = logging.getLogger("homemind.phrasebook")

NUM = re.compile(r"\d{1,2}:\d{2}|\d+(?:\.\d+)?")
SOURCES = ("starter", "learned", "correction", "grown", "added")

STARTER = [
    # sleep: slang, story, alarms, kids, number words, partial times
    ("zonked at 1, kid woke me at 5:40", "slept from 1 am to 5:40 am"),
    ("conked out around 11, alarm at 6", "slept from 11 pm to 6 am"),
    ("lights out 10:30, up 6:15", "slept from 10:30 pm to 6:15 am"),
    ("finally drifted off at 2 and the dog had me up at 6", "slept from 2 am to 6 am"),
    ("out like a light at 11, didn't wake till 8", "slept from 11 pm to 8 am"),
    ("passed out on the couch at 12, moved to bed, up at 7", "slept from 12 am to 7 am"),
    ("tossed and turned, maybe 4 hours total", "slept 4 hours"),
    ("barely slept, 3 hrs tops", "slept 3 hours"),
    ("got a solid 8 last night", "slept 8 hours"),
    ("ugh only 5 hrs", "slept 5 hours"),
    ("sleep was 6 and a half hours", "slept 6.5 hours"),
    ("sleep: 12 -> 7", "slept from 12 am to 7 am"),
    ("bed 11:45 / up 6:30", "slept from 11:45 pm to 6:30 am"),
    ("in bed by 10, asleep by 11, woke at 6", "slept from 11 pm to 6 am"),
    ("crashed hard around 9 and slept till 5", "slept from 9 pm to 5 am"),
    ("woke up at 7 after going to bed at 12", "slept from 12 am to 7 am"),
    ("slept in till 9, went down at 1", "slept from 1 am to 9 am"),
    ("seven hours of sleep", "slept 7 hours"),
    ("zzz 11-6", "slept from 11 pm to 6 am"),
    ("sleep 7h", "slept 7 hours"),
    # workouts: other orders, gym shorthand, cardio stories
    ("knocked out 5 sets of 5 on squats with 225", "squat 5x5 at 225 lb"),
    ("leg day: 4x10 leg press at 270, 3x12 lunges", "leg press 4x10 at 270 lb, lunge 3x12"),
    ("squat 225 5x5", "squat 5x5 at 225 lb"),
    ("bench 185x5x3", "bench press 3x5 at 185 lb"),
    ("did bench, 3 sets, 8 reps each, 155", "bench press 3x8 at 155 lb"),
    ("worked up to a 315 deadlift single", "deadlift 1x1 at 315 lb"),
    ("hit a 405 deadlift for 1", "deadlift 1x1 at 405 lb"),
    ("ohp 95 for 3 sets of 8", "overhead press 3x8 at 95 lb"),
    ("pullups: 10, 8, 6", "pull-up 3x8"),
    ("100 pushups throughout the day", "push-up 1x100"),
    ("curls 30s 3x12", "curl 3x12 at 30 lb"),
    ("did 3 rounds of 20 kb swings with 53", "kettlebell swing 3x20 at 53 lb"),
    ("couch to 5k day 3, 30 minutes", "run 30 min"),
    ("a 5k this morning in 27 minutes", "run 5 km in 27 min"),
    ("3 mile jog, took about 30 min", "run 3 miles in 30 min"),
    ("an hour on the bike", "bike 60 min"),
    ("spin class 45 min", "bike 45 min"),
    ("went on a 6 mile hike", "hike 6 miles"),
    ("walked the dog for 40 minutes", "walk 40 min"),
    ("swam 20 laps, about 25 min", "swim 25 min"),
    ("20 min on the rower", "row 20 min"),
    ("45 min of yoga before work", "yoga 45 min"),
    ("10k steps walk, around 90 minutes", "walk 90 min"),
    # not logs: plans, questions, habits, other activities, other nights
    ("watched TV for 2 hours last night", "NONE"),
    ("the party went from 8 to 12 last night", "NONE"),
    ("charged the car for 3 hours last night", "NONE"),
    ("I need 8 hours of sleep", "NONE"),
    ("I usually sleep 7 hours", "NONE"),
    ("going to bed at 11 tonight", "NONE"),
    ("remind me to run 3 miles tomorrow", "NONE"),
    ("how many sets of squats should I do", "NONE"),
    ("my bench max is 225", "NONE"),
    ("set the bedroom light to 30", "NONE"),
    ("the kids went to bed at 8", "NONE"),
    ("baby slept from 7 to 6", "NONE"),
    ("planning a 10 mile run on Saturday", "NONE"),
]


def extra_ok(said):
    """Numbers a plain line may have that weren't said as digits: midnight is 12, half past is :30, an hour is 60."""
    t = (said or "").lower()
    ok = set()
    if re.search(r"midnight|noon", t):
        ok |= {"12", "0"}
    if "half" in t:
        ok |= {"30", "5", "0.5"}
    if "quarter" in t:
        ok |= {"15", "45"}
    if re.search(r"\bhour|\bhr", t):
        ok |= {"60", "90", "1"}
    if re.search(r"\bsingle\b|\bfor (?:a |one )?1\b|\bfor 1\b", t):
        ok |= {"1"}
    if re.search(r"\b\d+\s*,\s*\d+", t):          # "10, 8, 6": sets counted from a list
        ok |= {str(n) for n in range(1, 11)}
    return ok


def numbers(text):
    return NUM.findall(text or "")


def new_numbers(said, plain):
    """Numbers in the plain line that the message didn't have, as digits or words (a time's ':00' counts as said)."""
    have = set(numbers(digits(said)))
    ok = extra_ok(said)
    half = "half" in (said or "").lower()
    out = set()
    for n in numbers(plain):
        if n in have or n in ok:
            continue
        if ":" in n:
            h, m = n.split(":")
            if (h in have or h in ok) and (m == "00" or m in have or m in ok):
                continue
        if half and n.endswith(".5") and n[:-2] in have:
            continue
        if n == "1" and re.search(r"(?<![\d.])1\s*x", plain):      # one set: "100 pushups" -> push-up 1x100
            continue
        out.add(n)
    return out


def meaning(plain, now=None):
    """What a plain line means, comparable between two readings: ('none',), ('sleep', ...) or ('workout', ...)."""
    from .sleeplog import parse_sleep
    from .fitness import parse_workout, describe
    p = (plain or "").strip()
    if not p or p.upper().startswith("NONE"):
        return ("none",)
    if p.lower().startswith("slept"):
        s = parse_sleep(p, now or time.time())
        if not s:
            return None
        return ("sleep", s["hours"], s["start"].strftime("%H:%M") if s.get("start") else None)
    w = parse_workout(p)
    return ("workout",) + tuple(describe(e) for e in w) if w else None


def kind_of(plain):
    m = meaning(plain)
    return m[0] if m else None


def _norm(text):
    t = digits((text or "").lower().replace("’", "'"))
    t = re.sub(r"[^\w:.\s/-]+", " ", t)
    t = re.sub(r"(?<!\d)[.:](?!\d)", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _words(text):
    return {("#" if NUM.fullmatch(w) else w) for w in re.findall(r"[a-z]+|\d{1,2}:\d{2}|\d+(?:\.\d+)?", _norm(text))
            if w not in STOP}


STOP = set("i a an the my me to at of and was is it for on in up by so".split()) - {"up", "in"}


def template(said, plain):
    """(regex for messages of the same shape, plain line with {0}, {1}... for the numbers), or None when the plain
    line has a number the message didn't, or the message is mostly numbers."""
    n = _norm(said)
    nums = numbers(n)
    if len(re.findall(r"[a-z]+", n)) < 2:
        return None
    parts = NUM.split(n)
    pattern = "".join(re.escape(p) + (r"(\d{1,2}:\d{2}|\d+(?:\.\d+)?)" if i < len(nums) else "")
                      for i, p in enumerate(parts))
    pattern = re.sub(r"(\\ )+", r"\\s*", pattern)
    if plain.strip().upper() == "NONE":
        return re.compile("^" + pattern + "$"), "NONE"
    used, out, pos = set(), [], 0
    for m in NUM.finditer(plain):
        cands = [i for i, x in enumerate(nums) if x == m.group(0)]
        if not cands:
            return None
        i = next((c for c in cands if c not in used), cands[0])
        used.add(i)
        out.append(plain[pos:m.start()].replace("{", "{{").replace("}", "}}") + "{" + str(i) + "}")
        pos = m.end()
    out.append(plain[pos:].replace("{", "{{").replace("}", "}}"))
    return re.compile("^" + pattern + "$"), "".join(out)


class Phrasebook:
    def __init__(self, store):
        self.store = store
        store.db.execute("""CREATE TABLE IF NOT EXISTS phrasebook (
            id INTEGER PRIMARY KEY, said TEXT NOT NULL, norm TEXT NOT NULL, plain TEXT NOT NULL, kind TEXT,
            source TEXT, status TEXT DEFAULT 'active', uses INTEGER DEFAULT 0, created REAL, updated REAL, note TEXT)""")
        store.db.execute("CREATE INDEX IF NOT EXISTS phrasebook_norm ON phrasebook(norm)")
        store.db.commit()
        self._compiled = None
        self.seed()

    # ---- keeping ----------------------------------------------------------------------------------------------------
    def seed(self):
        """The starter set; entries added in a later version are added on start, removed ones stay removed."""
        done = self.store.kv_get("phrasebook_starter") or []
        new = [(s, p) for s, p in STARTER if s not in done]
        for s, p in new:
            try:
                self.add(s, p, "starter")
            except ValueError as e:
                log.warning("starter entry %r: %s", s, e)
        if new:
            self.store.kv_set("phrasebook_starter", done + [s for s, _ in new])

    def check(self, said, plain, numbers_said=True):
        """ValueError unless the plain line is NONE or one the parsers read, with no number that wasn't said."""
        said, plain = (said or "").strip(), (plain or "").strip()
        if not said or not plain:
            raise ValueError("give what was said and the plain line")
        if len(said) > 300 or len(plain) > 200:
            raise ValueError("too long for a phrasebook entry")
        m = meaning(plain)
        if m is None:
            raise ValueError(f"the plain line {plain!r} isn't one Home Mind reads (e.g. 'slept from 12 am to 7 am', "
                             "'slept 6 hours', 'squat 3x8 at 185 lb', 'run 3 miles in 28 min', or NONE)")
        if numbers_said and m[0] != "none":
            extra = new_numbers(said, plain)
            if extra:
                raise ValueError(f"the plain line has numbers that weren't said: {', '.join(sorted(extra))}")
        return m[0]

    def add(self, said, plain, source="added", status="active", note="", numbers_said=True):
        kind = self.check(said, plain, numbers_said)
        plain = "NONE" if kind == "none" else plain.strip()
        n = _norm(said)
        now = time.time()
        old = self.store.db.execute("SELECT id, source FROM phrasebook WHERE norm=?", (n,)).fetchone()
        if old:
            if source == "grown" or (old["source"] == "correction" and source != "correction"):
                return self.get(old["id"])           # a correction is never overwritten by a guess
            self.store.db.execute("UPDATE phrasebook SET plain=?, kind=?, source=?, status=?, updated=?, note=? WHERE id=?",
                                  (plain, kind, source, status, now, note, old["id"]))
            rid = old["id"]
        else:
            rid = self.store.db.execute("INSERT INTO phrasebook(said, norm, plain, kind, source, status, created, updated, "
                                        "note) VALUES(?,?,?,?,?,?,?,?,?)",
                                        (said.strip(), n, plain, kind, source, status, now, now, note)).lastrowid
        self.store.db.commit()
        self._compiled = None
        return self.get(rid)

    def get(self, rid):
        r = self.store.db.execute("SELECT * FROM phrasebook WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None

    def set_status(self, rid, status):
        if status not in ("active", "pending", "rejected"):
            raise ValueError("status is active, pending or rejected")
        if not self.get(rid):
            raise ValueError(f"no phrasebook entry {rid}")
        self.store.db.execute("UPDATE phrasebook SET status=?, updated=? WHERE id=?", (status, time.time(), rid))
        self.store.db.commit()
        self._compiled = None
        return self.get(rid)

    def remove(self, rid):
        if not self.get(rid):
            raise ValueError(f"no phrasebook entry {rid}")
        self.store.db.execute("DELETE FROM phrasebook WHERE id=?", (rid,))
        self.store.db.commit()
        self._compiled = None
        return {"removed": rid}

    def used(self, rid):
        self.store.db.execute("UPDATE phrasebook SET uses=uses+1 WHERE id=?", (rid,))
        self.store.db.commit()

    def entries(self, status=None, source=None, limit=200):
        q, a = "SELECT * FROM phrasebook WHERE 1=1", []
        if status:
            q += " AND status=?"
            a.append(status)
        if source:
            q += " AND source=?"
            a.append(source)
        return [dict(r) for r in self.store.db.execute(q + " ORDER BY updated DESC LIMIT ?", a + [int(limit)])]

    def stats(self):
        rows = self.store.db.execute("SELECT source, status, kind, COUNT(*) n FROM phrasebook GROUP BY source, status, kind")
        out = {"active": 0, "pending": 0, "rejected": 0, "by_source": {}, "by_kind": {}}
        for r in rows:
            out[r["status"]] = out.get(r["status"], 0) + r["n"]
            if r["status"] == "active":
                out["by_source"][r["source"]] = out["by_source"].get(r["source"], 0) + r["n"]
                out["by_kind"][r["kind"]] = out["by_kind"].get(r["kind"], 0) + r["n"]
        out["last_grow"] = self.store.kv_get("phrasebook_grow_last")
        out["last_eval"] = self.store.kv_get("phrasebook_eval_last")
        return out

    def export(self):
        """Pairs for training a model (the FunctionGemma notebook, or any other)."""
        return [{"said": e["said"], "plain": e["plain"], "kind": e["kind"]} for e in self.entries("active", limit=100000)]

    # ---- reading ----------------------------------------------------------------------------------------------------
    def _templates(self):
        if self._compiled is None:
            out = []
            for e in self.entries("active", limit=100000):
                t = template(e["said"], e["plain"])
                if t:
                    out.append((t[0], t[1], e))
            # corrections first, then the most specific shapes
            out.sort(key=lambda x: (x[2]["source"] != "correction", -len(x[0].pattern)))
            self._compiled = out
        return self._compiled

    def match(self, said):
        """(entry, plain line) for a message with the same shape as an entry, or None. No model needed."""
        n = _norm(said)
        if not n:
            return None
        same = self.store.db.execute("SELECT * FROM phrasebook WHERE norm=? AND status='active'", (n,)).fetchone()
        if same:                       # the exact wording: a correction's own numbers, even ones that weren't said
            return dict(same), same["plain"]
        for rx, plain_t, e in self._templates():
            m = rx.match(n)
            if m:
                plain = plain_t if plain_t == "NONE" else plain_t.format(*m.groups())
                if plain == "NONE" or meaning(plain):
                    return e, plain
        return None

    def similar(self, said, k=6, kind=None):
        """The k entries most like the message (shared words, numbers counted as one word), best first."""
        w = _words(said)
        if not w:
            return []
        scored = []
        for e in self.entries("active", limit=100000):
            if kind and e["kind"] != kind:
                continue
            ew = _words(e["said"])
            s = len(w & ew) / (len(w | ew) or 1)
            if s > 0:
                scored.append((s, e))
        scored.sort(key=lambda x: -x[0])
        return [dict(e, score=round(s, 3)) for s, e in scored[:k]]

    def close(self, said):
        """How alike the message is to its nearest entry (0-1)."""
        s = self.similar(said, 1)
        return s[0]["score"] if s else 0.0

    def examples(self, said, k=6, exclude=None):
        """Few-shot lines for a model's prompt: the most similar entries, plus a NONE one so it knows it may say no."""
        picks = [e for e in self.similar(said, k + 2) if e["id"] != exclude][:k]
        if not any(e["kind"] == "none" for e in picks):
            no = [e for e in self.similar(said, 12, kind="none") if e["id"] != exclude][:1] or \
                [e for e in self.entries("active", source="starter", limit=500) if e["kind"] == "none"][:1]
            picks = picks[:k - 1] + no
        return "\n".join(f"Message: {e['said']}\nLine: {e['plain']}" for e in picks)

    # ---- growing ------------------------------------------------------------------------------------------------------
    GROW_TOPICS = [
        "last night's sleep, said casually, with bedtime and wake time in odd words (slang, story, alarms, kids, pets)",
        "last night's sleep given as a number of hours, vaguely or with complaints",
        "a weightlifting session in gym shorthand (weights, sets, reps in any order, several lifts)",
        "cardio: a run, walk, ride, swim, row or class, with distance or time said loosely",
        "messages that look like sleep or workout logs but are NOT: plans, questions, habits, other people, other "
        "activities with hours, other nights",
    ]
    GROW_SYSTEM = (
        "You write test cases for a home assistant that logs sleep and workouts. Reply with JSON only: a list of "
        "objects {\"said\": what a person types or says, \"plain\": the log line}. The plain line is one of:\n"
        "slept from <time> to <time> (e.g. slept from 11:30 pm to 6:45 am)\nslept <N> hours\n"
        "<exercise> <sets>x<reps> at <weight> lb|kg (several joined by ', '; no 'at' without a weight)\n"
        "<activity> <N> miles|km in <M> min (or '<activity> <M> min', or '<activity> <N> miles')\n"
        "NONE (not a log of last night's sleep or a workout the person did)\n"
        "Use only numbers the person says (as digits or words). Make them varied and realistic: slang, typos, "
        "shorthand, extra detail, odd word order. No two alike.")
    READ_SYSTEM = (
        "Rewrite the message as ONE log line in exactly one of these forms, in English, keeping the person's own "
        "numbers and units, or NONE if it isn't last night's sleep or a workout the person did:\n"
        "slept from <time> to <time>\nslept <N> hours\n<exercise> <sets>x<reps> at <weight> lb|kg\n"
        "<activity> <N> miles|km in <M> min\nNONE\nAnswer with the line only.")

    async def read(self, model, said, exclude=None, timeout=20):
        """One model's reading of a message, with the closest entries as examples (what Home Mind does at ask time)."""
        import asyncio
        ex = self.examples(said, exclude=exclude)
        system = self.READ_SYSTEM + ("\n\nExamples:\n" + ex if ex else "")
        r = await asyncio.wait_for(model.chat(system, [{"role": "user", "content": said}], max_tokens=60,
                                              temperature=0), timeout)
        line = (r.get("text") or "").strip().strip('"`').splitlines()
        line = line[0].strip() if line else ""
        return re.sub(r"^(?:line|answer)\s*:\s*", "", line, flags=re.I)

    async def grow(self, model, n=10, topics=None, timeout=90):
        """New hard cases from a model. Kept active when: the plain line parses, it adds no number, the parsers don't
        already read the message the same way, and a blind second reading agrees. A disagreement waits for review."""
        import asyncio
        from .sleeplog import parse_sleep
        from .fitness import parse_workout, describe, is_workout_log
        topics = topics or random.sample(self.GROW_TOPICS, min(2, len(self.GROW_TOPICS)))
        out = {"asked": 0, "kept": 0, "review": 0, "dropped": 0, "known": 0, "parser_misread": 0, "ids": []}
        for topic in topics:
            style = random.sample(self.entries("active", limit=500), min(4, len(self.entries("active", limit=500))))
            prompt = (f"Write {n} cases about: {topic}.\nFor the style, some existing ones:\n"
                      + "\n".join(json.dumps({"said": e["said"], "plain": e["plain"]}) for e in style))
            try:
                r = await asyncio.wait_for(model.chat(self.GROW_SYSTEM, [{"role": "user", "content": prompt}],
                                                      max_tokens=1500, temperature=0.9), timeout)
            except Exception as e:  # noqa: BLE001
                log.warning("phrasebook grow: %s", e)
                continue
            from .mind import parse_json
            txt = r.get("text") or ""
            cases = parse_json(txt) if txt.strip().startswith("{") else None
            if cases is None:
                try:
                    cases = json.loads(txt[txt.index("["):txt.rindex("]") + 1])
                except ValueError:
                    cases = []
            if isinstance(cases, dict):
                cases = cases.get("cases") or cases.get("items") or []
            for c in cases[:n * 2] if isinstance(cases, list) else []:
                if not isinstance(c, dict) or not isinstance(c.get("said"), str) or not isinstance(c.get("plain"), str):
                    out["dropped"] += 1
                    continue
                out["asked"] += 1
                said, plain = c["said"].strip(), c["plain"].strip()
                if self.store.db.execute("SELECT 1 FROM phrasebook WHERE norm=?", (_norm(said),)).fetchone():
                    out["known"] += 1
                    continue
                try:
                    self.check(said, plain)
                except ValueError:
                    out["dropped"] += 1
                    continue
                want = meaning(plain)
                # what the parsers make of it now: the same = nothing to learn; different = a case worth keeping
                ps = parse_sleep(said)
                pw = parse_workout(said) if is_workout_log(said) else []
                now = ("sleep", ps["hours"], ps["start"].strftime("%H:%M") if ps.get("start") else None) if ps else \
                    ("workout",) + tuple(describe(e) for e in pw) if pw else ("none",)
                if now == want:
                    out["known"] += 1
                    continue
                if now != ("none",):
                    out["parser_misread"] += 1
                try:
                    blind = await self.read(model, said)
                except Exception:  # noqa: BLE001
                    blind = ""
                agree = meaning(blind) == want
                e = self.add(said, plain, "grown", status="active" if agree else "pending",
                             note="" if agree else f"a second reading said: {blind or '(nothing)'}"
                             + (f"; the parsers read: {now}" if now != ('none',) else ""))
                out["ids"].append(e["id"])
                out["kept" if agree else "review"] += 1
        out["at"] = time.time()
        self.store.kv_set("phrasebook_grow_last", out)
        return out

    async def evaluate(self, model, limit=60, seed=7):
        """How well a model reads the phrasebook with the phrasebook's help: each entry read with the others as
        examples (never itself). 'wrong_log' is the one that matters most: a log where there should be none or a
        different one."""
        items = self.entries("active", limit=100000)
        random.Random(seed).shuffle(items)
        items = items[:limit]
        res = {"n": 0, "right": 0, "wrong_log": 0, "missed": 0, "errors": 0, "misses": []}
        for e in items:
            try:
                line = await self.read(model, e["said"], exclude=e["id"])
            except Exception:  # noqa: BLE001
                res["errors"] += 1
                continue
            res["n"] += 1
            got, want = meaning(line), meaning(e["plain"])
            if got and new_numbers(e["said"], line) and got[0] != "none":
                got = None                       # Home Mind would refuse it: a number nobody said
            if got == want:
                res["right"] += 1
                continue
            if got and got[0] != "none":
                res["wrong_log"] += 1
            else:
                res["missed"] += 1
            if len(res["misses"]) < 15:
                res["misses"].append({"said": e["said"], "want": e["plain"], "got": line})
        for k in ("right", "wrong_log", "missed"):
            res[k + "_share"] = round(res[k] / res["n"], 3) if res["n"] else None
        res["at"] = time.time()
        self.store.kv_set("phrasebook_eval_last", res)
        return res
