"""TickTick: your tasks in Home Mind (read and add, through TickTick's open API).

Setup, once (System > Overview > TickTick):
  1. At developer.ticktick.com, "Manage Apps" > "+App Name". Set its OAuth redirect URL to http://localhost/ (or set
     option ticktick_redirect to the one you used). Copy the Client ID and Client Secret into the options
     ticktick_client_id and ticktick_client_secret.
  2. Press Connect. TickTick asks you to allow Home Mind, then sends your browser to the redirect URL, which won't
     load. Copy that whole address from the address bar and paste it into Home Mind. It holds a one-time code that
     Home Mind trades for a token (it lasts about 180 days; the morning summary says when it is running out).
The token is kept in /data/ticktick.json, never in memory, notes or backups of notes.

Every 15 minutes the open tasks of every list (the inbox too) become notes on tasks/ticktick/<list>, written with the
full weekday and date like calendar events ("ASL HW 9-1, due Friday Nov 27 2026"). Changed tasks are rewritten;
tasks that are done or deleted in TickTick are archived. Questions about tasks, homework or what's due get the
matching tasks in the model's context (overdue first); the morning summary lists what is overdue and due today.

"add a task …", "remind me to …" and "add … to my task list" go to the TickTick inbox when it is connected (option
ticktick_reminders, on by default), and "check off …" / "mark … done" finds the task by its title and completes it.
"""
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

log = logging.getLogger("homemind.ticktick")

API = "https://api.ticktick.com/open/v1"
AUTHORIZE = "https://ticktick.com/oauth/authorize"
TOKEN = "https://ticktick.com/oauth/token"
SCOPE = "tasks:read tasks:write"
REDIRECT = "http://localhost/"
PRIORITY = {5: "high", 3: "medium", 1: "low"}

TASK_ASK = re.compile(
    r"\b(tasks?|to-?dos?|to do list|due|overdue|deadlines?|homework|assignments?|ticktick|tick tick|"
    r"what (?:do|should) i (?:have|need) to do|what(?:'s| is) left|on my plate)\b", re.I)


def _fmt_day(d):
    return d.strftime("%A %b %d %Y").replace(" 0", " ")


def _fmt_time(d):
    return d.strftime("%I:%M %p").lstrip("0")


def parse_due(s):
    """TickTick's dueDate ('2026-11-27T05:00:00.000+0000') as local naive time, or None."""
    if not s:
        return None
    s = re.sub(r"\.\d+", "", str(s).strip())
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo:
        d = d.astimezone().replace(tzinfo=None)
    return d


def due_text(due, all_day):
    if not due:
        return ""
    return _fmt_day(due) if all_day else f"{_fmt_day(due)} at {_fmt_time(due)}"


def code_from(pasted):
    """The code (and state) from what was pasted: the whole redirect address, a query string, or the bare code."""
    p = (pasted or "").strip()
    if not p:
        return None, None
    if "code=" in p:
        q = urlparse(p).query if "://" in p else p.lstrip("?")
        qs = parse_qs(q)
        return (qs.get("code") or [None])[0], (qs.get("state") or [None])[0]
    if re.fullmatch(r"[A-Za-z0-9_\-]{4,200}", p):
        return p, None
    return None, None


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "list"


