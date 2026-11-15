"""The pantry: what food is in the house, roughly when it goes bad, and what to make with it.

- Items live in a `pantry` table (name, amount, where: fridge / freezer / pantry, when added, use-by, status). If no
  use-by is given, it is estimated from typical shelf life by where it's kept.
- Adding: from a photo (a receipt, the fridge, a shelf) read by Claude through Home Assistant's AI Task; from Jeeves
  ("I bought chicken, spinach and eggs"); from the panel; and from the Home Assistant shopping list: items ticked off
  there move into the pantry.
- Using up: "I used the eggs" / "finished the milk" (Jeeves, panel). Past their date, items are asked about in the
  morning summary ("still have the yogurt?") instead of being silently dropped.
- Meals: what to make from what's here, using what expires first, fitted to today: the Steady watch (readiness,
  sleep, recovery, stress) says whether it's a tired day (lower-carb, steadier meals, as Yitz asked), the scale shows
  the weight trend, and the goals (more protein for muscle, fewer calories to lose weight) shape every suggestion.
  Missing ingredients can go straight onto the shopping list. The model (Haiku by default) picks meals; the numbers
  and dates come from here.
"""
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta

log = logging.getLogger("homemind.pantry")

# typical days until use, by where it's kept: (fridge, freezer, pantry). Rough, on the safe side.
SHELF = {
    "spinach": (5, 240, 2), "lettuce": (7, None, 2), "salad": (5, None, 1), "kale": (6, 240, 2),
    "broccoli": (5, 300, 2), "cauliflower": (7, 300, 3), "carrot": (21, 300, 7), "pepper": (10, 240, 4),
    "tomato": (7, 60, 5), "cucumber": (7, None, 3), "zucchini": (6, 240, 3), "mushroom": (6, 300, 1),
    "onion": (60, 240, 30), "garlic": (60, 300, 90), "potato": (30, 300, 21), "sweet potato": (21, 300, 21),
    "avocado": (5, 120, 3), "banana": (5, 90, 5), "apple": (35, 240, 10), "berries": (5, 240, 1),
    "strawberries": (5, 240, 1), "blueberries": (8, 240, 2), "grapes": (10, 300, 2), "orange": (21, 300, 10),
    "lemon": (28, 120, 10), "herbs": (7, 120, 2), "cilantro": (7, 120, 1), "parsley": (10, 120, 2),
    "chicken": (2, 270, None), "ground beef": (2, 120, None), "beef": (4, 300, None), "steak": (4, 300, None),
    "pork": (4, 180, None), "turkey": (2, 270, None), "fish": (2, 180, None), "salmon": (2, 180, None),
    "tuna": (3, 90, 730), "shrimp": (2, 180, None), "tofu": (5, 150, None), "deli": (5, 60, None),
    "sausage": (7, 60, None), "bacon": (7, 30, None), "hot dog": (10, 60, None),
    "eggs": (28, None, None), "egg": (28, None, None), "milk": (7, 90, None), "yogurt": (14, 60, None),
    "greek yogurt": (14, 60, None), "cottage cheese": (7, None, None), "cheese": (28, 180, None),
    "butter": (60, 270, 2), "cream": (7, 90, None), "hummus": (7, 120, None), "juice": (10, 240, None),
    "bread": (10, 90, 5), "tortilla": (21, 180, 7), "bagel": (7, 90, 4), "cooked": (4, 90, None),
    "leftover": (4, 90, None), "rice": (5, 180, 730), "pasta": (5, 90, 730), "oats": (None, None, 365),
    "cereal": (None, None, 180), "beans": (4, 180, 730), "lentils": (5, 180, 365), "chickpeas": (4, 180, 730),
    "flour": (None, 365, 240), "sugar": (None, None, 730), "peanut butter": (180, None, 90), "nuts": (180, 365, 90),
    "protein powder": (None, None, 365), "chips": (None, None, 60), "crackers": (None, None, 120),
    "soup": (4, 180, 730), "sauce": (21, 180, 365), "salsa": (14, 60, 365), "ketchup": (180, None, 365),
    "frozen": (None, 240, None), "ice cream": (None, 60, None), "pizza": (4, 60, None),
}
DEFAULT_DAYS = {"fridge": 7, "freezer": 120, "pantry": 180}
LOCATIONS = ("fridge", "freezer", "pantry")
FRIDGE_FIRST = ("chicken", "beef", "pork", "turkey", "fish", "salmon", "shrimp", "milk", "yogurt", "cheese", "eggs",
                "egg", "spinach", "lettuce", "berries", "tofu", "deli", "sausage", "bacon", "cream", "hummus")

