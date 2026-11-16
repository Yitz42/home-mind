"""What the house usually does, learned from the dated house notes (source "home"), with no model.

  habits (#41)        a thing changing to the same state at about the same time on most days ("Yitz usually gets
                      home around 7:30 PM on weekdays"), kept apart from coincidences by a chance test
  suggestions (#19)   habits on things Home Mind can switch, done by hand, that no automation touches yet: a drafted
                      automation, created in Home Assistant switched off, only when you ask
  baselines (#39)     each reading's usual range for each 3 hours of the day; a reading far outside it is unusual
  salience (#40)      how notable a change is: what kind of thing (locks and garage doors count most), whether that
                      state at that hour is new, whether it is a habit (routine), whether a reading is unusual

learn() builds all of it from the last DAYS days (nightly); the watcher scores each change with score() and the
evening brief lists the notable ones. Rules decide everything here; nothing asks a model.
"""
import json
import math
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

DAYS = 28
WINDOW = 60                 # minutes: a habit happens within this window
MIN_DAYS = 5                # ... on at least this many days
MIN_SHARE = 0.6             # ... and on this share of the days of its kind
CHANCE_X = 3.0              # ... and this many times more often than if the same changes fell at random times
MIN_HELD = 0.35             # ... and the window holds this share of that thing's changes to that state on those days
                            # (scanning every window finds a busy one by luck; a habit is where most of them are)
MAX_SPREAD = 25             # minutes: the typical distance from the habit's time
SWITCHABLE = ("light", "switch", "fan", "cover", "media_player", "humidifier", "input_boolean")
PRIORITY = {"lock": {"unlocked": 3, "open": 3, "locked": 1}, "alarm_control_panel": {"disarmed": 3, "triggered": 4},
            "cover": {"open": 2, "opening": 2}, "person": {"home": 1, "not_home": 1}}
NOTABLE, NOTIFY = 2, 4
DAYTYPES = {"every day": range(7), "weekdays": range(5), "weekends": (5, 6)}
# fewer days to go on means more chance of a run of luck: weekday and weekend habits must hold on more of their days
SHARE_FOR = {"every day": MIN_SHARE, "weekdays": 0.7, "weekends": 0.85}


def _ts_local(t):
    return datetime.fromtimestamp(t)


def _mins(t):
    d = _ts_local(t)
    return d.hour * 60 + d.minute


def _circ(a, b):
    d = abs(a - b) % 1440
    return min(d, 1440 - d)


def _clock(m):
    h, mm = divmod(int(round(m)) % 1440, 60)
    return datetime(2000, 1, 1, h, mm).strftime("%I:%M %p").lstrip("0")


def rows(store, since):
    """House notes since a time: entity -> [(ts, state, name, kind)] oldest first, plus the times automations and the
    brain acted (changes right after those weren't done by hand)."""
    out = defaultdict(list)
    for r in store.db.execute("SELECT created, title, meta, shelf FROM items WHERE source='home' AND created >= ? "
                              "AND shelf IN ('home/devices','home/readings') ORDER BY created", (since,)):
        m = json.loads(r["meta"] or "{}")
        eid = m.get("entity_id")
        if not eid or m.get("daily"):
            continue
        name = (r["title"] or "").split(":")[0].strip() or eid
        out[eid].append((r["created"], str(m.get("state")), name, "reading" if r["shelf"] == "home/readings" else "device"))
    acted = [r[0] for r in store.db.execute(
        "SELECT created FROM items WHERE created >= ? AND ((source='automations' AND title LIKE 'Ran %') OR "
        "shelf='home/actions')", (since,))]
    return out, sorted(acted)


def _by_hand(ts, acted):
    """Not within 3 minutes after an automation run or a brain action."""
    import bisect
    i = bisect.bisect_right(acted, ts)
    return not (i and ts - acted[i - 1] <= 180)


