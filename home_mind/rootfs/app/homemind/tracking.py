"""Trackers, the medicines log, significant events, nudges, live exertion zones and travel mode (design spec step 28,
Throughout; phase 13).

Trackers (from Bearable). At most 3 per person; weekly by default; paused on low and rough days and in travel mode;
part of the one ask cap (asked as routine asks, so they wait for the morning and count against the day's budget).
Logging takes one word and keeps the time: "log headache", "headache 3", "fog 2". Details are asked later, on a
good day, and never as a follow-up question. Custom trackers are descriptive only: they never feed tests or
experiments.

The medicines log: "took ibuprofen 400" is kept with its time, and the reply says what it is: "Nobody reviews this.
No interaction checks." Never dosing guidance. It is its owner's (personal). A second log of the same medicine inside
its window (4 hours unless set) is answered with the fact only: "You logged ibuprofen at 8:05." A beta-blocker or
ivabradine in the log switches exertion zones off (they flatten heart rate).

Significant events: a weekly checklist of fixed kinds (a cold, a trip, a hard week, a new medicine, poor sleep, a
big event). Unanswered is unknown, never "none". They're kept as possible confounders: they can explain a change but
never silence the worsening alarm.

Nudges. Each has a trigger (a time, arriving at or leaving a place with a 10-minute debounce, "nothing logged by", a
sensor going on), one message, a daily cap of one, quiet hours (as every push), and stops after 3 ignored. A nudge
serves a goal the person wrote, and says which rule it comes from. Ignored = no "done" within 2 hours.

Live exertion zones (from Visible): off by default (option exertion_zones). The line is the clinician's (option
exertion_threshold) or resting heart rate + 15. Off automatically when a beta-blocker or ivabradine is logged. Only
readings that pass the data checks count (20 to 230 bpm, fresh); a zone must hold for two readings (about 10 s)
before it changes. Only per-minute summaries are kept, in a file of their own, never the raw stream. On a low day,
one neutral notice at most ("Heart's up. Rest when you can."), none on a rough day, and "not today" mutes it.

Travel mode: routine asks, trackers, nudges and experiments pause; the house is set to away where it has an away
switch (input_boolean.away or the option travel_away_entity). Safety and important messages still reach the phone.
"""
import json
import os
import re
import statistics
import time
from datetime import datetime, timedelta

MAX_TRACKERS = 3
CADENCE = {"daily": 86400, "weekly": 7 * 86400}
MED_WINDOW_H = 4
MEDS_NOTE = "Nobody reviews this. No interaction checks."
RATE_LOWERING = re.compile(r"\b(?:\w+olol|bisoprolol|metoprolol|propranolol|atenolol|carvedilol|nebivolol|"
                           r"ivabradine|corlanor|beta[- ]?blocker)\b", re.I)
EVENT_KINDS = ("a cold or infection", "a trip", "a hard week", "a new medicine", "poor sleep", "a big event")
LOG_RX = re.compile(r"^\s*(?:log|track)\s+([a-z][a-z' -]{1,30}?)(?:\s+(?:at\s+)?(\d{1,2}(?:\.\d)?))?\s*[.!]*$", re.I)
WORD_NUM = re.compile(r"^\s*([a-z][a-z' -]{1,30}?)\s+(\d{1,2}(?:\.\d)?)\s*[.!]*$", re.I)
MED_RX = re.compile(r"^\s*(?:(?:ok|so|yeah|um|well)[\s,]+)?(?:i\s+|i've\s+|ive\s+|i have\s+)?(?:just\s+|already\s+)?"
                    r"(?:took|had|taken)\s+(?:my\s+|a\s+|an\s+|some\s+|the\s+)?"
                    r"(?:(?:one|two|three|four|1|2|3|4|half an?|half)\s+)?(?:of\s+(?:my\s+)?)?"
                    r"(?:morning\s+|evening\s+|night\s+|bedtime\s+)?([a-z][a-z-]{2,30})"
                    r"(?:\s+(\d+(?:\.\d+)?)\s*(mg|mcg|ml|g|units?|iu)?)?\b", re.I)
