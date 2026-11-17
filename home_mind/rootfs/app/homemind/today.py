"""The Today screen (#60): one call that gathers what the web panel's first page shows: readiness and sleep, the fog
check-in, the week of sleep / fog / readiness, food to eat first, protein so far, the bedroom and outside, what was
remembered today, and the house in a line. Every part is optional: a missing sensor or table leaves its field empty.
"""
import json
import re
import time
from datetime import datetime

from .speakers import visible

from . import house, structure
from .patterns import E, MIN_DAYS

ROOM = {"temp": E["room_temp"], "humidity": E["room_hum"], "aqi": E["aqi_in"], "pm": E["pm"],
        "filter": "sensor.bedroom_dyson_c2a_us_mdb9996a_hepa_filter_life"}
PROTEIN = re.compile(r"(\d+(?:\.\d+)?)\s*g protein", re.I)


def _num(s):
    try:
        return float((s or {}).get("state"))
    except (TypeError, ValueError):
        return None


async def build(A, now=None):
    now = now or time.time()
    st = A.mind.store
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    out = {"at": now, "version": None, "day": None, "room": {}, "outside": None, "week": [], "fog_days": 0,
           "fog_needed": 2 * MIN_DAYS, "soon": [], "expired": [], "where": {}, "meals": [], "protein_g": 0,
           "protein_target_g": None, "remembered": [], "house": {}, "review": 0, "comfort": None}
    from .version import VERSION
    out["version"] = VERSION

    ha = A.ha if getattr(A, "ha", None) and A.ha.available else None
    if ha:
        for k, eid in ROOM.items():
            try:
                out["room"][k] = _num(await ha.state(eid))
            except Exception:  # noqa: BLE001
                out["room"][k] = None
        try:
            w = await ha.state(E["weather"])
            if w:
                out["outside"] = {"state": (w.get("state") or "").replace("-", " "),
                                  "temp": (w.get("attributes") or {}).get("temperature")}
        except Exception:  # noqa: BLE001
            pass
    if getattr(A, "pantry", None):
        try:
            out["day"] = await A.pantry.today() if ha else None
        except Exception:  # noqa: BLE001
            out["day"] = None
        if out["day"]:
            out["protein_target_g"] = out["day"].get("protein_target_g")
        fit = getattr(A, "fitness", None)
        if fit:
            try:                     # a set protein goal (0.28) wins over the weight-based one
                out["protein_target_g"] = fit.goals().get("protein_g") or out["protein_target_g"]
            except Exception:  # noqa: BLE001
                pass
        items = A.pantry.items()
        for i in items:
            out["where"][i["location"] or "?"] = out["where"].get(i["location"] or "?", 0) + 1
            d = i["days_left"]
            if d is None:
                continue
            row = {"id": i["id"], "name": i["name"], "amount": i.get("amount") or "", "location": i["location"],
                   "days": d}
            if d < 0:
                out["expired"].append(row)
            elif d <= 7:
                out["soon"].append(row)
    if getattr(A, "patterns", None):
        rows = A.patterns.rows(120)
        out["fog_days"] = sum(1 for r in rows if r.get("fog") is not None)
        out["week"] = [{k: r.get(k) for k in ("day", "fog", "sleep_h", "readiness", "bedtime", "hrv")} for r in rows[-7:]]
    for r in st.db.execute("SELECT id, title, body, meta, created FROM items WHERE shelf='body/meals' AND status='active' "
                           "AND created >= ? ORDER BY created", (midnight,)):
        meta = json.loads(r["meta"] or "{}") if isinstance(r["meta"], str) else (r["meta"] or {})
        if not visible(meta):
            continue                # someone else's personal note (a person-bound token sees only its own)
        m = PROTEIN.search((r["title"] or "") + " " + (r["body"] or ""))
        g = float(m.group(1)) if m else meta.get("protein_g")
        out["meals"].append({"id": r["id"], "title": re.sub(r"^Ate:\s*", "", r["title"] or ""), "protein_g": g,
                             "calories": meta.get("calories"), "estimated": bool(meta.get("estimated")),
                             "at": r["created"]})
        out["protein_g"] += g or 0
    for r in st.db.execute("SELECT id, title, source, shelf, meta, created FROM items WHERE created >= ? AND status='active' "
                           "AND kind != 'procedure' ORDER BY created DESC", (midnight,)):
        if (structure.wanted(r["source"], r["shelf"]) or r["shelf"] in ("body/meals", "body/weight", "comfort/votes")) \
                and not (r["source"] or "").startswith("pack:"):
            if visible(json.loads(r["meta"] or "{}") if isinstance(r["meta"], str) else (r["meta"] or {})):
                out["remembered"].append({"id": r["id"], "title": r["title"], "shelf": r["shelf"]})
    out["remembered"] = out["remembered"][:6]
    model = st.kv_get("house_model") or {}
    done = set(st.kv_get("suggestions_done") or [])
    notable = 0
    for r in st.db.execute("SELECT meta FROM items WHERE source='home' AND created >= ? AND "
                           "shelf IN ('home/devices','home/readings')", (midnight,)):
        if (json.loads(r["meta"] or "{}").get("salience") or 0) >= house.NOTABLE:
            notable += 1
    out["house"] = {"habits": len(model.get("habits") or []), "baselines": len(model.get("baselines") or []),
                    "suggestions": len([s for s in model.get("suggestions") or [] if s["id"] not in done]),
                    "notable": notable, "habit_list": [{"text": h.get("text"), "minute": h.get("minute"),
                                                        "state": h.get("state"), "name": h.get("name")}
                                                       for h in (model.get("habits") or [])][:12]}
    try:
        out["review"] = len(st.review_queue())
    except Exception:  # noqa: BLE001
        pass
    if getattr(A, "comfort", None):
        try:
            last = A.comfort.status().get("last") or {}
            out["comfort"] = {"enabled": last.get("enabled") not in (None, "off", False), "preferred": last.get("preferred"),
                              "situation": last.get("situation")}
        except Exception:  # noqa: BLE001
            pass
    return out
