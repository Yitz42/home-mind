"""Claude with tools (0.41): the look-up loop that runs when an answer didn't know. No model or key needed: a fake
Claude scripts its tool calls.

  python tests/test_toolloop.py
"""
import asyncio
import logging
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
    m.ClientSession, m.ClientError = object, Exception
    sys.modules["aiohttp"] = m
from homemind.mind import Mind  # noqa: E402
from homemind.models import Model, sends_temperature  # noqa: E402
from homemind.speakers import VIEWER  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.toolloop import MAX_STEPS, ToolLoop  # noqa: E402

FAILS = []
logging.disable(logging.CRITICAL)
LOOP = asyncio.new_event_loop()


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(c):
    return LOOP.run_until_complete(c)


# ---- temperature and the request shape -------------------------------------------------------------------------------
check("temperature kept for Claude 3 and 4.0 to 4.6",
      all(sends_temperature(m) for m in ("claude-3-5-haiku-20241022", "claude-sonnet-4-20250514", "claude-sonnet-4-5",
                                         "claude-opus-4-6", "claude-haiku-4-5-20251001")))
check("temperature left out for Opus 4.7, 5.x and unknown names",
      not any(sends_temperature(m) for m in ("claude-opus-4-7", "claude-haiku-5-5", "claude-sonnet-5-5",
                                             "claude-opus-5-5", "claude-fable-5-2", "something-new")))


def captured(model_id):
    mo = Model({"name": "c", "type": "anthropic", "api_key": "k", "model": model_id}, None)
    sent = []

    async def post(url, payload, headers=None):
        sent.append(payload)
        return {"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn", "usage": {}}
    mo._post = post
    return mo, sent


mo, sent = captured("claude-haiku-5-5")
run(mo._chat_anthropic("s", [{"role": "user", "content": "q"}], 50, 0.2))
check("a 5.x model gets no temperature (it would answer HTTP 400)", "temperature" not in sent[0], sent[0])
mo, sent = captured("claude-3-5-haiku-20241022")
run(mo._chat_anthropic("s", [{"role": "user", "content": "q"}], 50, 0.2))
check("a Claude 3 model still gets its temperature", sent[0].get("temperature") == 0.2)
mo, sent = captured("claude-sonnet-5-5")
run(mo.messages_tools("s", [{"role": "user", "content": "q"}], [{"name": "t"}], tool_choice={"type": "none"}))
check("tools request: tools and tool_choice sent, no temperature",
      sent[0]["tools"] == [{"name": "t"}] and sent[0]["tool_choice"] == {"type": "none"} and "temperature" not in sent[0])
mo, sent = captured("claude-sonnet-5-5")
run(mo.messages_tools("s", [], []))
check("tool_choice left to the model (auto) unless asked", "tool_choice" not in sent[0])


# ---- the loop ----------------------------------------------------------------------------------------------------------
class FakeClaude:
    type, name = "anthropic", "claude"

    def __init__(self, script, key="k"):
        self.cfg = {"api_key": key}
        self.script, self.calls = list(script), []

    async def messages_tools(self, system, messages, tools, max_tokens=700, tool_choice=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": [t["name"] for t in tools],
                           "tool_choice": tool_choice})
        return self.script.pop(0) if self.script else {"content": [{"type": "text", "text": "Still nothing."}],
                                                        "stop_reason": "end_turn"}


def use(i, name, inp):
    return {"content": [{"type": "thinking", "thinking": "hmm", "signature": "sig"},
                        {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}], "stop_reason": "tool_use"}


def say(text):
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


class FakeMCP:
    def __init__(self):
        self.calls = []

    async def call(self, name, a, scope="admin"):
        self.calls.append((name, a, scope))
        if name == "mind_recall":
            return [{"id": 7, "title": "Shed", "body": "The spare key is under the blue pot.", "updated": 1}]
        if name == "mind_home":
            return {"now": "Porch light is on"}
        if name == "mind_control":
            return {"ok": True}
        if name == "mind_remember":
            return {"id": 99}
        return []


class Models:
    def __init__(self, **m):
        self.models = m

    def get(self, n):
        return self.models[n]


def app_with(claude, store=None):
    a = types.SimpleNamespace(models=Models(claude=claude), mcp=FakeMCP(), calendars=None)
    a.mind = types.SimpleNamespace(store=store or Store(os.path.join(tempfile.mkdtemp(), "m.db")))
    return a


c = FakeClaude([use(1, "recall", {"query": "spare key"}), say("Under the blue pot by the shed.")])
a = app_with(c)
tl = ToolLoop(a, "claude")
r = run(tl.answer("where is the spare key?", "QUESTION: where is the spare key?"))
check("loop: looks it up, then answers", r and r["text"] == "Under the blue pot by the shed." and r["tools"] == ["recall"],
      r)
check("loop: recall goes through the same gate as a plain link (scope write)",
      a.mcp.calls and a.mcp.calls[0][0] == "mind_recall" and a.mcp.calls[0][2] == "write")
second = c.calls[1]["messages"]
check("loop: the assistant turn goes back unchanged, thinking included",
      second[1]["role"] == "assistant" and second[1]["content"][0]["type"] == "thinking")
