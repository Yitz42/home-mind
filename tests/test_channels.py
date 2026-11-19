"""Phase 10: channels prove their sender and keep to their limits, guest mode, the monthly spend cap, and new
behaviours agreed on a clear day with 24 hours to change one's mind. No Home Assistant or model needed.

  python tests/test_channels.py
"""
import asyncio
import email
import email.policy
import os
import sys
import tempfile
import time
import types
from datetime import date, datetime, timedelta

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
from homemind import channels as C, voice as V  # noqa: E402
from homemind.consent import Consent  # noqa: E402
from homemind.day import Day  # noqa: E402
from homemind.learned_answers import LearnedAnswers  # noqa: E402
from homemind.mailbox import Mailbox  # noqa: E402
from homemind.messaging import Telegram  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.usage import Usage  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(c):
    return asyncio.run(c)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def store():
    return Store(os.path.join(tempfile.mkdtemp(), "mind.db"))


# ---- limits -------------------------------------------------------------------------------------------------------
clk = Clock(1000.0)
lim = C.Limits(clock=clk)
got = [lim.allow("telegram", 5) for _ in range(31)]
check("limits: 30 Telegram messages an hour per chat, then no", all(got[:30]) and not got[30])
check("limits: per chat", lim.allow("telegram", 6))
clk.t += 3601
check("limits: an hour later, again", lim.allow("telegram", 5))

# ---- Telegram: a listed group needs a listed sender ----------------------------------------------------------------
st = store()
asked = []


async def ask(text, model=None, **kw):
    asked.append(text)
    return {"answer": "Tuesday."}
app = types.SimpleNamespace(mind=types.SimpleNamespace(store=st, ask=ask), limits=C.Limits())
tg = Telegram(app, "t", ["111: Yitz", "-500: Family"])
r = run(tg.handle({"chat": {"id": 111, "type": "private"}, "from": {"id": 111}, "text": "when are the bins?"}))
check("telegram: a listed person in their own chat", r == "Tuesday.")
r = run(tg.handle({"chat": {"id": -500, "type": "group"}, "from": {"id": 999}, "text": "what's the gate code?"}))
check("telegram: in a listed group, someone who isn't listed is ignored", r is None and len(asked) == 1)
r = run(tg.handle({"chat": {"id": -500, "type": "group"}, "from": {"id": 111}, "text": "when are the bins?"}))
check("telegram: in a listed group, a listed person is answered", r == "Tuesday.")

# ---- email: only mail its server vouched for -----------------------------------------------------------------------


def mail(auth=None, sender="me@example.com", mid="<1@x>"):
    raw = f"From: {sender}\nSubject: Boiler warranty\nMessage-ID: {mid}\n"
    if auth:
        raw += f"Authentication-Results: mx.example.net; {auth}\n"
    raw += "\nThe boiler warranty runs to 2030.\n"
    return raw.encode()


mb = Mailbox(types.SimpleNamespace(mind=types.SimpleNamespace(store=st), limits=C.Limits()),
             {"email_senders": ["me@example.com"]})
r = mb.file(mail())
check("email: no sender check in the headers: not read", "sender check" in (r.get("skipped") or ""), r)
r = mb.file(mail("dkim=fail header.d=example.com; spf=softfail smtp.mailfrom=example.com", mid="<2@x>"))
check("email: a failed check: not read", "sender check" in (r.get("skipped") or ""), r)
r = mb.file(mail("dkim=pass header.d=evil.com", mid="<3@x>"))
check("email: a pass for someone else's domain doesn't count", "sender check" in (r.get("skipped") or ""), r)
r = mb.file(mail("dkim=pass header.d=example.com; spf=pass smtp.mailfrom=example.com", mid="<4@x>"))
check("email: a pass for the sender's own domain: filed", r.get("id"), r)
check("email: never a learned answer from it", "never a learned record" in
      (LearnedAnswers(st).check_answer(st.get(r["id"]), "The boiler warranty runs to 2030.") or ""))
mb2 = Mailbox(types.SimpleNamespace(mind=types.SimpleNamespace(store=st), limits=C.Limits()),
              {"email_senders": ["me@example.com"], "email_require_verified": False})
check("email: the check can be turned off for servers that don't add the header", mb2.file(mail(mid="<5@x>")).get("id"))

# ---- guest mode ---------------------------------------------------------------------------------------------------
st = store()
done = []


class App:
    def __init__(self):
        self.mind = types.SimpleNamespace(store=st)
        self.ha = types.SimpleNamespace(call=self.call)
        self.home = types.SimpleNamespace(catalog={"switch.kitchen_satellite_mute": {}, "light.desk": {}})

    async def call(self, dom, svc, data, approved=False):
        done.append((dom, svc, data["entity_id"]))

    async def answer(self, b, scope="admin"):
        return {"answer": "Tuesday night."}

    async def do(self, *a, **k):
        return {"answer": "done"}


