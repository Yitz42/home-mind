"""House commands without a model: "turn off the kitchen light", "den lamp to 40%", "dim the den lamp a bit", "lights
off and lock up", "bedtime scene", "all the kitchen lights off but leave the counter on".

plan(text, catalog, allowed) -> a list of steps, or None when the words aren't a plain command it can read with
certainty (then the local control model or the general model decides). A step is either an action for act()
({"entity_id", "action", "value"}) or a question back ({"which": [names], "device"}). Nothing here runs anything:
App.do sends every step through the same checks as a model's plan (control list, locks and garage doors need a yes).
"""
import re

from .localctl import resolve, _words

COLORS = ("warm white", "cool white", "daylight", "red", "orange", "yellow", "green", "blue", "purple", "pink", "white",
          "cyan", "magenta", "teal", "lavender")
POLITE = re.compile(r"^(?:(?:hey|ok|okay|please|so|um|uh)\b[\s,]*)+|^(?:can|could|would|will) you (?:please )?|"
                    r"\b(?:please|for me|right now|now|thanks|thank you)\b|[.!?]+$", re.I)
SPLIT = re.compile(r"\s*(?:,\s*(?:and\s+)?then\b|\band then\b|\bthen\b|;|,\s*and\b|\band also\b|\balso\b|\band\b|,)\s*",
                   re.I)
EXCEPT = re.compile(r"\s*,?\s*(?:but|except(?: for)?)\s+(?:leave|keep)?\s*(?:the\s+)?(.+?)(?:\s+(?:on|off|alone|as (?:it|they) "
                    r"(?:is|are)))?\s*$", re.I)
PLURAL = re.compile(r"\b(?:lights|lamps|switches|fans|blinds|shades|curtains|all|every|everything)\b", re.I)
NOUN_DOMAINS = {"lights": {"light"}, "lamps": {"light"}, "light": {"light"}, "lamp": {"light"},
                "switches": {"switch"}, "fans": {"fan"}, "fan": {"fan"},
                "blinds": {"cover"}, "shades": {"cover"}, "curtains": {"cover"}}
LATER = re.compile(r"\b(?:in (?:an?|one|two|three|\d+|a few|half an) (?:hour|minute|min|sec)|at \d|at (?:noon|midnight)|"
                   r"tonight|tomorrow|later|when\b|until|after|before|every|for \d+ (?:minutes|hours)|o'?clock|\d ?(?:am|pm))",
                   re.I)   # "lights off in an hour", "lock the door at 10pm": a schedule, not a command for now
QUESTION = re.compile(r"^(?:what|where|when|who|which|why|how|is|are|was|were|do|does|did|has|have|should)\b", re.I)
REL_STEP = 25        # "a bit" brighter or dimmer, in percent points

# (pattern, tool, how the groups are read). Order matters: specific before general.
RULES = [
    (r"^(un)?lock(?: up)?(?: the)?\s+(?P<d>.+)$", "lock", None),
    (r"^(?:open|raise)(?: up)?(?: the)?\s+(?P<d>.+)$", "open", None),
    (r"^(?:close|shut|lower)(?: the)?\s+(?P<d>.+)$", "close", None),
    (r"^(?:run|start|activate|trigger)(?: the)?\s+(?P<d>.+?)(?:\s+(?:scene|script|routine|mode))?$", "run", None),
    (r"^(?:the\s+)?(?P<d>.+?)\s+(?:scene|routine|mode)$", "run", None),
    (r"^(?:set|turn|put|bring|make|change|dim|brighten|adjust)?(?: the)?\s*(?P<d>.+?)\s+(?:brightness\s+)?(?:to|at)\s+"
     r"(?P<n>\d{1,3})\s*(?:%|percent)?$", "level", None),
    (r"^(?:the\s+)?(?P<d>.+?)\s+(?P<n>\d{1,3})\s*(?:%|percent)$", "level", None),
    (r"^(?P<dir>dim|brighten|turn down|turn up|lower|raise)(?: the)?\s+(?P<d>.+?)(?:\s+(?:a (?:bit|little|touch|"
     r"tad)|some|more|a lot|down|up))?$", "relative", None),
    (r"^(?:make|turn|set|change)(?: the)?\s+(?P<d>.+?)\s+(?:to\s+|color\s+(?:to\s+)?)?(?P<c>" + "|".join(COLORS) + r")$",
     "set_color", None),
    (r"^(?:turn|switch|flip|put|shut)\s+(?P<on>on|off)(?: the)?\s+(?P<d>.+)$", "onoff", None),
    (r"^(?:turn|switch|put|shut)(?: the)?\s+(?P<d>.+?)\s+(?P<on>on|off)$", "onoff", None),
    (r"^(?:the\s+)?(?P<d>.+?)\s+(?P<on>on|off|out)$", "onoff", None),
    (r"^(?:kill|cut)(?: the)?\s+(?P<d>.+)$", "off", None),
    (r"^(?:toggle|flip)(?: the)?\s+(?P<d>.+)$", "toggle", None),
]
RULES = [(re.compile(p, re.I), t, x) for p, t, x in RULES]


