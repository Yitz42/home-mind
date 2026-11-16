"""Patterns: what seems to go with brain fog, and small experiments to improve it.

Daily record
  One row per day, kept in Home Mind (Home Assistant's history only lasts about 10 days): the brain-fog rating from
  Steady (0-5) as the outcome, and next to it what might drive it - sleep (hours, bedtime, wake), the morning
  readings (readiness, recovery, HRV, resting heart rate, wake-ups), the day before and the day before that (steps,
  stress alerts: crashes after exertion often show up a day or two later), the bedroom overnight (temperature,
  humidity), the weather (temperature, humidity, pressure and how much it moved), food logged with Jeeves (how late
  the last meal was, caffeine after 2 pm) and how full the calendar was. The weather also goes into memory as one
  note a day, so "what was the weather Tuesday?" works.

Finding patterns
  Plain comparisons anyone can check: average fog on days with a factor vs. days without. A pattern is only
  reported with at least MIN_DAYS on each side, a difference of at least MIN_DIFF points, and a shuffle test saying
  it's unlikely to be chance; it's called "holding" only once it shows up in two weekly reports at least 10 days
  apart. Always "linked to", never "causes".

Experiments
  One small, low-risk change at a time for 1-2 weeks, then fog during it is compared with the two weeks before.
  Exertion changes are about pacing (no "do more" experiments), which suits post-viral fatigue.

Check-ins
  If the fog rating (or last night's sleep) is missing by option patterns_checkin_time (default 11:00), a phone
  notification asks with tap buttons; one more try in the evening. Answers go to Steady's helpers and memory.

Weekly report
  Saturday morning (option patterns_report_day/time), to the phone and memory (shelf summaries/patterns).
"""
import json
import logging
import random
import re
import statistics as stats
import time
from datetime import date, datetime, timedelta

log = logging.getLogger("homemind.patterns")

E = {
    "fog": "input_number.steady_fog_today", "sleep": "input_number.steady_sleep_hours",
    "readiness": "input_number.steady_readiness", "recovery": "input_number.steady_recovery",
    "hrv": "input_number.steady_hrv", "resting_hr": "input_number.steady_resting_hr",
    "night_low_hr": "input_number.steady_night_low_hr", "wakeups": "input_number.steady_wakeups",
    "steps": "input_number.steady_steps", "stress": "input_number.steady_stress_alerts",
    "calm": "input_number.steady_calm_pct", "bedtime": "input_button.steady_bedtime",
    "wake": "input_button.steady_wake_done", "room_temp": "sensor.dyson_c2a_us_mdb9996a_temperature",
    "room_hum": "sensor.dyson_c2a_us_mdb9996a_humidity", "weather": "weather.forecast_home",
    "nap_start": "input_button.steady_nap_start", "nap_end": "input_button.steady_nap_end",
    "lights_off": "input_button.steady_lights_off",
    "pm": "sensor.bedroom_dyson_c2a_us_mdb9996a_particulates", "voc": "sensor.bedroom_dyson_c2a_us_mdb9996a_voc",
    "aqi_in": "sensor.bedroom_dyson_c2a_us_mdb9996a_air_quality_index",
    "aqi_out": "sensor.bedroom_dyson_c2a_us_mdb9996a_outdoor_aqi",
    "where": "device_tracker.yitzs_iphone", "sun": "sun.sun",
    "vote_cold": "input_button.comfort_cold", "vote_hot": "input_button.comfort_hot",
}
MIN_DAYS = 5
MIN_DIFF = 0.75
P_MAX = 0.10
SHUFFLES = 2000
CAFFEINE = re.compile(r"coffee|espresso|latte|cappuccino|\btea\b|cola|\bcoke\b|pepsi|energy drink|red bull|"
                      r"monster|mountain dew|dr\.? pepper|matcha", re.I)
NO_CAFFEINE = re.compile(r"decaf|herbal|chamomile|caffeine[- ]free|rooibos|cinnamon spice", re.I)

