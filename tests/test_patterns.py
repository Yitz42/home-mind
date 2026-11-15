"""Patterns: daily rows from history, finding links to brain fog (and not finding them in noise), check-ins,
experiments, the weekly report, weather notes, meal logging."""
import asyncio
import os
import random
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.store import Store  # noqa: E402
from homemind.patterns import Patterns, Series, E, caffeinated  # noqa: E402
from homemind.pantry import Pantry  # noqa: E402


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


class FakeHA:
    def __init__(self, hist=None):
        self.calls, self.notes, self.hist = [], [], hist or []
        self.st = {E["weather"]: {"state": "rainy", "attributes": {"temperature": 41, "humidity": 80, "pressure": 29.8,
                                                                    "cloud_coverage": 100}}}

    async def state(self, eid):
        return self.st.get(eid)

    async def history(self, eids, start):
        return self.hist

    async def call(self, d, s, data):
        self.calls.append((d, s, data))

    async def notify(self, title, msg, tag):
        self.notes.append((title, msg, tag))


def synthetic(n, effect, seed):
    """n days; fog = 1.5 + effect if sleep < 6, plus noise. Returns rows."""
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        d = date.today() - timedelta(days=n - i)
        sleep = rnd.choice([4.5, 5.2, 5.8, 7.1, 7.4, 7.8, 8.2])
        fog = 1.5 + (effect if sleep < 6 else 0) + rnd.gauss(0, 0.8)
        rows.append({"day": d.isoformat(), "fog": max(0, min(5, round(fog))), "sleep_h": sleep,
                     "steps_prev": rnd.randint(3000, 9000), "pressure_change": rnd.choice([-0.15, 0.0, 0.02, 0.12]),
                     "room_night_temp": rnd.uniform(66, 72), "bedtime": rnd.choice([23.0, 23.5, 24.8, 25.2])})
    return rows