def _clean(text):
    t = re.sub(r"\s+", " ", (text or "").replace("’", "'")).strip()
    for _ in range(3):
        t = POLITE.sub("", t).strip(" ,")
    return t


def _level_tool(eid):
    return {"light": "set_brightness", "fan": "set_fan_speed", "humidifier": "set_humidity"}.get(eid.split(".")[0])


def _all(device, catalog, allowed, domains):
    """'the kitchen lights', 'all the lights', 'every lamp' -> every allowed thing of those domains whose name has the
    other words. None when the words don't say 'more than one'."""
    if not PLURAL.search(device):
        return None
    nouns = [w for w in _words(device) if w in NOUN_DOMAINS]
    if nouns:                                   # "the kitchen lights": lights only, not the kettle
        domains = set(domains) & set().union(*(NOUN_DOMAINS[w] for w in nouns))
        if not domains:
            return None
    elif not set(_words(device)) - {"all", "every", "everything", "the"}:
        return None                             # "all" / "everything" alone: too broad to guess
    rest = set(_words(re.sub(PLURAL, " ", device))) - {"room"} - set(NOUN_DOMAINS)
    out = []
    for eid, c in sorted(catalog.items()):
        if not allowed(eid) or eid.split(".")[0] not in domains:
            continue
        name = set(_words(c.get("name"))) | set(_words(eid.split(".", 1)[1].replace("_", " ")))
        if rest <= name:
            out.append(eid)
    return out[:15] or None


def _one(device, catalog, allowed, tool):
    """One device for a tool: (entity_id, None) or (None, which-one step) or (None, None) when nothing fits."""
    eid, cands = resolve(device, catalog, allowed, tool)
    if eid:
        return eid, None
    if cands:
        return None, {"which": cands, "device": device}
    return None, None


