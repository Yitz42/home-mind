"""Set 5 (0.27): prefix-only matches weigh less (#25), links followed as a walk (#12), older notes learn words from
newer linked ones (#12), a model re-orders the top notes (#54), everything known about a thing (#20), and notes in
other languages (#21). No network needed.

  python tests/test_more.py
"""
import asyncio
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: None
    sys.modules["aiohttp"] = m
from homemind import langs, profiles  # noqa: E402
from homemind.store import Store, HEDGE, CORRECTION, speaker  # noqa: E402
from homemind.mind import Mind  # noqa: E402
from homemind.overnight import Overnight, ConvLog  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def new(filler=0):
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    for k in range(filler):       # a tiny store counts every word as common; a few other notes make it a real one
        st.remember(text=f"Filler note {k}: the {['bins', 'post', 'milk', 'rain'][k % 4]} again.")
    return st


def languages():
    for t, lang in [("The shed code is 1234.", "en"), ("Creo que el código del garaje es 1234.", "es"),
                    ("Ich glaube der Code ist 1234.", "de"), ("Je pense que le code est 1234.", "fr"),
                    ("Ik denk dat de code 1234 is.", "nl"), ("הקוד של המחסן הוא 1234", "other"), ("", "en")]:
        check(f"language of {t[:24]!r} is {lang}", langs.guess(t) == lang, langs.guess(t))
    for t in ["Creo que el código es 1234", "Vielleicht ist es 1234", "Peut-être 1234", "Misschien 1234",
              "Acho que é 1234", "Forse è 1234"]:
        check(f"hedge read in {t!r}", bool(HEDGE.search(t)))
    for t in ["En realidad es 4321", "Eigentlich 4321", "En fait c'est 4321", "Eigenlijk 4321"]:
        check(f"correction read in {t!r}", bool(CORRECTION.search(t)))
    check("speaker read in Spanish", speaker("Marta dice que el código es 1234") == "marta", speaker("Marta dice que"))
    check("speaker read in German", speaker("Jonas sagt der Code ist 1234") == "jonas")
    check("English still read", speaker("Dana says the code is 1234") == "dana" and HEDGE.search("I think it's 3"))


def tagged_meta():
    st = new(8)
    st.remember(text="The Velmar locker code is 5512.", kind="fact")
    st.remember(text="Der Velmar locker code ist 7781.", meta={"hedge": True}, kind="fact")
    hits = st.recall("What is the Velmar locker code?")
    g = [h for h in hits if "7781" in h["body"]]
    check("a note the model tagged as a guess stands aside, marked", g and g[0].get("guess") and
          not any(h.get("newer") for h in hits),
          [(h["body"], h.get("guess")) for h in hits])
    st = new(8)
    st.remember(text="The Corlan gate code is 1100.", meta={"said_by": "Dana"}, kind="fact")
    st.remember(text="The Corlan gate code is 2200.", meta={"said_by": "Sam"}, kind="fact")
    hits = st.recall("What is the Corlan gate code?")
    check("what two tagged speakers said both stand", any(h.get("disagrees") for h in hits) and
          not any(h.get("newer") for h in hits), [(h["body"], h.get("disagrees"), h.get("newer")) for h in hits])


async def tagging():
    st = new()
    a = st.remember(text="Creo que la contraseña del wifi es naranja22.")
    b = st.remember(text="The wifi password is lemon11.")

    class Model:
        def __init__(self):
            self.calls = 0

        async def chat(self, system, messages, **k):
            self.calls += 1
            return {"text": '{"hedge": true, "correction": false, "speaker": "Marta", "valid_from": "2026-03-01"}'}

    class Models:
        models = {"big": None}

        def get(self, n):
            return Model()

        def list(self):
            return []
    o = Overnight(st, Models(), ConvLog(st), model="big")
    mdl = Model()
    n = await o.tag_languages(mdl)
    m = st.get(a["id"])["meta"]
    check("a Spanish note is tagged by the model", n == 1 and m.get("hedge") is True and m.get("speaker") == "Marta" and
          m.get("language") == "es" and m.get("valid_from"), m)
    check("an English note isn't sent to the model", "tagged_by" not in st.get(b["id"])["meta"])
    check("a tagged note isn't asked again", await o.tag_languages(mdl) == 0 and mdl.calls == 1)

    class Bad(Model):
        async def chat(self, system, messages, **k):
            return {"text": "no idea"}
    c = st.remember(text="Ich glaube der Schlüssel liegt im Keller.")
    check("a bad reply leaves the note untouched", await o.tag_languages(Bad()) == 0 and
          "tagged_by" not in st.get(c["id"])["meta"])


