"""Structure read out of plain notes when they are filed, and the lines worked out from it when a question needs
them. Nothing here needs a model; every rule is a small pattern, tested in tests/test_abilities.py and the suites.

  valid_from  "Last Tuesday the code changed to 1111", filed on Friday, became true on Tuesday (#49)
  recur       "The trash goes out every Tuesday": the next time is worked out when asked (#33)
  chore       "Change the furnace filter every 3 months" plus "I changed the furnace filter": last done, next due (#68)
  place       "The drill is in the red toolbox", "I moved the red toolbox to the shed": where things are, nested (#56)
  fields      "The dishwasher is a Bosch SHX78, serial 4471, warranty until March 2027": typed fields (#57)
  profile     "Dana is my partner": always shown, and "my partner" finds notes about Dana (#37)
"""
import calendar
import re
from datetime import date, datetime, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
NUMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "eleven": 11, "twelve": 12, "a": 1, "an": 1, "other": 2}
ART = r"(?:the|my|our|a|an|your)\s+"


def _n(s):
    s = (s or "").lower()
    return NUMS[s] if s in NUMS else int(s)


def _day(ts):
    return datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0)


def _mon(s):
    s = s.lower()[:3]
    return [m[:3] for m in MONTHS].index(s) + 1


def parse_date(text, now_ts=None, future=False):
    """A date written in a note: 'March 2027', 'Mar 3, 2027', 'March 3', '2027-03-01', '3/1/2027'. Returns a date or
    None. A month with no year is this year (next year if future and already past)."""
    t = (text or "").strip().lower()
    today = _day(now_ts).date() if now_ts else date.today()
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        return _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", t)
    if m:
        y = int(m.group(3))
        return _safe(y + 2000 if y < 100 else y, int(m.group(1)), int(m.group(2)))
    m = re.search(rf"\b{MON}\.? (\d{{1,2}})(?:st|nd|rd|th)?,? (\d{{4}})\b", t)
    if m:
        return _safe(int(m.group(3)), _mon(m.group(1)), int(m.group(2)))
    m = re.search(rf"\b{MON}\.?,? (\d{{4}})\b", t)
    if m:
        return _safe(int(m.group(2)), _mon(m.group(1)), 1)
    m = re.search(rf"\b{MON}\.? (\d{{1,2}})(?:st|nd|rd|th)?\b", t)
    if m:
        d = _safe(today.year, _mon(m.group(1)), int(m.group(2)))
        if d and future and d < today:
            d = _safe(today.year + 1, d.month, d.day)
        return d
    return None


def _safe(y, mo, d):
    try:
        return date(y, mo, d)
    except ValueError:
        return None


# ---- #49 when it became true ---------------------------------------------------------------------------------------
PAST = re.compile(r"\b(yesterday|(\d+|one|two|three|four|five|six|seven|a) (day|week)s? ago|last (" + "|".join(WEEKDAYS)
                  + r")|(?:on|this past) (" + "|".join(WEEKDAYS) + r")|(?:on|since|as of) " + MON + r"\.? \d{1,2}\b)",
                  re.I)


CHANGED = re.compile(r"\b(changed|moved|switched|became|was set|were set|reset|updated|replaced|swapped|started|"
                     r"stopped|got|went|put|set it|changed it)\b", re.I)


def valid_from(text, now_ts):
    """When a note says the change happened earlier than it was filed: the start of that day (noon, so a same-day
    note filed later still sorts after it). None if it doesn't say, or says the future. 'On Monday' alone is a habit
    ("gets watered on Monday"), so a weekday or a date also needs a past change ("on Monday I changed it")."""
    m = PAST.search(text or "")
    if not m:
        return None
    s0 = m.group(0).lower()
    if s0.startswith("on ") or s0.startswith("since ") or s0.startswith("as of "):
        lead = re.sub(r"^\s*(?:\w+(?: \w+)?:\s*)?", "", text[:m.start()])   # "Update: " and the like
        if not CHANGED.search(text) or not (lead.strip() == "" or lead.rstrip().endswith(",")):
            return None
    s = m.group(0).lower()
    today = _day(now_ts)
    if s == "yesterday":
        d = today - timedelta(days=1)
    elif "ago" in s:
        k = _n(m.group(2))
        d = today - timedelta(days=k * (7 if m.group(3).startswith("week") else 1))
    elif s.startswith("last ") or s.startswith("on ") and s.split()[1] in WEEKDAYS or s.startswith("this past"):
        wd = WEEKDAYS.index(s.split()[-1])
        back = (today.weekday() - wd) % 7 or 7
        d = today - timedelta(days=back)
    else:
        pd = parse_date(s.split(" ", 1)[1], now_ts)
        if not pd:
            return None
        d = datetime.combine(pd, datetime.min.time())
        if d > today:
            d = d.replace(year=d.year - 1)
    ts = (d + timedelta(hours=12)).timestamp()
    return ts if ts < now_ts else None


# ---- #33 repeating things, #68 chores ------------------------------------------------------------------------------
EVERY_DOW = re.compile(r"\bevery (other |second )?(" + "|".join(WEEKDAYS) + r")\b|\bon (" + "|".join(WEEKDAYS) + r")s\b|"
                       r"\b(" + "|".join(WEEKDAYS) + r")s\b", re.I)
