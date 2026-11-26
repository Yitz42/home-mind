"""The now board: working memory (design spec step 14 and Records › Now).

A small table of what is true right now, per person and per room, each entry with an expiry. One writer (this class,
called from code); every answer's brief reads a snapshot, so the model sees "NOW:" lines before the notes. Fading is
just the expiry. The model may only propose entries (propose()): typed, short, time-limited, checked here before
anything is written.

What goes on it now:
  checkin     how the day is going (per person)
  home        who is home: each person sees only their own presence; nobody can ask where someone else is
  did         what Home Mind did in the last 15 minutes
  waiting     an approval or a yes it is waiting for
  proposed    the model's own notes: waiting_on ("Anna's answer about Friday"), unsure_which ("which lamp")

Some days, at random per person (about 1 in 7), the board is left out of the brief, so its worth can be measured
against days without it (counted as board_off_days).
"""
import hashlib
import json
import time
import re
from datetime import date, datetime, timedelta

KINDS = ("checkin", "home", "did", "waiting", "waiting_on", "unsure_which", "doing", "note")
MODEL_KINDS = ("waiting_on", "unsure_which", "doing", "note")
MAX_TTL = 24 * 3600
MAX_VALUE = 140
OFF_ONE_IN = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS now_board (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, value TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
  owner TEXT, room TEXT, set_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS now_board_key ON now_board(key, owner);
"""


class NowBoard:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or time.time
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()

    def set(self, key, value, kind, ttl, source="code", owner=None, room=None):
        """Write (or replace) one entry. The only writer."""
        if kind not in KINDS:
            raise ValueError(f"kind is one of {', '.join(KINDS)}")
        value = str(value or "").strip()[:MAX_VALUE]
        if not value:
            return None
        now = self.clock()
        ttl = max(1, min(int(ttl), MAX_TTL))
        with self.store.lock:
            self.store.db.execute("DELETE FROM now_board WHERE key=? AND owner IS ?", (key, owner))
            cur = self.store.db.execute(
                "INSERT INTO now_board (key, value, kind, source, owner, room, set_at, expires_at) VALUES "
                "(?,?,?,?,?,?,?,?)", (key, value, kind, source, owner, room, now, now + ttl))
            self.store.db.commit()
        return cur.lastrowid

    def clear(self, key, owner=None):
        with self.store.lock:
            self.store.db.execute("DELETE FROM now_board WHERE key=? AND owner IS ?", (key, owner))
            self.store.db.commit()

    def propose(self, kind, value, owner=None, ttl=3600):
        """An entry the model suggests: only its own kinds, short, at most a day, no instructions. Returns the id or
        raises ValueError."""
        if kind not in MODEL_KINDS:
            raise ValueError(f"the model may propose only {', '.join(MODEL_KINDS)}")
        from .store import INSTRUCTION
        v = str(value or "").strip()
        if not v or len(v) > MAX_VALUE or INSTRUCTION.search(v):
            raise ValueError("a proposed entry is a short note, not an instruction")
        key = f"{kind}:{hashlib.sha1(v.lower().encode()).hexdigest()[:8]}"
        return self.set(key, v, kind, min(int(ttl), MAX_TTL), source="model", owner=owner)

    def snapshot(self, viewer=None, room=None):
        """What is current, for a viewer: household entries and their own (None = sees all, like the panel)."""
        now = self.clock()
        with self.store.lock:
            self.store.db.execute("DELETE FROM now_board WHERE expires_at < ?", (now - 3600,))
            rows = [dict(r) for r in self.store.db.execute(
                "SELECT * FROM now_board WHERE expires_at >= ? ORDER BY set_at DESC", (now,))]
        out = []
        for r in rows:
            if viewer is not None and r["owner"] and r["owner"].lower() != (viewer or "").lower():
                continue            # someone else's: their check-in, their presence, their notes
            if room and r["room"] and r["room"] != room:
                continue
            out.append(r)
        return out

    def off_today(self, person, day=None):
        """Is today a board-off day for this person (about 1 in 7, fixed per person and day)?"""
        d = (day or date.fromtimestamp(self.clock())).isoformat()
        h = int(hashlib.sha1(f"{(person or '').lower()}|{d}".encode()).hexdigest(), 16)
        return h % OFF_ONE_IN == 0

    def lines(self, viewer=None, room=None):
        """The brief's NOW: block (empty on a board-off day)."""
        if viewer is not None and self.off_today(viewer):
            return ""
        snap = self.snapshot(viewer, room)
        if not snap:
            return ""
        return "NOW (current, expires on its own):\n" + "\n".join(f"- {r['value']}" for r in snap[:12])

    # ---- what code puts there ----------------------------------------------------------------------------------
    def refresh(self, app):
        """Rebuild the code-written entries from the app's state (called every 20 s by the upkeep loop)."""
        day = getattr(app, "day", None)
        people = []
        try:
            people = app.people()
        except Exception:  # noqa: BLE001
            pass
        for p in people:
            if day is not None:
                s = day.get(p["name"])
                if s != "unknown":
                    self.set("checkin", f"{p['name']}'s day: {s}", "checkin", 6 * 3600, owner=p["name"])
            for e in p.get("persons") or []:
                c = (app.home.catalog or {}).get(e) or {}
                if c.get("state"):
                    self.set("home", f"{p['name']} is {where_from(c['state'])}", "home", 600, owner=p["name"])
        dlog = getattr(app, "decisions", None)
        if dlog is not None:
            for d in dlog.recent(5, status="done", since=self.clock() - 900):
                name = (app.home.catalog.get(d["entity_id"]) or {}).get("name") or d["entity_id"]
                self.set(f"did:{d['entity_id']}", f"Home Mind {d['action']} the {name} "
                         f"({int((self.clock() - d['ts']) // 60)} min ago)", "did", 900,
                         owner=d.get("person") or None)
        ap = getattr(app, "approvals", None)
        if ap is not None:
            ap.prune()
            for aid, p in list(ap.pending.items()):
                self.set(f"waiting:{aid}", f"Waiting for phone approval to {p['verb']} the {p['name']}", "waiting",
                         120, owner=(p.get("why") or {}).get("person") or None)
        return True

    def dump(self):
        return json.dumps(self.snapshot(), default=str)


def where_from(state):
    """A person entity's state in words: home, away, or the zone they're in ("at UMBC"), not just "out"."""
    st = str(state or "").strip()
    if st == "home":
        return "home"
    if st in ("not_home", "away", "", "unknown", "unavailable"):
        return "away" if st in ("not_home", "away") else "somewhere unknown"
    return f"at {st}"


SLEEP_ENTITY = re.compile(r"^(?:switch\.[a-z0-9_]*sleep_mode[a-z0-9_]*|input_boolean\.[a-z0-9_]*(?:asleep|sleeping|sleep_mode)[a-z0-9_]*)$")


_DATE_TAIL = re.compile(r"\s*\((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.? [A-Z][a-z]{2,8} \d{1,2},? \d{4}\)\s*$")


def _bare_title(t):
    """A calendar note's title without the date it was filed with ("Work Remote (Wednesday Nov 25 2026)"), and with a
    run of spaces or tabs between two parts ("Automatic Control          Dr. Wu Office Hours") as one " · ": the run
    used up the watch's 30 characters, so only the first part showed."""
    t = _DATE_TAIL.sub("", (t or "").strip())
    t = re.sub(r"[ \t\u00a0]{2,}|\s*[\r\n\t]+\s*", " · ", t)
    return re.sub(r"\s+", " ", t).strip(" ·")


def _wrap(text, width=30, most=2):
    """Up to `most` lines of at most `width` characters, broken between words (a word longer than a line is cut)."""
    out, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > width:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        out.append(cur)
    return [x[:width] for x in out[:most]]


def situation(app, viewer=None, now=None, events=2, tasks=3):
    """The SITUATION block every answer gets (0.41), board-off days too: the time, where the person asking is, the next
    calendar events, whether the house is in sleep mode, and the top open tasks. Built fresh at question time from
    what Home Mind already holds (no Home Assistant call). Only what the viewer may see: their own presence (a guest
    gets none), visible calendar events by title and time (never an invite's description), visible tasks."""
    now = now or datetime.now()
    out = [f"It's {now.strftime('%A, %B %d, %I:%M %p').replace(' 0', ' ')}."]
    catalog = getattr(getattr(app, "home", None), "catalog", None) or {}
    try:
        people = app.people() if hasattr(app, "people") else []
    except Exception:  # noqa: BLE001
        people = []
    if viewer != "":
        for p in people:
            if viewer is not None and (p.get("name") or "").lower() != viewer.lower():
                continue
            for e in p.get("persons") or []:
                c = catalog.get(e) or {}
                if c.get("state"):
                    out.append(f"{p['name']} is {where_from(c['state'])}.")
                    break
    cal = getattr(app, "calendars", None)
    if cal is not None and events:
        nxt = []
        try:
            for day in (now.date(), now.date() + timedelta(days=1)):
                for d in cal.events_on(day):
                    m = d.get("meta") or {}
                    if m.get("all_day"):
                        continue
                    st = datetime.fromisoformat(m["start"])
                    if st.tzinfo is not None:
                        st = st.astimezone().replace(tzinfo=None)
                    if st >= now - timedelta(minutes=15):
                        nxt.append((st, _bare_title(d.get("title"))[:80]))
        except Exception:  # noqa: BLE001
            nxt = []
        nxt.sort()
        if nxt:
            def fmt(st):
                day = "today" if st.date() == now.date() else "tomorrow"
                return f"{day} {st.strftime('%I:%M %p').lstrip('0')}"
            out.append("Next on the calendar: " + "; ".join(f"{t or '(no title)'} ({fmt(st)})" for st, t in nxt[:events]) + ".")
    asleep = [c.get("name") or e for e, c in catalog.items() if SLEEP_ENTITY.match(e) and c.get("state") == "on"]
    if asleep:
        out.append(f"Sleep mode is on ({asleep[0]}).")
    store = getattr(getattr(app, "mind", None), "store", None)
    if store is not None and tasks:
        try:
            open_t = store.tasks(limit=tasks)
        except Exception:  # noqa: BLE001
            open_t = []
        if open_t:
            out.append("Open tasks: " + "; ".join((t.get("title") or "")[:60] for t in open_t) + ".")
    return "SITUATION (right now):\n" + "\n".join(f"- {x}" for x in out)


def glance(app, viewer=None, now=None):
    """A watch-sized look at right now (0.41, the Pebble card and sensor.home_mind_now): a few lines of at most about
    30 characters, most pressing first. Same privacy as situation()."""
    now = now or datetime.now()
    lines, out = [], {"at": now.isoformat(timespec="minutes")}
    cal = getattr(app, "calendars", None)
    if cal is not None:
        try:
            nxt = []
            for day in (now.date(), now.date() + timedelta(days=1)):
                for d in cal.events_on(day):
                    m = d.get("meta") or {}
                    if m.get("all_day"):
                        continue
                    st = datetime.fromisoformat(m["start"])
                    if st.tzinfo is not None:
                        st = st.astimezone().replace(tzinfo=None)
                    if st >= now - timedelta(minutes=15):
                        nxt.append((st, _bare_title(d.get("title")) or "(no title)"))
            nxt.sort()
            if nxt:
                st, t = nxt[0]
                when = st.strftime("%I:%M%p").lstrip("0").lower()
                if st.date() != now.date():
                    when = "tmrw " + when
                out["next"] = {"title": t[:60], "start": st.isoformat(timespec="minutes")}
                lines += _wrap(f"{when} {t}")
        except Exception:  # noqa: BLE001
            pass
    store = getattr(getattr(app, "mind", None), "store", None)
    if store is not None:
        try:
            ts = store.tasks(limit=2)
        except Exception:  # noqa: BLE001
            ts = []
        out["tasks"] = [(t.get("title") or "")[:60] for t in ts]
        lines += [("- " + x)[:30] for x in out["tasks"]]
    day = getattr(app, "day", None)
    if day is not None and viewer:
        try:
            dstate = day.get(viewer)
        except Exception:  # noqa: BLE001
            dstate = "unknown"
        if dstate and dstate != "unknown":
            out["day"] = dstate
            lines.append(f"Day: {dstate}")
    ap = getattr(app, "approvals", None)
    n = len(getattr(ap, "pending", {}) or {}) if ap is not None else 0
    if n:
        out["waiting"] = n
        lines.insert(0, f"{n} approval{'s' if n > 1 else ''} waiting")
    out["lines"] = lines[:5] or ["Nothing pressing"]
    out["text"] = " | ".join(out["lines"])
    return out
