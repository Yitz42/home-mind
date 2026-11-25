"""Direct actions from the Home Ask bar, Talk, and Voice (notes, shopping/to-dos, pantry/meals, existing
automations, calendar events, timers, weigh-ins, fitness goals, nudges, and admin system controls).

Every handler reuses Home Mind's existing backend methods and enforces scope/guest checks before touching state.
Questions (ending in '?' or starting with interrogatives like 'what'/'do you') are never intercepted as writes.
"""
import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("homemind.ask_actions")

_POLITE = r"^\s*(?:please\s+|can you\s+|could you\s+|would you\s+)?"

_POLITE_START = re.compile(r"^\s*(?:can you|could you|would you)\b", re.I)

# ---- 1. Remember / save a note -----------------------------------------------------------------------------------
REMEMBER_RX = re.compile(
    r"^\s*(?:please\s+)?"
    r"(?:remember(?:\s+(privately|for me))?\s+(?!to\b)(?:that\s+)?|"
    r"(?:make a |save (?:a |this )?|file (?:a |this )?)?note(?:\s+(privately))?\s*(?:that\s+|:\s*)|"
    r"don'?t forget\s+(?!to\b)(?:that\s+)?)"
    r"(.+)$|"
    r"^\s*(?:can you|could you|would you)\s+"
    r"(?:remember(?:\s+(privately|for me))?\s+that\s+|"
    r"(?:make a |save (?:a |this )?|file (?:a |this )?)?note(?:\s+(privately))?\s*(?:that\s+|:\s*))"
    r"(.+)$",
    re.I,
)

# ---- 2. Shopping list & reminders / to-dos -----------------------------------------------------------------------
SHOP_ADD_RX = re.compile(
    _POLITE + r"(?:add|put)\s+(.+?)\s+(?:to|on|onto)\s+(?:my\s+|the\s+)?(?:shopping|grocery|groceries)\s*list\s*[.!?]?$",
    re.I,
)
TODO_DONE_RX = re.compile(
    _POLITE + r"(?:check off|cross off|mark\s+(?:as\s+)?done|complete|remove)\s+(.+?)"
    r"(?:\s+(?:from|off|on)\s+(?:my\s+|the\s+)?(shopping|grocery|groceries|to-?do|reminders?|tasks?)\s*list)?\s*[.!?]?$",
    re.I,
)
REMINDER_ADD_RX = re.compile(
    _POLITE + r"(?:remind me(?:\s+to)?\s+|remember to\s+|"
    r"add\s+(?:a\s+)?(?:to-?do|task|reminder)(?:\s+to|\s*:)?\s+)(.+?)\s*[.!?]?$",
    re.I,
)
TODO_LIST_ADD_RX = re.compile(
    _POLITE + r"(?:add|put)\s+(.+?)\s+(?:to|on|onto)\s+(?:my\s+|the\s+)?(?:to-?do|reminders?|tasks?)\s*list\s*[.!?]?$",
    re.I,
)

# ---- 3. Food eaten & pantry stock --------------------------------------------------------------------------------
MEAL_SLOT = r"(?:breakfast|lunch|dinner|supper|brunch|snack)"
ATE_RX = re.compile(
    r"^\s*(?:(?:i\s+)?(?:just\s+)?ate\s+(.+?)|"
    r"log\s+(?:a\s+)?(?:meal|food)\s*:?\s*(.+?)|"
    r"(?:i\s+)?(?:just\s+)?had\s+(.+?)\s+for\s+(" + MEAL_SLOT + r")|"
    r"for\s+(" + MEAL_SLOT + r")\s+(?:i\s+)?(?:just\s+)?(?:had|ate)\s+(.+?))\s*[.!]?$",
    re.I,
)
PANTRY_ADD_RX = re.compile(
    r"^\s*(?:(?:i|we)\s+(?:just\s+)?(?:bought|picked up|restocked)\s+(.+?)"
    r"(?:\s+(?:for|in|into)\s+(?:the\s+)?(fridge|freezer|pantry))?|"
    + _POLITE[1:] + r"(?:add|put)\s+(.+?)\s+(?:to|in|into)\s+(?:my\s+|the\s+)?(fridge|freezer|pantry))\s*[.!?]?$",
    re.I,
)
PANTRY_USE_RX = re.compile(
    r"^\s*(?:(?:we(?:'re| are)|i(?:'m| am))\s+out of\s+(.+?)|"
    r"(?:i|we)\s+(?:just\s+)?(finished|used up|ran out of|tossed|threw out|threw away)\s+(?:the\s+)?(.+?)|"
    r"(?:mark|remove)\s+(?:the\s+)?(.+?)\s+(?:as\s+)?(used|finished|tossed|gone)"
    r"(?:\s+from\s+(?:the\s+)?(?:pantry|fridge|freezer))?)\s*[.!]?$",
    re.I,
)
PANTRY_PART_RX = re.compile(
    r"^\s*(?:i|we)\s+(?:only\s+)?(?:just\s+)?(?:used|took)\s+"
    r"(half|a half|quarter|a quarter|a third|third|two thirds|three quarters|part|some|a bit|a little|"
    r"\d+(?:\.\d+)?(?:/\d+)?\s*(?:%|lb|lbs|pounds?|oz|ounces?|kg|g|grams?|cups?|tbsp|tsp|pieces?|slices?)?)\s+"
    r"(?:of\s+)?(?:the\s+|my\s+|our\s+)?(.+?)"
    r"(?:\s+from\s+(?:the\s+)?(?:pantry|fridge|freezer))?\s*[.!]?$",
    re.I,
)

# ---- 4. Controlling existing automations -------------------------------------------------------------------------
AUTO_CTRL_RX = re.compile(
    _POLITE + r"(turn off|disable|pause|turn on|enable|resume|run|trigger)\s+"
    r"(?:(?:the\s+|my\s+)?(.+?)\s+automation|(?:the\s+|my\s+)?automation\s+(.+?))\s*(?:\bnow)?\s*[.!?]?$",
    re.I,
)