MONTHLY = re.compile(r"\b(?:on )?the (\d{1,2})(?:st|nd|rd|th) (?:of )?(?:every|each) month\b|"
                     r"\b(?:every|each) month on the (\d{1,2})(?:st|nd|rd|th)\b|"
                     r"\bmonthly on the (\d{1,2})(?:st|nd|rd|th)\b|\bdue (?:on )?the (\d{1,2})(?:st|nd|rd|th)\b", re.I)
INTERVAL = re.compile(r"\bevery (\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|other)? ?"
                      r"(day|week|month|year)s?\b|\b(daily|weekly|monthly|yearly|annually|once a (?:week|month|year))\b",
                      re.I)
UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
STARTING = re.compile(r"\b(?:starting|from|beginning) (?:on )?(.+?)(?:[.;,]|$)", re.I)


def recurrence(text, now_ts):
    """How often something repeats, or None. weekly: {"kind": "weekly", "dow", "every": 1|2, "anchor"};
    monthly: {"kind": "monthly", "dom"}; interval: {"kind": "interval", "days", "anchor"}. The anchor of 'every other'
    is the first such weekday on or after the 'starting' date, else on or after the day it was filed."""
    t = text or ""
    m = MONTHLY.search(t)
    if m:
        return {"kind": "monthly", "dom": int(next(g for g in m.groups() if g))}
    start = None
    s = STARTING.search(t)
    if s:
        sd = parse_date(s.group(1), now_ts, future=True)
        if not sd:
            w = re.search(r"\b(" + "|".join(WEEKDAYS) + r")\b", s.group(1), re.I)
            if w:
                today = _day(now_ts)
                ahead = (WEEKDAYS.index(w.group(1).lower()) - today.weekday()) % 7
                sd = (today + timedelta(days=ahead)).date()
        if sd:
            start = datetime.combine(sd, datetime.min.time())
    m = EVERY_DOW.search(t)
    if not m and re.search(r"\b(weekly|every week|each week|once a week)\b", t, re.I):
        m = re.search(r"\b()(?:on )(" + "|".join(WEEKDAYS) + r")()()\b", t, re.I)
    if m and not re.search(r"\bevery (\d+|two|three|four) weeks\b", t, re.I):
        wd = WEEKDAYS.index((m.group(2) or m.group(3) or m.group(4)).lower())
        base = start or _day(now_ts)
        ahead = (wd - base.weekday()) % 7
        return {"kind": "weekly", "dow": wd, "every": 2 if m.group(1) else 1,
                "anchor": (base + timedelta(days=ahead)).timestamp()}
    m = INTERVAL.search(t)
    if m:
        if m.group(3):
            word = m.group(3).lower()
            unit = "week" if "week" in word else "month" if "month" in word else "year" if ("year" in word or "annual" in word) else "day"
            k = 1
        else:
            unit, k = m.group(2).lower(), _n(m.group(1) or "1")
        days = UNIT_DAYS[unit] * k
        return {"kind": "interval", "days": days, "anchor": (start or _day(now_ts)).timestamp()}
    return None


