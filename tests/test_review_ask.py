"""Expert review regressions: asking, end to end (what the user hears). Fake models that echo or record their page,
and the no-model path. No Home Assistant or network needed.

  python tests/test_review_ask.py
"""
import asyncio
import logging
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
from homemind import mind as M  # noqa: E402
from homemind.mind import Mind, followup, spoken  # noqa: E402
from homemind.messaging import tell  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []
logging.disable(logging.CRITICAL)


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class Fake:
    """A local model that records the page it was given and replies with a fixed text."""
    type, cfg = "ollama", {"url": "http://192.168.1.5:11434"}

    def __init__(self, reply="ECHO"):
        self.sent, self.reply = [], reply

    async def chat(self, system, messages, **k):
        self.sent.append((system, messages[-1]["content"]))
        return {"text": self.reply, "ms": 1}


class Down(Fake):
    async def chat(self, *a, **k):
        raise RuntimeError("off")


class Models:
    def __init__(self, **m):
        self.models = m

    def get(self, n):
        return self.models[n]


FILL = ["The wifi password is hunter22.", "The shed key hangs by the back door.", "Dana works at the library.",
        "The bins go out on Tuesday.", "The boiler was serviced in March.", "The red toolbox is in the shed.",
        "The car insurance renews in June.", "The dog's vet is Dr Pell."]


def mind(**models):
    m = Mind(tempfile.mkdtemp(), Models(**models))
    m.FALLBACK = []
    for t in FILL:
        m.store.remember(text=t)
    return m


async def temporal():
    f = Fake()
    m = mind(f=f)
    now = time.time()
    m.store.remember(text="The garage door code is 4417.", kind="fact", key="garage door|code", at=now - 40 * 86400)
    m.store.remember(text="The garage door code is 5512.", kind="fact", key="garage door|code", at=now - 2 * 86400)
    for q in ["What was the garage door code last month?", "What was the garage door code a month ago?",
              "What was the garage door code a week ago?"]:
        await m.ask(q, "f", fallback=False)
        page = f.sent[-1][1]
        check(f"'{q}': the model is shown the version current then, marked", "4417" in page and "TRUE ON" in page,
              page[:400])
    await m.ask("What was the garage door code two weeks ago?", "f", fallback=False)
    check("a time phrase doesn't make a plain question a LOW MATCH", M.LOW_MATCH not in f.sent[-1][1], f.sent[-1][1][:300])
    check("... a question with nothing like it still is",
          M.weak_match(m.store, "What colour is the hallway carpet two weeks ago?",
                       M.find(m.store, "What colour is the hallway carpet two weeks ago?", touch=False)))
    at = M.asked_time("what was it 2 months ago?", time.mktime((2026, 3, 31, 12, 0, 0, 0, 0, -1)))
    check("'2 months ago' from Mar 31 is Jan 31", at and at[1] == "Sat Jan 31", at)
    at = M.asked_time("what was it last month?", time.mktime((2026, 1, 10, 12, 0, 0, 0, 0, -1)))
    check("'last month' in January is the end of December", at and at[1] == "Wed Dec 31", at)


async def corrections():
    f = Fake("The garage door code is 4417.")
    m = mind(f=f)
    m.store.remember(text="The garage door code is 4417.", kind="fact", key="garage door|code")
    act = lambda: m.store.active_by_key("garage door|code")["body"]
    await m.ask("What is the garage door code?", "f", fallback=False, conversation_id="a")
    await m.ask("That's wrong", "f", fallback=False, conversation_id="a")
    r = await m.ask("never mind", "f", fallback=False, conversation_id="a")
    check("'never mind' after 'that's wrong' is not filed as the right value", "filed" not in r and "4417" in act(),
          (r["answer"], act()))
    r = await m.ask("5512", "f", fallback=False, conversation_id="a")
    check("... and the correction is closed", "filed" not in r and "4417" in act())

    await m.ask("What is the garage door code?", "f", fallback=False, conversation_id="b")
    await m.ask("That's wrong", "f", fallback=False, conversation_id="b")
    r = await m.ask("show me the shed key", "f", fallback=False, conversation_id="b")
    check("a command after 'that's wrong' is not filed over the fact", "filed" not in r and "4417" in act(), act())

    await m.ask("What is the garage door code?", "f", fallback=False, conversation_id="c")
    f.sent.clear()
    r = await m.ask("No, it's 5512", "f", fallback=False, conversation_id="c")
    check("'No, it's 5512' right after an answer files the right value",
          r.get("filed") and act() == "The garage door code is 5512." and r.get("marked_wrong"), (r.get("answer"), act()))
    check("... and the model never got it as a question", not f.sent, f.sent[-1:])

    m.store.remember(text="The garage door code is 4417.", kind="fact", key="garage door|code")
    await m.ask("What is the garage door code?", "f", fallback=False, conversation_id="d")
    r = await m.ask("Actually it's 7788 now", "f", fallback=False, conversation_id="d")
    check("'Actually it's 7788 now' files 'The garage door code is 7788.'", act() == "The garage door code is 7788.",
          act())
    m.store.remember(text="The garage door code is 4417.", kind="fact", key="garage door|code")
    await m.ask("What is the garage door code?", "f", fallback=False, conversation_id="e")
    r = await m.ask("No, it's fine", "f", fallback=False, conversation_id="e")
    check("'No, it's fine' is not a correction", "filed" not in r and "4417" in act(), act())
    await m.ask("What is the garage door code?", "f", fallback=False, conversation_id="g")
    r = await m.ask("No, it's not", "f", fallback=False, conversation_id="g")
    check("'No, it's not' marks the note and asks for the right value", r.get("marked_wrong") and "filed" not in r and
          "4417" in act(), r.get("answer"))