# ---- 5. Calendar event creation & timers -------------------------------------------------------------------------
CAL_ADD_RX = re.compile(
    _POLITE + r"(?:add|put|schedule|create)\s+(?:an?\s+(?:event|appointment|meeting)\s+)?(.+?)\s+"
    r"(?:on|to|onto)\s+(?:my\s+|the\s+)?calendar\b(.*?)\s*[.!?]?$",
    re.I,
)
CAL_SCHED_RX = re.compile(
    _POLITE + r"(?:schedule|book)\s+(?:an?\s+)?(.+?)\s+(?:(?:for|on)\s+)?"
    r"((?:today|tomorrow|next\s+\w+|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}|\d{4}-\d{2}-\d{2}).*?)\s*[.!?]?$",
    re.I,
)
TIMER_START_RX = re.compile(
    _POLITE + r"(?:set|start)\s+(?:an?\s+)?(?:(.+?)\s+timer(?:\s+(?:for|named|called)\s+(.+))?|"
    r"timer\s+for\s+(.+))\s*[.!?]?$",
    re.I,
)
TIMER_CTRL_RX = re.compile(
    _POLITE + r"(cancel|stop|pause|resume)\s+(?:the\s+|my\s+)?(?:(.+?)\s+)?timer\s*[.!?]?$",
    re.I,
)

# ---- 6. Weigh-ins, fitness goals & nudges ------------------------------------------------------------------------
WEIGH_RX = re.compile(
    r"^\s*(?:(?:i\s+)?(?:weigh|weighed(?:\s+in\s+at)?)|my\s+weight\s+is|log\s+(?:my\s+)?weight(?:\s+of)?|"
    r"weight\s*:)\s*(\d{2,3}(?:\.\d+)?)\s*(lb|lbs|pounds?|kg|kilograms?)?\s*[.!]?$",
    re.I,
)
FITNESS_GOAL_RX = re.compile(
    _POLITE + r"(?:set|change|update)\s+(?:my\s+)?(protein|calories?|kcal|carbs?|fiber|sugar|fat|steps?|"
    r"workouts?(?:\s+per\s+week)?|target\s+weight|weight\s+target|weight\s+goal|body\s+fat(?:\s+target|\s+goal)?|"
    r"sleep(?:\s+hours?|\s+goal)?|bedtime)\s*(?:goal|target)?\s*(?:to|at)\s*"
    r"([0-9:.]+(?:\s*(?:am|pm|g|grams?|kcal|cal|lb|lbs|pounds?|kg|kilograms?|%|h|hrs?|hours?))?)\s*[.!?]?$",
    re.I,
)
NUDGE_TIME_RX = re.compile(
    _POLITE + r"nudge me(?:\s+to)?\s+(.+?)\s+at\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*[.!?]?$",
    re.I,
)
NUDGE_PLACE_RX = re.compile(
    _POLITE + r"nudge me(?:\s+to)?\s+(.+?)\s+when\s+i\s+(arrive(?:\s+home)?|get\s+home|leave(?:\s+home)?)\s*[.!?]?$",
    re.I,
)

# ---- 7. System / privacy actions (admin) -------------------------------------------------------------------------
MODE_RX = re.compile(
    _POLITE + r"(?:switch|change|set)(?:\s+(?:my\s+|the\s+)?(?:model\s+)?mode)?\s+to\s+"
    r"(private|balanced|cloud)(?:\s+mode)?\s*[.!?]?$",
    re.I,
)
BACKUP_RX = re.compile(
    _POLITE + r"(?:run|start|do)\s+(?:an?\s+)?(?:off-?site\s+)?backup(?:\s+now)?\s*[.!?]?$",
    re.I,
)
EMAIL_RX = re.compile(
    _POLITE + r"check\s+(?:my\s+|the\s+)?(?:e-?mail|inbox)(?:\s+now)?\s*[.!?]?$",
    re.I,
)

_QUESTION_START = re.compile(r"^\s*(?:what|which|where|when|who|why|how|did|do|does|is|are|was|were|have|has)\b", re.I)


def _strip_polite_q(text):
    t = (text or "").replace("’", "'").strip()
    if t.endswith("?"):
        if not _POLITE_START.match(t):
            return ""
        t = t.rstrip("?").strip()
    return t


def claims_command(text):
    """True when `text` starts with a voice.COMMAND verb ('set', 'turn', 'switch', 'run', 'start', 'stop', 'pause',
    'resume') that belongs to an Ask action rather than a house device command in App.do()."""
    t = _strip_polite_q(text)
    if not t:
        return False
    return bool(
        AUTO_CTRL_RX.match(t)
        or TIMER_START_RX.match(t)
        or TIMER_CTRL_RX.match(t)
        or FITNESS_GOAL_RX.match(t)
        or MODE_RX.match(t)
        or BACKUP_RX.match(t)
    )


def _split_items(raw):
    """Split 'milk, eggs and avocados' into ['milk', 'eggs', 'avocados'] cleanly."""
    parts = re.split(r"\s*,\s*|\s+and\s+", (raw or "").strip(), flags=re.I)
    out = []
    for p in parts:
        s = re.sub(r"^(?:some|a|an|the)\s+", "", p.strip().rstrip(".!"), flags=re.I).strip()
        if s and s.lower() not in {x.lower() for x in out}:
            out.append(s)
    return out


def _parse_clock(raw):
    """Parse '3pm', '10:30 pm', '15:00' -> (hour, minute) or None."""
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", raw or "", re.I)
    if not m:
        return None
    h, mn, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if mn > 59 or h > 23 or (ap and (h < 1 or h > 12)):
        return None
    if ap == "pm" and h != 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    return (h, mn)