def next_time(rec, now_ts):
    """The next date (on or after today) a repeating thing happens."""
    today = _day(now_ts)
    if rec["kind"] == "weekly":
        anchor = _day(rec["anchor"])
        if rec.get("every", 1) == 1:
            ahead = (rec["dow"] - today.weekday()) % 7
            return (today + timedelta(days=ahead)).date()
        weeks = 0 if today <= anchor else -(-((today - anchor).days) // 14)
        d = anchor + timedelta(days=14 * weeks)
        while d < today:
            d += timedelta(days=14)
        return d.date()
    if rec["kind"] == "monthly":
        y, mo = today.year, today.month
        for _ in range(3):
            last = calendar.monthrange(y, mo)[1]
            d = date(y, mo, min(rec["dom"], last))
            if d >= today.date():
                return d
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    if rec["kind"] == "interval":
        anchor = _day(rec["anchor"])
        n = 0 if today <= anchor else -(-((today - anchor).days) // rec["days"])
        return (anchor + timedelta(days=rec["days"] * n)).date()
    return None


CHORE_VERB = r"(?:change|replace|clean|check|test|flush|empty|descale|service|oil|sharpen|charge|water|mow|vacuum|wash|" \
             r"rotate|refill|reset|inspect|drain|backup|back up|update|renew)"
PAST_VERB = {"change": "changed", "replace": "replaced", "clean": "cleaned", "check": "checked", "test": "tested",
             "flush": "flushed", "empty": "emptied", "descale": "descaled", "service": "serviced", "oil": "oiled",
             "sharpen": "sharpened", "charge": "charged", "water": "watered", "mow": "mowed", "vacuum": "vacuumed",
             "wash": "washed", "rotate": "rotated", "refill": "refilled", "reset": "reset", "inspect": "inspected",
             "drain": "drained", "backup": "backed up", "back up": "backed up", "update": "updated", "renew": "renewed"}
CHORE = re.compile(rf"^\s*(?:remember to |i (?:should|need to|have to) )?({CHORE_VERB})\s+(?:{ART})?(.+?)\s+"
                   rf"(?:every|each|once a)\b|^\s*(?:{ART})?(.+?)\s+needs? (?:to be )?({CHORE_VERB})[a-z]*\s+(?:every|each|once a)\b",
                   re.I)
DONE = re.compile(r"\b(?:i |we |just |finally )*(" + "|".join(sorted(set(PAST_VERB.values()), key=len, reverse=True))
                  + r")\b\s+(?:" + ART + r")?(.+?)(?:\s+(?:today|yesterday|this morning|last \w+|on \w+|again))?[.!]?$", re.I)


VERBS = set(PAST_VERB)


def _base_ing(text):
    """'needs replacing' -> 'needs replace' (only for chore verbs), so one pattern reads both."""
    def f(m):
        w = m.group(1).lower()
        for cand in (w + "e", w, w[:-1] if len(w) > 2 and w[-1] == w[-2] else None):
            if cand in VERBS:
                return cand
        return m.group(0)
    return re.sub(r"\b(\w+?)ing\b", f, text)


def _past(verb):
    """swap -> swapped, tune -> tuned, oil -> oiled (for verbs not in PAST_VERB)."""
    v = verb.lower()
    if v.endswith("ed"):
        return v                 # already past ("swapped")
    if v.endswith("e"):
        return v + "d"
    if re.search(r"[^aeiou][aeiou][bdgkmnprt]$", v):
        return v + v[-1] + "ed"
    return v + "ed"


def chore(text):
    """'Change the furnace filter every 3 months' -> {"verb": "change", "thing": "furnace filter"}."""
    m = CHORE.search(_base_ing(text or ""))
    if not m:
        return None
    verb, thing = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
    thing = re.sub(rf"^{ART}", "", thing.strip(), flags=re.I)
    return {"verb": verb.lower(), "thing": thing.lower()} if thing else None


def done_of(text):
    """'I changed the furnace filter today' -> ("changed", "furnace filter")."""
    m = DONE.search((text or "").strip())
    if not m:
        return None
    return m.group(1).lower(), re.sub(rf"^{ART}", "", m.group(2).strip().lower(), flags=re.I)


# ---- #56 where things are ------------------------------------------------------------------------------------------
NP = r"((?:[\w'-]+\s+){0,4}?[\w'-]+)"
PREP = r"(?:in|inside|into|on|under|behind|in the back of|on top of|next to)"
PLACE = [re.compile(rf"^\s*(?:{ART})?{NP}\s+(?:is|are)\s+(?:kept\s+|stored\s+|now\s+)*{PREP}\s+(?:{ART})?{NP}\s*[.!]?$", re.I),
         re.compile(rf"^\s*(?:i\s+|we\s+)?(?:put|moved|left|stored|keep|placed|stuck)\s+(?:{ART})?{NP}\s+"
                    rf"(?:in|into|to|on|under|behind|inside)\s+(?:{ART})?{NP}\s*[.!]?$", re.I)]
NOT_PLACE = re.compile(r"\b(code|password|charge of|love|trouble|good|bad|on sale|on fire|in stock|in progress|on time|"
                       r"in town|on vacation|due|open|closed|broken|empty|full|ready)\b", re.I)


_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
          r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")      # whole words only: "the marsh" is a place
_NUM = r"(?:\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|a few|a couple of|several)"
WHEN = re.compile(
    rf"^(?:(?:{'|'.join(WEEKDAYS)})s?(?:\s+(?:morning|afternoon|evening|night)s?)?|weekends?|weekdays?|today|tonight|"
    rf"tomorrow|yesterday|noon|midnight|the (?:morning|afternoon|evening|weekend)|"
    rf"(?:next|this|last|every|each)\s+\w+(?:\s+\w+)?|"
    rf"{_NUM}\s+(?:minutes?|hours?|days?|weeks?|months?|years?)(?:\s+(?:from now|time))?|"
    rf"(?:the\s+)?\d{{1,2}}(?:st|nd|rd|th)?(?:\s+of\s+\w+)?|"
    rf"{_MONTH}\.?(?:\s+\d{{1,2}}(?:st|nd|rd|th)?)?(?:,?\s+\d{{4}})?|\d{{4}}|"
    rf"\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm|a\.m\.|p\.m\.)?|\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?)$", re.I)


def place(text):
    """'The drill is in the red toolbox' / 'I moved the red toolbox to the shed' -> {"thing", "in"}. A time is not a
    place: "the meeting is on Friday", "the exam is in two weeks", "the birthday is on March 3"."""
    t = (text or "").strip()
    if len(t) > 120 or NOT_PLACE.search(t):
        return None
    for rx in PLACE:
        m = rx.search(t)
        if m:
            thing, where = m.group(1).lower().strip(), m.group(2).lower().strip()
            if thing in ("it", "they", "that", "this", "everything") or thing == where or WHEN.match(where):
                return None
            return {"thing": thing, "in": where}
    return None


# ---- #57 typed items with fields ------------------------------------------------------------------------------------
FIELD_RX = {
    "model": re.compile(r"\bmodel(?: number| no\.?)?(?: is|:)?\s+([A-Z0-9][\w-]{2,})", re.I),
    "serial": re.compile(r"\bserial(?: number| no\.?)?(?: is|:)?\s+([A-Z0-9][\w-]{2,})", re.I),
    "bought": re.compile(r"\b(?:bought|purchased|got it)(?: (?:in|on))?\s+(" + MON + r"\.?(?: \d{1,2}(?!\d),?)?(?: \d{4})?|\d{4}-\d\d-\d\d|\d{1,2}/\d{1,2}/\d{2,4})", re.I),
    "warranty_until": re.compile(r"\bwarranty(?: (?:on|for) (?:the |my )?[\w ]{1,30}?)?(?: is good| runs| lasts)?(?: until| till| through| ends| expires| is up)(?: on| in)?\s+("
                                 + MON + r"\.?(?: \d{1,2}(?!\d),?)?(?: \d{4})?|\d{4}-\d\d-\d\d|\d{1,2}/\d{1,2}/\d{2,4})", re.I),
    "phone": re.compile(r"\b(?:phone|number|cell)(?: number)?(?: is|:)?\s+(\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})", re.I),
    "birthday": re.compile(r"\bbirthday(?: is|:)?\s+(?:on\s+)?(" + MON + r"\.? \d{1,2})", re.I),
}
BRAND = re.compile(r"\bis an? ([A-Z][a-zA-Z]+) ([A-Z0-9][\w-]{2,})\b")
APPLIANCE = {"dishwasher", "fridge", "refrigerator", "washer", "dryer", "oven", "stove", "microwave", "furnace",
             "boiler", "water heater", "air conditioner", "ac", "heater", "dehumidifier", "humidifier", "fan", "tv",
             "television", "router", "printer", "laptop", "vacuum", "freezer", "range", "thermostat", "car", "bike"}


def fields(text, now_ts):
    """Typed fields in a note, and the thing's type. Dates are stored as YYYY-MM-DD."""
    t = text or ""
    out = {}
    for k, rx in FIELD_RX.items():
        m = rx.search(t)
        if m:
            v = m.group(1).strip().rstrip(".,")
            if k in ("bought", "warranty_until"):
                d = parse_date(v, now_ts, future=k == "warranty_until")
                if not d:
                    continue
                if k == "warranty_until" and re.fullmatch(MON + r"\.?,? \d{4}", v.lower()):
                    d = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])   # "until March 2027": its end
                v = d.isoformat()
            out[k] = v
    m = BRAND.search(t)
    if m and "model" not in out:
        out["brand"], out["model"] = m.group(1), m.group(2)
    if not out:
        return None
    lower = t.lower()
    kind = "appliance" if any(re.search(rf"\b{a}\b", lower) for a in APPLIANCE) or {"model", "serial", "warranty_until"} & set(out) \
        else "person" if {"phone", "birthday"} & set(out) else "thing"
    return {"type": kind, "fields": out}


