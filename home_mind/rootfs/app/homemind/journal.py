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

    async def summarize(self, day=None, model=None):
        day = day or datetime.now().date()
        ents = self.entries(day)
        if not ents:
            return {"day": str(day), "entries": 0, "summary": None}
        names = [n for n in self.models.models if n != "mock"]
        model = model or ("haiku" if "haiku" in names else names[0] if names else None)
        if not model:
            return {"error": "no model connected"}
        lines = "\n".join(f"[{datetime.fromtimestamp(e['created']).strftime('%I:%M %p').lstrip('0')}] {e['body']}"
                          for e in ents)
        r = await self.models.get(model).chat("You write short daily journal summaries.",
                                              [{"role": "user", "content": INSTRUCTIONS + "\n\n" + lines}],
                                              max_tokens=400, temperature=0.3)
        n = len(ents)
        title = f"Daily summary {day:%a %b %d %Y} ({n} entr{'y' if n == 1 else 'ies'})"
        at = datetime.combine(day, datetime.min.time()).replace(hour=23, minute=30).timestamp()
        out = self.store.remember(text=r["text"].strip(), title=title, kind="note", shelf="journal/summaries",
                                  tags=["journal", "summary"], source="journal", at=min(at, datetime.now().timestamp()),
                                  meta={"day": str(day), "entries": [e["id"] for e in ents], "model": model})
        return {"day": str(day), "entries": n, "summary": r["text"].strip(), "id": out.get("id")}

    async def tick(self):
        now = datetime.now()
        if (now.hour, now.minute) >= self.at and self.done_for != now.date():
            self.done_for = now.date()
            try:
                r = await self.summarize(now.date())
                log.info("journal summary: %s", {k: v for k, v in r.items() if k != "summary"})
            except Exception:  # noqa: BLE001
                log.exception("journal summary")
