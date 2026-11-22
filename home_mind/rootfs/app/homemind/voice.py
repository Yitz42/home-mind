"""Home Mind as a voice assistant (design spec step 19): one turn of a conversation, streamed.

The bridge's conversation agent (bridge/home_mind_bridge/conversation.py) sends what was said to /api/converse and
streams what comes back into Home Assistant's chat log, so text-to-speech can start on the first sentence. Home
Assistant's own commands ("turn on the kitchen light", timers, shopping list) are tried first by the voice pipeline
when its "Prefer handling commands locally" setting is on; only what they don't match reaches Home Mind.

One turn, as events (one JSON object per line):
  {"type": "delta", "text": ...}   something to say: an acknowledgement, then the answer one sentence at a time
  {"type": "done", "answer": ..., "continue": bool, ...}   the whole answer; continue = keep listening (a question
                                                          was asked: "Say yes to go ahead", the check-in)

Timing: if nothing is ready after ACK_SECONDS (1.5 s), a short acknowledgement is said first ("One moment."), so the
person knows they were heard. Past 6 s the answer goes to the person's phone (App.answer's handoff), and without a
phone the voice deadline and its best-note fallback stand.

What it does with the words: a house command ("turn off the den lamp", "dim the hall to 30") goes to the planner and
its checks (App.do); anything else (a question, something to remember, "undo", "why...") to App.answer. A command
nothing on the control list fits falls through to the brain, which may still know the answer.
"""
import asyncio
import json
import re
import time

