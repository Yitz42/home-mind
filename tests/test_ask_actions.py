"""Direct actions from the Home Ask bar, Talk, and Voice (ask_actions.py).

  python tests/test_ask_actions.py
"""
import asyncio
import os
import sys
import tempfile
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

from homemind import ask_actions as AA  # noqa: E402
from homemind.fitness import Fitness  # noqa: E402
from homemind.modes import Modes  # noqa: E402
from homemind.pantry import Pantry  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.talk import Talk  # noqa: E402
from homemind.tracking import Tracking  # noqa: E402
from homemind import voice as V  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(c):
    return asyncio.run(c)


class FakeHA:
    available = True

    def __init__(self, with_timer=True):
        self.calls = []
        self.notifs = []
        self.with_timer = with_timer
        self.todo_items = {
            "todo.shopping_list": [{"summary": "Milk", "uid": "u-milk"}, {"summary": "Bread", "uid": "u-bread"}],
            "todo.reminders": [{"summary": "Call mom", "uid": "u-mom"}],
        }

    async def states(self):
        out = [
            {"entity_id": "todo.shopping_list", "attributes": {"friendly_name": "Shopping List"}},
            {"entity_id": "todo.reminders", "attributes": {"friendly_name": "Reminders"}},
            {"entity_id": "calendar.mind_journal", "attributes": {"friendly_name": "Mind Journal"}},
            {"entity_id": "calendar.personal", "attributes": {"friendly_name": "Personal"}},
            {"entity_id": "automation.wake_up_mon_sat", "state": "on", "attributes": {"friendly_name": "Wake up"}},
            {"entity_id": "automation.bedtime_lights", "state": "on", "attributes": {"friendly_name": "Bedtime lights"}},
        ]
        if self.with_timer:
            out.append({"entity_id": "timer.kitchen", "state": "idle", "attributes": {"friendly_name": "Kitchen timer"}})
        return out

    async def post_json(self, path, payload, timeout=10):
        if "todo/get_items" in path:
            ent = payload["entity_id"]
            return {"service_response": {ent: {"items": self.todo_items.get(ent, [])}}}
        return {}

    async def call(self, domain, service, data, approved=False):
        self.calls.append((domain, service, data))
        return []

    async def notify(self, title, message, nid="home_mind"):
        self.notifs.append((title, message))


class FakeEngine:
    def __init__(self):
        self.autos = {"automation.wake_up_mon_sat": {"name": "Wake up"}}
        self.owned = {"automation.wake_up_mon_sat"}
        self.ops = []

    async def retire(self, ids):
        self.ops.append(("retire", ids))
        return {"retired": ids}

    async def run_now(self, eid, trig=None):
        self.ops.append(("run_now", eid))
        return {"ran": eid}


