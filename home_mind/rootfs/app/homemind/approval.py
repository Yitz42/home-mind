"""Who has to say yes before Home Mind acts, and what it may see at all (design spec step 8).

Approval levels, set in code for every action (a model or a learned record can never lower them):
  open     lights, media, fans, switches, scenes: done straight away
  confirm  a spoken yes from the person, in the same conversation (heating within normal limits)
  phone    a fresh approval on the owner's phone, behind Face ID or the phone's lock (locks, alarms, valves, garage
           doors and gates, scripts and switches that open the house, heaters and ovens, extreme temperatures)

Per action, a person can ask for more care than the code requires, never less. They see this as one ladder:
  ask      ask me first (a spoken yes; the phone where the code requires it)
  tell     tell me after (done, then a short notice)
  do       just do it (only where the code allows: the open level)

Device access, per entity: none (Home Mind can't see or touch it: it isn't in the house catalog, the model's context
or the control list), read (seen, never moved), act (may be moved, at its level). A device set by hand keeps its
setting. Otherwise devices Home Mind already knew when this came in keep what they had (act if on the control list,
else read); devices that show up later start at none if they are locks, alarms, valves, garage doors, heating,
cameras or microphones, else read (act if on the control list). Making access stricter applies at once; loosening it
waits 24 hours, and every change is announced.

Phone approval: an actionable notification to the owner's phone (Approve needs Face ID or the passcode on iPhone;
on Android, the phone has to be unlocked). Each request is single-use and expires after APPROVAL_SECONDS.
"""
import math
import re
import secrets
import time

OPEN, CONFIRM, PHONE = "open", "confirm", "phone"
RANK = {OPEN: 0, CONFIRM: 1, PHONE: 2}
LADDER = ("ask", "tell", "do")
ACCESS = ("none", "read", "act")
LOOSEN_SECONDS = 24 * 3600
APPROVAL_SECONDS = 120
HEATER = re.compile(r"heater|\boven\b|stove|\bhob\b|cooktop|space heat|radiator|fireplace", re.I)
RISKY_DOMAINS = ("lock", "alarm_control_panel", "valve", "camera", "climate", "water_heater", "assist_satellite",
                 "siren")
# a comfortable range: setpoints outside it are "extreme" and need the phone
SAFE_C = (16.0, 27.0)
SAFE_F = (61.0, 80.0)


class ApprovalNeeded(PermissionError):
    """Raised by Home.act when the action needs a yes it doesn't have. .level is CONFIRM or PHONE."""

    def __init__(self, level, message):
        super().__init__(message)
        self.level = level


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def extreme(temp, unit=None):
    v = _num(temp)
    if v is None:
        return False
    fahrenheit = unit == "°F" if unit else v > 40
    lo, hi = SAFE_F if fahrenheit else SAFE_C
    return not lo <= v <= hi


def heater(entity_id, name=""):
    return bool(HEATER.search(entity_id) or HEATER.search(name or ""))


def level(home, entity_id, action=None, value=None):
    """The code's approval level for this action (never lowered by anything a model or a record says)."""
    dom = entity_id.split(".")[0]
    c = home.catalog.get(entity_id) or {}
    if home.sensitive(entity_id):
        return PHONE
    if dom in ("climate", "water_heater"):
        unit = (c.get("attrs") or {}).get("temperature_unit") or c.get("unit")
        if (action or "").lower() in ("temperature", "set_temperature") and extreme(value, unit):
            return PHONE
        return PHONE if (action or "").lower() in ("on", "turn_on", "heat") and heater(entity_id, c.get("name")) \
            else CONFIRM
    if heater(entity_id, c.get("name")) and (action or "").lower() not in ("off", "turn_off"):
        return PHONE            # switching a heater or oven on; switching it off is always fine
    return OPEN


def required(home, entity_id, action=None, value=None, ladder=None):
    """(level needed, tell after?) once the person's ladder choice for this entity is applied on top of the code's."""
    lv = level(home, entity_id, action, value)
    pick = (ladder or {}).get(entity_id) if isinstance(ladder, dict) else ladder
    if pick == "ask" and RANK[lv] < RANK[CONFIRM]:
        lv = CONFIRM
    return lv, pick == "tell"


