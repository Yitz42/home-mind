"""A GPU computer as a model server (0.28): models on a computer that sleeps or restarts (the Precision with Voicebox
and Ollama) are checked every 30 seconds with a TCP connect and skipped while it's away, so an answer goes straight
to a fallback instead of waiting out a timeout; Ollama gets num_gpu/keep_alive so a model can share the GPU with
Voicebox (or use the CPU); and a persona model on that computer rewords answers, checked like the rules.

Part A runs anywhere (a stand-in aiohttp when it isn't installed). Part B needs real aiohttp (CI) and talks HTTP to
a fake Ollama."""
import asyncio
import json
import os
import socket
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
    REAL = True
except ImportError:
    REAL = False
    fake = types.ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, total=None, sock_connect=None, **k):
            self.total, self.sock_connect = total, sock_connect

    class ClientSession:
        posted = []

        def post(self, *a, **k):
            ClientSession.posted.append((a, k))
            raise RuntimeError("no HTTP in the stand-in")

        async def close(self):
            pass

    fake.ClientTimeout, fake.ClientSession, fake.ClientError = ClientTimeout, ClientSession, Exception
    sys.modules["aiohttp"] = fake

from homemind.models import Models, Model  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def part_a():
    m = Model({"name": "gpu", "type": "ollama", "url": "http://192.168.1.50:11434", "model": "qwen3:4b"}, None)
    check("endpoint from the url", m.endpoint() == ("192.168.1.50", 11434))
    check("ollama without a url is this computer", Model({"name": "o", "type": "ollama"}, None).endpoint() ==
          ("localhost", 11434))
    check("Home Assistant models aren't checked", Model({"name": "h", "type": "ha_ai_task"}, None).endpoint() is None)
    check("https default port", Model({"name": "x", "type": "openai", "url": "https://api.example.com/v1"},
                                      None).endpoint() == ("api.example.com", 443))
    check("a sleeping computer fails in seconds, not minutes",
          m.timeout.sock_connect == 4 and m.timeout.total == 120, (m.timeout.sock_connect, m.timeout.total))

    srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    up_port, down_port = srv.sockets[0].getsockname()[1], free_port()
    ms = Models([{"name": "gpu", "type": "ollama", "url": f"http://127.0.0.1:{down_port}", "model": "qwen3:4b"},
                 {"name": "spare", "type": "openai", "url": f"http://127.0.0.1:{up_port}/v1", "model": "x"},
                 {"name": "haiku", "type": "ha_ai_task", "entity_id": "ai_task.x"}])
    await ms.start(watch=True)
    check("asleep: down", ms.up("gpu") is False)
    check("there: up", ms.up("spare") is True)
    check("not checked: None", ms.up("haiku") is None and ms.up("mock") is None)
    lst = {d["name"]: d for d in ms.list()}
    check("the Models page sees it", lst["gpu"]["up"] is False and lst["gpu"]["host"] == f"127.0.0.1:{down_port}"
          and lst["spare"]["up"] is True and "up" not in lst["haiku"], lst["gpu"])
    ms.mark("spare", False)
    check("a failed connect marks it at once", ms.up("spare") is False)
    await ms.probe_all()
    check("and the next check brings it back", ms.up("spare") is True)

    # the away alert: once after `hours`, once when back, never repeated for the same absence
    sent = {}
    key = f"127.0.0.1:{down_port}"
    since = ms.hosts[key]["since"]
    check("not yet: away for less than the hours", ms.away_changes(sent, 3, now=since + 3600) == [])
    ch = ms.away_changes(sent, 3, now=since + 4 * 3600)
    check("away for 3 hours: one alert", ch == [("away", key, since)], ch)
    check("not repeated", ms.away_changes(sent, 3, now=since + 9 * 3600) == [])
    check("it knows which models run there", ms.on_host(key) == ["gpu"])
    ms.hosts[key] = {"up": True, "since": since + 10 * 3600, "checked": 0}
    check("back: one more", ms.away_changes(sent, 3) == [("back", key, since + 10 * 3600)] and sent == {})
    check("and quiet after", ms.away_changes(sent, 3) == [])
    ms.hosts[key] = {"up": False, "since": since + 11 * 3600, "checked": 0}

    # an answer: the asleep computer is skipped without waiting, the fallback answers
    from homemind.mind import Mind
    mind = Mind(tempfile.mkdtemp(), ms)
    mind.FALLBACK = ["mock"]
    mind.store.remember(text="The drill is in the garage, top shelf.", title="Drill: garage top shelf")
    check("the chain puts the asleep model last", mind._chain("gpu") == ["mock", "gpu"], mind._chain("gpu"))
    t0 = time.time()
    r = await mind.ask("where is the drill?", model="gpu")
    took = time.time() - t0
    check("answered by the fallback, without waiting", r["model"] == "mock" and took < 2, (r.get("model"), took))
    await ms.stop()
    srv.close()

    # Jeeves's words from a model: used when it passes the check, refused when it changes a fact
    from homemind.persona import Persona
    from homemind.store import Store
    p = Persona(Store(tempfile.mktemp()))

    class M:
        def __init__(self, text, delay=0):
            self.text, self.delay, self.seen = text, delay, None

        async def chat(self, system, messages, **k):
            self.seen = (system, messages)
            await asyncio.sleep(self.delay)
            return {"text": self.text}
    plain = "The furnace filter was changed on March 3, 47 days ago."
    good = M("The furnace filter was last changed on March 3, 47 days ago. Rather overdue, if I may say so.")
    out = await p.style_model(good, plain, "disapproving")
    check("model styling used when it keeps the facts", out == good.text, out)
    check("the model is told the emotion and shown examples", good.seen[1][0]["content"].startswith("[disapproving]")
          and "Examples:" in good.seen[0])
    check("a changed number is refused", await p.style_model(M("Changed 45 days ago, I'm afraid."), plain,
                                                             "disapproving") is None)
    check("a dropped 'not' is refused", await p.style_model(M("The front door is locked."),
                                                            "The front door is not locked.", "alert") is None)
    check("too slow: the rules instead", await p.style_model(M(good.text, delay=1), plain, "disapproving",
                                                             timeout=0.2) is None)
    check("a safety question isn't sent", await p.style_model(good, "Unlock the front door? Say yes.", "alert") is None)
    res = p.finish({"answer": plain, "hits": [1]}, "disapproving", styled_by_model=good.text)
    check("finish keeps the plain answer and marks it", res["answer"] == good.text and res["plain"] == plain and
          res["styled_by"] == "model" and res["emotion"] == "disapproving", res)
    check("the voice bridge finds the model's words too", p.emotion_for(good.text) == ("disapproving", "answer"))


