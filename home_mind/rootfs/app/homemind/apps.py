"""Read from your other home apps at question time (#62), so Home Mind answers without keeping a second copy.

  Homebox      (inventory)  option homebox_url and homebox_token (an API token from Homebox, or a session token):
               items matching the question, with their location and quantity
  Paperless-ngx (documents) option paperless_url and paperless_token (Settings > API token): documents matching the
               question, with their date and correspondent

Each is asked only when the question looks like it is about things or papers, with a 2.5 second limit; what they
return is added to the model's page labelled with where it came from. A failure leaves the answer to memory alone.
"""
import asyncio
import logging
import re

log = logging.getLogger("homemind.apps")

THINGS = re.compile(r"\b(where(?:'s| is| are| did)|how many|do (?:i|we) have|any|which box|stored|kept|spare|"
                    r"inventory|find)\b", re.I)
PAPERS = re.compile(r"\b(receipt|invoice|bill|warranty|contract|statement|letter|document|paper|tax|policy|manual|"
                    r"certificate|lease|insurance|report|form)s?\b", re.I)


def _terms(question):
    from .store import STOP, words
    return " ".join(w for w in words(question) if w not in STOP and len(w) > 2)[:80]


class Apps:
    def __init__(self, opts, session_fn):
        self.opts, self.session_fn = opts, session_fn

    @property
    def configured(self):
        return [n for n in ("homebox", "paperless") if self.opts.get(f"{n}_url") and self.opts.get(f"{n}_token")]

    async def _get(self, url, headers, params):
        import aiohttp
        async with self.session_fn().get(url, headers=headers, params=params,
                                         timeout=aiohttp.ClientTimeout(total=2.5)) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            return await r.json()

    async def homebox(self, question):
        base = self.opts["homebox_url"].rstrip("/")
        tok = self.opts["homebox_token"]
        j = await self._get(f"{base}/api/v1/items", {"Authorization": tok if tok.lower().startswith("bearer ")
                                                     else f"Bearer {tok}"}, {"q": _terms(question), "pageSize": 5})
        out = []
        for it in (j.get("items") or [])[:5]:
            loc = (it.get("location") or {}).get("name")
            qty = it.get("quantity")
            out.append(f"{it.get('name')}" + (f" (x{qty})" if qty and qty != 1 else "") + (f", in {loc}" if loc else ""))
        return out

    async def paperless(self, question):
        base = self.opts["paperless_url"].rstrip("/")
        j = await self._get(f"{base}/api/documents/", {"Authorization": f"Token {self.opts['paperless_token']}"},
                            {"query": _terms(question), "page_size": 4})
        out = []
        for d in (j.get("results") or [])[:4]:
            when = (d.get("created_date") or d.get("created") or "")[:10]
            out.append(f"{d.get('title')}" + (f", dated {when}" if when else "") + f" (Paperless document {d.get('id')})")
        return out

    async def lines(self, question):
        jobs = {}
        if not _terms(question):        # "where is it?": an empty search lists the first items, which aren't an answer
            return ""
        if "homebox" in self.configured and THINGS.search(question):
            jobs["HOMEBOX (inventory)"] = self.homebox(question)
        if "paperless" in self.configured and PAPERS.search(question):
            jobs["PAPERLESS (documents)"] = self.paperless(question)
        if not jobs:
            return ""
        got = await asyncio.gather(*jobs.values(), return_exceptions=True)
        parts = []
        for name, res in zip(jobs, got):
            if isinstance(res, Exception):
                log.warning("%s: %s", name, res)
                continue
            if res:
                parts.append(f"FROM {name}:\n" + "\n".join(f"- {x}" for x in res))
        return "\n\n".join(parts)
