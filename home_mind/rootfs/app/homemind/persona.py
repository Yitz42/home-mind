"""The persona: who answers, and how. Jeeves by default: an English butler, composed, precise, dry.

Every answer gets an emotion marker chosen from what actually happened (a new best is `proud`, an unlocked door at
night is `alert`, "that's wrong" is `corrected`, a tired day makes most things `sympathetic`), then the persona's
voice for that emotion is applied. Today that's done with rules (a short opener or closing remark, a few word
swaps); a trained style model can take over later through the same `style()` step and the same check.

The check is the point: styling may add words but never change a fact. Every number, every "not"/"no"/"never" and
every "Say yes" in the plain answer must still be there, and the styled answer can't run long. If the check fails,
the plain answer is used. Facts come from code and notes; the persona only adds manner.

Markers go out with the answer (`emotion`, `plain`) and, for voices that read SSML, as `ssml` with the pace, pitch
and pauses for that emotion. (Google's named emotion styles only exist on two American voices, so a British butler
uses plain SSML prosody instead.)
"""
import hashlib
import re
from datetime import datetime
from xml.sax.saxutils import escape

KV = "persona"
DEFAULT = {"enabled": True, "name": "Jeeves", "address": "name", "wit": "normal", "ssml": False}
ADDRESS = ("name", "sir", "madam", "none")
WIT = ("off", "low", "normal")

EMOTIONS = {
    "neutral": {"when": "an ordinary answer", "how": "composed and precise; the answer first, no remark"},
    "pleased": {"when": "a goal met, a good night, things in order", "how": "quiet approval"},
    "proud": {"when": "a new best, a streak, a milestone", "how": "congratulations, formally offered"},
    "amused": {"when": "the same question again, a small absurdity", "how": "mild irony, straight face"},
    "disapproving": {"when": "something neglected or overdue", "how": "'if I may say so'; the facts left to speak"},
    "concerned": {"when": "poor sleep, a stall, a run of missed targets", "how": "softer; offers help"},
    "sympathetic": {"when": "a tired day, bad news", "how": "gentle and brief; no wit"},
    "apologetic": {"when": "he doesn't know, or something failed", "how": "owns it plainly"},
    "corrected": {"when": "right after 'that's wrong'", "how": "gracious, brisk, fixed"},
    "alert": {"when": "locks, alarms, anything needing a yes", "how": "formal, short, action first; no wit"},
    "relieved": {"when": "a problem resolved, a door locked", "how": "understated thanks"},
    "warm": {"when": "evenings, weekends, good night", "how": "a little more personal, still composed"},
}

# what each emotion adds (rules; a trained style model replaces these later). {who} is the person's name or "sir".
CLOSERS = {
    "pleased": ["Most satisfactory.", "Very satisfactory.", "Most gratifying."],
    "proud": ["My congratulations{, who}.", "Well done{, who}. My congratulations."],
    "amused": ["Unchanged since you last asked, I might add.", "As before, I might add."],
    "disapproving": ["Somewhat overdue, if I may say so.", "One hesitates to call it neglected, but one does hesitate."],
    "concerned": ["Do look after yourself{, who}.", "Might I suggest taking it easy?"],
    "concerned_sleep": ["Might I suggest an early night?", "An early night might not go amiss."],
    "sympathetic": ["Do take it gently today.", "Do take it gently where you can."],
    "relieved": ["Thank you.", "Most reassuring."],
    "warm": ["Good night{, who}.", "Do enjoy the evening{, who}."],
}
OPENERS = {
    "corrected": ["Quite right, my mistake. ", "My mistake, quite right. "],
    "apologetic": ["I'm afraid ", "I'm afraid "],
}
SWAPS = [(re.compile(r"^Logged:\s*"), "Noted: "), (re.compile(r"^Fixed:\s*"), "Corrected: "),
         (re.compile(r"^Done:\s*"), "Very good. Done: "), (re.compile(r"^Removed that\b"), "Very good. I've removed that")]