tr = second[2]["content"][0]
check("loop: the result comes back as a tool_result for that call",
      second[2]["role"] == "user" and tr["type"] == "tool_result" and tr["tool_use_id"] == "t1" and "blue pot" in tr["content"])
check("loop: only read tools without tool_writes", c.calls[0]["tools"] == ["recall", "home_state", "calendar", "tasks"])

c = FakeClaude([use(i, "home_state", {"q": "porch"}) for i in range(10)])
tl = ToolLoop(app_with(c), "claude")
r = run(tl.answer("q", "q"))
check(f"loop: at most {MAX_STEPS} rounds of tools, then it must answer",
      len(c.calls) == MAX_STEPS + 1 and c.calls[-1]["tool_choice"] == {"type": "none"}
      and all(x["tool_choice"] is None for x in c.calls[:-1]), [x["tool_choice"] for x in c.calls])

c = FakeClaude([use(1, "do", {"entity_id": "light.porch", "action": "off"}), say("Done.")])
a = app_with(c)
tl = ToolLoop(a, "claude")
r = run(tl.answer("turn off the porch light", "q"))
check("loop: a write tool it wasn't offered is refused",
      not any(n == "mind_control" for n, *_ in a.mcp.calls) and '"error"' in c.calls[1]["messages"][2]["content"][0]["content"]
      and c.calls[1]["messages"][2]["content"][0].get("is_error") is True)

c = FakeClaude([use(1, "do", {"entity_id": "lock.front", "action": "unlock", "confirm": True}), say("Asked.")])
a = app_with(c)
tl = ToolLoop(a, "claude", writes=True)
tok = VIEWER.set("Dana")
try:
    run(tl.answer("unlock the front door", "q"))
finally:
    VIEWER.reset(tok)
ctl = [x for x in a.mcp.calls if x[0] == "mind_control"]
check("loop: with tool_writes, do goes to the control list and never passes confirm",
      ctl and "confirm" not in ctl[0][1] and ctl[0][1]["entity_id"] == "lock.front")
check("loop: with tool_writes and a known speaker, remember and do are offered",
      c.calls[0]["tools"][-2:] == ["remember", "do"])
tok = VIEWER.set("")
try:
    check("loop: an unknown voice or guest gets read tools only, even with tool_writes",
          [t["name"] for t in tl.tools()] == ["recall", "home_state", "calendar", "tasks"])
finally:
    VIEWER.reset(tok)

check("off: no tool_model", not ToolLoop(app_with(FakeClaude([])), None).ready())
check("off: the model has no api_key", not ToolLoop(app_with(FakeClaude([], key="")), "claude").ready())
check("off: a name that isn't connected", not ToolLoop(app_with(FakeClaude([])), "nope").ready())
tl = ToolLoop(app_with(FakeClaude([say("a"), say("b")])), "claude", daily=1)
run(tl.answer("q", "q"))
check("off: after tool_daily_limit uses today", not tl.ready() and run(tl.answer("q", "q")) is None)


# ---- in an answer ------------------------------------------------------------------------------------------------------
class Dunno:
    type, cfg = "ollama", {"url": "http://192.168.1.5:11434"}

    def __init__(self, reply="I don't know."):
        self.reply = reply

    async def chat(self, system, messages, **k):
        return {"text": self.reply, "ms": 1}


def mind(claude, reply="I don't know."):
    m = Mind(tempfile.mkdtemp(), Models(f=Dunno(reply), claude=claude))
    m.FALLBACK = []
    m.store.remember(text="The bins go out on Tuesday.")
    a = app_with(claude, m.store)
    m.toolloop = ToolLoop(a, "claude")
    return m, a


c = FakeClaude([use(1, "recall", {"query": "spare key"}), say("Under the blue pot by the shed.")])
m, a = mind(c)
out = run(m.ask("Where is the spare key?", "f", fallback=False))
check("answer: when the model didn't know, Claude looks it up and its answer stands",
      out["answer"] == "Under the blue pot by the shed." and out["model"] == "claude" and out.get("tools") == ["recall"], out)
check("answer: what Claude is sent starts from the notes already found", "QUESTION" in str(c.calls[0]["messages"][-1]))

c = FakeClaude([say("should not run")])
m, a = mind(c, reply="The bins go out on Tuesday.")
out = run(m.ask("When do the bins go out?", "f", fallback=False))
check("answer: a known answer never costs a look-up", not c.calls and out["answer"] == "The bins go out on Tuesday.")

c = FakeClaude([say("should not run")])
m, a = mind(c)
out = run(m.ask("What was my blood pressure last week?", "f", fallback=False))
check("answer: never for a private question", not c.calls, out)

c = FakeClaude([say("should not run")])
m, a = mind(c)
out = run(m.ask("Where is the spare key?", "f", fallback=False, voice=True))
check("answer: never by voice", not c.calls)

c = FakeClaude([say("I don't know either.")])
m, a = mind(c)
out = run(m.ask("Where is the spare key?", "f", fallback=False))
check("answer: if Claude doesn't find it either, the first answer stands",
      c.calls and out["model"] == "f" and "tools" not in out, out)

print("\nall ok" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
