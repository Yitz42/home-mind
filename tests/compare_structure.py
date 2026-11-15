"""Should overnight_structure be on for your model? Run through a running Home Mind (the Tests runner):

  python tests/compare_structure.py --url http://homeassistant.local:8099 --token "$(cat token)" --model qwen35b

1. paraphrase suite, brain arm: ingest raw (the patterns alone) vs ingest structure (the model reads what they
   missed). Higher is better.
2. the junk check: recall, update, locate, tricky, places, chores with ingest structure vs raw. These must not get
   worse; if they do, the model is adding wrong structure.

Turn it on (Configuration tab, or the "Overnight: read what the patterns missed" switch) only if 1 goes up and 2
stays the same.
"""
import argparse
import json
import sys
import time
import urllib.request


def call(base, token, path, body=None):
    req = urllib.request.Request(base.rstrip("/") + path, data=None if body is None else json.dumps(body).encode(),
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", "X-Home-Mind-Token": token})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def run(a, suite, ingest, seed):
    body = {"suite": suite, "arm": "brain", "seed": seed, "ingest": ingest}
    if ingest == "structure":
        body["model"] = a.model
    r = call(a.url, a.token, "/api/tests", body)
    while True:
        time.sleep(2)
        out = call(a.url, a.token, f"/api/runs/{r['id']}")
        if out["status"] != "running":
            return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--seeds", type=int, default=3)
    a = ap.parse_args()
    rows = []
    for suite in ("paraphrase", "recall", "update", "locate", "tricky", "places", "chores"):
        acc = {}
        for ingest in ("raw", "structure"):
            xs = []
            for seed in range(3001, 3001 + a.seeds):
                r = run(a, suite, ingest, seed)
                if r["status"] == "done":
                    xs.append(r["summary"]["accuracy"])
                else:
                    print(f"{suite} {ingest} {seed}: {r['status']} {r.get('summary')}", file=sys.stderr)
            acc[ingest] = sum(xs) / len(xs) if xs else float("nan")
            print(f"  {suite:10} {ingest:9} {acc[ingest]:.3f}", file=sys.stderr)
        rows.append((suite, acc["raw"], acc["structure"]))
    print(f"\n{'suite':10} {'patterns':>9} {'+ model':>9}")
    for suite, raw, st in rows:
        flag = "" if suite == "paraphrase" else ("  <- worse: the model adds wrong structure" if st < raw - 1e-9 else "")
        print(f"{suite:10} {raw:9.3f} {st:9.3f}{flag}")


if __name__ == "__main__":
    main()
