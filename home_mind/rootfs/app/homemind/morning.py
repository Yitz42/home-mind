"""The morning summary: once a day (option morning_time, default 07:30) a short note of what Home Mind filed in the
last 24 hours on its own or from what you told it, plus anything that went wrong and today's calendar. Sent to your
phone (the first mobile_app notify service, or option morning_notify) and as a Home Assistant notification, and kept
on summaries/morning. Anything filed wrongly can be forgotten from Memory > Recently added.

It also asks about one old fact a day ("Still true? ..."), with Still true / It changed buttons on the phone, so
facts nobody has confirmed in a long time get checked (secret values are never shown, only what they are for)."""
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
        self.enabled = True          # the Morning summary switch; off skips the daily send (not a button press)
        self.checkin_on = True       # option checkin_old_facts
        self.checkin_days = 180      # option checkin_after_days: a fact this old and not confirmed since gets asked
        self._checkin = None

    # ---- old facts: still true? ----------------------------------------------------------------------------------
    SKIP_SOURCES = ("home", "calendar", "automations", "journal", "scale", "morning", "patterns", "check-in", "test", "habits",
                    "tidy", "evening",
                    "pantry", "nutrition")

    def pick_checkin(self):
        """The old fact most worth checking: a current fact you told it (not one it filed from the house), older than
        checkin_days, not confirmed or asked about within that time, most used first."""
        if not self.checkin_on:
            return None
        cut = time.time() - self.checkin_days * 86400
        rows = self.store.db.execute(
            f"SELECT * FROM items WHERE kind='fact' AND status='active' AND created < ? AND source NOT IN "
            f"({','.join('?' * len(self.SKIP_SOURCES))}) AND source NOT LIKE 'pack:%' "
            "ORDER BY use_count DESC, created LIMIT 40", (cut, *self.SKIP_SOURCES)).fetchall()
        from .speakers import visible
        for r in rows:
            d = self.store._row(r)
            m = d["meta"] or {}
            if not visible(m, ""):
                continue        # the summary goes to a shared phone and notifications: no one's personal notes
            if max(m.get("confirmed") or 0, m.get("checked") or 0) >= cut:
                continue
            # an unkeyed fact may have a newer version that the search groups with it: skip those
            if any(h.get("newer") == d["id"] for h in self.store.recall(d["title"], limit=5, touch=False)):
                continue
            return d
        return None

    def _checkin_line(self, d):
        from . import privacy
        when = datetime.fromtimestamp(d["created"]).strftime("%b %Y")
        if privacy.is_sensitive(d["title"] + " " + d["body"], d["meta"]):
            what = re.sub(r"\s+(?:is|are|was|=|:)(?:\s.*)?$", "", d["title"], flags=re.I)
            what = re.sub(r"^(?:the|my|our)\s+", "", what, flags=re.I).strip()
            what = privacy.mask(what, "", lambda x: [], lambda x: set()) or "saved code"
            return f"Still true? The {what} you saved in {when}: has it changed?"
        return f"Still true? {_short(d)} (noted {when})"

    def _mark_checked(self, item_id):
        d = self.store.get(item_id)
        if not d:
            return
        meta = dict(d["meta"] or {}, checked=time.time())
        self.store.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(meta), item_id))
        self.store.db.commit()

    async def on_action(self, action):
        """A tapped Still true / It changed button from the morning summary."""
        m = re.fullmatch(r"HM_(STILL|CHANGED)_(\d+)", action or "")
        if not m:
            return None
        i = int(m.group(2))
        d = self.store.get(i)
        if not d:
            return None
        meta = dict(d["meta"] or {}, checked=time.time())
        if m.group(1) == "STILL":
            meta["confirmed"] = time.time()
            self.store.feedback(i, True, "confirmed still true")
        else:
            meta["doubted"] = time.time()
            self.store.feedback(i, False, "said it changed")
            await self.ha.notify("Home Mind: what changed?", f"You said this changed: {_short(d)}. Tell Jeeves or "
                                 "Home Mind the new value and it will replace the old one.", "hm_changed")
        self.store.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(meta), i))
        self.store.db.commit()
        return {"id": i, "answer": m.group(1).lower()}

    def _rows(self, where, args=()):
        return [self.store._row(r) for r in self.store.db.execute(
            f"SELECT * FROM items WHERE {where} ORDER BY created", args).fetchall()]

    def _due(self, structure, now, commit=False):
        """The Due line: each reminder only when its stage changes (due tomorrow, today, a week late; 30, 7 and 1 days
        before an end date), so an overdue chore isn't repeated every morning. Repeating things today or tomorrow are
        always listed."""
        said = self.store.kv_get("due_said") or {}
        keep, new = {}, []
        for it in structure.due_items(self.store, now):
            keep[it["key"]] = it["stage"]
            if said.get(it["key"]) != it["stage"]:
                new.append(it["text"])
        if commit:          # only what is sent counts as said; a preview changes nothing
            self.store.kv_set("due_said", keep)
        chores = {c["verb"] + " the " + c["thing"] for c in structure.chore_states(self.store, now)}
        rep = [x for x in structure.due_lines(self.store, now) if not any(x.startswith(c + " (") for c in chores)]
        return new + rep

    def build(self, hours=24, commit=False):
        """The summary is read on a shared phone, in Home Assistant's notifications and kept as a household note, so
        it is built as an unknown viewer would see memory: household notes only (#48)."""
        from .speakers import VIEWER
        tok = VIEWER.set("")
        try:
            return self._build(hours, commit)
        finally:
            VIEWER.reset(tok)

    def _build(self, hours=24, commit=False):
        from .speakers import visible
        since = time.time() - hours * 3600
        told = self._rows("created >= ? AND source NOT IN ('home','calendar','automations','journal','scale','habits','tidy','evening') "
                          "AND source NOT LIKE 'pack:%' AND kind != 'procedure' AND status='active'", (since,))
        told = [d for d in told if visible(d["meta"])]
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
        fit = getattr(self, "fitness", None)
        if fit:
            try:
                target = fit.protein_goal() or target       # a set protein goal wins over the weight-based one
                if fit.goals().get("weight_target"):
                    lines += [ln for ln in fit.status_lines() if ln.startswith("Weight:")]
            except Exception:  # noqa: BLE001
                pass
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
        from . import structure, tidy
        try:
            due = self._due(structure, self.store.clock(), commit)
        except Exception:  # noqa: BLE001
            log.exception("due reminders")
            due = []
        if due:
            lines.insert(0, "Due: " + "; ".join(due[:6]) + (" …" if len(due) > 6 else ""))
        dup = tidy.summary_line(self.store, _short)       # secret values hidden, as in the rest of the summary
        conf = tidy.conflict_line(self.store, _short)
        if conf:
            lines.append(conf)
        if dup:
            lines.append(dup)
        held = [r for r in self.store.review_queue() if r["status"] == "review"]
        if held:
            lines.append(f"Held for review {len(held)}: notes from conversations that read like instructions to the "
                         "assistant. Approve or drop them in Memory.")
        self._checkin = self.pick_checkin()
        if self._checkin:
            lines.append(self._checkin_line(self._checkin))
        return "\n".join(lines)

    async def _framed(self, text):
        """The persona's opening and closing lines around the brief (the facts in between stay as they are)."""
        pe = getattr(self, "persona", None)
        if pe is None:
            return text
        try:
            tired = bool(await self.tired()) if getattr(self, "tired", None) else False
            ppl = self.people() if getattr(self, "people", None) else []
            who = (ppl[0].get("name") or "").title() or None if len(ppl) == 1 else None   # one person: by name
            return pe.frame(text, "morning", who, tired=tired)
        except Exception:  # noqa: BLE001 - a brief is never lost to styling
            return text

    async def send(self, text=None):
        text = text or self.build(commit=True)
        text = await self._framed(text)
        title = "Home Mind this morning"
        await self.ha.notify(title, text + "\n\nReview or forget anything in Home Mind › Memory › Recently added.",
                             "home_mind_morning")
        target = self.notify or await self._phone()
        pushed = None
        batch, day = getattr(self, "batch", None), getattr(self, "day", None)
        if batch is not None:
            # routine notices held since yesterday: as many as today's budget allows (none on a rough day)
            held = batch.take(day.budget() if day is not None else 4)
            if held:
                text += "\n\nWhile you were away: " + "; ".join(f"{h['title']}: {h['message']}" if h["title"] else
                                                                 h["message"] for h in held)
        if target:
            data = {"title": title, "message": text}
            day = getattr(self, "day", None)
            if day is not None and day.get() == "unknown":
                # the check-in: one tap (phones show three buttons at most, so it comes first)
                data["message"] = text + "\n\nGood, low or rough today?"
                data["data"] = {"tag": "hm_morning", "actions": [
                    {"action": "HM_DAY_GOOD", "title": "Good"}, {"action": "HM_DAY_LOW", "title": "Low"},
                    {"action": "HM_DAY_ROUGH", "title": "Rough"}]}
                day.asked()
            elif self._checkin:
                data["data"] = {"tag": "hm_morning", "actions": [
                    {"action": f"HM_STILL_{self._checkin['id']}", "title": "Still true"},
                    {"action": f"HM_CHANGED_{self._checkin['id']}", "title": "It changed"}]}
            try:
                pushed = await self.ha.push(target, data)
            except Exception as e:  # noqa: BLE001
                log.warning("morning notify %s: %s", target, e)
        self.store.remember(text=text, title=f"Morning summary {datetime.now():%a %b %d %Y}", kind="note",
                            shelf="summaries/morning", tags=["summary"], source="morning")
        if self._checkin:
            self._mark_checked(self._checkin["id"])
        return {"sent_to": target or "Home Assistant notifications", "text": text, "phone": pushed,
                "checkin": self._checkin and self._checkin["id"]}

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
            if not self.enabled:
                return
            try:
                r = await self.send()
                log.info("morning summary sent to %s", r["sent_to"])
            except Exception:  # noqa: BLE001
                log.exception("morning summary")


def _short(d):
    """A note's title for the summary, with any secret value hidden (the summary shows on a phone's lock screen)."""
    from . import privacy
    t = d["title"]
    if privacy.is_sensitive(t + " " + (d.get("body") or ""), d.get("meta")):
        # everything after the secret word goes ("Guest wifi password sunflower88" -> "Guest wifi password [hidden]")
        cut = re.sub(rf"(\b{privacy.SECRET_WORDS}\b).*$", r"\1 [hidden]", t, flags=re.I)
        t = cut if cut != t else privacy.mask(t, "", lambda x: [], lambda x: set())
    return t if len(t) <= 60 else t[:57] + "…"