# ---- #37 who is who ------------------------------------------------------------------------------------------------
ROLE = r"(partner|wife|husband|spouse|girlfriend|boyfriend|son|daughter|kid|child|mom|mother|dad|father|brother|sister|" \
       r"roommate|neighbor|neighbour|landlord|doctor|dentist|vet|plumber|electrician|boss|dog|cat|cleaner|babysitter|" \
       r"nanny|aunt|uncle|grandma|grandpa|cousin|friend|mechanic|accountant|lawyer|therapist)"
ALIAS = [re.compile(rf"^\s*([A-Z][a-z]+(?: [A-Z][a-z]+)?) is (?:[Mm]y|[Oo]ur) {ROLE}\b", re.M),
         re.compile(rf"\b(?:[Mm]y|[Oo]ur) {ROLE}(?:'s name)? is ([A-Z][a-z]+(?: [A-Z][a-z]+)?)\b")]


def alias(text):
    """'Dana is my partner' / 'My dentist is Dr Kell' -> ("partner", "Dana")."""
    t = text or ""
    m = ALIAS[0].search(t)
    if m:
        return m.group(2).lower(), m.group(1)
    m = ALIAS[1].search(t)
    if m:
        return m.group(1).lower(), m.group(2)
    return None


def annotate(text, title, now_ts):
    """What to add to a note's meta when it is filed (only keys found)."""
    both = f"{title} {text}" if title and title not in text else text
    out = {}
    v = valid_from(text, now_ts)
    if v:
        out["valid_from"] = v
    c = chore(text)
    rec = recurrence(text, now_ts)
    if c and rec:
        out["chore"] = dict(c, every_days=rec["days"] if rec["kind"] == "interval" else 7 * rec.get("every", 1)
                            if rec["kind"] == "weekly" else 30)
    elif rec and not done_of(text):
        out["recur"] = rec
    p = place(text)
    if p:
        out["place"] = p
    f = fields(both, now_ts)
    if f:
        out.update(f)
    a = alias(text)
    if a:
        out["alias"] = {"role": a[0], "name": a[1]}
    return out


# ---- worked out at question time -----------------------------------------------------------------------------------
def _ago(n):
    return "today" if n == 0 else "yesterday" if n == 1 else f"{n} days ago"


def _fmt(d):
    return d.strftime("%a %b %d, %Y").replace(" 0", " ")


def _words(s):
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in ("the", "my", "a", "an", "our")]


def _rows(store, where, args=()):
    return [store._row(r) for r in store.db.execute(
        f"SELECT * FROM items WHERE status IN ('active','faded') AND {where}", args).fetchall()]


def eff(h):
    return float((h.get("meta") or {}).get("valid_from") or h["created"])


