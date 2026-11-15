"""Answer quality: past-day questions put the version current that day first; a low match is flagged; answers say
which note they came from; "that's wrong" marks that note; the notes-first reply is read; the options reach the model.

  python tests/test_answers.py
"""
import asyncio
import collections
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind import mind as M  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class Fake:
    """A model that answers with a fixed reply (or one made from the page), and keeps what it was sent."""
    type, cfg = "fake", {}

    def __init__(self, reply):
        self.reply, self.sent = reply, []

    async def chat(self, system, messages, max_tokens=800, temperature=0.2):
        self.sent.append((system, messages[-1]["content"]))
        text = self.reply(system, messages[-1]["content"]) if callable(self.reply) else self.reply
        return {"text": text, "ms": 1, "tokens": 0}


class Models:
    def __init__(self, **ms):
        self.models = ms

    def get(self, n):
        return self.models[n]


async def main():
    # ---- the version current on a past day comes first ----------------------------------------------------------
    m = M.Mind(tempfile.mkdtemp(), None)
    by = collections.defaultdict(list)
    for s in range(1001, 1006):
        r = await m.run_test_now("hard", "brain", seed=s)
        for d in r["details"]:
            by[d["type"]].append(bool(d.get("hit1")))
    rate = {t: sum(v) / len(v) for t, v in by.items()}
    check("yesterday: that day's version first", rate["yesterday"] >= 0.95, rate["yesterday"])
    check("a past weekday: that day's version first", rate["past_day"] >= 0.75, rate["past_day"])
    check("questions about now still get the newest", rate["many_now"] == 1.0 and rate["when_changed"] == 1.0, rate)

    # ---- low match: the abstain suite --------------------------------------------------------------------------
    flags = collections.defaultdict(list)
    for s in range(1001, 1011):
        r = await m.run_test_now("abstain", "brain", seed=s)
        for d in r["details"]:
            flags[d["type"]].append(d["flagged"])
    rem = sum(flags["abstain_removed"]) / len(flags["abstain_removed"])
    ctl = sum(flags["abstain_control"]) / len(flags["abstain_control"])
    check("missing answers flagged (most)", rem >= 0.7, rem)
    check("answerable questions rarely flagged", ctl <= 0.06, ctl)

    # ---- asking: sources, low match, "that's wrong", notes first, order ----------------------------------------
    d = tempfile.mkdtemp()
    plain = Fake(lambda sys_, page: "The Kosk locker code is 4417.")
    first = Fake(lambda sys_, page: "NOTES: #{}\nANSWER: It's 4417.".format(
        re.search(r"#(\d+)\] .*Kosk locker code", page).group(1)))
    shrug = Fake("I don't know.")
    m = M.Mind(d, Models(plain=plain, first=first, shrug=shrug))
    st = m.store
    old = st.remember(text="The Kosk locker code is 9921.", kind="fact")
    st.clock = lambda: time.time() + 60
    new = st.remember(text="The Kosk locker code is 4417.", kind="fact")
    st.remember(text="The Kosk locker is in the garage.", kind="fact")
    for i in range(6):
        st.remember(text=f"The Bexbel box {i} holds spare fuses number {i}.", kind="note")

    r = await m.ask("What's the Kosk locker code?", "plain", fallback=False, conversation_id="c1")
    check("answer names its note", [x["id"] for x in r["sources"]] == [new["id"]], r["sources"])
    check("source has the date it was noted", r["sources"] and re.match(r"[A-Z][a-z]{2} \d\d, \d{4}", r["sources"][0]["noted"]),
          r["sources"])
    check("good match not flagged", r["weak"] is False)

    r = await m.ask("That's wrong", "plain", fallback=False, conversation_id="c1")
    check("'that's wrong' marks the source note", r.get("marked_wrong") == new["id"] and "marked" in r["answer"], r)
    ev = st.db.execute("SELECT * FROM events WHERE type='feedback' ORDER BY id DESC LIMIT 1").fetchone()
    check("feedback logged against it", ev is not None and ev["item_id"] == new["id"], ev and dict(ev))
    r = await m.ask("That's wrong", "plain", fallback=False, conversation_id="other")
    check("'that's wrong' with nothing before is just a question", "marked_wrong" not in r)

    r = await m.ask("What's the wifi password for the guest network?", "shrug", fallback=False)
    check("low match flagged", r["weak"] is True)
    check("the model is told", M.LOW_MATCH in shrug.sent[-1][1])
    check("no source for 'I don't know'", r["sources"] == [])

    m.NOTES_FIRST = True
    r = await m.ask("What's the Kosk locker code?", "first", fallback=False)
    check("notes-first: system asks for NOTES/ANSWER", M.NOTES_FIRST in first.sent[-1][0])
    check("notes-first: answer read after ANSWER:", r["answer"] == "It's 4417.", r["answer"])
    check("notes-first: the note it named is the source", [x["id"] for x in r["sources"]] == [new["id"]], r["sources"])
    await m.ask("What's the Kosk locker code?", "first", fallback=False, voice=True)
    check("notes-first is skipped for voice", M.NOTES_FIRST not in first.sent[-1][0])
    m.NOTES_FIRST = False

    m.ORDER = "edges"
    await m.ask("Which Bexbel box holds spare fuses number 3?", "plain", fallback=False)
    page = plain.sent[-1][1]
    check("edges: best note repeated before the question", "BEST MATCH (repeated from above)" in page and
          page.index("BEST MATCH") < page.index("QUESTION:"), page[-300:])
    await m.ask("What's the Kosk locker code?", "plain", fallback=False)
    check("edges: short pages left alone", "BEST MATCH" not in plain.sent[-1][1])
    m.ORDER = "default"
    await m.ask("Which Bexbel box holds spare fuses number 3?", "plain", fallback=False)
    check("default order unchanged", "BEST MATCH" not in plain.sent[-1][1])

    # no model answers: the brain's own answer says when it is a low match
    class Down:
        type, cfg = "down", {}

        async def chat(self, *a, **k):
            raise RuntimeError("off")
    m.models.models["down"] = Down()
    r = await m.ask("Where are the spare smoke detector batteries kept?", "down", fallback=False)
    check("brain fallback says it isn't sure", r.get("fallback") == "brain" and r["weak"] and "clearly matches" in r["answer"],
          r["answer"])
    r = await m.ask("What's the Kosk locker code?", "down", fallback=False)
    check("brain fallback gives the newest note", "4417" in r["answer"] and r["sources"][0]["id"] == new["id"], r)

    # unit pieces
    check("read_answer without the format keeps the text", M.read_answer("Just 4417.", True) == ("Just 4417.", []))
    check("read_answer finds ids", M.read_answer("NOTES: #3, #9\nANSWER: Yes.", True) == ("Yes.", [3, 9]))
    hits = st.recall("Kosk locker code", limit=5, touch=False)
    check("sources_for: value match", [x["id"] for x in M.sources_for("It's 4417.", hits, "Kosk code")][:1] == [new["id"]])
    check("sources_for: old value points to the old note", old["id"] in
          [x["id"] for x in M.sources_for("It used to be 9921.", st.recall("Kosk locker code", limit=5, include_old=True,
                                                                          touch=False), "What was the Kosk code before?")])

    print("ANSWERS OK" if not FAILS else f"ANSWERS FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
