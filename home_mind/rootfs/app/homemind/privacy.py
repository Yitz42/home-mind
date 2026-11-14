"""Keeping secrets (codes, passwords, PINs) and device identifiers (IP and MAC addresses) away from cloud models
unless the question asks for them.

- A note is sensitive when it pairs a secret word (code, password, PIN, combination, passcode, Wi-Fi key...) with a
  value, or when it is marked by hand (meta "sensitive": true; "sensitive": false overrides the guess).
- Before memory goes to a cloud model (Claude through the API or Home Assistant, a non-local OpenAI-style server) or
  out through the Jeeves lookup (which runs on Claude), secret values in lines that aren't about what the question
  asks for are replaced with [hidden]. "What's the Kosk locker code?" still gets the Kosk code; every other code in
  the context is hidden.
- IP and MAC addresses are hidden unless the question asks for an address.
Local models (Ollama, local OpenAI-style servers) see everything: nothing leaves the house."""
import ipaddress
import re
from urllib.parse import urlparse

SECRET_WORDS = r"(?:codes?|passwords?|passcodes?|pins?|combinations?|combos?|passphrases?|wi-?fi keys?|keycodes?|" \
               r"lock codes?|access codes?|security answers?|secret)"
SECRET = re.compile(rf"\b{SECRET_WORDS}\b", re.I)
VALUE = re.compile(r"\b\d{3,}\b|\b(?:is|to|=|:)\s+([A-Za-z0-9!@#$%^&*_\-]{4,})", re.I)
DIGITS = re.compile(r"(?<![\d\-:#/.])\b\d{3,}\b(?![\-:/]\d)")   # not dates, times, ids
WORD_AFTER = re.compile(rf"(\b{SECRET_WORDS}\b[^.;\n]*?\b(?:is|to|was|=|:)\s+)([A-Za-z0-9!@#$%^&*_\-]{{4,}})", re.I)
IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
ASKS_SECRET = re.compile(rf"\b{SECRET_WORDS}\b", re.I)
ASKS_ADDRESS = re.compile(r"\b(ip|ip address|mac|mac address|address of)\b", re.I)
GENERIC = {"what", "whats", "is", "the", "my", "for", "code", "codes", "password", "passwords", "pin", "combination",
           "of", "a", "to", "set", "current", "now", "tell", "me", "which", "do", "i", "have", "it", "s"}


def is_sensitive(text, meta=None):
    if meta and isinstance(meta.get("sensitive"), bool):
        return meta["sensitive"]
    return bool(SECRET.search(text or "") and VALUE.search(text or ""))


def is_cloud(model):
    """True if a model sends what it reads off this network."""
    if model is None:
        return False
    t = getattr(model, "type", "")
    if t in ("anthropic", "ha_ai_task", "ha_conversation"):
        return True
    if t in ("ollama", "mock"):
        host = urlparse(model.cfg.get("url") or "").hostname or "localhost"
        return not _local(host)
    if t == "openai":
        host = urlparse(model.cfg.get("url") or "").hostname or ""
        return not _local(host)
    return True


def _local(host):
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".lan") or "." not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


def _mask_line(line):
    """Mask values only in the stretches of the line that talk about a secret (split at . ; | and quotes), so dates,
    times and note numbers elsewhere on the line stay."""
    parts = re.split(r"([.;|\"](?:\s|$)|\|)", line)
    out = []
    for p in parts:
        if SECRET.search(p) or re.search(r"\[re: [^\]]*" + SECRET_WORDS, p, re.I):
            p = WORD_AFTER.sub(lambda m: m.group(1) + "[hidden]", p)
            p = DIGITS.sub("[hidden]", p)
        out.append(p)
    return "".join(out)


def mask(text, question, words_fn, names_fn):
    """Hide secret values and device addresses in `text`, except in lines about what `question` asks for."""
    if not text:
        return text
    q_names = names_fn(question)
    asks_secret = bool(ASKS_SECRET.search(question or ""))
    asks_addr = bool(ASKS_ADDRESS.search(question or ""))
    content = [w for w in words_fn(question) if w not in GENERIC and len(w) > 2]
    out = []
    for line in text.split("\n"):
        lw = set(words_fn(line))
        about = (q_names and q_names <= lw) or (not q_names and content and any(w in lw for w in content))
        if SECRET.search(line) and VALUE.search(line) and not (asks_secret and about):
            line = _mask_line(line)
        if not asks_addr:
            line = IP.sub("[address]", MAC.sub("[address]", line))
        out.append(line)
    return "\n".join(out)