async def main():
    tmp = tempfile.mkdtemp()
    st = Store(os.path.join(tmp, "mind.db"))
    ha = FakeHA(with_timer=True)
    pantry = Pantry(st, ha, None, tmp)
    fit = Fitness(st, pantry=pantry)
    tk = Tracking(st)
    modes = Modes({}, st)
    eng = FakeEngine()
    now_dt = datetime(2026, 11, 22, 10, 0)  # Sunday Nov 22, 2026

    class FakeOffsite:
        async def run(self):
            return {"file": "home-mind-20261122.hmbk", "bytes": 1234, "target": "folder"}

    class FakeMailbox:
        on = True

        def run(self):
            return {"read": 3, "filed": 2, "skipped": 1}

    app = types.SimpleNamespace(
        mind=types.SimpleNamespace(store=st, VOICE_SECONDS=5),
        ha=ha, pantry=pantry, fitness=fit, tracking=tk, modes=modes, engine=eng,
        offsite=FakeOffsite(), mailbox=FakeMailbox(), opts={},
    )

    def ask(q, who="Dana", scope="admin", other=None, use_brain=True):
        return AA.dispatch(app, {"question": q, "use_brain": use_brain}, who, other=other, scope=scope, now_dt=now_dt)

    # ---- 1. Remember / save a note -------------------------------------------------------------------------------
    r = await ask("Remember that the gate code is 4812")
    check("remember: saves household note", r and r.get("via") == "action" and r.get("remembered")
          and "4812" in st.recall("gate code", limit=3)[0]["body"], r)
    r = await ask("Remember privately that my safe combo is 12-34-56")
    saved_priv = st.get(r["remembered"]["id"]) if r and r.get("remembered") else {}
    check("remember privately: sets personal scope", r and (saved_priv.get("meta") or {}).get("scope") == "personal"
          and (saved_priv.get("meta") or {}).get("owner") == "Dana", (r, saved_priv))
    check("remember: polite 'Can you remember that...' works",
          bool(await ask("Can you remember that Dr. Lee moved to 5th St?")))
    check("remember: recall question is NOT intercepted",
          await ask("Do you remember the gate code?") is None
          and await ask("Can you remember the gate code?") is None)

    # ---- 2. Shopping list & reminders / to-dos -------------------------------------------------------------------
    ha.calls.clear()
    r = await ask("Can you add milk, eggs and avocados to my shopping list?")
    added_items = [c[2]["item"] for c in ha.calls if c[:2] == ("todo", "add_item") and c[2]["entity_id"] == "todo.shopping_list"]
    check("shopping add: splits items and calls HA todo.add_item",
          r and r.get("via") == "action" and added_items == ["milk", "eggs", "avocados"], (r, ha.calls))
    ha.calls.clear()
    r = await ask("Check off milk from the shopping list")
    check("shopping done: marks matching UID completed",
          r and ("todo", "update_item", {"entity_id": "todo.shopping_list", "item": "u-milk", "status": "completed"}) in ha.calls,
          (r, ha.calls))
    ha.calls.clear()
    r = await ask("Remind me to call the dentist tomorrow at 2pm")
    check("reminder add: adds to non-shopping HA todo list with due_datetime",
          r and any(c[:2] == ("todo", "add_item") and c[2]["entity_id"] == "todo.reminders"
                    and c[2]["item"] == "call the dentist" and c[2].get("due_datetime") == "2026-11-23 14:00:00"
                    for c in ha.calls), (r, ha.calls))
    ha.calls.clear()
    r = await ask("Remember to buy birthday candles")
    check("'Remember to...' routes to reminder, not plain note",
          r and any(c[:2] == ("todo", "add_item") and c[2]["entity_id"] == "todo.reminders" for c in ha.calls), (r, ha.calls))

    # ---- 3. Pantry & meals eaten ---------------------------------------------------------------------------------
    r = await ask("I ate 2 eggs and toast")
    check("pantry ate: logs meal eaten", r and r.get("via") == "action" and bool(r.get("meal_logged")), r)
    r = await ask("I had oatmeal for breakfast")
    check("pantry ate: 'I had X for breakfast' logs meal", r and bool(r.get("meal_logged")), r)
    check("pantry ate: does NOT misclassify 'I had a headache' or 'What did I eat?'",
          await ask("I had a headache") is None and await ask("What did I eat?") is None)
    r = await ask("I bought chicken and spinach for the fridge")
    check("pantry add: stocks items into fridge",
          r and len(r.get("pantry_added") or []) == 2 and any("chicken" in x["name"].lower() for x in pantry.items()), r)
    r = await ask("We finished the spinach")
    check("pantry used: marks item used", r and bool(r.get("pantry_used")), r)
    r = await ask("I tossed the chicken")
    check("pantry tossed: marks item tossed", r and "tossed" in r["answer"].lower(), r)

    # ---- 4. Existing automations ---------------------------------------------------------------------------------
    eng.ops.clear()
    ha.calls.clear()
    r = await ask("Disable my Wake up automation")
    check("automation off (owned): retires via engine",
          r and ("retire", ["automation.wake_up_mon_sat"]) in eng.ops, (r, eng.ops))
    r = await ask("Run the wake up automation")
    check("automation trigger (owned): runs via engine",
          r and ("run_now", "automation.wake_up_mon_sat") in eng.ops, (r, eng.ops))
    r = await ask("Turn off the bedtime lights automation")
    check("automation off (HA-only): calls automation.turn_off",
          r and ("automation", "turn_off", {"entity_id": "automation.bedtime_lights"}) in ha.calls, (r, ha.calls))
    check("automation ctrl: questions about automations are NOT intercepted",
          await ask("What automations do I have?") is None)
    check("automation ctrl: refuses write-only scope (requires admin)",
          await ask("Disable my Wake up automation", scope="write") is None)

    # ---- 5. Calendar events & timers -----------------------------------------------------------------------------
    ha.calls.clear()
    r = await ask("Add dentist Tuesday at 2pm to my calendar")
    cal_call = next((c for c in ha.calls if c[:2] == ("calendar", "create_event")), None)
    check("calendar add: picks personal calendar (skips journal) and creates timed event",
          r and cal_call and cal_call[2]["entity_id"] == "calendar.personal"
          and cal_call[2]["summary"] == "dentist"
          and cal_call[2]["start_date_time"] == "2026-11-24 14:00:00"
          and cal_call[2]["end_date_time"] == "2026-11-24 15:00:00", (r, cal_call))
    ha.calls.clear()
    r = await ask("Schedule team sync tomorrow at 10:30am for 30 minutes")
    cal_call = next((c for c in ha.calls if c[:2] == ("calendar", "create_event")), None)
    check("calendar schedule: respects custom duration",
          r and cal_call and cal_call[2]["start_date_time"] == "2026-11-23 10:30:00"
          and cal_call[2]["end_date_time"] == "2026-11-23 11:00:00", (r, cal_call))
    ha.calls.clear()
    r = await ask("Set a 10 minute timer")
    check("timer start (HA timer entity): calls timer.start with 00:10:00",
          r and ("timer", "start", {"entity_id": "timer.kitchen", "duration": "00:10:00"}) in ha.calls, (r, ha.calls))
    ha.calls.clear()
    r = await ask("Cancel the timer")
    check("timer cancel (HA): calls timer.cancel",
          r and ("timer", "cancel", {"entity_id": "timer.kitchen"}) in ha.calls, (r, ha.calls))
    # Local fallback timer when HA has no timer.* entity
    ha.with_timer = False
    r = await ask("Set a 5 minute pasta timer")
    check("timer start (local fallback): starts in-process timer and can cancel",
          r and (r.get("timer") or {}).get("local") is True and "pasta" in r["answer"].lower(), r)
    r2 = await ask("Cancel the pasta timer")
    check("timer cancel (local fallback): cancels active local timer",
          r2 and (r2.get("timer") or {}).get("op") == "cancel", r2)
    ha.with_timer = True

    # ---- 6. Weigh-ins, fitness goals & nudges --------------------------------------------------------------------
    r = await ask("I weigh 178.4 lbs")
    check("weigh-in: logs on body/weight shelf readable by Fitness.weigh_ins()",
          r and any(abs(w["weight"] - 178.4) < 0.05 for w in fit.weigh_ins()), (r, fit.weigh_ins()))
    r = await ask("Set my protein goal to 160g")
    check("fitness goal: updates protein_g to 160",
          r and fit.goals().get("protein_g") == 160, (r, fit.goals()))
    r = await ask("Set my bedtime goal to 10:30 pm")
    check("fitness goal: converts bedtime to 22:30",
          r and fit.goals().get("bedtime") == "22:30", (r, fit.goals()))
    r = await ask("Nudge me to stretch at 3pm")
    check("nudge (time): adds time nudge at 15:00",
          r and any(n["trigger"] == {"type": "time", "at": "15:00"} and n["message"] == "stretch"
                    for n in tk.nudges("Dana")), (r, tk.nudges("Dana")))
    r = await ask("Nudge me to take vitamins when I get home")
    check("nudge (arrive): adds arrive home nudge",
          r and any(n["trigger"] == {"type": "arrive", "place": "home"} for n in tk.nudges("Dana")), r)

    # ---- 7. System & privacy actions (admin) ---------------------------------------------------------------------
    r = await ask("Switch to private mode")
    check("system mode: switches mode to private", r and modes.mode("Dana") == "private", r)
    r = await ask("Run a backup now")
    check("system backup: runs offsite.run()", r and "home-mind-20261122.hmbk" in r["answer"], r)
    r = await ask("Check my email")
    check("system email: runs mailbox.run()", r and "2 filed" in r["answer"], r)
    check("system actions: refused for write-only scope, guests, and other assistants",
          await ask("Switch to cloud mode", scope="write") is None
          and await ask("Remember that secret is 1", who="") is None
          and await ask("Remember that secret is 1", other={"name": "alexa"}) is None
          and await ask("Remember that secret is 1", scope="read") is None)

    # ---- Talk & Voice COMMAND guard integration ------------------------------------------------------------------
    done_calls = []

    async def fake_do(text, **kw):
        done_calls.append(text)
        return {"done": {"service": "light.turn_off", "entity_id": "light.desk", "name": "desk lamp"}}

    async def fake_answer(b, scope="admin"):
        r = await AA.dispatch(app, b, b.get("speaker") or "Dana", scope=scope, now_dt=now_dt)
        return r or {"answer": "plain", "hits": []}

    tv_app = types.SimpleNamespace(mind=types.SimpleNamespace(store=st, VOICE_SECONDS=5),
                                   answer=fake_answer, do=fake_do, opts={})
    talk = Talk(tv_app)
    for cmd in ("Set a 10 minute timer", "Set my protein goal to 165g", "Switch to balanced mode",
                "Run a backup now", "Turn off the bedtime lights automation"):
        rt = await talk.turn(cmd, "Dana")
        check(f"Talk.turn bypasses App.do for '{cmd}'", rt.get("via") == "action" and not done_calls, (rt, done_calls))
    rv = await V.turn(tv_app, {"text": "Can you set a 10 minute timer?"})
    check("voice.turn bypasses App.do for polite timer request", rv.get("via") == "action" and not done_calls, (rv, done_calls))
    # Regular device command STILL goes to App.do!
    rt_dev = await talk.turn("turn off the desk lamp", "Dana")
    check("Talk.turn still routes house device commands to App.do",
          rt_dev.get("via") == "do" and done_calls == ["turn off the desk lamp"], (rt_dev, done_calls))

    print()
    print("all passed" if not FAILS else f"{len(FAILS)} failed: {', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


run(main())
