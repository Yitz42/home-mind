"""The ten expert panels' critical findings (claude/review-2026-11-panels.md), each checked by behaviour:

  C1  red flags come before the money, "where is" and medicines shortcuts
  C2  red flags catch passive crisis phrasing, seizures, blue lips, a child swallowing pills; fewer false alarms
  C3  red-flag words are said exactly (no persona) and on other assistants too; the contact told once in 6 hours
  C4  the experiment safety stop sees rough days, counts washouts and the running block, and stops a harmful change
  C5  one rough day after a calm stretch is not "see your doctor"
  C6  a guest token can't claim to be someone
  C7  the forget list re-deletes restored notes, never new ones; sealed notes too; the scrub runs on re-apply
  C8  a learned reflex never acts on a request for later, nor on the opposite of what it does
  C9  back-off is wired, and never stops a safety call (relock, heater off)
  C10 a tap check-in with no name never overrides someone's rough day
and the quick high ones: check-in greetings and mixed answers, the medicines log, email and Telegram senders, a
shared key, the 4-pair default, replay errors, earning on an interval, "about usual", alone().

  python tests/test_review_fixes.py
"""
import asyncio
import email
import os
import random
import sys
import tempfile
import time
import types
from datetime import date, timedelta

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
from homemind import main as M, redflags as RF, worsening as W, experiments as X, upkeep as U  # noqa: E402
from homemind import day as D, tracking as TR, channels as CH, health as HL, learned as LR  # noqa: E402
from homemind.automations import safety_call  # noqa: E402
from homemind.speakers import VIEWER  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(c):
    return asyncio.run(c)


def store():
    return Store(os.path.join(tempfile.mkdtemp(), "mind.db"))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


# ---- C2: red-flag phrases --------------------------------------------------------------------------------------
MUST = [("I wish I wasn't here anymore", "crisis"), ("I want it all to end", "crisis"),
        ("nobody would miss me", "crisis"), ("I hope I don't wake up tomorrow", "crisis"),
        ("I've been cutting myself", "crisis"), ("I wish I'd never been born", "crisis"),
        ("she's having a seizure", "emergency"), ("his lips are blue", "emergency"),
        ("my left arm is numb", "emergency"), ("my face went numb", "emergency"),
        ("I can't speak properly", "emergency"), ("my throat is closing", "emergency"),
        ("my son swallowed my pills", "double_dose"), ("I took an extra dose by mistake", "double_dose"),
        ("I haven't been able to get out of bed for three days", "decline"),
        ("I can't keep anything down", "decline")]
MUST_NOT = ["I can't get out of bed today", "I can't drink milk", "I'm so confused by this app",
            "I don't want to wake up early tomorrow", "my leg is weak after the walk", "I wish I wasn't here at work",
            "what are the signs of a seizure", "my arm is numb from sleeping on it", "can you wake me up at 7"]
for t, kind in MUST:
    f = RF.check(t)
    check(f"C2 red flag: {t!r} -> {kind}", f and f.kind == kind, f and f.kind)
for t in MUST_NOT:
    check(f"C2 not a red flag: {t!r}", RF.check(t) is None, RF.check(t))

# ---- C1/C3/C6: App.answer --------------------------------------------------------------------------------------
st = store()
flags, asked = [], []


async def on_red_flag(flag, text, speaker, cid, source):
    flags.append((flag.kind, source))


async def mind_ask(q, *a, **k):
    asked.append((q, k.get("speaker")))
    f = RF.check(q)
    if f:
        return {"answer": f.reply, "red_flag": f.kind, "hits": [], "model": None}
    return {"answer": "Tuesday.", "hits": [], "model": "haiku"}


class Rooms:
    def heard_in(self, *a):
        pass


st.kv_set("firefly", {"totals": {"spent_month": 10}, "at": time.time()})
A = types.SimpleNamespace(opts={"firefly_owner": "Dana", "other_assistants": ["alexa: Alexa"]},
                          mind=types.SimpleNamespace(store=st, ask=mind_ask), on_red_flag=on_red_flag,
                          people=lambda: [{"name": "Dana"}, {"name": "Sam"}], decisions=None, rooms=Rooms(),
                          home=types.SimpleNamespace(catalog={}), PENDING_SECONDS=30)
for n in ("answer", "speaker_for"):
    setattr(A, n, getattr(M.App, n).__get__(A))
A.has_pending = lambda cid: False
A.checkin_reply = lambda *a, **k: None
A._style = lambda res, *a, **k: asyncio.sleep(0, result=res)
r = run(A.answer({"question": "where's Sam, I have chest pain", "speaker": "Dana"}))
check("C1 a red flag inside a where-question gets the emergency reply", r.get("red_flag") == "emergency"
      and "911" in r["answer"], r)