# key, label (what the "risk" side means), split, hunch?, experiment (None = can't be changed)
FACTORS = [
    ("sleep_h", "under 6 hours of sleep", ("lt", 6.0, "ge", 7.0), True,
     "Protect 7+ hours: lights off at your Steady bedtime every night for 2 weeks."),
    ("bedtime", "bedtime after 12:30 am", ("gt", 24.5, "le", 23.75), True,
     "Same bedtime within 30 minutes every night for 2 weeks."),
    ("steps_prev", "a busier day than usual the day before", ("rel_gt", 1.3, "rel_le", 1.0), True,
     "Pacing for 2 weeks: on good days stop around your typical step count instead of doing extra, and rest "
     "before you feel done."),
    ("steps_prev2", "a busier day than usual two days before", ("rel_gt", 1.3, "rel_le", 1.0), True,
     "Pacing for 2 weeks: after a big day, keep the next two days light."),
    ("stress_prev", "3+ stress alerts the day before", ("ge", 3, "le", 1), False,
     "A 5-minute Steady breathing session after each stress alert, for 2 weeks."),
    ("readiness", "morning readiness under 50", ("lt", 50, "ge", 65), False, None),
    ("fluid", "a higher fluid-balance reading on the scale (0.89+)", ("ge", 0.89, "lt", 0.88), False, None),
    ("hrv", "lower HRV than usual", ("rel_lt", 0.85, "rel_ge", 1.0), False, None),
    ("wakeups", "3+ wake-ups in the night", ("ge", 3, "le", 1), False, None),
    ("room_night_temp", "a warmer bedroom overnight", ("rel_gt", 1.03, "rel_le", 1.0), False,
     "Keep the bedroom about 2°F cooler overnight for 2 weeks (Home Mind's comfort control can do this)."),
    ("room_night_hum", "dry bedroom air overnight (under 35%)", ("lt", 35, "ge", 40), False,
     "Run the humidifier overnight to stay above 40% for 2 weeks."),
    ("nap_prev", "a nap the day before", ("ge", 20, "eq", 0), True, None),
    ("away_prev", "more time out of the house than usual the day before", ("rel_gt", 1.5, "rel_le", 1.0), True,
     "Pacing for 2 weeks: after a long day out, keep the next day mostly at home and light."),
    ("away_prev2", "more time out of the house than usual two days before", ("rel_gt", 1.5, "rel_le", 1.0), True,
     None),
    ("room_night_pm", "more particles in the bedroom air overnight", ("rel_gt", 1.5, "rel_le", 1.0), False,
     "Run the Dyson on auto overnight for 2 weeks."),
    ("room_night_voc", "more VOCs in the bedroom air overnight", ("rel_gt", 1.5, "rel_le", 1.0), False,
     "Air out the bedroom for 10 minutes before bed for 2 weeks."),
    ("outdoor_aqi", "worse outdoor air (AQI 50+)", ("ge", 50, "lt", 35), False, None),
    ("daylight_h", "short days (under 10 hours of daylight)", ("lt", 10.0, "ge", 11.0), False,
     "10 minutes of outdoor light within an hour of waking, every day for 2 weeks."),
    ("night_votes", "a too-hot or too-cold night (2+ votes)", ("ge", 2, "eq", 0), False,
     "Let the comfort control keep the bedroom steadier overnight for 2 weeks (fewer swings)."),
    ("pressure_change", "a pressure drop since yesterday", ("le", -0.10, "abs_lt", 0.05), False, None),
    ("temp_max", "a cold day (high under 45°F)", ("lt", 45, "ge", 55), False, None),
    ("late_meal_prev", "a late last meal the night before (after 9 pm)", ("ge", 21.0, "lt", 20.0), False,
     "Finish eating by 8 pm for 2 weeks."),
    ("caffeine_late_prev", "caffeine after 2 pm the day before", ("eq", 1, "eq", 0), False,
     "No caffeine after 2 pm for 2 weeks."),
    ("events", "a packed calendar (4+ events)", ("ge", 4, "le", 1), False,
     "On days with 4+ events, block a 30-minute empty gap, for 2 weeks."),
]
FACTOR = {f[0]: f for f in FACTORS}


def _f(x):
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return None


def _t(s):
    """HA timestamp -> local naive datetime."""
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


def _day_span(day):
    """Local midnight to the next local midnight, as timestamps (23 or 25 hours apart on DST days, not 86400)."""
    lo = datetime.combine(day, datetime.min.time())
    return lo.timestamp(), (lo + timedelta(days=1)).timestamp()


def _hours(dt, day):
    """Clock time as hours after midnight of `day` (a bedtime of 00:40 the next day is 24.67)."""
    return (dt - datetime.combine(day, datetime.min.time())).total_seconds() / 3600


BUTTONS = ("bedtime", "wake", "nap_start", "nap_end", "lights_off", "vote_cold", "vote_hot")