# ---- #41 habits -------------------------------------------------------------------------------------------------------
def habits(series, now, days=DAYS, acted=()):
    """Habits from device series: list of dicts (entity_id, name, state, daytype, minute, hits, days, by_hand)."""
    found = []
    start_day = (_ts_local(now) - timedelta(days=days)).date()
    for eid, evs in series.items():
        if not evs or evs[0][3] != "device":
            continue
        first_day = max(start_day, _ts_local(evs[0][0]).date())
        all_days = [first_day + timedelta(days=k) for k in range((_ts_local(now).date() - first_day).days)]
        by_state = defaultdict(list)
        for t, st, name, _ in evs:
            if _ts_local(t).date() >= first_day:
                by_state[st].append(t)
        for st, ts in by_state.items():
            if st in ("unavailable", "unknown") or len(ts) < MIN_DAYS:
                continue
            best = None
            for dt, wds in DAYTYPES.items():
                days_of = [d for d in all_days if d.weekday() in wds]
                if len(days_of) < MIN_DAYS:
                    continue
                mins_by_day = defaultdict(list)
                for t in ts:
                    d = _ts_local(t)
                    if d.weekday() in wds and d.date() in set(days_of):
                        mins_by_day[d.date()].append(_mins(t))
                if not mins_by_day:
                    continue
                top = (0, 0)
                for c in range(0, 1440, 5):
                    hits = sum(1 for ms in mins_by_day.values() if any(_circ(m, c) <= WINDOW / 2 for m in ms))
                    if hits > top[0]:
                        top = (hits, c)
                hits, c = top
                share = hits / len(days_of)
                # if the same number of changes fell at random times of day, how many days would have one in the window
                p = WINDOW / 1440
                expect = sum(1 - (1 - p) ** len(ms) for ms in mins_by_day.values())
                inside = [m for ms in mins_by_day.values() for m in ms if _circ(m, c) <= WINDOW / 2]
                if not inside:
                    continue
                # the habit's time: the circular mean of the changes in the window
                ang = [m / 1440 * 2 * math.pi for m in inside]
                mean = (math.atan2(sum(math.sin(a) for a in ang), sum(math.cos(a) for a in ang)) / (2 * math.pi) * 1440) % 1440
                spread = statistics.median(_circ(m, mean) for m in inside)
                held = len(inside) / sum(len(ms) for ms in mins_by_day.values())
                ok = hits >= MIN_DAYS and share >= SHARE_FOR[dt] and hits >= CHANCE_X * expect and hits - expect >= 3 and \
                    spread <= MAX_SPREAD and held >= MIN_HELD
                if not ok:
                    continue
                # prefer every day; weekdays or weekends only when that holds better
                score = (share if dt == "every day" else share - 0.1, hits)
                if not best or score > best[0]:
                    by_hand = sum(1 for t in ts if _circ(_mins(t), mean) <= WINDOW / 2 and _by_hand(t, acted))
                    best = (score, {"entity_id": eid, "name": evs[-1][2], "state": st, "daytype": dt,
                                    "minute": round(mean), "hits": hits, "days": len(days_of),
                                    "by_hand": by_hand, "in_window": len(inside)})
            if best:
                found.append(best[1])
    return found


VERB = {"home": "gets home", "not_home": "leaves home", "on": "is turned on", "off": "is turned off",
        "open": "is opened", "closed": "is closed", "locked": "is locked", "unlocked": "is unlocked",
        "playing": "starts playing", "heat": "starts heating", "cool": "starts cooling"}


def habit_text(h):
    who = h["name"]
    verb = VERB.get(h["state"], f"goes to {h['state']}")
    when = "" if h["daytype"] == "every day" else f" on {h['daytype']}"
    return f"{who} usually {verb} around {_clock(h['minute'])}{when} ({h['hits']} of the last {h['days']} days)."


# ---- #19 suggested automations --------------------------------------------------------------------------------------
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def suggestions(found, automation_configs=(), dismissed=()):
    """Drafted automations for habits done by hand on switchable things that no automation already touches."""
    used = json.dumps(list(automation_configs))
    out = []
    for h in found:
        dom = h["entity_id"].split(".")[0]
        if dom not in SWITCHABLE or h["state"] not in ("on", "off", "open", "closed"):
            continue
        if h["by_hand"] < max(MIN_DAYS, 0.7 * h["in_window"]):
            continue        # mostly done by an automation or the brain already
        if f'"{h["entity_id"]}"' in used:
            continue        # an automation already switches it
        sid = re.sub(r"[^a-z0-9]+", "_", f"{h['entity_id']}_{h['state']}_{h['daytype']}").strip("_")
        if sid in dismissed:
            continue
        svc = {"on": "turn_on", "off": "turn_off", "open": "open_cover", "closed": "close_cover"}[h["state"]]
        hh, mm = divmod(h["minute"], 60)
        cond = [] if h["daytype"] == "every day" else [{"condition": "time", "weekday": [WEEKDAYS[d] for d in DAYTYPES[h["daytype"]]]}]
        cfg = {"alias": f"{h['name']} {h['state']} at {_clock(h['minute'])}" + ("" if h["daytype"] == "every day" else f" ({h['daytype']})"),
               "description": f"Suggested by Home Mind: done by hand {h['by_hand']} times around {_clock(h['minute'])} "
                              f"in the last {h['days']} {h['daytype'].replace('every day', 'days')}.",
               "triggers": [{"trigger": "time", "at": f"{hh:02d}:{mm:02d}:00"}], "conditions": cond,
               "actions": [{"action": f"{dom if dom != 'cover' else 'cover'}.{svc}", "target": {"entity_id": h["entity_id"]}}],
               "mode": "single"}
        out.append({"id": sid, "habit": habit_text(h), "config": cfg})
    return out


# ---- #39 baselines ---------------------------------------------------------------------------------------------------
def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _q(vals, p):
    s = sorted(vals)
    k = (len(s) - 1) * p
    lo = int(k)
    return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (k - lo)


