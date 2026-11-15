"""Run the brain-only test suites on held-out seeds and fail if any drops below its floor.

    python3 tests/suites_check.py            # 20 held-out seeds per suite (what CI runs)
    python3 tests/suites_check.py --quick    # 5 seeds per suite
    python3 tests/suites_check.py --tuning   # the seeds the brain was tuned on (1-20)
"""
import asyncio
import collections
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "home_mind", "rootfs", "app"))
from homemind.mind import Mind  # noqa: E402

# suite: (questions per type, floor). Floors sit at the current scores; raise them when a suite improves.
SUITES = {"recall": (None, 1.0), "update": (None, 0.98), "locate": (None, 0.8), "tasks": (None, 1.0),
          "tricky": (4, 1.0), "tricky2": (4, 1.0), "hard": (3, 1.0), "hard2": (2, 1.0),
          "followup": (4, 1.0), "abstain": (None, 0.82),
          "recurring": (None, 1.0), "chores": (None, 1.0), "places": (None, 1.0), "typed": (None, 1.0),
          "valid": (None, 1.0), "profile": (None, 1.0), "tidy": (None, 1.0)}


async def main():
    full = "--full" in sys.argv
    base = 1 if "--tuning" in sys.argv else 1001
    seeds = range(base, base + (5 if "--quick" in sys.argv else 20))
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    m = Mind(tempfile.mkdtemp(), None)
    failed = []
    for suite, (n, floor) in SUITES.items():
        if only and suite not in only:
            continue
        t0, acc, by_type = time.time(), [], collections.defaultdict(list)
        for s in seeds:
            kw = {"n": n} if n else {}
            r = await m.run_test_now(suite, "brain", seed=s, **kw)
            if r["status"] != "done":
                print(f"{suite} seed {s}: {r['summary']}")
                failed.append(suite)
                break
            acc.append(r["summary"]["accuracy"])
            for t, v in (r["summary"].get("by_type") or {}).items():
                by_type[t].append(v)
        score = sum(acc) / max(1, len(acc))
        ok = score >= floor - 1e-9
        print(f"{'ok  ' if ok else 'FAIL'} {suite:8s} {score:.3f} (floor {floor:.2f}, {len(acc)} worlds, "
              f"{time.time() - t0:.0f}s)")
        if not ok:
            failed.append(suite)
            for t, v in sorted(by_type.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))[:5]:
                print(f"       {t:18s} {sum(v) / len(v):.3f}")
    if failed:
        print("FAILED:", ", ".join(failed))
        sys.exit(1)
    print("ALL SUITES AT OR ABOVE THEIR FLOORS")


asyncio.run(main())
