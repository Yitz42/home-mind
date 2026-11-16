"""Web lookups for current facts (#55): when a question asks about the world as it is now ("what's the price of",
"latest version of", "is X still open", "news about") and the memory has nothing good, a few search results are
added to the context, dated and with their links, so the model can answer and say where it came from.

Option `web_search`: "" (off), "searxng" (option `web_search_url`, your own SearXNG with JSON output on) or "brave"
(option `web_search_key`). Only the question goes out, and never one that holds a secret or a household member's
name; nothing from your notes is sent.
"""
import logging
import re
from datetime import datetime

log = logging.getLogger("homemind.web")

CURRENT = re.compile(
    r"\b(?:current(?:ly)?|latest|newest|today'?s?|right now|these days|this (?:week|month|year)|news|price of|cost of|"
    r"how much (?:is|does|are)|still (?:open|available|alive|running|on sale)|open (?:today|now|on)|weather|forecast|"
    r"who (?:is|are) the (?:current |new )?(?:ceo|president|prime minister|mayor|governor|owner|head|coach)|"
    r"exchange rate|stock|score|released?|version of|recall(?:ed)? on|opening hours|hours for)\b", re.I)
PERSONAL = re.compile(r"\b(?:my|our|mine|ours|i|me|we|us)\b", re.I)
RESULTS = 3


def wants(question, weak):
    """A question about the world now that the memory couldn't answer well (and isn't about the household)."""
    q = question or ""
    if not CURRENT.search(q):
        return False
    if PERSONAL.search(q) and not re.search(r"\b(?:price|cost|weather|forecast|news|open|hours|recall)", q, re.I):
        return False          # "what's my current router password" is a memory question, not a web one
    return bool(weak)


def safe_to_send(question, people=()):
    from .privacy import is_sensitive
    if is_sensitive(question):
        return False
    low = question.lower()
    return not any(p and re.search(rf"\b{re.escape(p.lower())}\b", low) for p in people)


class Web:
    def __init__(self, opts, session_fn):
        self.kind = (opts.get("web_search") or "").lower()
        self.url = (opts.get("web_search_url") or "").rstrip("/")
        self.key = opts.get("web_search_key") or ""
        self.people = [p.get("name") if isinstance(p, dict) else str(p) for p in (opts.get("people") or [])]
        self.session_fn = session_fn

    @property
    def on(self):
        return (self.kind == "searxng" and bool(self.url)) or (self.kind == "brave" and bool(self.key))

    async def search(self, q):
        import aiohttp
        t = aiohttp.ClientTimeout(total=3)
        if self.kind == "searxng":
            async with self.session_fn().get(self.url + "/search", params={"q": q, "format": "json"}, timeout=t) as r:
                r.raise_for_status()
                j = await r.json(content_type=None)
            res = j.get("results") or []
            return [{"title": x.get("title", ""), "url": x.get("url", ""), "text": x.get("content", ""),
                     "date": x.get("publishedDate")} for x in res[:RESULTS]]
        if self.kind == "brave":
            async with self.session_fn().get("https://api.search.brave.com/res/v1/web/search",
                                             params={"q": q, "count": RESULTS},
                                             headers={"X-Subscription-Token": self.key, "Accept": "application/json"},
                                             timeout=t) as r:
                r.raise_for_status()
                j = await r.json(content_type=None)
            res = (j.get("web") or {}).get("results") or []
            return [{"title": x.get("title", ""), "url": x.get("url", ""), "text": x.get("description", ""),
                     "date": x.get("page_age") or x.get("age")} for x in res[:RESULTS]]
        return []

    async def lines(self, question, weak):
        if not self.on or not wants(question, weak) or not safe_to_send(question, self.people):
            return ""
        try:
            res = await self.search(question)
        except Exception as e:  # noqa: BLE001 - the memory still answers
            log.warning("web search: %s", e)
            return ""
        if not res:
            return ""
        today = datetime.now().strftime("%Y-%m-%d")
        clean = lambda s: re.sub(r"<[^>]+>", "", re.sub(r"\s+", " ", s or "")).strip()[:400]
        out = [f"FROM THE WEB (searched {today}; current facts, not your notes. Name the site when you use one):"]
        for x in res:
            out.append(f"- {clean(x['title'])} ({x['url']}{', ' + str(x['date'])[:10] if x.get('date') else ''}): "
                       f"{clean(x['text'])}")
        return "\n".join(out)