NOT_MED = {"nap", "walk", "shower", "bath", "break", "rest", "look", "call", "photo", "picture", "bus", "train", "car",
           "drink", "coffee", "tea", "breakfast", "lunch", "dinner", "snack", "meal", "run", "seat", "turn", "trip",
           "muffin", "protein", "caffeine", "migraine", "headache", "phone", "someone", "bin", "bins", "wine",
           "chocolate", "cousin", "time", "chance", "day", "while", "minute", "moment", "second", "glass", "cup",
           "bite", "sip", "bite", "pizza", "toast", "cake", "dog", "kids", "rubbish", "trash", "nothing", "everything",
           "pain", "fever", "cold", "flu", "rough", "good", "bad", "great", "fine", "nice", "lovely", "long", "quick"}
# a medicine by its shape (drug-name endings) or a common name; anything else needs an amount or an earlier log
MEDISH = re.compile(r"(?:profen|amol|azole|prazole|statin|sartan|pril|olol|xetine|triptan|cillin|mycin|cycline|"
                    r"dronate|lukast|tadine|zepam|zolam|afil|gliptin|formin|tidine|dipine|codone|morphone|azepine|"
                    r"triptyline|pramine|semide|thiazide|oxacin|vudine|navir|parin|lamide|setron|zosin|sterone|"
                    r"olone|asone|ethasone|nisone|mab|tinib)$|"
                    r"^(?:pills?|tablets?|meds|medicine|medication|dose|tylenol|advil|aleve|motrin|aspirin|"
                    r"acetaminophen|paracetamol|ibuprofen|naproxen|vitamin\w*|magnesium|melatonin|iron|zinc|omega|"
                    r"insulin|inhaler|ventolin|antihistamine|benadryl|zyrtec|claritin|allegra|sudafed|tums|pepcid|"
                    r"imodium|vyvanse|adderall|ritalin|concerta|wellbutrin|zoloft|lexapro|prozac|xanax|ativan|"
                    r"klonopin|ldn|naltrexone|metformin|levothyroxine|synthroid|ozempic|wegovy|mounjaro|lithium|"
                    r"gabapentin|lyrica|propranolol|ivabradine|midodrine|fludrocortisone|nurtec|ubrelvy|sumatriptan|"
                    r"probiotic|probiotics|electrolytes|b12|d3|coq10|nac|quercetin|famotidine|loratadine|"
                    r"cetirizine|fexofenadine|ketotifen|cromolyn|montelukast)$", re.I)
# the same medicine under its other names: one name in the log, so the repeat guard sees a second dose
SAME_MED = {"tylenol": "acetaminophen", "paracetamol": "acetaminophen", "panadol": "acetaminophen",
            "advil": "ibuprofen", "motrin": "ibuprofen", "nurofen": "ibuprofen", "aleve": "naproxen",
            "benadryl": "diphenhydramine", "zyrtec": "cetirizine", "claritin": "loratadine", "allegra": "fexofenadine",
            "pepcid": "famotidine", "synthroid": "levothyroxine", "ldn": "naltrexone", "imodium": "loperamide"}
NUDGE_IGNORED_MAX = 3
NUDGE_IGNORE_SECONDS = 2 * 3600
DEBOUNCE = 600
ZONE_HOLD = 2


