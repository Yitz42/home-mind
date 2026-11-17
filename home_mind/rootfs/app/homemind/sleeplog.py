"""Sleep said in words: "slept from 12 to 7am last night", "went to bed at 11:30 and woke up at 6:45", "slept 6.5
hours", "got 7 hours of sleep". Filed on body/sleep, where it beats the watch (which reads 0 when it was off)."""
import re
from datetime import datetime, timedelta

T = r"(?:\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|am|pm)?|midnight|noon)"
NOT_HOURS = r"(?!\s*(?:h\b|hrs?\b|hours?\b|-?\s*\d))"
CUE = re.compile(r"\b(?:slept|sleep|sleeping|asleep|bed|crashed|dozed|conked|passed out|lights out|turned in|"
                 r"hit the (?:sack|hay)|last night)\b", re.I)
BED = re.compile(r"\b(?:slept|sleep|asleep|bed|crashed|dozed off|dozed|conked out|passed out|lights out|turned in|"
                 r"hit the (?:sack|hay))\b[^.\d?]{0,25}?(?<![\d:])(" + T + r")" + NOT_HOURS, re.I)
WAKE = re.compile(r"\b(?:woke|wake|waking|awake|up|got up|alarm|out of bed)\b[^.\d?]{0,15}?(?<![\d:])(" + T + r")"
                  + NOT_HOURS, re.I)
RANGE = re.compile(r"(?:\bbetween\s+)?(?<![\d:])(" + T + r")\s*(?:to|till|til|until|thru|through|and|-|–|—)\s*(" + T + r")"
                   + NOT_HOURS, re.I)
HOURS = re.compile(r"(?<![\d:])(?:(\d{1,2}(?:\.\d+)?)\s*(?:-|to|or)\s*)?(\d{1,2}(?:\.\d+)?|a half|half)\s*(and a half\s*)?"
                   r"(?:h\b|hrs?\b|hours?\b)", re.I)
STRONG = re.compile(r"\b(?:slept|sleep|sleeping|asleep|bed|crashed|dozed|conked|passed out|lights out|turned in|"
                    r"hit the (?:sack|hay))\b", re.I)
GOT = re.compile(r"^\s*(?:i\s+)?(?:only\s+|just\s+|barely\s+)?(?:got|had)\s+(?:only\s+|about\s+|around\s+|like\s+|maybe\s+)?"
                 r"[\d.]+\s*(?:-\s*[\d.]+\s*)?(?:and a half\s*)?(?:h|hrs?|hours?)\b(?:\s+(?:of\s+sleep\s+)?last night)?[\s.!]*$", re.I)
QUESTION = re.compile(r"^\s*(?:how|what|when|should|can|could|why|which|show|tell|remind|"
                      r"(?:did|do|does|was|is|were|am)\s+(?:i|my|it|that|the|you|we)\b)\b", re.I)
OTHER_NIGHT = re.compile(r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b|\bago\b|\bthe night before\b|\bon the \d|"
                         r"\btonight\b|\bgoing to\b|\bgonna\b|\bwill\b|\bshould\b|\bplan|\btry to\b|\bwant to\b|\bnap|"
                         r"\bneed\b|\busually\b|\bnormally\b|\btypically\b|\bevery night\b|\bon average\b|\bgoal\b", re.I)


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


def _span(a, b, wake_day):
    a, b = _clock(a), _clock(b)
    if not a or not b:
        return None
    ah, am_, aap = a
    bh, bm, _ = b
    if aap is None and ah <= 12:          # bedtime with no am/pm: 7-11 is evening, 12 is midnight, 1-6 is night
        ah = 0 if ah == 12 else ah + 12 if ah >= 7 else ah
    day0 = datetime.combine(wake_day, datetime.min.time())
    end, start = day0 + timedelta(hours=bh, minutes=bm), day0 + timedelta(hours=ah, minutes=am_)
    if start >= end:
        start -= timedelta(days=1)
    hours = round((end - start).total_seconds() / 3600, 2)
    return {"hours": hours, "start": start, "end": end, "night": wake_day} if 0.5 <= hours <= 16 else None


def parse_sleep(text, now=None):
    """Last night's sleep from words, or None. Returns {hours, start?, end?, night (the date woken up on)}. The pieces
    are found anywhere: "slept from 12 to 7", "crashed at 12, up at 7", "in bed 12-7", "slept like 6 hours"."""
    from .langs import digits
    t = digits((text or "").replace("’", "'")).strip()
    t = re.sub(r"\bhalf past (\d{1,2})\b", r"\1:30", t, flags=re.I)
    t = re.sub(r"\bquarter past (\d{1,2})\b", r"\1:15", t, flags=re.I)
    t = re.sub(r"\bquarter to (\d{1,2})\b", lambda m: f"{int(m.group(1)) - 1 or 12}:45", t, flags=re.I)
    t = re.sub(r"\b(\d{1,2}) o'?clock\b", r"\1", t, flags=re.I)
    t = re.sub(r"\b(\d{1,2}) (15|30|45)\b", r"\1:\2", t)              # "eleven thirty" -> 11:30
    if not t or t.endswith("?") or QUESTION.match(t) or OTHER_NIGHT.search(t) or not CUE.search(t):
        return None
    if not STRONG.search(t) and not GOT.match(t):
        return None                # "last night" alone: "watched TV for 2 hours last night" isn't sleep
    wake_day = (datetime.fromtimestamp(now) if now else datetime.now()).date()   # the night that ended this morning
    bed = BED.search(t)
    if bed:
        wake = WAKE.search(t, bed.end())
        if wake:
            return _span(bed.group(1), wake.group(1), wake_day)
    r = RANGE.search(t)
    if r and not re.search(r"\d\s*(?:x|×|sets?|reps?|miles?|min)", t, re.I):
        return _span(r.group(1), r.group(2), wake_day)
    m = HOURS.search(t)
    if m:
        h = 0.5 if "half" in m.group(2).lower() else float(m.group(2))
        if m.group(3):
            h += 0.5
        if m.group(1):                     # "6-7 hours": the middle
            h = (float(m.group(1)) + h) / 2
        if 0.5 <= h <= 16:
            return {"hours": round(h, 2), "night": wake_day}
    return None


SLEEP_CUE = CUE


def _hm(d):
    return f"{d:%I:%M %p}".lstrip("0").replace(":00 ", " ")


def log_sleep(store, text, now=None, meta=None, read_as=None):
    """File it on body/sleep. Returns {answer, id, hours}, or None if it isn't sleep. read_as: the same thing in the
    plain form (a model's rewrite of other wording), read instead of text; the note keeps what was said."""
    ts = now or store.clock()
    s = parse_sleep(read_as or text, ts)
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
    plain = (f"slept from {_hm(s['start']).lower()} to {_hm(s['end']).lower()}" if s.get("start")
             else f"slept {s['hours']:g} hours")
    return {"id": r.get("id"), "hours": s["hours"], "night": night.isoformat(), "plain": plain,
            "answer": f"Logged: {s['hours']:g} hours of sleep {eve:%A} night{when}. It replaces the watch's reading."}