class TickTick:
    def __init__(self, store, opts, session_fn, path="/data/ticktick.json"):
        self.store, self.opts, self.session_fn, self.path = store, opts, session_fn, path
        self.last = None
        self._state = None

    # ---- connection ---------------------------------------------------------------------------------------------
    @property
    def configured(self):
        return bool(self.opts.get("ticktick_client_id") and self.opts.get("ticktick_client_secret"))

    @property
    def redirect(self):
        return (self.opts.get("ticktick_redirect") or REDIRECT).strip()

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self, d):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        os.replace(tmp, self.path)

    @property
    def token(self):
        t = self._load()
        if t.get("access_token") and (not t.get("expires") or t["expires"] > time.time()):
            return t["access_token"]
        return None

    @property
    def connected(self):
        return bool(self.configured and self.token)

    def days_left(self):
        t = self._load()
        if not t.get("expires"):
            return None
        return max(0, int((t["expires"] - time.time()) // 86400))

    def auth_url(self):
        if not self.configured:
            return None
        self._state = secrets.token_urlsafe(12)
        return AUTHORIZE + "?" + urlencode({"scope": SCOPE, "client_id": self.opts["ticktick_client_id"],
                                            "state": self._state, "redirect_uri": self.redirect,
                                            "response_type": "code"})

    async def connect(self, pasted):
        """Trade the code in what was pasted for a token."""
        import aiohttp
        if not self.configured:
            raise ValueError("set ticktick_client_id and ticktick_client_secret in the app's Configuration first")
        code, state = code_from(pasted)
        if not code:
            raise ValueError("paste the whole address TickTick sent you to (it has code=… in it)")
        if state and self._state and state != self._state:
            raise ValueError("that address is from an older Connect: press Connect again and paste the new one")
        auth = aiohttp.BasicAuth(self.opts["ticktick_client_id"], self.opts["ticktick_client_secret"])
        data = {"code": code, "grant_type": "authorization_code", "scope": SCOPE, "redirect_uri": self.redirect}
        async with self.session_fn().post(TOKEN, data=data, auth=auth, timeout=aiohttp.ClientTimeout(total=20)) as r:
            txt = await r.text()
            if r.status != 200:
                raise ValueError(f"TickTick said no ({r.status}): {txt[:200]}")
            j = json.loads(txt)
        if not j.get("access_token"):
            raise ValueError("TickTick sent no token back")
        self._save({"access_token": j["access_token"], "scope": j.get("scope"),
                    "expires": time.time() + float(j.get("expires_in") or 180 * 86400), "at": time.time()})
        self._state = None
        return self.status()

    def disconnect(self):
        """Forget the token and the tasks copied from TickTick (they stay in TickTick)."""
        try:
            os.remove(self.path)
        except OSError:
            pass
        for r in self.store.db.execute("SELECT id FROM items WHERE source='ticktick' AND status='active'").fetchall():
            self.store.set_status(r["id"], "archived")
        self.last = None
        return self.status()

    def status(self):
        return {"configured": self.configured, "connected": self.connected, "days_left": self.days_left(),
                "redirect": self.redirect, "last": self.last, "open": len(self.open_tasks()) if self.store else 0}

    async def _req(self, method, path, body=None):
        import aiohttp
        tok = self.token
        if not tok:
            raise RuntimeError("TickTick isn't connected")
        async with self.session_fn().request(method, API + path, json=body,
                                             headers={"Authorization": f"Bearer {tok}"},
                                             timeout=aiohttp.ClientTimeout(total=15)) as r:
            txt = await r.text()
            if r.status == 401:
                raise RuntimeError("TickTick turned the token down: connect again")
            if r.status >= 300:
                raise RuntimeError(f"TickTick {r.status}: {txt[:200]}")
            return json.loads(txt) if txt.strip() else {}

    # ---- sync ---------------------------------------------------------------------------------------------------
    def _text(self, project, t):
        due = parse_due(t.get("dueDate"))
        all_day = bool(t.get("isAllDay"))
        title = (t.get("title") or "(no title)").strip()
        s = f"Task (TickTick, {project}): {title}"
        if due:
            s += f", due {due_text(due, all_day)}"
        pr = PRIORITY.get(t.get("priority") or 0)
        if pr:
            s += f", {pr} priority"
        items = [i for i in (t.get("items") or []) if (i.get("status") or 0) == 0 and i.get("title")]
        if items:
            s += ". Steps left: " + "; ".join(i["title"].strip() for i in items[:8])
        body = re.sub(r"\s+", " ", (t.get("content") or t.get("desc") or "")).strip()
        if body:
            s += f". {body[:300]}"
        return title, due, all_day, s + ("" if s.endswith(".") else ".")

    async def sync(self):
        if not self.connected:
            return {"skipped": "not connected"}
        projects = await self._req("GET", "/project")
        lists = [("inbox", "Inbox")] + [(p["id"], p.get("name") or "List") for p in (projects or [])
                                       if not p.get("closed") and p.get("id")]
        existing = {}
        for r in self.store.db.execute("SELECT id, body, meta FROM items WHERE source='ticktick' AND status='active'"):
            m = json.loads(r["meta"] or "{}")
            if m.get("tt"):
                existing[m["tt"]] = (r["id"], r["body"], m.get("project_id"))
        seen, ok, added, changed = set(), set(), 0, 0
        for pid, pname in lists:
            try:
                data = await self._req("GET", f"/project/{pid}/data")
            except Exception as e:  # noqa: BLE001
                log.warning("list %s: %s", pname, e)
                continue
            ok.add(pid)
            for t in data.get("tasks") or []:
                if (t.get("status") or 0) != 0 or not t.get("id"):
                    continue
                seen.add(t["id"])
                try:
                    title, due, all_day, text = self._text(pname, t)
                    meta = {"tt": t["id"], "project_id": t.get("projectId") or pid, "project": pname,
                            "due": due.isoformat() if due else None, "all_day": all_day,
                            "priority": t.get("priority") or 0, "summary": title}
                    if t["id"] in existing:
                        iid, body, _ = existing[t["id"]]
                        if body != text:
                            self.store.revise(iid, body=text, title=title[:120], meta=meta,
                                              shelf=f"tasks/ticktick/{_slug(pname)}")
                            changed += 1
                        continue
                    self.store.remember(text=text, title=title[:120], kind="note",
                                        shelf=f"tasks/ticktick/{_slug(pname)}", tags=["task", "ticktick"],
                                        source="ticktick", meta=meta)
                    added += 1
                except Exception:  # noqa: BLE001
                    log.exception("task %s", t.get("title"))
        done = 0
        for tid, (iid, _, pid) in existing.items():
            if tid in seen:
                continue
            if pid in ok or (pid and pid.startswith("inbox") and "inbox" in ok):
                self.store.set_status(iid, "archived")
                done += 1
        self.last = {"at": datetime.now().isoformat(timespec="seconds"), "lists": len(ok), "added": added,
                     "changed": changed, "done": done, "open": len(seen)}
        return self.last

    # ---- reading ------------------------------------------------------------------------------------------------
    def open_tasks(self):
        from .speakers import visible
        out = []
        for r in self.store.db.execute("SELECT * FROM items WHERE source='ticktick' AND status='active'"):
            d = self.store._row(r)
            if visible(d["meta"]):
                out.append(d)

        def key(d):
            m = d["meta"]
            due = m.get("due")
            return (0 if due else 1, due or "", -(m.get("priority") or 0), d["title"].lower())
        out.sort(key=key)
        return out

    @staticmethod
    def _due(d):
        s = d["meta"].get("due")
        return datetime.fromisoformat(s) if s else None

    def _line(self, d, today):
        m = d["meta"]
        due = self._due(d)
        s = f"- {m.get('summary') or d['title']} ({m.get('project') or 'TickTick'})"
        if due:
            s += f", due {due_text(due, m.get('all_day'))}"
            if due.date() < today:
                s += " (overdue)"
        pr = PRIORITY.get(m.get("priority") or 0)
        if pr:
            s += f", {pr} priority"
        return s

    def lines_for(self, question, today=None):
        if not self.store or not TASK_ASK.search(question or ""):
            return ""
        tasks = self.open_tasks()
        if not tasks:
            return ""
        from .calendars import asked_days
        today = today or datetime.now().date()
        days = asked_days(question, today)
        dated = [d for d in tasks if self._due(d)]
        undated = [d for d in tasks if not self._due(d)]
        overdue = [d for d in dated if self._due(d).date() < today]
        if days:
            want = set(days)
            pick = [d for d in dated if self._due(d).date() in want]
            if today in want:
                pick = overdue + [d for d in pick if d not in overdue]
            span = ", ".join(_fmt_day(datetime.combine(x, datetime.min.time())) for x in days[:3]) + \
                (" …" if len(days) > 3 else "")
            head = f"TASKS due {span} (from TickTick, synced every 15 minutes; open tasks only):"
            body = [self._line(d, today) for d in pick[:15]] or ["- no open tasks due then"]
        else:
            week = [d for d in dated if today <= self._due(d).date() <= today + timedelta(days=7)]
            later = [d for d in dated if self._due(d).date() > today + timedelta(days=7)]
            head = "TASKS (from TickTick, synced every 15 minutes; open tasks only, overdue first):"
            body = [self._line(d, today) for d in (overdue + week)[:15]]
            room = max(0, 12 - len(body))
            body += [self._line(d, today) for d in (later + undated)[:room]]
            rest = len(tasks) - len(body)
            if rest > 0:
                body.append(f"- and {rest} more open tasks")
        return head + "\n" + "\n".join(body)

    def brief_lines(self, today=None):
        """For the morning summary: overdue and due today, and a warning when the token is running out."""
        out = []
        if not self.connected:
            if self.configured and self._load().get("access_token"):
                out.append("TickTick: the connection ran out; connect again in Home Mind (System > Overview)")
            return out
        today = today or datetime.now().date()
        tasks = self.open_tasks()
        over = [d for d in tasks if self._due(d) and self._due(d).date() < today]
        now = [d for d in tasks if self._due(d) and self._due(d).date() == today]

        def nm(d):
            return d["meta"].get("summary") or d["title"]
        if over:
            out.append(f"Overdue tasks {len(over)}: " + "; ".join(nm(d) for d in over[:4]) + (" …" if len(over) > 4 else ""))
        if now:
            out.append("Tasks due today: " + "; ".join(nm(d) for d in now[:5]) + (" …" if len(now) > 5 else ""))
        left = self.days_left()
        if left is not None and left <= 14:
            out.append(f"TickTick connection runs out in {left} days; connect again in Home Mind (System > Overview)")
        return out

    # ---- writing ------------------------------------------------------------------------------------------------
    async def add(self, title, day=None, hm=None):
        """A new task in the inbox. day: a date; hm: (hour, minute). Returns the task TickTick made."""
        body = {"title": title[:200]}
        tzname = os.environ.get("TZ") or "UTC"
        if day or hm:
            d = day or datetime.now().date()
            if hm:
                local = datetime(d.year, d.month, d.day, hm[0], hm[1]).astimezone()
                body.update(isAllDay=False)
            else:
                local = datetime(d.year, d.month, d.day).astimezone()
                body.update(isAllDay=True)
            body["dueDate"] = local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
            body["startDate"] = body["dueDate"]
            body["timeZone"] = tzname
        t = await self._req("POST", "/task", body)
        try:
            await self.sync()
        except Exception:  # noqa: BLE001
            log.exception("sync after add")
        return t

    def find(self, words):
        """Open tasks whose title matches the words: an exact title wins, then titles holding them."""
        w = re.sub(r"\s+", " ", (words or "").lower()).strip(" .!?\"'")
        if not w:
            return []
        tasks = self.open_tasks()
        exact = [d for d in tasks if (d["meta"].get("summary") or d["title"]).lower().strip() == w]
        if exact:
            return exact
        return [d for d in tasks if w in (d["meta"].get("summary") or d["title"]).lower()]

    async def complete(self, d):
        m = d["meta"]
        await self._req("POST", f"/project/{m['project_id']}/task/{m['tt']}/complete")
        self.store.set_status(d["id"], "archived")
        return m.get("summary") or d["title"]

