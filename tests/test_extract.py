"""The extraction bench scores correctly: a perfect stand-in extractor files every true fact and no junk; storing
every line as is finds the facts but also the junk (jokes, corrections, hypotheticals)."""
import asyncio
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.mind import Mind  # noqa: E402
from homemind.overnight import parse_facts  # noqa: E402


class Oracle:
    type, cfg = "mock", {}

    async def chat(self, system, msgs, **k):
        facts = []
        for line in msgs[0]["content"].split("\n"):
            n, rest = line.split(". ", 1)
            who, txt = rest.split(": ", 1)
            if who != "U" or "?" in txt or "lol" in txt or txt.startswith("if "):
                continue
            txt = re.sub(r"is \d+, no wait, I mean (\d+)", r"is \1", txt)
            if any(w in txt for w in ("code", "toolbox", "bike", "crate")):
                facts.append({"text": txt, "turn": int(n)})
        return {"text": json.dumps({"facts": facts}), "ms": 1}


class Models:
    models = {"oracle": Oracle()}

    def get(self, n):
        return Oracle()


async def main():
    assert parse_facts('Sure! {"facts": [{"text": "The Kosk code is 4417.", "turn": 3}]}')[0]["turn"] == 3
    assert parse_facts("no json here") == []
    m = Mind(tempfile.mkdtemp(), None)
    m.models = Models()
    for seed in (1, 2, 3):
        raw = await m.run_test_now("extract", "brain", model="oracle", seed=seed, n=8, ingest="raw")
        ex = await m.run_test_now("extract", "brain", model="oracle", seed=seed, n=8, ingest="extract")
        e, r = ex["summary"]["extraction"], raw["summary"]["extraction"]
        assert ex["summary"]["accuracy"] == 1.0, ex["summary"]
        assert e["truth_found"] == e["truth_total"] and e["junk_filed"] == 0, e
        assert r["junk_filed"] > 0 and raw["summary"]["accuracy"] < 1.0, r
    print("EXTRACT BENCH OK")


asyncio.run(main())
