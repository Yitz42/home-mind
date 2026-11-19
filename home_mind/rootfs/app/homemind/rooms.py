"""Rooms, backing off, surprises and conflicts (design spec steps 24, 25 and 34, Records > Now; phase 11).

The room model: who is in each room right now, inside the system only. From what Home Assistant already knows:
people home or out (person entities), rooms with movement in the last 5 minutes (motion and occupancy sensors, by
area), and who was last heard by the satellite in a room (their voice, 10 minutes). Kept in memory, never stored, so
there is no history of where anyone was. Each person sees only their own place; nobody can ask where someone else
is ("where's Dana?" gets "I don't keep track of where people are"). Where things are ("where's the drill?") is
unaffected. It makes "alone" (who may hear a private answer) a question about the room, not the house.

Backing off (step 25): when a person changes something Home Mind just did, or changes a device by hand, Home Mind's
own automations leave that device alone for 3 hours. A request a person makes still works.

Surprises and gaps (step 24), one queue: missed predictions (a device didn't do what was expected), failed actions,
corrections ("that's wrong"), requests nothing could do ("nothing on the control list fits"), and questions the notes
couldn't answer. Each is one row; counts by kind feed the overnight gap list (step 32).

Settling conflicts (step 34): when people want different things from the same device, the one in that room wins; if
several are (or nobody is), a number goes to the middle of what they want, and on/off goes to what is more careful
(off for heat and noise, on for light when someone is there). Between learned records that match the same request,
the tie-break in learned.py decides, and with no tie-break nothing acts.
"""
import re
import statistics
import time

MOTION_SECONDS = 300
HEARD_SECONDS = 600
BACKOFF_SECONDS = 3 * 3600


class RoomModel:
    def __init__(self, home, clock=None):
        self.home = home
        self.clock = clock or time.time
        self.heard = {}           # room -> (person, at): the last voice a satellite there knew

    def heard_in(self, room, person):
        if room and person:
            self.heard[str(room).lower()] = (person, self.clock())

    def occupied(self):
        """Rooms with movement in the last 5 minutes."""
        areas = getattr(self.home, "areas", {}) or {}
        out = set()
        now = self.clock()
        for eid, c in (self.home.catalog or {}).items():
            if not eid.startswith("binary_sensor.") or c.get("state") != "on":
                continue
            dc = ((c.get("attrs") or {}).get("device_class") or "")
            if dc not in ("motion", "occupancy", "presence") and not re.search(r"motion|occupan|presence", eid):
                continue
            changed = c.get("last_changed_ts") or now
            if now - changed <= MOTION_SECONDS and areas.get(eid):
                out.add(areas[eid])
        return out

    def people_home(self):
        return {e: c.get("state") == "home" for e, c in (self.home.catalog or {}).items() if e.startswith("person.")}

    def where(self, person):
        """Only for that person themselves: the room they were last heard in, if recent."""
        now = self.clock()
        best = None
        for room, (p, at) in self.heard.items():
            if p.lower() == str(person or "").lower() and now - at <= HEARD_SECONDS and (best is None or at > best[1]):
                best = (room, at)
        return best[0] if best else None

    def in_room(self, room):
        now = self.clock()
        r = (room or "").lower()
        return sorted({p for rm, (p, at) in self.heard.items() if rm == r and now - at <= HEARD_SECONDS})

    def alone(self, person):
        """Is the person on their own? In their room if Home Mind knows it (nobody else heard there lately), else in
        the house. Unknown: not alone (careful)."""
        room = self.where(person)
        if room:
            others = [p for p in self.in_room(room) if p.lower() != str(person).lower()]
            return not others
        home = self.people_home()
        if not home:
            return False
        return sum(1 for v in home.values() if v) <= 1


WHERE = re.compile(r"\b(?:where(?:'s| is| are)|where did|is \w+ (?:home|in|at|out)|has \w+ (?:left|gone|come home)|"
                   r"when did \w+ (?:leave|get home|come home))\b", re.I)


def where_question(text, names):
    """A question about where a person is (not a thing): True. names: the household's people."""
    t = (text or "").replace("’", "'")
    if not WHERE.search(t):
        return False
    low = t.lower()
    return any(re.search(rf"\b{re.escape(n.lower())}(?:'s)?\b", low) for n in names if n) or \
        bool(re.search(r"\bwhere(?:'s| is| are) (?:my )?(?:wife|husband|partner|son|daughter|kids?|mum|mom|dad|"
                       r"roommate|everyone|everybody|anyone|someone)\b", low))


WHERE_ANSWER = "I don't keep track of where people are, so I can't say."


class BackOff:
    KV = "backoff"

    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or time.time

    def mark(self, entity_id, why="changed by hand", seconds=BACKOFF_SECONDS):
        d = dict(self.store.kv_get(self.KV) or {})
        now = self.clock()
        d = {k: v for k, v in d.items() if v.get("until", 0) > now}
        d[entity_id] = {"until": now + seconds, "why": why}
        self.store.kv_set(self.KV, d)

    def active(self, entity_id):
        v = (self.store.kv_get(self.KV) or {}).get(entity_id)
        return bool(v) and v.get("until", 0) > self.clock()

    def table(self):
        now = self.clock()
        return {k: v for k, v in (self.store.kv_get(self.KV) or {}).items() if v.get("until", 0) > now}


