"""Fitness goals (0.28): numbers to aim for, the weight trend against them, the day's protein and calories, a
workout log with last time / bests / stalls, the weekly review, and suggested target changes that never go below a
safe floor. No Home Assistant needed."""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.store import Store  # noqa: E402
from homemind import fitness as F  # noqa: E402
from homemind.fitness import Fitness, parse_workout, is_workout_log, e1rm, CALORIE_FLOOR  # noqa: E402
from homemind.speakers import VIEWER  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


NOW = datetime(2026, 11, 15, 20, 0).timestamp()      # a Sunday evening
DAY = 86400


class Clock:
    t = NOW

    def __call__(self):
        return self.t


def fresh():
    clk = Clock()
    st = Store(tempfile.mktemp(), clock=clk)
    return st, clk


def weigh(st, t, w, bf=None):
    meta = {"weigh_in": t, "series": "weigh-in", "weight": w}
    if bf is not None:
        meta["body_fat"] = bf
    st.remember(text=f"Weighed {w} lb", title=f"Weigh-in {w}", kind="fact", shelf="body/weight", source="scale",
                meta=meta, at=t)


def meal(st, t, text, protein=None, cal=None):
    meta = {"at": t}
    if protein is not None:
        meta["protein_g"] = protein
    if cal is not None:
        meta["calories"] = cal
    st.remember(text=text, title=text, kind="note", shelf="body/meals", source="api", meta=meta, at=t)


class Patterns:
    def __init__(self, steps=None, sleep=None):
        self.steps, self.sleep = steps or [], sleep or []

    def rows(self, days):
        return [{"steps_prev": s, "sleep_h": h, "readiness": 75} for s, h in zip(self.steps, self.sleep)]


class Pantry:
    def items(self, status="in"):
        return [{"name": "Cottage cheese"}, {"name": "Bread"}]


def test_parse():
    cases = {
        "squats 3x8 at 185": [("squat", 3, 8, 185)],
        "bench 3 sets of 5 at 135 lb": [("bench press", 3, 5, 135)],
        "log: deadlift 1x5 @ 275, pull ups 3x10": [("deadlift", 1, 5, 275), ("pull-up", 3, 10, None)],
        "I did squats 5x5 at 100 kg": [("squat", 5, 5, 220.5)],
        "OHP 3x5 at 95": [("overhead press", 3, 5, 95)],
    }
    for t, want in cases.items():
        got = [(e["exercise"], e["sets"], e["reps"], e.get("weight_lb")) for e in parse_workout(t)]
        check(f"parse: {t}", got == want, got)
    r = parse_workout("ran 3 miles in 28 minutes and 30 min of yoga")
    check("parse: cardio distance and time", r == [{"exercise": "run", "miles": 3.0, "minutes": 28},
                                                   {"exercise": "yoga", "minutes": 30}], r)
    check("parse: km to miles", parse_workout("rowed 2 km")[0]["miles"] == 1.24)
    for t in ["what did I squat last time?", "3x8", "Ate eggs", "", "turn on the lights", "how many reps should I do?",
              "remind me to do squats", "I ran into Bob"]:
        check(f"not a workout log: {t!r}", not is_workout_log(t))
    for t in ["squats 3x8 at 185", "ran 3 miles", "log: 30 min yoga", "walked 2 miles"]:
        check(f"workout log: {t!r}", is_workout_log(t))
    check("silly sets/reps ignored", parse_workout("squats 99x500") == [])
    check("e1rm epley", e1rm({"weight_lb": 100, "reps": 10}) == 133.3 and e1rm({"weight_lb": 200, "reps": 1}) == 200)


def test_goals():
    st, _ = fresh()
    fi = Fitness(st)
    check("goals start empty", all(v is None for v in fi.goals().values()))
    g = fi.set_goals({"weight_target": "175", "weight_by": "2027-03-01", "calories": 2100, "steps": 8000})
    check("goals set and typed", g["weight_target"] == 175.0 and g["calories"] == 2100 and g["steps"] == 8000)
    for bad in [{"calories": 800}, {"weight_target": "abc"}, {"weight_by": "March"}, {"color": "red"},
                {"protein_g": 1000}]:
        try:
            fi.set_goals(bad)
            check(f"bad goal refused: {bad}", False)
        except ValueError:
            check(f"bad goal refused: {bad}", True)
    check("bad goal changed nothing", fi.goals()["calories"] == 2100)
    fi.set_goals({"steps": None})
    check("goal cleared", fi.goals()["steps"] is None and fi.goals()["calories"] == 2100)


