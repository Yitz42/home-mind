"""Regression tests for data ingestion, privacy/scoping leaks, and processing integrity hardening."""
import asyncio
import json
import os
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "home_mind", "rootfs", "app"))

from homemind.store import Store  # noqa: E402
from homemind.speakers import VIEWER  # noqa: E402
from homemind.checkins import Checkins  # noqa: E402
from homemind.pantry import Pantry  # noqa: E402
from homemind.fitness import Fitness  # noqa: E402
from homemind.calendars import Calendars  # noqa: E402
from homemind.body import Body, _num as body_num  # noqa: E402
from homemind.home import _f as home_f  # noqa: E402
from homemind.overnight import extract, Overnight  # noqa: E402
from homemind import sqlview  # noqa: E402
from homemind.mdsync import MdSync  # noqa: E402
from homemind.mailbox import Mailbox  # noqa: E402
from homemind.messaging import Telegram  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def as_viewer(who, fn):
    tok = VIEWER.set(who)
    try:
        return fn()
    finally:
        VIEWER.reset(tok)


NOW = datetime(2026, 11, 22, 14, 0).timestamp()

# 1. Overnight cloud extract masks secrets & check_links never leaks personal titles into household notes
st = Store(tempfile.mktemp(), clock=lambda: NOW)


class CloudModel:
    type = "anthropic"
    cfg = {}

    def __init__(self, reply='{"facts": []}'):
        self.sent = []
        self.reply = reply

    async def chat(self, system, messages, **k):
        self.sent.append(messages[-1]["content"])
        return {"text": self.reply, "ms": 1}


cm = CloudModel()
asyncio.run(extract(cm, [{"who": "user", "text": "My alarm PIN is 8492 and my IP is 192.168.1.50.", "ts": NOW}]))
check("overnight extract masks PIN and IP before sending to cloud model",
      cm.sent and "8492" not in cm.sent[0] and "192.168.1.50" not in cm.sent[0], cm.sent)

# Cross-person pronoun link leak check
st.remember(text="Alice secret medical note about migraines", title="Alice Migraines",
            meta={"scope": "personal", "owner": "Alice"}, at=NOW - 300)
st.remember(text="Shared garage note about the red bike", title="Garage Bike", at=NOW - 200)
hh_ref = st.remember(text="Put it in the corner", title="Put it [re: Garage Bike]",
                     meta={"refers_to": 2}, at=NOW - 100)
cm_link = CloudModel(reply="1")
ov = Overnight(st, types.SimpleNamespace(models={"c": cm_link}, get=lambda n: cm_link), None)
asyncio.run(ov.check_links(cm_link))
check("check_links never exposes Alice's personal note to a household note's linking prompt",
      all("Migraines" not in s for s in cm_link.sent), cm_link.sent)

# 2. Check-ins & Coffee personal scoping + NaN rejection
ck = Checkins(st, clock=lambda: NOW)
a_ck = as_viewer("Alice", lambda: ck.add(fog=4, energy="low", note="Private headache"))
a_ck_row = st.get(a_ck["id"])
check("check-in sets scope=personal and owner=Alice",
      a_ck_row["meta"].get("scope") == "personal" and a_ck_row["meta"].get("owner") == "Alice", a_ck_row["meta"])
check("Bob cannot see Alice's check-in", as_viewer("Bob", lambda: ck.entries()) == [])
check("Household/guest view cannot see Alice's check-in", as_viewer("", lambda: ck.entries()) == [])
check("Alice sees her own check-in", len(as_viewer("Alice", lambda: ck.entries())) == 1)

as_viewer("Alice", lambda: ck.log("coffee", 1))
check("Bob cannot see Alice's coffee count", as_viewer("Bob", lambda: ck.coffees()) == 0)
as_viewer("Bob", lambda: ck.log("coffee", -1))
check("Bob taking coffee back cannot archive Alice's coffee", as_viewer("Alice", lambda: ck.coffees()) == 1)

nan_rejected = False
try:
    ck.add(fog=2, at=float("nan"))
except ValueError:
    nan_rejected = True
