"""Home Mind Voice: Voicebox as a Home Assistant voice, with Jeeves's emotions, and Piper when Voicebox can't answer.

Home Assistant talks Wyoming (the protocol Piper uses) to this bridge. For each sentence to speak:
  1. the emotion: asked of Home Mind (which marked the answer when it wrote it), else read from the words themselves
     ("I'm afraid" is apologetic, "My congratulations" is proud, "Say yes" is alert...)
  2. Voicebox, on another computer, speaks it through its OpenAI-compatible endpoint (POST /v1/audio/speech) with
     the emotion as a delivery instruction (honoured by Qwen VoiceDesign and CustomVoice voices)
  3. if Voicebox is off, asleep, slow or wrong: the same text goes to Piper, so the house never goes silent

The wording is Home Mind's (with its fact check); Voicebox only supplies the voice. Its own text-rewriting
"personality" is never used on this endpoint. Standard library only: nothing to install.
"""
import asyncio
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("home_mind_voice")
VERSION = "0.1.0"

DELIVERY = {        # Home Mind's emotion markers as Voicebox delivery instructions (a butler's range: narrow on purpose)
    "neutral": "Speak calmly and evenly, at a measured pace.",
    "pleased": "Speak with quiet, understated satisfaction.",
    "proud": "Speak warmly, with restrained pride, as if offering congratulations.",
    "amused": "Speak with dry, deadpan understatement and the faintest hint of amusement.",
    "disapproving": "Speak with polite, restrained disapproval, a little slower.",
    "concerned": "Speak gently, with quiet concern, a little slower.",
    "sympathetic": "Speak softly and kindly, slowly and gently.",
    "apologetic": "Speak in a polite, apologetic tone.",
    "corrected": "Speak graciously and briskly, acknowledging a small mistake.",
    "alert": "Speak firmly and clearly, formal and brisk, with no humour.",
    "relieved": "Speak with calm, understated relief.",
    "warm": "Speak warmly and softly, relaxed, as in the evening.",
}

# the words that give an emotion away, for sentences Home Mind didn't mark (the voice agent's own words)
CUES = [
    ("alert", r"\bSay yes\b|\bI regret to say\b|\b(?:is|are) (?:not locked|unlocked|left open)\b|\balarm\b.*\b(?:trigger|going off)"),
    ("corrected", r"\b(?:quite right, my mistake|my mistake, quite right|my mistake)\b"),
    ("proud", r"\bmy congratulations\b|\bwell done\b|\ba new best\b"),
    ("apologetic", r"\bI'?m afraid\b|\bmy apologies\b|\bI'?m sorry\b"),
    ("sympathetic", r"\btake it gently\b|\btake it easy\b"),
    ("concerned", r"\bearly night\b|\blook after yourself\b"),
    ("disapproving", r"\bif I may say so\b|\bone hesitates\b|\bsomewhat overdue\b"),
    ("amused", r"\bI might add\b"),
    ("pleased", r"\bmost satisfactory\b|\bvery satisfactory\b|\bmost gratifying\b"),
    ("relieved", r"\bmost reassuring\b"),
    ("warm", r"\bgood night\b|\benjoy the evening\b"),
]
CUE_RX = [(e, re.compile(rx, re.I)) for e, rx in CUES]


def emotion_of(text):
    for emo, rx in CUE_RX:
        if rx.search(text or ""):
            return emo
    return "neutral"


def speakable(text):
    """Symbols a voice stumbles on, said as words. The wording itself is left alone."""
    t = text or ""
    t = re.sub(r"(\d)\s*[×x]\s*(\d)", r"\1 by \2", t)
    t = re.sub(r"(\d)\s*(?:–|-)\s*(\d)", r"\1 to \2", t)
    t = re.sub(r"(\d(?::\d{2})?\s*[AP]M)\s*[–-]\s*(\d)", r"\1 to \2", t)
    t = re.sub(r"\b(\d+(?:\.\d+)?)\s*lb\b", r"\1 pounds", t)
    t = re.sub(r"\b(\d+(?:\.\d+)?)\s*kg\b", r"\1 kilograms", t)
    t = re.sub(r"\b(\d+(?:\.\d+)?)\s*kcal\b", r"\1 calories", t)
    t = re.sub(r"\b(\d+(?:\.\d+)?)\s*g\b", r"\1 grams", t)
    t = re.sub(r"\b(\d+(?:\.\d+)?)\s*mi\b", r"\1 miles", t)
    t = re.sub(r"\b(\d+)\s*min\b", r"\1 minutes", t)
    t = re.sub(r"\s*—\s*", ", ", t)
    t = re.sub(r"\s*·\s*", ", ", t)
    t = re.sub(r"[*_`#]+", "", t)
    return re.sub(r"\s{2,}", " ", t).strip()


