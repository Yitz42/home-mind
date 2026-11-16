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


async def draft(model, request, states, services_raw):
    """Ask the model for an automation; returns {"config", "problems", "ms"}."""
    ents, svcs, names, ent_text, svc_text = house_lists(states, services_raw)
    r = await model.chat(DRAFT_SYSTEM, [{"role": "user", "content": f"Entities:\n{ent_text}\n\nServices:\n{svc_text}"
                                                                  f"\n\nRequest: {request}"}],
                         max_tokens=700, temperature=0)
    cfg = parse(r.get("text"))
    return {"config": cfg, "problems": check(cfg, ents, svcs, names=names) if cfg else ["the model's reply wasn't JSON"],
            "ms": r.get("ms")}


def slug(alias):
    return "home_mind_" + (re.sub(r"[^a-z0-9]+", "_", (alias or "automation").lower()).strip("_")[:50] or "automation")
