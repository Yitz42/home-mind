"""Your experiments (design spec step 30; open decision 9's crash episode; phase 14).

Only three kinds of change can be tried: reducing load, sleep timing, meal timing. Never a medicine or a supplement
(a plan that mentions one is refused). Every experiment is a written plan, agreed on a clear day with 24 hours to
change your mind (consent.py), and fixed before it starts:
  - the change (B) against your usual (A), in blocks of at least 7 days, at least 4 A/B pairs, the order inside
    each pair drawn at random (that is what the randomisation test rests on), a washout at the start of each block
    (its days don't count), start dates set by the calendar (the next Monday), not by how you feel;
  - one primary outcome (fog, readiness or crash episodes), one analysis, everything else a check only;
  - crash episodes as the safety outcome, counted per block, with the episode written down first: it starts on the
    first rough check-in after at least 2 days that weren't rough and ends on the next day that isn't rough
    (the starting value of open decision 9; a clinician's definition replaces it; days with no check-in are unknown);
  - the smallest effect it could detect, said before it starts.
Adherence is logged each day ("did you do it today?", one tap). A new medicine ends the current A/B pair and is
flagged. Travel mode pauses it. A B block with 2 or more crash episodes more than its A block stops the experiment.

The analysis is exact: every way the A/B order inside the pairs could have fallen is tried, and the share at least
as far from zero as what happened is the p-value. Results are said plainly, with the smallest detectable effect next
to them, and "no clear difference" is a result.
"""
import itertools
import json
import random
import re
import statistics
import time
from datetime import date, timedelta

KINDS = ("reduce_load", "sleep_timing", "meal_timing")
OUTCOMES = ("fog", "readiness", "crash_episodes")
FORBIDDEN = re.compile(r"\b(?:medic\w*|meds?|pills?|tablets?|dose\w*|supplements?|vitamin\w*|magnesium|melatonin|"
                       r"caffeine pills?|prescri\w*|drug\w*|\w+olol|ivabradine)\b", re.I)
MIN_BLOCK, MIN_PAIRS = 7, 4
DEFAULT_PAIRS = 6          # the exact test's smallest p is 2 / 2^pairs: 4 pairs can't go below 0.125, 6 reach 0.031
CRASH_STOP = 2


def crash_starts(days):
    """days: [(iso date, "good"|"low"|"rough"|"unknown")] in order. The dates crash episodes start on."""
    out, calm = [], 0
    in_crash = False
    for d, s in days:
        if s == "rough":
            if not in_crash and calm >= 2:
                out.append(d)
            in_crash = True
            calm = 0
        elif s in ("good", "low"):
            in_crash = False
            calm += 1
        else:                       # unknown: not counted either way, and it breaks a run of calm days
            calm = 0
    return out


def next_monday(today):
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def schedule(start, pairs, block_days, rng):
    """[{"pair", "arm", "start", "end"}]: each pair's order drawn at random."""
    out, d = [], start
    for p in range(pairs):
        order = ["A", "B"] if rng.random() < 0.5 else ["B", "A"]
        for arm in order:
            out.append({"pair": p, "arm": arm, "start": d.isoformat(),
                        "end": (d + timedelta(days=block_days - 1)).isoformat()})
            d += timedelta(days=block_days)
    return out


def detectable(sd, pairs, z=2.8):
    """The smallest difference in the outcome's mean between arms it could reliably see (80% power, 5%), from the
    day-to-day spread before it starts: about 2.8 standard errors of a paired difference of block means."""
    if not sd or pairs < 1:
        return None
    return round(z * sd * (2 / pairs) ** 0.5, 2)


def exact_p(diffs):
    """Randomisation test on per-pair differences (B - A): every sign flip, the share at least as extreme."""
    if not diffs:
        return None
    obs = abs(statistics.mean(diffs))
    n = k = 0
    for signs in itertools.product((1, -1), repeat=len(diffs)):
        n += 1
        k += abs(statistics.mean([s * d for s, d in zip(signs, diffs)])) >= obs - 1e-12
    return k / n


