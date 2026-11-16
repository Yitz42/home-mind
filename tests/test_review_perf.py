"""Expert review (round 3, performance): what was slow on a big store, checked so it stays fast. No network needed.

  python tests/test_review_perf.py
"""
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind import mind, portable, profiles, structure, suites, tidy  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def flat(idx):
    return ({w: [(n, sorted(v)) for n, v in d.items()] for w, d in idx["things"].items()}, idx["universal"])


# ---- index(): updated from the changed notes, the same as a full rebuild ---------------------------------------------
# (it was rebuilt from every note after each new note: ~0.3 s at 30,000 notes, on the next question)
rng = random.Random(9)
names = [suites.name(rng) for _ in range(30)]
same = True
for trial in range(8):
    s = Store(":memory:")
    for step in range(120):
        ids = [r[0] for r in s.db.execute("SELECT id FROM items")]
        op = rng.random()
        if op < .55 or not ids:
            a, b = rng.sample(names, 2)
            s.remember(text=rng.choice([f"The {a} locker code is {rng.randint(1, 99)}. {b} crate too.",
                                        f"Changed all the {rng.choice(['locker', 'crate'])} codes to 1.",
                                        f"I saw {a} toolbox and {b} toolbox and {a} toolbox.",
                                        "The shed was cleared out.", f"{a}'s bike is red."]),
                       kind=rng.choice(["fact", "note"]), key=rng.choice([None, f"{a}|code"]))
        elif op < .7:
            s.revise(rng.choice(ids), body=f"Now the {rng.choice(names)} locker is {rng.randint(1, 9)}.")
        else:
            s.set_status(rng.choice(ids), rng.choice(["archived", "active", "faded"]))
        if rng.random() < .3:
            s.index()
    inc = flat(s.index())
    s._index = None
    same = same and inc == flat(s.index())
check("index kept up to date from changed notes equals a full rebuild (names in the same order)", same)

s = Store(":memory:")
for i in range(200):
    s.remember(text=f"The {suites.name(rng)}{i} locker code is {1000 + i}.", kind="fact")
s.index()
calls = []
orig = s._index_entries
s._index_entries = lambda r: (calls.append(r["id"]), orig(r))[1]
s.remember(text="The Quorvel locker code is 4417.", kind="fact")
idx = s.index()
check("a new note updates the index without reading every note", len(calls) == 1 and "quorvel" in idx["things"]["locker"],
      calls[:5])
calls.clear()
s.recall("What is the code for the Quorvel locker?", touch=True)
s.index()
check("using notes (touch) is not a change: nothing to re-read", calls == [], calls[:5])
s._index_entries = orig

# ---- one question repairs its words once, not at every step ---------------------------------------------------------
n = []
v = s.vocab
s.vocab = lambda: (n.append(1), v())[1]
q = "What is the code for the Quorvl locker?"
fixed = s.fix_typos(q)
hits = mind.find(s, q, 8)
mind.guarded_context(s, q, hits)
mind.nothing_close(s, q, hits)
check("typo repair worked out once per question (memo until a note changes)", len(n) <= 2 and "Quorvel" in fixed,
      (len(n), fixed))
s.remember(text="The Quorvex locker code is 1234.", kind="fact")
n.clear()
check("a new note makes it repair again", s.fix_typos("Quorvex locker") == "Quorvex locker" and len(n) == 1, len(n))
s.vocab = v

# ---- places and the profile: kept until a note changes --------------------------------------------------------------
s = Store(":memory:")
s.remember(text="The drill is in the red toolbox.", kind="fact")
s.remember(text="Dana is my partner.", kind="fact")
p1 = structure._places(s)
check("places reused while nothing changes", structure._places(s) is p1 and "drill" in p1, p1)
s.remember(text="The tape measure is in the red toolbox.", kind="fact")
check("places read again after a new note", "tape measure" in structure._places(s), structure._places(s))
check("profile lines kept and fresh", structure.profile(s) == structure.profile(s) and
      any("Dana" in x for x in structure.profile(s)))
