"""Home Mind Voice (the voice bridge app): Home Assistant asks over Wyoming, Voicebox speaks with the answer's emotion
as a delivery instruction, and Piper speaks when Voicebox is off, slow or wrong. Fake Voicebox (HTTP) and fake Piper
(Wyoming) servers; nothing real needed."""
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "home_mind_voice"))
sys.path.insert(0, os.path.join(ROOT, "home_mind", "rootfs", "app"))
import bridge as B  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class VB(BaseHTTPRequestHandler):
    """Fake Voicebox: POST /v1/audio/speech -> pcm; behaviour set on the class."""
    requests, mode, delay = [], "ok", 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        VB.requests.append(body)
        time.sleep(VB.delay)
        if VB.mode == "error":
            out = json.dumps({"error": {"message": "Voice 'Jeeves' is not a Voicebox voice profile."}}).encode()
            self.send_response(400)
        else:
            out = b"\x01\x00" * 12000          # half a second of 24 kHz audio
            self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):                       # fake Home Mind: /api/persona/emotion
        VB.requests.append({"get": self.path, "auth": self.headers.get("Authorization")})
        out = json.dumps({"emotion": "warm", "from": "answer"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def http_server():
    s = ThreadingHTTPServer(("127.0.0.1", 0), VB)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


async def fake_piper(reader, writer):
    ev = await B.read_event(reader)
    PIPER.append(ev[1])
    fmt = {"rate": 22050, "width": 2, "channels": 1}
    await B.write_event(writer, "audio-start", dict(fmt, timestamp=0))
    await B.write_event(writer, "audio-chunk", dict(fmt, timestamp=0), b"\x02\x00" * 500)
    await B.write_event(writer, "audio-stop", {"timestamp": 22})
    writer.close()

PIPER = []


async def ask(port, text=None, describe=False):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    if describe:
        await B.write_event(w, "describe")
        ev = await B.read_event(r)
        w.close()
        return [ev]
    await B.write_event(w, "synthesize", {"text": text, "voice": {"name": "Jeeves"}})
    out = []
    while True:
        ev = await asyncio.wait_for(B.read_event(r), 20)
        out.append(ev)
        if ev is None or ev[0] in ("audio-stop", "error"):
            break
    w.close()
    return out


def audio_of(evs):
    start = next((e[1] for e in evs if e and e[0] == "audio-start"), None)
    return start, sum(len(e[2] or b"") for e in evs if e and e[0] == "audio-chunk"), evs[-1][0] if evs[-1] else None


async def run():
    vb = http_server()
    vb_url = f"http://127.0.0.1:{vb.server_address[1]}"
    piper = await asyncio.start_server(fake_piper, "127.0.0.1", 0)
    pport = piper.sockets[0].getsockname()[1]
    opts = {"voicebox_url": vb_url, "voice": "Jeeves", "model": "tts-1", "language": "en_GB", "emotions": True,
            "timeout_seconds": 2, "retry_seconds": 60, "speed": 1.0, "fallback_host": "127.0.0.1",
            "fallback_port": pport, "fallback_voice": "en_GB-northern_english_male-medium"}
    b = B.Bridge(opts)
    srv = await asyncio.start_server(b.handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    ev = (await ask(port, describe=True))[0]
    tts = ev[1]["tts"][0]
    check("describe: a Wyoming voice named for the profile", ev[0] == "info" and tts["voices"][0]["name"] == "Jeeves"
          and tts["voices"][0]["languages"] == ["en_GB"], ev)

    VB.requests.clear()
    evs = await ask(port, "Squat, 3×5 at 195 lb: a new best. My congratulations, Yitz.")
    req = VB.requests[-1]
    check("Voicebox asked for pcm in the profile's voice", req["response_format"] == "pcm" and req["voice"] == "Jeeves"
          and req["model"] == "tts-1", req)
    check("the emotion goes as a delivery instruction", req.get("instructions") == B.DELIVERY["proud"], req)
    check("symbols said as words", req["input"] == "Squat, 3 by 5 at 195 pounds: a new best. My congratulations, Yitz.",
          req["input"])
    start, n, last = audio_of(evs)
    check("audio relayed at 24 kHz, all of it", start and start["rate"] == 24000 and n == 24000 and last == "audio-stop",
          (start, n, last))
    check("counted as Voicebox", b.stats["voicebox"] == 1)

    for text, emo in [("I regret to say the front door is not locked.", "alert"),
                      ("I'm afraid I have no record of the drill.", "apologetic"),
                      ("Do take it gently today.", "sympathetic"), ("The kitchen lights are off.", "neutral")]:
        await ask(port, text)
        check(f"emotion from the words: {emo}", VB.requests[-1].get("instructions") == B.DELIVERY[emo], VB.requests[-1])

    VB.mode = "error"
    PIPER.clear()
    evs = await ask(port, "The furnace filter was changed 47 days ago.")
    start, n, last = audio_of(evs)
    check("Voicebox error: Piper speaks instead", PIPER and start["rate"] == 22050 and n == 1000 and last == "audio-stop",
          (PIPER, start, n))
    check("the fallback gets its own voice", PIPER and PIPER[-1].get("voice") == {"name": opts["fallback_voice"]})
    check("the error is kept for the log", "not a Voicebox voice profile" in (b.stats["last_error"] or ""))
    VB.mode = "ok"

    VB.delay = 4
    t0 = time.time()
    evs = await ask(port, "Rain from 3 PM.")
    took = time.time() - t0
    check("slow Voicebox: Piper within the wait", audio_of(evs)[0]["rate"] == 22050 and took < 4.5, took)
    VB.delay = 0
    b._down_until = 0

    # unreachable: falls back, then skips Voicebox (no waiting) until retry_seconds pass
    b2 = B.Bridge(dict(opts, voicebox_url="http://127.0.0.1:9"))
    s2 = await asyncio.start_server(b2.handle, "127.0.0.1", 0)
    p2 = s2.sockets[0].getsockname()[1]
    evs = await ask(p2, "Hello.")
    check("unreachable Voicebox: Piper speaks", audio_of(evs)[0]["rate"] == 22050)
    check("and Voicebox is skipped for a while", b2._down_until > time.time() + 30)

    b3 = B.Bridge(dict(opts, voicebox_url="http://127.0.0.1:9", fallback_host=""))
    s3 = await asyncio.start_server(b3.handle, "127.0.0.1", 0)
    evs = await ask(s3.sockets[0].getsockname()[1], "Hello.")
    check("nothing can speak: a Wyoming error, not silence", evs[-1][0] == "error", evs[-1])

    b4 = B.Bridge(dict(opts, emotions=False))
    s4 = await asyncio.start_server(b4.handle, "127.0.0.1", 0)
    await ask(s4.sockets[0].getsockname()[1], "My congratulations.")
    check("emotions off: no instruction", "instructions" not in VB.requests[-1], VB.requests[-1])

    b5 = B.Bridge(dict(opts, homemind_url=vb_url, homemind_token="tok"))
    s5 = await asyncio.start_server(b5.handle, "127.0.0.1", 0)
    await ask(s5.sockets[0].getsockname()[1], "All quiet for the evening.")
    got = [r for r in VB.requests if "get" in r][-1]
    check("Home Mind asked for the marker, with its token", got["get"].startswith("/api/persona/emotion?text=All+quiet")
          and got["auth"] == "Bearer tok", got)
    check("and its emotion is used", VB.requests[-1].get("instructions") == B.DELIVERY["warm"], VB.requests[-1])

    evs = await ask(port, "   ")
    check("empty text: empty audio, no error", audio_of(evs)[2] == "audio-stop")
    vb.shutdown()


def test_static():
    from homemind.persona import TEXT_CUES, EMOTIONS
    check("the bridge's word cues are Home Mind's", B.CUES == TEXT_CUES)
    check("a delivery instruction for every emotion", set(B.DELIVERY) == set(EMOTIONS))
    import re
    cfg = open(os.path.join(ROOT, "home_mind_voice", "config.yaml")).read()
    opts = set(re.findall(r"^  (\w+):", cfg.split("schema:")[0].split("options:")[1], re.M))
    schema = set(re.findall(r"^  (\w+):", cfg.split("schema:")[1], re.M))
    tr = open(os.path.join(ROOT, "home_mind_voice", "translations", "en.yaml")).read()
    check("every option has a schema entry", opts == schema, opts ^ schema)
    check("every option has a translation", all(f"  {o}:\n    name:" in tr for o in opts))
    check("no install step: the standard library only", "pip" not in open(os.path.join(ROOT, "home_mind_voice",
                                                                                     "Dockerfile")).read())
    check("speakable keeps ISO dates and phone numbers intact",
          B.speakable("Due 2026-11-21; call 555-1234.") == "Due 2026-11-21; call 555-1234.",
          B.speakable("Due 2026-11-21; call 555-1234."))
    check("speakable expands sleep, HRV, bpm, and ranges",
          B.speakable("Slept 6 h 05 min, HRV 41 ms, 58 bpm, 12 AM–7 AM, 3–5.") ==
          "Slept 6 hours 5 minutes, HRV 41 milliseconds, 58 beats per minute, 12 AM to 7 AM, 3 to 5.",
          B.speakable("Slept 6 h 05 min, HRV 41 ms, 58 bpm, 12 AM–7 AM, 3–5."))


test_static()
try:
    asyncio.run(run())
except Exception as e:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    check(f"run raised {e!r}", False)
print(f"\n{len(FAILS)} failed" if FAILS else "\nall voice bridge checks passed")
sys.exit(1 if FAILS else 0)
