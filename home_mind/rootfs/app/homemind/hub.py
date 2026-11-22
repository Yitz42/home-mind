"""The Home screen (0.31): one call for everything the new Home shows around the ask bar.

  place      home / away from the person entities (None when Home Assistant has no person entity)
  logs       water, coffee and meals today (the bubbles above the bar)
  status     Energy, Fog and Readiness: today's value and seven days of each, for the folder tabs
  factors    per status and per day, what likely went with it (sleep, wakeups, HRV...), each with a short note on what
             to do and a tag (short, good...), the note first since it matters more
  needs      the For you queue: one thing at a time, only real ones (a check-in that's due, food to use today,
             notes to check, an automation idea, a door left unlocked while away)
  food       protein and calories so far against the goals, what to eat first, the shopping list
  reminders  open to-dos with a due time from Home Assistant's to-do lists (the shopping list aside)
  calendar   today's and tomorrow's events
  house      locks and the garage, for the House tile

The room, the devices and the weather come from /api/controls (home_ctrl.py), which the page loads beside this.
Every part is optional: a missing sensor, list or calendar leaves its field empty.
"""
import logging
import re
import statistics as stats
import time
from datetime import datetime, timedelta

from . import today as today_mod
from .day import today as day_of

log = logging.getLogger("homemind.hub")

ENERGY_LABEL = [None, "Low", "OK", "High"]
SHOPPING = "todo.shopping_list"


def _judge(key, v, base=None, live=False):
    """(tag, kind, note) for one factor value: kind good / ok / low (low = worth acting on)."""
    if key == "sleep_h":
        return ("short", "low", "Lights off earlier tonight to catch up." if live else "A short night.") if v < 6.5 \
            else ("good", "good", "Enough sleep.")
    if key == "wakeups":
        return ("broken", "low", "A restless night.") if v >= 3 else ("fine", "good", "Mostly slept through.")
    if key == "hrv":
        if base and v < base * 0.85:
            return ("low", "low", "Your body is strained. Go easy.")
        return ("your normal", "good", "Recovered.")
    if key == "steps_prev":
        return ("low", "low", "Mostly sitting the day before.") if v < 4000 else ("normal", "ok", "Normal movement.")
    if key == "events":
        return ("busy", "low", "A full calendar. Plan breaks.") if v >= 5 else ("light", "ok", "Room to breathe.")
    if key == "caffeine_late_prev":
        return ("late", "low", "Stop coffee by 2 PM.") if v else ("none late", "good", "No late coffee.")
    if key == "protein":
        return ("behind", "low", "A snack with protein before 2:30.") if v < 40 else ("on track", "good", "On pace.")
    if key == "water":
        return ("low", "low", "Two more glasses before 5 PM.") if v < 4 else ("ok", "ok", "Enough water so far.")
    if key == "coffee":
        return ("high", "low", "That's plenty for today.") if v > 2 else ("normal", "ok", "Fine.")
    return ("", "ok", "")


LABELS = {"sleep_h": ("Sleep", " h", 1), "wakeups": ("Wakeups", "", 0), "hrv": ("HRV", " ms", 0),
          "steps_prev": ("Steps the day before", "", 0), "events": ("Calendar events", "", 0),
          "caffeine_late_prev": ("Coffee after 2 PM, day before", "", None), "protein": ("Protein so far", " g", 0),
          "water": ("Water", " glasses", 0), "coffee": ("Coffee", " cups", 0)}
STAT_FACTORS = {"energy": ["sleep_h", "wakeups", "steps_prev", "caffeine_late_prev"],
                "fog": ["sleep_h", "events", "caffeine_late_prev", "wakeups"],
                "readiness": ["sleep_h", "hrv", "wakeups", "steps_prev"]}
EDITABLE = ("sleep_h", "wakeups", "hrv", "steps_prev")
LIVE = {"energy": ["protein", "coffee"], "fog": ["water"], "readiness": []}


def _fmt(key, v):
    label, unit, nd = LABELS[key]
    if nd is None:
        return "yes" if v else "no"
    if key == "steps_prev":
        return f"{int(v):,}"
    if key in ("water", "coffee") and round(v) == 1:
        return "1 glass" if key == "water" else "1 cup"
    return (f"{v:.{nd}f}" if nd else f"{round(v)}") + unit