A = App()
r = run(C.set_guest(A, True))
check("guest: on: the satellites' mute switches are turned on", done == [("switch", "turn_on",
                                                                          "switch.kitchen_satellite_mute")], done)
r = run(V.turn(A, {"text": "when do the bins go out?"}))
check("guest: nothing answered by voice", r.get("guest") and "not listening" in r["answer"], r)
r = run(V.turn(A, {"text": "I took a double dose of my pills"}))
check("guest: safety phrases still are", r.get("red_flag"), r)
run(C.set_guest(A, False))
check("guest: off: answered again", run(V.turn(A, {"text": "when do the bins go out?"}))["answer"] == "Tuesday night.")

# ---- the spend cap ------------------------------------------------------------------------------------------------
st = store()
u = Usage(st, {"haiku": (1.0, 5.0)})
for _ in range(3):
    u.record("haiku", True, 1_000_000, 200_000)
check("spend: this month's cost from usage", C.month_spend(u) == 6.0, C.month_spend(u))
check("spend: over a cap of 5", C.over_cap(u, 5) and not C.over_cap(u, 0) and not C.over_cap(u, 50))
from homemind import mind as MI  # noqa: E402
mi = MI.Mind(tempfile.mkdtemp(), types.SimpleNamespace(models={"haiku": 1, "gemma": 1}))
mi.FALLBACK = ("haiku", "gemma")
mi.blocked_models = lambda: C.priced(u) if C.over_cap(u, 5) else set()
check("spend: over the cap, cloud models leave the chain; local ones stay", mi._chain("haiku") == ["gemma"],
      mi._chain("haiku"))

# ---- consent on a clear day ---------------------------------------------------------------------------------------
st = store()
noon = datetime.combine(date.today(), datetime.min.time()).replace(hour=11).timestamp()
cc = Clock(noon)
day = Day(st, clock=cc)
con = Consent(st, day, clock=cc)
ran = []


async def turn_on(p):
    ran.append(p["entity_id"])
    return {"on": p["entity_id"]}
con.appliers["automation_on"] = turn_on
check("clear day: no check-in yet is never clear", not con.clear_day()[0])
day.set("low")
check("clear day: a low day isn't", not con.clear_day()[0])
c1 = con.propose("automation_on", "Switch on: Porch at dusk", {"entity_id": "automation.porch"})
r = con.yes(c1["id"])
check("consent: a yes on a day that isn't clear waits for one", r["status"] == "waiting" and r["why"], r)
day.set("good")
allx = day._all()
allx[(date.today() - timedelta(days=1)).isoformat()] = {"_": {"state": "rough", "at": noon - 86400, "how": "said"}}
st.kv_set(day.KV, allx)
check("clear day: a rough day yesterday: not yet", not con.clear_day()[0] and "rough" in con.clear_day()[1])
allx.pop((date.today() - timedelta(days=1)).isoformat())
st.kv_set(day.KV, allx)
check("clear day: good today, nothing rough lately, before 8 pm", con.clear_day()[0], con.clear_day())
check("consent: waiting changes are asked about again on a clear day", [c["id"] for c in con.to_ask()] == [c1["id"]])
r = con.yes(c1["id"])
check("consent: a yes on a clear day: agreed, taking effect in 24 hours", r["status"] == "agreed" and
      abs(r["effective"] - noon - 86400) < 1, r)
c2 = con.propose("automation_on", "Switch on: Hall at night", {"entity_id": "automation.hall"})
check("clear day: one yes is enough for a day", not con.clear_day()[0] and con.yes(c2["id"])["status"] == "waiting")
cc.t += 3600
check("consent: nothing happens before the 24 hours", run(con.due()) == [] and ran == [])
con.change_mind(c1["id"]) if False else None
cc.t += 86400
out = run(con.due())
check("consent: after 24 hours it takes effect", ran == ["automation.porch"] and con.get(c1["id"])["status"] == "done",
      out)
c3 = con.propose("automation_on", "Switch on: Garden", {"entity_id": "automation.garden"})
cc.t += 86400
day.set("good")
con.yes(c3["id"])
con.change_mind(c3["id"])
cc.t += 2 * 86400
run(con.due())
check("consent: changing your mind within 24 hours: it never happens", "automation.garden" not in ran and
      con.get(c3["id"])["status"] == "cancelled")
cc.t = noon + 9.5 * 3600
check("clear day: not after 8 pm", not con.clear_day()[0])
try:
    con.propose("run_anything", "x")
    check("consent: only kinds the app knows how to apply", False)
except ValueError:
    check("consent: only kinds the app knows how to apply", True)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
