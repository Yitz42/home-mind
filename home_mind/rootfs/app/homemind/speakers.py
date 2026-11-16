"""Who is speaking (#3), and who may see what (#48).

Who: from the strongest clue there is, in this order:
  signed in    the Home Assistant user (text chats, the app, an LLM tool given {{ context.user_id }})
  their device a phone or satellite listed for one person (option people: devices)
  the room     a satellite in an area only one person is listed for (option people: areas)
  only one home exactly one listed person is home (their person entity)
Otherwise the speaker is unknown. People come from option people, and from Home Assistant's person entities (their
user_id and name), so a signed-in user is recognised with no setup.

What they may see: a note is household (everyone) or personal (its owner only). With the default rule, a note a
known person files in the first person ("my", "I", "me") is personal; everything else is household. Unknown
speakers see household notes only. Calls with no speaker information at all (the panel, Claude through MCP, the API
without a speaker) see everything, as before; option unknown_caller "guest" treats them as unknown instead.
"""
import contextvars
import re

VIEWER = contextvars.ContextVar("homemind_viewer", default=None)   # None: sees all; "": unknown; else a name
FIRST_PERSON = re.compile(r"\b(?:I|I'm|I've|I'd|I'll|my|mine|me|myself)\b", re.I)


def people_from(opts_people, persons=()):
    """Option people plus HA person entities: [{"name", "users", "devices", "areas", "persons"}]."""
    out = {}

    def many(v):           # a list, or the add-on option's comma-separated text
        return [x.strip() for x in (v.split(",") if isinstance(v, str) else v or []) if str(x).strip()]
    for p in opts_people or []:
        n = (p.get("name") or "").strip()
        if n:
            out[n.lower()] = {"name": n, "users": [str(x).lower() for x in many(p.get("ha_users") or p.get("users"))],
                              "devices": [str(x) for x in many(p.get("devices"))],
                              "areas": [str(x).lower() for x in many(p.get("areas"))],
                              "persons": [str(x) for x in many(p.get("persons"))]}
    for s in persons or []:
        a = s.get("attributes") or {}
        n = (a.get("friendly_name") or s["entity_id"].split(".")[1]).strip()
        p = out.setdefault(n.lower(), {"name": n, "users": [], "devices": [], "areas": [], "persons": []})
        if s["entity_id"] not in p["persons"]:
            p["persons"].append(s["entity_id"])
        if a.get("user_id"):
            p["users"].append(str(a["user_id"]).lower())
        p["users"].append(n.lower())
        for d in a.get("device_trackers") or []:
            p["devices"].append(d)
    return list(out.values())


def resolve(people, user=None, device=None, area=None, home=None):
    """(name or None, how). home: entity_id -> state for person entities."""
    u = str(user or "").strip().lower()
    if u:
        for p in people:
            if u in p["users"] or u == p["name"].lower():
                return p["name"], "signed in"
    if device:
        for p in people:
            if str(device) in p["devices"]:
                return p["name"], "their device"
    if area:
        a = str(area).lower()
        hits = [p for p in people if a in p["areas"]]
        if len(hits) == 1:
            return hits[0]["name"], "the room"
    if home:
        here = [p for p in people if p["persons"] and any(home.get(e) == "home" for e in p["persons"])]
        if len(here) == 1 and len([p for p in people if p["persons"]]) > 1:
            return here[0]["name"], "only one home"
    return None, "unknown"


def scope_for(text, owner, rule="first_person"):
    """("personal", owner) or ("household", None) for a note a known person files."""
    if owner and rule == "first_person" and FIRST_PERSON.search(text or ""):
        return "personal", owner
    return "household", None


def own_meta(meta):
    """Meta sent with a person-bound token (#48): what it files is its own person's. An owner or said_by naming
    someone else (planting a "personal" note in another person's notes, or a value "said" by them) becomes the token's
    person."""
    v = VIEWER.get()
    if not v or not isinstance(meta, dict):
        return meta
    m = dict(meta)
    for k in ("owner", "said_by", "speaker"):
        if k in m and str(m[k] or "").lower() != v.lower():
            m[k] = v
    if m.get("scope") == "personal":
        m["owner"] = v
    return m


def visible(meta, viewer=None):
    """May this viewer see a note with this meta? (None sees everything.)"""
    viewer = VIEWER.get() if viewer is None else viewer
    if viewer is None:
        return True
    m = meta or {}
    return m.get("scope") != "personal" or (bool(viewer) and str(m.get("owner") or "").lower() == viewer.lower())


def sql(alias=""):
    """A WHERE clause (and args) for the current viewer, for queries on items."""
    v = VIEWER.get()
    if v is None:
        return "1=1", []
    col = f"{alias}meta" if alias else "meta"
    if not v:
        return f"COALESCE({col},'') NOT LIKE '%\"scope\": \"personal\"%'", []
    # the owner is compared the way visible() compares it (hm_seen, registered by Store): a LIKE on the stored JSON
    # missed owners with accents ("Zoë" is stored as "Zo\\u00eb", and SQLite's LOWER folds ASCII only), so their
    # own personal notes vanished from search while get() still showed them
    return (f"(COALESCE({col},'') NOT LIKE '%\"scope\": \"personal\"%' OR hm_seen({col}, ?))", [v])


def seen_sql(meta, viewer):
    """hm_seen(meta, viewer) for SQLite: visible() on a stored meta."""
    import json
    try:
        return 1 if visible(json.loads(meta or "{}"), viewer) else 0
    except (ValueError, TypeError, AttributeError):
        return 0
