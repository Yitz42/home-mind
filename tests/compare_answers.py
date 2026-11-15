"""Compare the answer options on your own models, through a running Home Mind (the same test runner as the Tests tab).

  python tests/compare_answers.py --url http://homeassistant.local:8099 --token "$(cat token)" --model qwen9b
  python tests/compare_answers.py ... --suites recall,update,locate,abstain,hard --seeds 3

For each option (as it is, repeat the best note before the question, name the notes first, both) it runs the
model+brain arm on each suite and seed and prints accuracy, stale answers, right answers that point to their note,
and time per answer. Turn an option on (Configuration tab, or its switch on the Home Mind device) only if it wins
on your models. The token is in the app's /data/token; the port must be opened on the app's Network settings.
"""
import argparse
import json
import sys
import time
import urllib.request

VARIANTS = [("as it is", {}), ("best note repeated", {"order": "edges"}), ("notes first", {"notes_first": True}),
            ("both", {"order": "edges", "notes_first": True})]


def call(base, token, path, body=None):
    req = urllib.request.Request(base.rstrip("/") + path, data=None if body is None else json.dumps(body).encode(),
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", "X-Home-Mind-Token": token})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--suites", default="recall,update,locate,abstain,hard")
    ap.add_argument("--seeds", type=int, default=3)
    a = ap.parse_args()
    suites = a.suites.split(",")
    results = {}
    for label, opts in VARIANTS:
        for suite in suites:
            for seed in range(2001, 2001 + a.seeds):       # the same worlds for every option
                run = call(a.url, a.token, "/api/tests", {"suite": suite, "arm": "model+brain", "model": a.model,
                                                          "seed": seed, **opts})
                while True:
                    time.sleep(2)
                    r = call(a.url, a.token, f"/api/runs/{run['id']}")
                    if r["status"] != "running":
                        break
                if r["status"] != "done":
                    print(f"{label} {suite} seed {seed}: {r['status']} {r.get('summary')}", file=sys.stderr)
                    continue
                results.setdefault((label, suite), []).append(r["summary"])
                print(f"  {label:20} {suite:8} seed {seed}: accuracy {r['summary'].get('accuracy')}", file=sys.stderr)
    mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    print(f"\n{'option':20} {'suite':8} {'accuracy':>8} {'stale':>6} {'source':>7} {'ms':>7}")
    for label, _ in VARIANTS:
        for suite in suites:
            rs = results.get((label, suite), [])
            print(f"{label:20} {suite:8} {mean([r.get('accuracy', 0) for r in rs]):8.3f} "
                  f"{mean([r.get('stale_answers', 0) for r in rs]):6.3f} "
                  f"{mean([r['source_ok'] for r in rs if 'source_ok' in r]):7.3f} "
                  f"{mean([r.get('ms_avg', 0) for r in rs]):7.0f}")
        print()


if __name__ == "__main__":
    main()
