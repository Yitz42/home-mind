"""Automations from a request, checked before anything is installed (#42).

  draft(request)   a model writes the automation as JSON (Home Assistant's own automation format), given the house's
                   entities and services, and nothing else
  check(config)    every entity ID must exist, every action must be a service Home Assistant has, the triggers,
                   conditions and actions must be ones Home Assistant accepts (unknown keys are refused), and actions
                   that open the house (unlock, open a garage door or a cover, disarm an alarm) need the PIN set in
                   option automation_pin; with no PIN set they are refused
  install          only a config that passes check: created in Home Assistant, switched off unless asked otherwise

The model only proposes. Rules decide whether it is installed.
"""
import json
import re


def _list(x):
    return [] if x is None else x if isinstance(x, list) else [x]

DRAFT_SYSTEM = (
    "You write one Home Assistant automation for a request. Reply with JSON only, no prose: an object with alias, "
    "description, triggers, conditions, actions and mode, in Home Assistant's format (triggers like "
    '{"trigger": "time", "at": "22:30:00"} or {"trigger": "state", "entity_id": "...", "to": "on"}; actions like '
    '{"action": "light.turn_off", "target": {"entity_id": "..."}}). Use only entity IDs and services from the lists '
    "given. If the request can't be done with them, reply {\"error\": \"why\"}.")

TRIGGER_KINDS = {"state", "numeric_state", "time", "time_pattern", "sun", "event", "template", "zone", "homeassistant"}
CONDITION_KINDS = {"state", "numeric_state", "time", "template", "trigger", "and", "or", "not", "sun", "zone"}
ACTION_KEYS = {"action", "service", "delay", "choose", "if", "repeat", "variables", "wait_for_trigger", "stop", "event",
               "parallel", "sequence"}
# actions that open the house: allowed only with the PIN
OPENING = [(re.compile(r"^lock\.(unlock|open)$"), None), (re.compile(r"^alarm_control_panel\.alarm_disarm$"), None),
           (re.compile(r"^cover\.(open_cover|toggle|set_cover_position)$"), re.compile(r"garage|gate|door", re.I)),
           (re.compile(r"^button\.press$"), re.compile(r"garage|gate|door|unlock", re.I))]
# services that act on whatever entities they are given: they open the house when given a lock, an alarm or a garage
# door (homeassistant.turn_on opens a cover; scene.apply sets a lock to unlocked)
ANY_DOMAIN = re.compile(r"^(?:homeassistant\.(?:turn_on|toggle)|scene\.apply)$")
DOOR = re.compile(r"garage|gate|door", re.I)
# scripts, automations, relays and buttons that open the house by what they're called ("script.open_garage" run
# directly or through script.turn_on, automation.trigger on "Unlock front door", switch.garage_opener)
RUNS = re.compile(r"^(?:script\.\w+|automation\.trigger|switch\.(?:turn_on|toggle)|(?:input_)?button\.press)$")
OPENER = re.compile(r"garage|gate|door|unlock|disarm|opener", re.I)
HIDDEN_TARGETS = ("area_id", "device_id", "floor_id", "label_id")
ENTITY_RX = re.compile(r"\b([a-z_]+\.[a-z0-9_]+)\b")


def _entities_in(x, out):
    """Entity IDs anywhere in a config (entity_id fields, target lists and scene.apply's entities), not inside
    templates."""
    if isinstance(x, dict):
        for k, v in x.items():
            if k in ("entity_id", "zone"):
                for e in _list(v):
                    if isinstance(e, str) and "{{" not in e:
                        out.add(e)
            elif k == "entities" and isinstance(v, dict):
                out.update(e for e in v if isinstance(e, str) and "{{" not in e)
            elif k not in ("value_template", "data_template"):
                _entities_in(v, out)
    elif isinstance(x, list):
        for v in x:
            _entities_in(v, out)
    return out


