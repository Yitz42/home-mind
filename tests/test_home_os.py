"""Home OS complete capabilities tests:
1. Saved Recipes + Barcode/UPC quick-log + Pantry-aware shopping list
2. Medications, Supplements & Daily Habit streaks + body/meds correlation
3. Home Maintenance, Warranties & Recurring Chores + linked docs + hub For You surfacing
4. Multi-Person Household Viewer Switcher (admin-only X-Home-Mind-Viewer header)
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "home_mind", "rootfs", "app"))

from homemind.store import Store  # noqa: E402
from homemind.recipes import Recipes  # noqa: E402
from homemind.meds import MedsHabits  # noqa: E402
from homemind.maintenance import Maintenance  # noqa: E402
from homemind.speakers import VIEWER  # noqa: E402
from homemind import hub as HUB  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


NOW = datetime(2026, 11, 22, 12, 0).timestamp()
DAY = 86400


class FakeKnowledge:
    def __init__(self, items):
        self.items = items

    def by_upc(self, code):
        return self.items.get(code)


class FakePantry:
    def __init__(self, stock):
        self._stock = stock
        self.added = []

    def items(self, status="in"):
        return [{"name": n} for n in self._stock]

    async def add_to_shopping(self, names):
        self.added.extend(names)
        return list(names)

    def meal_day_time(self, day, when):
        return NOW


class FakeDocs:
    def __init__(self, names):
        self._names = names

    def list(self):
        return [{"name": n} for n in self._names]


# ---------------- 1. Recipes & Barcode ----------------
st = Store(tempfile.mktemp())
fk = FakeKnowledge({
    "012345678905": {
        "name": "Greek Yogurt", "brand": "Fage",
        "serving": {"text": "170 g", "g": 170, "kcal": 90, "protein": 18, "carbs": 5, "fat": 0, "fiber": 0, "sugar": 5},
    }
})
fp = FakePantry(["chicken breast", "olive oil"])
rec = Recipes(st, pantry=fp, knowledge=fk)

r1 = rec.save({
    "name": "Chicken Rice Bowl", "slot": "lunch", "minutes": 20,
    "calories": 620, "protein_g": 52, "carbs_g": 65, "fat_g": 14,
    "ingredients": ["chicken breast", "jasmine rice", "broccoli", "olive oil"],
    "steps": "Cook rice, sear chicken, steam broccoli.",
})
check("recipe saved with macros and ingredients", r1["id"] > 0 and r1["calories"] == 620 and len(r1["ingredients"]) == 4)

r1_upd = rec.save({"id": r1["id"], "name": "Chicken Rice Bowl", "slot": "dinner", "calories": 650, "protein_g": 55,
                   "ingredients": r1["ingredients"]})
check("recipe updated in place", r1_upd["id"] == r1["id"] and r1_upd["slot"] == "dinner" and r1_upd["calories"] == 650)

rec.save({"name": "Overnight Oats", "slot": "breakfast", "calories": 380, "protein_g": 24, "ingredients": ["oats", "milk"]})
check("recipe search by ingredient works", [x["name"] for x in rec.list(q="broccoli")] == ["Chicken Rice Bowl"])
check("recipe filter by slot works", [x["name"] for x in rec.list(slot="breakfast")] == ["Overnight Oats"])

bc = rec.lookup_barcode("0123-4567-8905")
check("barcode lookup strips dashes and returns serving macros",
      bc["upc"] == "012345678905" and bc["match"]["name"] == "Greek Yogurt (Fage)" and bc["match"]["protein_g"] == 18)
check("barcode lookup unknown returns match=None", rec.lookup_barcode("99999999")["match"] is None)
try:
    rec.lookup_barcode("123")
    check("short barcode raises ValueError", False)
except ValueError:
    check("short barcode raises ValueError", True)

logged = rec.log_recipe(r1["id"], servings=1.5)
check("log_recipe scales macros exactly", logged["calories"] == 975 and logged["protein_g"] == 82)
meal_row = st.get(logged["id"])
check("log_recipe saves exact non-estimated note on body/meals",
      meal_row["shelf"] == "body/meals" and meal_row["meta"].get("estimated") is False and meal_row["meta"].get("recipe_id") == r1["id"])

shop_res = asyncio.run(rec.add_to_shopping(r1["id"]))
check("recipe +Shop subtracts in-stock pantry items",
      shop_res["added"] == ["jasmine rice", "broccoli"] and shop_res["skipped"] == ["chicken breast", "olive oil"], shop_res)

# Personal recipe scope isolation
tok_a = VIEWER.set("Alice")
try:
    ra = rec.save({"name": "Alice Shake", "scope": "personal", "calories": 300, "protein_g": 30})
    check("Alice sees her personal recipe", any(x["id"] == ra["id"] for x in rec.list()))
finally:
    VIEWER.reset(tok_a)

tok_b = VIEWER.set("Bob")
try:
    check("Bob does not see Alice's personal recipe", all(x["id"] != ra["id"] for x in rec.list()))
finally:
    VIEWER.reset(tok_b)

del_r = rec.delete(r1["id"])
check("recipe delete removes recipe", del_r["removed"] == r1["id"] and all(x["id"] != r1["id"] for x in rec.list()))

# ---------------- 2. Meds, Supplements & Habits ----------------
mh = MedsHabits(st)
m1 = mh.add("Vitamin D3", kind="med", dose="2000 IU", schedule="morning")
h1 = mh.add("Stretch 10m", kind="habit", dose="10 min", schedule="evening")
check("med and habit created", m1["id"] > 0 and h1["id"] > 0 and m1["kind"] == "med" and h1["kind"] == "habit")

d0 = datetime.fromtimestamp(NOW).date()
d_prev2 = (d0 - timedelta(days=2)).isoformat()
d_prev1 = (d0 - timedelta(days=1)).isoformat()
d_today = d0.isoformat()

mh.toggle(m1["id"], day=d_prev2, done=True, now=NOW - 2 * DAY)
mh.toggle(m1["id"], day=d_prev1, done=True, now=NOW - DAY)
t_today = mh.toggle(m1["id"], day=d_today, done=True, now=NOW)
check("3-day consecutive streak computed", t_today["done"] is True and t_today["streak"] == 3, t_today)

med_notes = st.db.execute("SELECT * FROM items WHERE shelf='body/meds'").fetchall()
check("checking off a med logs to body/meds", len(med_notes) >= 1 and "Vitamin D3" in med_notes[-1]["title"])

before_med_cnt = len(med_notes)
mh.toggle(h1["id"], day=d_today, done=True, now=NOW)
after_med_cnt = st.db.execute("SELECT COUNT(*) FROM items WHERE shelf='body/meds'").fetchone()[0]
check("checking off a habit does not write to body/meds", after_med_cnt == before_med_cnt)

dash = mh.dashboard(day=d_today, now=NOW)
check("dashboard summary counts meds and habits",
      dash["summary"] == {"total": 2, "done": 2, "meds_total": 1, "meds_done": 1, "habits_total": 1, "habits_done": 1},
      dash["summary"])
m1_dash = [x for x in dash["items"] if x["id"] == m1["id"]][0]
check("7-day completion array reflects last 3 days done", m1_dash["week"] == [False, False, False, False, True, True, True], m1_dash["week"])

t_off = mh.toggle(m1["id"], day=d_today, now=NOW)
check("toggling off unchecks today while preserving streak through yesterday", t_off["done"] is False and t_off["streak"] == 2, t_off)

mh.delete(h1["id"])
check("deleted habit disappears from dashboard", len(mh.dashboard(day=d_today, now=NOW)["items"]) == 1)

# ---------------- 3. Home Maintenance & Warranties ----------------
docs = FakeDocs(["furnace_manual.pdf", "fridge_warranty.pdf"])
mt = Maintenance(st, docs=docs)

over_due = (d0 - timedelta(days=5)).isoformat()
soon_due = (d0 + timedelta(days=7)).isoformat()
ok_due = (d0 + timedelta(days=60)).isoformat()
war_date = (d0 + timedelta(days=365)).isoformat()

u1 = mt.add("Furnace filter", category="hvac", every_days=90, next_due=over_due,
            warranty_until=war_date, doc_name="furnace_manual.pdf", notes="16x25x1 MERV 11", now=NOW)
u2 = mt.add("Water filter", category="appliance", every_days=180, next_due=soon_due, now=NOW)
u3 = mt.add("Car oil change", category="auto", every_days=180, next_due=ok_due, now=NOW)

ml = mt.list(now=NOW)
check("maintenance sorts overdue -> soon -> ok and lists linked docs",
      [x["state"] for x in ml["items"]] == ["overdue", "soon", "ok"]
      and ml["overdue"] == 1 and ml["soon"] == 1 and "furnace_manual.pdf" in ml["docs"], ml)
check("warranty days computed", u1["warranty_days"] == 365 and u1["doc_name"] == "furnace_manual.pdf")

done_u1 = mt.mark_done(u1["id"], note="Installed fresh MERV 11", now=NOW)
expected_next = (d0 + timedelta(days=90)).isoformat()
check("mark_done advances next_due by every_days and sets state=ok",
      done_u1["next_due"] == expected_next and done_u1["state"] == "ok", done_u1)
m_notes = st.db.execute("SELECT * FROM items WHERE shelf='home/maintenance'").fetchall()
check("mark_done logs to home/maintenance", len(m_notes) == 1 and "Furnace filter" in m_notes[0]["body"])

# ---------------- 4. Hub Integration ----------------
# Add a fresh overdue item so hub.build() surfaces it in needs
mt.add("Smoke detector batteries", category="safety", every_days=365, next_due=over_due, now=NOW)


from homemind.checkins import Checkins  # noqa: E402


class StubApp:
    version = "0.34.0"
    mind = types.SimpleNamespace(store=st)
    checkins = Checkins(st, clock=lambda: NOW)
    patterns = types.SimpleNamespace(rows=lambda days: [])
    calendars = None
    home = types.SimpleNamespace(catalog={})
    meds = mh
    maintenance = mt
    ha = None
    pantry = None
    fitness = None


hub_out = asyncio.run(HUB.build(StubApp(), now=NOW))
check("hub includes meds summary", hub_out.get("meds", {}).get("total") == 1)
check("hub includes maintenance counts", hub_out.get("maintenance", {}).get("overdue") == 1)
check("hub For You surfaces overdue maintenance item",
      any(n.get("kind") == "Upkeep" and "Smoke detector batteries" in n.get("title", "") for n in hub_out.get("needs", [])),
      hub_out.get("needs"))

# ---------------- 5. HTTP & Admin-only X-Home-Mind-Viewer ----------------
os.environ["HOME_MIND_DATA"] = tempfile.mkdtemp()
os.environ["HOME_MIND_OPTIONS"] = os.path.join(os.environ["HOME_MIND_DATA"], "options.json")
with open(os.environ["HOME_MIND_OPTIONS"], "w") as f:
    json.dump({"people": [{"name": "Alice"}, {"name": "Bob"}]}, f)
for k in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
    os.environ.pop(k, None)

_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_review_sec.py")).read()
_ns = {"__file__": os.path.abspath(__file__), "__name__": "stub"}
exec(_src.split("\n_fake_aiohttp()\n")[0], _ns)
_ns["_fake_aiohttp"]()

from homemind import main as M  # noqa: E402
logging.disable(logging.CRITICAL)

A = M.App()
A.token = "admin-tok"


async def _start():
    await A.on_start(None)
    for t in A.tasks:
        t.cancel()


asyncio.run(_start())
web_app = M.build(A)
bob_tok = A.tokens.make("bob-phone", "write", person="Bob")["token"]


class Req(dict):
    def __init__(self, method, path, body=None, token="admin-tok", viewer=None):
        super().__init__()
        from urllib.parse import urlsplit, parse_qsl
        u = urlsplit(path)
        self.method, self.path, self.query = method, u.path, dict(parse_qsl(u.query, keep_blank_values=True))
        self.headers = {"X-Home-Mind-Token": token}
        if viewer is not None:
            self.headers["X-Home-Mind-Viewer"] = viewer
        self.remote, self._body, self.match_info = "10.0.0.9", body, {}
        self.can_read_body, self.content_type = body is not None, "application/json"

    async def json(self):
        return json.loads(json.dumps(self._body))


async def call(method, path, body=None, token="admin-tok", viewer=None):
    req = Req(method, path, body=body, token=token, viewer=viewer)
    handler = None
    for rm, pat, fn in web_app.routes:
        if rm != method:
            continue
        if "{" in pat:
            import re as _re
            rx = "^" + _re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pat) + "$"
            m = _re.match(rx, req.path)
            if m:
                req.match_info = m.groupdict()
                handler = fn
                break
        elif pat == req.path:
            handler = fn
            break
    assert handler is not None, f"no route for {method} {path}"
    chain = handler
    for mw in reversed(web_app.middlewares):
        prev = chain
        chain = (lambda m, p: lambda r: m(r, p))(mw, prev)
    resp = await chain(req)
    return resp.status, resp.data


async def run_http():
    # Alice saves a personal recipe via admin + X-Home-Mind-Viewer: Alice
    st_code, body = await call("POST", "/api/recipes/save",
                               {"name": "Alice Pasta", "scope": "personal", "calories": 500},
                               token="admin-tok", viewer="Alice")
    check("HTTP admin save personal recipe as Alice", st_code == 200 and body["recipe"]["owner"] == "Alice", body)

    # Admin viewing as Alice sees it; admin viewing as household hides it
    _, alice_list = await call("GET", "/api/recipes", token="admin-tok", viewer="Alice")
    _, hh_list = await call("GET", "/api/recipes", token="admin-tok", viewer="household")
    check("X-Home-Mind-Viewer=Alice sees Alice's personal recipe",
          any(x["name"] == "Alice Pasta" for x in alice_list["recipes"]), alice_list)
    check("X-Home-Mind-Viewer=household hides Alice's personal recipe",
          all(x["name"] != "Alice Pasta" for x in hh_list["recipes"]), hh_list)

    # Security: Bob's person-bound token CANNOT spoof X-Home-Mind-Viewer: Alice
    _, bob_spoof = await call("GET", "/api/recipes", token=bob_tok, viewer="Alice")
    check("person-bound token cannot spoof X-Home-Mind-Viewer to read Alice's recipe",
          all(x["name"] != "Alice Pasta" for x in bob_spoof["recipes"]), bob_spoof)

    # HTTP smoke test for /api/habits and /api/maintenance
    sc1, hb_add = await call("POST", "/api/habits/add", {"name": "Magnesium", "kind": "med", "dose": "400 mg"})
    sc2, hb_tog = await call("POST", "/api/habits/toggle", {"id": hb_add["item"]["id"]})
    sc3, hb_get = await call("GET", "/api/habits")
    check("HTTP /api/habits CRUD works",
          sc1 == 200 and sc2 == 200 and sc3 == 200 and hb_tog["done"] is True and hb_get["summary"]["done"] == 1)

    sc4, mt_add = await call("POST", "/api/maintenance/add", {"name": "HVAC Filter", "category": "hvac", "every_days": 60})
    sc5, mt_done = await call("POST", "/api/maintenance/done", {"id": mt_add["item"]["id"]})
    sc6, mt_get = await call("GET", "/api/maintenance")
    check("HTTP /api/maintenance CRUD works",
          sc4 == 200 and sc5 == 200 and sc6 == 200 and mt_done["item"]["state"] == "ok" and len(mt_get["items"]) == 1)


asyncio.run(run_http())

print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
