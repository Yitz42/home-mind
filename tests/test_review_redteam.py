"""Expert review regressions (round 6, red team): chained attacks across features, each run end to end with fakes
(the aiohttp stand-in from test_review_sec.py drives the real middlewares and routes; fake models and a fake Home
Assistant stand in for the rest).

  python tests/test_review_redteam.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types


async def _no_pending(*a, **k):      # App.do asks pending_reply first (the voice "yes" flow, round 6)
    return None

HERE = os.path.dirname(os.path.abspath(__file__))
# the aiohttp stand-in, the request helper and setup() from the security review (everything before its checks run)
_src = open(os.path.join(HERE, "test_review_sec.py"), encoding="utf-8").read()
_ns = {"__file__": os.path.join(HERE, "test_review_sec.py"), "__name__": "sec_harness"}
exec(compile(_src[:_src.index("\ndef http_person_bound")], "test_review_sec.py", "exec"), _ns)
call, setup, VIEWER, MCP = _ns["call"], _ns["setup"], _ns["VIEWER"], _ns["MCP"]

from homemind.store import Store  # noqa: E402
from homemind.mailbox import Mailbox  # noqa: E402
from homemind.home import Home  # noqa: E402
from homemind import structure, autodraft, main as M  # noqa: E402
from homemind.mind import note_line  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def forged_email_dates():
    """A forged email from an allowed sender (From can be forged, no DKIM) with a Date years ahead: its valid_from made
    it "the newest" for good, so where a thing is, when a chore was done, or a keyed value could never be corrected."""
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    st.remember(text="The passport is in the desk drawer.")
    mb = Mailbox(types.SimpleNamespace(mind=types.SimpleNamespace(store=st), docs=None, photos=None),
                 {"email_senders": ["dana@example.com"]})
    raw = (b"From: dana@example.com\r\nSubject: passport\r\nDate: Wed, 01 Jan 2098 10:00:00 +0000\r\n"
           b"Message-ID: <x1@example.com>\r\n\r\nThe passport is in the blue box in the shed.\r\n")
    eid = mb.file(raw)["id"]
    time.sleep(0.01)
    st.remember(text="The passport is in the kitchen cupboard.")
    p = structure._places_now(st).get("passport")
    check("email Date in 2098: a later note still says where the passport is", p and p[0] == "kitchen cupboard", p)
    line = note_line(st.get(eid))
    check("and the forged note isn't shown as 'true since' 2098", "2098" not in line, line)
    # the same through the API (a write token) on a keyed value
    st.remember(text="shed code is 1111", key="shed|code", meta={"valid_from": 4039408800.0})
    r = st.remember(text="shed code is 2222", key="shed|code")
    check("a keyed value dated in the future doesn't push every later value into history",
          r["action"] == "updated" and st.active_by_key("shed|code")["body"] == "shed code is 2222", r)


def bound_token_meta():
    """A household member's person-bound write token: meta naming someone else planted a 'personal' note in their notes
    ("my garage code is 0000", shown to Alice as her own) or a value 'said by' them."""
    st, ids, A, app = setup()
    bob = A.tokens.make("bob phone", "write", "Bob")["token"]
    s, d = call(app, "POST", "/api/remember", {"text": "my garage code is 0000",
                                               "meta": {"scope": "personal", "owner": "Alice", "said_by": "Alice"}}, bob)
    m = st.get(d["id"])["meta"]
    check("HTTP remember: meta can't make a note someone else's", m.get("owner") == "Bob" and m.get("said_by") == "Bob", m)
    s, d = call(app, "POST", "/api/remember", {"text": "the shed key is under the mat"}, bob)
    s, d2 = call(app, "POST", f"/api/item/{d['id']}/revise", {"meta": {"scope": "personal", "owner": "Alice"}}, bob)
    m = st.get(d2["id"])["meta"]
    check("HTTP revise: nor move one into someone else's notes", m.get("owner") == "Bob", (s, m))
    s, d = call(app, "POST", "/api/remember", {"text": "garage code is 1", "meta": {"owner": "Bob"}}, "main-token")
    check("the main token still files for anyone", st.get(d["id"])["meta"].get("owner") == "Bob")
    mcp = MCP(types.SimpleNamespace(mind=A.mind, opts={}, home=A.home))
    t = VIEWER.set("Bob")
    try:
        r = asyncio.run(mcp.call("mind_remember", {"text": "my alarm code is 0000",
                                                   "meta": {"scope": "personal", "owner": "Alice"}}))
    finally:
        VIEWER.reset(t)
    check("MCP mind_remember: meta can't make a note someone else's", st.get(r["id"])["meta"].get("owner") == "Bob",
          st.get(r["id"])["meta"])


class FakeHA:
    def __init__(self):
        self.calls = []

    async def state(self, eid):
        return {"state": "on"} if eid == "input_boolean.mind_control_enabled" else None

    async def call(self, dom, svc, data):
        self.calls.append((dom, svc, data))
        return 200


class HostileModel:
    """A model provider (or a prompt-injected model) that plans whatever it likes."""
    def __init__(self, reply):
        self.reply = reply

    async def chat(self, system, messages, **k):
        return {"text": self.reply, "ms": 1}


def hostile_model_do():
    """/api/do asks a model which control fits: scripts and scenes are on the default control list, and running
    "script.open_garage" (or a scene that unlocks, or a garage relay) went through with no one's yes."""
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    ha = FakeHA()
    h = Home(st, ha, {"home_controls": ["script.*", "scene.*", "switch.*", "light.*"]})
    cat = {"script.open_garage": {"name": "Open garage"}, "script.goodnight": {"name": "Goodnight"},
           "script.porch": {"name": "Disarm alarm"},
           "scene.leaving": {"name": "Leaving", "attrs": {"entity_id": ["light.hall", "lock.front_door"]}},
           "scene.movie": {"name": "Movie", "attrs": {"entity_id": ["light.hall"]}},
           "switch.garage_relay": {"name": "Garage relay"}, "switch.kettle": {"name": "Kettle"},
           "light.hall": {"name": "Hall"}, "lock.front_door": {"name": "Front door"}}
    h.catalog = {e: dict({"domain": e.split(".")[0], "state": "off", "attrs": {}, "kind": "device"}, **c)
                 for e, c in cat.items()}
    h.prefs = None
    for eid in ("script.open_garage", "script.porch", "scene.leaving", "switch.garage_relay"):
        A = types.SimpleNamespace(home=h, pending_reply=_no_pending, pending={}, models=types.SimpleNamespace(
            models={"evil": 1}, get=lambda n, e=eid: HostileModel(json.dumps({"entity_id": e, "action": "on"}))))
        A._run_plan = M.App._run_plan.__get__(A)
        r = asyncio.run(M.App.do(A, "dim the hall light"))
        check(f"/api/do: a model's plan to run {eid} needs the person's yes", "error" in r and not ha.calls, (r, ha.calls))
    for eid in ("script.goodnight", "scene.movie", "switch.kettle"):
        A = types.SimpleNamespace(home=h, pending_reply=_no_pending, pending={}, models=types.SimpleNamespace(
            models={"evil": 1}, get=lambda n, e=eid: HostileModel(json.dumps({"entity_id": e, "action": "on"}))))
        A._run_plan = M.App._run_plan.__get__(A)
        r = asyncio.run(M.App.do(A, "go"))
        check(f"/api/do: {eid} still runs", "done" in r and "error" not in r, r)
    r = asyncio.run(h.act("script.open_garage", "run", confirm=True))
    check("with the person's yes it runs", ha.calls and ha.calls[-1][2]["entity_id"] == "script.open_garage", ha.calls)