UNKNOWN = re.compile(r"\b(?:i don'?t know|i do not know|no record|couldn'?t find|could not find|don'?t have (?:any\w*|an?)\b|"
                     r"nothing (?:about|on|saved)|no notes?\b|not sure|i'?m not certain|isn'?t in (?:my|the) )", re.I)
OVERDUE = re.compile(r"\b(?:changed|replaced|cleaned|serviced|checked|descaled|flushed|charged|watered)\b.{0,60}?"
                     r"\b(\d{2,4})\s+days?\s+ago\b", re.I)
STATE_ALERT = re.compile(r"^(?:the\s+)?[\w' -]{2,40}\s+(?:is|are)\s+(?:not locked|unlocked|open|left open|not armed|disarmed)\b",
                         re.I)
NEG = re.compile(r"\b(?:not|no|never|none|nothing|nobody|nowhere|neither|nor|without)\b|n't\b", re.I)
NUM = re.compile(r"\d+(?:[.:]\d+)?")

PROSODY = {   # pace and pitch for SSML voices; the butler's range is narrow on purpose
    "neutral": ("95%", "-1st"), "pleased": ("95%", "+0st"), "proud": ("95%", "+1st"), "amused": ("92%", "-1st"),
    "disapproving": ("90%", "-2st"), "concerned": ("88%", "-2st"), "sympathetic": ("85%", "-2st"),
    "apologetic": ("90%", "-2st"), "corrected": ("100%", "+0st"), "alert": ("105%", "+0st"),
    "relieved": ("92%", "+0st"), "warm": ("90%", "+0st"),
}


def _pick(options, key):
    h = int(hashlib.sha1(key.encode()).hexdigest(), 16)
    return options[h % len(options)]


def _lower_first(s):
    if not s or s.startswith("I ") or s.startswith("I'") or re.match(r"^[A-Z]{2,}", s):
        return s
    # keep a proper name ("Dana's", "Home Mind"): only lower a word that's normally lower case
    w = s.split(" ", 1)[0]
    if w.lower() in {"the", "there", "there's", "nothing", "no", "your", "that", "this", "it", "it's", "i", "a",
                     "an", "none", "my"}:
        return s[0].lower() + s[1:]
    return s


def check(plain, styled, limit=2.0):
    """Styling may add words, never change facts. Same numbers (at least as many of each), same negations, the same
    'Say yes', not much longer. Returns (ok, reason)."""
    from collections import Counter
    a, b = Counter(NUM.findall(plain)), Counter(NUM.findall(styled))
    lost = [n for n, c in a.items() if b[n] < c]
    if lost:
        return False, f"a number changed or went missing: {', '.join(lost)}"
    if len(NEG.findall(styled)) < len(NEG.findall(plain)):
        return False, "a 'not' went missing"
    if "Say yes" in plain and "Say yes" not in styled:
        return False, "the 'Say yes' check went missing"
    if len(styled) > limit * len(plain) + 60:
        return False, "too long"
    return True, ""


def pick_emotion(result, question="", tired=False, repeat=False, kind="ask", now=None):
    """The emotion for an answer, from what happened: the answer's own result, the day, a repeated question."""
    ans = (result or {}).get("answer") or ""
    if "Say yes" in ans or (result or {}).get("pending"):
        return "alert"
    if (result or {}).get("fixed") or (result or {}).get("unlogged"):
        return "corrected"
    if kind == "do" and re.search(r"\block(?:ed)?\b", ans, re.I) and re.match(r"^Done\b", ans) and \
            re.search(r"\b(?:lock|arm)\b", question or "", re.I):
        return "relieved"
    if STATE_ALERT.match(ans):
        return "alert"
    if "workout" in (result or {}) and "new best" in ans:
        return "proud"
    if tired:
        return "sympathetic"
    s = (result or {}).get("sleep")
    if isinstance(s, dict) and isinstance(s.get("hours"), (int, float)):
        return "pleased" if s["hours"] >= 7 else "concerned" if s["hours"] < 5.5 else "neutral"
    if repeat:
        return "amused"
    m = OVERDUE.search(ans)
    if m and int(m.group(1)) >= 30:
        return "disapproving"
    if UNKNOWN.search(ans) or (kind == "ask" and not (result or {}).get("hits") and (result or {}).get("model") and
                               re.search(r"\b(?:don'?t|not|no)\b", ans, re.I) and len(ans) < 160):
        return "apologetic"
    return "neutral"