def factors_for(rows, live, today):
    """{stat: [[{key, label, value, tag, kind, note}, ...] per day, oldest first]} for the seven days in rows."""
    hrvs = [r["hrv"] for r in rows if isinstance(r.get("hrv"), (int, float))]
    base = stats.mean(hrvs) if len(hrvs) >= 3 else None
    out = {}
    for s, keys in STAT_FACTORS.items():
        days = []
        for r in rows:
            is_today = r.get("day") == today
            fs = []
            for k in keys + (LIVE[s] if is_today else []):
                v = live.get(k) if k in live and is_today else r.get(k)
                if k == "sleep_h" and v is None and r.get("day"):
                    fs.append({"key": k, "label": LABELS[k][0], "value": "–", "tag": "add", "kind": "",
                               "note": "No sleep reading. Tap to add it.", "raw": None, "edit": True, "edited": False})
                    continue
                if v is None or isinstance(v, bool) and k != "caffeine_late_prev":
                    continue
                if not isinstance(v, (int, float)):
                    continue
                tag, kind, note = _judge(k, v, base, live=is_today)
                fs.append({"key": k, "label": LABELS[k][0], "value": _fmt(k, v), "tag": tag, "kind": kind,
                           "note": note, "raw": v, "edit": k in EDITABLE,
                           "edited": k in (r.get("edited") or [])})
            days.append(fs)
        out[s] = days
    return out


NUTRIENTS = (("protein", "protein_g"), ("carbs", "carbs_g"), ("fiber", "fiber_g"), ("sugar", "sugar_g"),
             ("fat", "fat_g"))


def food_totals(t):
    """Today's grams of each nutrient over the meals that have it, and how many meals have the full set."""
    meals = t.get("meals") or []
    out = {"protein": round(t.get("protein_g") or 0), "calories": round(sum(m.get("calories") or 0 for m in meals))}
    for name, key in NUTRIENTS[1:]:
        out[name] = round(sum(m.get(key) or 0 for m in meals))
    out["full"] = sum(1 for m in meals if m.get("fiber_g") is not None)
    return out


def food_goals(g, protein_target):
    """{nutrient: [goal, 'min' | 'max']}: what the person set, else defaults (fiber 30, sugar 50, the rest from calories)."""
    cal = g.get("calories") or g.get("kcal")
    out = {"protein": [protein_target, "min"] if protein_target else None,
           "calories": [cal, "max"] if cal else None,
           "fiber": [g.get("fiber_g") or 30, "min"], "sugar": [g.get("sugar_g") or 50, "max"],
           "carbs": [g.get("carbs_g") or (round(cal * 0.45 / 4) if cal else None), "max"],
           "fat": [g.get("fat_g") or (round(cal * 0.30 / 9) if cal else None), "max"]}
    return {k: v for k, v in out.items() if v and v[0]}


async def _todo(ha, entity, status="needs_action"):
    try:
        r = await ha.post_json("services/todo/get_items?return_response", {"entity_id": entity, "status": status},
                               timeout=10)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(r, dict):
        return []
    return ((r.get("service_response") or {}).get(entity) or {}).get("items") or []


async def reminders(ha, now):
    """Open to-dos with a due date or time, soonest first, from up to four to-do lists (not the shopping list)."""
    try:
        ents = [s["entity_id"] for s in (await ha.states() or []) if str(s.get("entity_id", "")).startswith("todo.")
                and s["entity_id"] != SHOPPING]
    except Exception:  # noqa: BLE001
        return []
    out = []
    for e in ents[:4]:
        for it in await _todo(ha, e):
            due = it.get("due")
            if not due:
                continue
            try:
                d = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
                t = (d.astimezone() if d.tzinfo else d).timestamp() if "T" in str(due) else \
                    datetime.fromisoformat(str(due)).replace(hour=23, minute=59).timestamp()
            except ValueError:
                continue
            if t < now - 86400 * 3 or t > now + 86400 * 2:
                continue
            out.append({"title": it.get("summary") or "", "uid": it.get("uid"), "entity": e, "due": t,
                        "timed": "T" in str(due)})
    out.sort(key=lambda x: x["due"])
    return out[:8]


async def shopping(ha):
    return [{"title": it.get("summary") or "", "uid": it.get("uid")} for it in await _todo(ha, SHOPPING)][:20]


DATED = re.compile(r"\s*\((?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day \w+ \d{1,2},? \d{4}\)\s*$")


def clean_title(t):
    """Calendar notes are filed with the date in the title ("Stop the Bleed (Saturday Nov 21 2026)"); the tiles show
    the event's name."""
    t = DATED.sub("", str(t or "")).strip()
    return "Busy" if not t or t.lower() == "(no title)" else t


def calendar(A, now):
    cal = getattr(A, "calendars", None)
    if cal is None:
        return {"today": [], "tomorrow": []}
    out = {}
    d0 = datetime.fromtimestamp(now).date()
    for name, d in (("today", d0), ("tomorrow", d0 + timedelta(days=1))):
        evs = []
        try:
            for e in cal.events_on(d):
                m = e.get("meta") or {}
                try:
                    s = datetime.fromisoformat(m["start"]).timestamp()
                    en = datetime.fromisoformat(m["end"]).timestamp() if m.get("end") else s + 3600
                except (KeyError, ValueError):
                    continue
                evs.append({"title": clean_title(e.get("title")), "start": s, "end": en,
                            "all_day": bool(m.get("all_day"))})
        except Exception:  # noqa: BLE001
            log.info("calendar for the Home screen", exc_info=True)
        out[name] = evs
    return out


