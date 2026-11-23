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

# 11. Daily cooking instructions (Pantry.meal_steps) — LLM, caching, force refresh, and fallback
class StepsModel:
    def __init__(self):
        self.calls = 0

    async def chat(self, sys_p, msgs, max_tokens=900, temperature=0.2):
        self.calls += 1
        return {"text": '{"steps": {"breakfast": ["Whisk 3 eggs with spinach.", "Scramble in butter 3 min."], "dinner": ["Preheat pan to medium-high.", "Sear chicken 5 min per side.", "Serve over warm rice."]}}'}


class StepsModels:
    def __init__(self, m=None):
        self.models = {"haiku": m} if m else {}

    def get(self, name):
        return self.models[name]


st_ms = Store(tempfile.mktemp())
st_ms.kv_set(Pantry.WEEK_KV, {
    "days": [{
        "day": "Mon",
        "date": "2026-11-22",
        "meals": [
            {"slot": "breakfast", "name": "Spinach Scramble", "effort": "quick", "minutes": 8, "uses": ["eggs", "spinach"]},
            {"slot": "lunch", "name": "Leftover Chili", "effort": "reheat", "is_cook": False, "minutes": 4, "uses": ["chili"]},
            {"slot": "dinner", "name": "Chicken Rice Bowl", "effort": "normal", "minutes": 25, "uses": ["chicken", "rice"]},
        ],
    }],
})
sm = StepsModel()
pan_ms = Pantry(st_ms, None, StepsModels(sm), tempfile.mkdtemp())
res1 = asyncio.run(pan_ms.meal_steps(day="2026-11-22"))
by_sl = {m["slot"]: m.get("steps") for m in res1["meals"]}
check("meal_steps uses LLM output and deterministic reheat fallback",
      sm.calls == 1 and len(by_sl["breakfast"]) == 2 and len(by_sl["dinner"]) == 3
      and any("Reheat" in s or "reheat" in s for s in by_sl["lunch"]), by_sl)
res2 = asyncio.run(pan_ms.meal_steps(day="2026-11-22"))
check("meal_steps caches instructions onto week_plan without redundant LLM call", sm.calls == 1 and res2["meals"][0]["steps"] == by_sl["breakfast"])
res3 = asyncio.run(pan_ms.meal_steps(day_index=0, slot="dinner", force=True))
check("meal_steps force=True regenerates requested slot", sm.calls == 2 and len(res3["meals"]) == 1 and res3["meals"][0]["slot"] == "dinner")

# Fallback when no non-mock model is configured
st_ms2 = Store(tempfile.mktemp())
st_ms2.kv_set(Pantry.WEEK_KV, {
    "days": [{"day": "Tue", "date": "2026-11-23", "meals": [{"slot": "dinner", "name": "Salmon Tacos", "effort": "quick", "minutes": 15, "uses": ["salmon", "tortillas"]}]}],
})
pan_fb = Pantry(st_ms2, None, StepsModels(None), tempfile.mkdtemp())
res_fb = asyncio.run(pan_fb.meal_steps(day_index=0))
check("meal_steps produces deterministic fallback steps without LLM",
      len(res_fb["meals"][0].get("steps") or []) >= 3, res_fb["meals"])

# 12. Reversible cloud masking in overnight.extract preserves real secret locally without leaking to cloud
class EchoCloudModel:
    type = "anthropic"
    seen = None

    async def chat(self, sys_p, msgs, max_tokens=900, temperature=0):
        self.seen = msgs[0]["content"]
        # Echo back the masked placeholder exactly as a cloud model would
        m_sec = self.seen.split("code is ")[-1].split(".")[0]
        return {"text": json.dumps({"facts": [{"text": f"The locker code is {m_sec}.", "turn": 1}]})}


ecm = EchoCloudModel()
ex_facts = asyncio.run(extract(ecm, [{"who": "user", "text": "The locker code is 5150.", "ts": NOW}]))
check("cloud extract masks secret in prompt but restores it locally on returned fact",
      "5150" not in (ecm.seen or "") and ex_facts and "5150" in ex_facts[0]["text"], (ecm.seen, ex_facts))

# 13. MedsHabits toggle deduplicates body/meds note and archives it when unchecked
from homemind.meds import MedsHabits
st_mh = Store(tempfile.mktemp())
mh = MedsHabits(st_mh)
med_item = mh.add("Vitamin D3", kind="med", dose="2000 IU")
mh.toggle(med_item["id"], day="2026-11-22", done=True)
mh.toggle(med_item["id"], day="2026-11-22", done=True)
active_meds = st_mh.db.execute("SELECT id FROM items WHERE shelf='body/meds' AND status='active'").fetchall()
check("toggling a med ON twice does not create duplicate body/meds notes", len(active_meds) == 1, len(active_meds))
mh.toggle(med_item["id"], day="2026-11-22", done=False)
active_after_off = st_mh.db.execute("SELECT id FROM items WHERE shelf='body/meds' AND status='active'").fetchall()
check("unchecking a med archives its body/meds note", len(active_after_off) == 0, len(active_after_off))

