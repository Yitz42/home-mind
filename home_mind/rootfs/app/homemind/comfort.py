"""Room comfort: learns when Yitz feels cold or hot and runs the room through Home Assistant variables.

How it works
  1. A vote (cold / hot / ok) is stored with a snapshot of that moment: room temperature and humidity, the target, what
     the heater and fan are doing, the situation (night / morning / day), and the latest watch and scale numbers.
  2. For each situation it finds the preferred temperature that fits the votes best: a cold vote at 69 says "warmer
     than 70", a hot vote at 75 says "cooler than 74", an ok vote says "about here". Contradicting votes are weighed,
     not obeyed blindly, and a weak pull toward the old fixed setting keeps it sane with few votes.
  3. Each body/room signal (HRV, resting HR, sleep, readiness, steps, humidity, weight, body fat, body water) is
     tested: does it explain when he wants it warmer or cooler than usual? A signal is used only when it predicts
     held-out votes clearly better than the plain preference does (leave-one-out), with enough votes. Otherwise it is
     reported as not confirmed. (The same confirmation rule as the v7 structure.)
  4. Control never touches a device. It writes Home Assistant variables (input_number helpers); a driver automation in
     HA applies them to the heater, fan and humidifier. Limits are HA variables too, and input_boolean
     mind_comfort_enabled is the breaking point: off, and the brain writes nothing. If the room leaves the limits by
     more than BREAK_DEGREES for BREAK_MINUTES while it is in control, it switches itself off and says why.
"""
import statistics as st
import time
from datetime import datetime, timedelta

SIGNALS = {
    "hrv": "input_number.steady_hrv", "resting_hr": "input_number.steady_resting_hr",
    "sleep_hours": "input_number.steady_sleep_hours", "readiness": "input_number.steady_readiness",
    "steps": "input_number.steady_steps", "night_low_hr": "input_number.steady_night_low_hr",
    "weight": "sensor.yitz_weight", "body_fat": "sensor.yitz_body_fat", "body_water": "sensor.yitz_body_water",
}
ROOM = {"climate": "climate.bedroom_temp", "temp_sensor": "sensor.dyson_c2a_us_mdb9996a_temperature",
        "humidifier": "humidifier.humidifier_bedroom", "fan": "fan.dyson_c2a_us_mdb9996a",
        "phone_battery": "sensor.yitzs_iphone_battery_state", "alarm": "input_datetime.alarm"}
VARS = {"enabled": "input_boolean.mind_comfort_enabled", "target": "input_number.mind_target_temp",
        "fan": "input_number.mind_fan_speed", "humidity": "input_number.mind_humidity_target",
        "min": "input_number.mind_temp_min", "max": "input_number.mind_temp_max",
        "hum_min": "input_number.mind_humidity_min", "hum_max": "input_number.mind_humidity_max",
        "sleep": "input_number.sleep_temperature", "wake": "input_number.wake_temperature"}
VOTE_BUTTONS = {"cold": "input_button.comfort_cold", "hot": "input_button.comfort_hot", "ok": "input_button.comfort_ok"}
PRIOR = {"night": 68.0, "morning": 72.0, "day": 71.0}
FEELINGS = ("cold", "hot", "ok")
MARGIN = 1.0           # a cold vote at T says: preferred is at least T + MARGIN
MIN_SIGNAL_VOTES = 10
SIGNAL_GAIN = 0.15     # held-out error must drop by this share for a signal to be confirmed
SIGNAL_CAP = 3.0       # most a signal may move the preference (°F)
BREAK_DEGREES, BREAK_MINUTES = 3.0, 30
NUDGE, NUDGE_MINUTES = 1.5, 90


def situation(now, alarm_hm=None, bedtime_hm=(22, 0)):
    """night: bedtime until 90 min before the alarm; morning: 90 min before to 60 min after the alarm; day: the rest."""
    ah, am = alarm_hm or (7, 0)
    alarm = now.replace(hour=ah, minute=am, second=0, microsecond=0)
    if now - alarm > timedelta(hours=12):
        alarm += timedelta(days=1)
    elif alarm - now > timedelta(hours=12):
        alarm -= timedelta(days=1)
    if alarm - timedelta(minutes=90) <= now <= alarm + timedelta(minutes=60):
        return "morning"
    bed = now.replace(hour=bedtime_hm[0], minute=bedtime_hm[1], second=0, microsecond=0)
    if now >= bed or now < alarm - timedelta(minutes=90) and now.hour < 12:
        return "night"
    return "day"