def test_trend_and_progress():
    st, _ = fresh()
    fi = Fitness(st)
    check("no weigh-ins: n=0", fi.trend() == {"n": 0})
    # 190 -> ~186 over 4 weeks: about -1 lb a week, with daily noise
    for i in range(28, -1, -1):
        weigh(st, NOW - i * DAY, round(186 + i / 7 + (0.8 if i % 3 == 0 else -0.4), 1), bf=24 - (28 - i) * 0.02)
    t = fi.trend()
    check("7-day average", 185 < t["average_7d"] < 187.5, t)
    check("weekly rate about -1", -1.2 < t["rate_lb_week"] < -0.8, t)
    fi.set_goals({"weight_target": 176, "weight_by": "2027-01-24"})      # 10 weeks out, ~1 lb a week needed
    p = fi.progress()["weight"]
    check("left to go", -11 < p["left"] < -9, p)
    check("projected date about 10 weeks", "2027-01" in p["projected"] or "2027-02" in p["projected"], p)
    check("on track", p["on_track"] is True, p)
    check("not too fast", p["too_fast"] is False)
    fi.set_goals({"weight_by": "2026-12-01"})                            # 2 weeks: far too fast
    p = fi.progress()["weight"]
    check("behind and too fast", p["on_track"] is False and p["too_fast"] is True, p)
    check("suggests a later date", any(s["kind"] == "date" for s in fi.suggestions()))
    lines = fi.status_lines()
    check("status line mentions goal", any("goal 176 lb" in ln and "behind" in ln for ln in lines), lines)
    fi.set_goals({"body_fat_target": 20})
    check("body fat progress", fi.progress()["body_fat"]["target"] == 20)


def test_tally_and_snacks():
    st, _ = fresh()
    fi = Fitness(st, pantry=Pantry())
    weigh(st, NOW - DAY, 180)
    meal(st, NOW - 10 * 3600, "eggs and toast", protein=20, cal=400)
    meal(st, NOW - 6 * 3600, "chicken salad with 35 g protein", cal=600)
    meal(st, NOW - 3 * 3600, "an apple")
    meal(st, NOW - DAY - 3600, "yesterday's dinner", protein=50, cal=900)
    fi.set_goals({"calories": 2200})
    d = fi.tally()
    check("tally protein (meta + text)", d["protein_g"] == 55, d)
    check("tally calories", d["calories"] == 1000, d)
    check("unestimated meal counted", d["unestimated"] == 1 and d["meals"] == 3, d)
    check("protein target from weight", d["protein_target"] == 130, d)
    check("protein left", d["protein_left"] == 75 and d["calories_left"] == 1200, d)
    ideas = fi.protein_ideas(75)
    check("snack ideas: pantry first", ideas and ideas[0].startswith("cottage cheese"), ideas)
    fi.set_goals({"protein_g": 160})
    check("set protein goal wins", fi.tally()["protein_target"] == 160)
    ev = fi.evening_lines()
    check("evening: protein short with ideas", any("short" in ln and "cottage cheese" in ln for ln in ev), ev)
    check("evening: Sunday brings the review", "Week in review:" in ev, ev)
    # personal meals of someone else are not counted for this viewer
    st.remember(text="secret snack", title="secret snack", shelf="body/meals",
                meta={"at": NOW - 3600, "protein_g": 99, "scope": "personal", "owner": "dana"}, at=NOW - 3600)
    tok = VIEWER.set("yitz")
    try:
        check("other person's meals hidden", fi.tally()["protein_g"] == 55)
    finally:
        VIEWER.reset(tok)


def test_workouts():
    st, clk = fresh()
    fi = Fitness(st)
    clk.t = NOW - 30 * DAY
    r = fi.log("squats 3x5 at 185, ran 2 miles in 18 min")
    check("first log answered", r["answer"].startswith("Logged: squat 3×5 at 185 lb") and "last time" not in r["answer"], r)
    clk.t = NOW - 27 * DAY
    r = fi.log("squats 3x5 at 195")
    check("last time and stronger", "last time squat 3×5 at 185 lb, stronger" in r["answer"], r)
    check("new best", "a new best" in r["answer"], r)
    for k in (20, 13, 6):            # three sessions, none above 195x5
        clk.t = NOW - k * DAY
        fi.log("squats 3x5 at 190")
    clk.t = NOW
    check("last()", fi.last("squats")["entry"]["weight_lb"] == 190)
    check("records", fi.records()["squat"]["entry"]["weight_lb"] == 195)
    s = fi.stalls()
    check("stall found", [x["exercise"] for x in s] == ["squat"], s)
    check("stall suggestion", any(x["kind"] == "lift" for x in fi.suggestions()))
    check("sessions this week", fi.sessions(7) == 1)
    try:
        fi.log("what's for dinner")
        check("non-workout refused", False)
    except ValueError:
        check("non-workout refused", True)
    w = st.db.execute("SELECT meta FROM items WHERE shelf='body/workouts' LIMIT 1").fetchone()
    check("filed on body/workouts with entries", json.loads(w["meta"])["workout"][0]["exercise"] == "squat")
    ctx = fi.context_lines("what did I squat last time?")
    check("context lines have last squat", "Last squat: squat 3×5 at 190 lb" in ctx, ctx)


