"""Talk: a conversation with Home Mind in the panel, by voice or by typing, that keeps its thread and offers the
answers it expects as buttons.

The panel's Talk view sends each turn to /api/talk ({"text", "spoken"?}). A turn is handled like the voice agent's
(voice.py): a house command goes to App.do, anything else to App.answer, both in this person's conversation, so a
follow-up ("and yesterday?", "yes") is understood. The answer comes back whole (the panel speaks it if the turn was
spoken), with:

  choices   the answers Home Mind expects next, as buttons: Yes / No for an action that needs a yes, Good / Low /
            Rough for the check-in, the devices for "Which one: ...?", the options of any "A or B?" question,
            and a couple of follow-ups after a connected answer ("What could I try?")
  expects   "choice" when it is waiting on one of them (a typed answer still works), else None

The thread (what was said, the answers, the choices) is kept per person, the last 100 turns, so it is the same on
the phone and the computer and survives a reload. "New conversation" starts a fresh one. A guest's turns are never
kept. The check-in is asked once a day here too, the first time someone talks and hasn't answered it.
"""
import re
import secrets
import time

KV = "talk_threads"
KEEP = 100
WHICH = re.compile(r"Which one:\s*(.+?)\?\s*$", re.I)
OR_Q = re.compile(r"(?:^|[.!?]\s+)([^.!?]*\b(?:or)\b[^.!?]*)\?\s*$", re.I)
YES_NO = re.compile(r"\b(?:shall i|should i|do you want me to|want me to|ok to|okay to|is that right|say yes)\b[^.!?]*\?"
                    r"\s*$|\bsay yes\b", re.I)
FOLLOW_UPS = ["What could I try?", "Show me the numbers"]
MEAL_FOLLOW = "More meal ideas"


def _person_key(viewer):
    return (viewer or "_").lower()


def choices_for(res, viewer=None):
    """The answers this reply expects, as [label, ...], and whether it waits on one ("choice") or not (None)."""
    a = (res.get("answer") or "").strip()
    if res.get("checkin_asked"):
        return ["Good", "Low", "Rough"], "choice"
    if res.get("confirm_needed") or res.get("pending") or YES_NO.search(a):
        return ["Yes", "No"], "choice"
    if res.get("which"):
        return [str(w) for w in res["which"]][:6], "choice"
    m = WHICH.search(a)
    if m:
        parts = [p.strip() for p in re.split(r",\s*|\s+or\s+", m.group(1)) if p.strip()]
        if 1 < len(parts) <= 6:
            return parts, "choice"
    m = OR_Q.search(a)
    if m and not res.get("deep"):
        q = re.sub(r"^\s*(?:is it|was it|do you mean|did you mean|would you like|one thing:)\s*", "", m.group(1),
                   flags=re.I)
        parts = [p.strip(" ,") for p in re.split(r",\s*|\s+or\s+", q) if p.strip(" ,")]
        if 1 < len(parts) <= 5 and all(len(p.split()) <= 4 for p in parts):
            return [p[:1].upper() + p[1:] for p in parts], "choice"
    if res.get("deep"):
        return list(FOLLOW_UPS), None
    if res.get("pantry") and res.get("meals"):
        return [MEAL_FOLLOW], None
    return [], None