# ---- Wyoming (JSON header line, optional JSON data, optional payload) -----------------------------------------------
async def read_event(reader):
    line = await reader.readline()
    if not line:
        return None
    head = json.loads(line)
    data = head.get("data") or {}
    if head.get("data_length"):
        data.update(json.loads(await reader.readexactly(head["data_length"])))
    payload = await reader.readexactly(head["payload_length"]) if head.get("payload_length") else None
    return head["type"], data, payload


async def write_event(writer, etype, data=None, payload=None):
    head = {"type": etype, "version": "1.5.0"}
    body = json.dumps(data, ensure_ascii=False).encode() if data else None
    if body:
        head["data_length"] = len(body)
    if payload:
        head["payload_length"] = len(payload)
    writer.write(json.dumps(head).encode() + b"\n")
    if body:
        writer.write(body)
    if payload:
        writer.write(payload)
    await writer.drain()


class Bridge:
    CHUNK = 4096            # bytes of audio per chunk sent to Home Assistant
    RATE = 24000            # Voicebox's pcm: 16-bit mono at 24 kHz

    def __init__(self, opts):
        self.o = opts
        self.stats = {"voicebox": 0, "fallback": 0, "failed": 0, "last_error": None, "last_ms": None}
        self._down_until = 0.0

    # ---- emotion ---------------------------------------------------------------------------------------------------
    async def emotion(self, text):
        if not self.o.get("emotions", True):
            return None
        url = (self.o.get("homemind_url") or "").rstrip("/")
        if url:
            try:
                q = urllib.parse.urlencode({"text": text})
                req = urllib.request.Request(f"{url}/api/persona/emotion?{q}",
                                             headers={"Authorization": f"Bearer {self.o.get('homemind_token') or ''}"})
                r = await asyncio.wait_for(asyncio.to_thread(lambda: urllib.request.urlopen(req, timeout=2).read()), 2.5)
                e = json.loads(r).get("emotion")
                if e in DELIVERY:
                    return e
            except Exception as ex:  # noqa: BLE001 - the words still give it away
                log.debug("Home Mind emotion: %s", ex)
        return emotion_of(text)

    # ---- Voicebox --------------------------------------------------------------------------------------------------
    def _voicebox(self, text, instructions):
        body = {"model": self.o.get("model") or "tts-1", "input": text, "voice": self.o.get("voice") or None,
                "response_format": "pcm"}
        if instructions:
            body["instructions"] = instructions
        if float(self.o.get("speed") or 1.0) != 1.0:
            body["speed"] = float(self.o["speed"])
        req = urllib.request.Request(self.o["voicebox_url"].rstrip("/") + "/v1/audio/speech",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-Voicebox-Client-Id": "home-mind-voice"})
        try:
            with urllib.request.urlopen(req, timeout=float(self.o.get("timeout_seconds") or 10)) as r:
                audio = r.read()
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", {}).get("message")
            except Exception:  # noqa: BLE001
                msg = None
            raise RuntimeError(f"Voicebox said {e.code}: {msg or e.reason}") from None
        if len(audio) < 200 or len(audio) % 2:
            raise RuntimeError("Voicebox sent no usable audio")
        return audio

    async def speak_voicebox(self, writer, text, emotion):
        if not self.o.get("voicebox_url") or time.time() < self._down_until:
            return False
        instr = DELIVERY.get(emotion) if emotion else None
        t0 = time.time()
        try:
            audio = await asyncio.wait_for(asyncio.to_thread(self._voicebox, text, instr),
                                           float(self.o.get("timeout_seconds") or 10) + 1)
        except Exception as e:  # noqa: BLE001 - asleep, off, slow or misconfigured: Piper speaks instead
            self.stats["last_error"] = f"{type(e).__name__}: {e}"[:300]
            log.warning("Voicebox: %s; using the fallback voice", self.stats["last_error"])
            if isinstance(e, (urllib.error.URLError, ConnectionError, OSError, asyncio.TimeoutError, TimeoutError)):
                self._down_until = time.time() + float(self.o.get("retry_seconds") or 60)
            return False
        self.stats["last_ms"] = round((time.time() - t0) * 1000)
        fmt = {"rate": self.RATE, "width": 2, "channels": 1}
        await write_event(writer, "audio-start", dict(fmt, timestamp=0))
        for i in range(0, len(audio), self.CHUNK):
            await write_event(writer, "audio-chunk", dict(fmt, timestamp=i // 2 * 1000 // self.RATE),
                              audio[i:i + self.CHUNK])
        await write_event(writer, "audio-stop", {"timestamp": len(audio) // 2 * 1000 // self.RATE})
        self.stats["voicebox"] += 1
        return True

    # ---- Piper (or any Wyoming voice) ------------------------------------------------------------------------------
    async def speak_fallback(self, writer, text):
        host, port = self.o.get("fallback_host"), int(self.o.get("fallback_port") or 10200)
        if not host:
            return False
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
        except Exception as e:  # noqa: BLE001
            log.warning("fallback voice %s:%s: %s", host, port, e)
            return False
        try:
            data = {"text": text}
            if self.o.get("fallback_voice"):
                data["voice"] = {"name": self.o["fallback_voice"]}
            await write_event(w, "synthesize", data)
            while True:
                ev = await asyncio.wait_for(read_event(r), 30)
                if ev is None:
                    return False
                etype, d, payload = ev
                if etype in ("audio-start", "audio-chunk", "audio-stop"):
                    await write_event(writer, etype, d, payload)
                if etype == "audio-stop":
                    self.stats["fallback"] += 1
                    return True
                if etype == "error":
                    log.warning("fallback voice: %s", d.get("text"))
                    return False
        except Exception as e:  # noqa: BLE001
            log.warning("fallback voice: %s", e)
            return False
        finally:
            w.close()

    # ---- serving Home Assistant ------------------------------------------------------------------------------------
    def info(self):
        name = self.o.get("voice") or "Voicebox"
        att = {"name": "Voicebox + Home Mind", "url": "https://github.com/Yitz42/home-mind"}
        return {"asr": [], "handle": [], "intent": [], "wake": [], "tts": [{
            "name": "home_mind_voice", "description": "Voicebox with Home Mind's emotions, Piper when it's away",
            "attribution": att, "installed": True, "version": VERSION, "supports_synthesize_streaming": False,
            "voices": [{"name": name, "description": f"{name} (Voicebox)", "attribution": att, "installed": True,
                        "version": VERSION, "languages": [self.o.get("language") or "en_GB"], "speakers": None}]}]}

    async def say(self, writer, text):
        emo = await self.emotion(text) if (text or "").strip() else None     # asked about the words as written
        text = speakable(text)
        if not text:
            await write_event(writer, "audio-start", {"rate": self.RATE, "width": 2, "channels": 1, "timestamp": 0})
            await write_event(writer, "audio-stop", {"timestamp": 0})
            return "empty"
        if await self.speak_voicebox(writer, text, emo):
            return "voicebox"
        if await self.speak_fallback(writer, text):
            return "fallback"
        self.stats["failed"] += 1
        await write_event(writer, "error", {"text": "Neither Voicebox nor the fallback voice could speak.",
                                            "code": "tts-failed"})
        return "failed"

    async def handle(self, reader, writer):
        try:
            while True:
                ev = await read_event(reader)
                if ev is None:
                    break
                etype, data, _ = ev
                if etype == "describe":
                    await write_event(writer, "info", self.info())
                elif etype == "synthesize":
                    how = await self.say(writer, data.get("text") or "")
                    log.info("spoke via %s: %r", how, (data.get("text") or "")[:60])
        except (asyncio.IncompleteReadError, ConnectionResetError, json.JSONDecodeError) as e:
            log.debug("client: %s", e)
        finally:
            writer.close()

    async def warm(self):
        """Load the voice model on the Voicebox computer now, so the first real answer isn't the slow one."""
        if not self.o.get("voicebox_url"):
            return
        try:
            await asyncio.to_thread(self._voicebox, "Ready.", DELIVERY["neutral"])
            log.info("Voicebox is ready (%s)", self.o.get("voice"))
        except Exception as e:  # noqa: BLE001
            log.warning("Voicebox not reachable yet (%s); Piper will speak until it is", e)


def discover(port):
    """Tell Home Assistant this voice exists (Supervisor discovery, like Piper's add-on)."""
    tok = os.environ.get("SUPERVISOR_TOKEN")
    if not tok:
        return
    body = json.dumps({"service": "wyoming", "config": {"uri": f"tcp://{socket.gethostname()}:{port}"}}).encode()
    req = urllib.request.Request("http://supervisor/discovery", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        log.info("announced to Home Assistant as a Wyoming voice")
    except Exception as e:  # noqa: BLE001
        log.warning("discovery: %s (add the Wyoming integration by hand: host %s, port %s)", e,
                    socket.gethostname(), port)


async def main(opts):
    b = Bridge(opts)
    port = int(opts.get("port") or 10200)
    server = await asyncio.start_server(b.handle, "0.0.0.0", port)
    log.info("Home Mind Voice %s on port %s: Voicebox %s, voice %r, fallback %s", VERSION, port,
             opts.get("voicebox_url") or "(not set)", opts.get("voice"), opts.get("fallback_host") or "(none)")
    discover(port)
    asyncio.create_task(b.warm())
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    path = os.environ.get("OPTIONS", "/data/options.json")
    with open(path) as f:
        asyncio.run(main(json.load(f)))
