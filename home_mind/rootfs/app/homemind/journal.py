"""The Mind Journal, kept in Home Mind. Entries spoken to Jeeves are saved on the journal shelf (through the
"Remember in Home Mind" script). Every night at 23:30 the day's entries are summarized by a model (Haiku if
connected) with the same instructions the old calendar automation used, and the summary is saved on
journal/summaries. Days with no entries get nothing."""
import logging
from datetime import datetime, timedelta

log = logging.getLogger("homemind.journal")

INSTRUCTIONS = ("These are my spoken journal entries from today, with the time of each. Write a short daily summary "
                "(3-6 sentences) in plain language, in second person (\"you\"). Cover the main themes, decisions, and "
                "anything I said I want to do next. Only mention feelings I actually stated. Never infer emotions, add "
                "advice, or comment on the entries themselves (times, labels, how many there are). Use only what I said.")


class Journal:
    def __init__(self, store, models, at="23:30"):
        self.store, self.models = store, models
        self.at = tuple(int(x) for x in at.split(":"))
        self.done_for = None

    def entries(self, day):
        start = datetime.combine(day, datetime.min.time()).timestamp()
        end = start + 86400
        rows = self.store.db.execute(
            "SELECT * FROM items WHERE shelf='journal' AND status IN ('active','faded') AND created >= ? AND created < ? "
            "ORDER BY created", (start, end)).fetchall()
        return [self.store._row(r) for r in rows]

    def summarized(self, day):
        return self.store.db.execute(
            "SELECT 1 FROM items WHERE shelf='journal/summaries' AND status IN ('active','faded') AND "
            "json_extract(meta, '$.day') = ? LIMIT 1", (str(day),)).fetchone() is not None

    async def summarize(self, day=None, model=None):
        day = day or datetime.now().date()
        ents = self.entries(day)
        if not ents:
            return {"day": str(day), "entries": 0, "summary": None}
        names = [n for n in self.models.models if n != "mock"]
        model = model or ("haiku" if "haiku" in names else names[0] if names else None)
        if not model:
            return {"error": "no model connected"}
        # a person's personal entries get a summary of their own, personal too; household entries one for everyone
        groups = {}
        for e in ents:
            m = e.get("meta") or {}
            owner = m.get("owner") if m.get("scope") == "personal" and m.get("owner") else None
            groups.setdefault(owner, []).append(e)
        out = None
        for owner, group in sorted(groups.items(), key=lambda kv: kv[0] is not None):
            r = await self._summarize(day, model, group, owner)
            out = out or r
        if len(groups) > 1:
            out = dict(out, summaries=len(groups))
        return out

    async def _summarize(self, day, model, ents, owner):
        from .privacy import is_cloud, mask_all
        lines = "\n".join(f"[{datetime.fromtimestamp(e['created']).strftime('%I:%M %p').lstrip('0')}] {e['body']}"
                          for e in ents)
        if is_cloud(self.models.get(model)):
            lines = mask_all(lines)        # a summary never needs a code or password, and this one leaves the house
        r = await self.models.get(model).chat("You write short daily journal summaries.",
                                              [{"role": "user", "content": INSTRUCTIONS + "\n\n" + lines}],
                                              max_tokens=400, temperature=0.3)
        n = len(ents)
        title = f"Daily summary {day:%a %b %d %Y} ({n} entr{'y' if n == 1 else 'ies'})"
        at = datetime.combine(day, datetime.min.time()).replace(hour=23, minute=30).timestamp()
        meta = {"day": str(day), "entries": [e["id"] for e in ents], "model": model}
        if owner:
            meta.update(scope="personal", owner=owner)
        out = self.store.remember(text=r["text"].strip(), title=title, kind="note", shelf="journal/summaries",
                                  tags=["journal", "summary"], source="journal", at=min(at, datetime.now().timestamp()),
                                  meta=meta)
        return {"day": str(day), "entries": n, "summary": r["text"].strip(), "id": out.get("id")}

    async def tick(self):
        now = datetime.now()
        if (now.hour, now.minute) >= self.at and self.done_for != now.date():
            self.done_for = now.date()
            if self.summarized(now.date()):
                return          # a restart after tonight's summary: done_for was lost, the summary wasn't
            try:
                r = await self.summarize(now.date())
                log.info("journal summary: %s", {k: v for k, v in r.items() if k != "summary"})
            except Exception:  # noqa: BLE001
                log.exception("journal summary")