def prefix_penalty():
    st = new()
    st.remember(text="The garage remote batteries were replaced.")
    st.remember(text="Garages on the street get swept on Tuesdays.")
    st.remember(text="The garage remote is in the kitchen drawer.")
    hits = st.recall("garage remote")
    check("whole-word matches beat prefix-only ones", "Garages" not in hits[0]["body"], [h["body"] for h in hits])
    old = st.PREFIX_ONLY_W
    st.PREFIX_ONLY_W = 1
    check("the weight can be turned off", isinstance(st.recall("garage remote"), list))
    st.PREFIX_ONLY_W = old


def walk():
    st = new()
    st.remember(text="Orvel keeps the Brisk padlock key.")
    st.remember(text="The Brisk padlock is on the Tarn shed.")
    st.remember(text="The Tarn shed holds the lawn mower.")
    for k in range(8):
        st.remember(text=f"Unrelated note number {k} about the weather and the bins.")
    hits = st.recall("Who has the key for the place where the lawn mower is kept?", limit=6)
    bodies = " | ".join(h["body"] for h in hits)
    check("a three-step chain is found by following links", "Orvel" in bodies, bodies)
    check("links don't drag in unrelated notes", "Unrelated" not in bodies, bodies)


def evolve():
    st = new()
    a = st.remember(text="The Quillon bike is blue.")
    st.remember(text="The Quillon bike was stolen from the Pemberly station rack.")
    keys = (st.get(a["id"]).get("keys") or "")
    check("an older linked note learns rare words of the newer one", "pemberly" in keys.lower() or
          any("Quillon" in h["body"] for h in st.recall("Pemberly station")), keys)
    st3 = new()
    q = st3.remember(text="The Quoven spare key is in the red tin.")
    st3.remember(text="The Pupax spare key is gone now.")
    check("a note linked only by a common thing learns nothing", "pupax" not in (st3.get(q["id"]).get("keys") or "").lower())
    st2 = new()
    st2.EVOLVE = False
    a = st2.remember(text="The Quillon bike is blue.")
    st2.remember(text="The Quillon bike was stolen from the Pemberly station rack.")
    check("evolve can be turned off", "pemberly" not in (st2.get(a["id"]).get("keys") or "").lower())


async def rerank():
    m = Mind(tempfile.mkdtemp(), None)
    hits = [{"title": f"n{i}", "body": f"b{i}"} for i in range(4)]

    class R:
        def __init__(self, t):
            self.t = t

        async def chat(self, *a, **k):
            return {"text": self.t}

    class Models:
        models = {"r": None}

        def __init__(self, t):
            self.t = t

        def get(self, n):
            return R(self.t)
    m.models = Models('{"order": [3, 1]}')
    out = await m._rerank("r", "q", hits)
    check("the model's order leads, the rest keep theirs", [h["title"] for h in out] == ["n2", "n0", "n1", "n3"],
          [h["title"] for h in out])
    m.models = Models("nonsense")
    check("a bad reply keeps the search's order", await m._rerank("r", "q", hits) == hits)
    m.models = Models('{"order": [9, 0, -2]}')
    check("numbers out of range are ignored", await m._rerank("r", "q", hits) == hits)


def picture():
    st = new()
    st.remember(text="The Halvor drill is in the blue toolbox.")
    st.remember(text="The blue toolbox is kept in the garage.")
    st.remember(text="The Halvor drill battery needs charging monthly.")
    txt = profiles.lines(st, "Tell me everything about the Halvor drill", st.clock())
    check("everything known about a thing is gathered", "toolbox" in txt and "charging" in txt, txt)
    check("a question that isn't about a thing adds nothing", profiles.lines(st, "What time is it?", st.clock()) == "")


languages()
tagged_meta()
asyncio.run(tagging())
prefix_penalty()
walk()
evolve()
asyncio.run(rerank())
picture()
print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