def place(A):
    """home / away from the person entities, or None."""
    cat = getattr(getattr(A, "home", None), "catalog", None) or {}
    ppl = {e: (c.get("state") or "") for e, c in cat.items() if e.startswith("person.")}
    if not ppl:
        return None
    return "home" if any(v == "home" for v in ppl.values()) else "away"


def house(A):
    cat = getattr(getattr(A, "home", None), "catalog", None) or {}
    out = []
    for e, c in sorted(cat.items()):
        dom = e.split(".")[0]
        if dom == "lock" or (dom == "cover" and "garage" in e):
            st = c.get("state")
            ok = st in ("locked", "closed")
            out.append({"entity": e, "name": c.get("name") or e, "state": st, "ok": ok,
                        "kind": "lock" if dom == "lock" else "garage"})
    return {"items": out, "open": sum(1 for x in out if not x["ok"] and x["state"] not in (None, "unavailable",
                                                                                            "unknown"))}


def needs(A, t, latest, now, where, hs):
    """The For you queue, most important first. Each: kind, title, sub, a (button), b (second button or None),
    act (what the button does: checkin / ask / go / do), and its text or page."""
    out = []
    hr = datetime.fromtimestamp(now).hour
    if where == "away" and hs["open"]:
        x = next(i for i in hs["items"] if not i["ok"] and i["state"] not in (None, "unavailable", "unknown"))
        verb = "Lock" if x["kind"] == "lock" else "Close"
        out.append({"id": f"open:{x['entity']}", "kind": "House", "title": f"{x['name']} is {x['state']}",
                    "sub": "You're away. It asks your phone before it moves.", "a": verb, "act": "do",
                    "text": f"{verb.lower()} the {x['name']}"})
    if 8 <= hr < 22 and (latest is None or now - latest["at"] > 4 * 3600):
        out.append({"id": f"checkin:{day_of(now)}:{hr // 4}", "kind": "Check-in", "title": "How are you feeling?",
                    "sub": f"Last check-in at {latest['time']}." if latest else "No check-in yet today.",
                    "a": "Check in", "act": "checkin"})
    soon = [s for s in (t.get("soon") or []) if s["days"] < 1]
    if soon:
        n = soon[0]["name"]
        out.append({"id": f"use:{soon[0]['id']}", "kind": "Food", "title": f"Use the {n} today",
                    "sub": "It's at its date. Want a meal that uses it?", "a": "Meal idea", "act": "ask",
                    "text": f"What can I make with the {n}?"})
    if t.get("review"):
        n_rev = t["review"]
        out.append({"id": f"review:{day_of(now)}", "kind": "Memory",
                    "title": f"{n_rev} {'note' if n_rev == 1 else 'notes'} to check",
                    "sub": "Quick yes / no on things I wasn't sure about.", "a": "Review", "act": "go", "go": "memory"})
    if (t.get("house") or {}).get("suggestions"):
        out.append({"id": f"auto:{day_of(now)}", "kind": "Idea", "title": "An automation idea from your habits",
                    "sub": "Look it over; nothing is installed without you.", "a": "See it", "act": "go",
                    "go": "house"})
    return out


