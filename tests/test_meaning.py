"""Finding (0.24): search by meaning fused with keywords (#10), things without capitalized names (#11), links
between notes (#12), importance (#75) and categories that fade at their own pace (#38). A fake embedding model stands
in for a real one; no network needed.

  python tests/test_meaning.py
"""
import asyncio
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    sys.modules["aiohttp"] = types.ModuleType("aiohttp")
from homemind import embeddings as E, categories as C  # noqa: E402
from homemind.store import Store, modified_things  # noqa: E402
from homemind.mind import Mind  # noqa: E402

FAILS = []
DAY = 86400


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


CONCEPTS = {"planter": 0, "flower": 0, "box": 0, "pot": 0, "sofa": 1, "couch": 1, "settee": 1, "invoice": 2, "bill": 2,
            "receipt": 2, "boat": 3, "kayak": 3, "canoe": 3}


class FakeEmbedder:
    name = "fake"
    calls = 0

    async def embed(self, texts):
        FakeEmbedder.calls += 1
        out = []
        for t in texts:
            v = [0.0] * 5
            for w in t.lower().replace(".", " ").replace("?", " ").split():
                v[CONCEPTS.get(w, 4)] += 1.0 if w in CONCEPTS else 0.05
            out.append(v)
        return out


def fresh(clock=None):
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"), **({"clock": clock} if clock else {}))
    for t in ["The drill is in the red toolbox.", "Mia waters the ferns on Sunday.", "The boiler was serviced in May.",
              "Spare bulbs are in the hall cupboard.", "The bins go out on Tuesday night.", "Ben's soccer is at the park.",
              "The vet is Dr. Patel on Main Street.", "Batteries are in the laundry room bin."]:
        st.remember(text=t)
    return st


async def meaning():
    st = fresh()
    a = st.remember(text="The flower box by the window needs repotting.")
    st.remember(text="The settee cushions are in the attic.")
    m = Mind(tempfile.mkdtemp(), None)
    m.store = st
    hits, _ = await m.search(st, "Where is the planter?", 5)
    check("without embeddings, 'planter' doesn't find 'flower box'", a["id"] not in [h["id"] for h in hits[:3]])
    m.embeddings = E.Embeddings(st, FakeEmbedder())
    n = await m.embeddings.index(64)
    check("notes are embedded in batches", n > 0 and await m.embeddings.index(64) == 0)
    hits, _ = await m.search(st, "Where is the planter?", 5)
    check("with embeddings it does, fused with the keyword results", a["id"] in [h["id"] for h in hits[:3]],
          [h["title"] for h in hits])
    check("the meaning-only hit is marked", any(h.get("meaning") for h in hits if h["id"] == a["id"]))
    c0 = FakeEmbedder.calls
    await m.search(st, "Where is the planter?", 5)
    check("a repeated question is embedded once", FakeEmbedder.calls == c0)
    st.remember(text="My kayak is on the garage rack.", meta={"scope": "personal", "owner": "Dana"})
    await m.embeddings.index(64)
    from homemind.speakers import VIEWER
    tok = VIEWER.set("Sam")
    try:
        hits, _ = await m.search(st, "Where is the canoe?", 5)
    finally:
        VIEWER.reset(tok)
    check("meaning search respects personal notes", not any("kayak" in h["body"] for h in hits))
    b = st.remember(text="The invoice for the roof is in the blue folder.")
    await m.embeddings.index(64)
    st.purge([b["id"]])
    check("deleting for good removes the vector too", not st.db.execute(
        "SELECT 1 FROM vectors WHERE item_id=?", (b["id"],)).fetchone())

    class Down:
        name = "down"

        async def embed(self, texts):
            raise RuntimeError("model is off")
    m.embeddings = E.Embeddings(st, Down())
    hits, _ = await m.search(st, "Where is the drill?", 5)
    check("an embedding model that's down leaves keyword search working", hits and "drill" in hits[0]["body"])

    class Counting:
        name = "asleep"
        calls = 0

        async def embed(self, texts):
            Counting.calls += 1
            return [[1.0, 0.0] for _ in texts]
    m.embeddings = E.Embeddings(st, Counting(), is_up=lambda: False)
    hits, _ = await m.search(st, "Where is the drill kept?", 5)
    n = await m.embeddings.index(8)
    check("0.41: an asleep embedding computer isn't called (keywords at once, indexing waits)",
          hits and Counting.calls == 0 and n == 0, Counting.calls)
    check("fuse: rank fusion keeps both lists' best", [h["id"] for h in E.fuse([{"id": 1}, {"id": 2}], [{"id": 3}, {"id": 1}], 3)]
          == [1, 3, 2])


