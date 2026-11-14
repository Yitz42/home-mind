"""Brain-only benchmark across many seeds, run in-process (no Home Assistant, no model).

  python tests/bench.py                 # tuning seeds 1-20
  python tests/bench.py --holdout       # held-out seeds 1001-1020 (look at these only to confirm a change)
  python tests/bench.py --fails update  # print wrong answers for one suite
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "home_mind", "rootfs", "app"))
from homemind.mind import Mind  # noqa: E402

PLAN = [("recall", {}), ("update", {}), ("update", {"keyed": True}), ("locate", {}), ("tasks", {}),
        ("scale", {}), ("update", {"n": 60})]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true")
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--fails")
    ap.add_argument("--json")
    a = ap.parse_args()
    seeds = range(1001, 1001 + a.seeds) if a.holdout else range(1, 1 + a.seeds)
    m = Mind(tempfile.mkdtemp(), None)
    out = {}
    for suite, kw in PLAN:
        if a.fails and suite != a.fails:
            continue
        label = suite + "".join(f" {k}={v}" for k, v in kw.items())
        agg = {}
        for s in seeds:
            r = await m.run_test_now(suite, "brain", seed=s, n=kw.get("n"), keyed=kw.get("keyed", False))
            for k in ("accuracy", "hit1", "hit5", "stale_above", "shelf_ok"):
                if k in r["summary"]:
                    agg.setdefault(k, []).append(r["summary"][k])
            if a.fails:
                for d in r["details"]:
                    if not d.get("correct"):
                        print(json.dumps({k: d.get(k) for k in ("q", "gold", "answer", "rank", "stale_above")}))
        out[label] = {k: round(sum(v) / len(v), 3) for k, v in agg.items()}
        print(f"{label:22s} " + "  ".join(f"{k} {v:.3f}" for k, v in out[label].items()))
    if a.json:
        json.dump(out, open(a.json, "w"), indent=1)


asyncio.run(main())