class Tracking:
    def __init__(self, store, day=None, clock=None, folder=None):
        self.store = store
        self.day = day
        self.clock = clock or time.time
        self.folder = folder
        with store.lock:
            store.db.executescript("""
CREATE TABLE IF NOT EXISTS trackers (id INTEGER PRIMARY KEY, owner TEXT, name TEXT NOT NULL, kind TEXT NOT NULL,
  cadence TEXT NOT NULL, question TEXT, created REAL, last_asked REAL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS tracker_entries (id INTEGER PRIMARY KEY, tracker_id INTEGER, owner TEXT, ts REAL NOT NULL,
  word TEXT, value REAL, details TEXT, confirmed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS meds_log (id INTEGER PRIMARY KEY, owner TEXT, ts REAL NOT NULL, name TEXT NOT NULL,
  amount TEXT);
CREATE TABLE IF NOT EXISTS sig_events (week TEXT NOT NULL, owner TEXT NOT NULL, kind TEXT NOT NULL, answer TEXT,
  PRIMARY KEY (week, owner, kind));
CREATE TABLE IF NOT EXISTS nudges (id INTEGER PRIMARY KEY, owner TEXT, goal TEXT NOT NULL, trigger TEXT NOT NULL,
  message TEXT NOT NULL, created REAL, last_sent REAL, ignored INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL
  DEFAULT 1, awaiting REAL);
""")
            store.db.commit()

    # ---- travel ---------------------------------------------------------------------------------------------------
    def travelling(self):
        return bool((self.store.kv_get("travel_mode") or {}).get("on"))

    def set_travel(self, on):
        self.store.kv_set("travel_mode", {"on": bool(on), "at": self.clock()})
        return {"on": bool(on)}

    def paused(self, person=None):
        """Trackers and nudges wait: travel, or a low or rough day."""
        if self.travelling():
            return "travel mode"
        st = self.day.get(person) if self.day is not None else "unknown"
        return f"a {st} day" if st in ("low", "rough") else None

    # ---- trackers -------------------------------------------------------------------------------------------------
    def trackers(self, owner=None):
        q = "SELECT * FROM trackers WHERE active=1" + (" AND owner=?" if owner else "") + " ORDER BY id"
        return [dict(r) for r in self.store.db.execute(q, (owner,) if owner else ())]

    def add_tracker(self, name, owner=None, kind="scale", cadence="weekly", question=None):
        name = (name or "").strip().lower()
        if not name:
            raise ValueError("a tracker needs a name")
        if cadence not in CADENCE:
            raise ValueError("cadence is daily or weekly")
        if kind not in ("scale", "yesno", "word"):
            raise ValueError("kind is scale, yesno or word")
        if len(self.trackers(owner)) >= MAX_TRACKERS:
            raise ValueError(f"at most {MAX_TRACKERS} trackers: stop one first")
        with self.store.lock:
            cur = self.store.db.execute("INSERT INTO trackers (owner, name, kind, cadence, question, created) VALUES "
                                        "(?,?,?,?,?,?)", (owner, name, kind, cadence,
                                                          question or f"{name.capitalize()} this week?", self.clock()))
            self.store.db.commit()
        return cur.lastrowid

    def stop_tracker(self, tid):
        with self.store.lock:
            self.store.db.execute("UPDATE trackers SET active=0 WHERE id=?", (tid,))
            self.store.db.commit()

    def due(self, owner=None):
        """Trackers to ask now (as routine asks): none while paused."""
        if self.paused(owner):
            return []
        now = self.clock()
        return [t for t in self.trackers(owner) if now - (t["last_asked"] or t["created"] or 0) >= CADENCE[t["cadence"]]]

    def asked(self, tid):
        with self.store.lock:
            self.store.db.execute("UPDATE trackers SET last_asked=? WHERE id=?", (self.clock(), tid))
            self.store.db.commit()

    def log_said(self, text, owner=None):
        """'log headache' / 'headache 3' / 'fog 2' for a tracker this person has: one entry with the time. Returns
        the reply, or None if it isn't a log."""
        t = (text or "").strip()
        m = LOG_RX.match(t) or WORD_NUM.match(t)
        if not m:
            return None
        word = m.group(1).strip().lower()
        val = float(m.group(2)) if m.group(2) else None
        tr = next((x for x in self.trackers(owner) if x["name"] == word or word.startswith(x["name"])), None)
        if tr is None:
            return None        # not one of this person's trackers: left for the rest ("log a run" is fitness's)
        with self.store.lock:
            self.store.db.execute("INSERT INTO tracker_entries (tracker_id, owner, ts, word, value) VALUES (?,?,?,?,?)",
                                  (tr["id"] if tr else None, owner, self.clock(), word, val))
            self.store.db.commit()
        when = datetime.fromtimestamp(self.clock()).strftime("%-I:%M %p").lower()
        return f"Logged {word}{f' {val:g}' if val is not None else ''} at {when}."

    def to_confirm(self, owner=None):
        """Entries whose details wait for a good day (never a follow-up question)."""
        if self.day is not None and self.day.get(owner) != "good":
            return []
        return [dict(r) for r in self.store.db.execute(
            "SELECT * FROM tracker_entries WHERE confirmed=0 AND (owner IS ? OR owner=?) ORDER BY ts LIMIT 5",
            (owner, owner))]

    def confirm(self, entry_id, details=None):
        with self.store.lock:
            self.store.db.execute("UPDATE tracker_entries SET confirmed=1, details=COALESCE(?, details) WHERE id=?",
                                  (details, entry_id))
            self.store.db.commit()

    def entries(self, tid, days=60):
        return [dict(r) for r in self.store.db.execute(
            "SELECT ts, word, value FROM tracker_entries WHERE tracker_id=? AND ts>=? ORDER BY ts",
            (tid, self.clock() - days * 86400))]

    # ---- the medicines log ------------------------------------------------------------------------------------------
    def med_said(self, text, owner=None):
        t = (text or "").replace("’", "'")
        if "?" in t:
            return None           # "had my meds?" is a question, never a dose
        m = MED_RX.match(t)
        if not m:
            return None
        said, amount = m.group(1).lower(), " ".join(x for x in (m.group(2), m.group(3)) if x)
        kind = re.match(r"\s+(pills?|tablets?|meds|medicine|medication|inhaler|spray|drops)\b", t[m.end(1):], re.I)
        if kind and not amount:
            said = f"{said} {kind.group(1).lower()}"      # "my migraine pill", "my allergy meds": that's the name
        elif said in NOT_MED:
            return None
        name = SAME_MED.get(said, said)
        known = {r["name"] for r in self.store.db.execute("SELECT DISTINCT name FROM meds_log WHERE owner IS ? OR "
                                                          "owner=?", (owner, owner))}
        if not (kind and not amount or name in known or said in known or MEDISH.search(said)
                or amount and re.search(r"\b(?:mg|mcg|iu|units?)\b", amount)):
            return None           # "I took the dog out", "had a muffin 2" aren't medicines
        windows = self.store.kv_get("med_windows") or {}
        win = float(windows.get(name, MED_WINDOW_H)) * 3600
        prev = self.store.db.execute("SELECT ts FROM meds_log WHERE name=? AND (owner IS ? OR owner=?) AND ts>=? "
                                     "ORDER BY ts DESC LIMIT 1", (name, owner, owner, self.clock() - win)).fetchone()
        with self.store.lock:
            self.store.db.execute("INSERT INTO meds_log (owner, ts, name, amount) VALUES (?,?,?,?)",
                                  (owner, self.clock(), name, amount or None))
            self.store.db.commit()
        if name not in known:
            cb = getattr(self, "on_new_medicine", None)     # experiments.py: a new medicine ends the current pair
            if cb is not None:
                try:
                    cb(name)
                except Exception:  # noqa: BLE001
                    pass
        if RATE_LOWERING.search(name):
            self.store.kv_set("zones_blocked", {"by": name, "at": self.clock()})
        if prev:
            at = datetime.fromtimestamp(prev["ts"]).strftime("%-I:%M %p").lower()
            return {"answer": f"You logged {name} at {at}. I've logged this one too.", "med": name, "repeat": True}
        return {"answer": f"Logged {name}{' ' + amount if amount else ''}. {MEDS_NOTE}", "med": name}

    def set_window(self, name, hours):
        w = dict(self.store.kv_get("med_windows") or {})
        w[name.lower()] = float(hours)
        self.store.kv_set("med_windows", w)

    def meds(self, owner=None, days=30):
        return [dict(r) for r in self.store.db.execute(
            "SELECT ts, name, amount FROM meds_log WHERE (owner IS ? OR owner=?) AND ts>=? ORDER BY ts DESC",
            (owner, owner, self.clock() - days * 86400))]

    # ---- significant events -----------------------------------------------------------------------------------------
    def week(self):
        return time.strftime("%G-W%V", time.localtime(self.clock()))

    def events_answer(self, owner, answers):
        """answers: {kind: "yes" | "no"}; kinds not answered stay unknown."""
        wk = self.week()
        with self.store.lock:
            for k, v in answers.items():
                if k in EVENT_KINDS and v in ("yes", "no"):
                    self.store.db.execute("INSERT OR REPLACE INTO sig_events (week, owner, kind, answer) VALUES "
                                          "(?,?,?,?)", (wk, owner or "_", k, v))
            self.store.db.commit()
        return self.events(owner)

    def events(self, owner=None, week=None):
        wk = week or self.week()
        got = {r["kind"]: r["answer"] for r in self.store.db.execute(
            "SELECT kind, answer FROM sig_events WHERE week=? AND owner=?", (wk, owner or "_"))}
        return {k: got.get(k, "unknown") for k in EVENT_KINDS}

    # ---- nudges -----------------------------------------------------------------------------------------------------
    def add_nudge(self, goal, trigger, message, owner=None):
        if not (goal or "").strip():
            raise ValueError("a nudge serves a goal you wrote: give the goal")
        t = dict(trigger or {})
        if t.get("type") not in ("time", "arrive", "leave", "nothing_logged_by", "sensor_on"):
            raise ValueError("trigger type is time, arrive, leave, nothing_logged_by or sensor_on")
        with self.store.lock:
            cur = self.store.db.execute("INSERT INTO nudges (owner, goal, trigger, message, created) VALUES "
                                        "(?,?,?,?,?)", (owner, goal.strip(), json.dumps(t), message.strip()[:200],
                                                        self.clock()))
            self.store.db.commit()
        return cur.lastrowid

    def nudges(self, owner=None, active=True):
        q = "SELECT * FROM nudges" + (" WHERE active=1" if active else "") + " ORDER BY id"
        out = [dict(r, trigger=json.loads(r["trigger"])) for r in self.store.db.execute(q)]
        return [n for n in out if owner is None or n["owner"] in (None, owner)]

    def _fires(self, n, ctx):
        t, now = n["trigger"], datetime.fromtimestamp(self.clock())
        typ = t.get("type")
        if typ == "time":
            h, m = map(int, str(t.get("at", "09:00")).split(":"))
            return (now.hour, now.minute) >= (h, m) and now.hour < h + 2
        if typ in ("arrive", "leave"):
            ch = (ctx.get("place_changes") or {}).get(n["owner"] or "_")
            if not ch or ch.get("to" if typ == "arrive" else "from") != t.get("place"):
                return False
            return self.clock() - ch.get("at", 0) >= DEBOUNCE      # 10 minutes there (or gone) before it counts
        if typ == "nothing_logged_by":
            h, m = map(int, str(t.get("at", "20:00")).split(":"))
            if (now.hour, now.minute) < (h, m):
                return False
            start = datetime.combine(now.date(), datetime.min.time()).timestamp()
            return not self.store.db.execute("SELECT 1 FROM tracker_entries WHERE ts>=? AND word=? LIMIT 1",
                                             (start, (t.get("tracker") or "").lower())).fetchone()
        if typ == "sensor_on":
            return (ctx.get("states") or {}).get(t.get("entity_id")) == "on"
        return False

    def nudges_due(self, ctx=None):
        """Nudges to send now: firing, not sent today, not paused, not stopped. Also counts the ignored ones."""
        ctx = ctx or {}
        out = []
        today = datetime.fromtimestamp(self.clock()).date()
        for n in self.nudges():
            if n["awaiting"] and self.clock() - n["awaiting"] >= NUDGE_IGNORE_SECONDS:
                self._ignored(n)
                n = dict(n, ignored=n["ignored"] + 1, awaiting=None)
                if n["ignored"] >= NUDGE_IGNORED_MAX:
                    continue
            if self.paused(n["owner"]):
                continue
            if n["last_sent"] and datetime.fromtimestamp(n["last_sent"]).date() == today:
                continue
            if self._fires(n, ctx):
                out.append(n)
        return out

    def sent(self, nid):
        with self.store.lock:
            self.store.db.execute("UPDATE nudges SET last_sent=?, awaiting=? WHERE id=?", (self.clock(), self.clock(), nid))
            self.store.db.commit()

    def _ignored(self, n):
        stop = n["ignored"] + 1 >= NUDGE_IGNORED_MAX
        with self.store.lock:
            self.store.db.execute("UPDATE nudges SET ignored=ignored+1, awaiting=NULL, active=? WHERE id=?",
                                  (0 if stop else 1, n["id"]))
            self.store.db.commit()

    def nudge_done(self, nid):
        with self.store.lock:
            self.store.db.execute("UPDATE nudges SET ignored=0, awaiting=NULL WHERE id=?", (nid,))
            self.store.db.commit()

    def nudge_message(self, n):
        return f"{n['message']} (Your goal: {n['goal']}.)"