class Experiments:
    KV = "experiment_v2"

    def __init__(self, store, day=None, patterns=None, tracking=None, clock=None, rng=None):
        self.store = store
        self.day = day
        self.patterns = patterns
        self.tracking = tracking
        self.clock = clock or time.time
        self.rng = rng or random.Random()

    def current(self):
        return self.store.kv_get(self.KV)

    def plan(self, question, change, kind, outcome="fog", pairs=DEFAULT_PAIRS, block_days=MIN_BLOCK, washout_days=2,
             crash_definition=None):
        """A written plan, checked. Not started: it goes to consent first."""
        if kind not in KINDS:
            raise ValueError("an experiment can only reduce load, or change sleep timing or meal timing")
        if FORBIDDEN.search(f"{question} {change}"):
            raise ValueError("never a medicine or a supplement: that's for your doctor")
        if outcome not in OUTCOMES:
            raise ValueError(f"the primary outcome is one of {', '.join(OUTCOMES)}")
        pairs, block_days, washout_days = int(pairs), int(block_days), int(washout_days)
        if pairs < MIN_PAIRS or block_days < MIN_BLOCK:
            raise ValueError(f"at least {MIN_PAIRS} A/B pairs of at least {MIN_BLOCK} days each")
        if not 0 <= washout_days < block_days - 3:
            raise ValueError("the washout must leave at least 4 days of each block to count")
        sd = self._spread(outcome)
        start = next_monday(date.fromtimestamp(self.clock()))
        return {"question": question.strip(), "change": change.strip(), "kind": kind, "outcome": outcome,
                "pairs": pairs, "block_days": block_days, "washout_days": washout_days,
                "crash_definition": crash_definition or "starts on the first rough check-in after at least 2 days "
                                                        "that weren't rough; ends on the next day that isn't rough",
                "analysis": "per block; randomisation test over the A/B order inside each pair; one primary "
                            "outcome, everything else a check only",
                "start": start.isoformat(), "blocks": schedule(start, pairs, block_days, self.rng),
                "smallest_effect": detectable(sd, pairs) if outcome != "crash_episodes" else None,
                "smallest_effect_note": (f"about {detectable(sd, pairs)} points of {outcome}" if sd and
                                         outcome != "crash_episodes" else "too few days yet to say"),
                "smallest_p": round(2 / 2 ** pairs, 4),
                "power_note": None if 2 / 2 ** pairs <= 0.05 else
                f"with {pairs} pairs the test can't show a difference at the usual 5% line (its smallest p is "
                f"{2 / 2 ** pairs:.3f}); 6 pairs can",
                "planned": self.clock()}

    def _spread(self, outcome):
        if self.patterns is None or outcome == "crash_episodes":
            return None
        key = {"fog": "fog", "readiness": "readiness"}[outcome]
        vals = [r[key] for r in self.patterns.rows(60) if r.get(key) is not None]
        return statistics.pstdev(vals) if len(vals) >= 10 else None

    async def start(self, plan):
        """Called 24 hours after a yes on a clear day (consent kind experiment_start)."""
        if self.current() and not self.current().get("ended"):
            return {"error": "one experiment at a time"}
        e = dict(plan, started=self.clock(), adherence={}, broken_pairs=[], flags=[])
        self.store.kv_set(self.KV, e)
        self.store.remember(text=f"Experiment planned: {plan['change']} (blocks of {plan['block_days']} days, "
                                 f"{plan['pairs']} A/B pairs, outcome {plan['outcome']}, from {plan['start']}).",
                            title="Experiment started", shelf="body/experiments", source="experiments")
        return {"started": plan["start"]}

    def block_on(self, d):
        e = self.current()
        if not e:
            return None
        iso = d.isoformat() if hasattr(d, "isoformat") else d
        return next((b for b in e["blocks"] if b["start"] <= iso <= b["end"]), None)

    def adhered(self, d, yes):
        e = self.current()
        if not e:
            raise ValueError("no experiment running")
        e["adherence"][d.isoformat() if hasattr(d, "isoformat") else d] = bool(yes)
        self.store.kv_set(self.KV, e)

    def medicine_changed(self, name, d):
        """A new medicine: the current pair ends and is flagged."""
        e = self.current()
        b = self.block_on(d)
        if not e or not b:
            return None
        if b["pair"] not in e["broken_pairs"]:
            e["broken_pairs"].append(b["pair"])
            e["flags"].append(f"pair {b['pair'] + 1} ended: {name} started on {d}")
            self.store.kv_set(self.KV, e)
        return b["pair"]

    def _day_rows(self):
        rows = {r["day"]: r for r in (self.patterns.rows(200) if self.patterns is not None else [])}
        states = dict(self.store.kv_get("experiment_days") or {})    # kept here: the check-in keeps only 2 weeks
        if self.day is not None:
            for d, v in self.day._all().items():
                st = (v.get("_") or next(iter(v.values()), {}) if v else {}).get("state")
                if st:
                    states[d] = st
        return rows, states

    def analyse(self, today=None):
        e = self.current()
        if not e:
            return None
        rows, states = self._day_rows()
        today = today or date.fromtimestamp(self.clock())
        first = date.fromisoformat(e["blocks"][0]["start"])
        span = [(first + timedelta(days=i)) for i in range((today - first).days + 1)] if today >= first else []
        crashes = set(crash_starts([(d.isoformat(), states.get(d.isoformat(), "unknown")) for d in span]))
        travel = set((self.store.kv_get("experiment_travel_days") or []))
        blocks = []
        for b in e["blocks"]:
            s, en = date.fromisoformat(b["start"]), date.fromisoformat(b["end"])
            counted = [s + timedelta(days=i) for i in range(e["washout_days"], (en - s).days + 1)]
            counted = [d for d in counted if d <= today and d.isoformat() not in travel]
            vals = []
            for d in counted:
                r = rows.get(d.isoformat()) or {}
                v = r.get(e["outcome"]) if e["outcome"] != "crash_episodes" else None
                if v is not None:
                    vals.append(v)
            done = en <= today
            n_crash = sum(1 for d in counted if d.isoformat() in crashes)
            adh = [e["adherence"].get(d.isoformat()) for d in counted if d.isoformat() in e["adherence"]]
            blocks.append(dict(b, done=done, days=len(counted), crashes=n_crash,
                               mean=round(statistics.mean(vals), 2) if vals else None,
                               adherence=round(sum(adh) / len(adh), 2) if adh else None))
        diffs, safety = [], None
        for p in range(e["pairs"]):
            pb = {x["arm"]: x for x in blocks if x["pair"] == p}
            if len(pb) < 2 or not (pb["A"]["done"] and pb["B"]["done"]) or p in e["broken_pairs"]:
                continue
            if pb["B"]["crashes"] - pb["A"]["crashes"] >= CRASH_STOP:
                safety = f"pair {p + 1}: {pb['B']['crashes']} crash episodes with the change against " \
                         f"{pb['A']['crashes']} without"
            if e["outcome"] == "crash_episodes":
                diffs.append(pb["B"]["crashes"] - pb["A"]["crashes"])
            elif pb["A"]["mean"] is not None and pb["B"]["mean"] is not None:
                diffs.append(pb["B"]["mean"] - pb["A"]["mean"])
        p = exact_p(diffs) if len(diffs) >= 2 else None
        out = {"blocks": blocks, "pairs_complete": len(diffs), "difference": round(statistics.mean(diffs), 2)
               if diffs else None, "p": round(p, 3) if p is not None else None, "safety_stop": safety,
               "flags": e["flags"], "smallest_effect": e.get("smallest_effect")}
        if safety:
            out["text"] = f"Stopped for safety: {safety}."
        elif len(diffs) < e["pairs"] - len(e["broken_pairs"]):
            out["text"] = f"{len(diffs)} of {e['pairs']} pairs done."
        elif p is not None and p <= 0.05:
            out["text"] = (f"With the change, {e['outcome']} was {abs(out['difference'])} "
                           f"{'higher' if out['difference'] > 0 else 'lower'} on average (p = {out['p']}).")
        else:
            out["text"] = (f"No clear difference in {e['outcome']} (difference {out['difference']}, p = {out['p']}); "
                           f"it could only have seen about {e.get('smallest_effect') or '?'} or more.")
        return out

    def stop(self, why="stopped"):
        e = self.current()
        if not e:
            return None
        res = self.analyse()
        e["ended"] = self.clock()
        e["end_reason"] = why
        e["result"] = res
        self.store.kv_set(self.KV, e)
        self.store.remember(text=f"Experiment ended ({e['change']}): {res['text'] if res else why}",
                            title="Experiment result", shelf="body/experiments", source="experiments")
        return res

    def check(self, today=None):
        """Daily: travel days, a safety stop, the end."""
        e = self.current()
        if not e or e.get("ended"):
            return None
        today = today or date.fromtimestamp(self.clock())
        if self.day is not None:          # keep every day's check-in for the whole experiment
            kept = dict(self.store.kv_get("experiment_days") or {})
            for d, v in self.day._all().items():
                st = (v.get("_") or next(iter(v.values()), {}) if v else {}).get("state")
                if st:
                    kept[d] = st
            self.store.kv_set("experiment_days", kept)
        if self.tracking is not None and self.tracking.travelling():
            t = set(self.store.kv_get("experiment_travel_days") or [])
            t.add(today.isoformat())
            self.store.kv_set("experiment_travel_days", sorted(t))
        res = self.analyse(today)
        if res and res["safety_stop"]:
            return self.stop("safety")
        if today > date.fromisoformat(e["blocks"][-1]["end"]):
            return self.stop("done")
        return None


__all__ = ["Experiments", "crash_starts", "exact_p", "detectable", "schedule", "json"]
