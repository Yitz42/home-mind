"""Expert review findings: each check reproduces a bug found in review and fixed. No network needed.

  python tests/test_review.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def clocked():
    t = [1_700_000_000.0]
    s = Store(":memory:", clock=lambda: t[0])
    return s, t


# ---- round 1: search ------------------------------------------------------------------------------------------------
# a shelf or kind asked for holds for notes reached through links too
s = Store(":memory:")
s.remember("Kofi's bike is the red Trek", shelf="people/kofi")
s.remember("The red Trek has a Kryptonite lock in the garage", shelf="garage")
res = s.recall("Kofi bike lock garage", shelf="people", limit=5)
check("search: links keep to the shelf asked for", res and all(d["shelf"].startswith("people") for d in res),
      [d["shelf"] for d in res])

# a revised note keeps its search keys and links
s = Store(":memory:")
x = s.remember("We have a beagle called Pip")["id"]
y = s.revise(x, body="We have a beagle called Pippa")["id"]
check("search: a revised note is still found by its keys", [d["id"] for d in s.recall("dog")] == [y])
s = Store(":memory:")
a = s.remember("Kofi's bike is the red Trek")["id"]
b = s.remember("The red Trek has a Kryptonite lock")["id"]
a2 = s.revise(a, tags=["bikes"])["id"]
check("search: a revised note keeps its links", b in s.linked(a2) and a2 in s.linked(b), s.linked(a2))

# names with accents are found ("Müller" used to split into "m" + "ller", which match nothing)
s = Store(":memory:")
s.remember("Herr Müller fixes the boiler, number 0171 555")
s.remember("The plumber comes on Tuesday")
res = s.recall("who is Müller?")
check("search: a name with an accent is found", res and "Müller" in res[0]["body"], [d["title"] for d in res])
s.remember("Müller replaced the boiler valve")
check("search: notes naming an accented name are linked", len(s.linked(1)) == 1, s.linked(1))
g = s.gather("everything about Müller")
check("search: everything about an accented name", g and len(g["items"]) == 2, g)

# a word that is only a search key is not "repaired" into another stored word
s = Store(":memory:")
s.remember("Charge the ebike battery every Sunday")
s.remember("Bake bread on Saturday")
check("search: a key word is not taken for a typo", s.fix_typos("where is the bike charger") ==
      "where is the bike charger", s.fix_typos("where is the bike charger"))
res = s.recall("bike")
check("search: a key word finds its note", res and "ebike" in res[0]["body"], [d["title"] for d in res])

# moving or tagging an old version doesn't make it newer than the version that replaced it
s, t = clocked()
for i in range(10):
    s.remember(f"filler note {i} about the garden hose", kind="fact")
old = s.remember("The Kosk locker code is 1111", kind="fact")["id"]
t[0] += 86400
new = s.remember("The Kosk locker code is 2222", kind="fact")["id"]
t[0] += 86400
s.revise(old, tags=["garage"])
res = s.recall("What is the Kosk locker code?")
check("search: tagging an old version keeps the newer one current", res[0]["id"] == new and res[1].get("newer") == new,
      [(d["id"], d.get("newer")) for d in res])

# a phone's curly apostrophe ("Ranven’s locker") picks the same thing as a straight one
s, t = clocked()
for i in range(10):
    s.remember(f"filler note {i} about the garden hose", kind="fact")
s.remember("The code for Ranven's locker is 1111", kind="fact")
t[0] += 3600
s.remember("The code for Ranven's locker is 2222", kind="fact")
t[0] += 3600
s.remember("The code for Ranven's toolbox is 3333", kind="fact")
res = s.recall("What is the code for Ranven’s locker?")
check("search: curly apostrophe finds the thing asked about", "2222" in res[0]["body"] and
      not any(d.get("newer") and "toolbox" in d["body"] for d in res) and
      all(d.get("newer") != res[-1]["id"] for d in res if "locker" in d["body"]),
      [(d["body"], d.get("newer")) for d in res])

if FAILS:
    print(f"{len(FAILS)} FAILED")
    sys.exit(1)
print("all ok")