async def main():
    st = Store(tempfile.mktemp())
    # --- daily row from history -------------------------------------------------------------------------------------
    today = date.today()
    d0 = datetime.combine(today, datetime.min.time()).astimezone()
    y0 = d0 - timedelta(days=1)

    def rows(eid, pts, attrs=None):
        return [{"entity_id": eid, "state": str(v), "last_changed": iso(t), "attributes": attrs or {}} for t, v in pts]
    hist = [
        rows(E["fog"], [(d0 + timedelta(hours=9), 3.0)]),
        rows(E["sleep"], [(d0 + timedelta(hours=1), 0.0), (d0 + timedelta(hours=6), 5.4)]),
        rows(E["readiness"], [(d0 + timedelta(hours=1), 0.0), (d0 + timedelta(hours=6, minutes=5), 41)]),
        rows(E["steps"], [(y0 + timedelta(hours=12), 4000), (y0 + timedelta(hours=20), 11000), (d0 + timedelta(hours=8), 200)]),
        # buttons: the state is the press time; the first row is the window-start snapshot of an old press
        [{"entity_id": E["bedtime"], "state": iso(y0 - timedelta(days=2)), "last_changed": iso(y0 - timedelta(hours=1))},
         {"entity_id": E["bedtime"], "state": iso(d0 + timedelta(minutes=40)), "last_changed": iso(d0 + timedelta(minutes=40))}],
        [{"entity_id": E["nap_start"], "state": iso(y0 + timedelta(hours=14)), "last_changed": iso(y0 + timedelta(hours=14))}],
        [{"entity_id": E["nap_end"], "state": iso(y0 + timedelta(hours=14, minutes=45)), "last_changed": iso(y0 + timedelta(hours=14, minutes=45))}],
        rows(E["where"], [(y0 - timedelta(hours=10), "home"), (y0 + timedelta(hours=9), "not_home"), (y0 + timedelta(hours=17), "home")]),
        rows(E["pm"], [(y0 + timedelta(hours=20), 4.0), (d0 + timedelta(hours=2), 8.0)]),
        rows(E["room_temp"], [(y0 + timedelta(hours=22), 70.0), (d0 + timedelta(hours=3), 68.0)]),
        [{"entity_id": E["weather"], "state": "cloudy", "last_changed": iso(d0 + timedelta(hours=h)),
          "attributes": {"temperature": 40 + h, "humidity": 70, "pressure": 30.0}} for h in (2, 8, 14)],
    ]
    ha = FakeHA(hist)
    pt = Patterns(st, ha, opts={"patterns_checkin_time": "00:00"})
    h = await pt.fetch(3)
    row = pt.build_day(today, h)
    assert row["fog"] == 3.0 and row["sleep_h"] == 5.4 and row["readiness"] == 41, row
    assert row["steps_prev"] == 11000 and abs(row["bedtime"] - 24.67) < 0.02, row
    assert row["nap_prev"] == 45 and row["away_prev"] == 8.0 and row["room_night_pm"] is not None, row
    assert row["nap_min"] is None or row["nap_min"] == 0
    assert 67.9 < row["room_night_temp"] < 70.1 and row["temp_min"] == 42 and row["temp_max"] == 54, row
    stale = Series(rows(E["fog"], [(y0 + timedelta(hours=9), 3.0), (d0 + timedelta(hours=10), 3.0)]))
    n0_ = d0.replace(tzinfo=None)
    assert stale.changed(n0_ + timedelta(hours=5), n0_ + timedelta(hours=28)) is None      # re-sent, not a rating
    fresh = Series(rows(E["fog"], [(y0 + timedelta(hours=9), 3.0), (d0 + timedelta(hours=10), 2.0)]))
    assert fresh.changed(n0_ + timedelta(hours=5), n0_ + timedelta(hours=28)) == 2.0
    s = Series(rows(E["room_temp"], [(d0, 60.0), (d0 + timedelta(hours=6), 70.0)]))
    n0 = d0.replace(tzinfo=None)
    assert abs(s.mean(n0, n0 + timedelta(hours=8)) - 62.5) < 0.01          # time-weighted, not 65
    assert await pt.refresh(1) >= 1 and pt.get(today)["fog"] == 3.0

    # --- finding links -------------------------------------------------------------------------------------------------
    found = {f["factor"]: f for f in pt.analyze(synthetic(30, 2.0, 1))}
    assert found["sleep_h"]["status"] == "candidate", found["sleep_h"]
    assert found["sleep_h"]["fog_risk"] > found["sleep_h"]["fog_base"]
    false = 0
    for seed in range(10):                       # pure noise: links should be rare
        false += sum(1 for f in pt.analyze(synthetic(30, 0.0, seed)) if f["status"] == "candidate")
    assert false <= 3, false
    few = {f["factor"]: f for f in pt.analyze(synthetic(6, 2.0, 2))}
    assert few["sleep_h"]["status"] == "not enough days"

    # --- holding needs two reports 10+ days apart -----------------------------------------------------------------------
    for r in synthetic(30, 2.0, 1):
        r["day"] = (date.fromisoformat(r["day"])).isoformat()
        pt.save(r)
    rep = pt.report(record=True)
    assert "under 6 hours of sleep" in rep["text"] and "new, watching" in rep["text"], rep["text"]
    st.kv_set("patterns_seen", {"sleep_h": [(today - timedelta(days=11)).isoformat()]})
    rep = pt.report()
    assert "holding for 2+ weeks" in rep["text"] and "Suggested experiment" in rep["text"], rep["text"]

    # --- experiments ---------------------------------------------------------------------------------------------------
    e = pt.start_experiment("sleep_h", 14)
    assert pt.experiment()["factor"] == "sleep_h" and "7+ hours" in e["change"]
    fake = [{"day": (date.fromisoformat(e["start"]) - timedelta(days=14 - i)).isoformat(), "fog": 3} for i in range(14)]
    fake += [{"day": (date.fromisoformat(e["start"]) + timedelta(days=i)).isoformat(), "fog": 1} for i in range(10)]
    j = pt.judge(e, fake)
    assert j["done"] and "fog was lower" in j["text"], j
    assert not pt.judge(e, fake[:3])["done"]
    try:
        pt.start_experiment("pressure_change")
        raise AssertionError("weather can't be an experiment")
    except ValueError:
        pass

    # --- check-ins -----------------------------------------------------------------------------------------------------
    st2 = Store(tempfile.mktemp())
    ha2 = FakeHA([])

    async def phone():
        return "notify.mobile_app_test"
    p2 = Patterns(st2, ha2, opts={"patterns_checkin_time": "00:00", "patterns_evening_time": "23:59"}, phone=phone)
    await p2.checkin_tick()
    sent = [c for c in ha2.calls if c[0] == "notify"]
    assert len(sent) == 2 and any(a["action"] == "HM_FOG_3" for a in sent[0][2]["data"]["actions"]), sent
    await p2.checkin_tick()                                                  # not asked twice
    assert len([c for c in ha2.calls if c[0] == "notify"]) == 2
    r = await p2.on_action("HM_FOG_4")
    assert r == {"fog": 4.0} and ("input_number", "set_value", {"entity_id": E["fog"], "value": 4.0}) in ha2.calls
    await p2.on_action("HM_SLEEP_6.5")
    assert p2._answer(today, "fog") == 4.0 and p2._answer(today, "sleep") == 6.5
    assert await p2.on_action("SOMETHING_ELSE") is None

    # --- weather: snapshot, daily note ---------------------------------------------------------------------------------
    w = await p2.snapshot_weather()
    assert w["pressure"] == 29.8
    p2.save({"day": (today - timedelta(days=1)).isoformat(), "temp_min": 38, "temp_max": 50, "humidity": 70,
             "pressure": 29.9, "pressure_change": -0.12, "condition": "rainy"})
    assert p2.file_weather(today - timedelta(days=1)) and not p2.file_weather(today - timedelta(days=1))
    hit = st2.recall("what was the weather yesterday pressure", limit=3)
    assert any("down 0.12" in x["body"] for x in hit), hit

    # --- meals ---------------------------------------------------------------------------------------------------------
    assert caffeinated("a cup of coffee") and caffeinated("green tea") and not caffeinated("chamomile tea")
    assert not caffeinated("root beer") and not caffeinated("decaf coffee")
    pan = Pantry(st2, ha2, None, tempfile.mkdtemp())
    late = datetime.combine(today - timedelta(days=1), datetime.min.time()) + timedelta(hours=21, minutes=30)
    pan.ate("coffee", when=late.timestamp())
    m = p2._meals(today - timedelta(days=1))
    assert m and m[0]["caffeine"] and abs(m[0]["hour"] - 21.5) < 0.01
    row = p2.build_day(today, {})
    assert row["late_meal_prev"] == 21.5 and row["caffeine_late_prev"] == 1, row
    print("PATTERNS OK")


asyncio.run(main())
