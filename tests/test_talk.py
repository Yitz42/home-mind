"""Talk (talk.py): the panel's conversation, by voice or typing, with the answers Home Mind expects as buttons.

  python tests/test_talk.py
"""
import asyncio
import os
import sys
import tempfile
import time
import types
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: None
    m.WSMsgType = types.SimpleNamespace(TEXT=1)
    m.ClientSession, m.ClientError, m.BasicAuth = object, Exception, (lambda *a: a)
    m.web = types.ModuleType("aiohttp.web")
    sys.modules["aiohttp"] = m
    sys.modules["aiohttp.web"] = m.web
from homemind.talk import Talk, choices_for, FOLLOW_UPS  # noqa: E402
from homemind.day import Day, QUESTION  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(c):
    return asyncio.run(c)


# ---- choices ---------------------------------------------------------------------------------------------------
check("choices: the check-in", choices_for({"answer": f"OK. {QUESTION}", "checkin_asked": True}) ==
      (["Good", "Low", "Rough"], "choice"))
check("choices: an action that needs a yes", choices_for({"answer": "Do you want me to open the garage door?",
                                                          "confirm_needed": True}) == (["Yes", "No"], "choice"))
check("choices: which one", choices_for({"answer": "Which one: desk lamp, desk light strip?",
                                         "which": ["desk lamp", "desk light strip"]})[0] == ["desk lamp",
                                                                                             "desk light strip"])
check("choices: which one from the words alone", choices_for({"answer": "Which one: hall, porch or garden?"})[0] ==
      ["hall", "porch", "garden"])
check("choices: an 'A or B?' question", choices_for({"answer": "Did you mean the morning one or the evening one?"})[0]
      == ["The morning one", "The evening one"], choices_for({"answer": "Did you mean the morning one or the evening one?"}))
check("choices: a connected answer offers follow-ups, not waiting", choices_for({"answer": "x", "deep": True}) ==
      (FOLLOW_UPS, None))
check("choices: a plain answer, none", choices_for({"answer": "The bins go out on Tuesday."}) == ([], None))

# ---- turns -----------------------------------------------------------------------------------------------------
st = Store(os.path.join(tempfile.mkdtemp(), "mind.db"))
NOW = time.mktime(datetime(2026, 11, 19, 15, 0).timetuple())
asked, done = [], []


async def answer(b, scope="admin"):
    asked.append(b)
    if b.get("force_deep"):
        return {"answer": "Try a few days without lentils.", "deep": True, "hits": []}
    if "gassy" in b["question"]:
        return {"answer": "Commonly beans; you had lentils 3 days.", "deep": True, "hits": [],
                "quick": {"answer": "I don't know."}, "compare_id": "c1"}
    return {"answer": "Tuesday.", "hits": []}


async def do(text, source="api", conversation_id=None, deadline=None, area=None):
    done.append((text, conversation_id))
    if "garage" in text:
        return {"answer": "Do you want me to open the garage door?", "confirm_needed": True}
    return {"done": {"service": "light.turn_off", "entity_id": "light.desk", "name": "desk lamp"}}
app = types.SimpleNamespace(mind=types.SimpleNamespace(store=st), answer=answer, do=do,
                            day=Day(st, clock=lambda: NOW))
tk = Talk(app, clock=lambda: NOW)
r = run(tk.turn("Why am I feeling so gassy lately?", "Dana"))
check("turn: a question goes to App.answer in this person's conversation", asked[-1]["speaker"] == "Dana" and
      asked[-1]["conversation_id"].startswith("talk:"), asked[-1])
check("turn: the check-in asked once, with its buttons", r["answer"].endswith(QUESTION) and
      r["choices"] == ["Good", "Low", "Rough"] and r["expects"] == "choice", r)
check("turn: the side-by-side comes back", r.get("compare_id") == "c1" and r["quick"]["answer"] == "I don't know.")
r2 = run(tk.turn("What could I try?", "Dana"))
check("turn: a follow-up after a connected answer asks the same question again with it",
      asked[-1].get("force_deep") and "gassy" in asked[-1]["question"], asked[-1])
check("turn: the check-in isn't asked twice", QUESTION not in r2["answer"])
r3 = run(tk.turn("turn off the desk lamp", "Dana"))
check("turn: a house command goes to App.do, in the same conversation", done[-1][1] == asked[-1]["conversation_id"]
      and r3["answer"] == "OK, the desk lamp is off.", (done, r3))
r4 = run(tk.turn("open the garage door", "Dana"))
check("turn: an action that needs a yes offers Yes and No", r4["choices"] == ["Yes", "No"] and r4["expects"] == "choice")
th = tk.thread("Dana")
check("thread: kept, with who said what and the choices", len(th["turns"]) == 8 and th["turns"][0]["who"] == "you"
      and th["turns"][1].get("deep") and th["turns"][-1]["choices"] == ["Yes", "No"], th["turns"])
check("thread: per person", tk.thread("Sam")["turns"] == [])
cid = th["conversation_id"]
n = tk.new("Dana")
check("new conversation: a fresh thread and conversation id", n["turns"] == [] and n["conversation_id"] != cid)
run(tk.turn("when do the bins go out?", ""))
check("guest: never kept", tk.thread("")["turns"] == [] and tk.thread("").get("guest"))
for i in range(120):
    run(tk.turn(f"q {i}", "Sam"))
check("thread: the last 100 turns", len(tk.thread("Sam")["turns"]) == 100)
try:
    run(tk.turn("  ", "Dana"))
    empty = False
except ValueError:
    empty = True
check("turn: nothing said is a 400", empty)

print()
print("all passed" if not FAILS else f"{len(FAILS)} failed: {', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