async def part_b():
    from aiohttp import web
    bodies = []

    async def chat(req):
        bodies.append(await req.json())
        return web.json_response({"message": {"content": "OK"}, "prompt_eval_count": 3, "eval_count": 1})
    app = web.Application()
    app.router.add_post("/api/chat", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    ms = Models([{"name": "style", "type": "ollama", "url": f"http://127.0.0.1:{port}", "model": "qwen3:1.7b",
                  "num_gpu": 0, "keep_alive": "30m", "num_ctx": 2048}])
    await ms.start(watch=False)
    r = await ms.get("style").chat("sys", [{"role": "user", "content": "hi"}], max_tokens=50)
    b = bodies[-1]
    check("Ollama gets num_gpu (CPU), num_ctx and keep_alive", b["options"].get("num_gpu") == 0 and
          b["options"].get("num_ctx") == 2048 and b.get("keep_alive") == "30m" and b["options"]["num_predict"] == 50, b)
    check("and answers", r["text"] == "OK")
    await ms.stop()
    await runner.cleanup()


asyncio.run(part_a())
if REAL:
    asyncio.run(part_b())
else:
    print("skip part B (HTTP to a fake Ollama): needs aiohttp, runs on CI")
print(f"\n{len(FAILS)} failed" if FAILS else "\nall GPU server checks passed")
sys.exit(1 if FAILS else 0)