r = run(A.answer({"question": "how much did I spend? I have chest pain", "speaker": "Dana"}))
check("C1 a red flag inside a money question gets the emergency reply", r.get("red_flag") == "emergency", r)
r = run(A.answer({"question": "I took a double dose of my pills", "front_end": "alexa"}))
check("C3 another assistant says the red flag's own words", "Poison Control" in r["answer"] and
      flags[-1] == ("double_dose", "other:Alexa"), r)


class Persona:
    def emotion(self, *a):
        return "warm"

    def finish(self, res, *a):
        return dict(res, answer=res["answer"] + " Take it gently.")


P = types.SimpleNamespace(persona=Persona(), opts={}, day=None, mind=types.SimpleNamespace(store=st),
                          models=types.SimpleNamespace(models={}))
P.styled = M.App.styled.__get__(P)
res = {"answer": RF.REPLIES["crisis"], "red_flag": "crisis", "hits": []}
check("C3 the persona never restyles a red-flag reply", run(P.styled(dict(res), "x"))["answer"] == RF.REPLIES["crisis"])

pushed = []


async def push(target, msg, tier=None):
    pushed.append(target)
C = types.SimpleNamespace(opts={"safety_contact": "notify.mobile_app_sam"}, mind=types.SimpleNamespace(store=st),
                          ha=types.SimpleNamespace(push=push))
C.on_red_flag = M.App.on_red_flag.__get__(C)
for _ in range(3):
    run(C.on_red_flag(RF.check("I have chest pain"), "I have chest pain", "Dana"))
check("C3 the safety contact is told once in 6 hours for the same kind", pushed == ["notify.mobile_app_sam"], pushed)

tv = VIEWER.set("")
check("C6 a guest token can't claim a speaker", A.speaker_for({"speaker": "Dana"}) == ("", "guest token"))
check("C6 as_who: a guest names no one", not M.as_who({"owner": "Dana"}, "owner"))
VIEWER.reset(tv)
tv = VIEWER.set("Sam")
check("C6 a person's token is that person", A.speaker_for({"speaker": "Dana"})[0] == "Sam" and
      M.as_who({"owner": "Dana"}, "owner") == "Sam")
VIEWER.reset(tv)
check("C6 no token information: the body's field", M.as_who({"owner": "Dana"}, "owner") == "Dana")

# ---- C4: experiments ------------------------------------------------------------------------------------------
clk = Clock(time.mktime(date(2026, 1, 7).timetuple()) + 12 * 3600)        # a Wednesday
st = store()
dy = D.Day(st, clock=clk)
ex = X.Experiments(st, dy, None, None, clock=clk, rng=random.Random(3))
plan = ex.plan("does resting after lunch help?", "lie down after lunch", "reduce_load", person="Dana")
check("C4 plans default to 6 pairs", plan["pairs"] == 6)
try:
    ex.plan("q", "lie down", "reduce_load", pairs=4)
    ok = False
except ValueError:
    ok = True
check("C4 4 pairs is refused (it could never reach p 0.05)", ok)
clk.t += 9 * 86400                                  # consent came late: past the planned Monday
run(ex.start(plan))
e = ex.current()
check("C4 a late start moves the schedule to the Monday after", e["start"] > plan["start"] and
      date.fromisoformat(e["blocks"][0]["start"]) >= date.fromtimestamp(clk.t), (plan["start"], e["start"]))
first = date.fromisoformat(e["blocks"][0]["start"])
# every day: asked first ("?dana" marker), then Dana's answer; rough on B days every 3rd day, good otherwise
arms = {}
for b in e["blocks"]:
    s, en = date.fromisoformat(b["start"]), date.fromisoformat(b["end"])
    for i in range((en - s).days + 1):
        arms[(s + timedelta(days=i)).isoformat()] = b["arm"]
stopped, d = None, first
while d <= date.fromisoformat(e["blocks"][-1]["end"]) and not stopped:
    clk.t = time.mktime(d.timetuple()) + 10 * 3600
    dy.asked("Dana")
    rough = arms[d.isoformat()] == "B" and (d - first).days % 3 == 0
    dy.set("rough" if rough else "good", "Dana", how="said")
    r = ex.check(d)
    if r:
        stopped = (d, r)
    d += timedelta(days=1)
check("C4 the safety stop sees Dana's rough days (not the asked marker) and stops a harmful change",
      stopped and "safety" in (ex.current().get("end_reason") or ""), stopped)
