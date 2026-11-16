"""Comfort learning against a simulated person (no Home Assistant).

The person has hidden preferences per situation and one hidden body effect: after short sleep they want it warmer.
Other signals (body fat, HRV, steps, weight) are noise. Their room follows whatever target is set, with some error.
They vote cold/hot when the room is more than 1.5 °F off what they want, sometimes 'ok', and 10% of votes are wrong.

Compared over 21 days: the old fixed settings (68 night / 72 morning / 71 day) vs the learning brain.

  python tests/test_comfort.py [--seeds 10]
"""
import argparse
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "home_mind", "rootfs", "app"))
from homemind.comfort import PRIOR, ComfortModel  # noqa: E402

CHECKS = {"night": 3, "morning": 2, "day": 5}   # moments per day the person notices the temperature


def person(rng):
    return {"night": rng.uniform(65, 69), "morning": rng.uniform(70, 75), "day": rng.uniform(68.5, 73),
            "sleep_effect": rng.choice([0.0, 0.6, 0.9])}   # °F warmer per hour of sleep under 7


def run(seed, days=21, learn=True):
    rng = random.Random(seed)
    P = person(rng)
    votes, uncomfortable, total, err = [], 0, 0, []
    for day in range(days):
        sleep = max(3.0, min(9.0, rng.gauss(6.5, 1.3)))
        sig = {"sleep_hours": round(sleep, 1), "hrv": round(rng.gauss(45, 10)), "steps": round(rng.gauss(6000, 2500)),
               "body_fat": round(rng.gauss(23, 0.4), 1), "weight": round(rng.gauss(168, 1.0), 1),
               "resting_hr": round(rng.gauss(70, 5)), "humidity": round(rng.gauss(45, 5))}
        model = ComfortModel(votes) if learn else None
        day_sigs = model.signals() if learn else None     # signal tests once a day
        for sit, n in CHECKS.items():
            want = P[sit] + P["sleep_effect"] * max(0.0, 7 - sleep)
            target = model.target(sit, sig, day_sigs)[0] if learn else PRIOR[sit]
            target = max(64, min(76, target))
            for _ in range(n):
                room = round(target + rng.gauss(0, 0.8), 1)
                total += 1
                off = room - want
                if abs(off) > 1.5:
                    uncomfortable += 1
                    feeling = "cold" if off < 0 else "hot"
                elif rng.random() < 0.2:
                    feeling = "ok"
                else:
                    continue
                if rng.random() < 0.10:
                    feeling = rng.choice(["cold", "hot", "ok"])
                votes.append({"feeling": feeling, "room": room, "situation": sit, "signals": sig})
                if learn:
                    model = ComfortModel(votes)
                    target = max(64, min(76, model.target(sit, sig, day_sigs)[0]))
        if learn and day == days - 1:
            m = ComfortModel(votes)
            err = [abs(m.pref(s) - P[s]) for s in PRIOR]
    out = {"uncomfortable": uncomfortable / total, "votes": len(votes), "pref_err": sum(err) / len(err) if err else None,
           "effect": P["sleep_effect"]}
    if learn:
        sigs = ComfortModel(votes).signals()
        out["confirmed"] = sorted(k for k, s in sigs.items() if s["confirmed"])
        out["sleep_slope"] = sigs["sleep_hours"].get("slope")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--start", type=int, default=1)
    a = ap.parse_args()
    fixed, brain, errs = [], [], []
    right_sig = wrong_sig = missed = 0
    for s in range(a.start, a.start + a.seeds):
        f, b = run(s, learn=False), run(s)
        fixed.append(f["uncomfortable"])
        brain.append(b["uncomfortable"])
        errs.append(b["pref_err"])
        has = "sleep_hours" in b["confirmed"]
        right_sig += has and b["effect"] > 0
        missed += (not has) and b["effect"] > 0
        wrong_sig += len([c for c in b["confirmed"] if c != "sleep_hours"]) + (has and b["effect"] == 0)
        print(f"seed {s:3d}: uncomfortable fixed {f['uncomfortable']:.0%} -> brain {b['uncomfortable']:.0%}; "
              f"pref error {b['pref_err']:.2f}°F; votes {b['votes']}; sleep effect {b['effect']} "
              f"-> confirmed {b['confirmed'] or '-'} (slope {b['sleep_slope']})")
    print(f"\nmean uncomfortable: fixed {sum(fixed)/len(fixed):.0%}, brain {sum(brain)/len(brain):.0%}")
    print(f"real sleep effect found {right_sig}, missed {missed}; false signals confirmed {wrong_sig}")
    # the brain must clearly beat a fixed schedule and rarely confirm a signal that isn't there (CI fails otherwise)
    fails = []
    if not sum(brain) / len(brain) <= 0.25:
        fails.append("brain uncomfortable share above 25%")
    if not sum(fixed) / len(fixed) - sum(brain) / len(brain) >= 0.15:
        fails.append("brain not 15+ points better than the fixed schedule")
    if not sum(errs) / len(errs) < 1.0:
        fails.append("learned preferences off by 1°F or more on average")
    if wrong_sig > 2:       # 0-2 per 10 worlds on any seed window; more means the signal test got loose
        fails.append("too many false signals confirmed")
    if not right_sig:
        fails.append("no real sleep effect found")
    print("\n".join("FAIL " + f for f in fails) or "COMFORT OK")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