WHEN_NEXT = re.compile(r"\b(when|what day|which day|next|how soon|is .* (?:today|tomorrow))\b", re.I)
CHORE_Q = re.compile(r"\b(last|due|overdue|when did|how long since|chores?|maintenance|next)\b", re.I)
WHERE_Q = re.compile(r"\b(where(?:'s| is| are| did| do| can)?|what(?:'s| is) (?:in|inside|on)|what do i keep in|"
                     r"what(?:'s| is) kept in)\b", re.I)
FIELD_Q = {"model": r"\bmodel\b", "serial": r"\bserial\b", "bought": r"\b(bought|purchased|buy|purchase)\b",
           "warranty_until": r"\bwarrant", "phone": r"\b(phone|number|call)\b", "birthday": r"\bbirthday\b"}


def recurring_lines(store, question, hits, now):
    if not WHEN_NEXT.search(question or "") or re.search(r"\b(did|last|ago|was|were)\b", question or "", re.I):
        return []          # "when did I last ..." asks about the past, not the next time
    out = []
    for h in hits[:4]:
        rec = (h.get("meta") or {}).get("recur")
        if rec and not h.get("newer"):
            out.append(f"NEXT (worked out from #{h['id']}, which repeats): {_fmt(next_time(rec, now))}")
    return out[:2]


def chore_states(store, now, pick=None):
    """Each chore with when it was last done and when it is next due: dicts with verb, thing, every, last (ts or
    None), due (date), left (days; negative = overdue), id."""
    chores = pick if pick is not None else [h for h in _rows(store, "meta LIKE '%\"chore\"%'")
                                            if (h.get("meta") or {}).get("chore")]
    today = _day(now)
    out = []
    for c in sorted(chores, key=lambda c: c["meta"]["chore"]["thing"]):
        ch = c["meta"]["chore"]
        tw = set(_words(ch["thing"]))
        last = None
        word = max(tw, key=len) if tw else ""
        past = PAST_VERB.get(ch["verb"]) or _past(ch["verb"])
        for d in _rows(store, "kind != 'procedure' AND body LIKE ?", (f"%{word}%",)):
            if d["id"] == c["id"] or (d.get("meta") or {}).get("chore"):
                continue
            dn = done_of(d["body"])
            hit = dn and tw <= set(_words(dn[1])) | set(_words(d["body"])) and dn[0] == past
            if not hit and ch["verb"] not in PAST_VERB:
                hit = bool(re.search(rf"\b{re.escape(past)}\b", d["body"], re.I)) and tw <= set(_words(d["body"]))
            if hit:
                t = eff(d)
                last = t if last is None or t > last else last
        due = (_day(last or c["created"]) + timedelta(days=ch["every_days"])).date()
        out.append({"id": c["id"], "verb": ch["verb"], "thing": ch["thing"], "every": ch["every_days"], "last": last,
                    "due": due, "left": (due - today.date()).days})
    return out


def _state(left):
    return f"overdue by {-left} day{'s' if left != -1 else ''}" if left < 0 else "due today" if left == 0 \
        else f"in {left} day{'s' if left != 1 else ''}"


def chore_lines(store, question, hits, now):
    q = question or ""
    if not CHORE_Q.search(q):
        return []
    chores = [h for h in _rows(store, "meta LIKE '%\"chore\"%'") if (h.get("meta") or {}).get("chore")]
    if not chores:
        return []
    qw = set(_words(q))
    every = re.search(r"\b(chores?|maintenance|overdue|what(?:'s| is) due)\b", q, re.I)
    pick = chores if every else [c for c in chores if set(_words(c["meta"]["chore"]["thing"])) <= qw]
    if not pick:
        return []
    today = _day(now)
    lines = []
    for c in chore_states(store, now, pick):
        last = c["last"]
        lines.append(f"- {c['verb']} the {c['thing']}: every {c['every']} days; last done "
                     + (f"{_fmt(_day(last))} ({_ago((today - _day(last)).days)})" if last else "never noted")
                     + f"; next due {_fmt(c['due'])} ({_state(c['left'])})")
    if every and re.search(r"\boverdue|due\b", q, re.I):
        lines = [x for x in lines if "overdue" in x or "due today" in x] or ["- nothing is overdue"]
    return ["CHORES (worked out from the chore notes and when each was last done):"] + lines


def due_lines(store, now):
    """For the morning summary: chores overdue or due by tomorrow, and repeating things happening today or tomorrow."""
    out = []
    for c in chore_states(store, now):
        if c["left"] <= 1:
            out.append(f"{c['verb']} the {c['thing']} ({_state(c['left']).replace('in 1 day', 'tomorrow')})")
    today = _day(now).date()
    for h in _rows(store, "meta LIKE '%\"recur\"%' AND kind != 'procedure'"):
        rec = (h.get("meta") or {}).get("recur")
        if not rec:
            continue
        nxt = next_time(rec, now)
        if nxt and (nxt - today).days <= 1:
            when = "today" if nxt == today else "tomorrow"
            t = re.sub(r"\s+(?:is|are|goes out|happens|happen)\b.*$", "", h["title"]).strip()
            out.append(f"{t} ({when})")
    return out


# Notes Home Mind writes itself (summaries, studies, readings) are not things the user said: a study's "daily" is not
# a chore, and a summary's "weekly" is not a repeating event.
SYSTEM_SOURCES = {"home", "calendar", "automations", "scale", "nutrition-tracker", "morning", "patterns", "weather",
                  "check-in", "journal-summary", "summary", "tidy", "usage"}
