"""Calendars from Home Assistant, kept in Home Mind (read-only: the calendars stay where they are).

Every hour the events from 14 days ago to 60 days ahead are read from every calendar except the ones that are not
schedules (the old Mind Journal, the Dyson's own schedule). Each event is one note on calendar/<calendar>, written
out with the full weekday and date so it can be searched ("Tuesday Nov 17 2026, 10:00 AM to 11:15 AM"). Changed
events are rewritten, events that disappear from the window ahead are archived, and the same event on two calendars
(the three Hebcal feeds) is kept once.

Questions that name a day ("today", "tomorrow", "Tuesday", "Nov 20", "this week") also get that day's events from
here, in order, in the model's context."""
import hashlib
import logging
import re
from datetime import date, datetime, timedelta

log = logging.getLogger("homemind.calendars")
SKIP = ("calendar.mind_journal", "calendar.bedroom_dyson_c2a_us_mdb9996a_schedule")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _when(x):
    """An event start/end from Home Assistant: {'dateTime': ...} or {'date': ...} or a string."""
    if isinstance(x, dict):
        x = x.get("dateTime") or x.get("date")
    if not x:
        return None, False
    if len(x) == 10:
        return datetime.fromisoformat(x), True
    return datetime.fromisoformat(x.replace("Z", "+00:00")).astimezone().replace(tzinfo=None), False


def _fmt_day(d):
    return d.strftime("%A %b %d %Y").replace(" 0", " ")


def _fmt_time(d):
    return d.strftime("%I:%M %p").lstrip("0")