def _hidden_targets(x):
    """Does an action reach entities the check can't see (an area, a device, a label, a template, "all")?"""
    if isinstance(x, dict):
        for k, v in x.items():
            if k in HIDDEN_TARGETS and v:
                return True
            if k == "entity_id" and any(not isinstance(e, str) or "{{" in e or e == "all" for e in _list(v)):
                return True
            if k == "entities" and v is not None and (not isinstance(v, dict) or any("{{" in str(e) for e in v)):
                return True
            if _hidden_targets(v):
                return True
    elif isinstance(x, list):
        return any(_hidden_targets(v) for v in x)
    return False


# keys of an action that hold data, conditions or triggers, never more actions
NOT_ACTIONS = {"action", "service", "data", "data_template", "target", "variables", "triggers", "trigger",
               "wait_for_trigger", "conditions", "condition", "if", "while", "until", "for_each", "event_data",
               "response_variable"}


def _actions(acts, out):
    """Every action in a list, however deep it is nested (if/then/else, choose, repeat, parallel, sequence, ...): an
    action Home Assistant runs from a block this doesn't know must still be checked."""
    for a in _list(acts):
        if not isinstance(a, dict):
            out.append(("?", a))
            continue
        if "action" in a or "service" in a:
            out.append((a.get("action") or a.get("service"), a))
        for k, v in a.items():
            if k not in NOT_ACTIONS and isinstance(v, (list, dict)):
                _actions(v, out)
    return out


def check(cfg, entities, services, pin="", given_pin="", names=None):
    """Problems with a drafted automation (empty list: it may be installed). entities: set of entity IDs; services:
    {"light": {"turn_on", ...}, ...} as Home Assistant lists them; names: entity -> friendly name (for the PIN rule)."""
    names = names or {}
    p = []
    if not isinstance(cfg, dict):
        return ["not an automation"]
    if cfg.get("error"):
        return [f"the model couldn't: {cfg['error']}"]
    trig = _list(cfg.get("triggers") or cfg.get("trigger"))
    acts = _list(cfg.get("actions") or cfg.get("action"))
    if not trig:
        p.append("no trigger")
    if not acts:
        p.append("no action")
    for t in trig:
        k = t.get("trigger", t.get("platform")) if isinstance(t, dict) else None
        if k not in TRIGGER_KINDS:
            p.append(f"trigger type {k!r} isn't one Home Assistant has here")
    for c in _list(cfg.get("conditions") or cfg.get("condition")):
        k = c.get("condition") if isinstance(c, dict) else ("template" if isinstance(c, str) else None)
        if k not in CONDITION_KINDS:
            p.append(f"condition type {k!r} isn't one Home Assistant has")
    for a in acts:
        if isinstance(a, dict) and not (set(a) & ACTION_KEYS):
            p.append(f"an action with no action in it: {json.dumps(a)[:80]}")
    for e in sorted(_entities_in(cfg, set())):
        if e not in entities:
            p.append(f"no such entity: {e}")
    if cfg.get("mode") not in (None, "single", "restart", "queued", "parallel"):
        p.append(f"mode {cfg.get('mode')!r}")
    for svc, a in _actions(acts, []):
        if not isinstance(svc, str) or "." not in svc:
            p.append(f"odd action {svc!r}")
            continue
        dom, name = svc.split(".", 1)
        if name not in services.get(dom, ()):
            p.append(f"no such service: {svc}")
            continue
        targets = _entities_in(a, set())
        # an area, a device or a template may hold the garage door: the PIN rule can't be dodged by not naming it
        hidden = _hidden_targets(a) or not targets
        opens = any(rx.match(svc) and (needs is None or hidden or any(needs.search(t + " " + names.get(t, ""))
                                                                      for t in targets))
                    for rx, needs in OPENING)
        if not opens and ANY_DOMAIN.match(svc):
            opens = hidden or any(t.split(".")[0] in ("lock", "alarm_control_panel") or
                                  (t.startswith("cover.") and DOOR.search(t + " " + names.get(t, ""))) for t in targets)
        if not opens and RUNS.match(svc):
            if dom == "script" and name not in ("turn_on", "toggle", "reload", "turn_off"):
                targets = targets | {svc}           # script.open_garage: the service is the script
                hidden = _hidden_targets(a)
            opens = (hidden and dom in ("script", "automation")) or \
                any(OPENER.search(t + " " + names.get(t, "")) for t in targets)
        if opens:
            if not pin:
                p.append(f"{svc} opens the house: set option automation_pin to allow it")
            elif given_pin != pin:
                p.append(f"{svc} opens the house: give the PIN")
    return p


