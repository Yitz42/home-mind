import asyncio, sys, os, tempfile, collections
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "home_mind", "rootfs", "app"))
from homemind import mind as M
from homemind.store import names_in
import metaphone; print("metaphone ok")

async def abstain(m, seeds):
    c = collections.Counter()
    for s in seeds:
        r = await m.run_test_now("abstain", "brain", seed=s)
        for d in r["details"]:
            k = "named" if names_in(d["q"]) else "unnamed"
            c[(d["type"], k)] += 1; c[(d["type"], k, "f")] += d["flagged"]
            c[d["type"]] += 1; c[(d["type"], "f")] += d["flagged"]
            c[(d["type"], "ok")] += bool(d["correct"])
    rm, ct = "abstain_removed", "abstain_control"
    return (f"removed {c[(rm,'f')]}/{c[rm]} (named {c[(rm,'named','f')]}/{c[(rm,'named')]}, unnamed {c[(rm,'unnamed','f')]}/{c[(rm,'unnamed')]})"
            f" | control flagged {c[(ct,'f')]}/{c[ct]} (named {c[(ct,'named','f')]}/{c[(ct,'named')]}, unnamed {c[(ct,'unnamed','f')]}/{c[(ct,'unnamed')]}), control ok {c[(ct,'ok')]}/{c[ct]}")

async def main():
    m = M.Mind(tempfile.mkdtemp(), None)
    weak = M.weak_match
    M.weak_match = lambda *a: False
    print("NAMES ONLY tuning ", await abstain(m, range(1, 21)))
    print("NAMES ONLY heldout", await abstain(m, range(1001, 1021)))
    M.weak_match = weak
    for T in (0.25, 0.3, 0.35, 0.4, 0.45, 0.5):
        M.WEAK_COVER = T
        print(f"T={T} tuning ", await abstain(m, range(1, 21)))
    for T in (0.3, 0.35, 0.4):
        M.WEAK_COVER = T
        print(f"T={T} heldout", await abstain(m, range(1001, 1021)))
    # past days: with and without the new ordering
    for label, f in (("old", lambda st, q, k=8, shelf=None, touch=True: st.recall(q, shelf=shelf, limit=k, touch=touch)), ("new", M.find)):
        M.find = f
        by = collections.defaultdict(list)
        for s in range(1001, 1021):
            r = await m.run_test_now("hard", "brain", seed=s)
            for d in r["details"]:
                by[d["type"]].append((bool(d.get("hit1")), bool(d.get("correct"))))
        print(label, {t: f"h1 {sum(h for h,_ in v)}/{len(v)} ok {sum(c for _,c in v)}" for t, v in sorted(by.items())})

asyncio.run(main())
