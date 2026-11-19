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


async def turn(app, body, scope="admin"):
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
        if modes.mode(None) == "private":
            b["fallback"] = False
    from .tokens import allows
    if COMMAND.match(text) and allows(scope, "admin"):
        r = await app.do(text, source=fe, conversation_id=cid, deadline=app.mind.VOICE_SECONDS)
        nothing_fits = (r.get("error") == "nothing on the control list fits")
        if not nothing_fits:
            return dict(r, answer=said_for(r), via="do",
                        **({"continue": True} if r.get("confirm_needed") else {}))
    r = await app.answer(b, scope)
    cont = bool(r.get("confirm_needed") or r.get("checkin_asked"))
    return dict(r, via="ask", **({"continue": True} if cont else {}))


async def converse(app, body, scope="admin", ack_seconds=ACK_SECONDS):
    """The turn as events (an async generator), with the acknowledgement if it takes more than ack_seconds."""
    t0 = time.monotonic()
    work = asyncio.ensure_future(turn(app, body, scope))
    # only a voice device (a satellite) needs "One moment." to fill the silence; in the app's chat window the
    # acknowledgement just runs into the answer ("Let me check. I don't have...")
    # the phone app's Assist sends a device_id as well, so a satellite_id (newer Home Assistant) decides when sent
    spoken = bool(body.get("satellite_id")) if "satellite_id" in body else bool(body.get("device_id"))
    done, _ = await asyncio.wait({work}, timeout=ack_seconds) if spoken else (set(), None)
    acked = None
    if spoken and not done:
        acked = ACKS[int(t0 * 10) % len(ACKS)]
        yield {"type": "delta", "text": acked, "ack": True}
    try:
        r = await work
    except Exception as e:  # noqa: BLE001 - the person always hears something
        r = {"answer": "Sorry, something went wrong there.", "error": f"{type(e).__name__}: {e}"}
    for s in sentences(r.get("answer") or ""):
        yield {"type": "delta", "text": s}
    keep = {k: r[k] for k in ("via", "red_flag", "checkin", "checkin_asked", "approval_sent", "confirm_needed",
                              "handoff", "private", "undo", "why", "error") if k in r}
    yield {"type": "done", "answer": r.get("answer") or "", "continue": bool(r.get("continue")),
           "acked": acked, "ms": round((time.monotonic() - t0) * 1000), **keep}


def ndjson(event):
    return (json.dumps(event, default=str) + "\n").encode()