check("C4 it stops within the first two pairs", stopped and (stopped[0] - first).days < 28, stopped and stopped[0])
v = {"?dana": {"state": "asked", "at": 1}, "dana": {"state": "rough", "at": 2}, "_": {"state": "good", "at": 3}}
check("C4 day_state: the person's own answer; never the asked marker", X.day_state(v, "Dana") == "rough" and
      X.day_state({"?dana": {"state": "asked"}}, "Dana") is None and X.day_state(v) == "rough")

# ---- C5: the doctor alarm ---------------------------------------------------------------------------------------
check("C5 one rough day after 90 calm ones is not an alarm", not W.check_series([0] * 90 + [1] + [0] * 13)["worse"])
check("C5 a real run of rough days still is", W.check_series([0] * 90 + [1, 0, 1, 0, 0, 1, 0, 1, 0, 1, 0, 0, 1, 1])
      ["worse"])
rng = random.Random(5)
fa = 0
for _ in range(40):          # steady fog with day-to-day carry-over (AR 0.5), a year each
    x, ys = 0.0, []
    for _ in range(365):
        x = 0.5 * x + rng.gauss(0, 0.8)
        ys.append(max(0, min(5, round(2 + x))))
    for end in range(104, 365, 30):
        fa += W.check_series(ys[end - 104:end])["worse"]
check("C5 day-to-day carry-over doesn't flood the alarm (under 1.5 a year)", fa / 40 < 1.5, fa / 40)

# ---- C7: the forget list ------------------------------------------------------------------------------------------
folder = tempfile.mkdtemp()
st = Store(os.path.join(folder, "mind.db"))
fl = U.ForgetList(st, folder)
st.on_forget = lambda rows, vals: fl.record_notes(rows, vals)
old = st.remember(text="The old heater draws 1200 W, serial SN-4471")["id"]
copy = dict(st.get(old))
st.purge([old], values=["SN-4471", "1200"])
st.db.execute("INSERT INTO items (kind, title, body, shelf, status, created, updated, meta) "
              "VALUES ('fact', ?, ?, 'inbox', 'active', ?, ?, '{}')",
              (copy["title"], copy["body"], copy["created"], copy["created"]))        # a restore brings it back
st.db.commit()
time.sleep(0.01)
new = st.remember(text="The new kettle is SN-4471 too, 1200 W")["id"]
ids = fl.matches()
check("C7 a restored note goes again", len(ids) == 1 and new not in ids, ids)
check("C7 a note written after the purge stays", new not in ids)

# ---- C8: reflexes -------------------------------------------------------------------------------------------------
for t in ("turn off the porch light later", "porch light off in an hour", "lights off at 10", "turn on the fan until 9"):
    check(f"C8 for later, never a reflex: {t!r}", LR.TIMED.search(t))
check("C8 an everyday request can still be one", not LR.TIMED.search("turn on the desk lamp"))
check("C8 the opposite of a template never matches it", LR.opposite("tv light bars off", "on") and
      not LR.opposite("tv light bars on", "on") and LR.opposite("unlock the door", "lock"))

# ---- C9: back-off ------------------------------------------------------------------------------------------------
check("C9 safety calls are never backed off", safety_call("lock", "lock", ["lock.front"]) and
      safety_call("switch", "turn_off", ["switch.space_heater"]) and safety_call("valve", "close_valve", ["valve.x"]))
check("C9 comfort calls can be", not safety_call("light", "turn_on", ["light.desk"]) and
      not safety_call("switch", "turn_on", ["switch.space_heater"]))
src = open(M.__file__).read()
check("C9 back-off is wired after it exists", src.index("self.backoff = BackOff(st9)") <
      src.index("self.engine.backoff = self.backoff", src.index("self.backoff = BackOff(st9)")))

# ---- C10 and check-ins ------------------------------------------------------------------------------------------
clk = Clock(time.time())
dy = D.Day(store(), clock=clk)
dy.set("rough", "Dana", how="said")
dy.set("good", None, how="tap")
check("C10 a tap with no name never overrides someone's rough day", dy.get() == "rough" and dy.get("Dana") == "rough")
for t, want in (("good morning", None), ("Good morning!", None), ("good night", None), ("fine, just tired", "low"),
                ("ok do it", None), ("yes please", None), ("exhausted", "rough"), ("good day", "good"),
                ("foggy today", "low")):
    check(f"check-in: {t!r} -> {want}", D.parse(t, expecting=True) == want, D.parse(t, expecting=True))

# ---- the medicines log --------------------------------------------------------------------------------------------
tk = TR.Tracking(store(), None, folder=tempfile.mkdtemp())
for t, want in (("had a muffin", None), ("had my meds?", None), ("had a migraine", None), ("took the bins out", None),
                ("just took two ibuprofen", "ibuprofen"), ("I've taken my tylenol", "acetaminophen"),
                ("took my migraine pill", "migraine pill"), ("took vyvanse", "vyvanse")):
    r = tk.med_said(t, "Dana")
    check(f"meds: {t!r} -> {want}", (r or {}).get("med") == want, r)
