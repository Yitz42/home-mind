"""The morning summary: once a day (option morning_time, default 07:30) a short note of what Home Mind filed in the
last 24 hours on its own or from what you told it, plus anything that went wrong and today's calendar. Sent to your
phone (the first mobile_app notify service, or option morning_notify) and as a Home Assistant notification, and kept
on summaries/morning. Anything filed wrongly can be forgotten from Memory > Recently added."""
import json
import logging
import re
import time
from datetime import datetime

from . import nutrition

log = logging.getLogger("homemind.morning")


class Morning:
    def __init__(self, store, ha, calendars, at="07:30", notify=""):
        self.store, self.ha, self.cal = store, ha, calendars
        self.at = tuple(int(x) for x in (at or "07:30").split(":"))
        self.notify = notify
        self.done_for = None

    def _rows(self, where, args=()):
        return [self.store._row(r) for r in self.store.db.execute(
            f"SELECT * FROM items WHERE {where} ORDER BY created", args).fetchall()]

    def build(self, hours=24):
        since = time.time() - hours * 3600
        told = self._rows("created >= ? AND source NOT IN ('home','calendar','automations','journal','scale') "
                          "AND source NOT LIKE 'pack:%' AND kind != 'procedure' AND status='active'", (since,))
        overnight = [d for d in told if d["source"] == "overnight"]
        told = [d for d in told if d["source"] != "overnight"]
        ideas = [d for d in told if d["shelf"].startswith("ideas")]
        journal = [d for d in told if d["shelf"] == "journal"]
        facts = [d for d in told if d not in ideas and d not in journal]
        runs = self._rows("created >= ? AND source='automations' AND shelf='home/automations' AND title LIKE 'Ran %'",
                          (since,))
        errors = [d for d in runs if "error" in str(d["meta"].get("result", ""))]
        weigh = self._rows("created >= ? AND shelf='body/weight'", (since,))
        summary = self._rows("created >= ? AND shelf='journal/summaries'", (since,))
        cal_new = self._rows("created >= ? AND source='calendar'", (since,))
        lines = []
        if facts:
            lines.append(f"Remembered {len(facts)}: " + "; ".join(_short(d) for d in facts[:4])
                         + (" …" if len(facts) > 4 else ""))
        if overnight:
            lines.append(f"Learned overnight {len(overnight)} (check these): " + "; ".join(_short(d) for d in overnight[:5]))
        if ideas:
            lines.append(f"Ideas {len(ideas)}: " + "; ".join(_short(d) for d in ideas[:3]))
        if journal:
            lines.append(f"Journal entries: {len(journal)}" + (" (summary written)" if summary else ""))
        if weigh:
            w = weigh[-1]["meta"].get("weight")
            lines.append(f"Weigh-in: {w:.1f} lb" if isinstance(w, (int, float)) else "Weigh-in filed")
        if runs:
            names = sorted({d["title"][4:] for d in runs})
            lines.append(f"Automations ran {len(runs)} times ({', '.join(names[:5])}{' …' if len(names) > 5 else ''})"
                         + (f"; {len(errors)} failed: " + "; ".join(d["title"][4:] for d in errors[:3]) if errors else ""))
        if cal_new:
            lines.append(f"Calendar: {len(cal_new)} new or changed events")
        today = self.cal.events_on(datetime.now().date()) if self.cal else []
        if today:
            def brief(e):
                t = e["meta"].get("summary") or e["title"]
                if e["meta"].get("all_day"):
                    return f"{t} (all day)"
                s = datetime.fromisoformat(e["meta"]["start"])
                return f"{s:%I:%M %p}".lstrip("0") + f" {t}"
            lines.append("Today: " + "; ".join(brief(e) for e in today[:5]) + (" …" if len(today) > 5 else ""))
        last_w = self.store.db.execute("SELECT meta FROM items WHERE shelf='body/weight' AND status='active' "
                                       "ORDER BY created DESC LIMIT 1").fetchone()
        target = nutrition.protein_target(json.loads(last_w["meta"] or "{}").get("weight")) if last_w else None
        if target:
            lines.append(f"Protein today: aim for about {target} g (1.6 g per kg, the level where muscle gains top out)")
        pantry = getattr(self, "pantry", None)
        if pantry:
            soon = pantry.soon(2)
            past = [i for i in soon if i["days_left"] < 0]
            use = [i for i in soon if i["days_left"] >= 0]
            if use:
                lines.append("Use soon: " + ", ".join(f"{i['name']} ({'today' if i['days_left'] < 1 else str(round(i['days_left'])) + 'd'})"
                                                     for i in use[:6]))
            if past:
                lines.append("Past their date, still have them? " + ", ".join(i["name"] for i in past[:6])
                             + " (tell Jeeves \"I used…\" or \"I tossed…\")")
        if not lines:
            lines.append("Nothing new was filed in the last day.")
        return "\n".join(lines)

    async def send(self, text=None):
        text = text or self.build()
        title = "Home Mind this morning"
        await self.ha.notify(title, text + "\n\nReview or forget anything in Home Mind › Memory › Recently added.",
                             "home_mind_morning")
        target = self.notify or await self._phone()
        if target:
            dom, svc = target.split(".", 1)
            try:
                await self.ha.call(dom, svc, {"title": title, "message": text})
            except Exception as e:  # noqa: BLE001
                log.warning("morning notify %s: %s", target, e)
        self.store.remember(text=text, title=f"Morning summary {datetime.now():%a %b %d %Y}", kind="note",
                            shelf="summaries/morning", tags=["summary"], source="morning")
        return {"sent_to": target or "Home Assistant notifications", "text": text}

    async def _phone(self):
        svcs = await self.ha.get_json("services") or []
        for d in svcs:
            if d.get("domain") == "notify":
                for name in d.get("services", {}):
                    if name.startswith("mobile_app_"):
                        return f"notify.{name}"
        return None

    async def tick(self):
        now = datetime.now()
        if (now.hour, now.minute) >= self.at and self.done_for != now.date():
            self.done_for = now.date()
            try:
                r = await self.send()
                log.info("morning summary sent to %s", r["sent_to"])
            except Exception:  # noqa: BLE001
                log.exception("morning summary")


def _short(d):
    t = d["title"]
    return t if len(t) <= 60 else t[:57] + "…"