class Talk:
    def __init__(self, app, clock=None):
        self.app = app
        self.clock = clock or time.time

    @property
    def store(self):
        return self.app.mind.store

    def _threads(self):
        return dict(self.store.kv_get(KV) or {})

    def thread(self, viewer):
        """{"conversation_id", "turns": [...]} for this person (a new one if there is none)."""
        if viewer == "":
            return {"conversation_id": None, "turns": [], "guest": True}
        t = self._threads().get(_person_key(viewer))
        if not t:
            t = {"conversation_id": f"talk:{secrets.token_hex(6)}", "turns": [], "started": self.clock()}
        return t

    def _save(self, viewer, t):
        if viewer == "":
            return
        a = self._threads()
        t["turns"] = t["turns"][-KEEP:]
        a[_person_key(viewer)] = t
        self.store.kv_set(KV, a)

    def new(self, viewer):
        t = {"conversation_id": f"talk:{secrets.token_hex(6)}", "turns": [], "started": self.clock()}
        self._save(viewer, t)
        return t

    async def turn(self, text, viewer, scope="admin", spoken=False):
        """One turn: what was said in, the reply out (with choices), both kept in the thread."""
        from .voice import COMMAND, said_for
        from .tokens import allows
        text = (text or "").strip()
        if not text:
            raise ValueError("say or type something")
        app = self.app
        t = self.thread(viewer)
        cid = t["conversation_id"]
        t["turns"].append({"who": "you", "text": text, "at": self.clock(), "spoken": bool(spoken)})
        res = None
        if COMMAND.match(text) and allows(scope, "admin") and hasattr(app, "do"):
            r = await app.do(text, source="panel_talk", conversation_id=cid)
            if r.get("error") != "nothing on the control list fits":
                res = dict(r, answer=said_for(r), via="do")
        if res is None:
            b = {"question": text, "conversation_id": cid, "front_end": "panel_talk"}
            prev = [x for x in t["turns"][:-1] if x["who"] == "home mind"]
            if text in FOLLOW_UPS and prev and prev[-1].get("deep"):
                # "What could I try?" after a connected answer: the same question, asked again with the follow-up
                asked = [x for x in t["turns"][:-1] if x["who"] == "you"]
                b["question"] = f"{asked[-1]['text'] if asked else ''} {text}".strip()
                b["force_deep"] = True
            elif text == MEAL_FOLLOW and prev and prev[-1].get("pantry"):
                asked = [x for x in t["turns"][:-1] if x["who"] == "you" and x["text"] != MEAL_FOLLOW]
                base = asked[-1]["text"].rstrip("?!. ") if asked else "What should I make?"
                b["question"] = f"{base} (different ideas)"
            if viewer is not None:
                b["speaker"] = viewer
            res = await app.answer(b, scope)
            res = dict(res, via="ask")
        # the check-in, once a day, the first time someone talks here and hasn't answered it
        day = getattr(app, "day", None)
        if day is not None and viewer != "" and not res.get("red_flag") and not res.get("checkin") \
                and not res.get("confirm_needed"):
            from .day import QUESTION, DAY_STARTS
            from datetime import datetime
            who = viewer or None
            if day.get(who) == "unknown" and datetime.fromtimestamp(self.clock()).hour >= DAY_STARTS and \
                    not day.asked(who):
                res = dict(res, answer=f"{(res.get('answer') or '').rstrip()} {QUESTION}".strip(), checkin_asked=True)
        choices, expects = choices_for(res, viewer)
        missing = []
        for x in res.get("missing") or []:
            s = str(x).strip()
            if s and s.lower() not in {y.lower() for y in missing}:
                missing.append(s)
        for m in res.get("meals") or []:
            if isinstance(m, dict):
                for x in m.get("missing") or []:
                    s = str(x).strip()
                    if s and s.lower() not in {y.lower() for y in missing}:
                        missing.append(s)
        out = {"answer": res.get("answer") or "", "choices": choices, "expects": expects, "via": res.get("via"),
               "conversation_id": cid}
        for k in ("deep", "model", "quick", "compare_id", "red_flag", "checkin", "checkin_asked", "confirm_needed",
                  "private", "handoff", "error", "sources", "pantry", "meals", "week_plan"):
            if res.get(k) is not None:
                out[k] = res[k]
        if missing:
            out["missing"] = missing
        t["turns"].append({"who": "home mind", "text": out["answer"], "at": self.clock(), "choices": choices,
                           "expects": expects, **({"deep": True} if res.get("deep") else {}),
                           **({"pantry": True} if res.get("pantry") else {}),
                           **({"missing": missing} if missing else {}),
                           **({"meals": res["meals"][:3]} if res.get("meals") else {}),
                           **({"sources": res["sources"]} if res.get("sources") else {}),
                           **({"red_flag": res["red_flag"]} if res.get("red_flag") else {}),
                           **({"quick": res["quick"], "compare_id": res["compare_id"]} if res.get("compare_id")
                              else {})})
        self._save(viewer, t)
        return out


__all__ = ["Talk", "choices_for", "FOLLOW_UPS", "MEAL_FOLLOW"]