# ---- exertion zones -------------------------------------------------------------------------------------------------
class Zones:
    """Live exertion zones. Off unless turned on; never stores raw readings."""

    def __init__(self, store, opts, day=None, clock=None, folder=None):
        self.store = store
        self.opts = opts or {}
        self.day = day
        self.clock = clock or time.time
        self.folder = folder
        self.zone = "below"
        self._pending = (None, 0)
        self._minute = None

    def on(self):
        return bool(self.opts.get("exertion_zones")) and not self.store.kv_get("zones_blocked")

    def why_off(self):
        if not self.opts.get("exertion_zones"):
            return "off (the default): turn on exertion_zones to use them"
        b = self.store.kv_get("zones_blocked")
        return f"off: {b['by']} in the medicines log flattens heart rate" if b else None

    def threshold(self, resting=None):
        t = self.opts.get("exertion_threshold")
        if t:
            return float(t)
        return float(resting) + 15 if resting else None

    def reading(self, bpm, at=None, resting=None):
        """One reading. Returns the zone change ("above" / "below") or None."""
        if not self.on():
            return None
        try:
            bpm = float(bpm)
        except (TypeError, ValueError):
            return None
        now = self.clock()
        if not (20 <= bpm <= 230) or (at is not None and now - at > 120):
            return None                       # fails the data checks: doesn't count
        th = self.threshold(resting)
        if th is None:
            return None
        self._summarise(bpm, now)
        z = "above" if bpm >= th else "below"
        if z == self.zone:
            self._pending = (None, 0)
            return None
        cand, n = self._pending
        n = n + 1 if cand == z else 1
        self._pending = (z, n)
        if n >= ZONE_HOLD:                    # held for two readings (about 10 s) before it changes
            self.zone, self._pending = z, (None, 0)
            return z
        return None

    def _summarise(self, bpm, now):
        """Per-minute summaries only, in a file of their own; the raw readings are never written."""
        m = int(now // 60)
        if self._minute is None or self._minute[0] != m:
            if self._minute is not None and self.folder:
                vals = self._minute[1]
                os.makedirs(self.folder, exist_ok=True)
                with open(os.path.join(self.folder, "zones.jsonl"), "a") as f:
                    f.write(json.dumps({"minute": self._minute[0] * 60, "n": len(vals), "mean": round(
                        statistics.mean(vals), 1), "max": max(vals), "above": self.zone == "above"}) + "\n")
            self._minute = (m, [])
        self._minute[1].append(bpm)

    def notice(self, person=None):
        """On a low day, at most one neutral notice a day; none on a rough day; 'not today' mutes it."""
        state = self.day.get(person) if self.day is not None else "unknown"
        if state != "low":
            return None
        today = datetime.fromtimestamp(self.clock()).date().isoformat()
        k = self.store.kv_get("zones_notice") or {}
        if k.get("sent") == today or k.get("muted") == today:
            return None
        self.store.kv_set("zones_notice", dict(k, sent=today))
        return "Heart's up. Rest when you can."

    def mute_today(self):
        k = self.store.kv_get("zones_notice") or {}
        self.store.kv_set("zones_notice", dict(k, muted=datetime.fromtimestamp(self.clock()).date().isoformat()))

    def unblock(self):
        self.store.kv_set("zones_blocked", None)


def week_start(ts):
    d = datetime.fromtimestamp(ts).date()
    return d - timedelta(days=d.weekday())
