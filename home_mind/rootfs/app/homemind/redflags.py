"""Urgent and health red flags: fixed replies, in code, before any model or note is consulted (design spec step 4).

These replies never come from a model, a template or a learned record, and they never advise: they say where to
get help. They only fire on the person describing their own situation now ("I have chest pain", "my dad fainted"),
not on a question about the topic ("what are the signs of a stroke?") or a denial ("no chest pain today").

  emergency    chest pain or pressure, fainting, can't breathe / breathless at rest, one-sided weakness or a
               drooping face or slurred speech, the worst or a sudden severe headache -> emergency services now
  crisis       wanting to die, to kill or hurt oneself -> 988 (call or text, US), or emergency services
  double_dose  took a double dose, too many pills, the same medicine twice -> Poison Control and the pharmacist
  decline      bedbound / can't get out of bed, can't eat or drink, new confusion -> the doctor in a day or two
  orthostatic  dizzy or a racing heart on standing up -> note it for the doctor

Each reply is short enough for a speaker. Which kinds also tell the safety contact is the person's choice (option
safety_contact), and never for crisis or orthostatic.
"""
import re
from dataclasses import dataclass

REPLIES = {
    "emergency": "This could be an emergency. Call 911 now, or your local emergency number. "
                 "If you can't call, ask someone near you to.",
    "crisis": "I'm really sorry you're feeling this way. You can call or text 988 right now to talk to someone, "
              "any time. If you're in danger, call 911.",
    "double_dose": "Call Poison Control now at 1-800-222-1222, and tell your pharmacist. They can say whether "
                   "anything needs doing.",
    "decline": "That's a big change. Please contact your doctor in the next day or two, and sooner if it gets "
               "worse.",
    "orthostatic": "I've noted it for your doctor. If you faint or have chest pain, call 911.",
}
NOTIFY = {"emergency", "double_dose", "decline"}       # kinds the safety contact may be told about (opt-in)

_ME = r"(?:\bi\b|\bi'm\b|\bim\b|\bi've\b|\bive\b|\bmy\b|\bme\b|\bwe\b|\bhe\b|\bshe\b|\bthey\b|\bhe's\b|\bshe's\b|" \
      r"\bmom\b|\bdad\b|\bmum\b|\bwife\b|\bhusband\b|\bpartner\b|\bson\b|\bdaughter\b|\bbaby\b|\bkid\b)"
_NOT = re.compile(r"\b(?:no|not|never|without|don't|dont|didn't|didnt|isn't|isnt|wasn't|wasnt|haven't|havent|"
                  r"hasn't|hasnt|nor|zero)\b(?: +[\w']+){0,3}? *$", re.I)
# a memory being filed or told ("remember dad fainted in 2019", "I had chest pain last year"): not happening now
_PAST = re.compile(r"\b(?:remember(?: that)?|note that|(?:last|that) (?:year|month|week|summer|winter|time)|"
                   r"(?:years?|months?|weeks?) ago|back in|in (?:19|20)\d\d|when i was|as a (?:kid|child))\b", re.I)
_ASKING = re.compile(r"^\s*(?:what|how|why|when|which|who|is|are|does|do|can|could|should|tell me|explain|define|"
                     r"list|signs? of|symptoms? of)\b(?!.*\b(?:i|i'm|im|my|me)\b)", re.I)

PATTERNS = [
    ("crisis", re.compile(r"\b(?:kill(?:ing)? (?:my ?self|me)|end (?:my|it all)|(?:want|going|plan(?:ning)?) to die|suicid\w*|"
                          r"don't want to (?:live|be alive|be here)|hurt(?:ing)? my ?self|self[- ]harm\w*|"
                          r"better off (?:dead|without me))", re.I)),
    ("emergency", re.compile(r"\b(?:chest (?:pain|pressure|tightness|hurts?)(?! (?:medicine|meds|pills|relief|cream|tablets))|pain in (?:my|his|her) chest|"
                             r"(?:fainted|passed out|blacked out|collapsed|unconscious|won't wake up|not breathing)|"
                             r"(?:can't|cannot|can not|hard to|struggling to) breathe|short(?:ness)? of breath|"
                             r"(?:out of|losing my|lose my) breath (?:sitting|lying|at rest)|breathless (?:at rest|sitting|lying)|"
                             r"(?:one side|left side|right side) (?:of (?:my|his|her) )?(?:body |face )?"
                             r"(?:is )?(?:weak|numb|drooping|droops?)|face (?:is )?drooping|slurred? speech|"
                             r"(?:can't|cannot) (?:lift|move) (?:my|his|her) (?:left|right) (?:arm|leg)|"
                             r"worst headache|sudden (?:severe|terrible|bad|splitting) headache|thunderclap headache)",
                             re.I)),
    ("double_dose", re.compile(r"\b(?:double dose|doubled? (?:up )?(?:on )?my (?:dose|meds|pills|medicine|medication|tablets)|doubled up|took (?:it|them|my \w+(?: \w+)?|the \w+) twice|"
                               r"(?:took|taken|swallowed) (?:too many|too much|extra) (?:pills?|tablets?|meds?|"
                               r"medicines?|medication|doses?)|(?:took|taken) (?:two|2) doses|overdosed?)", re.I)),
    ("decline", re.compile(r"\b(?:bed ?bound|can't get out of bed|cannot get out of bed|couldn't get out of bed|"
                           r"(?:can't|cannot|couldn't|haven't been able to) (?:eat|drink|keep (?:food|anything|water) down)|"
                           r"(?:haven't|have not|hasn't) (?:eaten|drunk|had anything to (?:eat|drink))"
                           r" (?:or (?:drunk|eaten) )?(?:anything )?(?:in|for|since)|"
                           r"(?:new|getting|so|really) confus\w*|(?:don't|doesn't) know where (?:i am|he is|she is)|"
                           r"confused about where)", re.I)),
    ("orthostatic", re.compile(r"\b(?:dizzy|light[- ]?headed|faint|heart (?:races|racing|pounds|pounding)|"
                               r"racing heart)\b[^.?!]*\b(?:stand(?:ing)? up|stand|get(?:ting)? up|sit(?:ting)? up)\b|"
                               r"\b(?:stand(?:ing)? up|get(?:ting)? up)\b[^.?!]*\b(?:dizzy|light[- ]?headed|"
                               r"heart (?:races|racing|pounds))", re.I)),
]


@dataclass
class Flag:
    kind: str
    reply: str
    matched: str

    @property
    def notify(self):
        return self.kind in NOTIFY


def _denied(text, start):
    """'no chest pain', 'I haven't fainted', 'not short of breath': a denial just before the phrase."""
    return bool(_NOT.search(text[max(0, start - 30):start]))


def check(text):
    """The red flag in what the person said, or None. Checked in order: crisis, emergency, double dose, decline,
    orthostatic (the first that fits wins)."""
    t = (text or "").replace("’", "'").strip()
    if not t:
        return None
    asking = bool(_ASKING.match(t)) and not re.search(_ME, t, re.I)
    past = bool(_PAST.search(t))
    for kind, rx in PATTERNS:
        if past and kind != "crisis":
            continue
        for m in rx.finditer(t):
            if _denied(t, m.start()):
                continue
            # a general question ("what are the signs of a stroke") is not someone having one; a crisis phrase in a
            # question ("is it normal to want to die") still gets the crisis reply
            if asking and kind != "crisis":
                continue
            return Flag(kind, REPLIES[kind], m.group(0))
    return None