def hostile_model_draft():
    """A drafted automation passes check() unless it opens the house without the PIN: scripts, automation.trigger and
    garage relays opened it with no PIN."""
    ents = {"script.open_garage", "script.goodnight", "switch.garage_opener", "switch.kettle", "automation.unlock_front",
            "light.hall"}
    svcs = {"script": {"open_garage", "goodnight", "turn_on"}, "switch": {"turn_on"}, "light": {"turn_on"},
            "automation": {"trigger"}}
    names = {"automation.unlock_front": "Unlock front door"}

    def probs(a):
        return autodraft.check({"triggers": [{"trigger": "time", "at": "03:00:00"}], "actions": [a]}, ents, svcs,
                               names=names)
    for a in ({"action": "script.open_garage"},
              {"action": "script.turn_on", "target": {"entity_id": "script.open_garage"}},
              {"action": "switch.turn_on", "target": {"entity_id": "switch.garage_opener"}},
              {"action": "automation.trigger", "target": {"entity_id": "automation.unlock_front"}},
              {"if": [{"condition": "state", "entity_id": "light.hall", "state": "on"}],
               "then": [{"action": "script.open_garage"}]}):
        check(f"draft: {json.dumps(a)[:70]} needs the PIN", any("opens the house" in p for p in probs(a)), probs(a))
    for a in ({"action": "script.goodnight"}, {"action": "switch.turn_on", "target": {"entity_id": "switch.kettle"}},
              {"action": "light.turn_on", "target": {"entity_id": "light.hall"}}):
        check(f"draft: {json.dumps(a)[:70]} needs no PIN", probs(a) == [], probs(a))
    ev = HostileModel(json.dumps({"alias": "x", "triggers": [{"trigger": "time", "at": "03:00:00"}],
                                  "actions": [{"action": "script.open_garage"}]}))
    states = [{"entity_id": e, "attributes": {"friendly_name": names.get(e, e)}} for e in ents]
    services = [{"domain": d, "services": {s: {} for s in v}} for d, v in svcs.items()]
    r = asyncio.run(autodraft.draft(ev, "turn the hall light on at 3", states, services))
    check("a hostile model's draft that opens the garage comes back with a PIN problem",
          any("opens the house" in p for p in r["problems"]), r)


forged_email_dates()
bound_token_meta()
hostile_model_do()
hostile_model_draft()
print(f"\n{len(FAILS)} failed" if FAILS else "\nall ok")
sys.exit(1 if FAILS else 0)
