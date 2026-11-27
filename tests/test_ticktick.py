"""0.42.0: TickTick tasks in Home Mind: dates, the connect code, syncing tasks into notes, answers about what's due,
the morning lines, and adding / checking off by voice. No network and no model.

  python tests/test_ticktick.py
"""
import asyncio
import json
import logging
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
    m.ClientSession, m.ClientError = object, Exception
    m.BasicAuth = lambda *a: a
    m.WSMsgType = types.SimpleNamespace(TEXT=1)
    sys.modules["aiohttp"] = m
from homemind import ask_actions  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.ticktick import TickTick, code_from, parse_due  # noqa: E402
from homemind.tokens import needed  # noqa: E402

FAILS = []
logging.disable(logging.CRITICAL)


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


os.environ["TZ"] = "America/New_York"
time.tzset()

# ---- dates and the pasted code ------------------------------------------------------------------------------------
check("all-day due (05:00 UTC) is that local day", parse_due("2026-11-27T05:00:00.000+0000") == datetime(2026, 11, 27))
check("timed due converts to local time", parse_due("2026-11-27T19:30:00+0000") == datetime(2026, 11, 27, 14, 30))
check("no due is None", parse_due(None) is None and parse_due("") is None)
check("garbage due is None", parse_due("soon") is None)
check("code from the whole address", code_from("http://localhost/?code=Ab12cD&state=xyz") == ("Ab12cD", "xyz"))
check("code from a bare code", code_from("  Ab12cD ") == ("Ab12cD", None))
check("nothing usable in a sentence", code_from("it didn't work") == (None, None))
check("TickTick routes are admin only", needed("POST", "/api/ticktick/connect") == "admin"
      and needed("GET", "/api/ticktick") == "admin")

# ---- connect state ------------------------------------------------------------------------------------------------
tmp = tempfile.mkdtemp()
st = Store(os.path.join(tmp, "m.db"))
opts = {}
tt = TickTick(st, opts, lambda: None, os.path.join(tmp, "ticktick.json"))
check("not configured without the app's id and secret", not tt.configured and not tt.connected and tt.auth_url() is None)
opts.update(ticktick_client_id="cid", ticktick_client_secret="sec")
u = tt.auth_url()
check("connect address asks for read and write with the redirect", u and "client_id=cid" in u and "tasks%3Aread" in u
      and "redirect_uri=http%3A%2F%2Flocalhost%2F" in u, u)
tt._save({"access_token": "tok", "expires": time.time() + 100 * 86400})
check("token file is private", oct(os.stat(tt.path).st_mode & 0o777) == "0o600")
check("connected with a live token", tt.connected and tt.days_left() in (99, 100))

# ---- sync ---------------------------------------------------------------------------------------------------------
TODAY = datetime.now().date()


