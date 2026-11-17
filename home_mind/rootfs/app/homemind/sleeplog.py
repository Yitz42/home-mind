"""Sleep said in words: "slept from 12 to 7am last night", "went to bed at 11:30 and woke up at 6:45", "slept 6.5
hours", "got 7 hours of sleep". Filed on body/sleep, where it beats the watch (which reads 0 when it was off)."""
import re
from datetime import datetime, timedelta

T = r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|am|pm)?|(midnight|noon)"
RANGE = [
    re.compile(r"\bslept\s+(?:from\s+)?(?:about\s+|around\s+)?(?P<a>" + T.replace("(", "(?:").replace("(?:?:", "(?:")
               + r")\s*(?:to|till|until|-|–)\s*(?P<b>" + T.replace("(", "(?:").replace("(?:?:", "(?:") + r")", re.I),
    re.compile(r"\b(?:went to (?:bed|sleep)|fell asleep|asleep|in bed)\s+(?:at\s+|around\s+|about\s+)?(?P<a>"
               + T.replace("(", "(?:").replace("(?:?:", "(?:") + r").{0,40}?\b(?:woke(?: up)?|got up|up)\s+(?:at\s+|around\s+|about\s+)?(?P<b>"
               + T.replace("(", "(?:").replace("(?:?:", "(?:") + r")", re.I),
]
HOURS = re.compile(r"\b(?:slept|got|had|sleep(?:ing)?\s+was)\s+(?:only\s+|about\s+|around\s+|maybe\s+)?(?P<h>\d{1,2}(?:\.\d+)?|"
                   r"(?:a\s+)?half)\s*(?P<half>and a half\s*)?(?:h\b|hrs?\b|hours?\b)(?:\s+of\s+sleep)?", re.I)
QUESTION = re.compile(r"^\s*(?:how|what|when|did|do|does|was|is|should|can|why|which|show|tell|remind)\b", re.I)
OTHER_NIGHT = re.compile(r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b|\bago\b|\bthe night before\b|\bon the \d", re.I)


def _clock(s):
    """'12' / '7am' / '11:30 pm' / 'midnight' -> (hour, minute, explicit am/pm or None)."""
    s = s.strip().lower()
    if s == "midnight":
        return 0, 0, "am"
    if s == "noon":
        return 12, 0, "pm"
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*([ap])?", s)
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if h > 23 or mi > 59:
        return None
    ap = m.group(3)
    if ap == "a":
        h = 0 if h == 12 else h
    elif ap == "p":
        h = h if h == 12 else h + 12
    return h, mi, ap


def parse_sleep(text, now=None):
    """The night's sleep from words, or None. Returns {hours, start?, end?, night (the date woken up on)}."""
    t = (text or "").replace("’", "'").strip()
    if not t or t.endswith("?") or QUESTION.match(t) or OTHER_NIGHT.search(t):
        return None
    now = datetime.fromtimestamp(now) if now else datetime.now()
    wake_day = now.date()                 # "last night" is the night that ended this morning
    for rx in RANGE:
        m = rx.search(t)
        if not m:
            continue
        a, b = _clock(m.group("a")), _clock(m.group("b"))
        if not a or not b:
            return None
        ah, am_, aap = a
        bh, bm, bap = b
        if aap is None and ah <= 12:          # bedtime with no am/pm: 7-11 is evening, 12 is midnight, 1-6 is night
            ah = 0 if ah == 12 else ah + 12 if ah >= 7 else ah
        if bap is None and bh == 12:
            bh = 12                            # "to 12" = noon
        end = datetime.combine(wake_day, datetime.min.time()) + timedelta(hours=bh, minutes=bm)
        start = datetime.combine(wake_day, datetime.min.time()) + timedelta(hours=ah, minutes=am_)
        if start >= end:
            start -= timedelta(days=1)
        hours = round((end - start).total_seconds() / 3600, 2)
        if not 0.5 <= hours <= 16:
            return None
        return {"hours": hours, "start": start, "end": end, "night": wake_day}
    m = HOURS.search(t)
    if m and re.search(r"\b(?:slept|sleep|last night)\b", t, re.I):
        h = 0.5 if "half" in m.group("h").lower() else float(m.group("h"))
        if m.group("half"):
            h += 0.5
        if 0.5 <= h <= 16:
            return {"hours": h, "night": wake_day}
    return None


def _hm(d):
    return f"{d:%I:%M %p}".lstrip("0").replace(":00 ", " ")


def log_sleep(store, text, now=None, meta=None):
    """File it on body/sleep. Returns {answer, id, hours}, or None if it isn't sleep."""
    ts = now or store.clock()
    s = parse_sleep(text, ts)
    if not s:
        return None
    night = s["night"]
    eve = night - timedelta(days=1)
    span = f"slept from {_hm(s['start'])} to {_hm(s['end'])} " if s.get("start") else "slept "
    body = f"Sleep {eve:%a} night to {night:%a %b %d %Y}: {span}({s['hours']:g} hours). Said: {text.strip()}"
    m = dict(meta or {}, sleep=s["hours"], night=night.isoformat())
    if s.get("start"):
        m.update(bedtime=s["start"].timestamp(), wake=s["end"].timestamp())
    r = store.remember(text=body, title=f"Sleep about {s['hours']:g} h ({eve:%a} night)", kind="fact",
                       shelf="body/sleep", tags=["sleep"], source="said", meta=m, at=ts)
    when = f" ({_hm(s['start'])}–{_hm(s['end'])})" if s.get("start") else ""
    return {"id": r.get("id"), "hours": s["hours"], "night": night.isoformat(),
            "answer": f"Logged: {s['hours']:g} hours of sleep {eve:%A} night{when}. It replaces the watch's reading."}
