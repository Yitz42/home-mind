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

from . import nutrition

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
    "tea": (730, 730, 730), "coffee": (730, 730, 730), "instant coffee": (730, 730, 730),
    "salt": (1825, 1825, 1825), "cocoa": (730, 730, 730), "cocoa powder": (730, 730, 730),
    "powder": (1095, 1095, 1095), "spice": (1095, 1095, 1095), "seasoning": (1095, 1095, 1095),
    "black pepper": (1095, 1095, 1095), "soy sauce": (730, 730, 730), "enhancer": (365, 365, 365),
    "water enhancer": (365, 365, 365), "honey": (730, 730, 730), "vinegar": (730, 730, 730), "oil": (365, 365, 365),
    "ramen": (365, 365, 365), "noodles": (365, 365, 365), "instant noodles": (365, 365, 365),
    "biscuits": (180, 180, 180), "cookies": (180, 180, 180), "pastries": (120, 120, 120), "puffs": (120, 120, 120),
    "mac and cheese": (365, 365, 365), "seeds": (730, 730, 730), "chia seeds": (730, 730, 730),
    "macaroni and cheese": (365, 365, 365), "macaroni": (730, 730, 730), "grits": (365, 365, 365),
    "mix": (365, 365, 365), "cocoa mix": (730, 730, 730), "hot cocoa": (730, 730, 730), "rice a roni": (365, 365, 365), "pasta roni": (365, 365, 365), "hamburger helper": (365, 365, 365),
    "instant lunch": (365, 365, 365), "cup noodles": (365, 365, 365), "kraft dinner": (365, 365, 365),
    "oatmeal": (365, 365, 365), "popcorn": (240, 240, 240),
    "snacks": (180, 180, 180), "chocolate": (365, 365, 365), "candy": (365, 365, 365),
    "soda": (270, 270, 270), "root beer": (270, 270, 270), "cola": (270, 270, 270),
    "almonds": (365, 365, 365), "cashews": (365, 365, 365), "walnuts": (365, 365, 365), "peanuts": (365, 365, 365),
    "mango": (7, 300, 5), "peach": (5, 300, 3), "pear": (10, 300, 5), "pineapple": (5, 300, 3), "kiwi": (14, None, 5),
    "melon": (5, None, 5), "watermelon": (5, None, 7), "lasagna": (4, 270, None), "meatballs": (4, 120, None),
    "enchiladas": (4, 270, None), "dinner": (4, 270, None), "meal": (4, 270, None), "burrito": (4, 270, None),
    "canned": (None, None, 730), "can": (None, None, 730), "cans": (None, None, 730), "peas": (5, 300, 2),
}
COMBOS = ("peas and carrots", "mac and cheese", "macaroni and cheese", "rice and beans", "salt and pepper",
          "peanut butter and jelly", "sour cream and onion", "franks and beans", "pork and beans")
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
    "Never suggest skipping meals or very low calories. Aim for roughly a third of the daily protein target in a main "
    "meal. Some foods carry nutrition in [brackets] from USDA lab data or the package label, matched by name: base "
    "protein and calories on those (ignore a match that is clearly a different food) and estimate only the rest. "
    "When one of the listed studies supports a choice, name it briefly in 'why' (for example 'beans lower LDL ~5%, "
    "26 trials'); call preliminary findings preliminary and never overstate them. Their stated dislikes beat any study. "
    "Keep every field short so the answer stays compact: meal name under 6 words, 'why' under 20 words, 'steps' "
    "under 35 words in total, and no text outside the JSON. "
    "Reply with JSON only: {\"focus\": \"one short line on today's "
    "focus\", \"meals\": [{\"name\": \"...\", \"meal\": \"breakfast|lunch|dinner|snack\", \"uses\": [\"pantry items "
    "used\"], \"missing\": [\"items to buy, if any\"], \"why\": \"one line: what it uses up and how it fits today\", "
    "\"protein_g\": number, \"calories\": number, \"steps\": \"2-4 short steps\"}]}")


