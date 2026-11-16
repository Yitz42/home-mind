"""The evening home brief (#51): what happened in the house today, at 21:00 (option evening_time) on the phone and
in Home Assistant's notifications. Notable house changes (house.NOTABLE and up: locks, garage doors, things at an
unusual hour, unusual readings), automations that ran or failed, what the comfort brain did, what was learned, and
suggested automations waiting. Routine changes are left out. Nothing is sent on a day with nothing to say.
"""
import json
import logging
from datetime import datetime

from .privacy import mask_all

log = logging.getLogger("homemind.evening")


class Evening:
    def __init__(self, store, ha, at="21:00", notify="", phone=None):
        self.store, self.ha = store, ha
        self.at = tuple(int(x) for x in (at or "21:00").split(":"))
        self.notify, self.phone = notify, phone
        self.enabled = True
        self.done_for = None

    def _rows(self, where, args=()):
        return [dict(r) for r in self.store.db.execute(f"SELECT * FROM items WHERE {where} ORDER BY created", args)]

    def build(self, now=None):
        from . import house
        now = now or self.store.clock()
        start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        lines = []
        notable = []
        for r in self._rows("source='home' AND created >= ? AND created <= ? AND shelf IN ('home/devices','home/readings')",
                            (start, now)):
            m = json.loads(r["meta"] or "{}")
            if (m.get("salience") or 0) >= house.NOTABLE:
                t = datetime.fromtimestamp(r["created"]).strftime("%I:%M %p").lstrip("0")
                why = ", ".join(x for x in (m.get("why") or []) if x != "habit")
                notable.append(m.get("unusual") or f"{t} {r['title']}" + (f" ({why})" if why else ""))
        if notable:
            lines.append(f"House ({len(notable)}): " + "; ".join(notable[:6]) + (" …" if len(notable) > 6 else ""))
        runs = self._rows("created >= ? AND created <= ? AND source='automations' AND shelf='home/automations' AND "
                          "title LIKE 'Ran %'", (start, now))
        if runs:
            failed = [r for r in runs if "fail" in (r["body"] or "").lower() or "error" in (r["body"] or "").lower()]
            names = sorted({r["title"][4:].split(" (")[0] for r in runs})
            lines.append(f"Automations ran {len(runs)} times ({', '.join(names[:5])}{' …' if len(names) > 5 else ''})"
                         + (f", {len(failed)} failed" if failed else ""))
        comfort = self._rows("created >= ? AND created <= ? AND shelf='comfort/actions'", (start, now))
        if comfort:
            lines.append(f"Comfort: {len(comfort)} change{'s' if len(comfort) != 1 else ''}, last: "
                         + mask_all(comfort[-1]["body"] or comfort[-1]["title"])[:90])
        learned = self._rows("created >= ? AND created <= ? AND status='active' AND kind != 'procedure' AND source NOT IN "
                             "('home','calendar','automations','journal','scale','morning','evening','habits','tidy',"
                             "'patterns','comfort','weather') AND source NOT LIKE 'pack:%' AND shelf NOT LIKE 'summaries%'",
                             (start, now))
        from .speakers import visible
        learned = [r for r in learned if visible(json.loads(r["meta"] or "{}"), "")]    # shared phone: household only
        if learned:
            lines.append(f"Remembered {len(learned)}: " + "; ".join(mask_all(r["title"])[:50] for r in learned[:4])
                         + (" …" if len(learned) > 4 else ""))
        model = self.store.kv_get("house_model") or {}
        sug = [s for s in model.get("suggestions") or [] if s["id"] not in (self.store.kv_get("suggestions_done") or [])]
        if sug:
            lines.append(f"Suggested automations {len(sug)}: " + "; ".join(s["config"]["alias"] for s in sug[:2])
                         + ". Create or dismiss them in Home Mind › House.")
        return "\n".join(lines)

    async def send(self, text=None):
        text = text if text is not None else self.build()
        if not text:
            return {"sent_to": None, "text": "", "skipped": "nothing to say today"}
        title = "Home Mind this evening"
        await self.ha.notify(title, text, "home_mind_evening")
        target = self.notify or (await self.phone() if self.phone else None)
        pushed = None
        if target:
            try:
                pushed = await self.ha.push(target, {"title": title, "message": text})
            except Exception as e:  # noqa: BLE001
                log.warning("evening notify %s: %s", target, e)
        self.store.remember(text=text, title=f"Evening brief {datetime.now():%a %b %d %Y}", kind="note",
                            shelf="summaries/evening", tags=["summary"], source="evening")
        return {"sent_to": target or "Home Assistant notifications", "text": text, "phone": pushed}

    async def tick(self):
        now = datetime.now()
        if (now.hour, now.minute) >= self.at and self.done_for != now.date():
            self.done_for = now.date()
            if not self.enabled:
                return
            try:
                r = await self.send()
                log.info("evening brief: %s", r.get("skipped") or f"sent to {r['sent_to']}")
            except Exception:  # noqa: BLE001
                log.exception("evening brief")