def test_adjust():
    st, _ = fresh()
    fi = Fitness(st, patterns=Patterns(steps=[3000, 4000, 9000, 3500, 5000, 4200, 3900], sleep=[7] * 7))
    for i in range(28, -1, -1):                      # flat at 200
        weigh(st, NOW - i * DAY, 200 + (0.6 if i % 2 else -0.6))
    for i in range(1, 15):                           # ate to target
        meal(st, NOW - i * DAY - 5 * 3600, "lunch", protein=60, cal=1000)
        meal(st, NOW - i * DAY - 2 * 3600, "dinner", protein=70, cal=1000)
    fi.set_goals({"weight_target": 185, "calories": 2000, "steps": 8000})
    s = fi.suggestions()
    cal = [x for x in s if x["kind"] == "calories"]
    check("stalled loss: 150 less", cal and cal[0]["set"] == {"calories": 1850}, s)
    check("steps nudge", any(x["kind"] == "steps" for x in s), s)
    fi.set_goals({"calories": 1850})
    check("ate over target: a note, not a cut", any(x["kind"] == "note" for x in fi.suggestions()))
    check("nothing changed by itself", fi.goals()["calories"] == 1850)
    rv = fi.review()
    check("review has weight, protein, workouts, steps, sleep",
          all(any(ln.startswith(k) for ln in rv) for k in ("Weight:", "Protein:", "Workouts:", "Steps:", "Sleep:")), rv)


def test_floor():
    st, _ = fresh()
    fi = Fitness(st)
    for i in range(28, -1, -1):
        weigh(st, NOW - i * DAY, 160 + (0.5 if i % 2 else -0.5))
    for i in range(1, 15):
        meal(st, NOW - i * DAY - 5 * 3600, "lunch", protein=60, cal=700)
        meal(st, NOW - i * DAY - 2 * 3600, "dinner", protein=70, cal=850)
    fi.set_goals({"weight_target": 150, "calories": 1600})
    cal = [x for x in fi.suggestions() if x["kind"] == "calories"]
    check("never below the floor", cal and cal[0]["set"]["calories"] == CALORIE_FLOOR, cal)
    fi.set_goals({"calories": CALORIE_FLOOR})
    check("at the floor: no cut suggested", not [x for x in fi.suggestions() if x["kind"] == "calories"])


def test_fast_loss():
    st, _ = fresh()
    fi = Fitness(st)
    for i in range(28, -1, -1):                      # 3 lb a week off 200: too fast
        weigh(st, NOW - i * DAY, round(188 + i * 3 / 7, 1))
    fi.set_goals({"weight_target": 170, "calories": 1700})
    s = [x for x in fi.suggestions() if x["kind"] == "calories"]
    check("too-fast loss: 150 more", s and s[0]["set"] == {"calories": 1850}, s)


def test_mind_logging():
    from homemind.mind import Mind
    d = tempfile.mkdtemp()
    m = Mind.__new__(Mind)
    st, _ = fresh()
    m.store = st
    m.fitness = Fitness(st)
    called = []

    async def ctx(*a, **k):
        called.append(a)
        raise RuntimeError("model path reached")
    m.context_for = ctx
    m._conversations = lambda store: {}
    m._handoff = lambda *a: None
    m.views = None
    try:
        out = asyncio.run(m._ask("squats 3x8 at 185", use_brain=True))
    except RuntimeError:
        out = None
    check("ask logs a workout without a model", out and out.get("workout") and not called, out)
    tok = VIEWER.set("yitz")
    try:
        out = asyncio.run(m._ask("bench 3x5 at 135"))
    finally:
        VIEWER.reset(tok)
    row = st.db.execute("SELECT meta FROM items WHERE shelf='body/workouts' ORDER BY id DESC LIMIT 1").fetchone()
    mt = json.loads(row["meta"])
    check("voice workout is personal to the speaker", mt.get("scope") == "personal" and mt.get("owner") == "yitz", mt)
    del d


for fn in [test_parse, test_goals, test_trend_and_progress, test_tally_and_snacks, test_workouts, test_adjust,
           test_floor, test_fast_loss, test_mind_logging]:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(f"{fn.__name__} raised {e!r}", False)
print(f"\n{len(FAILS)} failed" if FAILS else "\nall fitness checks passed")
sys.exit(1 if FAILS else 0)