DEFAULT_GOALS = {
    "goals": ["more protein to build muscle", "fewer calories to lose weight"],
    "tired_days": "lower-carb meals with steady energy on tired days",
    "avoid": ["mushy vegetables", "very spicy food", "mushrooms"],
    "notes": "No allergies; most other foods are fine.",
}

PHOTO_INSTRUCTIONS = (
    "List every food item you can see in this photo (a receipt, a fridge, a pantry shelf or groceries). For each give "
    "a short plain name (\"chicken breast\", \"baby spinach\", \"eggs\"), the amount if you can tell (\"1 dozen\", \"2 "
    "lb\", \"3\"), where it's kept or belongs (fridge, freezer or pantry) and a use-by date only if one is printed and "
    "readable (YYYY-MM-DD). Skip non-food items, bags, taxes and totals. Reply with JSON only: {\"items\": [{\"name\": "
    "\"...\", \"amount\": \"...\", \"location\": \"fridge|freezer|pantry\", \"use_by\": \"YYYY-MM-DD or null\"}]}")

MEAL_SYSTEM = (
    "You suggest meals from what someone has at home. Use foods that expire soonest first. Fit today's focus and their "
    "goals. Prefer real meals they can actually make in under 30 minutes unless asked otherwise; keep it practical. "
    "Only use pantry staples (salt, pepper, oil, common spices) beyond the listed food unless you put the extra "
    "ingredient in 'missing'. Give rough estimates only (protein in grams, calories), and say they are estimates. "
    "Never suggest skipping meals or very low calories. Reply with JSON only: {\"focus\": \"one short line on today's "
    "focus\", \"meals\": [{\"name\": \"...\", \"meal\": \"breakfast|lunch|dinner|snack\", \"uses\": [\"pantry items "
    "used\"], \"missing\": [\"items to buy, if any\"], \"why\": \"one line: what it uses up and how it fits today\", "
    "\"protein_g\": number, \"calories\": number, \"steps\": \"2-4 short steps\"}]}")


def _norm(name):
    n = re.sub(r"[^a-z ]", " ", (name or "").lower())
    return re.sub(r"\s+", " ", n).strip()


def estimate(name, location):
    """Days until use for a food kept somewhere, from the table (longest matching key wins)."""
    n = _norm(name)
    idx = {"fridge": 0, "freezer": 1, "pantry": 2}.get(location, 0)
    best = None
    for k, v in SHELF.items():
        if re.search(rf"\b{re.escape(k)}s?\b", n) and (best is None or len(k) > len(best[0])):
            best = (k, v)
    if best and best[1][idx] is not None:
        return best[1][idx]
    if best and best[1][idx] is None and location == "pantry":
        return 1          # something that belongs in the fridge left out
    return DEFAULT_DAYS.get(location, 7)


def guess_location(name):
    n = _norm(name)
    if "frozen" in n or "ice cream" in n:
        return "freezer"
    if any(re.search(rf"\b{k}s?\b", n) for k in FRIDGE_FIRST):
        return "fridge"
    for k, v in SHELF.items():
        if re.search(rf"\b{re.escape(k)}s?\b", n):
            return "fridge" if v[2] is None or (v[0] and v[0] < 30 and (v[2] or 0) < 7) else "pantry"
    return "pantry"