def ladder_of(lv, pick):
    """What the person sees for an entity: the rung in force (the code's floor wins)."""
    if lv != OPEN:
        return "ask"
    return pick if pick in LADDER else "do"


class Access:
    """Per-entity device access (none/read/act), with changes that loosen access delayed by LOOSEN_SECONDS."""

    KV, KNOWN = "device_access", "device_access_known"

    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or time.time

    def _all(self):
        return dict(self.store.kv_get(self.KV) or {})

    def known(self):
        k = self.store.kv_get(self.KNOWN)
        return None if k is None else set(k)

    def baseline(self, entity_ids):
        """The first time: every entity there is now keeps what it had before device access existed."""
        if self.store.kv_get(self.KNOWN) is None:
            self.store.kv_set(self.KNOWN, sorted(entity_ids))
            return True
        return False

    def setting(self, entity_id):
        """The hand-set access in force now (pending loosening not yet applied), or None."""
        s = self._all().get(entity_id)
        if not s:
            return None
        if s.get("pending") and s.get("effective_at", 0) <= self.clock():
            return s["pending"]
        return s.get("level")

    def effective(self, entity_id, allowed, risky):
        """allowed: on the control list; risky: a lock, garage door, heater... (for devices that are new)."""
        s = self.setting(entity_id)
        if s in ACCESS:
            return "act" if s == "act" and allowed else "read" if s == "act" else s
        known = self.known()
        if known is not None and entity_id not in known and risky:
            return "none"
        return "act" if allowed else "read"

    def set(self, entity_id, level_, by="admin"):
        """Change one device's access. Stricter: now. Looser: in 24 hours. Returns the change, for the notice."""
        if level_ not in ACCESS:
            raise ValueError(f"access is one of {', '.join(ACCESS)}")
        allx = self._all()
        cur = allx.get(entity_id) or {}
        now = self.clock()
        before = self.setting(entity_id)
        if before is None or ACCESS.index(level_) <= ACCESS.index(before):
            allx[entity_id] = {"level": level_, "set_at": now, "by": by}
            change = {"entity_id": entity_id, "to": level_, "from": before, "when": "now"}
        else:
            allx[entity_id] = {"level": cur.get("level", before), "pending": level_, "effective_at": now + LOOSEN_SECONDS,
                               "set_at": now, "by": by}
            change = {"entity_id": entity_id, "to": level_, "from": before, "when": "in 24 hours",
                      "effective_at": now + LOOSEN_SECONDS}
        self.store.kv_set(self.KV, allx)
        return change

    def table(self):
        return self._all()


class Approvals:
    """Pending phone approvals: one notification each, single use, expiring."""

    def __init__(self, clock=None):
        self.clock = clock or time.time
        self.pending = {}          # id -> {"plan", "why", "source", "t", "name", "verb"}

    def new(self, plan, why=None, source="api", name="", verb=""):
        self.prune()
        aid = secrets.token_hex(6)
        self.pending[aid] = {"plan": plan, "why": why or {}, "source": source, "t": self.clock(), "name": name,
                             "verb": verb}
        return aid

    def prune(self):
        now = self.clock()
        for k in [k for k, v in self.pending.items() if now - v["t"] > APPROVAL_SECONDS]:
            self.pending.pop(k)

    def take(self, aid):
        """The request, once: None if unknown, used or expired."""
        self.prune()
        return self.pending.pop(aid, None)

    @staticmethod
    def message(aid, name, verb, who):
        return {"title": "Home Mind needs your OK",
                "message": f"{verb.capitalize()} the {name}? Asked by {who}. This expires in 2 minutes.",
                "data": {"tag": f"hm_approval_{aid}", "push": {"interruption-level": "time-sensitive"},
                         "actions": [{"action": f"HM_APPROVE_{aid}", "title": "Approve", "authenticationRequired": True,
                                      "destructive": True},
                                     {"action": f"HM_DENY_{aid}", "title": "Deny"}]}}