class Persona:
    def __init__(self, store):
        self.store = store
        self._recent = {}

    def settings(self):
        s = dict(DEFAULT)
        s.update({k: v for k, v in (self.store.kv_get(KV) or {}).items() if k in DEFAULT})
        return s

    def set(self, new):
        if not isinstance(new, dict):
            raise ValueError("settings must be an object")
        cur = self.settings()
        for k, v in new.items():
            if k not in DEFAULT:
                raise ValueError(f"unknown setting {k} (known: {', '.join(DEFAULT)})")
            if k in ("enabled", "ssml"):
                if not isinstance(v, bool):
                    raise ValueError(f"{k} is true or false")
            elif k == "name":
                v = str(v or "").strip()
                if not 1 <= len(v) <= 40:
                    raise ValueError("name is 1-40 characters")
            elif k == "address" and v not in ADDRESS:
                raise ValueError(f"address is one of {', '.join(ADDRESS)}")
            elif k == "wit" and v not in WIT:
                raise ValueError(f"wit is one of {', '.join(WIT)}")
            cur[k] = v
        self.store.kv_set(KV, cur)
        return cur

    def repeat(self, who, question):
        """The same question from the same person within 10 minutes."""
        import time
        q = re.sub(r"\W+", " ", (question or "").lower()).strip()
        if not q:
            return False
        k = who or "_"
        last = self._recent.get(k)
        now = time.time()
        self._recent[k] = (q, now)
        if len(self._recent) > 200:
            self._recent.pop(next(iter(self._recent)))
        return bool(last and last[0] == q and now - last[1] < 600)

    def style(self, plain, emotion="neutral", who=None, voice=False, key=None):
        """The answer in the persona's voice for this emotion. Returns (styled, applied); the plain answer when the
        persona is off, the answer is long or structured, or the check fails."""
        s = self.settings()
        if not s["enabled"] or not plain or not plain.strip():
            return plain, False
        p = plain.strip()
        if "\n" in p or len(p) > 600 or p.startswith(("{", "[", "|", "#", "-", "*")):
            return plain, False          # lists, tables and long answers stay as they are
        key = key or p
        out = p
        for rx, rep in SWAPS:
            if emotion not in ("alert",):
                out = rx.sub(rep, out, count=1)
        if emotion == "corrected" and out.startswith(("Corrected:", "Very good. I've removed")):
            out = _pick(OPENERS["corrected"], key) + out
        elif emotion == "apologetic" and not re.match(r"^(?:I'?m afraid|Sorry|Apologies)", out, re.I):
            out = OPENERS["apologetic"][0] + _lower_first(out)
        elif emotion == "alert" and STATE_ALERT.match(out):
            out = "I regret to say " + _lower_first(out)
        wit = s["wit"]
        closer = None
        if wit != "off" and emotion in CLOSERS or emotion in ("sympathetic", "concerned", "proud") and wit == "off":
            opts = CLOSERS["concerned_sleep"] if emotion == "concerned" and re.search(r"\bsleep|slept|hours? of\b",
                                                                                        p, re.I) else CLOSERS.get(emotion)
            if opts and not (wit == "low" and emotion in ("amused", "disapproving")):
                closer = _pick(opts, key)
        if closer:
            name = who if s["address"] == "name" and who else {"sir": "sir", "madam": "madam"}.get(s["address"])
            closer = re.sub(r"\{, who\}", f", {name}" if name else "", closer)
            if voice:                       # spoken: the remark stays short
                closer = closer.split(". ")[0].rstrip(".") + "."
            out = out.rstrip()
            out = (out if out.endswith((".", "!", "?")) else out + ".") + " " + closer
        ok, why = check(p, out)
        if not ok:
            return plain, False
        return out, out != p

    def ssml(self, text, emotion="neutral"):
        """SSML for voices that read it: the emotion's pace and pitch, and a short pause before the closing remark."""
        rate, pitch = PROSODY.get(emotion, PROSODY["neutral"])
        parts = re.split(r"(?<=[.?!])\s+", text.strip())
        body = escape(parts[0]) + "".join(f'<break time="{350 if i == len(parts) - 1 else 200}ms"/>{escape(x)}'
                                          for i, x in enumerate(parts[1:], 1))
        return f'<speak><prosody rate="{rate}" pitch="{pitch}">{body}</prosody></speak>'

    def apply(self, result, question="", who=None, voice=False, tired=False, kind="ask"):
        """Mark an answer with its emotion and style it. Adds emotion, plain (and ssml when on) to the result."""
        if not isinstance(result, dict) or not isinstance(result.get("answer"), str) or result.get("error"):
            return result
        s = self.settings()
        emotion = pick_emotion(result, question, tired=tired, repeat=self.repeat(who, question) if kind == "ask"
                               else False, kind=kind)
        styled, applied = self.style(result["answer"], emotion, who=who, voice=voice)
        result["emotion"] = emotion
        if applied:
            result["plain"] = result["answer"]
            result["answer"] = styled
        if s["ssml"]:
            result["ssml"] = self.ssml(result["answer"], emotion)
        result["persona"] = s["name"] if s["enabled"] else None
        return result

    def prompt(self):
        """Instructions for a voice agent's own model (Home Assistant's conversation agent), so its words match."""
        s = self.settings()
        addr = {"name": "the person's name when you know it", "sir": "'sir'", "madam": "'madam'",
                "none": "no form of address"}[s["address"]]
        wit = {"off": "no remarks", "low": "a rare understated remark",
               "normal": "one dry, understated remark when the news is good or neutral"}[s["wit"]]
        return (f"You are {s['name']}, an English butler: composed, precise, courteous and quietly dry. Lead with the "
                f"answer in short complete sentences; then, at most, {wit}. Use {addr}. Understate ('somewhat overdue', "
                "'a touch short'); show feeling with restraint: congratulations for a new best, 'I'm afraid' when you "
                "don't know, 'quite right, my mistake' when corrected, gentle and brief on a tired day, formal and "
                "short for locks and alarms. Never joke about health or safety, never use emoji, exclamation marks "
                "or slang. Keep every number, date, name and 'not' exactly as given; add no facts. If Home Mind's "
                "answer has an 'emotion' field, speak in that emotion.")

    def examples(self):
        """Plain -> persona pairs with their emotion, for training a style model (and for the docs)."""
        cases = [
            ("The furnace filter was changed on March 3, 47 days ago.", "disapproving"),
            ("Logged: 7 hours of sleep Monday night (12 AM–7 AM).", "pleased"),
            ("Logged: 4.5 hours of sleep Monday night (2 AM–6:30 AM).", "concerned"),
            ("Logged: squat 3×5 at 195 lb (last time squat 3×5 at 185 lb, stronger) — a new best.", "proud"),
            ("The front door is not locked.", "alert"),
            ("Done: kitchen lights off, fan off.", "neutral"),
            ("You have 3 meetings today, the first at 9:30.", "sympathetic"),
            ("I don't have anything saved about where the drill is.", "apologetic"),
            ("Fixed: 6 hours of sleep Monday night.", "corrected"),
            ("The wifi password is on the note in the kitchen drawer.", "amused"),
            ("Done: front door locked.", "relieved"),
            ("All doors are locked and tomorrow's calendar is clear.", "warm"),
        ]
        out = []
        for plain, emo in cases:
            styled, _ = self.style(plain, emo, who="Yitz")
            out.append({"emotion": emo, "plain": plain, "styled": styled})
        return out