def utc_midnight(d):
    return datetime(d.year, d.month, d.day).astimezone().astimezone(
        __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


DATA = {
    "/project": [{"id": "p1", "name": "School"}, {"id": "p2", "name": "Old", "closed": True}],
    "/project/inbox/data": {"tasks": [
        {"id": "t1", "projectId": "inbox123", "title": "Call the dentist", "status": 0},
    ]},
    "/project/p1/data": {"tasks": [
        {"id": "t2", "projectId": "p1", "title": "ASL HW 9-1", "status": 0, "isAllDay": True, "priority": 5,
         "dueDate": utc_midnight(TODAY)},
        {"id": "t3", "projectId": "p1", "title": "ENTR 200 essay", "status": 0, "isAllDay": True,
         "dueDate": utc_midnight(TODAY - timedelta(days=2))},
        {"id": "t4", "projectId": "p1", "title": "Team report", "status": 0, "isAllDay": True,
         "dueDate": utc_midnight(TODAY + timedelta(days=3)), "items": [{"title": "intro", "status": 0},
                                                                     {"title": "outline", "status": 1}]},
        {"id": "t5", "projectId": "p1", "title": "Already done", "status": 2},
    ]},
}
CALLS = []


async def fake_req(method, path, body=None):
    CALLS.append((method, path, body))
    if method == "POST" and path == "/task":
        return {"id": "new1", **(body or {})}
    if method == "POST" and path.endswith("/complete"):
        return {}
    return json.loads(json.dumps(DATA[path]))


tt._req = fake_req
r = asyncio.run(tt.sync())
check("sync files every open task, skips done ones and closed lists", r["open"] == 4 and r["added"] == 4, r)
check("closed list isn't read", not any(p == "/project/p2/data" for _, p, _ in CALLS))
rows = {d["meta"]["tt"]: d for d in tt.open_tasks()}
check("task note has list, due date and priority", "Task (TickTick, School): ASL HW 9-1, due " in rows["t2"]["body"]
      and "high priority" in rows["t2"]["body"], rows["t2"]["body"])
check("unfinished steps are in the note", "Steps left: intro" in rows["t4"]["body"] and "outline" not in rows["t4"]["body"])
check("tasks sit on tasks/ticktick/<list>", rows["t2"]["shelf"] == "tasks/ticktick/school"
      and rows["t1"]["shelf"] == "tasks/ticktick/inbox")
r = asyncio.run(tt.sync())
check("a second sync changes nothing", r["added"] == 0 and r["changed"] == 0 and r["done"] == 0, r)
DATA["/project/p1/data"]["tasks"][0]["title"] = "ASL HW 9-1 and 9-2"
DATA["/project/inbox/data"]["tasks"] = []
r = asyncio.run(tt.sync())
rows = {d["meta"]["tt"]: d for d in tt.open_tasks()}
check("a renamed task is rewritten, title too", r["changed"] == 1 and rows["t2"]["title"] == "ASL HW 9-1 and 9-2", r)
check("a task gone from TickTick is archived", r["done"] == 1 and "t1" not in rows, r)


# ---- answers ------------------------------------------------------------------------------------------------------
check("questions not about tasks get nothing", tt.lines_for("what's the wifi password?") == "")
a = tt.lines_for("what tasks do I have?")
check("overdue comes first and is marked", a.index("ENTR 200 essay") < a.index("ASL HW") and "(overdue)" in a, a)
a = tt.lines_for("what's due today?")
check("due today lists today's and the overdue, not later ones", "ASL HW" in a and "ENTR 200" in a
      and "Team report" not in a, a)
a = tt.lines_for("what homework is due tomorrow?")
check("a day with nothing due says so", "no open tasks due then" in a, a)
b = tt.brief_lines()
check("morning: overdue and due today", any(x.startswith("Overdue tasks 1: ENTR 200 essay") for x in b)
      and any(x.startswith("Tasks due today: ASL HW") for x in b), b)
tt._save({"access_token": "tok", "expires": time.time() + 5 * 86400})
check("morning warns when the connection runs out soon", any("runs out in" in x for x in tt.brief_lines()))
tt._save({"access_token": "tok", "expires": time.time() + 100 * 86400})

# ---- voice: add and check off -------------------------------------------------------------------------------------


class FakeHA:
    available = True

    def __init__(self):
        self.calls = []

    async def states(self):
        return [{"entity_id": "todo.reminders", "attributes": {"friendly_name": "Reminders"}}]

    async def post_json(self, path, payload, timeout=10):
        return {"service_response": {payload["entity_id"]: {"items": []}}}

    async def call(self, domain, service, data, approved=False):
        self.calls.append((domain, service, data))
        return []


ha = FakeHA()
app = types.SimpleNamespace(ha=ha, ticktick=tt, opts=opts, mind=types.SimpleNamespace(store=st))
now_dt = datetime(2026, 11, 26, 22, 0)
CALLS.clear()
r = asyncio.run(ask_actions.dispatch(app, {"question": "remind me to buy stamps tomorrow"}, "Yitz", now_dt=now_dt))
adds = [c for c in CALLS if c[1] == "/task"]
check("remind me goes to TickTick", r and r.get("ticktick_added") == "buy stamps" and adds, r)
check("with tomorrow as an all-day due", adds and adds[0][2].get("isAllDay") is True
      and parse_due(adds[0][2]["dueDate"]) == datetime(2026, 11, 27), adds[0][2] if adds else None)
check("Home Assistant's list wasn't used", not any(c[1] == "add_item" for c in ha.calls))
CALLS.clear()
r = asyncio.run(ask_actions.dispatch(app, {"question": "add a task: email Dr. Seaman at 3pm"}, "Yitz", now_dt=now_dt))
adds = [c for c in CALLS if c[1] == "/task"]
check("a timed task is not all day", adds and adds[0][2].get("isAllDay") is False
      and parse_due(adds[0][2]["dueDate"]).hour == 15, adds[0][2] if adds else r)
opts["ticktick_reminders"] = False
CALLS.clear()
r = asyncio.run(ask_actions.dispatch(app, {"question": "remind me to water plants"}, "Yitz", now_dt=now_dt))
check("option off: reminders stay in Home Assistant", not [c for c in CALLS if c[1] == "/task"]
      and any(c[1] == "add_item" for c in ha.calls), r)
opts["ticktick_reminders"] = True
CALLS.clear()
r = asyncio.run(ask_actions.dispatch(app, {"question": "check off team report"}, "Yitz", now_dt=now_dt))
check("check off completes the TickTick task", r and r.get("ticktick_done") == "Team report"
      and any(p == "/project/p1/task/t4/complete" for _, p, _ in CALLS), r)
check("and it leaves the open tasks", "t4" not in {d["meta"]["tt"] for d in tt.open_tasks()})
r = asyncio.run(ask_actions.dispatch(app, {"question": "mark essay done in ticktick"}, "Yitz", now_dt=now_dt))
check("'… in TickTick' finds it by a word of the title", r and r.get("ticktick_done") == "ENTR 200 essay", r)
r = asyncio.run(ask_actions.dispatch(app, {"question": "mark nonsense done in ticktick"}, "Yitz", now_dt=now_dt))
check("an unknown task says so", r and "couldn't find" in r["answer"], r)
r = asyncio.run(ask_actions.dispatch(app, {"question": "what tasks are due?"}, "Yitz", now_dt=now_dt))
check("a question is never taken as an action", r is None, r)

# ---- disconnect ---------------------------------------------------------------------------------------------------
s = tt.disconnect()
check("disconnect forgets the token and the copied tasks", not s["connected"] and not tt.open_tasks()
      and not os.path.exists(tt.path))

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