SYSTEM_SHELVES = ("summaries/", "journal/summaries", "nutrition/", "home/", "body/weight", "body/meals", "comfort/")


def wanted(source, shelf):
    """Whether a note is the user's own words (so its sentences are read for structure)."""
    src, sh = (source or ""), (shelf or "")
    return not (src in SYSTEM_SOURCES or src.startswith("pack:") or sh.startswith(SYSTEM_SHELVES))


DERIVED = ("valid_from", "recur", "chore", "place", "fields", "type", "alias")


def unread_system(store):
    """Take structure off notes Home Mind wrote itself (read by mistake before 0.19.1). Returns how many."""
    import json
    n = 0
    for r in store.db.execute("SELECT id, meta, source, shelf FROM items").fetchall():
        if wanted(r["source"], r["shelf"]):
            continue
        m = json.loads(r["meta"] or "{}")
        if any(k in m for k in DERIVED):
            for k in DERIVED:
                m.pop(k, None)
            store.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(m), r["id"]))
            n += 1
    store.db.commit()
    return n


def backfill(store):
    """Read structure from notes filed before 0.19 (once): the same rules as filing, dated by when each was filed."""
    n = 0
    rows = store.db.execute("SELECT id, kind, title, body, shelf, created, meta, source FROM items WHERE status IN "
                            "('active','faded','superseded') AND kind NOT IN ('procedure','task') AND source NOT IN "
                            "('home','calendar','automations','scale') AND source NOT LIKE 'pack:%'").fetchall()
    import json
    for r in rows:
        if not wanted(r["source"], r["shelf"]):
            continue
        m = json.loads(r["meta"] or "{}")
        if any(k in m for k in ("valid_from", "recur", "chore", "place", "fields", "alias")):
            continue
        add = annotate(r["body"] or "", r["title"] or "", r["created"])
        if not add:
            continue
        m.update(add)
        shelf = "profile" if "alias" in add and r["shelf"] == "inbox" else r["shelf"]
        store.db.execute("UPDATE items SET meta=?, shelf=? WHERE id=?", (json.dumps(m), shelf, r["id"]))
        n += 1
    store.db.commit()
    return n


def _places(store):
    """thing -> (container, when, id), from each thing's latest place note."""
    cur = {}
    for h in _rows(store, "meta LIKE '%\"place\"%'"):
        p = (h.get("meta") or {}).get("place")
        if not p:
            continue
        t = eff(h)
        if p["thing"] not in cur or t > cur[p["thing"]][1]:
            cur[p["thing"]] = (p["in"], t, h["id"])
    return cur


def _match(name, qw):
    w = set(_words(name))
    return bool(w) and w <= qw


def place_lines(store, question, hits, now):
    q = question or ""
    if not WHERE_Q.search(q):
        return []
    cur = _places(store)
    if not cur:
        return []
    qw = set(_words(q))
    out = []
    inside = re.search(r"\bwhat(?:'s| is| do i keep| is kept)? (?:in|inside|on)\b", q, re.I)
    if inside:
        boxes = [b for b in {v[0] for v in cur.values()} | set(cur) if _match(b, qw)]
        if boxes:
            box = max(boxes, key=lambda b: len(_words(b)))
            found = []
            for thing in cur:
                chain, x, seen = [], thing, set()
                while x in cur and x not in seen and len(chain) < 6:
                    seen.add(x)
                    x = cur[x][0]
                    chain.append(x)
                if box in chain:
                    found.append(thing if chain[0] == box else f"{thing} (in the {' in the '.join(chain[:chain.index(box)])})")
            if found:
                out.append(f"IN THE {box.upper()} (worked out from where things were last put): " + "; ".join(sorted(found)))
            return out
    things = [t for t in cur if _match(t, qw)]
    if not things:
        return []
    thing = max(things, key=lambda t: len(_words(t)))
    chain, x, seen = [], thing, set()
    while x in cur and x not in seen and len(chain) < 6:
        seen.add(x)
        c, t, i = cur[x]
        chain.append(f"the {c} (#{i}, {_fmt(_day(t))})")
        x = c
    return [f"WHERE (worked out from the latest place notes): the {thing} is in " + ", which is in ".join(chain)]


