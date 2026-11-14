"""How the brain holds up with many notes: time to store, time per question (plain, with a typo, after a new
note), and accuracy on changed facts and on notes found by description, buried among N other notes.

  python tests/scale.py 10000 50000
"""
import os
import random
import statistics as st
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "home_mind", "rootfs", "app"))
from homemind import suites  # noqa: E402
from homemind.store import Store  # noqa: E402


def run(n, seed=7):
    rng = random.Random(seed)
    d = tempfile.mkdtemp()
    clock = {"t": time.time() - 400 * 86400}
    s = Store(os.path.join(d, "mind.db"), clock=lambda: clock["t"])
    # filler: notes on shelves, facts about other things, chatter
    t0 = time.perf_counter()
    # (the note generator has ~3,000 distinct notes; beyond that they repeat with a number added)
    filler = suites.locate(seed, n=min(2500, n // 2))["events"] + suites.update(seed + 1, n=min(2000, n // 8))["events"]
    k = 0
    while k < n:
        ev = filler[k % len(filler)]
        clock["t"] += 600
        if k % 3 == 2:
            s.remember(text=rng.choice(suites.CHATTER) + f" ({k})", kind="note")
        else:
            s.remember(text=ev["text"] + ("" if k < len(filler) else f" ({k})"), title=ev.get("title", ""),
                       shelf=ev.get("shelf", "inbox"), kind=ev.get("kind", "note"))
        k += 1
    store_s = time.perf_counter() - t0
    # the test world, mixed in at the end
    w = suites.update(seed + 99, n=40)
    ref = {}
    for ev in w["events"]:
        clock["t"] += 600
        ref[ev["ref"]] = s.remember(text=ev["text"], kind="fact")["id"]
    lw = suites.locate(seed + 77, n=40)
    for ev in lw["events"]:
        clock["t"] += 600
        ref["L" + ev["ref"]] = s.remember(text=ev["text"], title=ev["title"], shelf=ev["shelf"], kind="note")["id"]
    times, ok = [], 0
    for q in w["questions"]:
        t1 = time.perf_counter()
        h = s.recall(q["q"], limit=8, touch=False)
        times.append(time.perf_counter() - t1)
        ok += bool(h) and h[0]["id"] == ref[q["ref"]]
    lok = 0
    for q in lw["questions"]:
        t1 = time.perf_counter()
        h = s.recall(q["q"], limit=8, touch=False)
        times.append(time.perf_counter() - t1)
        lok += bool(h) and h[0]["id"] == ref["L" + q["ref"]]
    # a question with a typo in a name, right after a new note (the vocabulary must be rebuilt)
    q = w["questions"][0]["q"]
    typo_q = q.replace(next(x for x in q.split() if x[:1].isupper() and x != q.split()[0]),
                       suites.typo(next(x for x in q.split() if x[:1].isupper() and x != q.split()[0]), rng), 1)
    s.remember(text="One more note just now.", kind="note")
    t1 = time.perf_counter()
    s.recall(typo_q, limit=8, touch=False)
    typo_s = time.perf_counter() - t1
    t1 = time.perf_counter()
    s.recall(typo_q, limit=8, touch=False)
    typo_warm = time.perf_counter() - t1
    size = os.path.getsize(os.path.join(d, "mind.db")) / 1e6
    print(f"{n:>6} notes: stored in {store_s:.1f}s ({1000 * store_s / n:.2f} ms each), db {size:.0f} MB | "
          f"question median {1000 * st.median(times):.0f} ms, worst {1000 * max(times):.0f} ms | "
          f"typo question after a new note {1000 * typo_s:.0f} ms (again {1000 * typo_warm:.0f} ms) | "
          f"changed facts {ok}/{len(w['questions'])}, notes found {lok}/{len(lw['questions'])}")


if __name__ == "__main__":
    for n in [int(x) for x in sys.argv[1:]] or [10000]:
        run(n)