def parse_plan(text):
    """The meal plan JSON, or as much of it as finished if the reply was cut off mid-way."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        plan = json.loads(m.group(0)) if m else {}
        if isinstance(plan, dict):
            return plan
    except ValueError:
        pass
    plan = {}
    f = re.search(r'"focus"\s*:\s*"((?:[^"\\]|\\.)*)"', text or "")
    if f:
        plan["focus"] = json.loads('"' + f.group(1) + '"')
    meals = []
    for chunk in re.findall(r"\{[^{}]*\}", text or ""):   # each meal is a flat object
        try:
            o = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("name"):
            meals.append(o)
    plan["meals"] = meals
    if meals:
        plan["cut_off"] = True
    return plan


def _norm(name):
    n = re.sub(r"[^a-z ]", " ", (name or "").lower())
    return re.sub(r"\s+", " ", n).strip()


TRAILING_CONTAINERS = {"cup", "cups", "pack", "packs", "packet", "packets", "box", "boxes", "bag", "bags",
                       "jar", "jars", "bottle", "bottles", "can", "cans", "tub", "tubs", "carton", "cartons"}


SHELF_BRANDS = ("rice a roni", "pasta roni", "hamburger helper", "instant lunch", "cup noodles", "kraft dinner")


def _tail_match(n):
    """The table entry the end of the name names ("chicken ramen" is ramen, "milk biscuits" is biscuits)."""
    for b in SHELF_BRANDS:            # boxed products named by brand ("Rice-A-Roni beef" is not beef)
        if re.search(rf"\b{b}\b", n):
            return (b, SHELF[b])
    words = n.split()
    while len(words) > 1 and words[-1] in TRAILING_CONTAINERS:   # "ramen cups" is ramen
        words.pop()
    for size in range(len(words), 0, -1):
        tail = " ".join(words[-size:])
        best = None
        for k, v in SHELF.items():
            if re.fullmatch(rf"(?:.*\b)?{re.escape(k)}s?", tail) and (best is None or len(k) > len(best[0])):
                best = (k, v)
        if best:
            return best
    return None


def estimate(name, location):
    """Days until use for a food kept somewhere, from the table (longest matching key wins)."""
    n = _norm(name)
    idx = {"fridge": 0, "freezer": 1, "pantry": 2}.get(location, 0)
    best = None
    if location == "pantry" and re.search(r"\b(canned|can|cans|jar|jarred|dried|dry)\b", n):
        return 730
    # the last word names the food ("strawberry banana yogurt" is yogurt), so try the end of the name first
    best = _tail_match(n)
    if best is None:                  # "chicken breast": the food word isn't last
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
    if re.search(r"\b(canned|dried|dry)\b", n):
        return "pantry"
    tail = _tail_match(n)
    if tail:                          # "chicken ramen" keeps like ramen; "milk biscuits" like biscuits
        v = tail[1]
        return "fridge" if v[2] is None or (v[0] and v[0] < 30 and (v[2] or 0) < 7) else "pantry"
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
        d = datetime.fromtimestamp(ub)
        return {"id": iid, "name": name, "location": loc,
                "use_by": ("past its date (" + d.strftime("%b %d %Y") + ")") if ub < now else
                d.strftime("%a %b %d") if ub - now < 300 * 86400 else d.strftime("%b %Y")}

    def nutrition(self, name):
        """The best USDA / label match for a food name, or None (cached; needs the food database)."""
        kn = getattr(self, "knowledge", None)
        if not kn or not kn.food_ready():
            return None
        cache = self.__dict__.setdefault("_nut", {})
        key = re.sub(r"\(.*?\)", "", name.lower()).strip()
        if key not in cache:
            hits = kn.food(key, limit=1)
            cache[key] = hits[0] if hits else None
        return cache[key]

    def nutrition_note(self, name):
        n = self.nutrition(name)
        if not n:
            return ""
        src = n["source"] + (f" {n['brand']}" if n.get("brand") else "")
        sv, p = n.get("serving") or {}, n["per_100g"]
        if sv.get("protein") is not None:
            per, vals = f"per serving ({sv.get('text') or str(sv['g']) + ' g'})", sv
        elif p.get("protein") is not None:
            per, vals = "per 100 g", p
        else:
            return ""
        kcal = f", {vals['kcal']:.0f} kcal" if vals.get("kcal") is not None else ""
        return f" [{src} match '{n['name']}': {vals['protein']} g protein{kcal} {per}]"

    def ate(self, text, when=None, source="api"):
        """Log what was eaten or drunk (for the fog patterns: how late, caffeine). Doesn't change the pantry."""
        from .patterns import caffeinated
        text = (text or "").strip()
        if not text:
            raise ValueError("nothing eaten")
        now = when or time.time()
        caf = caffeinated(text)
        t = datetime.fromtimestamp(now)
        r = self.store.remember(text=f"Ate {text} at {t:%I:%M %p}".replace(" 0", " ") + f" on {t:%a %b %d}."
                                + (" (caffeine)" if caf else ""), title=f"Ate: {text}"[:80], kind="fact",
                                shelf="body/meals", source=source, tags=["meal"], meta={"at": now, "caffeine": caf},
                                at=now)
        return {"logged": text, "caffeine": caf, "at": t.strftime("%I:%M %p").lstrip("0"), "id": r.get("id")}

    def meal_hints(self, text):
        """Nutrition we already know for the foods named in a logged meal (pantry matches and the food database)."""
        body = re.sub(r"^\s*(breakfast|lunch|dinner|snack)\s*:\s*", "", text or "", flags=re.I)
        parts = [x.strip() for x in re.split(r",| and |\+", body) if x.strip()]
        rows = self.items("in") + self.items("used")      # just used counts: you often log a meal after finishing it
        out = []
        for part in parts:
            n = _norm(part)
            words = set(n.split()) - {"the", "a", "some", "my", "of", "half", "leftover", "pint", "serving", "cup"}
            names = [r["name"] for r in rows if words and len(words & set(_norm(r["name"]).split())) >= min(2, len(words))]
            note = ""
            for name in names[:1] + [part]:
                note = self.nutrition_note(name)
                if note:
                    break
            out.append(f"- {part}" + (f" (in the pantry as: {names[0]})" if names else "") + note)
        return "\n".join(out)

    async def estimate(self, iid, text=None, model=None):
        """Rough protein and calories for a logged meal, saved on it so Today can add up the day."""
        row = self.store.db.execute("SELECT title, meta FROM items WHERE id=?", (int(iid),)).fetchone()
        if not row:
            raise ValueError("no such meal")
        text = text or re.sub(r"^Ate:\s*", "", row["title"] or "")
        names = [x for x in self.models.models if x != "mock"]
        model = model or ("haiku" if "haiku" in names else names[0] if names else None)
        if not model:
            return None
        r = await self.models.get(model).chat(
            "Estimate the protein (grams) and calories in what someone ate. Use the nutrition given in [brackets] "
            "when it fits the food, scaled to the amount eaten; otherwise use typical values for a normal portion. "
            "Reply with JSON only: {\"protein_g\": number, \"calories\": number}",
            [{"role": "user", "content": f"Ate: {text}\nWhat we know:\n{self.meal_hints(text)}"}],
            max_tokens=200, temperature=0)
        m = re.search(r"\{.*?\}", r.get("text") or "", re.S)
        try:
            est = json.loads(m.group(0)) if m else {}
            p, c = float(est["protein_g"]), float(est.get("calories") or 0)
        except (ValueError, KeyError, TypeError):
            return None
        if not (0 <= p <= 200 and 0 <= c <= 4000):
            return None
        meta = json.loads(row["meta"] or "{}") if isinstance(row["meta"], str) else dict(row["meta"] or {})
        meta.update(protein_g=round(p), calories=round(c), estimated=True)
        self.store.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(meta), int(iid)))
        self.store.db.commit()
        return {"id": int(iid), "protein_g": round(p), "calories": round(c)}

    def remove(self, iid):
        """Delete a line entered by mistake (used or tossed keep a record; this doesn't)."""
        self.store.db.execute("DELETE FROM pantry WHERE id=?", (int(iid),))
        self.store.db.commit()
        return {"removed": int(iid)}

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
        text = text or ""
        for c in COMBOS:                      # "peas and carrots" is one food
            text = re.sub(rf"\b{c}\b", c.replace(" and ", " & "), text, flags=re.I)
        parts = [x.replace(" & ", " and ") for x in re.split(r",|\band\b|\n|;", text)]
        out = []
        for p in parts:
            p = re.sub(r"^\s*(i\s+)?(just\s+)?(bought|got|picked up|have|added|used|finished|ate|threw out|tossed)\s+",
                       "", p.strip(), flags=re.I)
            p = re.sub(r"^(the|some|an|a(?!\s+(?:dozen|couple|few)\b))\s+", "", p.strip(), flags=re.I).strip(" .!")
            if not p:
                continue
            m = re.match(r"^((?:bag|box|carton|can|jar|bunch|pack|bottle|loaf|head|tub|container|pint|quart|gallon)s?) "
                         r"(?:of\s+)?(.+)$", p, re.I)
            if m:                     # "a bag of baby carrots": the container is the amount
                out.append((m.group(2).strip(), "1 " + m.group(1).lower()))
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
            r = await self.ha.post_json("services/todo/get_items?return_response",
                                        {"entity_id": entity, "status": "completed"})
        except Exception:  # noqa: BLE001
            return 0
        if not isinstance(r, dict):               # Home Assistant restarting: try again next time
            return 0
        items = ((r.get("service_response") or {}).get(entity) or {}).get("items") or []
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
            r = await self.ha.post_json("services/ai_task/generate_data?return_response", {
                "task_name": "home_mind_pantry_photo", "entity_id": entity, "instructions": PHOTO_INSTRUCTIONS,
                "attachments": [{"media_content_id": f"media-source://media_source/local/home_mind/{name}",
                                 "media_content_type": ctype}]}, timeout=120)
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
        target = nutrition.protein_target(trend["latest"]) if trend else None
        return {"tired": bool(tired_why), "tired_why": tired_why, "signals": sig, "weight": trend,
                "protein_target_g": target}

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
            lines.append(f"- {i['name']}{(' (' + i['amount'] + ')') if i['amount'] else ''}, {i['location']}, {when}"
                         + self.nutrition_note(i["name"]))
        focus = []
        if day["tired"]:
            focus.append(f"Tired day ({', '.join(day['tired_why'])}): {g.get('tired_days')}")
        else:
            focus.append("Normal energy today")
        if day["weight"]:
            wt = day["weight"]
            focus.append(f"Weight {wt['latest']} lb" + (f", {wt['change']:+} lb since {wt['since']}" if "change" in wt else ""))
        if day.get("protein_target_g"):
            focus.append(f"Protein target about {day['protein_target_g']} g/day (1.6 g/kg for muscle; more adds nothing)")
        studies = nutrition.relevant([i["name"] for i in items])
        content = (f"Today: {datetime.now():%A %b %d, %I:%M %p}\n"
                   f"Today's focus: {'; '.join(focus)}\n"
                   f"Goals: {'; '.join(g.get('goals') or [])}\n"
                   + (f"Avoid: {', '.join(g['avoid'])}\n" if g.get("avoid") else "")
                   + (f"Notes: {g['notes']}\n" if g.get("notes") else "")
                   + f"Meal wanted: {meal or 'the next meal'}" + (f" ({request})" if request else "") + "\n"
                   f"Suggest {n} options.\n\nIN THE HOUSE (soonest to expire first):\n" + ("\n".join(lines) or "- nothing recorded")
                   + ("\n\nSTUDIES THAT APPLY (from their nutrition evidence tracker):\n" + nutrition.lines(studies) if studies else ""))
        r = await self.models.get(model).chat(MEAL_SYSTEM, [{"role": "user", "content": content}], max_tokens=3000,
                                              temperature=0.5)
        plan = parse_plan(r["text"])
        plan.setdefault("meals", [])
        plan["today"] = day
        plan["model"] = model
        plan["studies"] = [x["topic"] for x in studies]
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