def field_lines(store, question, hits, now):
    q = question or ""
    asked = [k for k, rx in FIELD_Q.items() if re.search(rx, q, re.I)]
    if not asked:
        return []
    out = []
    many = re.search(r"\b(which|what|list|all|any)\b.*\b(warranties|warranty|appliances|serials|models|birthdays)\b|"
                     r"\b(warranties|birthdays)\b", q, re.I)
    if many:
        rows = [h for h in _rows(store, "meta LIKE '%\"fields\"%'") if (h.get("meta") or {}).get("fields")]
        key = "warranty_until" if re.search(r"warrant", q, re.I) else "birthday" if re.search(r"birthday", q, re.I) \
            else asked[0]
        have = [(h, h["meta"]["fields"][key]) for h in rows if key in h["meta"]["fields"]]
        if key == "warranty_until":
            today = _day(now).date()
            end = None
            if re.search(r"\bthis year\b", q, re.I):
                end = date(today.year, 12, 31)
            elif re.search(r"\bnext year\b", q, re.I):
                end = date(today.year + 1, 12, 31)
            m = re.search(r"\b(?:in the next|within) (\d+|one|two|three|six) (month|year)s?\b", q, re.I)
            if m:
                end = today + timedelta(days=_n(m.group(1)) * (30 if m.group(2) == "month" else 365))
            if re.search(r"\b(expired|ended|run out|ran out)\b", q, re.I):
                have = [(h, v) for h, v in have if date.fromisoformat(v) < today]
            elif end:
                have = [(h, v) for h, v in have if today <= date.fromisoformat(v) <= end]
        label = {"warranty_until": "warranty until", "birthday": "birthday"}.get(key, key)
        out.append(f"{label.upper()} (every note with one{', in the range asked' if key == 'warranty_until' else ''}): "
                   + ("; ".join(f"{h['title'][:50]} (#{h['id']}): {v}" for h, v in sorted(have, key=lambda x: x[1]))
                      or "none"))
        return out
    for h in hits[:3]:
        f = (h.get("meta") or {}).get("fields")
        if f and not h.get("newer") and any(k in f for k in asked):
            out.append(f"FIELDS of #{h['id']} ({h['meta'].get('type', 'thing')}): "
                       + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in f.items()))
    return out[:2]


def derived_lines(store, question, hits, now):
    """Everything worked out for this question, in the order a model should read it."""
    out = []
    for fn in (recurring_lines, chore_lines, place_lines, field_lines):
        try:
            out += fn(store, question, hits, now)
        except Exception:  # noqa: BLE001  (a parse slip must never break an answer)
            pass
    return out


# ---- #37 profile ---------------------------------------------------------------------------------------------------
PROFILE_MAX = 15


def profile(store):
    """The always-loaded lines: notes on the profile shelf (newest per title first), at most PROFILE_MAX."""
    rows = store.db.execute("SELECT * FROM items WHERE status='active' AND (shelf='profile' OR shelf LIKE 'profile/%') "
                            "ORDER BY created DESC LIMIT 60").fetchall()
    seen, lines = set(), []
    for r in rows:
        d = store._row(r)
        k = (d["meta"].get("alias") or {}).get("role") or d["title"].lower()
        if k in seen:
            continue
        seen.add(k)
        lines.append(d["body"] or d["title"])
        if len(lines) >= PROFILE_MAX:
            break
    return lines


def expand_aliases(store, question):
    """'my partner' -> 'my partner (Dana)', so the search also finds notes that only say Dana."""
    q = question or ""
    if not re.search(rf"\b(?:my|our) {ROLE}\b", q, re.I):
        return q
    roles = {}
    same = {"partner", "wife", "husband", "spouse", "girlfriend", "boyfriend"}
    for r in store.db.execute("SELECT meta FROM items WHERE status='active' AND meta LIKE '%\"alias\"%' "
                              "ORDER BY created").fetchall():
        import json
        a = json.loads(r["meta"] or "{}").get("alias")
        if a:
            roles[a["role"]] = a["name"]
            if a["role"] in same:
                for x in same:
                    roles.setdefault(x, a["name"])
    def sub(m):
        name = roles.get(m.group(2).lower())
        return f"{m.group(1)} {name}" if name and name.lower() not in q.lower() else m.group(1)
    return re.sub(rf"\b((?:my|our) {ROLE})\b", sub, q, flags=re.I)


def overview(store, now):
    """Everything worked out, for the web panel: profile, chores, repeating things, where things are, typed items."""
    today = _day(now)
    chores = [dict(c, last=_fmt(_day(c["last"])) if c["last"] else None, due=_fmt(c["due"]), state=_state(c["left"]))
              for c in chore_states(store, now)]
    repeating = []
    for h in _rows(store, "meta LIKE '%\"recur\"%' AND kind != 'procedure'"):
        rec = (h.get("meta") or {}).get("recur")
        if rec:
            repeating.append({"id": h["id"], "title": h["title"], "next": _fmt(next_time(rec, now))})
    cur = _places(store)
    places = []
    for thing in sorted(cur):
        chain, x, seen = [], thing, set()
        while x in cur and x not in seen and len(chain) < 6:
            seen.add(x)
            chain.append(cur[x][0])
            x = cur[x][0]
        places.append({"thing": thing, "chain": chain, "id": cur[thing][2], "since": _fmt(_day(cur[thing][1]))})
    typed = [{"id": h["id"], "title": h["title"], "type": h["meta"].get("type"), "fields": h["meta"]["fields"]}
             for h in _rows(store, "meta LIKE '%\"fields\"%'") if (h.get("meta") or {}).get("fields")]
    return {"profile": profile(store), "chores": chores, "repeating": sorted(repeating, key=lambda r: r["title"]),
            "places": places, "typed": typed, "today": _fmt(today)}


# ---- what the patterns miss: a model reads it (overnight, option overnight_structure; off until it proves itself) --
HINT = re.compile(r"\b(every|each|weekly|monthly|daily|yearly|bin day|day is|inside|lives in|in the|on the|under|"
                  r"behind|keep|serial|model|warranty|bought|married|my \w+|ago|last \w+day|yesterday|swapp?ed|"
                  r"changed|replaced|cleaned)\b", re.I)