# 14. Maintenance rejects non-finite every_days with ValueError (not OverflowError)
from homemind.maintenance import Maintenance
st_mt = Store(tempfile.mktemp())
mt = Maintenance(st_mt)
try:
    mt.add("Furnace filter", every_days="inf")
    check("maintenance rejects every_days='inf' cleanly", False, "no error raised")
except ValueError:
    check("maintenance rejects every_days='inf' cleanly", True)

# 15. Recipes add_to_shopping uses word boundaries and save() clears owner on household scope
from homemind.recipes import Recipes
st_rc = Store(tempfile.mktemp())
pan_rc = Pantry(st_rc, None, None, tempfile.mkdtemp())
pan_rc.add("salt", location="pantry")
pan_rc.add("oil", location="pantry")
rc = Recipes(st_rc, pantry=pan_rc)
tok_a = VIEWER.set("Alice")
try:
    saved_r = rc.save({"name": "Eggs on Toast", "ingredients": ["unsalted butter", "boiled eggs", "kosher salt"], "scope": "personal"})
    updated_r = rc.save({"id": saved_r["id"], "name": "Eggs on Toast", "ingredients": ["unsalted butter", "boiled eggs", "kosher salt"], "scope": "household"})
finally:
    VIEWER.reset(tok_a)
check("switching recipe from personal to household clears owner", updated_r["scope"] == "household" and updated_r["owner"] is None, updated_r)
shop_res = asyncio.run(rc.add_to_shopping(updated_r["id"]))
check("recipe +Shop matches 'kosher salt' against 'salt' without skipping 'unsalted butter' or 'boiled eggs'",
      "kosher salt" in shop_res["skipped"] and "unsalted butter" not in shop_res["skipped"] and "boiled eggs" not in shop_res["skipped"], shop_res)

# 16. Fitness.log() defaults scope=personal & owner=VIEWER when called without explicit meta
st_fl = Store(tempfile.mktemp())
fit_fl = Fitness(st_fl)
tok_a2 = VIEWER.set("Alice")
try:
    fit_fl.log("bench press 3x8 at 135 lb")
finally:
    VIEWER.reset(tok_a2)
tok_b2 = VIEWER.set("Bob")
try:
    bob_ws = fit_fl.workouts(30)
finally:
    VIEWER.reset(tok_b2)
check("Fitness.log() defaults personal owner from VIEWER so Bob cannot see Alice's workout", bob_ws == [], bob_ws)

# 17. check_types supports bool fields (fixes KeyError: <class 'bool'> on meal_steps, habits/toggle, fitness/prefs, meal_edit)
from homemind.main import check_types
check("check_types accepts bool True/False", check_types({"force": False, "reestimate": True}, {"force": bool, "reestimate": bool}) == {"force": False, "reestimate": True})
try:
    check_types({"force": "yes"}, {"force": bool})
    check("check_types rejects non-bool with clean ValueError", False, "no error")
except ValueError as e:
    check("check_types rejects non-bool with clean ValueError", "true or false" in str(e), str(e))
try:
    check_types({"force": 1}, {"force": bool})
    check("check_types rejects int 1 for bool field", False, "no error")
except ValueError as e:
    check("check_types rejects int 1 for bool field", "true or false" in str(e), str(e))

# 18. Pantry partial consumption (use_part + Ask natural language)
st_pp = Store(tempfile.mktemp())
pan_pp = Pantry(st_pp, None, None, tempfile.mkdtemp())
a_milk = pan_pp.add("milk", "", location="fridge")
r_m1 = pan_pp.use_part(item_id=a_milk["id"], fraction=0.5)
check("use_part half on unquantified item -> '1/2 left'", r_m1["partial"] and r_m1["amount"] == "1/2 left", r_m1)
r_m2 = pan_pp.use_part(item_id=a_milk["id"], fraction=0.5)
check("use_part half again -> '1/4 left'", r_m2["partial"] and r_m2["amount"] == "1/4 left", r_m2)

a_beef = pan_pp.add("ground beef", "2 lb", location="fridge")
r_b1 = pan_pp.use_part(item_id=a_beef["id"], used="8 oz")
check("use_part subtracts 8 oz from 2 lb -> '1.5 lb'", r_b1["partial"] and r_b1["amount"] == "1.5 lb", r_b1)

a_eggs = pan_pp.add("eggs", "a dozen", location="fridge")
r_e1 = pan_pp.use_part(item_id=a_eggs["id"], used="3")
check("use_part subtracts 3 from a dozen -> '9'", r_e1["partial"] and r_e1["amount"] == "9", r_e1)
r_e2 = pan_pp.use_part(item_id=a_eggs["id"], remaining="0")
check("use_part remaining='0' marks item used", (not r_e2["partial"]) and r_e2["status"] == "used", r_e2)

from homemind import ask_actions
a_chk = pan_pp.add("chicken breast", "2 lb", location="fridge")
fake_app = types.SimpleNamespace(pantry=pan_pp)
ask_r = asyncio.run(ask_actions.dispatch(fake_app, {"question": "I only used half of the chicken breast"}, None))
check("Ask action handles 'I only used half of the chicken breast'",
      ask_r and ask_r.get("pantry_part", {}).get("amount") == "1 lb", ask_r)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)



