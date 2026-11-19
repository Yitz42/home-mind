"""Worsening: see the doctor (design spec step 31).

A pre-set alarm on the trend of rough days and brain fog, never a model's judgement:
  - each day: rough (1 if the check-in was rough, 0 if good or low, left out if unknown) and fog (the daily record's
    fog score, left out if missing)
  - an EWMA (exponentially weighted moving average, weight 0.2) of each, against the person's own baseline: the 60
    days before the last 14
  - it fires when an EWMA has been above baseline + L x its spread for 7 of the last 14 days AND the last 14 days are
    worse on average than the baseline. L is set so a steady person sees about one false alarm a year. The spread
    allows for days carrying over (lag-1 autocorrelation of the baseline), and for rough days it is never below that
    of a 10% rate, and at least 3 rough days in 14 (2 more than usual) are needed: one rough day after a calm
    stretch is not a trend.
  - at least 30 days of data first, then a 30-day cooldown after it speaks
  - logged events (a cold, a trip) explain changes but never silence it

What it says, calmly, once: "This has been worse for two weeks. It may be worth booking your doctor." A fast decline
(bedbound, can't eat or drink, new confusion) is the red flags' job, the same day; this is for slow drifts.
"""
import math
import time
from datetime import date, timedelta

LAMBDA = 0.2
# Simulated (tests/test_deliberate.py): steady data gives about 1.1 false alarms a year across both measures; a rise
# in fog from 4 to 5.5 (of 10) is caught within six weeks 9 times in 10, a rise in rough days from 20% to 45% about
# half the time (fast declines are the red flags' job)
L_SIGMA = 2.2
BASELINE_DAYS = 60
RECENT_DAYS = 14
MIN_DAYS = 30
COOLDOWN_DAYS = 30
MESSAGE = "This has been worse for two weeks. It may be worth booking your doctor."


def ewma(values, lam=LAMBDA, start=None):
    """EWMA over a list (None = a missing day: the average carries on unchanged)."""
    out, z = [], start
    for v in values:
        if v is not None:
            z = v if z is None else lam * v + (1 - lam) * z
        out.append(z)
    return out


def _mean_sd(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None, None
    m = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    return m, sd


def _lag1(xs):
    """Lag-1 autocorrelation of the days that have values next to each other (0 if too few)."""
    pairs = [(a, b) for a, b in zip(xs, xs[1:]) if a is not None and b is not None]
    if len(pairs) < 10:
        return 0.0
    m = sum(a for a, _ in pairs) / len(pairs)
    num = sum((a - m) * (b - m) for a, b in pairs)
    den = sum((a - m) ** 2 for a, _ in pairs)
    return max(0.0, min(0.9, num / den)) if den else 0.0


def check_series(values, lam=LAMBDA, L=L_SIGMA):
    """values: one per day, oldest first (None = missing). Returns {"worse": bool, ...}."""
    n = len(values)
    if sum(v is not None for v in values) < MIN_DAYS or n < RECENT_DAYS + 16:
        return {"worse": False, "why": "not enough days yet"}
    base = values[max(0, n - RECENT_DAYS - BASELINE_DAYS):n - RECENT_DAYS]
    recent = values[n - RECENT_DAYS:]
    m, sd = _mean_sd(base)
    if m is None:
        return {"worse": False, "why": "not enough baseline"}
    binary = max(v for v in base if v is not None) <= 1 and all(v in (0, 1) for v in values if v is not None)
    if binary:
        # rough days (0/1): the spread of a yes/no day, never below that of a 10% rate, so a calm stretch doesn't
        # make one rough day look like a change
        sd = max(sd, math.sqrt(max(m, 0.1) * (1 - max(m, 0.1))))
    else:
        sd = max(sd, 0.3)                                   # never a zero spread
    r1 = _lag1(base)
    # days carry over (a bad day makes the next one likelier): the average moves more than for independent days,
    # so its spread is widened by the AR(1) factor for an EWMA (Lucas & Saccucci 1990)
    carry = (1 + (1 - lam) * r1) / (1 - (1 - lam) * r1) if r1 > 0 else 1.0
    limit = m + L * sd * math.sqrt(lam / (2 - lam) * carry)
    z = ewma(values[max(0, n - RECENT_DAYS - BASELINE_DAYS):], lam, start=m)[-RECENT_DAYS:]
    above = sum(1 for x in z if x is not None and x > limit)
    rm, _ = _mean_sd(recent)
    worse = above >= 7 and rm is not None and rm > m
    if binary and worse:
        # and enough rough days to mean something: at least 3 in the last 14, and at least 2 more than usual
        k = sum(1 for v in recent if v == 1)
        expected = m * sum(1 for v in recent if v is not None)
        worse = k >= 3 and k >= expected + 2
    return {"worse": worse, "baseline": round(m, 3), "recent": round(rm, 3) if rm is not None else None,
            "limit": round(limit, 3), "days_above": above}


class Worsening:
    KV = "worsening"

    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or time.time

    def _state(self):
        return dict(self.store.kv_get(self.KV) or {"history": {}, "last_alarm": None})

    def record(self, day_iso, rough=None, fog=None):
        """One day's values (rough 0/1, fog a number); kept for 400 days."""
        s = self._state()
        h = s.setdefault("history", {})
        h[day_iso] = {"rough": rough, "fog": fog}
        for k in sorted(h)[:-400]:
            h.pop(k)
        self.store.kv_set(self.KV, s)

    def series(self, field, days=BASELINE_DAYS + RECENT_DAYS + 30):
        h = self._state().get("history", {})
        end = date.fromtimestamp(self.clock())
        out = []
        for i in range(days - 1, -1, -1):
            d = (end - timedelta(days=i)).isoformat()
            out.append((h.get(d) or {}).get(field))
        return out

    def check(self):
        """Should it speak today? Returns {"alarm": bool, "message"?, "rough": ..., "fog": ...}."""
        s = self._state()
        last = s.get("last_alarm")
        rough, fog = check_series(self.series("rough")), check_series(self.series("fog"))
        out = {"rough": rough, "fog": fog, "alarm": False}
        if last and self.clock() - last < COOLDOWN_DAYS * 86400:
            out["cooldown_until"] = last + COOLDOWN_DAYS * 86400
            return out
        if rough["worse"] or fog["worse"]:
            out.update(alarm=True, message=MESSAGE)
            s["last_alarm"] = self.clock()
            self.store.kv_set(self.KV, s)
        return out

    def collect(self, app):
        """Yesterday's values from the check-in and the daily record (called once a day)."""
        y = date.fromtimestamp(self.clock()) - timedelta(days=1)
        rough = fog = None
        day = getattr(app, "day", None)
        if day is not None:
            st = ((day._all().get(y.isoformat()) or {}).get("_") or {}).get("state")
            people = {k: v for k, v in (day._all().get(y.isoformat()) or {}).items() if not k.startswith("?")}
            states = [v.get("state") for v in people.values() if v.get("state") in ("good", "low", "rough")]
            st = st if st in ("good", "low", "rough") else (max(states, key=("good", "low", "rough").index)
                                                            if states else None)
            rough = None if st is None else int(st == "rough")
        pat = getattr(app, "patterns", None)
        if pat is not None:
            try:
                row = next((r for r in pat.rows(3) if r.get("day") == y.isoformat()), None)
                fog = row.get("fog") if row else None
            except Exception:  # noqa: BLE001
                fog = None
        self.record(y.isoformat(), rough, fog)
        return {"day": y.isoformat(), "rough": rough, "fog": fog}
