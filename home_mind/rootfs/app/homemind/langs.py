"""Which language a note is in, roughly (#21): enough to know when the English patterns can't read it."""
import re

COMMON = {
    "en": "the and is in of to it my was for on at with this that are have be not but",
    "es": "el la los las de que y en es un una por con para del se no lo mi su",
    "de": "der die das und ist nicht ein eine zu mit den im auf ich mein sie es auch",
    "fr": "le la les et est un une des du que dans pour pas sur mon ma il elle au",
    "nl": "de het een en is van niet op ik mijn met voor dat die zijn er te",
    "pt": "o a os as de que e em um uma para com não do da meu minha no na",
    "it": "il la lo gli le di che e un una per con non del della mio mia nel sono",
}
WORDS = {k: set(v.split()) for k, v in COMMON.items()}


def guess(text):
    text = text or ""
    letters = [c for c in text if c.isalpha()]
    if letters and sum(1 for c in letters if ord(c) > 0x024F) > len(letters) * 0.3:
        return "other"          # another script (Hebrew, Cyrillic, CJK...)
    ws = re.findall(r"[a-zà-ÿ]+", text.lower())
    if not ws:
        return "en"
    score = {k: sum(1 for w in ws if w in v) for k, v in WORDS.items()}
    best = max(score, key=score.get)
    return best if score[best] else "en"


def english(text):
    return guess(text) == "en"


_ONES = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                     "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_NUM_WORD = re.compile(r"\b(" + "|".join(sorted(_TENS, key=len, reverse=True)) + r")(?:[\s-](" +
                       "|".join(w for w in _ONES if w not in ("zero",) and _ONES[w] < 10) + r"))?\b|\b(" +
                       "|".join(sorted(_ONES, key=len, reverse=True)) + r")\b|\b(a dozen|dozen)\b", re.I)


def digits(text):
    """English number words as digits: 'five sets of five' -> '5 sets of 5', 'seven and a half hours' keeps 'and a
    half'. 'one' before a noun that isn't a count ('the one in the den') is rare in a log line, so it's converted too."""
    def sub(m):
        if m.group(1):
            return str(_TENS[m.group(1).lower()] + (_ONES[m.group(2).lower()] if m.group(2) else 0))
        if m.group(3):
            return str(_ONES[m.group(3).lower()])
        return "12"
    return _NUM_WORD.sub(sub, text or "")