class Surprises:
    KINDS = ("missed", "failed", "corrected", "cant_do", "dont_know")

    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or time.time
        with store.lock:
            store.db.execute("CREATE TABLE IF NOT EXISTS surprises (id INTEGER PRIMARY KEY, ts REAL NOT NULL, "
                             "kind TEXT NOT NULL, what TEXT NOT NULL, ref TEXT, status TEXT NOT NULL DEFAULT 'open', "
                             "UNIQUE(kind, ref))")
            store.db.commit()

    def add(self, kind, what, ref=None, ts=None):
        if kind not in self.KINDS:
            raise ValueError(kind)
        with self.store.lock:
            self.store.db.execute("INSERT OR IGNORE INTO surprises (ts, kind, what, ref) VALUES (?,?,?,?)",
                                  (ts or self.clock(), kind, str(what)[:300], ref))
            self.store.db.commit()

    def collect(self, dlog=None, cases=None):
        """Fill the queue from the decision log, corrections and the ask log since last time."""
        cur = dict(self.store.kv_get("surprises_cursor") or {})
        n = 0
        if dlog is not None:
            with dlog.lock:
                rows = dlog.db.execute("SELECT id, ts, entity_id, action, status, seen, reason, request FROM decisions "
                                       "WHERE id>? ORDER BY id LIMIT 1000", (cur.get("d", 0),)).fetchall()
            for r in rows:
                cur["d"] = r["id"]
                what = r["request"] or f"{r['action']} {r['entity_id']}"
                if r["status"] == "done" and r["seen"] == "mismatch":
                    self.add("missed", f"{what}: {r['entity_id']} didn't do what was expected", f"d{r['id']}", r["ts"])
                    n += 1
                elif r["status"] in ("error", "failed"):
                    self.add("failed", f"{what}: {r['reason'] or 'failed'}", f"d{r['id']}", r["ts"])
                    n += 1
        if cases is not None:
            for c in cases.all():
                if c["id"] > cur.get("c", 0):
                    cur["c"] = c["id"]
                    self.add("corrected", f"{c['question']} -> {c['right']}", f"c{c['id']}", c.get("ts"))
                    n += 1
        rows = self.store.db.execute("SELECT id, ts, type, data FROM events WHERE id>? AND type IN ('ask', 'cant_do') "
                                     "ORDER BY id LIMIT 2000", (cur.get("e", 0),)).fetchall()
        import json
        for r in rows:
            cur["e"] = r["id"]
            try:
                d = json.loads(r["data"] or "{}")
            except ValueError:
                continue
            if r["type"] == "cant_do":
                self.add("cant_do", d.get("request") or "?", f"e{r['id']}", r["ts"])
                n += 1
            elif re.search(r"\b(?:don'?t know|no (?:note|record)|nothing in memory)\b", d.get("answer") or "", re.I):
                self.add("dont_know", d.get("question") or "?", f"e{r['id']}", r["ts"])
                n += 1
        self.store.kv_set("surprises_cursor", cur)
        return n

    def open(self, kind=None, limit=100):
        q = "SELECT * FROM surprises WHERE status='open'" + (" AND kind=?" if kind else "") + " ORDER BY ts DESC LIMIT ?"
        return [dict(r) for r in self.store.db.execute(q, ((kind, limit) if kind else (limit,)))]

    def counts(self, days=7):
        since = self.clock() - days * 86400
        return {r["kind"]: r["n"] for r in self.store.db.execute(
            "SELECT kind, COUNT(*) n FROM surprises WHERE ts>=? GROUP BY kind", (since,))}

    def seen(self, sid):
        with self.store.lock:
            self.store.db.execute("UPDATE surprises SET status='seen' WHERE id=?", (sid,))
            self.store.db.commit()


CAREFUL_OFF = ("climate", "water_heater", "media_player", "fan", "humidifier")


def settle(entity_id, wants, room=None, rooms=None, home=None):
    """wants: {person: value}. rooms: {person: room they're in}. home: {person: bool}. Returns (value, why)."""
    if not wants:
        return None, "nobody wants anything"
    rooms, home = rooms or {}, home or {}
    here = {p: v for p, v in wants.items() if room and rooms.get(p) == room}
    pool = here or {p: v for p, v in wants.items() if home.get(p, True)} or wants
    vals = list(pool.values())
    if len(set(map(str, vals))) == 1:
        return vals[0], ("the person in the room" if here else "everyone agrees")
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(nums) == len(vals):
        return statistics.median(nums), "the middle of what people want"
    dom = entity_id.split(".")[0]
    if set(map(str, vals)) <= {"on", "off"}:
        return ("off" if dom in CAREFUL_OFF else "on"), "the careful choice"
    return vals[0], "the first who asked"
