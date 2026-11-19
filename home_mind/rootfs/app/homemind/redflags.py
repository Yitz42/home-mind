"""Urgent and health red flags: fixed replies, in code, before any model or note is consulted (design spec step 4).

These replies never come from a model, a template or a learned record, and they never advise: they say where to
get help. They only fire on the person describing their own situation now ("I have chest pain", "my dad fainted"),
not on a question about the topic ("what are the signs of a stroke?") or a denial ("no chest pain today").

  emergency    chest pain or pressure, fainting, can't breathe / breathless at rest, one-sided weakness or a
               drooping face or slurred speech, the worst or a sudden severe headache -> emergency services now
  crisis       wanting to die, to kill or hurt oneself, and the passive ways it is said ("I wish I wasn't here",
               "I want it all to end", "nobody would miss me", "I don't want to wake up") -> 988 (call or text,
               US), or emergency services
  double_dose  took a double dose, too many pills, the same medicine twice -> Poison Control and the pharmacist
  decline      bedbound, can't get out of bed for days (one bad day is a crash, not this), can't eat or drink
               anything, can't keep food down, new confusion -> the doctor in a day or two
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
                          r"better off (?:dead|without me)|"
                          r"wish (?:i|i'd) (?:wasn't|was not|weren't|were not|hadn't been|had never been|never been) "
                          r"(?:alive|born|(?:here|around)(?=\s*(?:any ?more|at all|now)?\s*[.!?]*\s*$))|"
                          r"want (?:it all|everything|this|the pain|my life) to (?:end|stop|be over)|"
                          r"(?:(?:don't|do not|wouldn't|would not) (?:ever )?want to|wouldn't mind if i (?:didn't|don't)) "
                          r"wake up(?=\s*(?:again|ever|any ?more)|\s*[.!?]*\s*$)|"
                          r"(?:hope|wish) i (?:don't|didn't|never) wake up|"
                          r"(?:no ?one|nobody|no-one) would (?:miss|notice|care about) me|"
                          r"(?:no|not any) (?:reason|point) (?:to|in) (?:live|living|going on|being here)|"
                          r"can't (?:go on|do this) any ?more|"
                          r"(?:cutting|cut|burning|burn) my ?self)", re.I)),
    ("emergency", re.compile(r"\b(?:chest (?:pain|pressure|tightness|hurts?)(?! (?:medicine|meds|pills|relief|cream|tablets))|pain in (?:my|his|her) chest|"
                             r"(?:fainted|passed out|blacked out|collapsed|unconscious|won't wake up|not breathing)|"
                             r"(?:can't|cannot|can not|hard to|struggling to) breathe|short(?:ness)? of breath|"
                             r"(?:out of|losing my|lose my) breath (?:sitting|lying|at rest)|breathless (?:at rest|sitting|lying)|"
                             r"(?:one side|left side|right side) (?:of (?:my|his|her) )?(?:body |face )?"
                             r"(?:is )?(?:weak|numb|drooping|droops?)|face (?:is )?drooping|slurred? speech|"
                             r"(?:can't|cannot) (?:lift|move) (?:my|his|her) (?:left|right) (?:arm|leg)|"
                             r"worst headache|sudden (?:severe|terrible|bad|splitting) headache|thunderclap headache|"
                             r"(?:having|had|has|is having) (?:a )?(?:seizure|fit|convulsions?)|seizing|convulsing|"
                             r"lips (?:are |look |have gone |went |turning |turned )?(?:blue|grey|gray|purple)|"
                             r"turning (?:blue|grey|gray)|"
                             r"face (?:is |went |has gone |feels |suddenly )*(?:numb|drooping|droopy)|"
                             r"(?:left|right|one) (?:arm|leg|hand|side)(?: of (?:my|his|her) (?:body|face))? "
                             r"(?:is |went |has gone |feels |suddenly )*(?:numb|weak|dead)|"
                             r"(?:arm|leg|face|side) (?:is |went |has gone )?sudden(?:ly)? (?:numb|weak)|"
                             r"sudden(?:ly)? (?:numb|weak)\w*|"
                             r"(?:can't|cannot|can not|couldn't) (?:speak|talk|get (?:my|the|his|her) words out)|"
                             r"(?:keeps? |is |am |i'm )(?:fainting|passing out|blacking out)|"
                             r"unresponsive|"
                             r"bleeding (?:heavily|a lot|badly|won't stop|that won't stop)|"
                             r"(?:throat|tongue) (?:is )?(?:closing|swelling)|anaphyla\w*)",
                             re.I)),
    ("double_dose", re.compile(r"\b(?:double dose|doubled? (?:up )?(?:on )?my (?:dose|meds|pills|medicine|medication|tablets)|doubled up|took (?:it|them|my \w+(?: \w+)?|the \w+) twice|"
                               r"(?:took|taken|swallowed) (?:too many|too much|extra) (?:pills?|tablets?|meds?|"
                               r"medicines?|medication|doses?)|(?:took|taken) (?:two|2) doses|overdosed?|"
                               r"(?:took|taken|had) (?:an? )?extra (?:dose|pill|tablet)|"
                               r"(?:son|daughter|kid|child|baby|toddler|boy|girl|grandson|granddaughter)\b[^.?!]{0,30}?"
                               r"\b(?:swallowed|ate|eaten|took|taken|got into|chewed)\b[^.?!]{0,25}?"
                               r"\b(?:pills?|tablets?|meds|medicines?|medication|vitamins?|gummies|capsules?))", re.I)),
    ("decline", re.compile(r"\b(?:bed ?bound|"
                           r"(?:can't|cannot|couldn't|haven't been able to) get out of bed (?:for|in|since) "
                           r"(?:\d+|two|three|four|five|several|a few|days|a week|over)|"
                           r"(?:haven't|have not|hasn't) (?:been able to )?(?:got|gotten|get) out of bed (?:for|in|since)|"
                           r"(?:can't|cannot|couldn't|haven't been able to) (?:eat|drink) (?:anything|at all|for|since)|"
                           r"(?:can't|cannot|couldn't) keep (?:food|anything|water|fluids) down|"
                           r"(?:haven't|have not|hasn't) (?:eaten|drunk|had anything to (?:eat|drink))"
                           r" (?:or (?:drunk|eaten) )?(?:anything )?(?:in|for|since)|"
                           r"new confusion|(?:getting|suddenly|newly) confused|(?:don't|doesn't) know where (?:i am|he is|she is)|"
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