def _parse_duration(raw):
    """Parse '10 minutes', '1 hour 30 minutes', '45s', 'half an hour' -> total seconds or None."""
    s = (raw or "").strip().lower()
    if not s:
        return None
    if re.search(r"\bhalf\s+an?\s+hour\b", s):
        return 1800
    if re.fullmatch(r"(?:an?\s+)?hour", s):
        return 3600
    if re.fullmatch(r"(?:a\s+)?minute", s):
        return 60
    total = 0.0
    found = False
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", s):
        found = True
        v = float(num)
        if unit.startswith("h"):
            total += v * 3600
        elif unit.startswith("m"):
            total += v * 60
        else:
            total += v
    if not found or total <= 0 or total > 86400:
        return None
    return int(round(total))


def _fmt_hms(secs):
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_dur_words(secs):
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour" + ("s" if h != 1 else ""))
    if m:
        parts.append(f"{m} minute" + ("s" if m != 1 else ""))
    if s and not h:
        parts.append(f"{s} second" + ("s" if s != 1 else ""))
    return " ".join(parts) or f"{secs}s"


_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _extract_when(text, now_dt=None):
    """Extract (clean_text, date_obj_or_None, (h, m)_or_None, duration_secs) from a phrase like
    'dentist Tuesday at 2pm for 30 minutes'."""
    now = now_dt or datetime.now()
    today = now.date()
    s = (text or "").strip()
    dur = 3600
    d_obj = None
    hm = None

    m_dur = re.search(r"\bfor\s+(\d+(?:\.\d+)?\s*(?:hours?|hrs?|minutes?|mins?)|an?\s+hour|half\s+an?\s+hour)\b", s, re.I)
    if m_dur:
        parsed_d = _parse_duration(m_dur.group(1))
        if parsed_d:
            dur = parsed_d
            s = (s[:m_dur.start()] + " " + s[m_dur.end():]).strip()

    m_time = re.search(r"\b(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b|\bat\s+(\d{1,2}:\d{2})\b", s, re.I)
    if m_time:
        raw_t = m_time.group(1) or m_time.group(2)
        hm = _parse_clock(raw_t)
        if hm is not None:
            s = (s[:m_time.start()] + " " + s[m_time.end():]).strip()

    m_iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", s)
    if m_iso:
        try:
            d_obj = date.fromisoformat(m_iso.group(1))
            s = (s[:m_iso.start()] + " " + s[m_iso.end():]).strip()
        except ValueError:
            pass
    if d_obj is None:
        m_rel = re.search(r"\b(?:on\s+)?(today|tomorrow)\b", s, re.I)
        if m_rel:
            d_obj = today if m_rel.group(1).lower() == "today" else today + timedelta(days=1)
            s = (s[:m_rel.start()] + " " + s[m_rel.end():]).strip()
    if d_obj is None:
        m_mon = re.search(r"\b(?:on\s+)?(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
                          r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
                          r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?\b", s, re.I)
        if m_mon:
            mon = _MONTHS[m_mon.group(1)[:3].lower()]
            day = int(m_mon.group(2))
            yr = int(m_mon.group(3)) if m_mon.group(3) else today.year
            try:
                cand = date(yr, mon, day)
                if not m_mon.group(3) and cand < today:
                    cand = date(yr + 1, mon, day)
                d_obj = cand
                s = (s[:m_mon.start()] + " " + s[m_mon.end():]).strip()
            except ValueError:
                pass
    if d_obj is None:
        m_wd = re.search(r"\b(?:on\s+)?(?:(next)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
                         s, re.I)
        if m_wd:
            target = _WEEKDAYS[m_wd.group(2).lower()]
            delta = (target - today.weekday()) % 7
            if delta == 0 or (m_wd.group(1) and delta < 7):
                delta = delta + 7 if delta == 0 else delta
            d_obj = today + timedelta(days=delta)
            s = (s[:m_wd.start()] + " " + s[m_wd.end():]).strip()

    s = re.sub(r"\s+", " ", s).strip(" ,.-")
    s = re.sub(r"\b(?:on|at|for)\s*$", "", s, flags=re.I).strip(" ,.-")
    return s, d_obj, hm, dur


# ---- 1b. Ideas and journal entries, by voice or typed (0.39) -------------------------------------------------------
# "idea for the book: ...", "project idea, ...", "I have an idea: ...", "journal: ...", "dear journal ...",
# "note to self: ..." land on the shelf script.home_mind_remember uses, so they're saved even when the conversation
# agent is Home Mind itself (which never calls that script).
CAPTURE_RX = re.compile(
    r"^\s*(?:(?:save|log|add|file|capture|record|remember|take)\s+(?:down\s+)?(?:an?\s+|this\s+|my\s+|the\s+)?)?"
    r"(?:(?:i\s+(?:have|had|got)\s+an?|here'?s\s+an?|new|another|quick)\s+)?"
    r"(?:(?P<kind>book|framework|religion|project|app|pebble|random|business)\s+)?"
    r"(?P<what>idea|journal(?:\s+entry)?|note\s+to\s+self)"
    r"(?:\s+(?:for|about|on)\s+(?:the\s+|my\s+|a\s+|an\s+)?(?P<topic>[^:,;.!?\u2014-]{1,40}?))?"
    r"\s*(?::|,|;|\u2014|\s-\s|\.\s|\s+that\s+|\s+is\s+)\s*(?P<text>\S.{1,})$", re.I)
DEAR_JOURNAL_RX = re.compile(r"^\s*(?:dear\s+journal|add\s+to\s+my\s+journal|journal\s+this)\b[\s,:.-]*(?P<text>\S.{1,})$",
                             re.I)
_BOOK = re.compile(r"\b(?:book|framework|religion|secular|ideology|chapter|philosoph\w*)\b", re.I)
_PROJECT = re.compile(r"\b(?:project|app|apps|pebble|watch\w*|home mind|jeeves|robot\w*|rc|game|code|build|website|"
                      r"plugin|feature|firmware|lora|print|3d|cad|design|invention|business|startup|product)\b", re.I)
IDEA_SHELVES = {"book": ("ideas/book-and-framework", "Idea (book and framework)", "book ideas"),
                "projects": ("ideas/projects", "Idea (projects)", "project ideas"),
                "random": ("ideas/random", "Idea", "ideas")}


def capture_parse(text):
    """(shelf, saved text, spoken place) for an idea / journal / note-to-self, or None."""
    t = (text or "").strip()
    m = DEAR_JOURNAL_RX.match(t)
    if m:
        body = m.group("text").strip()
        return ("journal", "Journal entry: " + body, "your journal") if len(body) >= 2 else None
    m = CAPTURE_RX.match(t)
    if not m:
        return None
    body = m.group("text").strip()
    if len(body) < 2 or body.endswith("?"):
        return None                     # "idea, what should I cook?" is a question
    what = m.group("what").lower()
    if what.startswith("journal"):
        return "journal", "Journal entry: " + body, "your journal"
    if what.startswith("note"):
        return "notes", body, "your notes"
    kind, topic = (m.group("kind") or "").lower(), (m.group("topic") or "").strip()
    probe = " ".join([kind, topic, body])
    which = "book" if _BOOK.search(kind + " " + topic) else "projects" if (_PROJECT.search(kind) or topic) else \
        "book" if _BOOK.search(probe) else "projects" if _PROJECT.search(probe) else "random"
    shelf, prefix, place = IDEA_SHELVES[which]
    label = f"{prefix} for {topic}" if topic and which != "book" else prefix
    return shelf, f"{label}: {body}", f"your {place}"


async def _handle_capture(app, text, who, voice):
    parsed = capture_parse(text)
    if not parsed:
        return None
    st = getattr(getattr(app, "mind", None), "store", None)
    if st is None:
        return None
    shelf, body, place = parsed
    meta = {"said_by": who} if who else {}
    if shelf == "journal" and who:
        meta.update(scope="personal", owner=who)     # a journal is its writer's own (personal_rule first_person)
    out = st.remember(text=body, kind="note", shelf=shelf, tags=["voice"] if voice else None,
                      source="voice" if voice else "ask", meta=meta or None)
    return {"answer": f"Saved to {place}.", "hits": [], "model": None, "remembered": out, "via": "action",
            "captured": shelf}


# ---- handlers ----------------------------------------------------------------------------------------------------
async def _handle_remember(app, text, who, voice):
    m = REMEMBER_RX.match(text)
    if not m:
        return None
    private = bool(m.group(1) or m.group(2) or m.group(4) or m.group(5))
    note = (m.group(3) or m.group(6) or "").strip()
    if len(note) < 2:
        return None
    st = getattr(getattr(app, "mind", None), "store", None)
    if st is None:
        return None
    meta = {"said_by": who} if who else {}
    if private and who:
        meta.update(scope="personal", owner=who)
    out = st.remember(text=note, kind="note", shelf="inbox",
                      source="voice" if voice else "ask", meta=meta or None)
    label = "private note" if private else "note"
    return {"answer": f"Saved {label}: {note}", "hits": [], "model": None,
            "remembered": out, "via": "action"}


async def _pick_reminder_list(ha):
    """First non-shopping HA todo.* entity, or None."""
    from .hub import SHOPPING
    if not (ha and getattr(ha, "available", False)):
        return None
    try:
        ents = [s["entity_id"] for s in (await ha.states() or [])
                if str(s.get("entity_id", "")).startswith("todo.") and s["entity_id"] != SHOPPING]
        return ents[0] if ents else None
    except Exception:  # noqa: BLE001
        return None


async def _handle_todo(app, text, who, voice, now_dt=None):
    ha = getattr(app, "ha", None)
    ha_ok = bool(ha and getattr(ha, "available", False))
    from .hub import SHOPPING, _todo

    # 1. Add to shopping list
    ms = SHOP_ADD_RX.match(text)
    if ms:
        items = _split_items(ms.group(1))
        if not items:
            return None
        pantry = getattr(app, "pantry", None)
        if pantry is not None and ha_ok:
            added = await pantry.add_to_shopping(items)
            if added:
                return {"answer": f"Added {', '.join(added)} to your shopping list.",
                        "hits": [], "model": None, "shopping_added": added, "via": "action"}
        if ha_ok:
            for it in items:
                await ha.call("todo", "add_item", {"entity_id": SHOPPING, "item": it})
            return {"answer": f"Added {', '.join(items)} to your shopping list.",
                    "hits": [], "model": None, "shopping_added": items, "via": "action"}
        return {"answer": "Home Assistant isn't reachable right now, so I couldn't update your shopping list.",
                "hits": [], "model": None}

    # 2. Check off / complete an item on shopping or to-do list
    md = TODO_DONE_RX.match(text)
    if md:
        raw_item = (md.group(1) or "").strip()
        which_list = (md.group(2) or "").lower()
        # Only intercept bare "remove X" if a list was named, or if verb was explicitly "check off"/"cross off"
        if not which_list and not re.match(_POLITE + r"(?:check off|cross off)\b", text, re.I):
            md = None
    if md:
        if not ha_ok:
            return {"answer": "Home Assistant isn't reachable right now, so I couldn't check off that item.",
                    "hits": [], "model": None}
        target = raw_item.lower().strip(" .!")
        if which_list in ("shopping", "grocery", "groceries"):
            lists = [SHOPPING]
        else:
            try:
                all_todo = [s["entity_id"] for s in (await ha.states() or [])
                            if str(s.get("entity_id", "")).startswith("todo.")]
            except Exception:  # noqa: BLE001
                all_todo = [SHOPPING]
            lists = ([e for e in all_todo if e != SHOPPING] + [SHOPPING]) if which_list else \
                ([SHOPPING] + [e for e in all_todo if e != SHOPPING])
        for ent in lists[:5]:
            for it in await _todo(ha, ent):
                summary = (it.get("summary") or "").strip()
                uid = it.get("uid") or summary
                if summary and (target == summary.lower() or target in summary.lower() or summary.lower() in target):
                    await ha.call("todo", "update_item", {"entity_id": ent, "item": uid, "status": "completed"})
                    return {"answer": f"Checked off {summary}.", "hits": [], "model": None,
                            "todo_done": {"entity": ent, "item": summary}, "via": "action"}
        return {"answer": f"I couldn't find '{raw_item}' on your list.", "hits": [], "model": None}

    # 3. Add a reminder / to-do
    mr = REMINDER_ADD_RX.match(text) or TODO_LIST_ADD_RX.match(text)
    if mr:
        raw = (mr.group(1) or "").strip()
        if not raw:
            return None
        clean, d_obj, hm, _ = _extract_when(raw, now_dt)
        item = (clean or raw)[:200]
        ent = await _pick_reminder_list(ha)
        if ent and ha_ok:
            payload = {"entity_id": ent, "item": item}
            if d_obj and hm:
                payload["due_datetime"] = f"{d_obj.isoformat()} {hm[0]:02d}:{hm[1]:02d}:00"
            elif d_obj:
                payload["due_date"] = d_obj.isoformat()
            elif hm:
                today_d = (now_dt or datetime.now()).date()
                payload["due_datetime"] = f"{today_d.isoformat()} {hm[0]:02d}:{hm[1]:02d}:00"
            await ha.call("todo", "add_item", payload)
            when_str = f" for {d_obj.strftime('%a %b %d')}" if d_obj else ""
            if hm:
                when_str += f" at {datetime(2000, 1, 1, hm[0], hm[1]).strftime('%I:%M %p').lstrip('0')}"
            return {"answer": f"Added reminder: {item}{when_str}.", "hits": [], "model": None,
                    "todo_added": {"entity": ent, "item": item}, "via": "action"}
        st = getattr(getattr(app, "mind", None), "store", None)
        if st is not None:
            meta = {"said_by": who} if who else {}
            if d_obj:
                meta["due"] = d_obj.isoformat()
            out = st.remember(text=raw, title=item[:80], kind="task", shelf="tasks",
                              source="voice" if voice else "ask", meta=meta or None)
            return {"answer": f"Saved task: {raw}.", "hits": [], "model": None,
                    "remembered": out, "via": "action"}
    return None


async def _handle_pantry(app, text, who, voice):
    pantry = getattr(app, "pantry", None)
    if pantry is None:
        return None
    src = "voice" if voice else "ask"

    # 1. Meal eaten
    ma = ATE_RX.match(text)
    if ma:
        food = (ma.group(1) or ma.group(2) or ma.group(3) or ma.group(6) or "").strip()
        slot = (ma.group(4) or ma.group(5) or "").strip().lower()
        if food and len(food) >= 2:
            full = f"{food} ({slot})" if slot else food
            out = pantry.ate(full, source=src)
            if out.get("id") and hasattr(app, "_spawn"):
                mid = out["id"]
                app._spawn(f"estimate:{mid}", lambda: pantry.estimate(mid, full))
            return {"answer": f"Logged meal: {full}.", "hits": [], "model": None,
                    "meal_logged": out, "via": "action"}

    # 2. Pantry stocked / bought
    mp = PANTRY_ADD_RX.match(text)
    if mp:
        raw = (mp.group(1) or mp.group(3) or "").strip()
        loc = (mp.group(2) or mp.group(4) or "").strip().lower() or None
        parsed = pantry.parse_list(raw)
        if parsed:
            added = [pantry.add(n, a, loc, source=src) for n, a in parsed]
            names = ", ".join(x.get("name") or n for x, (n, _) in zip(added, parsed))
            where = f" ({loc})" if loc else ""
            return {"answer": f"Added to pantry{where}: {names}.", "hits": [], "model": None,
                    "pantry_added": added, "via": "action"}

    # 3a. Pantry partial consumption ("I used half the milk", "I took 2 eggs", "I only used part of the chicken")
    mpt = PANTRY_PART_RX.match(text)
    if mpt:
        used_s = (mpt.group(1) or "").strip()
        item_name = (mpt.group(2) or "").strip()
        if item_name and hasattr(pantry, "use_part"):
            res = pantry.use_part(name=item_name, used=used_s)
            if res.get("found"):
                if res.get("partial"):
                    ans = f"Updated {res['name']}: {res['amount']}."
                else:
                    ans = f"Marked {res['name']} as used up."
                return {"answer": ans, "hits": [], "model": None,
                        "pantry_part": res, "via": "action"}
            return {"answer": f"I couldn't find {item_name} in your pantry.", "hits": [], "model": None,
                    "pantry_part": res, "via": "action"}

    # 3. Pantry used / finished / tossed
    mu = PANTRY_USE_RX.match(text)
    if mu:
        verb = (mu.group(2) or mu.group(5) or "used").lower()
        raw = (mu.group(1) or mu.group(3) or mu.group(4) or "").strip()
        status = "tossed" if any(w in verb for w in ("toss", "threw")) else "used"
        names = [n for n, _ in pantry.parse_list(raw)] or _split_items(raw)
        if names:
            done = [pantry.use(n, status) for n in names]
            label = "tossed" if status == "tossed" else "used up"
            return {"answer": f"Marked as {label}: {', '.join(names)}.", "hits": [], "model": None,
                    "pantry_used": done, "via": "action"}
    return None


async def _handle_automation_ctrl(app, text):
    m = AUTO_CTRL_RX.match(text)
    if not m:
        return None
    verb = m.group(1).lower()
    target = (m.group(2) or m.group(3) or "").strip()
    if not target:
        return None
    ha = getattr(app, "ha", None)
    eng = getattr(app, "engine", None)
    candidates = {}
    if eng is not None:
        for eid, info in (getattr(eng, "autos", None) or {}).items():
            candidates[eid] = info.get("name") or info.get("alias") or eid
    if ha and getattr(ha, "available", False):
        try:
            for s in await ha.states() or []:
                eid = s.get("entity_id") or ""
                if eid.startswith("automation."):
                    candidates.setdefault(eid, (s.get("attributes") or {}).get("friendly_name") or eid)
        except Exception:  # noqa: BLE001
            pass
    if not candidates:
        return {"answer": "I couldn't load your Home Assistant automations right now.", "hits": [], "model": None}

    q_low = target.lower()
    q_toks = set(re.findall(r"[a-z0-9]+", q_low))
    exact = [e for e, n in candidates.items() if n.lower() == q_low or e.lower() == q_low or e.lower() == f"automation.{q_low}"]
    hits = exact or [e for e, n in candidates.items() if q_low in n.lower() or q_low in e.lower()]
    if not hits and q_toks:
        hits = [e for e, n in candidates.items() if q_toks <= set(re.findall(r"[a-z0-9]+", f"{n} {e}".lower()))]
    if not hits:
        return {"answer": f"I couldn't find an automation matching '{target}'.", "hits": [], "model": None}
    if len(hits) > 1:
        names = [candidates[e] for e in hits[:5]]
        return {"answer": f"Which automation: {', '.join(names)}?", "which": names, "hits": [], "model": None}

    eid = hits[0]
    name = candidates[eid]
    owned = eng is not None and eid in (getattr(eng, "owned", None) or ())
    if verb in ("run", "trigger"):
        if owned:
            await eng.run_now(eid)
        elif ha and getattr(ha, "available", False):
            await ha.call("automation", "trigger", {"entity_id": eid})
        else:
            return {"answer": "Home Assistant isn't reachable right now.", "hits": [], "model": None}
        return {"answer": f"Triggered automation {name}.", "hits": [], "model": None,
                "automation_action": {"entity_id": eid, "op": "trigger"}, "via": "action"}
    if verb in ("turn off", "disable", "pause"):
        if owned:
            await eng.retire([eid])
        elif ha and getattr(ha, "available", False):
            await ha.call("automation", "turn_off", {"entity_id": eid})
        else:
            return {"answer": "Home Assistant isn't reachable right now.", "hits": [], "model": None}
        return {"answer": f"Turned off automation {name}.", "hits": [], "model": None,
                "automation_action": {"entity_id": eid, "op": "turn_off"}, "via": "action"}
    # turn on / enable / resume
    if ha and getattr(ha, "available", False):
        await ha.call("automation", "turn_on", {"entity_id": eid})
        return {"answer": f"Turned on automation {name}.", "hits": [], "model": None,
                "automation_action": {"entity_id": eid, "op": "turn_on"}, "via": "action"}
    return {"answer": "Home Assistant isn't reachable right now.", "hits": [], "model": None}


async def _pick_calendar(app):
    ha = getattr(app, "ha", None)
    if not (ha and getattr(ha, "available", False)):
        return None, None
    try:
        states = await ha.states() or []
    except Exception:  # noqa: BLE001
        return None, None
    cals = [(s["entity_id"], (s.get("attributes") or {}).get("friendly_name") or s["entity_id"])
            for s in states if str(s.get("entity_id", "")).startswith("calendar.")]
    if not cals:
        return None, None
    # Prefer a writable personal/household calendar over journal/holiday/birthday calendars
    skip = re.compile(r"journal|holiday|birthday|week_number|trash|garbage", re.I)
    for eid, name in cals:
        if not skip.search(f"{eid} {name}"):
            return eid, name
    return cals[0]


async def _handle_calendar(app, text, now_dt=None):
    m = CAL_ADD_RX.match(text)
    if m:
        combined = f"{m.group(1)} {m.group(2)}".strip()
    else:
        m = CAL_SCHED_RX.match(text)
        if not m:
            return None
        combined = f"{m.group(1)} {m.group(2)}".strip()
    title, d_obj, hm, dur = _extract_when(combined, now_dt)
    if not title or d_obj is None:
        if hm is not None and title:
            d_obj = (now_dt or datetime.now()).date()
        else:
            return None
    eid, cname = await _pick_calendar(app)
    if not eid:
        return {"answer": "I couldn't find a Home Assistant calendar to add that event to.",
                "hits": [], "model": None}
    payload = {"entity_id": eid, "summary": title[:200]}
    if hm is not None:
        start = datetime(d_obj.year, d_obj.month, d_obj.day, hm[0], hm[1])
        end = start + timedelta(seconds=dur)
        payload["start_date_time"] = start.strftime("%Y-%m-%d %H:%M:%S")
        payload["end_date_time"] = end.strftime("%Y-%m-%d %H:%M:%S")
        when_str = f"{start.strftime('%a %b %d')} at {start.strftime('%I:%M %p').lstrip('0')}"
    else:
        payload["start_date"] = d_obj.isoformat()
        payload["end_date"] = (d_obj + timedelta(days=1)).isoformat()
        when_str = f"{d_obj.strftime('%a %b %d')} (all day)"
    await app.ha.call("calendar", "create_event", payload)
    cal_mod = getattr(app, "calendars", None)
    if cal_mod is not None and hasattr(app, "_spawn"):
        app._spawn("cal_sync", cal_mod.sync)
    return {"answer": f"Added '{title}' to {cname} on {when_str}.", "hits": [], "model": None,
            "calendar_event": payload, "via": "action"}


async def _handle_timer(app, text, who):
    ha = getattr(app, "ha", None)
    ha_ok = bool(ha and getattr(ha, "available", False))
    timers = app.__dict__.setdefault("_ask_timers", {})

    mc = TIMER_CTRL_RX.match(text)
    if mc:
        op = mc.group(1).lower()
        label = (mc.group(2) or "").strip().lower()
        ha_timers = []
        if ha_ok:
            try:
                ha_timers = [s for s in (await ha.states() or [])
                             if str(s.get("entity_id", "")).startswith("timer.")]
            except Exception:  # noqa: BLE001
                pass
        if ha_timers:
            pick = next((s for s in ha_timers if label and label in
                         ((s.get("attributes") or {}).get("friendly_name") or s["entity_id"]).lower()), None) \
                or next((s for s in ha_timers if s.get("state") in ("active", "paused")), ha_timers[0])
            svc = "cancel" if op in ("cancel", "stop") else ("pause" if op == "pause" else "start")
            await ha.call("timer", svc, {"entity_id": pick["entity_id"]})
            tname = (pick.get("attributes") or {}).get("friendly_name") or pick["entity_id"]
            past = {"cancel": "Cancelled", "pause": "Paused", "start": "Resumed"}[svc]
            return {"answer": f"{past} {tname}.", "hits": [], "model": None,
                    "timer": {"entity_id": pick["entity_id"], "op": svc}, "via": "action"}
        if op in ("cancel", "stop") and timers:
            key = next((k for k in timers if label and label in k.lower()), next(iter(timers)))
            task = timers.pop(key, None)
            if task and not task.done():
                task.cancel()
            return {"answer": f"Cancelled {key} timer.", "hits": [], "model": None,
                    "timer": {"name": key, "op": "cancel"}, "via": "action"}
        return {"answer": "There isn't an active timer to change right now.", "hits": [], "model": None}

    ms = TIMER_START_RX.match(text)
    if not ms:
        return None
    if ms.group(3):
        dur_raw = ms.group(3)
        name = "Timer"
    else:
        first, second = (ms.group(1) or "").strip(), (ms.group(2) or "").strip()
        if _parse_duration(first):
            dur_raw = first
            rem = re.sub(r"\b\d+(?:\.\d+)?\s*(?:hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b|\bhalf\s+an?\s+hour\b|\band\b",
                         "", first, flags=re.I).strip(" ,.-")
            name = second or rem or "Timer"
        elif _parse_duration(second):
            dur_raw = second
            name = first or "Timer"
        else:
            dur_raw = first
            name = "Timer"
    secs = _parse_duration(dur_raw)
    if not secs:
        return None
    hms = _fmt_hms(secs)
    words = _fmt_dur_words(secs)

    # Prefer a real Home Assistant timer.* entity if one exists
    if ha_ok:
        try:
            ha_timers = [s for s in (await ha.states() or [])
                         if str(s.get("entity_id", "")).startswith("timer.")]
        except Exception:  # noqa: BLE001
            ha_timers = []
        if ha_timers:
            pick = next((s for s in ha_timers if name != "Timer" and name.lower() in
                         ((s.get("attributes") or {}).get("friendly_name") or s["entity_id"]).lower()), None) \
                or next((s for s in ha_timers if s.get("state") == "idle"), ha_timers[0])
            await ha.call("timer", "start", {"entity_id": pick["entity_id"], "duration": hms})
            tname = (pick.get("attributes") or {}).get("friendly_name") or pick["entity_id"]
            return {"answer": f"Started {tname} for {words}.", "hits": [], "model": None,
                    "timer": {"entity_id": pick["entity_id"], "duration": hms, "seconds": secs}, "via": "action"}

    # Fallback: lightweight in-process timer that sends an HA notification when done
    label = name.strip() or "Timer"
    old = timers.pop(label, None)
    if old and not old.done():
        old.cancel()

    async def _ring():
        try:
            await asyncio.sleep(secs)
            msg = f"{label} ({words}) is done."
            if ha and getattr(ha, "available", False):
                await ha.notify("Timer done", msg, nid="home_mind_timer")
                phone = app.phone_of(who) if who and hasattr(app, "phone_of") else None
                if phone:
                    await ha.push(phone, {"title": "Timer done", "message": msg}, tier="important", now=True)
        except asyncio.CancelledError:
            pass
        finally:
            timers.pop(label, None)

    timers[label] = asyncio.ensure_future(_ring())
    title_lbl = f"{label} timer" if label.lower() != "timer" else "a timer"
    return {"answer": f"Started {title_lbl} for {words}.", "hits": [], "model": None,
            "timer": {"name": label, "duration": hms, "seconds": secs, "local": True}, "via": "action"}


async def _handle_weigh_in(app, text, who, voice):
    m = WEIGH_RX.match(text)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "lb").lower()
    from .body import KG_LB, note_text
    lb = val * KG_LB if unit.startswith("k") else val
    if not (50 <= lb <= 600):
        return None
    st = getattr(getattr(app, "mind", None), "store", None)
    if st is None:
        return None
    when = datetime.now().astimezone()
    ts = when.astimezone(timezone.utc).isoformat()
    vals = {"weight": round(lb, 1)}
    note = note_text(when, vals)
    meta = {"weigh_in": ts, "series": "weigh-in", "weight": round(lb, 1)}
    if who:
        meta.update(said_by=who, scope="personal", owner=who)
    out = st.remember(text=note, title=note.split(": my ")[0] + f": {lb:.1f} lb",
                      kind="fact", shelf="body/weight", tags=["weight", "scale", "body"],
                      source="voice" if voice else "ask", meta=meta, at=when.timestamp())
    return {"answer": f"Logged weigh-in: {lb:.1f} lb.", "hits": [], "model": None,
            "weigh_in": out, "via": "action"}