def asked_days(question, today):
    """The days a question asks about, or []."""
    q = (question or "").lower()
    out = []
    if re.search(r"\btoday\b|\btonight\b|\bthis (morning|afternoon|evening)\b", q):
        out.append(today)
    if re.search(r"\btomorrow\b", q):
        out.append(today + timedelta(days=1))
    if re.search(r"\byesterday\b", q):
        out.append(today - timedelta(days=1))
    if re.search(r"\bthis week\b", q):
        out += [today + timedelta(days=i) for i in range(7)]
    if re.search(r"\bnext week\b", q):
        mon = today + timedelta(days=7 - today.weekday())
        out += [mon + timedelta(days=i) for i in range(7)]
    for i, wd in enumerate(WEEKDAYS):
        m = re.search(rf"\b(last|next|this|on)?\s*{wd}\b", q)
        if m:
            ahead = (i - today.weekday()) % 7
            if m.group(1) == "last":
                out.append(today - timedelta(days=(today.weekday() - i) % 7 or 7))
            else:
                out.append(today + timedelta(days=ahead + (7 if m.group(1) == "next" and ahead == 0 else 0)))
    for m in re.finditer(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (\d{1,2})\b", q):
        try:
            d = date(today.year, MONTHS.index(m.group(1)) + 1, int(m.group(2)))
            if d < today - timedelta(days=180):
                d = d.replace(year=d.year + 1)
            elif d > today + timedelta(days=185):
                d = d.replace(year=d.year - 1)      # "Dec 31" asked on Jan 2: the one just past, not next December
            out.append(d)
        except ValueError:
            pass
    if re.search(r"\b(next|upcoming|coming up|schedule|soon)\b", q) and not out:
        out = [today + timedelta(days=i) for i in range(3)]
    seen, days = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            days.append(d)
    return days[:8]


QUIET_ASK = re.compile(r"\b(hebcal|jewish|shabbat|shabbos|havdalah|candle|parash|parasha|parashat|holiday|holidays|"
                       r"yom tov|rosh|chanukah|hanukkah|pesach|passover|sukkot|purim|torah)\b", re.I)


class Calendars:
    def __init__(self, store, ha, quiet=None):
        self.store, self.ha = store, ha
        self.last = None
        # calendars left out of answers and summaries unless the question is about them (kept in memory regardless)
        self.quiet = set(quiet or [])

    def _key(self, cal, ev):
        start, _ = _when(ev.get("start"))
        return hashlib.sha1(f"{(ev.get('summary') or '').strip().lower()}|{start}".encode()).hexdigest()[:16]

    def _text(self, calname, ev):
        start, allday = _when(ev.get("start"))
        end, _ = _when(ev.get("end"))
        s = (ev.get("summary") or "(no title)").strip()
        if allday:
            last = (end - timedelta(days=1)) if end else start
            when = _fmt_day(start) + (f" to {_fmt_day(last)}" if last.date() > start.date() else "") + " (all day)"
        else:
            when = f"{_fmt_day(start)}, {_fmt_time(start)}" + (f" to {_fmt_time(end)}" if end and end != start else "")
            if end and end.date() > start.date():
                when = f"{_fmt_day(start)} {_fmt_time(start)} to {_fmt_day(end)} {_fmt_time(end)}"
        t = f"Calendar ({calname}): {s}, {when}"
        if ev.get("location"):
            t += f", at {ev['location'].strip()}"
        if ev.get("description"):
            d = re.sub(r"\s+", " ", ev["description"]).strip()
            t += f". {d[:300]}"
        return s, t + ("" if t.endswith(".") else ".")

    async def sync(self, back=14, ahead=60):
        states = await self.ha.states() or []
        cals = [(s["entity_id"], (s.get("attributes") or {}).get("friendly_name") or s["entity_id"]) for s in states
                if s["entity_id"].startswith("calendar.") and s["entity_id"] not in SKIP]
        now = datetime.now()
        start = (now - timedelta(days=back)).strftime("%Y-%m-%dT00:00:00")
        end = (now + timedelta(days=ahead)).strftime("%Y-%m-%dT00:00:00")
        existing = {}
        for r in self.store.db.execute("SELECT id, body, meta FROM items WHERE source='calendar' AND status='active'"):
            m = re.search(r'"event": "([0-9a-f]+)"', r["meta"] or "")
            if m:
                existing[m.group(1)] = (r["id"], r["body"])
        seen, ok_cals, added, changed = set(), set(), 0, 0
        for eid, name in cals:
            evs = await self.ha.get_json(f"calendars/{eid}", {"start": start, "end": end})
            if not isinstance(evs, list):
                continue
            ok_cals.add(eid)
            low = name.lower()
            calname = ("UMBC" if "umbc" in low else "Personal" if "gmail" in low else
                       "Rivka" if "rivka" in low else "Hebcal" if "hebcal" in low else re.sub(r"@.*$", "", name).strip())
            for ev in evs:
                try:
                    k = self._key(eid, ev)
                    if k in seen:
                        continue        # same event on another calendar (the Hebcal feeds)
                    seen.add(k)
                    title, text = self._text(calname, ev)
                    st, allday = _when(ev.get("start"))
                    if k in existing:
                        if existing[k][1] != text:
                            self.store.revise(existing[k][0], body=text)
                            changed += 1
                        continue
                    self.store.remember(text=text, title=f"{title} ({_fmt_day(st)})", kind="note",
                                        shelf="calendar/" + re.sub(r"[^a-z0-9]+", "-", calname.lower()).strip("-"),
                                        tags=["calendar"], source="calendar",
                                        meta={"event": k, "calendar": eid, "start": st.isoformat(), "all_day": allday,
                                              "summary": title})
                    added += 1
                except Exception:  # noqa: BLE001
                    log.exception("event %s", ev.get("summary"))
        # events in the window ahead that are gone from a calendar that actually answered were cancelled
        gone = 0
        for k, (iid, _) in existing.items():
            if k in seen:
                continue
            row = self.store.get(iid)
            meta = (row or {}).get("meta") or {}
            if meta.get("calendar") not in ok_cals:
                continue
            st = meta.get("start")
            if st and datetime.fromisoformat(st) >= now - timedelta(days=1):
                self.store.set_status(iid, "archived")
                gone += 1
        self.last = {"at": now.isoformat(timespec="seconds"), "calendars": len(cals), "added": added,
                     "changed": changed, "cancelled": gone}
        return self.last

    def events_on(self, day, question=None):
        from .speakers import visible
        lo = datetime.combine(day, datetime.min.time())
        hi = lo + timedelta(days=1)
        out = []
        for r in self.store.db.execute("SELECT * FROM items WHERE source='calendar' AND status='active'"):
            d = self.store._row(r)
            if not visible(d["meta"]):
                continue
            st = d["meta"].get("start")
            if not st:
                continue
            if d["meta"].get("calendar") in self.quiet and not QUIET_ASK.search(question or ""):
                continue
            s = datetime.fromisoformat(st)
            if d["meta"].get("all_day"):
                # all-day events cover their whole span; the text holds the end
                m = re.search(r" to (\w+ \w+ \d+ \d{4}) \(all day\)", d["body"])
                last = datetime.strptime(m.group(1), "%A %b %d %Y") if m else s
                if s.date() <= day <= last.date():
                    out.append((s, d))
            elif lo <= s < hi:
                out.append((s, d))
        out.sort(key=lambda x: x[0])
        return [d for _, d in out]

    def lines_for(self, question):
        days = asked_days(question, datetime.now().date())
        if not days:
            return ""
        parts = []
        for d in days:
            evs = self.events_on(d, question)
            body = "\n".join("- " + re.sub(r"^Calendar \(", "(", e["body"]) for e in evs) or "- nothing on the calendars"
            parts.append(f"{_fmt_day(datetime.combine(d, datetime.min.time()))}:\n{body}")
        return "CALENDAR (from your calendars, synced hourly):\n" + "\n".join(parts)