async def voice():
    f = Fake("**The code is 4417** (#{id}). See https://example.com/codes for more.")
    m = mind(f=f, down=Down())
    n = m.store.remember(text="The garage door code is 4417.", kind="fact", key="garage door|code")
    f.reply = f.reply.replace("{id}", str(n["id"]))
    m.store.remember(title="Boiler reset", text="Boiler manual: https://example.com/boiler.pdf - hold the reset button "
                     "for 10 seconds. " + "Then wait for the pilot light and check the pressure gauge. " * 12)
    r = await m.ask("What is the garage door code?", "f", fallback=False, voice=True)
    a = r["answer"]
    check("voice: no markdown, note id or URL in the spoken answer", "4417" in a and not any(
        x in a for x in ("*", "#", "http")), a)
    check("voice: the cited note is still the source", [s["id"] for s in r["sources"]] == [n["id"]], r["sources"])
    check("voice: the model is told the answer is spoken", M.VOICE_SYSTEM in f.sent[-1][0])
    r = await m.ask("What is the garage door code?", "f", fallback=False)
    check("typed answers are left as the model wrote them", "**" in r["answer"])
    r = await m.ask("How do I reset the boiler?", "down", fallback=False, voice=True)
    check("voice fallback: the best note is cut short and has no URL",
          len(r["answer"]) < 400 and "http" not in r["answer"] and "reset" in r["answer"], r["answer"])
    r = await m.ask("How do I reset the boiler?", None, voice=True)
    check("voice, no model: the same", len(r["answer"]) < 400 and "http" not in r["answer"], r["answer"])
    check("spoken: '#3' that isn't a note id is read as a number",
          spoken("Spare fuses are in box #3 (#12).", ids={12}) == "Spare fuses are in box number 3.",
          spoken("Spare fuses are in box #3 (#12).", ids={12}))


async def disagreement():
    m = mind(down=Down())
    now = time.time()
    m.store.remember(text="Dana says the Kosk locker code is 4417.", kind="fact", at=now - 2 * 86400)
    m.store.remember(text="Sam says the Kosk locker code is 9999.", kind="fact", at=now - 86400)
    for model in (None, "down"):
        r = await m.ask("What is the code for the Kosk locker?", model, fallback=False)
        check(f"no model ({model}): both sides of a disagreement are given", "4417" in r["answer"] and
              "9999" in r["answer"], r["answer"])


def followups():
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    for t in ["The Kosk locker code is 4417.", "The drill is in the red toolbox in the shed.",
              "The boiler beeps when the pressure is low."]:
        st.remember(text=t)
    for prev, q, want in [("Where is the drill?", "Is it going to rain today?", "Is it going to rain today?"),
                          ("Where is the drill?", "What time is it?", "What time is it?"),
                          ("Where is the drill?", "How do I reset the boiler if it beeps?",
                           "How do I reset the boiler if it beeps?"),
                          ("Where is the drill?", "Thanks, that's it", "Thanks, that's it"),
                          ("What is the code for the Kosk locker?", "Where is it?", "Where is the Kosk locker?"),
                          ("Where is the Kosk locker?", "What's the code for it?", "What's the code for the Kosk locker?"),
                          ("Where is the drill?", "Who gave it to me?", "Who gave the drill to me?")]:
        got = followup(q, prev, st)
        check(f"follow-up {q!r} after {prev!r} -> {want!r}", got == want, got)


def telegram():
    for t in ["Remember where I put the drill?", "remember what the wifi password is", "Remember when I changed the code?"]:
        check(f"telegram: {t!r} is a question, not a note", tell(t) is None, tell(t))
    for t, want in [("remember the bins go out Tuesday", "the bins go out Tuesday"),
                    ("Remember that the code is 4417", "the code is 4417"), ("Note to self: buy milk", "buy milk"),
                    ("remember: when the dryer beeps, empty the filter", "when the dryer beeps, empty the filter"),
                    ("/remember where's the drill? in the shed", "where's the drill? in the shed")]:
        check(f"telegram: {t!r} is filed", tell(t) == want, tell(t))


async def main():
    await temporal()
    await corrections()
    await voice()
    await disagreement()
    followups()
    telegram()
    print(f"\n{len(FAILS)} failed" if FAILS else "\nall ok")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