check("check-in rejects NaN timestamp", nan_rejected)

# 3. Pantry personal meal scoping + estimate() vs edit_meal() race guard
class SlowMealModel:
    type = "mock"
    cfg = {}

    async def chat(self, system, messages, **k):
        return {"text": '{"protein_g": 20, "calories": 400, "carbs_g": 40, "fat_g": 12}'}


sm = SlowMealModel()
models = types.SimpleNamespace(models={"haiku": sm}, get=lambda n: sm)
pan = Pantry(st, None, models, tempfile.mkdtemp())
a_meal = as_viewer("Alice", lambda: pan.ate("Greek yogurt bowl", when=NOW))
a_meal_row = st.get(a_meal["id"])
check("pantry.ate sets scope=personal and owner=Alice",
      a_meal_row["meta"].get("scope") == "personal" and a_meal_row["meta"].get("owner") == "Alice")
check("Bob's meals_on hides Alice's meal", len(as_viewer("Bob", lambda: pan.meals_on(now=NOW)["meals"])) == 0)

# Manual edit before background estimate completes must not be clobbered!
as_viewer("Alice", lambda: pan.edit_meal(a_meal["id"], calories=555, protein_g=42))
est_res = asyncio.run(pan.estimate(a_meal["id"], "Greek yogurt bowl"))
after_edit = as_viewer("Alice", lambda: pan.get_meal(a_meal["id"]))
check("background estimate does not clobber manual user macro edits",
      est_res is None and after_edit["calories"] == 555 and after_edit["protein_g"] == 42, after_edit)

# 4. Fitness weigh-in personal scoping
fit = Fitness(st)
as_viewer("Alice", lambda: fit.log_weigh_in(135.0, now=NOW))
check("Bob's weigh_ins hides Alice's personal weigh-in", len(as_viewer("Bob", lambda: fit.weigh_ins(now=NOW))) == 0)
check("Bob's latest_body hides Alice's personal weigh-in", as_viewer("Bob", lambda: fit.latest_body(now=NOW)) == {})
check("Alice sees her own weigh-in", len(as_viewer("Alice", lambda: fit.weigh_ins(now=NOW))) == 1)

# 5. sqlview compact JSON sensitive masking & Unicode owner matching
st_sql = Store(os.path.join(tempfile.mkdtemp(), "sql.db"))
with st_sql.lock:
    st_sql.db.execute(
        "INSERT INTO items(kind,title,body,tags,shelf,status,created,updated,strength,source,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("fact", "Compact Secret", "Door code 9999", "[]", "inbox", "active", NOW, NOW, 1.0, "test", '{"sensitive":true}'),
    )
    st_sql.db.commit()
zoe_note = st_sql.remember(text="Zoë's diary entry", title="Diary", meta={"scope": "personal", "owner": "Zoë"})
sql_sec = sqlview.query(st_sql.path, "SELECT title, body FROM notes WHERE id=1", viewer=None)["rows"]
check("sqlview masks compact JSON sensitive=true notes", sql_sec == [["[secret]", "[secret]"]], sql_sec)
sql_zoe = sqlview.query(st_sql.path, "SELECT body FROM notes WHERE id=?", (zoe_note["id"],), viewer="Zoë")["rows"]
sql_bob = sqlview.query(st_sql.path, "SELECT body FROM notes WHERE id=?", (zoe_note["id"],), viewer="Bob")["rows"]
check("sqlview matches Unicode owner Zoë and hides from Bob", sql_zoe == [["Zoë's diary entry"]] and sql_bob == [])

# 6. Calendars: partial HA failure does NOT wipe future events, and events_on respects visibility
class FlakyHA:
    def __init__(self, fail_cal=False):
        self.fail_cal = fail_cal

    async def states(self):
        return [{"entity_id": "calendar.work", "attributes": {"friendly_name": "Work"}}]

    async def get_json(self, path, params=None):
        if self.fail_cal:
            return {"error": "timeout"}
        tom = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        return [{"summary": "Dentist", "start": {"dateTime": f"{tom}T10:00:00"}, "end": {"dateTime": f"{tom}T11:00:00"}}]