_GOAL_MAP = {
    "protein": ("protein_g", "g"),
    "calorie": ("calories", "kcal"),
    "calories": ("calories", "kcal"),
    "kcal": ("calories", "kcal"),
    "carb": ("carbs_g", "g"),
    "carbs": ("carbs_g", "g"),
    "fiber": ("fiber_g", "g"),
    "sugar": ("sugar_g", "g"),
    "fat": ("fat_g", "g"),
    "step": ("steps", "steps"),
    "steps": ("steps", "steps"),
    "workout": ("workouts_per_week", "workouts/week"),
    "workouts": ("workouts_per_week", "workouts/week"),
    "workouts per week": ("workouts_per_week", "workouts/week"),
    "target weight": ("weight_target", "lb"),
    "weight target": ("weight_target", "lb"),
    "weight goal": ("weight_target", "lb"),
    "body fat": ("body_fat_target", "%"),
    "body fat target": ("body_fat_target", "%"),
    "body fat goal": ("body_fat_target", "%"),
    "sleep": ("sleep_hours", "hours"),
    "sleep hours": ("sleep_hours", "hours"),
    "sleep goal": ("sleep_hours", "hours"),
    "bedtime": ("bedtime", ""),
}


async def _handle_fitness_goal(app, text):
    m = FITNESS_GOAL_RX.match(text)
    if not m:
        return None
    fit = getattr(app, "fitness", None)
    if fit is None:
        return None
    metric = re.sub(r"\s+", " ", m.group(1).lower().strip())
    raw_val = m.group(2).strip()
    key, unit = _GOAL_MAP.get(metric, (None, None))
    if not key:
        return None
    if key == "bedtime":
        hm = _parse_clock(raw_val)
        if hm is None:
            return None
        val = f"{hm[0]:02d}:{hm[1]:02d}"
    else:
        nm = re.match(r"^(\d+(?:\.\d+)?)\s*([a-z%]*)$", raw_val, re.I)
        if not nm:
            return None
        num = float(nm.group(1))
        if key == "weight_target" and nm.group(2).lower().startswith("k"):
            from .body import KG_LB
            num = round(num * KG_LB, 1)
        val = num
    try:
        goals = fit.set_goals({key: val})
    except ValueError as e:
        return {"answer": f"Couldn't update that goal: {e}", "hits": [], "model": None}
    shown = goals.get(key, val)
    suffix = f" {unit}" if unit else ""
    return {"answer": f"Updated {metric} goal to {shown}{suffix}.", "hits": [], "model": None,
            "goals": goals, "via": "action"}


