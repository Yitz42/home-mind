"""Phase 15: other assistants as guest front ends (open-level only, nothing private, safety to the phone, turned off by
a person or guest mode, never teaching learned records), the three registers, the four views, and Firefly III totals
(numbers only, owner only, said by code).

  python tests/test_frontends.py
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
    m.WSMsgType = types.SimpleNamespace(TEXT=1)
    m.ClientSession, m.ClientError, m.BasicAuth = object, Exception, (lambda *a: a)
    m.web = types.ModuleType("aiohttp.web")
    sys.modules["aiohttp"] = m
    sys.modules["aiohttp.web"] = m.web
from homemind import frontends as FE, main as M  # noqa: E402
from homemind.channels import set_guest  # noqa: E402
from homemind.day import Day  # noqa: E402
from homemind.decisions import DecisionLog  # noqa: E402
from homemind.home import Home  # noqa: E402
from homemind.learned import Learned  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(c):
    return asyncio.run(c)


OPTS = {"other_assistants": ["alexa: Alexa", "u_gemini: Gemini"]}
check("other: recognised by front end or user id", FE.other_assistant({"front_end": "alexa"}, OPTS)["name"] == "Alexa"
      and FE.other_assistant({"user_id": "u_gemini"}, OPTS)["name"] == "Gemini")
check("other: Home Assistant's own Assist isn't one", FE.other_assistant({"front_end": "assist"}, OPTS) is None)
for q in ("how did I sleep?", "what's my balance?", "where is Dana?", "where's the drill?", "how's my heart rate"):
    check(f"other: refuses {q!r}", FE.other_refuses(q))
check("other: an everyday question is fine", not FE.other_refuses("when do the bins go out?"))


# ---- through App.answer and App._run_plan ------------------------------------------------------------------------
class FakeHA:
    def __init__(self):
        self.calls = []

    async def state(self, eid):
        return {"state": "on"} if eid == "input_boolean.mind_control_enabled" else None

    async def call(self, dom, svc, data, approved=False):
        self.calls.append((dom, svc, data))
        return 200


st = Store(os.path.join(tempfile.mkdtemp(), "mind.db"))
h = Home(st, FakeHA(), {"home_controls": ["light.*", "lock.*"]})
h.catalog = {"light.desk": {"name": "desk lamp", "kind": "device", "domain": "light", "state": "off", "attrs": {}},
             "lock.front": {"name": "front door", "kind": "device", "domain": "lock", "state": "locked", "attrs": {}}}
h.access.baseline(list(h.catalog))
flags = []


async def on_red_flag(flag, text, speaker, cid, source):
    flags.append((flag.kind, source))


asked = []


async def mind_ask(q, *a, **k):
    from homemind.speakers import VIEWER
    asked.append((q, VIEWER.get()))
    return {"answer": "Tuesday night.", "hits": [], "model": "haiku"}
A = types.SimpleNamespace(opts=dict(OPTS), home=h, mind=types.SimpleNamespace(store=st, ask=mind_ask), PENDING_SECONDS=30,
                          on_red_flag=on_red_flag, people=lambda: [], decisions=None)
for n in ("answer", "speaker_for", "_run_plan", "_verb", "has_pending", "pending_reply", "checkin_reply"):
    if hasattr(M.App, n):
        setattr(A, n, getattr(M.App, n).__get__(A))
A.has_pending = lambda cid: False
A.checkin_reply = lambda *a, **k: None
A._style = lambda res, *a, **k: asyncio.sleep(0, result=res)


async def ask_phone(plan, why=None, source="api"):
    return {"plan": plan, "approval_sent": True}
A.ask_phone = ask_phone
r = run(A.answer({"question": "I took a double dose of my pills", "front_end": "alexa"}))
check("other: a red flag isn't answered on its speaker; the real reply goes out through Home Mind",
      r["answer"] == FE.OTHER_ANSWER and flags and flags[-1] == ("double_dose", "other:Alexa"), (r, flags))
r = run(A.answer({"question": "how did I sleep last night?", "front_end": "alexa"}))
check("other: health is never answered there", r["answer"] == FE.OTHER_ANSWER and not asked, r)
try:
    r = run(A.answer({"question": "when do the bins go out?", "front_end": "alexa"}))
except Exception as e:  # noqa: BLE001 - a stand-in app lacks parts of the real one
    r = {"error": repr(e)}
check("other: an everyday question is asked as a guest (no personal notes)",
      asked and asked[-1][1] == "", (r, asked))
r = run(A._run_plan({"entity_id": "lock.front", "action": "off"}, "unlock the front door", "other:Alexa", "c1"))
check("other: nothing that needs a yes or the phone", "Please use Home Mind" in r.get("answer", "") and
      not h.ha.calls, r)
r = run(A._run_plan({"entity_id": "light.desk", "action": "on"}, "desk lamp on", "other:Alexa", "c1"))
check("other: lights and media work", r.get("done") and h.ha.calls[-1][:2] == ("light", "turn_on"), r)
FE.set_assistants(st, "Dana", True)
r = run(A.answer({"question": "when do the bins go out?", "front_end": "alexa"}))
check("other: a person turning them off turns them off", "turned off" in r["answer"], r)
FE.set_assistants(st, "Dana", False)
run(set_guest(types.SimpleNamespace(mind=types.SimpleNamespace(store=st), home=types.SimpleNamespace(catalog={}),
                                    ha=FakeHA()), True))
check("other: guest mode turns them off", FE.assistants_off(st))
run(set_guest(types.SimpleNamespace(mind=types.SimpleNamespace(store=st), home=types.SimpleNamespace(catalog={}),
                                    ha=FakeHA()), False))

# never teaching learned records
dl = DecisionLog(":memory:", quiet_seconds=0)
dl.record("light.desk", "on", None, "light.turn_on", {"entity_id": "light.desk"}, context_id="x1",
          request="turn on the desk lamp", channel="other:Alexa")
dl.db.execute("UPDATE decisions SET seen='match', ts=ts-5000")
dl.db.commit()
lr = Learned(st, h)
check("other: their requests never become learned templates", lr.learn_from(dl)["created"] == 0 and lr.all() == [])

# ---- registers ----------------------------------------------------------------------------------------------------
day = Day(st)
check("register: normal by default", FE.register(st, "Dana", day) == "normal")
day.set("low", "Dana")
check("register: quiet on a low day", FE.register(st, "Dana", day) == "quiet")
day.set("rough", "Dana")
check("register: plain on a rough day", FE.register(st, "Dana", day) == "plain")
day.set("good", "Dana")
check("register: plain right after a failure", FE.register(st, "Dana", day, failed=True) == "plain")
check("register: quiet when the room isn't sure who's there", FE.register(st, "Dana", day, room_unsure=True) == "quiet")
FE.set_plain(st, "Dana", True)
check("register: the plain voice switch", FE.register(st, "Dana", day) == "plain" and FE.register(st, "Sam", day) ==
      "normal")

# ---- views --------------------------------------------------------------------------------------------------------
st.remember("Bins go out Tuesday night.", title="Bins", shelf="home/routines")
st.remember("Dana's dentist is Dr Lee.", title="Dentist", shelf="health/dentist",
            meta={"scope": "personal", "owner": "Dana"})
app = types.SimpleNamespace(mind=types.SimpleNamespace(store=st), home=h)
mem = FE.view_memory(app, "Sam")
check("views: memory by topic, only what you may see", {t["topic"] for t in mem["topics"]} >= {"home"} and
      "health" not in {t["topic"] for t in mem["topics"]}, mem)
check("views: one topic at a time", FE.view_memory(app, "Dana", "health")["notes"][0]["title"] == "Dentist")
rules = FE.view_rules(app)
check("views: rules as the one ladder in plain words", {r["rule"] for r in rules["rules"]} <=
      {"ask me first", "tell me after", "just do it"} and any(r["entity_id"] == "lock.front" and
                                                               r["rule"] == "ask me first" for r in rules["rules"]), rules)
asks = FE.view_asks(app)
check("views: asks one at a time (nothing waiting)", asks == {"next": None, "waiting": 0}, asks)

# ---- Firefly III ----------------------------------------------------------------------------------------------------
j = {"spent-in-USD": {"key": "spent-in-USD", "monetary_value": "-1234.5", "currency_code": "USD",
                      "sub_title": "Groceries at Whole Foods"},
     "earned-in-USD": {"key": "earned-in-USD", "monetary_value": "4000", "currency_code": "USD"},
     "balance-in-USD": {"key": "balance-in-USD", "monetary_value": "2765.5", "currency_code": "USD"},
     "bills-paid-in-USD": {"key": "bills-paid-in-USD", "monetary_value": "-500", "currency_code": "USD",
                           "sub_title": "Rent to J. Smith"}}
t = FE.summarise_firefly(j)
check("firefly: the three totals only, numbers only (no payee or description)",
      t == {"spent": 1234.5, "earned": 4000.0, "balance": 2765.5, "currency": "USD"}, t)
said = FE.say_money(t, __import__("time").time())
check("firefly: said by code", said.startswith("This month: spent 1,234.50 USD") and "Whole Foods" not in said, said)
st.kv_set("firefly", {"totals": t, "at": __import__("time").time()})
A.opts["firefly_owner"] = "Dana"
r = run(A.answer({"question": "how much have I spent this month?", "speaker": "Sam"}))
check("firefly: someone else: private", r["answer"] == "That's private.", r)
r = run(A.answer({"question": "how much have I spent this month?", "speaker": "Dana"}))
check("firefly: its owner: the totals, no model", r["answer"].startswith("This month:") and r["model"] is None, r)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
