"""Expert review regressions: what leaves the house (cloud models, files, notifications) and what crosses between
household members (personal notes). No Home Assistant or network needed.

  python tests/test_review.py
"""
import asyncio
import glob
import json
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
from homemind import speakers as S, structure  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.mind import Mind, guarded_context, find  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class Recorder:
    """A model that records what it was sent. type anthropic = cloud; ollama on a LAN address = local."""
    def __init__(self, cloud=True, reply='{"order": [1]}'):
        self.type = "anthropic" if cloud else "ollama"
        self.cfg = {} if cloud else {"url": "http://192.168.1.5:11434"}
        self.sent, self.reply = [], reply

    async def chat(self, system, messages, **k):
        self.sent.append(system + "\n" + messages[-1]["content"])
        return {"text": self.reply, "ms": 1}


class Models:
    def __init__(self, **m):
        self.models = m

    def get(self, n):
        return self.models[n]

    def list(self):
        return [{"name": n} for n in self.models]


def fresh():
    return Store(os.path.join(tempfile.mkdtemp(), "m.db"))


def as_viewer(who, fn, *a):
    tok = S.VIEWER.set(who)
    try:
        return fn(*a)
    finally:
        S.VIEWER.reset(tok)


async def cloud_paths():
    cloud = Recorder()
    m = Mind(tempfile.mkdtemp(), Models(haiku=cloud))
    st = m.store
    for t in ["The garage door code is 4417.", "The garage has a red toolbox.", "The garage light is on a timer.",
              "The garage fridge holds drinks."]:
        st.remember(text=t)
    st.remember(text="To leave, type the alarm code 7731 and close the door.", kind="procedure",
                title="Leaving the house")
    m.rerank_model = "haiku"
    await m.ask("what's in the garage? how do I leave the house", "haiku", fallback=False)
    check("the re-ranker (a cloud model) gets no code the question doesn't ask for",
          len(cloud.sent) >= 2 and "4417" not in cloud.sent[0], cloud.sent[:1])
    check("a procedure's code isn't sent to a cloud model in the system message",
          all("7731" not in s for s in cloud.sent) and any("Leaving the house" in s for s in cloud.sent), cloud.sent[-1:])
    cloud.sent.clear()
    m.rerank_model = None
    m.CODE_FROM_NOTES = False       # this checks what the cloud model is sent; a plain code question skips it
    await m.ask("what is the alarm code to leave the house?", "haiku", fallback=False)
    check("... but it is when the question asks for that code", any("7731" in s for s in cloud.sent))
    local = Recorder(cloud=False)
    m2 = Mind(tempfile.mkdtemp(), Models(qwen=local))
    m2.store.remember(text="To leave, type the alarm code 7731 and close the door.", kind="procedure",
                      title="Leaving the house")
    await m2.ask("how do I leave the house", "qwen", fallback=False)
    check("a local model still reads the procedure whole", any("7731" in s for s in local.sent))


async def overnight_paths():
    from homemind.overnight import Overnight, ConvLog
    from homemind.keys import model_keys
    for cloud in (True, False):
        st = fresh()
        st.remember(text="Le code du portail est 4417.", kind="fact")                 # not English: tagged
        st.remember(text="The shed has a padlock.")
        a = st.remember(text="The shed padlock code is 8812.")
        b = st.remember(text="Changed its code to 9931 yesterday.")
        mdl = Recorder(cloud=cloud, reply="{}")
        o = Overnight(st, Models(big=mdl), ConvLog(st), model="big")
        await o.tag_languages(mdl)
        await o.check_links(mdl)
        await model_keys(st, mdl)
        sent = "\n".join(mdl.sent)
        if cloud:
            check("overnight tagging, links and keys send no code to a cloud model",
                  sent and "4417" not in sent and "8812" not in sent and "9931" not in sent, sent[:300])
        else:
            check("... a local overnight model reads them whole", "4417" in sent and "8812" in sent, sent[:300])
        _ = (a, b)


async def journal_paths():
    from homemind.journal import Journal
    from datetime import datetime
    st = fresh()
    mdl = Recorder(reply="A summary.")
    st.remember(text="I worried about my debts today.", shelf="journal", meta={"scope": "personal", "owner": "Sam"})
    st.remember(text="The safe code is 5521 now.", shelf="journal")
    j = Journal(st, Models(haiku=mdl))
    r = await j.summarize(datetime.now().date())
    rows = [st._row(x) for x in st.db.execute("SELECT * FROM items WHERE shelf='journal/summaries'")]
    owners = [(d["meta"].get("scope"), d["meta"].get("owner")) for d in rows]
    check("a personal journal entry gets a personal summary, not a household one",
          len(rows) == 2 and ("personal", "Sam") in owners and r.get("summaries") == 2, owners)
    check("journal entries go to a cloud model without codes", mdl.sent and all("5521" not in s for s in mdl.sent))


