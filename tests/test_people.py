"""Who is speaking and who may see what (0.23): recognising the speaker (#3), personal and household notes (#48),
nothing secret or in bulk for an unknown voice (#78), learning only from known voices (#79), and extra search keys
(#71). No Home Assistant needed.

  python tests/test_people.py
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
from homemind import speakers as S, keys as K  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.mind import Mind, guarded_context, find  # noqa: E402
from homemind.overnight import ConvLog, Overnight  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def fresh():
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    for t in ["The drill is in the red toolbox.", "Mia waters the ferns on Sunday.", "The boiler was serviced in May.",
              "Spare bulbs are in the hall cupboard.", "The bins go out on Tuesday night.", "Ben's soccer is at the park.",
              "The vet is Dr. Patel on Main Street.", "Batteries are in the laundry room bin."]:
        st.remember(text=t)
    return st


def resolving():
    people = S.people_from([{"name": "Dana", "devices": "dev_kitchen_sat, dev_dana_phone", "areas": "office"},
                            {"name": "Sam", "ha_users": "sam_user"}],
                           [{"entity_id": "person.dana", "attributes": {"friendly_name": "Dana", "user_id": "u1"}},
                            {"entity_id": "person.sam", "attributes": {"friendly_name": "Sam", "user_id": "u2"}}])
    check("people from the option and person entities", {p["name"] for p in people} == {"Dana", "Sam"} and
          "u1" in next(p for p in people if p["name"] == "Dana")["users"])
    check("signed in by user id", S.resolve(people, user="u2") == ("Sam", "signed in"))
    check("signed in by name", S.resolve(people, user="dana") == ("Dana", "signed in"))
    check("their device", S.resolve(people, device="dev_dana_phone") == ("Dana", "their device"))
    check("a room only one person is listed for", S.resolve(people, area="Office") == ("Dana", "the room"))
    check("only one person home", S.resolve(people, device="dev_living", home={"person.dana": "not_home",
                                                                              "person.sam": "home"}) == ("Sam", "only one home"))
    check("two home and an unknown device: unknown", S.resolve(people, device="dev_living", home={
        "person.dana": "home", "person.sam": "home"}) == (None, "unknown"))
    check("first person by a known speaker is personal", S.scope_for("My PIN is 1234", "Dana") == ("personal", "Dana"))
    check("not first person is household", S.scope_for("The PIN for the gate is 1234", "Dana") == ("household", None))
    check("household rule: everything shared", S.scope_for("My PIN is 1234", "Dana", "household") == ("household", None))
    check("unknown speaker files household", S.scope_for("My PIN is 1234", None) == ("household", None))


async def seeing():
    st = fresh()
    st.remember(text="My bank PIN is 4821.", kind="fact", meta={"scope": "personal", "owner": "Dana"})
    st.remember(text="The guest wifi password is harbor77.", kind="fact")
    st.remember(text="Dana's dentist is on Elm Street.", meta={"said_by": "Dana"})

    def page(q, who, voice=False):
        tok = S.VIEWER.set(who)
        try:
            return guarded_context(st, q, find(st, q, touch=False), voice=voice)[0]
        finally:
            S.VIEWER.reset(tok)
    check("the owner sees their personal note", "4821" in page("What is my bank PIN?", "Dana"))
    check("someone else doesn't", "4821" not in page("What is my bank PIN?", "Sam"))
    check("an unknown voice doesn't", "4821" not in page("What is my bank PIN?", ""))
    check("no speaker information sees everything (panel, Claude)", "4821" in page("What is my bank PIN?", None))
    check("household notes for everyone", "Elm Street" in page("Where is Dana's dentist?", ""))
    p = page("What's the guest wifi password?", "", voice=True)
    check("no password read out to an unknown voice", "harbor77" not in p and "UNKNOWN VOICE" in p, p[:200])
    check("a known voice gets it", "harbor77" in page("What's the guest wifi password?", "Sam", voice=True))
    p = page("List all my codes.", "")
    check("an unknown voice asking for every code gets nothing", "harbor77" not in p and "4821" not in p and
          "share nothing" in p, p[:200])
    m = Mind(tempfile.mkdtemp(), None)
    r = await m.ask("What's the guest wifi password?", None, store=st, voice=True, speaker="")
    check("brain-only answers follow the same rule", "harbor77" not in (r.get("answer") or "") and r["speaker"] == "unknown",
          r.get("answer"))
    r = await m.ask("What's the guest wifi password?", None, store=st, voice=True, speaker="Sam")
    check("... and give it to a known voice", "harbor77" in (r.get("answer") or ""), r.get("answer"))
    check("the viewer doesn't leak into the next call", S.VIEWER.get() is None)

    async def one(who):
        await asyncio.sleep(0)
        tok = S.VIEWER.set(who)
        await asyncio.sleep(0.01)
        try:
            return S.VIEWER.get()
        finally:
            S.VIEWER.reset(tok)
    got = await asyncio.gather(one("Dana"), one(""), one("Sam"))
    check("simultaneous requests keep their own speaker", got == ["Dana", "", "Sam"], got)


async def learning():
    st = fresh()
    conv = ConvLog(st)
    conv.who = lambda dev: ("Dana", "their device") if dev == "dev_dana" else (None, "unknown")
    conv.add(time.time(), "My locker code is 5150.", "Got it.", "c1", "r1", "p", device_id="dev_dana")
    conv.add(time.time(), "The pool gate code is 7272.", "Okay.", "c2", "r2", "p", device_id="dev_tv")
    rows = {r["user_text"]: r for r in conv.pending()}
    check("a turn on Dana's device is hers", rows["My locker code is 5150."]["speaker"] == "Dana")
    check("a turn from an unknown device has no speaker", rows["The pool gate code is 7272."]["speaker"] is None)

    class Model:
        type = "mock"

        async def chat(self, system, messages, **k):
            text = messages[-1]["content"]
            facts = []
            if "5150" in text:
                facts.append({"text": "My locker code is 5150."})
            if "7272" in text:
                facts.append({"text": "The pool gate code is 7272."})
            import json
            return {"text": json.dumps({"facts": facts}), "ms": 1}

    class Models:
        models = {"big": None}

        def get(self, name):
            return Model()

        def list(self):
            return [{"name": "big"}]
    o = Overnight(st, Models(), conv, model="big")
    o.known_only, o.structure_on, o.keys_on = True, False, False
    r = await o.run()
    bodies = {x[0]: x[1] for x in st.db.execute("SELECT body, meta FROM items WHERE source='overnight'")}
    check("with people set up, an unknown voice isn't learned from", not any("7272" in b for b in bodies) and
          r.get("unknown_voices") == 1, (list(bodies), r))
    mine = next((m for b, m in bodies.items() if "5150" in b), "")
    check("a known speaker's first-person fact is theirs alone", '"scope": "personal"' in mine and '"owner": "Dana"' in mine,
          mine)


def search_keys():
    check("lexicon: a beagle is a dog", {"dog", "pet", "breed"} <= set(K.lexicon_keys("We adopted a beagle named Rufus")))
    check("lexicon: two-word names", "cat" in K.lexicon_keys("Our maine coon is huge"))
    check("lexicon: words already there aren't repeated", "dog" not in K.lexicon_keys("Our dog is a beagle"))
    check("model keys are cleaned", K.clean(["Dog", "pet!", "x", "a really long phrase that is too long to be a key",
                                             "beagle"], "a beagle") == ["dog"])
    st = fresh()
    a = st.remember(text="We adopted a beagle named Rufus.")
    check("keys are filed with the note", "dog" in (st.db.execute("SELECT keys FROM items WHERE id=?", (a["id"],)).fetchone()[0] or ""))
    check("... and found by a broader question", st.recall("What's our new dog called?", limit=3)[0]["id"] == a["id"])
    check("keys are not in what a model reads", "breed" not in guarded_context(st, "dog", st.recall("dog", limit=3))[0])
    st.add_keys(a["id"], ["puppy"], by="model")
    check("model keys are added and marked", st.get(a["id"])["meta"].get("keys_by") == "model" and
          st.recall("puppy", limit=2)[0]["id"] == a["id"])
    st.forget(a["id"]) if hasattr(st, "forget") else None
    # an old database (no keys column) is upgraded and keeps its search
    import sqlite3
    p = os.path.join(tempfile.mkdtemp(), "old.db")
    db = sqlite3.connect(p)
    from homemind import store as storemod
    db.executescript(storemod.SCHEMA)
    db.executescript(storemod.FTS.replace(", keys,", ",").replace(", keys)", ")").replace(", new.keys)", ")")
                     .replace(", old.keys)", ")").replace("shelf, keys ON", "shelf ON"))
    db.execute("INSERT INTO items(kind,title,body,status,created,updated,source,meta,shelf) VALUES('note','x',"
               "'We adopted a poodle named Fifi.','active',1,1,'test','{}','inbox')")
    db.commit()
    db.close()
    up = Store(p)
    hit = up.recall("Fifi", limit=1)
    check("an old database is upgraded: keys added, search rebuilt", hit and "dog" in (up.db.execute(
        "SELECT keys FROM items").fetchone()[0] or "") and up.recall("our dog", limit=1), hit)


resolving()
asyncio.run(seeing())
asyncio.run(learning())
search_keys()
print(f"\n{len(FAILS)} failed" if FAILS else "\nALL PEOPLE AND KEYS CHECKS PASSED")
sys.exit(1 if FAILS else 0)