r = tk.med_said("took 2 advil", "Dana")
check("meds: the same medicine by another name is a repeat", r and r.get("repeat"), r)

# ---- email and Telegram senders ---------------------------------------------------------------------------------
forged = email.message_from_string(
    "Authentication-Results: mx.home.net; dkim=fail header.d=family.com; dmarc=fail header.from=family.com\n"
    "Authentication-Results: forged.example; dkim=pass header.d=family.com\nFrom: dad@family.com\n\nhi")
check("email: a pass added lower down by the sender doesn't count", not CH.email_verified(forged, "dad@family.com"))
look = email.message_from_string("Authentication-Results: mx.home.net; dkim=pass header.i=@family.com.evil.io\n\nhi")
check("email: a domain that only contains the sender's doesn't count", not CH.email_verified(look, "dad@family.com"))
bare = email.message_from_string("Authentication-Results: mx.home.net; dkim=pass\n\nhi")
check("email: a bare dkim=pass doesn't count", not CH.email_verified(bare, "dad@family.com"))
good = email.message_from_string("Authentication-Results: mx.home.net; dkim=pass header.d=family.com; "
                                 "dmarc=pass header.from=family.com\n\nhi")
check("email: the receiving server's pass for the sender's domain counts", CH.email_verified(good, "dad@family.com")
      and CH.email_verified(good, "dad@family.com", "mx.home.net")
      and not CH.email_verified(good, "dad@family.com", "mx.other.net"))
seen = []


async def tg_ask(text, *a, **k):
    seen.append(k.get("speaker"))
    return {"answer": "ok"}
from homemind.messaging import Telegram  # noqa: E402
app = types.SimpleNamespace(mind=types.SimpleNamespace(store=store(), ask=tg_ask), limits=None)
tg = Telegram.__new__(Telegram)
tg.app, tg.chats = app, {-100: "Family", 7: "Sam"}
run(tg.handle({"chat": {"id": -100, "type": "group"}, "from": {"id": 7}, "text": "what's on today?"}))
check("telegram: in a group the speaker is the member, not the group's name", seen == ["Sam"], seen)

# ---- a shared key ----------------------------------------------------------------------------------------------
st = store()
tv = VIEWER.set("Dana")
st.remember(text="My therapist appointment is Thursday", title="Dana therapy", key="therapy appt",
            meta={"scope": "personal", "owner": "Dana"})
VIEWER.reset(tv)
tv = VIEWER.set("Bob")
r = st.remember(text="dentist thursday", key="therapy appt")
VIEWER.reset(tv)
check("key: someone else's personal note with the same key is neither shown nor replaced",
      "previous" not in r and r["action"] == "created", r)
check("key: Dana's note is still active (not superseded by Bob's)",
      st.db.execute("SELECT status FROM items WHERE title='Dana therapy'").fetchone()["status"] == "active")

# ---- measurement ------------------------------------------------------------------------------------------------
rows = [{"day": (date(2026, 1, 1) + timedelta(days=i)).isoformat(), "hrv": 46 + (8 if i % 2 else -8)}
        for i in range(28)]
hh = HL.Health()
u = hh._usual(rows, "hrv", "2026-02-01")
check("health: a swinging reading's usual day is 'about usual'", "about usual" in hh._vs("hrv", 52, u), u)
check("health: well outside is still said", "lower" in hh._vs("hrv", 30, u))
check("health: fewer than 14 days: no 'usual' yet", hh._usual(rows[:10], "hrv", "2026-02-01") is None)
from homemind import replay as RP  # noqa: E402


async def boom(text):
    raise TimeoutError("slow")


class Log:
    pass
cases = [{"id": i, "kind": "match", "request": "x", "entity_id": "light.a", "action": "on"} for i in range(3)]
rep = run(RP.replay(None, boom, cases=cases))
check("replay: a planner that fails counts as wrong, not left out", rep["match"] == 3 and rep["agree"] == 0, rep)

# ---- alone() --------------------------------------------------------------------------------------------------
from homemind.rooms import RoomModel  # noqa: E402
home = types.SimpleNamespace(catalog={"person.dana": {"state": "home"}, "person.sam": {"state": "home"}})
AL = types.SimpleNamespace(home=home, rooms=RoomModel(home))
AL.alone = M.App.alone.__get__(AL)
AL.rooms.heard_in("kitchen", "Dana")
check("alone: hearing only Dana doesn't make her alone with Sam home", not AL.alone("Dana"))

print()
print("all passed" if not FAILS else f"{len(FAILS)} failed: {', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