def fit_preference(votes, prior, shift=None):
    """The preferred temperature that best fits the votes. Loss: a cold vote at T costs max(0, T + MARGIN - p), a hot
    vote max(0, p - (T - MARGIN)), an ok vote 0.7*|p - T|, plus a weak pull 0.3*|p - prior|. The loss is convex and
    piecewise linear, so the best p is found exactly by sweeping its kinks (no grid). shift(v) moves a vote's
    temperature first (used to take a signal's effect out)."""
    kinks = [(prior, 0.6)]           # (where, how much the slope rises there)
    slope = -0.3
    for v in votes:
        t = v["room"] - (shift(v) if shift else 0.0)
        if v["feeling"] == "cold":
            kinks.append((t + MARGIN, 1.0))
            slope -= 1.0
        elif v["feeling"] == "hot":
            kinks.append((t - MARGIN, 1.0))
        else:
            kinks.append((t, 1.4))
            slope -= 0.7
    for where, rise in sorted(kinks):
        slope += rise
        if slope >= -1e-9:
            return max(58.0, min(82.0, where))
    return 82.0


def vote_loss(v, p):
    t = v["room"]
    if v["feeling"] == "cold":
        return max(0.0, t + MARGIN - p)
    if v["feeling"] == "hot":
        return max(0.0, p - (t - MARGIN))
    return 0.7 * abs(p - t)


B_GRID = [round(-1.5 + 0.1 * i, 2) for i in range(31)]   # °F per unit of a signal, scaled per signal below


class ComfortModel:
    """Pure learning, no Home Assistant: fit preferences per situation and test signals."""

    def __init__(self, votes, priors=None):
        self.votes = [v for v in votes if v.get("room") is not None and v.get("feeling") in FEELINGS]
        self.priors = dict(PRIOR, **(priors or {}))
        self._sigs = None

    def pref(self, sit, exclude=None):
        vs = [v for v in self.votes if v["situation"] == sit and v is not exclude]
        return fit_preference(vs, self.priors[sit])

    def _fit(self, votes, name, b, mx):
        """Preferences per situation when the signal moves the preference by b * (x - mx)."""
        shift = (lambda v: b * ((v.get("signals") or {}).get(name, mx) - mx)) if name else None
        return {s: fit_preference([v for v in votes if v["situation"] == s], self.priors[s], shift) for s in self.priors}

    def _loss(self, votes, prefs, name, b, mx):
        return sum(vote_loss(v, prefs[v["situation"]] + (b * ((v.get("signals") or {}).get(name, mx) - mx) if name else 0))
                   for v in votes)

    def _best_b(self, votes, name, mx, scale):
        best = (None, 0.0)
        for g in B_GRID:
            b = g / scale
            prefs = self._fit(votes, name, b, mx)
            loss = self._loss(votes, prefs, name, b, mx)
            if best[0] is None or loss < best[0] - 1e-9:
                best = (loss, b)
        return best[1]

    def signals(self, folds=5):
        """Each signal is tested by fitting it together with the preferences, then checking on held-out votes (k-fold,
        votes interleaved in time) whether it predicts them better than the preferences alone. Confirmed only with
        enough votes, a clear drop in held-out loss, and the same direction in most folds."""
        if self._sigs is not None:
            return self._sigs
        out = {}
        for name in list(SIGNALS) + ["humidity"]:
            vs = [v for v in self.votes if (v.get("signals") or {}).get(name) is not None]
            info = {"n": len(vs), "confirmed": False}
            if len(vs) >= MIN_SIGNAL_VOTES:
                xs = [v["signals"][name] for v in vs]
                mx, sd = st.fmean(xs), (st.pstdev(xs) or 1.0)
                base = sig = 0.0
                signs = []
                for k in range(folds):
                    train = [v for i, v in enumerate(vs) if i % folds != k]
                    test = [v for i, v in enumerate(vs) if i % folds == k]
                    if not test or not train:
                        continue
                    base += self._loss(test, self._fit(train, None, 0, mx), None, 0, mx)
                    b = self._best_b(train, name, mx, sd)
                    sig += self._loss(test, self._fit(train, name, b, mx), name, b, mx)
                    signs.append(_sign(b))
                b = self._best_b(vs, name, mx, sd)
                agree = sum(1 for x in signs if x == _sign(b) and x != 0)
                info.update(slope=round(b, 4), mean=round(mx, 3), per_sd=round(b * sd, 2),
                            loss_without=round(base, 2), loss_with=round(sig, 2), folds_agree=agree,
                            confirmed=b != 0 and sig <= (1 - SIGNAL_GAIN) * base and agree >= folds - 1)
            out[name] = info
        self._sigs = out
        return out

    def target(self, sit, now_signals, sigs=None):
        """Preferred temperature now: the situation's preference refit with the one best confirmed signal."""
        sigs = sigs or self.signals()
        conf = [(k, s) for k, s in sigs.items() if s["confirmed"] and now_signals.get(k) is not None]
        if not conf:
            return self.pref(sit), None, sigs
        k, s = min(conf, key=lambda kv: kv[1]["loss_with"] / max(kv[1]["loss_without"], 1e-9))
        prefs = self._fit(self.votes, k, s["slope"], s["mean"])
        adj = max(-SIGNAL_CAP, min(SIGNAL_CAP, s["slope"] * (now_signals[k] - s["mean"])))
        return prefs[sit] + adj, {"signal": k, "adjust": round(adj, 2)}, sigs


