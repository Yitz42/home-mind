"""0.27: Home Mind as a voice assistant (the bridge's conversation agent and /api/converse), a person's own Claude
link through the bridge, and routine notices held for the morning check-in. No Home Assistant needed: the bridge's
agent runs against small stand-ins for Home Assistant's conversation classes.

  python tests/test_voice.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
APP = os.path.join(ROOT, "home_mind", "rootfs", "app")
sys.path.insert(0, APP)
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
from homemind import voice as V  # noqa: E402
from homemind.day import AskBatch, Day  # noqa: E402
from homemind.ha import HA  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []
LOOP = asyncio.new_event_loop()


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(c):
    return LOOP.run_until_complete(c)


# ---- one voice turn ------------------------------------------------------------------------------------------------
class App:
    def __init__(self, do_result=None, ask_result=None, delay=0.0):
        self.mind = types.SimpleNamespace(VOICE_SECONDS=8)
        self.do_result = do_result or {"plan": {"entity_id": "light.desk"}, "done": {
            "entity_id": "light.desk", "name": "desk lamp", "service": "light.turn_on", "data": {}}}
        self.ask_result = ask_result or {"answer": "The bins go out Tuesday. Recycling is every other week.",
                                         "hits": []}
        self.delay, self.calls = delay, []

    async def do(self, text, source=None, conversation_id=None, deadline=None):
        self.calls.append(("do", text, source))
        await asyncio.sleep(self.delay)
        return self.do_result

    async def answer(self, b, scope="admin"):
        self.calls.append(("ask", b["question"], b.get("voice"), b.get("user")))
        await asyncio.sleep(self.delay)
        return self.ask_result


a = App()
r = run(V.turn(a, {"text": "turn on the desk lamp", "user_id": "u1"}))
check("turn: a house command goes to the planner", a.calls[0][:2] == ("do", "turn on the desk lamp") and
      r["via"] == "do" and r["answer"] == "OK, the desk lamp is on.", r)
a = App(do_result={"error": "nothing on the control list fits", "plan": {"entity_id": None}})
r = run(V.turn(a, {"text": "turn on the moon"}))
check("turn: a command nothing fits falls through to the brain", [c[0] for c in a.calls] == ["do", "ask"] and
      r["via"] == "ask")
a = App()
r = run(V.turn(a, {"text": "when do the bins go out?", "user_id": "u1"}))
check("turn: a question goes to the brain, by voice, as the signed-in user",
      a.calls == [("ask", "when do the bins go out?", True, "u1")] and r["via"] == "ask")
a = App()
run(V.turn(a, {"text": "unlock the front door"}, scope="read"))
check("turn: a read-only caller never reaches the planner", a.calls[0][0] == "ask")
a = App(do_result={"plan": {"entity_id": "light.desk"}, "confirm_needed": True,
                   "answer": "Do you want me to turn on the Desk Lamp? Say yes to go ahead."})
r = run(V.turn(a, {"text": "turn on the desk lamp"}))
check("turn: a question back keeps the microphone open", r.get("continue") is True)
check("said: brightness", V.said_for({"done": {"name": "hall", "service": "light.turn_on",
                                               "data": {"brightness_pct": 30}}}) == "OK, the hall is at 30%.")
check("said: an error is short", V.said_for({"error": "x"}) == "Sorry, I couldn't do that.")
check("sentences: cut at sentence ends", V.sentences("It's 5512. The old one was 4417! Anything else?") ==
      ["It's 5512.", "The old one was 4417!", "Anything else?"] and V.sentences("v2.5 is out") == ["v2.5 is out"])


async def collect(gen):
    return [e async for e in gen]
ev = run(collect(V.converse(App(), {"text": "when do the bins go out?"}, ack_seconds=0.5)))
check("converse: fast answers aren't acknowledged; one event per sentence, then done",
      [e["type"] for e in ev] == ["delta", "delta", "done"] and not any(e.get("ack") for e in ev) and
      ev[-1]["answer"].startswith("The bins"), ev)
ev = run(collect(V.converse(App(delay=0.3), {"text": "when do the bins go out?", "device_id": "sat1"},
                            ack_seconds=0.1)))
check("converse: on a voice device, past the acknowledgement time, something is said first",
      ev[0].get("ack") and ev[0]["text"] in V.ACKS and ev[-1]["acked"] == ev[0]["text"], ev)
ev = run(collect(V.converse(App(delay=0.3), {"text": "when do the bins go out?"}, ack_seconds=0.1)))
check("converse: typed in the app (no voice device), no acknowledgement runs into the answer",
      not any(e.get("ack") for e in ev) and ev[0]["text"].startswith("The bins") and ev[-1]["acked"] is None, ev)

ev = run(collect(V.converse(App(delay=0.3), {"text": "when do the bins go out?", "device_id": "iphone",
                                             "satellite_id": None}, ack_seconds=0.1)))
check("converse: the phone app's Assist (a device, but no satellite) gets no acknowledgement in the text",
      not any(e.get("ack") for e in ev), ev)
ev = run(collect(V.converse(App(delay=0.3), {"text": "when do the bins go out?", "device_id": "sat1",
                                             "satellite_id": "assist_satellite.kitchen"}, ack_seconds=0.1)))
check("converse: a satellite does", ev[0].get("ack"), ev)


class Broken(App):
    async def answer(self, b, scope="admin"):
        raise RuntimeError("boom")
ev = run(collect(V.converse(Broken(), {"text": "hello"}, ack_seconds=1)))
check("converse: a failure is still something to hear", ev[-1]["answer"].startswith("Sorry") and "error" in ev[-1])
ev = run(collect(V.converse(App(), {"text": "  "})))
check("converse: nothing said", ev[-1]["answer"] == "Sorry, I didn't catch that.")
check("ndjson: one line per event", V.ndjson({"type": "done"}) == b'{"type": "done"}\n')

# ---- the bridge's conversation agent (against stand-ins for Home Assistant) ---------------------------------------


def stub(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class ChatLog:
    def __init__(self):
        self.conversation_id, self.content, self.deltas = "conv-1", [], []

    async def async_add_delta_content_stream(self, agent_id, stream):
        text = ""
        async for d in stream:
            self.deltas.append(d)
            if "content" in d:
                text += d["content"]
        self.content.append(text)
        yield text

    def async_add_assistant_content_without_tools(self, content):
        self.content.append(content.content)


class Result:
    def __init__(self, response=None, conversation_id=None, continue_conversation=False):
        self.response, self.conversation_id, self.continue_conversation = response, conversation_id, continue_conversation


class IntentResponse:
    def __init__(self, language=None):
        self.speech = None

    def async_set_speech(self, s):
        self.speech = s


class Entity:
    pass


stub("homeassistant")
stub("homeassistant.components")
conv_mod = stub("homeassistant.components.conversation", ChatLog=ChatLog, ConversationEntity=Entity,
                ConversationInput=object, ConversationResult=Result,
                ConversationEntityFeature=types.SimpleNamespace(CONTROL=1),
                AssistantContent=lambda agent_id, content: types.SimpleNamespace(content=content))
stub("homeassistant.config_entries", ConfigEntry=object)
stub("homeassistant.core", HomeAssistant=object)
stub("homeassistant.helpers")
stub("homeassistant.helpers.intent", IntentResponse=IntentResponse)
stub("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: None)
stub("homeassistant.helpers.device_registry", DeviceInfo=lambda **k: k)
pkg = types.ModuleType("home_mind_bridge")
pkg.__path__ = [os.path.join(APP, "bridge", "home_mind_bridge")]
sys.modules["home_mind_bridge"] = pkg
import importlib  # noqa: E402
BC = importlib.import_module("home_mind_bridge.conversation")

agent = BC.HomeMindAgent(None, types.SimpleNamespace(entry_id="e1"))
agent.entity_id = "conversation.home_mind"
lines = [json.dumps({"type": "delta", "text": "One moment.", "ack": True}), "",
         json.dumps({"type": "delta", "text": "Your code is 5512."}),
         json.dumps({"type": "delta", "text": "Anything else?"}),
         json.dumps({"type": "done", "answer": "Your code is 5512. Anything else?", "continue": True})]
sent = []


async def fake_lines(body):
    sent.append(body)
    for ln in lines:
        yield ln + "\n"
agent._lines = fake_lines
ui = types.SimpleNamespace(text="what's the gate code", conversation_id="c9", language="en", device_id="sat-1",
                           context=types.SimpleNamespace(user_id="u1"))
log_ = ChatLog()
res = run(agent._async_handle_message(ui, log_))
check("agent: sends the words, the signed-in user and the satellite", sent[0] == {
    "text": "what's the gate code", "conversation_id": "c9", "user_id": "u1", "device_id": "sat-1", "language": "en",
    "front_end": "assist"}, sent)
check("agent: one assistant message, streamed in pieces (acknowledgement first)",
      log_.deltas[0] == {"role": "assistant"} and log_.deltas[1] == {"content": "One moment."} and
      log_.content[-1] == "One moment. Your code is 5512. Anything else?", log_.deltas)
check("agent: keeps listening when Home Mind asked something", res.continue_conversation is True)
check("agent: supports streaming and any language", agent._attr_supports_streaming and agent.supported_languages == "*")


async def down(body):
    if body is not None:
        raise RuntimeError("connection refused")
    yield ""                                     # never reached: makes this an async generator
agent._lines = down
log_ = ChatLog()
res = run(agent._async_handle_message(ui, log_))
check("agent: Home Mind unreachable is said, not silence", "can't reach Home Mind" in log_.content[-1])

src = open(os.path.join(APP, "bridge", "home_mind_bridge", "__init__.py")).read()
check("bridge: a person's own link is answered with their token, the plain link with the main one",
      'own.startswith("hm_") else conn["token"]' in src and 'request.query.get("token")' in src)
check("bridge: the conversation platform is loaded", '"conversation"]' in src)
man = json.load(open(os.path.join(APP, "bridge", "home_mind_bridge", "manifest.json")))
check("bridge: a new version, so the app reinstalls it", man["version"] == "0.3.0" and
      "conversation" in man["dependencies"])

# ---- routine notices held for the morning -----------------------------------------------------------------------
st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
clk = [time.time()]
batch = AskBatch(st, clock=lambda: clk[0])
ha = HA(None)
calls = []


async def call(dom, svc, data, approved=False):
    calls.append(data.get("message"))
    return 200
ha.call, ha.batcher = call, batch
check("batch: a routine notice waits for the morning", run(ha.push("notify.p", {"title": "Humidity", "message": "low"},
                                                                  batch=True)) == "batched" and not calls)
check("batch: important ones don't", run(ha.push("notify.p", {"message": "x"}, "important", batch=True)) == "sent")
check("batch: only when asked to", run(ha.push("notify.p", {"message": "y"})) == "sent")
for i in range(5):
    batch.add(f"t{i}", f"m{i}")
got = batch.take(2)
check("batch: the morning takes the newest within the budget; the rest expire", [g["title"] for g in got] ==
      ["t4", "t3"] and batch.pending() == 0, got)
batch.add("old", "x")
clk[0] += 2 * 86400
check("batch: after a day it expires", batch.take(4) == [])

from homemind.morning import Morning  # noqa: E402


class MHA:
    def __init__(self):
        self.pushed = []

    async def notify(self, *a):
        pass

    async def push(self, target, data, *a, **k):
        self.pushed.append(data["message"])
        return "sent"

mha = MHA()
mo = Morning(st, mha, None, "07:30", "notify.p")
mo.build = lambda commit=False: "Good morning."
mo._checkin = None
mo.day = Day(st)
mo.day.set("good")
mo.batch = AskBatch(st)
mo.batch.add("House", "The freezer door was open for 10 minutes.")
run(mo.send())
check("morning: held notices come with it", "While you were away: House: The freezer door was open" in mha.pushed[-1],
      mha.pushed)
mo.batch.add("House", "Humidity was high.")
mo.day.set("rough")
run(mo.send())
check("morning: none on a rough day", "While you were away" not in mha.pushed[-1])

print()
print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