def personal_paths():
    st = fresh()
    st.remember(text="My girlfriend is Priya.", kind="fact", meta={"scope": "personal", "owner": "Sam"})
    st.remember(text="I'm allergic to penicillin.", kind="fact", shelf="profile",
                meta={"scope": "personal", "owner": "Sam"})
    st.remember(text="The house was built in 1931.", kind="fact", shelf="profile")
    st.remember(text="Lunch with Priya on Friday was lovely.")
    st.remember(text="I told Priya about my debt problems.", meta={"scope": "personal", "owner": "Sam"})
    st.remember(text="The Kosk locker is in the garage.")
    st.remember(text="The Bexbel locker is in the shed.")
    st.remember(text="I keep my Vrel locker for my private letters.", meta={"scope": "personal", "owner": "Sam"})
    st.remember(text="Spare bulbs are in the hall cupboard.")

    def page(q, who):
        return as_viewer(who, lambda: guarded_context(st, q, find(st, q, touch=False))[0])
    p = page("Where are the spare bulbs?", "Dana")
    check("the always-shown profile has no one else's personal notes",
          "penicillin" not in p and "Priya" not in p and "1931" in p, p[:300])
    check("... and the owner still sees theirs", "penicillin" in page("Where are the spare bulbs?", "Sam"))
    check("an unknown voice gets no personal profile line", "penicillin" not in page("Where are the spare bulbs?", ""))
    check("'my girlfriend' isn't expanded with someone else's partner",
          as_viewer("Dana", structure.expand_aliases, st, "what does my girlfriend like?") == "what does my girlfriend like?")
    check("... but is for the person who said it",
          "Priya" in as_viewer("Sam", structure.expand_aliases, st, "what does my girlfriend like?"))
    check("gathering everything about a name skips another person's notes",
          "debt" not in page("Tell me everything about Priya", "Dana") and
          "debt" in page("Tell me everything about Priya", "Sam"))
    check("gathering a list of things skips another person's notes",
          "Vrel" not in page("Which lockers do we have?", "Dana") and "Vrel" not in page("Which lockers do we have?", ""))


async def view_paths():
    from homemind.views import Views
    m = Mind(tempfile.mkdtemp(), None)
    st = m.store
    st.remember(text="My therapist appointment is Thursday.", shelf="health", meta={"scope": "personal", "owner": "Sam"})
    st.remember(text="The medicine cabinet code is 4417.", kind="fact", shelf="health")
    m.views = Views(st)
    m.views.save("health", shelf="health")
    r = await m.ask("show my health", None, speaker="")
    check("a saved view read to an unknown speaker: no personal notes, no codes",
          "therapist" not in r["answer"] and "4417" not in r["answer"] and "cabinet" in r["answer"], r["answer"])
    r = await m.ask("show my health", None, speaker="Sam")
    check("... the owner gets their own note and the code", "therapist" in r["answer"] and "4417" in r["answer"])
    r = await m.ask("show my health", None, speaker="Dana")
    check("... another person doesn't get the personal note", "therapist" not in r["answer"], r["answer"])


async def brief_paths():
    from homemind.morning import Morning
    from homemind.evening import Evening
    st = fresh()
    st.remember(text="My therapist appointment moved to Thursday.", meta={"scope": "personal", "owner": "Sam"})
    st.remember(text="The bins go out on Tuesday.")
    st.remember(text="I take my heart pills every day.", meta={"scope": "personal", "owner": "Sam",
                                                               "chore": {"verb": "take", "thing": "heart pills",
                                                                         "every_days": 1}})

    class HA:
        def __init__(self):
            self.sent = []

        async def notify(self, title, msg, nid=None):
            self.sent.append(msg)
    ha = HA()
    mo = Morning(st, ha, None)
    text = mo.build()
    check("the morning summary lists no one's personal notes", "therapist" not in text and "Bins" in text.title()
          and "heart" not in text, text)
    old = st.remember(text="My locker code is 1234.", kind="fact", meta={"scope": "personal", "owner": "Sam"})
    pub = st.remember(text="The shed key is in the blue mug.", kind="fact")
    st.db.execute("UPDATE items SET created=? WHERE id IN (?, ?)", (time.time() - 400 * 86400, old["id"], pub["id"]))
    st.db.execute("UPDATE items SET use_count=9 WHERE id=?", (old["id"],))     # the personal one is the most used
    st.db.commit()
    pick = mo.pick_checkin()
    check("the morning check-in never asks about a personal note", pick and pick["id"] == pub["id"], pick)
    sec = st.remember(text="The gate code is 8812.", kind="fact")
    await mo.on_action(f"HM_CHANGED_{sec['id']}")
    check("'it changed' notification shows no code", ha.sent and "8812" not in ha.sent[-1], ha.sent[-1:])
    ev = Evening(st, ha).build()
    check("the evening brief lists no one's personal notes", "therapist" not in ev and "locker" not in ev.lower(), ev)


def revise_paths():
    from homemind.mdsync import MdSync
    st = fresh()
    st.vault_key = os.urandom(32)
    d = st.remember(text="The side gate has a keypad.", kind="fact")
    r = st.revise(d["id"], body="The side gate code is 4417.", title="Side gate code is 4417")
    row = st.db.execute("SELECT secret, body FROM items WHERE id=?", (r["id"],)).fetchone()
    check("an edit that adds a code marks the note secret and seals it",
          st.get(r["id"])["meta"].get("sensitive") is True and row["secret"] is not None and "4417" not in row["body"])
    st2 = fresh()
    st2.remember(text="The back door has a keypad.", kind="fact")
    f = tempfile.mkdtemp()
    ms = MdSync(st2, f)
    ms.run()
    p = glob.glob(f + "/**/*.md", recursive=True)[0]
    t = open(p).read()
    open(p, "w").write(t.replace("has a keypad", "code is 5150"))
    ms.run()
    ms.run()
    left = [open(x).read() for x in glob.glob(f + "/**/*.md", recursive=True)]
    act = [x[0] for x in st2.db.execute("SELECT body FROM items WHERE status='active'")]
    check("a code typed into a Markdown file is kept in memory, not left in (or written back to) the folder",
          not any("5150" in x for x in left) and any("5150" in b for b in act), (left, act))


async def main():
    await cloud_paths()
    await overnight_paths()
    await journal_paths()
    personal_paths()
    await view_paths()
    await brief_paths()
    revise_paths()
    print(f"\n{len(FAILS)} failed" if FAILS else "\nALL OK")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