async def build(A, now=None):
    now = now or time.time()
    t = await today_mod.build(A, now)
    ck = getattr(A, "checkins", None)
    td = day_of(now)
    latest = ck.latest() if ck else None
    series = ck.series(7) if ck else []
    counts = ck.counts() if ck else {"water": 0, "coffee": 0}
    rows = []
    if getattr(A, "patterns", None):
        try:
            rows = A.patterns.rows(14)
        except Exception:  # noqa: BLE001
            rows = []
    by_day = {r.get("day"): r for r in rows}
    d0 = datetime.fromtimestamp(now).date()
    days = [s["day"] for s in series] if series else [(d0 - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    seven = [dict(by_day.get(d) or {}, day=d) for d in days]
    ser = {s["day"]: s for s in series}
    fog_s = []
    for d in days:
        f = (ser.get(d) or {}).get("fog")
        fog_s.append(f if f is not None else (by_day.get(d) or {}).get("fog"))
    en_s = [(ser.get(d) or {}).get("energy") for d in days]
    rd_s = [(by_day.get(d) or {}).get("readiness") for d in days]
    sig = ((t.get("day") or {}).get("signals") or {})
    ready = sig.get("readiness", rd_s[-1])
    if ready is not None:
        rd_s[-1] = ready
    en_now = latest.get("energy") if latest and latest.get("energy") else None
    fog_now = latest.get("fog") if latest and latest.get("fog") is not None else fog_s[-1]
    day_state = t.get("checkin")
    status = {
        "energy": {"value": {"low": "Low", "ok": "OK", "high": "High"}.get(en_now) or
                   {"good": "OK", "low": "Low", "rough": "Low"}.get(day_state) or "–",
                   "sub": f"at {latest['time']}" if latest and en_now else "", "series": en_s, "max": 3,
                   "labels": ENERGY_LABEL},
        "fog": {"value": fog_now, "sub": f"at {latest['time']}" if latest and latest.get("fog") is not None else "",
                "series": fog_s, "max": 5},
        "readiness": {"value": round(ready) if isinstance(ready, (int, float)) else None,
                      "sub": f"{sig.get('sleep_hours') or seven[-1].get('sleep_h') or 0:.1f} h sleep"
                      if (sig.get("sleep_hours") or seven[-1].get("sleep_h")) else "", "series": rd_s, "max": 100},
    }
    live = {"protein": t.get("protein_g") or 0, "water": counts["water"], "coffee": counts["coffee"]}
    if sig.get("sleep_hours") is not None and "sleep_h" not in (seven[-1].get("edited") or []):
        seven[-1]["sleep_h"] = sig["sleep_hours"]
    ha = A.ha if getattr(A, "ha", None) and A.ha.available else None
    where = place(A)
    hs = house(A)
    out = {
        "at": now, "day": td, "version": t.get("version"), "place": where,
        "logs": {"water": counts["water"], "coffee": counts["coffee"], "meals": len(t.get("meals") or [])},
        "checkin": latest, "checkins_today": len(ck.entries(day=td)) if ck else 0,
        "status": status, "days": days, "factors": factors_for(seven, live, td),
        "needs": needs(A, t, latest, now, where, hs),
        "food": {"protein_g": round(t.get("protein_g") or 0), "protein_target_g": t.get("protein_target_g"),
                 "calories": round(sum(m.get("calories") or 0 for m in t.get("meals") or [])),
                 "meals": t.get("meals") or [], "soon": (t.get("soon") or [])[:6],
                 "totals": food_totals(t), "goals": food_goals({}, t.get("protein_target_g")),
                 "expired": len(t.get("expired") or [])},
        "reminders": [], "shopping": [], "calendar": calendar(A, now), "house": hs,
        "outside": t.get("outside"), "review": t.get("review") or 0,
    }
    fit = getattr(A, "fitness", None)
    if fit:
        try:
            g = dict(fit.goals() or {})
            bl = fit.auto_baseline(now) if hasattr(fit, "auto_baseline") else None
            if bl and isinstance(bl.get("effective"), dict):
                for k, v in bl["effective"].items():
                    if g.get(k) in (None, "") and v not in (None, ""):
                        g[k] = v
            pt = out["food"].get("protein_target_g")
            if hasattr(fit, "prefs") and hasattr(fit, "daily_plan") and fit.prefs().get("sync_adaptive", True):
                dp = fit.daily_plan(now)
                t_dp = dp.get("targets") or {}
                if t_dp.get("calories"):
                    g["calories"] = t_dp["calories"]
                if t_dp.get("protein_g"):
                    pt = t_dp["protein_g"]
                    out["food"]["protein_target_g"] = pt
                out["food"]["adaptive_mode"] = dp.get("mode_label")
            out["food"]["calorie_target"] = g.get("calories") or g.get("kcal")
            out["food"]["goals"] = food_goals(g, pt)
        except Exception:  # noqa: BLE001
            pass
    meds_svc = getattr(A, "meds", None)
    if meds_svc:
        try:
            out["meds"] = meds_svc.dashboard(now=now)["summary"]
        except Exception:  # noqa: BLE001
            pass
    maint_svc = getattr(A, "maintenance", None)
    if maint_svc:
        try:
            ml = maint_svc.list(now=now)
            out["maintenance"] = {"overdue": ml["overdue"], "soon": ml["soon"]}
            over = [x for x in ml["items"] if x["state"] == "overdue"]
            if over:
                out["needs"].append({
                    "id": f"maint:{over[0]['id']}", "kind": "Upkeep",
                    "title": f"{over[0]['name']} is overdue",
                    "sub": f"Was due {over[0]['next_due']}. Tap to mark done or update.",
                    "a": "Upkeep", "act": "go", "go": "house",
                })
        except Exception:  # noqa: BLE001
            pass
    if ha:
        out["reminders"] = await reminders(ha, now)
        out["shopping"] = await shopping(ha)
    return out


__all__ = ["build", "factors_for", "needs", "place", "house", "calendar", "STAT_FACTORS"]