class Series:
    """One entity's history as (time, value) pairs. For buttons the state is the press time itself, which is used
    instead of last_changed (history stamps the state in force at the window start with the start time)."""
    def __init__(self, rows, attr=None, button=False):
        self.pts = []
        for r in rows or []:
            st = r.get("state")
            if st in (None, "unknown", "unavailable"):
                continue
            if button:
                try:
                    t = _t(st)
                except ValueError:
                    continue
                if not any(p[0] == t for p in self.pts):
                    self.pts.append((t, st))
                continue
            t = _t(r.get("last_changed") or r.get("last_updated"))
            if attr:
                v = (r.get("attributes") or {}).get(attr)
                self.pts.append((t, _f(v)))
            else:
                self.pts.append((t, _f(st) if _f(st) is not None else st))
        self.pts.sort(key=lambda p: p[0])

    def within(self, a, b):
        return [(t, v) for t, v in self.pts if a <= t < b]

    def last(self, a, b, nonzero=False):
        for t, v in reversed(self.within(a, b)):
            if isinstance(v, float) and (v != 0 or not nonzero):
                return v
        return None

    def max(self, a, b):
        vs = [v for _, v in self.within(a, b) if isinstance(v, float)]
        return max(vs) if vs else None

    def mean(self, a, b):
        """Time-weighted mean over [a, b), carrying the value in force at a."""
        before = [(t, v) for t, v in self.pts if t < a and isinstance(v, float)]
        pts = ([(a, before[-1][1])] if before else []) + [(t, v) for t, v in self.within(a, b) if isinstance(v, float)]
        if not pts:
            return None
        tot, acc = 0.0, 0.0
        for i, (t, v) in enumerate(pts):
            end = pts[i + 1][0] if i + 1 < len(pts) else b
            w = max((end - t).total_seconds(), 0)
            acc, tot = acc + v * w, tot + w
        return acc / tot if tot else pts[-1][1]

    def hours_where(self, a, b, test):
        """Hours in [a, b) whose state passes test(state), carrying the state in force at a."""
        before = [(t, v) for t, v in self.pts if t < a]
        pts = ([(a, before[-1][1])] if before else []) + self.within(a, b)
        if not pts:
            return None
        tot = 0.0
        for i, (t, v) in enumerate(pts):
            end = pts[i + 1][0] if i + 1 < len(pts) else min(b, datetime.now())
            if test(v):
                tot += max((end - t).total_seconds(), 0)
        return round(tot / 3600, 1)

    def changed(self, a, b):
        """The last value in [a, b) that differs from the one before it. Steady re-sends the same fog rating on
        every sync and Home Assistant restart, so a repeat is not a new rating."""
        prev, out = None, None
        for t, v in self.pts:
            if t >= b:
                break
            if t >= a and isinstance(v, float) and v != prev:
                out = v
            prev = v
        return out

    def times(self, a, b):
        return [t for t, _ in self.within(a, b)]