async def _handle_nudge(app, text, who):
    tk = getattr(app, "tracking", None)
    if tk is None:
        return None
    mt = NUDGE_TIME_RX.match(text)
    if mt:
        msg = mt.group(1).strip()
        hm = _parse_clock(mt.group(2))
        if msg and hm is not None:
            at = f"{hm[0]:02d}:{hm[1]:02d}"
            nid = tk.add_nudge(goal=msg, trigger={"type": "time", "at": at}, message=msg, owner=who or None)
            pretty = datetime(2000, 1, 1, hm[0], hm[1]).strftime("%I:%M %p").lstrip("0")
            return {"answer": f"Added nudge: '{msg}' at {pretty}.", "hits": [], "model": None,
                    "nudge_id": nid, "via": "action"}
    mp = NUDGE_PLACE_RX.match(text)
    if mp:
        msg = mp.group(1).strip()
        ev = mp.group(2).lower()
        typ = "leave" if "leave" in ev else "arrive"
        if msg:
            nid = tk.add_nudge(goal=msg, trigger={"type": typ, "place": "home"}, message=msg, owner=who or None)
            when_str = "when you leave home" if typ == "leave" else "when you get home"
            return {"answer": f"Added nudge: '{msg}' {when_str}.", "hits": [], "model": None,
                    "nudge_id": nid, "via": "action"}
    return None