def baselines(series):
    """entity -> {"name", "all": stats, "b0".."b7": stats per 3 hours}; stats = n, med, mad, lo (2%), hi (98%)."""
    out = {}
    for eid, evs in series.items():
        if not evs or evs[0][3] != "reading":
            continue
        vals = [(t, _num(st)) for t, st, _, _ in evs]
        vals = [(t, v) for t, v in vals if v is not None]
        if len(vals) < 30:
            continue

        def stats(vs):
            med = statistics.median(vs)
            return {"n": len(vs), "med": med, "mad": statistics.median(abs(v - med) for v in vs),
                    "lo": _q(vs, 0.02), "hi": _q(vs, 0.98)}
        b = {"name": evs[-1][2], "all": stats([v for _, v in vals])}
        by = defaultdict(list)
        for t, v in vals:
            by[_ts_local(t).hour // 3].append(v)
        for k, vs in by.items():
            if len(vs) >= 15:
                b[f"b{k}"] = stats(vs)
        out[eid] = b
    return out


def unusual(base, eid, value, ts):
    """None, or (low/high, stats) when a reading is far outside its usual range for that time of day: outside the 2-98%
    range by a tenth of its width, and more than 5 typical deviations from the middle."""
    b = (base or {}).get(eid)
    v = _num(value)
    if not b or v is None:
        return None
    s = b.get(f"b{_ts_local(ts).hour // 3}") or b["all"]
    width = max(s["hi"] - s["lo"], 1e-6)
    pad = 0.1 * width
    far = abs(v - s["med"]) > 5 * max(1.4826 * s["mad"], 0.05 * width, 1e-6)
    if v > s["hi"] + pad and far:
        return "high", s
    if v < s["lo"] - pad and far:
        return "low", s
    return None


def _fmt(x):
    return f"{x:.1f}".rstrip("0").rstrip(".")


def unusual_text(name, value, unit, how, s):
    u = f" {unit}" if unit else ""
    return (f"{name} is {_fmt(float(value))}{u}, {'higher' if how == 'high' else 'lower'} than usual for this time of "
            f"day (usually {_fmt(s['lo'])}-{_fmt(s['hi'])}{u}).")


# ---- #40 salience ----------------------------------------------------------------------------------------------------
def counts(series):
    """(entity, state) -> events per 3-hour block, and entity -> total, for novelty."""
    by, tot = defaultdict(lambda: [0] * 8), defaultdict(int)
    for eid, evs in series.items():
        for t, st, _, kind in evs:
            if kind == "device":
                by[f"{eid}|{st}"][_ts_local(t).hour // 3] += 1
                tot[eid] += 1
    return {"by": dict(by), "tot": dict(tot)}


def score(model, eid, state, ts, anomaly=None):
    """(score, reasons). Notable from NOTABLE up; a phone notice from NOTIFY up; 0 or less is routine."""
    model = model or {}
    dom = eid.split(".")[0]
    s, why = PRIORITY.get(dom, {}).get(state, 0), []
    if s >= 2:
        why.append(f"{dom.replace('_', ' ')} {state}")
    c = model.get("counts") or {}
    tot = (c.get("tot") or {}).get(eid, 0)
    if anomaly:
        s += 4               # a reading far outside its usual range is worth a notice by itself (#39)
        why.append("unusual reading")
    elif tot >= 10:
        blocks = (c.get("by") or {}).get(f"{eid}|{state}")
        if not blocks:
            s += 2
            why.append("first time in this state")
        elif blocks[_ts_local(ts).hour // 3] / max(1, sum(blocks)) < 0.05 and sum(blocks) >= 5:
            s += 2
            why.append("unusual time")
    for h in model.get("habits") or []:
        if h["entity_id"] == eid and h["state"] == state and _circ(_mins(ts), h["minute"]) <= WINDOW and \
                _ts_local(ts).weekday() in DAYTYPES[h["daytype"]]:
            s -= 1
            why.append("habit")
            break
    return s, why


def learn(store, now, automation_configs=(), dismissed=()):
    """The nightly model: habits, suggestions, baselines and counts from the last DAYS days."""
    series, acted = rows(store, now - DAYS * 86400)
    hb = habits(series, now, acted=acted)
    return {"at": now, "habits": hb, "suggestions": suggestions(hb, automation_configs, dismissed),
            "baselines": baselines(series), "counts": counts(series)}


def file_habits(store, model):
    """One note per habit on home/patterns (keyed, so a changed time replaces the old one); habits no longer found are
    archived."""
    keep = set()
    for h in model.get("habits") or []:
        key = f"habit:{h['entity_id']}|{h['state']}|{h['daytype']}"
        keep.add(key)
        text = habit_text(h)
        title = re.sub(r" \(\d+ of the last \d+ days\)\.$", "", text)
        old = store.active_by_key(key)
        if old and (old["body"] == text or old["title"] == title):
            continue        # the same habit at the same time: only its day count moved, not worth a new version a night
        store.remember(text=text, title=title, kind="fact",
                       shelf="home/patterns", source="habits", key=key, meta={"habit": h})
    for r in store.db.execute("SELECT id, key FROM items WHERE status='active' AND source='habits'").fetchall():
        if r["key"] and r["key"] not in keep:
            store.set_status(r["id"], "archived")
    return len(keep)