def _sign(x):
    return (x > 0) - (x < 0)


def _slope(rows):
    xs = [x for x, _ in rows]
    ys = [y for _, y in rows]
    mx, my = st.fmean(xs), st.fmean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    return (sum((x - mx) * (y - my) for x, y in rows) / vx if vx > 1e-12 else 0.0), mx


def _corr(rows):
    xs = [x for x, _ in rows]
    ys = [y for _, y in rows]
    if len(rows) < 3 or st.pstdev(xs) < 1e-9 or st.pstdev(ys) < 1e-9:
        return 0.0
    mx, my = st.fmean(xs), st.fmean(ys)
    return sum((x - mx) * (y - my) for x, y in rows) / (len(rows) * st.pstdev(xs) * st.pstdev(ys))


def _num(s, attr=None):
    if not s:
        return None
    v = s.get("attributes", {}).get(attr) if attr else s.get("state")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Comfort:
    """The live part: reads Home Assistant, stores votes in the mind, writes the variables."""

    def __init__(self, mind, ha, clock=time.time):
        self.mind, self.ha, self.clock = mind, ha, clock
        self.store = mind.store
        self.last = {}
        self.out_since = None

    # ---- reading ----------------------------------------------------------------------------------------------
    async def snapshot(self):
        ids = list(SIGNALS.values()) + list(ROOM.values()) + list(VARS.values())
        states = {i: await self.ha.state(i) for i in ids}
        clim = states.get(ROOM["climate"])
        room = _num(clim, "current_temperature") or _num(states.get(ROOM["temp_sensor"]))
        alarm = states.get(ROOM["alarm"])
        ahm = None
        if alarm and alarm.get("state") and ":" in alarm["state"]:
            try:
                h, m = alarm["state"].split(":")[:2]
                ahm = (int(h), int(m))
            except ValueError:
                pass
        now = datetime.fromtimestamp(self.clock())
        sig = {k: _num(states.get(e)) for k, e in SIGNALS.items()}
        sig["humidity"] = _num(states.get(ROOM["humidifier"]), "current_humidity")
        return {"room": room, "target": _num(clim, "temperature"),
                "heating": (clim or {}).get("attributes", {}).get("hvac_action"),
                "fan": (states.get(ROOM["fan"]) or {}).get("state"),
                "phone": (states.get(ROOM["phone_battery"]) or {}).get("state"),
                "situation": situation(now, ahm), "hour": now.hour, "signals": sig,
                "vars": {k: _num(states.get(e)) if k != "enabled" else (states.get(e) or {}).get("state")
                         for k, e in VARS.items()}}

    def votes(self):
        rows = self.store.db.execute("SELECT id, created, meta FROM items WHERE kind='episode' AND shelf='comfort/votes' "
                                     "AND status IN ('active','faded') ORDER BY created").fetchall()
        import json
        out = []
        for r in rows:
            m = json.loads(r["meta"])
            out.append({"id": r["id"], "ts": r["created"], **m})
        return out

    # ---- votes ------------------------------------------------------------------------------------------------
    async def vote(self, feeling, source="api"):
        if feeling not in FEELINGS:
            raise ValueError("feeling must be cold, hot or ok")
        snap = await self.snapshot()
        meta = {"feeling": feeling, "source": source, "room": snap["room"], "target": snap["target"],
                "heating": snap["heating"], "fan": snap["fan"], "situation": snap["situation"], "hour": snap["hour"],
                "signals": snap["signals"]}
        words = {"cold": "felt cold", "hot": "felt hot", "ok": "felt just right"}[feeling]
        r = self.store.remember(text=f"Yitz {words} at {snap['room']}°F ({snap['situation']}).",
                                title=f"Comfort: {words} at {snap['room']}°F", kind="episode",
                                shelf="comfort/votes", tags=["comfort", feeling, snap["situation"]], source=source,
                                meta=meta)
        self.last["vote"] = {"feeling": feeling, "ts": self.clock(), "id": r["id"]}
        act = await self.cycle(reason=f"vote: {feeling}")
        return {"vote": r["id"], "snapshot": meta, "action": act}

    async def poll_buttons(self):
        """Votes from input_buttons (pressed by Jeeves, the watch or a dashboard): a new press time is a new vote."""
        seen = self.store.kv_get("comfort_buttons", {})
        out = []
        for feeling, ent in VOTE_BUTTONS.items():
            s = await self.ha.state(ent)
            if not s or s.get("state") in (None, "unknown", "unavailable"):
                continue
            if seen.get(ent) is None:
                seen[ent] = s["state"]          # first sight: remember, don't count the old press
                continue
            if s["state"] != seen[ent]:
                seen[ent] = s["state"]
                out.append(await self.vote(feeling, source="button"))
        self.store.kv_set("comfort_buttons", seen)
        return out

    # ---- control ----------------------------------------------------------------------------------------------
    async def cycle(self, reason="cycle"):
        snap = await self.snapshot()
        v = snap["vars"]
        model = self.model()
        sit = snap["situation"]
        pref, used, sigs = model.target(sit, snap["signals"])
        lo, hi = v.get("min") or 64.0, v.get("max") or 76.0
        state = {"situation": sit, "room": snap["room"], "preferred": round(pref, 2), "signal_used": used,
                 "limits": [lo, hi], "enabled": v.get("enabled"), "votes": len(model.votes)}
        self.last["status"] = state
        if v.get("enabled") != "on":
            state["action"] = "none: mind_comfort_enabled is off"
            return state
        # breaking point: room far outside the limits for too long -> stop and say so
        room = snap["room"]
        if room is not None and (room < lo - BREAK_DEGREES or room > hi + BREAK_DEGREES):
            self.out_since = self.out_since or self.clock()
            if self.clock() - self.out_since >= BREAK_MINUTES * 60:
                await self.ha.call("input_boolean", "turn_off", {"entity_id": VARS["enabled"]})
                msg = (f"Home Mind turned comfort control off: the bedroom has been at {room}°F, outside your limits "
                       f"{lo:g}-{hi:g}°F by more than {BREAK_DEGREES:g}° for {BREAK_MINUTES} minutes (window open? "
                       "heater fault?). Turn input_boolean.mind_comfort_enabled back on when it's fixed.")
                await self.ha.notify("Home Mind comfort paused", msg, "home_mind_comfort")
                self._log("pause", msg)
                state["action"] = "paused (breaking point)"
                return state
        else:
            self.out_since = None
        writes, why = {}, []
        lv = self.last.get("vote")
        recent = lv and self.clock() - lv["ts"] < NUDGE_MINUTES * 60
        desired = pref
        if recent and lv["feeling"] == "cold" and room is not None:
            desired = max(desired, room + NUDGE)
            why.append(f"felt cold {int((self.clock() - lv['ts']) / 60)} min ago")
        if recent and lv["feeling"] == "hot" and room is not None:
            desired = min(desired, room - NUDGE)
            why.append(f"felt hot {int((self.clock() - lv['ts']) / 60)} min ago")
        desired = round(max(lo, min(hi, desired)) * 2) / 2
        if sit != "morning":   # the wake automation owns the morning; it reads wake_temperature
            if v.get("target") is None or abs(desired - v["target"]) >= 0.5:
                writes["target"] = desired
        night_pref = round(max(lo, min(hi, model.target("night", snap["signals"])[0])))
        wake_pref = round(max(lo, min(hi, model.target("morning", snap["signals"])[0])))
        if v.get("sleep") is None or abs(night_pref - v["sleep"]) >= 1:
            writes["sleep"] = night_pref
        if v.get("wake") is None or abs(wake_pref - v["wake"]) >= 1:
            writes["wake"] = wake_pref
        fan = 40.0 if (recent and lv["feeling"] == "hot" and room is not None and room > desired + 0.5) else 0.0
        if v.get("fan") is None or fan != v["fan"]:
            writes["fan"] = fan
        hl, hh = v.get("hum_min") or 40.0, v.get("hum_max") or 50.0
        hum = round((hl + hh) / 2)
        if v.get("humidity") is None or abs(hum - v["humidity"]) >= 1:
            writes["humidity"] = hum
        for k, val in writes.items():
            await self.ha.call("input_number", "set_value", {"entity_id": VARS[k], "value": val})
        if writes:
            base = (f"{sit}: preferred {pref:.1f}°F from {len([x for x in model.votes if x['situation'] == sit])} "
                    f"votes" + (f", {used['signal']} moved it {used['adjust']:+.1f}" if used else ""))
            msg = "; ".join([base] + why) + " -> " + ", ".join(f"{k} {val:g}" for k, val in writes.items())
            self._log(reason, msg)
            state["action"] = msg
        else:
            state["action"] = "no change"
        state["writes"] = writes
        await self.ha.sensor("sensor.home_mind_comfort", desired,
                             {"friendly_name": "Home Mind comfort target", "unit_of_measurement": "°F",
                              "icon": "mdi:thermometer-lines", "situation": sit, "room": room,
                              "preferred": {s: model.pref(s) for s in PRIOR}, "votes": len(model.votes),
                              "signal_used": used, "last_action": state["action"],
                              "signals": {k: {"n": s["n"], "confirmed": s["confirmed"], **({"corr": s["corr"]} if "corr" in s else {})}
                                          for k, s in sigs.items()}})
        return state

    def model(self):
        """The fitted model, with its signal tests cached until a new vote arrives."""
        votes = self.votes()
        key = (len(votes), votes[-1]["id"] if votes else None)
        if self.last.get("model_key") != key:
            m = ComfortModel(votes)
            m.signals()
            self.last.update(model_key=key, model=m)
        return self.last["model"]

    def status(self):
        model = self.model()
        sigs = model.signals()
        return {"preferred": {s: model.pref(s) for s in PRIOR},
                "votes": {s: {f: sum(1 for v in model.votes if v["situation"] == s and v["feeling"] == f)
                              for f in FEELINGS} for s in PRIOR},
                "signals": sigs, "last": self.last.get("status"),
                "recent_actions": [dict(r) for r in self.store.db.execute(
                    "SELECT created, title, body FROM items WHERE shelf='comfort/actions' ORDER BY id DESC LIMIT 10")]}

    def _log(self, reason, msg):
        self.store.remember(text=msg, title=f"Comfort action ({reason})", kind="episode", shelf="comfort/actions",
                            tags=["comfort", "action"], source="comfort")