def parse(text):
    """The model's JSON, from a reply that may wrap it in prose or a code block."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def house_lists(states, services_raw):
    """(entities set, services dict, names dict, a compact text list for the model) from /api/states and
    /api/services."""
    ents = {s["entity_id"] for s in states or []}
    names = {s["entity_id"]: (s.get("attributes") or {}).get("friendly_name") or s["entity_id"] for s in states or []}
    svcs = {d["domain"]: set((d.get("services") or {}).keys()) for d in services_raw or []}
    keep = [e for e in sorted(ents) if e.split(".")[0] in ("light", "switch", "fan", "cover", "lock", "climate",
                                                           "media_player", "scene", "script", "input_boolean",
                                                           "binary_sensor", "sensor", "person", "humidifier",
                                                           "vacuum", "alarm_control_panel", "sun", "zone")]
    text = "\n".join(f"{e} ({names[e]})" for e in keep[:400])
    doms = sorted({e.split(".")[0] for e in keep} | {"notify", "automation", "homeassistant"})
    svc_text = "\n".join(f"{d}: {', '.join(sorted(svcs.get(d, ())))}" for d in doms if svcs.get(d))
    return ents, svcs, names, text, svc_text


async def draft(model, request, states, services_raw, pin=""):
    """Ask the model for an automation; returns {"config", "problems", "ms"}."""
    ents, svcs, names, ent_text, svc_text = house_lists(states, services_raw)
    r = await model.chat(DRAFT_SYSTEM, [{"role": "user", "content": f"Entities:\n{ent_text}\n\nServices:\n{svc_text}"
                                                                  f"\n\nRequest: {request}"}],
                         max_tokens=700, temperature=0)
    cfg = parse(r.get("text"))
    return {"config": cfg, "problems": check(cfg, ents, svcs, pin=pin, names=names) if cfg else ["the model's reply wasn't JSON"],
            "ms": r.get("ms")}


def slug(alias):
    return "home_mind_" + (re.sub(r"[^a-z0-9]+", "_", (alias or "automation").lower()).strip("_")[:50] or "automation")


# ---- Ask / Talk routing & plain-English formatting ---------------------------------------------------------------
NOT_AUTOMATION_REQ_Q = re.compile(
    r"^\s*(?:what|which|how many|why|did|when|list|show|delete|remove|disable|enable|turn (?:on|off)|run|trigger)\b"
    r".{0,40}\bautomations?\b",
    re.I,
)
AUTOMATION_REQ_Q = re.compile(
    r"\b(?:set ?up|create|make|build|add|write|draft|new)\b.{0,30}\b(?:an?\s+)?(?:home assistant\s+)?automation\b|"
    r"^\s*(?:please\s+|can you\s+|could you\s+|would you\s+)?automate\b.{4,}",
    re.I,
)


def automation_request(question):
    """Return the cleaned automation request string if `question` asks to create/set up an automation, else None."""
    q = (question or "").strip()
    if not q or len(q) > 400 or NOT_AUTOMATION_REQ_Q.search(q):
        return None
    if not AUTOMATION_REQ_Q.search(q):
        return None
    return q


def _fmt_trigger(t):
    if not isinstance(t, dict):
        return str(t)
    kind = t.get("trigger") or t.get("platform") or ""
    if kind == "time" and t.get("at"):
        return f"at {t['at']}"
    if kind == "sun":
        ev = t.get("event") or "sunset"
        off = f" ({t['offset']})" if t.get("offset") else ""
        return f"at {ev}{off}"
    if kind == "state":
        ent = ", ".join(_list(t.get("entity_id")))
        to = f" -> {t['to']}" if t.get("to") is not None else ""
        for_d = f" for {t['for']}" if t.get("for") else ""
        return f"{ent}{to}{for_d}".strip()
    if kind == "zone":
        ent = ", ".join(_list(t.get("entity_id")))
        ev = t.get("event") or "leave"
        zn = t.get("zone") or ""
        return f"{ent} {ev}s {zn}".strip()
    if kind == "numeric_state":
        ent = ", ".join(_list(t.get("entity_id")))
        parts = [f"above {t['above']}" if t.get("above") is not None else "",
                 f"below {t['below']}" if t.get("below") is not None else ""]
        return f"{ent} {' '.join(p for p in parts if p)}".strip()
    return kind or "trigger"


def _fmt_action(a):
    if not isinstance(a, dict):
        return str(a)
    svc = a.get("action") or a.get("service") or ""
    ents = sorted(_entities_in(a, set()))
    if svc and ents:
        return f"{svc} ({', '.join(ents)})"
    return svc or "action"


def draft_summary(res):
    """Build compact UI metadata for an automation draft (config, problems, needs_pin, installable, alias)."""
    cfg = res.get("config") if isinstance(res, dict) else None
    problems = list((res or {}).get("problems") or ())
    pin_only = [p for p in problems if "opens the house: give the PIN" in str(p)]
    hard_problems = [p for p in problems if p not in pin_only]
    needs_pin = bool(pin_only) or any("opens the house" in str(p) for p in problems)
    installable = bool(isinstance(cfg, dict) and not cfg.get("error") and not hard_problems)
    return {
        "config": cfg,
        "alias": (cfg.get("alias") if isinstance(cfg, dict) else None) or "New automation",
        "problems": problems,
        "needs_pin": needs_pin,
        "installable": installable,
    }


def format_draft(res, voice=False):
    """Human-readable summary of a drafted automation for Ask / Talk / Voice."""
    info = draft_summary(res)
    cfg = info["config"]
    problems = info["problems"]
    if not isinstance(cfg, dict) or cfg.get("error"):
        err = (cfg or {}).get("error") if isinstance(cfg, dict) else None
        why = err or ("; ".join(problems) if problems else "couldn't draft a valid automation")
        return f"I couldn't draft that automation yet: {why}"

    alias = info["alias"]
    trigs = [_fmt_trigger(t) for t in _list(cfg.get("triggers") or cfg.get("trigger"))]
    acts = [_fmt_action(a) for a in _list(cfg.get("actions") or cfg.get("action"))]
    if voice:
        t_txt = ", ".join(trigs[:2]) or "triggered"
        a_txt = ", ".join(acts[:3]) or "runs actions"
        if info["installable"]:
            return f"I drafted {alias}: when {t_txt}, it runs {a_txt}. Tap Install switched off in the app to add it."
        return f"I drafted {alias}, but it still needs a fix: {problems[0]}."

    lines = [f"Drafted automation: {alias}"]
    if trigs:
        lines.append("When: " + "; ".join(trigs))
    if acts:
        lines.append("Then: " + "; ".join(acts))
    if info["installable"]:
        if info["needs_pin"]:
            lines.append("Ready to install switched off (PIN required because it unlocks/opens the house).")
        else:
            lines.append("Checked against your Home Assistant entities — click Install switched off below to add it.")
    elif problems:
        lines.append("Needs a fix before installing: " + "; ".join(problems))
    return "\n".join(lines)