def lowercase_and_links():
    check("modified things", modified_things("Changed the garage freezer setting and the blue toolbox code") ==
          {"freezer": {"garage"}, "toolbox": {"blue"}})
    check("names and filler words aren't modifiers", modified_things("the new Trek and the same box") == {})
    st = fresh()
    st.remember(text="The chest freezer temperature is 3123.", kind="fact")
    g = st.remember(text="The garage freezer is set to 7662.", kind="fact")
    check("a different modifier ranks lower", st.recall("What is the garage freezer temperature?", limit=2)[0]["id"] == g["id"])
    a = st.remember(text="Kofi's bike is the red Trek.")
    for k in range(10):
        st.remember(text=f"The {['blue', 'green', 'teal', 'gray', 'pink'][k % 5]} cart {k} is locked at the gym.")
    b = st.remember(text="The red Trek is locked at the station.")
    check("filing links notes about the same thing", b["id"] in st.linked(a["id"]) and a["id"] in st.linked(b["id"]))
    hits = st.recall("Where is Kofi's bike locked?", limit=5)
    check("search follows the link to the second step", any(h["id"] == b["id"] for h in hits[:3]),
          [h["body"] for h in hits])
    led = st._follow_links("Where is Kofi's bike locked?", [dict(st.get(a["id"]), score=1.0)], 5, False)
    check("a linked note found only through the link says so", led[1]["id"] == b["id"] and led[1]["via"] == a["id"], led)
    for k in range(8):
        st.remember(text=f"Dana fed the cat at {k}.")
    c = st.remember(text="Dana bought new shoes.")
    check("a name in many notes is too common to link on", len(st.linked(c["id"])) <= st.LINK_MAX)
    st.purge([b["id"]])
    check("deleting for good removes its links", b["id"] not in st.linked(a["id"]))


def categories():
    for text, want in [("Mia is allergic to peanuts; her EpiPen is in the hall drawer.", "safety"),
                       ("The gas shutoff is behind the dryer.", "safety"), ("Ben was born in 2015.", "identity"),
                       ("Dana loves oat milk.", "preference"), ("The dishwasher model number is SHX88.", "device"),
                       ("We had pizza on Friday.", "note")]:
        check(f"category: {want}", C.categorize(text)[0] == want, C.categorize(text))
    check("corrections are their own category", C.categorize("It's 5512", source="correction")[0] == "correction")
    t = {"now": time.time() - 400 * DAY}
    st = fresh(lambda: t["now"])
    ids = {k: st.remember(text=x)["id"] for k, x in [("safety", "The water main shutoff is in the basement."),
                                                     ("pref", "Dana prefers the window seat."),
                                                     ("device", "The router password is on the router."),
                                                     ("plain", "We had pizza on Friday."),
                                                     ("ep", "Walked to the park.")]}
    st.db.execute("UPDATE items SET kind='episode' WHERE id=?", (ids["ep"],))
    st.db.commit()
    t["now"] = time.time()
    st.fade(60)
    status = {k: st.get(i)["status"] for k, i in ids.items()}
    check("400 days unused: plain notes and episodes fade", status["plain"] == "faded" and status["ep"] == "faded", status)
    check("safety notes never fade", status["safety"] == "active", status)
    check("preferences (3x) and devices (4x) last longer", status["pref"] == "faded" and status["device"] == "faded", status)
    st2 = fresh(lambda: t["now"])
    t["now"] = time.time() - 150 * DAY
    p = st2.remember(text="Dana prefers the window seat.")["id"]
    n = st2.remember(text="We had pizza on Friday.")["id"]
    t["now"] = time.time()
    st2.fade(60)
    check("150 days: a note fades, a preference doesn't yet", st2.get(n)["status"] == "faded" and
          st2.get(p)["status"] == "active")
    s1 = st2.remember(text="The fire extinguisher is under the sink.")
    s2 = st2.remember(text="The spare sponge is under the sink.")
    sc = {h["id"]: h["score"] for h in st2.recall("What's under the sink?", limit=5)}
    check("importance leans toward what matters", sc.get(s1["id"], 0) >= sc.get(s2["id"], 0), sc)


asyncio.run(meaning())
lowercase_and_links()
categories()
print(f"\n{len(FAILS)} failed" if FAILS else "\nALL MEANING, LINKS AND CATEGORY CHECKS PASSED")
sys.exit(1 if FAILS else 0)
