"""Undo, why, and "help me do it" (design spec step 6), answered from the decision log.

  undo    "undo", "undo that", "put it back", "change it back": the last action asked for in the last 10 minutes
          (this conversation's first), put back as it was, brightness and speed included. Saying it again steps
          back one more. It goes through the same checks as any action: undoing an unlock still needs the phone.
  why     "why did you turn on the hall light?", "why is the fan on?": one sentence from the log (who asked, or which
          automation, and when). Someone else's request is "a household rule" to anyone but them.
  help    "help me ...", "walk me through ...": the steps, one at a time, and nothing is done for the person.
"""
import re
import time
from datetime import datetime

UNDO = re.compile(r"^\s*(?:(?:oops|no|wait|actually|never ?mind)[,.!]?\s+)?(?:please\s+)?(?:undo(?: that| it| this)?|"
                  r"put it back|change it back|go back|reverse that|take that back)(?:\s+please)?\s*[.!]?\s*$", re.I)
WHY = re.compile(r"^\s*why\s+(?:did you|did it|did|is|are|was|were|has|have|'s)\b\s*(.*?)\s*\??\s*$", re.I)
HELP = re.compile(r"^\s*(?:can you\s+|could you\s+|please\s+)?(?:help me(?: to)?|walk me through(?: how to)?|"
                  r"show me how to|talk me through(?: how to)?)\s+(.+?)\s*\??\s*$", re.I)
HELP_PROMPT = ("Walk me through this one step at a time. Number the steps, keep each step to one short sentence, "
               "and don't do anything for me: ")


def _when(ts):
    return datetime.fromtimestamp(ts).strftime("%I:%M %p").lstrip("0").lower().replace(" am", " am").replace(" pm", " pm")


def inverse(d):
    """The action that puts a decision's device back as it was: (action, value) or None."""
    eid, prior = d.get("entity_id") or "", d.get("prior")
    a = d.get("prior_attrs") or {}
    dom = eid.split(".")[0]
    if prior in (None, "", "unknown", "unavailable"):
        return None
    if dom == "light":
        if prior == "off":
            return "off", None
        if a.get("brightness") is not None:
            return "brightness", max(1, round(float(a["brightness"]) * 100 / 255))
        return "on", None
    if dom == "fan":
        if prior == "off":
            return "off", None
        if a.get("percentage") is not None:
            return "speed", a["percentage"]
        return "on", None
    if dom == "humidifier":
        if prior == "off":
            return "off", None
        if a.get("humidity") is not None and d.get("action") == "humidity":
            return "humidity", a["humidity"]
        return "on", None
    if dom in ("switch", "input_boolean", "media_player"):
        return ("on", None) if prior == "on" else ("off", None) if prior in ("off", "standby") else None
    if dom == "cover":
        return ("open", None) if prior in ("open", "opening") else ("close", None) if prior in ("closed", "closing") \
            else None
    if dom == "lock":
        return ("lock", None) if prior in ("locked", "locking") else ("unlock", None) if prior in ("unlocked",) \
            else None
    return None


def _back(dom, action, value):
    if action in ("off", "close", "lock"):
        return {"off": "off", "close": "closed", "lock": "locked"}[action]
    if action in ("open", "unlock"):
        return {"open": "open", "unlock": "unlocked"}[action]
    if action == "brightness":
        return f"at {value}%"
    if action == "speed":
        return f"at speed {value}%"
    return "on"


async def undo(app, conversation_id=None, source="voice", why=None):
    """Undo the newest undoable action. Returns the reply."""
    dlog = getattr(app, "decisions", None)
    if dlog is None:
        return {"answer": "I can't undo things right now: my record of what I did isn't running."}
    d = dlog.undoable(conversation_id=conversation_id)
    if not d:
        return {"answer": "There's nothing from the last 10 minutes for me to undo.", "undo": None}
    inv = inverse(d)
    name = (app.home.catalog.get(d["entity_id"]) or {}).get("name") or d["entity_id"]
    if not inv:
        dlog.mark_undone(d["id"], 0)        # so "undo" again steps past it
        return {"answer": f"I can't put the {name} back as it was, so I left it.", "undo": d["id"]}
    action, value = inv
    w = dict(why or {}, request=f"undo {d['id']}", conversation_id=conversation_id)
    act = getattr(app, "act_checked", None) or app.home.act
    try:
        r = await act(d["entity_id"], action, value, source, why=w)
    except (ValueError, PermissionError) as e:
        return {"answer": f"I couldn't undo that: {e}", "undo": d["id"], "error": str(e)}
    if r.get("approval_sent"):
        dlog.mark_undone(d["id"], -1)
        return dict(r, undo=d["id"])
    dlog.mark_undone(d["id"], r.get("decision_id") or -1)
    return {"answer": f"Undone. {name[:1].upper() + name[1:]} back {_back(d['entity_id'].split('.')[0], action, value)}.",
            "undo": d["id"], "done": r}


def why(app, text, viewer=None):
    """One sentence: why a device is the way it is, from the decision log. None if the words aren't a why."""
    m = WHY.match(text or "")
    if not m:
        return None
    dlog = getattr(app, "decisions", None)
    if dlog is None:
        return None
    rest = m.group(1)
    hits = app.home.matching(rest, 1) if rest else []
    eid = hits[0] if hits else None
    asks_me = bool(re.search(r"\byou\b", text, re.I))
    d = dlog.last_for(eid) if eid else (dlog.recent(1, status="done") or [None])[0] if asks_me else None
    if not asks_me and (not d or dlog.clock() - d["ts"] > 86400):
        return None         # "why is the humidity high?": a question for the brain, not about something I did
    if not d:
        if eid:
            name = (app.home.catalog.get(eid) or {}).get("name") or eid
            return {"answer": f"I didn't change the {name} recently; someone or something else did.", "why": None}
        return {"answer": "I haven't changed anything recently.", "why": None}
    name = (app.home.catalog.get(d["entity_id"]) or {}).get("name") or d["entity_id"]
    at = _when(d["ts"])
    rec = (d.get("records") or [{}])[0] if d.get("records") else {}
    if d.get("channel") == "automation":
        said = f"My automation \"{rec.get('name') or d.get('source')}\" changed the {name} at {at}."
        tail = " Want me to stop it doing that? You can switch it off in Rules."
    elif viewer is not None and d.get("person") and d["person"] != viewer:
        said, tail = f"A household rule changed the {name} at {at}.", ""
    elif (d.get("request") or "").startswith("undo"):
        said, tail = f"You asked me to undo a change to the {name} at {at}.", ""
    elif d.get("request"):
        how = {"voice": " by voice", "telegram": " on Telegram", "claude": " through Claude", "panel": " on the panel"
               }.get(d.get("channel") or "", "")
        said = f"You asked me to{how} at {at}: \"{d['request'][:80]}\"."
        tail = " Say \"undo\" to put it back." if dlog.clock() - d["ts"] < 600 and not d.get("undone_by") else ""
    else:
        said, tail = f"I changed the {name} at {at}.", ""
    return {"answer": said + tail, "why": d["id"]}


def help_request(text):
    """The task in "help me ..." / "walk me through ...", or None."""
    m = HELP.match(text or "")
    return m.group(1) if m else None