class Pantry:
    def __init__(self, store, ha, models, data_dir):
        self.store, self.ha, self.models, self.data_dir = store, ha, models, data_dir
        store.db.execute("""CREATE TABLE IF NOT EXISTS pantry (
            id INTEGER PRIMARY KEY, name TEXT, amount TEXT, location TEXT, added REAL, use_by REAL,
            status TEXT DEFAULT 'in', source TEXT, updated REAL, note TEXT)""")
        store.db.commit()
        self.seen_done = set()

    # ---- goals ------------------------------------------------------------------------------------------------------
    def goals(self):
        return self.store.kv_get("food_goals", DEFAULT_GOALS)

    def set_goals(self, g):
        cur = dict(self.goals())
        cur.update({k: v for k, v in (g or {}).items() if k in DEFAULT_GOALS})
        self.store.kv_set("food_goals", cur)
        return cur

    # ---- items ------------------------------------------------------------------------------------------------------
    def items(self, status="in"):
        rows = self.store.db.execute("SELECT * FROM pantry WHERE status=? ORDER BY use_by", (status,)).fetchall()
        now = time.time()
        out = []
        for r in rows:
            d = dict(r)
            d["days_left"] = None if d["use_by"] is None else round((d["use_by"] - now) / 86400, 1)
            out.append(d)
        return out

    def add(self, name, amount="", location=None, use_by=None, source="api", when=None):
        name = (name or "").strip()
        if not name:
            raise ValueError("no item name")
        loc = (location or "").lower().strip()
        loc = loc if loc in LOCATIONS else guess_location(name)
        now = when or time.time()
        if use_by:
            try:
                ub = datetime.fromisoformat(str(use_by)[:10]).replace(hour=23, minute=59).timestamp()
            except ValueError:
                ub = now + estimate(name, loc) * 86400
        else:
            ub = now + estimate(name, loc) * 86400
        # the same food already in stock: top it up instead of a second line
        same = self.store.db.execute("SELECT id FROM pantry WHERE status='in' AND lower(name)=? AND location=?",
                                     (name.lower(), loc)).fetchone()
        if same:
            self.store.db.execute("UPDATE pantry SET amount=?, use_by=?, added=?, updated=?, source=? WHERE id=?",
                                  (amount or "", ub, now, now, source, same["id"]))
            iid = same["id"]
        else:
            cur = self.store.db.execute("INSERT INTO pantry(name,amount,location,added,use_by,status,source,updated) "
                                        "VALUES(?,?,?,?,?,'in',?,?)", (name, amount or "", loc, now, ub, source, now))
            iid = cur.lastrowid
        self.store.db.commit()
        return {"id": iid, "name": name, "location": loc,
                "use_by": datetime.fromtimestamp(ub).strftime("%a %b %d")}

    def find(self, name):
        n = _norm(name)
        rows = self.items()
        exact = [r for r in rows if _norm(r["name"]) == n]
        if exact:
            return exact
        words = set(n.split()) - {"the", "a", "some", "my", "of"}
        scored = [(len(words & set(_norm(r["name"]).split())), r) for r in rows]
        best = max((s for s, _ in scored), default=0)
        return [r for s, r in scored if s == best and s > 0]

    def use(self, name, status="used"):
        hits = self.find(name)
        if not hits:
            return {"name": name, "found": False}
        r = sorted(hits, key=lambda x: x["use_by"] or 0)[0]     # the one going bad first
        self.store.db.execute("UPDATE pantry SET status=?, updated=? WHERE id=?", (status, time.time(), r["id"]))
        self.store.db.commit()
        return {"name": r["name"], "found": True, "id": r["id"], "status": status}

    def parse_list(self, text):
        """'chicken, 2 lb ground beef and a dozen eggs' -> [(name, amount)]."""
        parts = re.split(r",|\band\b|\n|;", text or "")
        out = []
        for p in parts:
            p = re.sub(r"^\s*(i\s+)?(just\s+)?(bought|got|picked up|have|added|used|finished|ate|threw out|tossed)\s+",
                       "", p.strip(), flags=re.I)
            p = re.sub(r"^(the|some|an|a(?!\s+(?:dozen|couple|few)\b))\s+", "", p.strip(), flags=re.I).strip(" .!")
            if not p:
                continue
            m = re.match(r"^((?:\d+(?:\.\d+)?|a dozen|dozen|half a|a couple|a few|one|two|three|four|five|six)"
                         r"(?:\s*(?:lb|lbs|pounds?|oz|ounces?|kg|g|bags?|boxes?|cans?|jars?|bunch(?:es)?|cartons?|"
                         r"packs?|bottles?|heads?|loaf|loaves|dozen|pieces?))?(?:\s+of)?)\s+(.+)$", p, re.I)
            out.append((m.group(2).strip(), m.group(1).strip()) if m else (p, ""))
        return out

    # ---- shopping list -----------------------------------------------------------------------------------------------
    async def sync_shopping(self, entity="todo.shopping_list"):
        """Items ticked off the Home Assistant shopping list are in the house now: add them, clear them from the list."""
        try:
            r = await self.ha._post("services/todo/get_items?return_response",
                                    {"entity_id": entity, "status": "completed"})
        except Exception:  # noqa: BLE001
            return 0
        items = (((r or {}).get("service_response") or {}).get(entity) or {}).get("items") or []
        n = 0
        for it in items:
            uid = it.get("uid")
            if not uid or uid in self.seen_done:
                continue
            self.seen_done.add(uid)
            name, amount = (self.parse_list(it.get("summary", "")) or [(it.get("summary", ""), "")])[0]
            self.add(name, amount, source="shopping list")
            n += 1
        if n:
            try:
                await self.ha.call("todo", "remove_completed_items", {"entity_id": entity})
            except Exception:  # noqa: BLE001
                pass
        return n

    async def add_to_shopping(self, items, entity="todo.shopping_list"):
        done = []
        for it in items:
            try:
                await self.ha.call("todo", "add_item", {"entity_id": entity, "item": it})
                done.append(it)
            except Exception as e:  # noqa: BLE001
                log.warning("shopping list %s: %s", it, e)
        return done

    # ---- photos -------------------------------------------------------------------------------------------------------
    async def from_photo(self, data, filename="photo.jpg", entity="ai_task.claude_ai_task"):
        """Read food items off a photo with Claude (Home Assistant AI Task) and add them."""
        ext = os.path.splitext(filename)[1].lower() or ".jpg"
        ctype = {".png": "image/png", ".webp": "image/webp", ".heic": "image/heic"}.get(ext, "image/jpeg")
        media = "/media/home_mind"
        os.makedirs(media, exist_ok=True)
        name = f"pantry_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}{ext}"
        with open(os.path.join(media, name), "wb") as f:
            f.write(data)
        try:
            r = await self.ha._post("services/ai_task/generate_data?return_response", {
                "task_name": "home_mind_pantry_photo", "entity_id": entity, "instructions": PHOTO_INSTRUCTIONS,
                "attachments": [{"media_content_id": f"media-source://media_source/local/home_mind/{name}",
                                 "media_content_type": ctype}]})
        finally:
            try:
                os.remove(os.path.join(media, name))
            except OSError:
                pass
        raw = ((r or {}).get("service_response") or {}).get("data")
        text = raw if isinstance(raw, str) else json.dumps(raw or {})
        m = re.search(r"\{.*\}", text, re.S)
        found = (json.loads(m.group(0)).get("items") if m else None) or []
        added = [self.add(i.get("name"), i.get("amount") or "", i.get("location"), i.get("use_by"), source="photo")
                 for i in found if isinstance(i, dict) and i.get("name")]
        return {"read": len(found), "added": added}

    # ---- today ---------------------------------------------------------------------------------------------------------
    async def today(self):
        """How the day looks from the watch and the scale, and what that means for food."""
        async def num(eid):
            s = await self.ha.state(eid)
            try:
                return float((s or {}).get("state"))
            except (TypeError, ValueError):
                return None
        sig = {k: await num(f"input_number.steady_{k}") for k in
               ("readiness", "sleep_hours", "recovery", "hrv", "stress_alerts", "fog_today", "steps")}
        # a sleep note from today beats the watch (it was off one night)
        note = self.store.db.execute("SELECT body FROM items WHERE shelf='body/sleep' AND status='active' AND created > ? "
                                     "ORDER BY created DESC LIMIT 1", (time.time() - 20 * 3600,)).fetchone()
        if note:
            m = re.search(r"\((\d+(?:\.\d+)?) hours?\)", note["body"])
            if m:
                sig["sleep_hours"] = float(m.group(1))
        tired_why = []
        if sig["readiness"] is not None and sig["readiness"] < 50:
            tired_why.append(f"readiness {sig['readiness']:.0f}")
        if sig["sleep_hours"] is not None and sig["sleep_hours"] < 6:
            tired_why.append(f"{sig['sleep_hours']:g} h sleep")
        if sig["recovery"] is not None and sig["recovery"] < 40:
            tired_why.append(f"recovery {sig['recovery']:.0f}")
        if sig["fog_today"] is not None and sig["fog_today"] >= 4:
            tired_why.append("brain fog")
        w = self.store.db.execute("SELECT meta, created FROM items WHERE shelf='body/weight' AND status='active' "
                                  "ORDER BY created DESC LIMIT 6").fetchall()
        weights = [(r["created"], json.loads(r["meta"] or "{}").get("weight")) for r in w]
        weights = [(t, v) for t, v in weights if isinstance(v, (int, float))]
        trend = None
        if len(weights) >= 2:
            (t1, v1), (t0, v0) = weights[0], weights[-1]
            trend = {"latest": round(v1, 1), "change": round(v1 - v0, 1), "since": datetime.fromtimestamp(t0).strftime("%b %d")}
        elif weights:
            trend = {"latest": round(weights[0][1], 1)}
        return {"tired": bool(tired_why), "tired_why": tired_why, "signals": sig, "weight": trend}

    # ---- meals ------------------------------------------------------------------------------------------------------------
    async def suggest(self, meal=None, model=None, n=3, request=""):
        items = self.items()
        day = await self.today()
        g = self.goals()
        names = [x for x in self.models.models if x != "mock"]
        model = model or ("haiku" if "haiku" in names else names[0] if names else None)
        if not model:
            raise RuntimeError("no model connected")
        lines = []
        for i in items:
            dl = i["days_left"]
            when = "past its date" if dl is not None and dl < 0 else ("use today" if dl is not None and dl < 1 else
                                                                      f"{dl:.0f} days left" if dl is not None else "")
            lines.append(f"- {i['name']}{(' (' + i['amount'] + ')') if i['amount'] else ''}, {i['location']}, {when}")
        focus = []
        if day["tired"]:
            focus.append(f"Tired day ({', '.join(day['tired_why'])}): {g.get('tired_days')}")
        else:
            focus.append("Normal energy today")
        if day["weight"]:
            wt = day["weight"]
            focus.append(f"Weight {wt['latest']} lb" + (f", {wt['change']:+} lb since {wt['since']}" if "change" in wt else ""))
        content = (f"Today: {datetime.now():%A %b %d, %I:%M %p}\n"
                   f"Today's focus: {'; '.join(focus)}\n"
                   f"Goals: {'; '.join(g.get('goals') or [])}\n"
                   + (f"Avoid: {', '.join(g['avoid'])}\n" if g.get("avoid") else "")
                   + (f"Notes: {g['notes']}\n" if g.get("notes") else "")
                   + f"Meal wanted: {meal or 'the next meal'}" + (f" ({request})" if request else "") + "\n"
                   f"Suggest {n} options.\n\nIN THE HOUSE (soonest to expire first):\n" + ("\n".join(lines) or "- nothing recorded"))
        r = await self.models.get(model).chat(MEAL_SYSTEM, [{"role": "user", "content": content}], max_tokens=1200,
                                              temperature=0.5)
        m = re.search(r"\{.*\}", r["text"], re.S)
        try:
            plan = json.loads(m.group(0)) if m else {}
        except ValueError:
            plan = {}
        plan.setdefault("meals", [])
        plan["today"] = day
        plan["model"] = model
        if not plan["meals"]:
            plan["raw"] = r["text"][:1500]
        return plan

    # ---- for answers and the morning summary ----------------------------------------------------------------------------
    def soon(self, days=2):
        return [i for i in self.items() if i["days_left"] is not None and i["days_left"] <= days]

    def lines(self):
        items = self.items()
        if not items:
            return "PANTRY: nothing recorded."
        out = ["PANTRY (what's in the house, soonest to expire first):"]
        for i in items:
            dl = i["days_left"]
            when = "past its date" if dl is not None and dl < 0 else f"{max(dl, 0):.0f} days left" if dl is not None else ""
            out.append(f"- {i['name']}{(' (' + i['amount'] + ')') if i['amount'] else ''}: {i['location']}, {when}, "
                       f"added {datetime.fromtimestamp(i['added']):%b %d}")
        return "\n".join(out)