async def _handle_system(app, text, who):
    mm = MODE_RX.match(text)
    if mm:
        modes = getattr(app, "modes", None)
        if modes is None:
            return None
        mode = mm.group(1).lower()
        set_to = modes.set_mode(mode, who or None)
        return {"answer": f"Switched model mode to {set_to}.", "hits": [], "model": None,
                "mode": set_to, "via": "action"}
    if BACKUP_RX.match(text):
        off = getattr(app, "offsite", None)
        if off is None:
            return None
        res = await off.run()
        if isinstance(res, dict) and res.get("error"):
            return {"answer": f"Backup couldn't run: {res['error']}", "hits": [], "model": None}
        fname = (res or {}).get("file") or "backup"
        return {"answer": f"Backup complete ({fname}).", "hits": [], "model": None,
                "backup": res, "via": "action"}
    if EMAIL_RX.match(text):
        mb = getattr(app, "mailbox", None)
        if mb is None:
            return None
        if not getattr(mb, "on", False):
            return {"answer": "Email isn't configured yet — set email_host, email_user, and email_password in options.",
                    "hits": [], "model": None}
        res = await asyncio.to_thread(mb.run)
        filed = (res or {}).get("filed", 0)
        read = (res or {}).get("read", 0)
        return {"answer": f"Checked email: {read} read, {filed} filed.", "hits": [], "model": None,
                "email": res, "via": "action"}
    return None