def _clause(text, catalog, allowed):
    """One command -> a list of steps, or None if it isn't one this can read."""
    for rx, kind, _ in RULES:
        m = rx.match(text)
        if not m:
            continue
        d = (m.groupdict().get("d") or "").strip()
        if not d or len(d) > 60:
            continue
        if kind == "lock":
            tool = "unlock" if m.group(1) else "lock"
            if d in ("up", "everything", "the house", "house") and tool == "lock":
                locks = [e for e in sorted(catalog) if e.startswith("lock.") and allowed(e)]
                return [{"entity_id": e, "action": "lock", "value": None} for e in locks] or None
            eid, ask = _one(d, catalog, allowed, tool)
            if eid or ask:
                return [ask or {"entity_id": eid, "action": tool, "value": None}]
            continue
        if kind in ("open", "close"):
            many = _all(d, catalog, allowed, {"cover"})
            if many:
                return [{"entity_id": e, "action": kind, "value": None} for e in many
                        if not _garage(e, catalog)]          # "close all the blinds" never moves a garage door
            eid, ask = _one(d, catalog, allowed, kind)
            if eid or ask:
                return [ask or {"entity_id": eid, "action": kind, "value": None}]
            continue
        if kind == "run":
            eid, ask = _one(d, catalog, allowed, "run")
            if eid or ask:
                return [ask or {"entity_id": eid, "action": "run", "value": None}]
            continue
        if kind == "level":
            d = re.sub(r"\s+(?:down|up|speed|brightness|level|humidity)$", "", d).strip()
            n = int(m.group("n"))
            if not 0 <= n <= 100:
                return None
            many = _all(d, catalog, allowed, {"light"})
            if many:
                return [{"entity_id": e, "action": "brightness", "value": max(1, n)} for e in many]
            eid, cands = resolve(d, catalog, allowed, None)
            tool = _level_tool(eid) if eid else None
            if eid and tool:
                if n == 0:
                    return [{"entity_id": eid, "action": "off", "value": None}]
                return [{"entity_id": eid, "action": {"set_brightness": "brightness", "set_fan_speed": "speed",
                                                       "set_humidity": "humidity"}[tool], "value": n}]
            if cands:
                return [{"which": cands, "device": d}]
            continue
        if kind == "relative":
            up = m.group("dir").lower() in ("brighten", "turn up", "raise")
            eid, cands = resolve(d, catalog, allowed, None)
            dom = eid.split(".")[0] if eid else None
            if dom in ("light", "fan"):
                c = catalog[eid]
                a = c.get("attrs") or {}
                if dom == "light":
                    cur = round((a.get("brightness") or 0) / 2.55) if c.get("state") == "on" else 0
                else:
                    cur = int(a.get("percentage") or 0) if c.get("state") == "on" else 0
                new = min(100, (cur or 25) + REL_STEP) if up else max(1, (cur or 100) - REL_STEP)
                return [{"entity_id": eid, "action": "brightness" if dom == "light" else "speed", "value": new}]
            if cands:
                return [{"which": cands, "device": d}]
            continue
        if kind == "set_color":
            eid, ask = _one(d, catalog, allowed, "set_color")
            if eid or ask:
                return [ask or {"entity_id": eid, "action": "color", "value": m.group("c").lower()}]
            continue
        if kind in ("onoff", "off", "toggle"):
            act = "toggle" if kind == "toggle" else "off" if kind == "off" else \
                ("on" if m.group("on").lower() == "on" else "off")
            many = _all(d, catalog, allowed, {"light", "switch", "fan"} if act == "off" else {"light"})
            if many and act != "toggle":
                if d.lower() in ("everything", "all", "it all"):
                    return None          # "everything off" is too broad to guess: the model decides
                return [{"entity_id": e, "action": act, "value": None} for e in many]
            eid, ask = _one(d, catalog, allowed, "turn_on")
            if eid or ask:
                return [ask or {"entity_id": eid, "action": act, "value": None}]
            continue
    return None


def _garage(eid, catalog):
    c = catalog.get(eid) or {}
    return (c.get("attrs") or {}).get("device_class") in ("garage", "gate", "door") or \
        bool(re.search(r"garage|gate", eid + " " + (c.get("name") or ""), re.I))


def _bare(t, catalog, allowed):
    """Just a scene's or script's name ("goodnight", "movie night"): run it."""
    want = set(_words(t))
    for eid, c in catalog.items():
        if eid.split(".")[0] in ("script", "scene") and allowed(eid) and want and \
                want == set(_words(c.get("name"))):
            return [{"entity_id": eid, "action": "run", "value": None}]
    return None


def plan(text, catalog, allowed):
    """The whole request -> steps, or None. Every clause must be readable, or none is used (a half-understood
    request goes to a model whole)."""
    t = _clean(text)
    if not t or len(t) > 200 or QUESTION.match(t) or LATER.search(t):
        return None
    keep = []
    m = EXCEPT.search(t)
    if m:
        keep = [m.group(1)]
        t = t[:m.start()].strip()
    steps = None
    parts = [p for p in SPLIT.split(t) if p.strip()]
    if 2 <= len(parts) <= 6:
        steps = []
        for p in parts:
            s = _clause(p.strip(), catalog, allowed)
            if s is None or any("which" in x for x in s) and len(parts) > 1 and _clause(t, catalog, allowed):
                steps = None
                break
            steps += s
    if steps is None:
        steps = _clause(t, catalog, allowed)
        if steps is None:
            steps = _bare(t, catalog, allowed)
        if steps is None:
            return None
    if keep:
        drop = set()
        for k in keep:
            eid, _ = resolve(k, catalog, allowed, None)
            if not eid:
                return None          # "but leave the ... on" that names nothing: don't guess
            drop.add(eid)
        steps = [s for s in steps if s.get("entity_id") not in drop]
    seen, out = set(), []
    for s in steps:
        key = (s.get("entity_id"), s.get("action"), str(s.get("value"))) if s.get("entity_id") else ("which", s["device"])
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out or None