ACK_SECONDS = 1.5
ACKS = ("One moment.", "Let me check.", "Just a second.")
COMMAND = re.compile(r"^\s*(?:(?:hey|ok|okay)\s+\w+[,.]?\s+)?(?:please\s+|can you\s+|could you\s+|would you\s+)?"
                     r"(?:turn|switch|set|dim|brighten|open|close|shut|lock|unlock|start|stop|run|play|pause|resume|"
                     r"raise|lower|increase|decrease|activate|deactivate|toggle)\b", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def sentences(text):
    """An answer cut at sentence ends, so speech can start on the first one."""
    return [p.strip() for p in _SENT.split((text or "").strip()) if p.strip()]


class SentenceStream:
    """Turns a stream of token deltas into complete, speakable sentences as soon as each finishes."""

    def __init__(self, emit, clean=None, hold_first_if=None):
        self.emit = emit
        self.clean = clean or (lambda s: s.strip())
        self.hold_first_if = hold_first_if
        self.buf = ""
        self.held = []
        self.emitted = []

    def _send(self, raw):
        s = self.clean(raw)
        if not s:
            return
        if not self.emitted and not self.held and self.hold_first_if and self.hold_first_if(s):
            self.held.append(s)
            return
        if self.held:
            for h in self.held:
                self.emitted.append(h)
                self.emit(h)
            self.held.clear()
        self.emitted.append(s)
        self.emit(s)

    def feed(self, chunk):
        if not chunk:
            return
        self.buf += chunk
        parts = _SENT.split(self.buf)
        if len(parts) <= 1:
            return
        for p in parts[:-1]:
            if p.strip():
                self._send(p)
        self.buf = parts[-1]

    def flush(self):
        for h in self.held:
            self.emitted.append(h)
            self.emit(h)
        self.held.clear()
        rem, self.buf = self.buf.strip(), ""
        if rem:
            s = self.clean(rem)
            if s:
                self.emitted.append(s)
                self.emit(s)


def said_for(r):
    """What to say back for a do() result."""
    if r.get("answer"):
        return r["answer"]
    if r.get("done"):
        d = r["done"]
        what = {"turn_on": "on", "turn_off": "off", "open_cover": "opening", "close_cover": "closing",
                "lock": "locked", "unlock": "unlocked", "set_percentage": f"at {d.get('data', {}).get('percentage')}%",
                "toggle": "switched"}.get((d.get("service") or ".").split(".", 1)[1], "done")
        if (d.get("service") or "") == "light.turn_on" and d.get("data", {}).get("brightness_pct"):
            what = f"at {d['data']['brightness_pct']}%"
        return f"OK, the {d.get('name') or d.get('entity_id')} is {what}."
    if r.get("error"):
        return "Sorry, I couldn't do that."
    return "OK."


async def turn(app, body, scope="admin", on_sentence=None):
    """One turn: the result dict (what /api/converse finally says)."""
    text = (body.get("text") or "").strip()
    if not text:
        return {"answer": "Sorry, I didn't catch that.", "empty": True}
    st = getattr(getattr(app, "mind", None), "store", None)
    if st is not None:
        from .channels import guest_on
        if guest_on(st):
            # guest mode: microphones off and nothing kept; only the safety phrases are still answered
            from . import redflags
            flag = redflags.check(text)
            if flag:
                hook = getattr(app, "on_red_flag", None)
                if hook:
                    try:
                        await hook(flag, text, None, body.get("conversation_id"), "voice")
                    except Exception:  # noqa: BLE001
                        pass
                return {"answer": flag.reply, "red_flag": flag.kind}
            return {"answer": "Guest mode is on, so I'm not listening right now.", "guest": True}
    cid = body.get("conversation_id")
    fe = body.get("front_end") or "assist"
    b = {"question": text, "conversation_id": cid, "voice": True, "front_end": fe,
         "user": body.get("user_id") or body.get("user"), "device_id": body.get("device_id"),
         "area": body.get("area")}
    modes = getattr(app, "modes", None)
    if modes is not None:
        # who writes the answer, by model mode (private: the local model or the notes alone, never the cloud)
        b["model"] = modes.answer_model(None)
        b["mode_model"] = True          # chosen by the mode, not by hand: connected answers still apply
        if modes.mode(None) == "private":
            b["fallback"] = False
    from .tokens import allows
    from .frontends import other_assistant
    oa = other_assistant(body, getattr(app, "opts", None) or {})
    if oa:
        from . import redflags
        if redflags.check(text):
            return await app.answer(b, scope)        # safety goes to the phone, never this speaker (App.answer)
        fe = f"other:{oa['name']}"
        b["front_end"] = body.get("front_end") or fe
    elif on_sentence is not None:
        b["on_sentence"] = on_sentence
    from .ask_actions import claims_command
    from .autodraft import automation_request
    if not automation_request(text) and not claims_command(text) and COMMAND.match(text) and allows(scope, "admin"):
        if oa:
            from .frontends import assistants_off
            if assistants_off(app.mind.store):
                return {"answer": f"{oa['name']} is turned off for Home Mind.", "other": oa["name"]}
        r = await app.do(text, source=fe, conversation_id=cid, deadline=app.mind.VOICE_SECONDS)
        nothing_fits = (r.get("error") == "nothing on the control list fits")
        if not nothing_fits:
            return dict(r, answer=said_for(r), via="do",
                        **({"continue": True} if r.get("confirm_needed") else {}))
    r = await app.answer(b, scope)
    cont = bool(r.get("confirm_needed") or r.get("checkin_asked"))
    return dict(r, via=r.get("via") or "ask", **({"continue": True} if cont else {}))


async def converse(app, body, scope="admin", ack_seconds=ACK_SECONDS):
    """The turn as events (an async generator), streaming sentences live as the model writes them, with a short
    acknowledgement on a voice satellite if nothing is ready within ack_seconds."""
    t0 = time.monotonic()
    q = asyncio.Queue()
    emitted = []

    def push(s):
        if s:
            emitted.append(s)
            q.put_nowait(s)

    work = asyncio.ensure_future(turn(app, body, scope, on_sentence=push))
    # only a voice device (a satellite) needs "One moment." to fill the silence; in the app's chat window the
    # acknowledgement just runs into the answer ("Let me check. I don't have...")
    # the phone app's Assist sends a device_id as well, so a satellite_id (newer Home Assistant) decides when sent
    spoken = bool(body.get("satellite_id")) if "satellite_id" in body else bool(body.get("device_id"))
    acked = None
    ack_at = t0 + ack_seconds
    while not work.done() or not q.empty():
        while not q.empty():
            yield {"type": "delta", "text": q.get_nowait()}
        if work.done():
            break
        get_task = asyncio.ensure_future(q.get())
        wait_for = None
        if spoken and acked is None and not emitted:
            wait_for = max(0.0, ack_at - time.monotonic())
        done, _ = await asyncio.wait({work, get_task}, timeout=wait_for, return_when=asyncio.FIRST_COMPLETED)
        if get_task in done:
            yield {"type": "delta", "text": get_task.result()}
        else:
            get_task.cancel()
            try:
                await get_task
            except asyncio.CancelledError:
                pass
            if not done and spoken and acked is None and not emitted:
                acked = ACKS[int(t0 * 10) % len(ACKS)]
                yield {"type": "delta", "text": acked, "ack": True}
    while not q.empty():
        yield {"type": "delta", "text": q.get_nowait()}
    try:
        r = await work
    except Exception as e:  # noqa: BLE001 - the person always hears something
        r = {"answer": "Sorry, something went wrong there.", "error": f"{type(e).__name__}: {e}"}
    ans = (r.get("answer") or "").strip()
    if emitted:
        # emit any trailing sentence appended after streaming (such as the daily check-in question)
        spoken_so_far = " ".join(emitted).strip()
        if ans.startswith(spoken_so_far):
            tail = ans[len(spoken_so_far):].strip()
            for s in sentences(tail):
                yield {"type": "delta", "text": s}
    else:
        for s in sentences(ans):
            yield {"type": "delta", "text": s}
    keep = {k: r[k] for k in ("via", "red_flag", "checkin", "checkin_asked", "approval_sent", "confirm_needed",
                              "handoff", "private", "undo", "why", "error") if k in r}
    yield {"type": "done", "answer": ans, "continue": bool(r.get("continue")),
           "acked": acked, "ms": round((time.monotonic() - t0) * 1000), **keep}


def ndjson(event):
    return (json.dumps(event, default=str) + "\n").encode()