st_cal = Store(tempfile.mktemp())
ha_cal = FlakyHA(fail_cal=False)
cal = Calendars(st_cal, ha_cal)
r_ok = asyncio.run(cal.sync())
ha_cal.fail_cal = True
r_fail = asyncio.run(cal.sync())
tom_date = (datetime.now() + timedelta(days=1)).date()
check("calendar sync failure does not archive existing future events",
      r_ok["added"] == 1 and r_fail["cancelled"] == 0 and len(cal.events_on(tom_date)) == 1, (r_ok, r_fail))

# 7. body._num & home._f reject NaN/inf/non-positive; body.backfill handles None vs str units
check("body._num rejects NaN, inf, zero, negative",
      body_num("nan", "lb") is None and body_num("inf", "lb") is None and body_num(0, "lb") is None and body_num(-5, "lb") is None)
check("home._f rejects NaN and inf", home_f("nan") is None and home_f("inf") is None and home_f("72.5") == 72.5)


class TieHA:
    async def history(self, ids, start):
        t0 = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc).isoformat()
        return [
            [{"entity_id": "sensor.yitz_last_weigh_in", "state": t0, "last_changed": t0, "attributes": {}}],
            [{"entity_id": "sensor.yitz_weight", "state": "175.0", "last_changed": t0, "attributes": {"unit_of_measurement": None}},
             {"entity_id": "sensor.yitz_weight", "state": "175.2", "last_changed": t0, "attributes": {"unit_of_measurement": "lb"}}],
        ]


st_b = Store(tempfile.mktemp())
n_bf = asyncio.run(Body(st_b, TieHA()).backfill())
check("body.backfill does not crash on same-timestamp None vs str units", n_bf == 1)

# 8. Mailbox does NOT mark rate-limited emails as Seen on IMAP
class FakeLimiter:
    def allow(self, kind, key):
        return False


class FakeIMAP:
    def __init__(self, host):
        self.stored = []

    def login(self, u, p): pass
    def select(self, f): pass
    def search(self, *a): return ("OK", [b"1"])
    def fetch(self, num, what):
        raw = b"From: alice@example.com\r\nSubject: Receipt\r\nMessage-ID: <m1@x>\r\n\r\nBought filter"
        return ("OK", [(b"1", raw)])
    def store(self, num, op, flags): self.stored.append((num, op, flags))
    def logout(self): pass


fake_imap = FakeIMAP("imap.test")
mb = Mailbox(types.SimpleNamespace(mind=types.SimpleNamespace(store=st), limits=FakeLimiter()),
             {"email_host": "imap.test", "email_user": "u", "email_password": "p",
              "email_senders": ["alice@example.com"], "email_require_verified": False},
             imap_factory=lambda h: fake_imap)
mb_res = mb.run()
check("rate-limited email is deferred and NOT marked \\Seen on IMAP",
      mb_res["skipped"] == 1 and fake_imap.stored == [], (mb_res, fake_imap.stored))

# 9. MdSync skips unflagged secret text
st_md = Store(tempfile.mktemp())
with st_md.lock:
    st_md.db.execute(
        "INSERT INTO items(kind,title,body,tags,shelf,status,created,updated,strength,source,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("fact", "WiFi", "Guest wifi password is sunflower88", "[]", "house", "active", NOW, NOW, 1.0, "test", "{}"),
    )
    st_md.db.commit()
md_dir = tempfile.mkdtemp()
MdSync(st_md, md_dir).run()
written_files = [os.path.join(r, f) for r, _, fs in os.walk(md_dir) for f in fs]
check("mdsync skips unflagged notes matching privacy.is_sensitive", written_files == [], written_files)

# 10. Telegram persists offset across restarts
st_tg = Store(tempfile.mktemp())
st_tg.kv_set("telegram_offset", 4242)
tg = Telegram(types.SimpleNamespace(mind=types.SimpleNamespace(store=st_tg)), "tok", [])
check("Telegram loads persisted offset from store.kv on startup", tg.offset == 4242)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