MODEL_SYSTEM = (
    "You read one short note from a personal memory and pull out structure. Reply with JSON only, using only words that "
    "appear in the note (copy them exactly), and null for anything the note does not say: "
    '{"place": {"thing": ..., "in": ...} (where a thing is kept or was put), '
    '"repeats": {"weekday": "monday".."sunday" or null, "every_weeks": 1 or 2, "day_of_month": 1-31 or null, '
    '"every_days": number or null} (something that happens on a schedule), '
    '"chore": {"verb": ..., "thing": ..., "every_days": number} (a task to do on a schedule), '
    '"fields": {"model", "serial", "bought", "warranty_until", "phone", "birthday"} (only those stated), '
    '"alias": {"role": e.g. partner, sister, dentist, "name": ...} (who is who to the writer), '
    '"days_ago": number or null (if the note says the change happened that many days before it was written)}.')
FIELD_KEYS = ("model", "serial", "bought", "warranty_until", "phone", "birthday")
ROLES = set(ROLE.strip("()").split("|"))


def _in(x, text):
    return isinstance(x, str) and 0 < len(x) <= 60 and x.lower().strip() in (text or "").lower()


def from_model(reply, text, now_ts):
    """The model's JSON, kept only where it holds up: every name, thing and value must be copied from the note (no
    made-up places or numbers), numbers in range, roles from the known list. Returns meta to add, maybe empty."""
    import json
    m = re.search(r"\{.*\}", reply or "", re.S)
    try:
        j = json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}
    if not isinstance(j, dict):
        return {}
    out = {}
    p = j.get("place")
    if isinstance(p, dict) and _in(p.get("thing"), text) and _in(p.get("in"), text) and \
            p["thing"].lower().strip() != p["in"].lower().strip():
        clean = lambda x: re.sub(rf"^{ART}", "", x.lower().strip(), flags=re.I)
        out["place"] = {"thing": clean(p["thing"]), "in": clean(p["in"])}
    c = j.get("chore")
    if isinstance(c, dict) and _in(c.get("verb"), text) and _in(c.get("thing"), text) and \
            isinstance(c.get("every_days"), (int, float)) and 1 <= c["every_days"] <= 3650:
        verb = c["verb"].lower().split()[0]
        out["chore"] = {"verb": verb, "thing": re.sub(rf"^{ART}", "", c["thing"].lower().strip(), flags=re.I),
                        "every_days": int(c["every_days"])}
    r = j.get("repeats")
    if "chore" not in out and isinstance(r, dict):
        day = _day(now_ts)
        wd = (r.get("weekday") or "").lower() if isinstance(r.get("weekday"), str) else ""
        if wd in WEEKDAYS and wd in text.lower():
            ahead = (WEEKDAYS.index(wd) - day.weekday()) % 7
            out["recur"] = {"kind": "weekly", "dow": WEEKDAYS.index(wd), "every": 2 if r.get("every_weeks") == 2 else 1,
                            "anchor": (day + timedelta(days=ahead)).timestamp()}
        elif isinstance(r.get("day_of_month"), int) and 1 <= r["day_of_month"] <= 31 and str(r["day_of_month"]) in text:
            out["recur"] = {"kind": "monthly", "dom": r["day_of_month"]}
        elif isinstance(r.get("every_days"), (int, float)) and 1 <= r["every_days"] <= 3650:
            out["recur"] = {"kind": "interval", "days": int(r["every_days"]), "anchor": day.timestamp()}
    f = j.get("fields")
    if isinstance(f, dict):
        keep = {}
        for k in FIELD_KEYS:
            v = f.get(k)
            if not _in(v, text):
                continue
            if k in ("bought", "warranty_until"):
                d = parse_date(v, now_ts, future=k == "warranty_until")
                if not d:
                    continue
                v = d.isoformat()
            keep[k] = v.strip()
        if keep:
            out["fields"] = keep
            out["type"] = "appliance" if {"model", "serial", "warranty_until"} & set(keep) else "person"
    a = j.get("alias")
    if isinstance(a, dict) and isinstance(a.get("role"), str) and a["role"].lower() in ROLES and \
            _in(a.get("name"), text) and a["name"][:1].isupper():
        out["alias"] = {"role": a["role"].lower(), "name": a["name"].strip()}
    k = j.get("days_ago")
    if isinstance(k, (int, float)) and 1 <= k <= 60 and re.search(r"\b(ago|last|yesterday|back|earlier|before)\b",
                                                                   text, re.I):
        out["valid_from"] = (_day(now_ts) - timedelta(days=int(k)) + timedelta(hours=12)).timestamp()
    if out:
        out["structure_by"] = "model"
    return out


async def ask_model(model, text, now_ts):
    r = await model.chat(MODEL_SYSTEM, [{"role": "user", "content": text}], max_tokens=300, temperature=0)
    return from_model(r.get("text"), text, now_ts)


TASKLIKE = re.compile(r"\b(should|needs?|have to|has to|must|remember to|supposed to)\b", re.I)


def missed(meta, text):
    """A note worth showing the model: the patterns found nothing and a word hints at structure, or they found a
    schedule but no chore in what reads like a task ("the filter should be swapped out every three months")."""
    m = meta or {}
    if "recur" in m and "chore" not in m and TASKLIKE.search(text or ""):
        return True
    return not any(k in m for k in ("valid_from", "recur", "chore", "place", "fields", "alias")) and \
        bool(HINT.search(text or ""))