async def dispatch(app, b, who, other=None, scope="admin", now_dt=None):
    """Try to handle `b['question']` as a direct Ask action. Returns a reply dict or None if it's a normal question."""
    from .tokens import allows
    if other or who == "" or b.get("use_brain") is False:
        return None
    text = _strip_polite_q(b.get("question") or "")
    if not text or len(text) > 400 or _QUESTION_START.match(text):
        return None
    voice = bool(b.get("voice"))

    # Admin-only actions (existing automations & system controls)
    if allows(scope, "admin"):
        for fn in (
            lambda: _handle_system(app, text, who),
            lambda: _handle_automation_ctrl(app, text),
        ):
            r = await fn()
            if r is not None:
                return r

    # Write-scope actions (notes, shopping/to-dos, pantry/food, calendar, timers, weigh-ins, goals, nudges)
    if not allows(scope, "write"):
        return None
    for fn in (
        lambda: _handle_todo(app, text, who, voice, now_dt=now_dt),
        lambda: _handle_capture(app, text, who, voice),
        lambda: _handle_remember(app, text, who, voice),
        lambda: _handle_pantry(app, text, who, voice),
        lambda: _handle_calendar(app, text, now_dt=now_dt),
        lambda: _handle_timer(app, text, who),
        lambda: _handle_weigh_in(app, text, who, voice),
        lambda: _handle_fitness_goal(app, text),
        lambda: _handle_nudge(app, text, who),
    ):
        r = await fn()
        if r is not None:
            return r
    return None
