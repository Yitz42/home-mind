"""Replay: logged decisions become test cases (design spec: "Replay before shipping", "replay harness first").

A case is a request asked in words whose outcome was checked (DecisionLog.cases). Replay asks a planner, the same
code path that plans a real request but never acts, what it would do now, and compares:
  - "match" cases (it worked and nobody undid it): the logged action is the right answer; the planner should agree.
  - "override" cases (a person undid it): the logged action was wrong; the planner should NOT repeat it.
  - "device" cases (the device didn't respond as expected): say nothing about the plan; counted, not scored.

Truth is the checked outcome, never another model's opinion. Rates come with a 90% interval, and a change is only
called better or worse when the intervals say so (compare()).
"""
import asyncio
import time

from .decisions import wilson

ACTION_SAME = {"open": "on", "close": "off", "turn_on": "on", "turn_off": "off"}


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def same_action(plan, case):
    """Does the plan do what the logged decision did? Same entity, same action, and a close enough value."""
    if not plan or plan.get("entity_id") != case.get("entity_id"):
        return False
    a = (plan.get("action") or "").lower()
    b = (case.get("action") or "").lower()
    if ACTION_SAME.get(a, a) != ACTION_SAME.get(b, b):
        return False
    va, vb = plan.get("value"), case.get("value")
    if va in (None, "") and vb in (None, ""):
        return True
    na, nb = _num(va), _num(vb)
    if na is not None and nb is not None:
        return abs(na - nb) <= 5
    if va in (None, "") or vb in (None, ""):
        return a == "on"            # "turn on" against "turn on (to your usual brightness)": the same request
    return str(va).strip().lower() == str(vb).strip().lower()


async def replay(log, planner, limit=200, ids=None, since=None, timeout=30, concurrency=4, cases=None):
    """planner: async request -> plan ({"entity_id", "action", "value"} or {} for "not a device action").
    Returns a report: counts, agreement on match cases with its interval, repeats of overridden actions,
    disagreements to look at."""
    if cases is None:
        cases = log.cases(limit=limit, since=since, ids=ids)
    sem = asyncio.Semaphore(concurrency)

    async def one(c):
        async with sem:
            t0 = time.monotonic()
            try:
                plan = await asyncio.wait_for(planner(c["request"]), timeout)
                err = None
            except Exception as e:  # noqa: BLE001 - a failing case is reported, never stops the run
                plan, err = None, f"{type(e).__name__}: {e}"
            return c, plan, err, round((time.monotonic() - t0) * 1000)

    results = await asyncio.gather(*(one(c) for c in cases))
    rep = {"cases": len(cases), "match": 0, "agree": 0, "override": 0, "repeats": 0, "device": 0, "errors": 0,
           "by_route": {}, "by_channel": {}, "disagree": [], "repeated": [], "ms": []}
    for c, plan, err, ms in results:
        rep["ms"].append(ms)
        if err:
            # a planner that fails or times out on a case is wrong on it: counted, never left out (leaving it out
            # would score a planner that fails on the hard cases higher)
            rep["errors"] += 1
            rep["disagree"].append({"id": c["id"], "request": c["request"], "error": err})
            if c["kind"] == "match":
                rep["match"] += 1
            elif c["kind"] != "device":
                rep["override"] += 1
                rep["repeats"] += 1
            continue
        if c["kind"] == "device":
            rep["device"] += 1
            continue
        same = same_action(plan, c)
        r = rep["by_route"].setdefault(c.get("route") or "?", {"match": 0, "agree": 0, "override": 0, "repeats": 0})
        ch = rep["by_channel"].setdefault(c.get("channel") or "?", {"cases": 0, "right": 0})
        ch["cases"] += 1
        ch["right"] += int(same if c["kind"] == "match" else not same)
        if c["kind"] == "match":
            rep["match"] += 1
            r["match"] += 1
            if same:
                rep["agree"] += 1
                r["agree"] += 1
            elif len(rep["disagree"]) < 25:
                rep["disagree"].append({"id": c["id"], "request": c["request"],
                                        "logged": {k: c.get(k) for k in ("entity_id", "action", "value")},
                                        "now": {k: (plan or {}).get(k) for k in ("entity_id", "action", "value")}})
        else:
            rep["override"] += 1
            r["override"] += 1
            if same:
                rep["repeats"] += 1
                r["repeats"] += 1
                if len(rep["repeated"]) < 25:
                    rep["repeated"].append({"id": c["id"], "request": c["request"], "undone_by": c["override"]})
    ms = sorted(rep.pop("ms"))
    rep["p95_ms"] = ms[min(len(ms) - 1, int(0.95 * len(ms)))] if ms else None
    rep["agree_rate"] = round(rep["agree"] / rep["match"], 3) if rep["match"] else None
    rep["agree_90"] = [round(x, 3) for x in wilson(rep["agree"], rep["match"])] if rep["match"] else None
    rep["repeat_rate"] = round(rep["repeats"] / rep["override"], 3) if rep["override"] else None
    return rep


def compare(new, old):
    """Is a replay better, worse, or not distinguishable from an earlier one (e.g. against the frozen v1 set)?
    Only says better or worse when the 90% intervals don't overlap."""
    if not new.get("agree_90") or not old.get("agree_90"):
        return "not enough cases"
    lo_n, hi_n = new["agree_90"]
    lo_o, hi_o = old["agree_90"]
    if lo_n > hi_o:
        return "better"
    if hi_n < lo_o:
        return "worse"
    return "no clear difference"
