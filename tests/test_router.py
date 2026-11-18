"""The router (0.28): with no model named, simple lookups go to the local model first, harder questions to the cloud
model first, private ones local first; a computer that's away goes last; a local "I don't know" despite good notes
gets one more try. Runs anywhere (a stand-in aiohttp when it isn't installed)."""
import asyncio
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    fake = types.ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, total=None, sock_connect=None, **k):
            self.total, self.sock_connect = total, sock_connect

    class ClientSession:
        async def close(self):
            pass
    fake.ClientTimeout, fake.ClientSession, fake.ClientError = ClientTimeout, ClientSession, Exception
    sys.modules["aiohttp"] = fake

from homemind.models import Models  # noqa: E402
from homemind.router import classify, route  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def test_classify():
    for q, want in [("where's the drill?", ("simple", False)), ("what's the wifi password?", ("simple", True)),
                    ("why does the furnace short cycle in the morning?", ("complex", False)),
                    ("compare the two quotes for the roof and recommend one", ("complex", False)),
                    ("summarise what the plumber said about the leak, the water heater and the new valve",
                     ("complex", False)),
                    ("why am I so tired lately?", ("complex", True)), ("how much protein should I eat?", ("complex", True))]:
        lvl, priv, why = classify(q)
        check(f"classify {q!r}", (lvl, priv) == want, (lvl, priv, why))
    lvl, priv, _ = classify("what did the doctor say?", [{"shelf": "body/health", "score": 1}])
    check("a body note among the hits makes it private", priv)
    order, info = route("why does the furnace short cycle?", [], ["gpu"], ["haiku"])
    check("complex: cloud first", order == ["haiku", "gpu"] and info["level"] == "complex")
    order, _ = route("why am I so tired lately?", [], ["gpu"], ["haiku"])
    check("private, even complex: local first", order == ["gpu", "haiku"])
    order, _ = route("where's the drill?", [], ["gpu"], ["haiku"])
    check("simple: local first", order == ["gpu", "haiku"])


async def test_mind():
    srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    ms = Models([{"name": "gpu", "type": "ollama", "url": f"http://127.0.0.1:{port}", "model": "qwen3:4b"},
                 {"name": "haiku", "type": "ha_ai_task", "entity_id": "ai_task.x"}])
    await ms.start(watch=True)
    calls, replies = [], {}

    def fake(name):
        async def chat(system, messages, **k):
            calls.append(name)
            r = replies.get(name, f"{name} says: the drill is in the garage.")
            if isinstance(r, Exception):
                raise r
            return {"text": r, "ms": 5, "model": name}
        return chat
    for n in ("gpu", "haiku"):
        ms.get(n).chat = fake(n)
    from homemind.mind import Mind
    mind = Mind(tempfile.mkdtemp(), ms)
    mind.FALLBACK = ["gpu", "haiku"]
    mind.store.remember(text="The drill is in the garage, on the top shelf next to the sander.",
                        title="Drill: garage top shelf")
    check("the router splits the fallbacks by where they run", mind.route_lists() == (["gpu"], ["haiku"]),
          mind.route_lists())

    r = await mind.ask("where is the drill?")
    check("simple, no model named: the local model answers", r["model"] == "gpu" and r["route"]["level"] == "simple",
          r)
    calls.clear()
    r = await mind.ask("why do I keep losing the drill, and what should I change about the garage shelves?")
    check("complex: the cloud model answers first", calls[:1] == ["haiku"] and r["model"] == "haiku", (calls, r))
    r = await mind.ask("where is the drill?", model="haiku")
    check("a named model is never rerouted", r["model"] == "haiku" and "route" not in r)

    replies["gpu"] = "I don't know where the drill is."
    calls.clear()
    r = await mind.ask("where is the drill?")
    check("local 'I don't know' despite good notes: the next model tries", calls == ["gpu", "haiku"] and
          r["model"] == "haiku" and "escalated" in r["route"] and not r.get("weak"), (calls, r.get("route")))
    replies["haiku"] = RuntimeError("cloud down")
    r = await mind.ask("where is the drill?")
    check("and if that fails, the local answer stands", r["model"] == "gpu" and "don't know" in r["answer"], r)
    replies.clear()

    srv.close()
    await srv.wait_closed()
    await ms.probe_all()                 # nothing listening there now: the computer is away
    calls.clear()
    r = await mind.ask("where is the drill?")
    check("the local computer away: the cloud answers straight away", calls == ["haiku"] and r["model"] == "haiku",
          (calls, r.get("model")))

    mind.ROUTER = False
    r = await mind.ask("where is the drill?")
    check("router off: no model named means the best note, as before", r["model"] is None and r["answer"])
    mind.ROUTER = True
    mind.FALLBACK = []
    r = await mind.ask("where is the drill?")
    check("nothing to route to: the best note", r["model"] is None)
    await ms.stop()


test_classify()
try:
    asyncio.run(test_mind())
except Exception as e:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    check(f"test_mind raised {e!r}", False)
print(f"\n{len(FAILS)} failed" if FAILS else "\nall router checks passed")
sys.exit(1 if FAILS else 0)