s.remember(text="Kofi is our son.", kind="fact")
check("profile sees a new who-is-who note", any("Kofi" in x for x in structure.profile(s)), structure.profile(s))
# a picture works out only the asked thing's chores
s.remember(text="Water the Nimsk fern every Saturday.", kind="fact")
s.remember(text="Clean the dryer vent every 90 days.", kind="fact")
seen = []
cs = structure.chore_states
structure.chore_states = lambda st, now, pick=None: (seen.append(len(pick) if pick is not None else -1), cs(st, now, pick))[1]
pic = profiles.picture(s, "nimsk fern", s.clock())
structure.chore_states = cs
check("a picture works out only its own thing's chores", seen == [1] and any("Chore: water the nimsk fern" in x for x in pic),
      (seen, pic))

# ---- duplicates: same pairs, without comparing every note with every note sharing a word ----------------------------
rng = random.Random(4)
W = "alpha beta gamma delta eps zeta eta theta iota kappa lam mu nu xi omicron pi rho sigma tau".split()
s = Store(":memory:")
s.LINKS = False
info = []
for k in range(250):
    txt = " ".join(rng.choice(W[:rng.randint(3, len(W))]) for _ in range(rng.randint(1, 12)))
    if rng.random() < .3:
        txt += f" {rng.randint(1, 3)}"
    s.remember(text=txt, kind=rng.choice(["note", "fact"]))
rows = s.db.execute("SELECT id, kind, title, body FROM items WHERE status='active' ORDER BY id").fetchall()
naive, seen_norm = set(), {}
for r in rows:
    t = r["body"] or r["title"]
    if tidy._norm(t) in seen_norm:
        naive.add((seen_norm[tidy._norm(t)], r["id"]))
    else:
        seen_norm[tidy._norm(t)] = r["id"]
        info.append((r["id"], r["kind"], set(tidy.words(t)), set(tidy.re.findall(r"\d+", t)), tidy.names_in(t)))
for i, a in enumerate(info):
    for b in info[i + 1:]:
        if a[1] == b[1] and a[3] == b[3] and a[4] == b[4] and a[2] and \
                len(a[2] & b[2]) / max(len(a[2]), len(b[2])) >= tidy.DUP_SHARE:
            naive.add((a[0], b[0]))
got = tidy.duplicates(s, limit=10 ** 6)
check("duplicates finds exactly the pairs a full comparison finds", set(got) == naive and len(got) == len(naive),
      (len(got), len(naive)))

s = Store(":memory:")
pool = [f"w{chr(97 + i % 26)}{chr(97 + i // 26)}x" for i in range(300)]
with s.lock:
    for k in range(5000):
        s.db.execute("INSERT INTO items(kind,title,body,created,updated) VALUES('note','',?,?,?)",
                     ("today the weather " + " ".join(rng.sample(pool, 3)), k, k))
    s.db.commit()
t = time.perf_counter()
tidy.duplicates(s)
dt = time.perf_counter() - t
check("duplicates over 5,000 notes sharing common words is quick (was ~6 s)", dt < 2.5, f"{dt:.2f}s")

# ---- import: a note already here is found by when it was filed, not by a scan of every note per imported note ---------
s = Store(":memory:")
with s.lock:
    for k in range(12000):
        s.db.execute("INSERT INTO items(kind,title,body,created,updated) VALUES('note',?,?,?,?)",
                     (f"note {k}", f"body {k % 50}", 1_700_000_000 + k * 0.4, 1_700_000_000))
    s.db.commit()
data = portable.export(s)
t = time.perf_counter()
r = portable.import_(s, data, dry_run=True)
dt = time.perf_counter() - t
check("re-importing 12,000 notes skips them all, quickly (was ~7 s)", r["skipped"] == 12000 and r["added"] == 0 and dt < 2,
      (r, f"{dt:.2f}s"))

if FAILS:
    print(f"{len(FAILS)} FAILED")
    sys.exit(1)
print("all ok")