class Patterns:
    def __init__(self, store, ha, calendars=None, opts=None, phone=None):
        self.store, self.ha, self.cal = store, ha, calendars
        o = opts or {}
        self.checkin_at = tuple(int(x) for x in (o.get("patterns_checkin_time") or "11:00").split(":"))
        self.evening_at = tuple(int(x) for x in (o.get("patterns_evening_time") or "19:00").split(":"))
        self.report_day = int(o.get("patterns_report_day", 5))          # Monday 0 ... Saturday 5
        self.report_at = tuple(int(x) for x in (o.get("patterns_report_time") or "08:00").split(":"))
        self.checkins_on = o.get("patterns_checkins", True) is not False
        self.phone = phone                                              # async () -> "notify.mobile_app_x" or None
        self.last_refresh = 0
        store.db.execute("CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, data TEXT, updated REAL)")
        store.db.execute("CREATE TABLE IF NOT EXISTS weather_hours (t REAL PRIMARY KEY, data TEXT)")
        store.db.commit()

    # ---- weather --------------------------------------------------------------------------------------------------
    async def snapshot_weather(self):
        s = await self.ha.state(E["weather"])
        if not s or s.get("state") in (None, "unknown", "unavailable"):
            return None
        a = s.get("attributes") or {}
        d = {k: a.get(k) for k in ("temperature", "humidity", "pressure", "dew_point", "cloud_coverage",
                                   "wind_speed", "uv_index")}
        d["condition"] = s["state"]
        t = int(time.time() // 3600 * 3600)
        self.store.db.execute("INSERT OR REPLACE INTO weather_hours(t, data) VALUES(?, ?)", (t, json.dumps(d)))
        self.store.db.commit()
        return d

    def _weather_from_table(self, day):
        lo, hi = _day_span(day)
        rows = [json.loads(r["data"]) for r in self.store.db.execute(
            "SELECT data FROM weather_hours WHERE t >= ? AND t < ? ORDER BY t", (lo, hi))]
        return rows

    @staticmethod
    def _weather_summary(rows):
        def col(k):
            return [_f(r.get(k)) for r in rows if _f(r.get(k)) is not None]
        t, h, p, c = col("temperature"), col("humidity"), col("pressure"), col("cloud_coverage")
        if not t and not p:
            return {}
        conds = [r.get("condition") for r in rows if r.get("condition") not in (None, "unavailable", "unknown")]
        return {"temp_min": min(t) if t else None, "temp_max": max(t) if t else None,
                "humidity": round(stats.mean(h)) if h else None, "pressure": round(stats.mean(p), 2) if p else None,
                "cloud": round(stats.mean(c)) if c else None,
                "condition": max(set(conds), key=conds.count) if conds else None}

    # ---- daily rows -----------------------------------------------------------------------------------------------
    async def fetch(self, days):
        """Home Assistant history for the last `days` days (plus 3 for the day-before factors)."""
        start = (datetime.utcnow() - timedelta(days=days + 3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        h = await self.ha.history(list(E.values()), start) or []
        out = {}
        for rows in h:
            if rows:
                out[rows[0]["entity_id"]] = rows
        return out

    def _fluid(self, d0):
        """The scale's fluid-balance reading (100/20 kHz impedance ratio) from that day's last weigh-in, if any."""
        lo = d0.timestamp()
        r = self.store.db.execute("SELECT meta FROM items WHERE shelf='body/weight' AND created >= ? AND created < ? "
                                  "AND meta LIKE '%fluid_balance%' ORDER BY created DESC LIMIT 1", (lo, lo + 86400)).fetchone()
        if not r:
            return None
        return json.loads(r["meta"] or "{}").get("wyze_scale_fluid_balance_100_20_khz_impedance_ratio")

    def build_day(self, day, hist):
        """One day's row from fetched history (and Home Mind's own notes)."""
        S = {k: Series(hist.get(eid), button=k in BUTTONS) for k, eid in E.items() if k != "weather"}
        d0 = datetime.combine(day, datetime.min.time())
        h = lambda x: d0 + timedelta(hours=x)  # noqa: E731
        row = {"day": day.isoformat()}
        fog = S["fog"].changed(h(5), h(28))
        ans = self._answer(day, "fog")
        row["fog"] = ans if ans is not None else fog
        sleep = self._answer(day, "sleep")
        row["sleep_h"] = sleep if sleep is not None else S["sleep"].last(h(3), h(14), nonzero=True)
        for k in ("readiness", "recovery", "hrv", "resting_hr", "night_low_hr"):
            row[k] = S[k].last(h(3), h(14), nonzero=True)
        row["wakeups"] = S["wakeups"].last(h(3), h(14))
        row["fluid"] = self._fluid(d0)
        bt = S["bedtime"].times(h(-6), h(6))
        row["bedtime"] = round(_hours(bt[-1], day - timedelta(days=1)), 2) if bt else None
        wk = S["wake"].times(h(4), h(13))
        row["wake"] = round(_hours(wk[0], day), 2) if wk else None
        row["steps_prev"] = S["steps"].max(h(-24), h(0)) or None             # 0 steps = the watch didn't sync
        row["steps_prev2"] = S["steps"].max(h(-48), h(-24)) or None
        row["calm_prev"] = S["calm"].last(h(-24), h(0)) or None
        row["stress_prev"] = S["stress"].max(h(-24), h(0)) if (row["steps_prev"] or row["calm_prev"]) else None
        lo = S["lights_off"].times(h(-6), h(6))
        row["lights_off"] = round(_hours(lo[-1], day - timedelta(days=1)), 2) if lo else None
        if row["bedtime"] is None and row["lights_off"] is not None:
            row["bedtime"] = row["lights_off"]
        row["nap_min"] = self._naps(S, h(8), h(22))
        row["nap_prev"] = self._naps(S, h(-16), h(-2))
        away = lambda v: isinstance(v, str) and v not in ("home",)  # noqa: E731
        row["away_h"] = S["where"].hours_where(h(6), h(24), away) if day < date.today() else None
        row["away_prev"] = S["where"].hours_where(h(-18), h(0), away)
        row["away_prev2"] = S["where"].hours_where(h(-42), h(-24), away)
        row["daylight_h"] = S["sun"].hours_where(h(0), h(24), lambda v: v == "above_horizon") if day < date.today() \
            else None
        for k, key in (("room_night_pm", "pm"), ("room_night_voc", "voc"), ("room_night_aqi", "aqi_in")):
            v = S[key].mean(h(-1), h(7))
            row[k] = round(v, 3 if key == "voc" else 1) if v is not None else None
        v = S["aqi_out"].mean(h(0), h(24))
        row["outdoor_aqi"] = round(v) if v is not None else None
        row["night_votes"] = len(S["vote_cold"].times(h(-2), h(8))) + len(S["vote_hot"].times(h(-2), h(8)))
        t = S["room_temp"].mean(h(-1), h(7))
        row["room_night_temp"] = round(t, 1) if t is not None else None
        hu = S["room_hum"].mean(h(-1), h(7))
        row["room_night_hum"] = round(hu) if hu is not None else None
        w = self._weather_summary(self._weather_from_table(day))
        if not w and hist.get(E["weather"]):
            wr = [dict((r.get("attributes") or {}), condition=r.get("state")) for r in hist[E["weather"]]
                  if h(0) <= _t(r.get("last_changed") or r.get("last_updated")) < h(24)]
            w = self._weather_summary(wr)
        row.update(w)
        prev = self.get(day - timedelta(days=1))
        if row.get("pressure") is not None and prev and prev.get("pressure") is not None:
            row["pressure_change"] = round(row["pressure"] - prev["pressure"], 2)
        meals = self._meals(day - timedelta(days=1))
        row["late_meal_prev"] = max((m["hour"] for m in meals), default=None)
        row["caffeine_late_prev"] = (1 if any(m["caffeine"] and m["hour"] >= 14 for m in meals) else 0) if meals else None
        if self.cal:
            try:
                ev = self.cal.events_on(day)
                row["events"] = sum(1 for e in ev if not e["meta"].get("all_day"))
            except Exception:  # noqa: BLE001
                row["events"] = None
        return row

    @staticmethod
    def _naps(S, a, b):
        """Minutes napped in [a, b) from Steady's nap buttons (a start with no end counts 30 minutes)."""
        starts, ends = S["nap_start"].times(a, b), S["nap_end"].times(a, b + timedelta(hours=4))
        if not starts:
            return 0 if (S["nap_start"].pts or S["nap_end"].pts) else None
        tot = 0
        for st_ in starts:
            e = next((x for x in ends if x > st_), None)
            tot += min((e - st_).total_seconds() / 60, 240) if e else 30
        return round(tot)

    def _answer(self, day, what):
        """A check-in answer (or note) for that day, which beats the watch."""
        shelf = "body/fog" if what == "fog" else "body/sleep"
        lo, hi = _day_span(day)
        r = self.store.db.execute(f"SELECT body, meta FROM items WHERE shelf=? AND status='active' AND created >= ? "
                                  f"AND created < ? ORDER BY created DESC LIMIT 1", (shelf, lo, hi)).fetchone()
        if not r:
            return None
        m = json.loads(r["meta"] or "{}")
        if what in m:
            return _f(m[what])
        if what == "sleep":
            x = re.search(r"\((\d+(?:\.\d+)?) hours?\)", r["body"])
            return float(x.group(1)) if x else None
        return None

    def _meals(self, day):
        lo, hi = _day_span(day)
        out = []
        for r in self.store.db.execute("SELECT body, meta, created FROM items WHERE shelf='body/meals' AND "
                                       "status='active' AND created >= ? AND created < ?", (lo, hi + 4 * 3600)):
            m = json.loads(r["meta"] or "{}")
            t = datetime.fromtimestamp(m.get("at") or r["created"])
            out.append({"hour": round(_hours(t, day), 2), "caffeine": bool(m.get("caffeine"))})
        return out

    def save(self, row):
        self.store.db.execute("INSERT OR REPLACE INTO days(day, data, updated) VALUES(?, ?, ?)",
                              (row["day"], json.dumps(row), time.time()))
        self.store.db.commit()

    def get(self, day):
        r = self.store.db.execute("SELECT data FROM days WHERE day=?", (day.isoformat(),)).fetchone()
        return json.loads(r["data"]) if r else None

    def rows(self, days=120):
        since = (date.today() - timedelta(days=days)).isoformat()
        return [json.loads(r["data"]) for r in self.store.db.execute(
            "SELECT data FROM days WHERE day >= ? ORDER BY day", (since,))]

    async def refresh(self, days=3):
        """Rebuild the last `days` days (older days stay as they were saved)."""
        hist = await self.fetch(days)
        if not hist:
            return 0
        today = date.today()
        n = 0
        for i in range(days, -1, -1):
            day = today - timedelta(days=i)
            row = self.build_day(day, hist)
            if any(v is not None for k, v in row.items() if k != "day"):
                old = self.get(day) or {}
                # keep what history no longer has (it only lasts ~10 days)
                for k, v in old.items():
                    if row.get(k) is None and v is not None:
                        row[k] = v
                self.save(row)
                n += 1
        self.last_refresh = time.time()
        return n

    def file_weather(self, day):
        """One memory note per day for the weather, so questions about it work."""
        row = self.get(day)
        if not row or row.get("temp_max") is None:
            return False
        key = f"weather|{day.isoformat()}"
        if self.store.active_by_key(key):
            return False
        pc = row.get("pressure_change")
        bits = [f"{row['temp_min']:.0f}-{row['temp_max']:.0f}°F"]
        if row.get("condition"):
            bits.append(row["condition"].replace("partlycloudy", "partly cloudy").replace("clear-night", "clear"))
        if row.get("humidity") is not None:
            bits.append(f"humidity {row['humidity']}%")
        if row.get("pressure") is not None:
            bits.append(f"pressure {row['pressure']:.2f} inHg"
                        + (f" ({'up' if pc > 0 else 'down'} {abs(pc):.2f} from the day before)" if pc else ""))
        when = f"{day:%a %b} {day.day} {day.year}"
        self.store.remember(text=f"Weather {when}: " + ", ".join(bits) + ".", title=f"Weather {when}",
                            kind="fact", shelf="home/weather", key=key, source="weather", tags=["weather"],
                            meta={"day": day.isoformat(), "series": "weather:daily"})
        return True

    # ---- analysis ---------------------------------------------------------------------------------------------------
    @staticmethod
    def _split(rows, key, split):
        vals = [r[key] for r in rows if r.get(key) is not None and r.get("fog") is not None]
        if not vals:
            return [], []
        ref = stats.median(vals)

        def side(op, x, v):
            if op.startswith("rel_"):
                x = x * ref
                op = op[4:]
            return {"lt": v < x, "le": v <= x, "gt": v > x, "ge": v >= x, "eq": v == x,
                    "abs_lt": abs(v) < x}[op]
        a_op, a_x, b_op, b_x = split
        risk, base = [], []
        for r in rows:
            v, fog = r.get(key), r.get("fog")
            if v is None or fog is None:
                continue
            if side(a_op, a_x, v):
                risk.append(fog)
            elif side(b_op, b_x, v):
                base.append(fog)
        return risk, base

    @staticmethod
    def _shuffle_p(a, b, seed=7):
        """Two-sided permutation test on the difference of means."""
        obs = abs(stats.mean(a) - stats.mean(b))
        pool, na = a + b, len(a)
        rnd = random.Random(seed)
        hit = 0
        for _ in range(SHUFFLES):
            rnd.shuffle(pool)
            if abs(stats.mean(pool[:na]) - stats.mean(pool[na:])) >= obs - 1e-12:
                hit += 1
        return (hit + 1) / (SHUFFLES + 1)

    def analyze(self, rows=None):
        rows = rows if rows is not None else self.rows()
        out = []
        for key, label, split, hunch, exp in FACTORS:
            risk, base = self._split(rows, key, split)
            res = {"factor": key, "label": label, "n_risk": len(risk), "n_base": len(base), "hunch": hunch,
                   "experiment": exp}
            if len(risk) >= MIN_DAYS and len(base) >= MIN_DAYS:
                diff = stats.mean(risk) - stats.mean(base)
                p = self._shuffle_p(list(risk), list(base))
                res.update(fog_risk=round(stats.mean(risk), 1), fog_base=round(stats.mean(base), 1),
                           diff=round(diff, 2), p=round(p, 3),
                           status="candidate" if diff >= MIN_DIFF and p < P_MAX else
                           ("opposite" if diff <= -MIN_DIFF and p < P_MAX else "no link"))
            else:
                res["status"] = "not enough days"
            out.append(res)
        return out

    def _seen(self, findings, record=False):
        """Mark candidates that showed up in an earlier weekly report at least 10 days before: those are 'holding'."""
        hist = self.store.kv_get("patterns_seen", {}) or {}
        today = date.today().isoformat()
        for f in findings:
            if f["status"] == "candidate":
                days = hist.get(f["factor"], [])
                if any((date.fromisoformat(today) - date.fromisoformat(d)).days >= 10 for d in days):
                    f["status"] = "holding"
                if record and today not in days:
                    hist[f["factor"]] = (days + [today])[-6:]
        if record:
            self.store.kv_set("patterns_seen", hist)
        return findings

    # ---- experiments -------------------------------------------------------------------------------------------------
    def experiment(self):
        return self.store.kv_get("patterns_experiment")

    def start_experiment(self, factor, days=14):
        f = FACTOR.get(factor)
        if not f or not f[4]:
            raise ValueError("no experiment for that")
        e = {"factor": factor, "change": f[4], "start": date.today().isoformat(),
             "end": (date.today() + timedelta(days=days)).isoformat()}
        self.store.kv_set("patterns_experiment", e)
        self.store.remember(text=f"Started a brain-fog experiment on {date.today():%b %d}: {f[4]}",
                            title="Brain-fog experiment started", shelf="body/experiments", source="patterns",
                            meta=e)
        return e

    def stop_experiment(self):
        e = self.experiment()
        if e:
            res = self.judge(e)
            self.store.remember(text=f"Brain-fog experiment ended ({e['change']}): {res['text']}",
                                title="Brain-fog experiment result", shelf="body/experiments", source="patterns",
                                meta=dict(e, result=res))
            self.store.kv_set("patterns_experiment", None)
            return res
        return None

    def judge(self, e, rows=None):
        rows = rows if rows is not None else self.rows()
        s, en = date.fromisoformat(e["start"]), date.fromisoformat(e["end"])
        during = [r["fog"] for r in rows if r.get("fog") is not None and s <= date.fromisoformat(r["day"]) <= en]
        before = [r["fog"] for r in rows if r.get("fog") is not None and
                  s - timedelta(days=14) <= date.fromisoformat(r["day"]) < s]
        if len(during) < 7 or len(before) < 5:
            return {"text": f"not enough fog ratings yet ({len(during)} during, {len(before)} before; need 7 and 5)",
                    "done": False}
        d = stats.mean(during) - stats.mean(before)
        p = self._shuffle_p(list(during), list(before))
        verdict = ("fog was lower" if d <= -0.5 and p < 0.2 else "fog was higher" if d >= 0.5 and p < 0.2
                   else "no clear change")
        return {"text": f"{verdict}: average {stats.mean(during):.1f} during vs {stats.mean(before):.1f} in the two "
                        f"weeks before ({len(during)} vs {len(before)} days)", "diff": round(d, 2), "p": round(p, 3),
                "done": True}

    # ---- the report --------------------------------------------------------------------------------------------------
    def report(self, record=False):
        rows = self.rows()
        fog_days = [r for r in rows if r.get("fog") is not None]
        recent = [r for r in rows if date.fromisoformat(r["day"]) > date.today() - timedelta(days=7)]
        lines = []
        rf = [r["fog"] for r in recent if r.get("fog") is not None]
        lines.append(f"This week: fog rated on {len(rf)} of 7 days" + (f", average {stats.mean(rf):.1f}/5" if rf else ""))
        sl = [r["sleep_h"] for r in recent if r.get("sleep_h")]
        if sl:
            lines.append(f"Sleep averaged {stats.mean(sl):.1f} h ({min(sl):.1f}-{max(sl):.1f})")
        findings = self._seen(self.analyze(rows), record=record)
        strong = [f for f in findings if f["status"] in ("holding", "candidate")]
        strong.sort(key=lambda f: (f["status"] != "holding", -f["diff"]))
        if strong:
            lines.append("Linked to more fog (not proof of cause):")
            for f in strong[:4]:
                lines.append(f"- {f['label']}: fog {f['fog_risk']} vs {f['fog_base']} "
                             f"({f['n_risk']} vs {f['n_base']} days)"
                             + (" - holding for 2+ weeks" if f["status"] == "holding" else " - new, watching"))
        ready = [f for f in findings if f["status"] != "not enough days"]
        if not strong:
            if len(fog_days) < 2 * MIN_DAYS:
                lines.append(f"Patterns: {len(fog_days)} days with a fog rating so far; comparisons start at "
                             f"{2 * MIN_DAYS} and get useful around 21.")
            else:
                lines.append(f"No clear links yet ({len(ready)} factors checked).")
        e = self.experiment()
        if e:
            res = self.judge(e, rows)
            if date.fromisoformat(e["end"]) < date.today() and res.get("done"):
                lines.append(f"Experiment finished ({e['change']}): {res['text']}")
                if record:
                    self.stop_experiment()
            else:
                lines.append(f"Experiment until {date.fromisoformat(e['end']):%b %d}: {e['change']} ({res['text']})")
        else:
            nxt = next((f for f in strong if f["experiment"]), None)
            if nxt:
                lines.append(f"Suggested experiment: {nxt['experiment']} Tell Jeeves \"start the fog experiment\".")
        gaps = self.gaps(recent)
        if gaps:
            lines.append("Missing: " + gaps)
        return {"text": "\n".join(lines), "findings": findings, "fog_days": len(fog_days)}

    @staticmethod
    def gaps(rows):
        miss = []
        for k, label in (("fog", "fog rating"), ("sleep_h", "sleep"), ("readiness", "morning readings")):
            n = sum(1 for r in rows if r.get(k) is None)
            if n:
                miss.append(f"{label} on {n} day{'s' if n > 1 else ''}")
        return ", ".join(miss)

    async def send_report(self):
        r = self.report(record=True)
        text = r["text"]
        self.store.remember(text=text, title=f"Brain-fog patterns {datetime.now():%a %b %d %Y}", kind="note",
                            shelf="summaries/patterns", tags=["summary", "fog"], source="patterns")
        await self._notify("Home Mind: this week's fog patterns", text, "hm_patterns")
        return r

    # ---- check-ins ---------------------------------------------------------------------------------------------------
    async def _notify(self, title, message, tag, actions=None):
        await self.ha.notify(title, message, tag)
        target = await self.phone() if self.phone else None
        if target:
            data = {"title": title, "message": message, "data": {"tag": tag}}
            if actions:
                data["data"]["actions"] = actions
            try:
                await self.ha.push(target, data)
            except Exception as e:  # noqa: BLE001
                log.warning("patterns notify: %s", e)

    async def _missing_today(self):
        today = date.today()
        hist = await self.fetch(0)
        row = self.build_day(today, hist) if hist else {}
        return {"fog": row.get("fog") is None or not self._rated_today(hist),
                "sleep": row.get("sleep_h") is None}

    def _rated_today(self, hist):
        if self._answer(date.today(), "fog") is not None:
            return True
        s = Series(hist.get(E["fog"]))
        d0 = datetime.combine(date.today(), datetime.min.time())
        return bool(s.within(d0 + timedelta(hours=5), d0 + timedelta(hours=28)))

    async def ask(self, what):
        if what == "fog":
            acts = [{"action": f"HM_FOG_{i}", "title": t} for i, t in
                    enumerate(["0 clear", "1", "2", "3", "4", "5 heavy fog"])]
            await self._notify("How's your head today?", "Brain fog 0 (clear) to 5 (heavy). Tap one, even if it's "
                               "the same as yesterday.", "hm_fog", acts)
        else:
            acts = [{"action": f"HM_SLEEP_{v}", "title": t} for v, t in
                    (("4.5", "under 5 h"), ("5.5", "5-6 h"), ("6.5", "6-7 h"), ("7.5", "7-8 h"), ("8.5", "8+ h"))]
            await self._notify("How long did you sleep?", "The watch didn't get last night's sleep. Tap one.",
                               "hm_sleep", acts)

    async def checkin_tick(self):
        if not self.checkins_on:
            return
        now = datetime.now()
        today = now.date().isoformat()
        asked = self.store.kv_get("patterns_asked", {}) or {}
        if asked.get("day") != today:
            asked = {"day": today, "fog": 0, "sleep": 0}
        due = None
        if (now.hour, now.minute) >= self.checkin_at:
            due = 1
        if (now.hour, now.minute) >= self.evening_at:
            due = 2
        if not due:
            return
        want = [w for w in ("fog", "sleep") if asked.get(w, 0) < (due if w == "fog" else 1)]
        if not want:
            return
        miss = await self._missing_today()
        for w in want:
            if miss.get(w):
                await self.ask(w)
            asked[w] = due if w == "fog" else 1
        self.store.kv_set("patterns_asked", asked)

    async def on_action(self, action):
        """A tapped notification button (mobile_app_notification_action)."""
        m = re.fullmatch(r"HM_(FOG|SLEEP)_(\d+(?:\.\d+)?)", action or "")
        if not m:
            return None
        what, v = m.group(1).lower(), float(m.group(2))
        now = datetime.now()
        if what == "fog":
            await self.ha.call("input_number", "set_value", {"entity_id": E["fog"], "value": v})
            self.store.remember(text=f"Brain fog {v:.0f}/5 on {now:%a %b %d} (check-in).",
                                title=f"Brain fog {v:.0f}/5", shelf="body/fog", source="check-in",
                                meta={"fog": v, "day": now.date().isoformat()})
        else:
            self.store.remember(text=f"Slept about {v:g} hours the night before {now:%a %b %d} ({v:g} hours), "
                                     f"from a check-in.", title=f"Sleep about {v:g} h", shelf="body/sleep",
                                source="check-in", meta={"sleep": v, "day": now.date().isoformat()})
        try:
            await self.refresh(1)
        except Exception:  # noqa: BLE001
            pass
        return {what: v}

    # ---- the clock -----------------------------------------------------------------------------------------------------
    async def tick(self):
        """Called every few minutes from the upkeep loop."""
        now = datetime.now()
        if now.minute < 10 and self.store.kv_get("patterns_wx_hour") != now.strftime("%Y%m%d%H"):
            await self.snapshot_weather()
            self.store.kv_set("patterns_wx_hour", now.strftime("%Y%m%d%H"))
        if time.time() - self.last_refresh > 3 * 3600:
            await self.refresh(3)
            self.file_weather(now.date() - timedelta(days=1))
        await self.checkin_tick()
        week = now.strftime("%G-W%V")
        if (now.weekday() == self.report_day and (now.hour, now.minute) >= self.report_at
                and self.store.kv_get("patterns_report_week") != week):
            self.store.kv_set("patterns_report_week", week)
            await self.refresh(3)
            await self.send_report()


def caffeinated(text):
    return bool(CAFFEINE.search(text or "")) and not NO_CAFFEINE.search(text or "")
