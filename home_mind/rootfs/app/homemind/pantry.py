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


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


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
    "You suggest meals from what someone has at home. Use foods that expire soonest first, and if the request names "
    "specific ingredients, feature them. Fit today's focus and their "
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


AISLE_ORDER = ("produce", "meat & seafood", "dairy & eggs", "pantry & dry", "frozen", "other")
AISLE_HINTS = {
    "produce": ("apple", "avocado", "banana", "basil", "bell pepper", "berries", "broccoli", "cabbage", "carrot",
                "cauliflower", "celery", "cilantro", "cucumber", "garlic", "ginger", "grape", "green onion", "herb",
                "kale", "lemon", "lettuce", "lime", "mango", "melon", "mushroom", "onion", "orange", "parsley",
                "peach", "pear", "pepper", "pineapple", "potato", "salad", "scallion", "spinach", "squash",
                "strawberry", "sweet potato", "tomato", "vegetable", "zucchini"),
    "meat & seafood": ("bacon", "beef", "chicken", "cod", "deli", "duck", "fish", "ground", "ham", "lamb",
                       "meatball", "pork", "salmon", "sausage", "shrimp", "steak", "tilapia", "tofu", "tuna", "turkey"),
    "dairy & eggs": ("butter", "cheese", "cottage cheese", "cream", "egg", "feta", "greek yogurt", "milk",
                     "mozzarella", "parmesan", "sour cream", "yogurt"),
    "frozen": ("frozen", "ice cream"),
    "pantry & dry": ("almond", "bagel", "bean", "bread", "broth", "canned", "cashew", "cereal", "chickpea",
                     "chili", "coconut milk", "coffee", "corn", "cracker", "curry", "flour", "honey", "hummus",
                     "lentil", "noodle", "oat", "oil", "olive", "pasta", "peanut", "pita", "quinoa", "ramen",
                     "rice", "salsa", "sauce", "seed", "soy sauce", "spice", "stock", "sugar", "tea", "tortilla",
                     "vinegar", "walnut"),
}

WEEK_SYSTEM = (
    "You build a practical multi-day meal plan for a household from what they already have at home plus a concise "
    "grocery list. Put foods expiring soonest on the earliest days. Overlap fresh ingredients across days so nothing "
    "goes to waste. Respect their goals, Avoid list, and any ALLERGY items strictly (never include an allergen). "
    "Lean toward LEARNED TASTES when they fit. Keep weeknight meals realistic (under ~35 min). "
    "Only list non-staple ingredients that aren't already in stock in 'need' (skip basic salt, pepper, cooking oil). "
    "Keep every field short so the full week fits cleanly in JSON. "
    'Reply with JSON only: {"summary": "one short sentence on the week\'s theme", '
    '"days": [{"day": "Mon Nov 23", "meals": [{"slot": "dinner", "name": "short meal name", '
    '"uses": ["in-stock items used"], '
    '"need": [{"item": "ingredient", "amount": "qty + unit", "aisle": "produce|meat & seafood|dairy & eggs|pantry & dry|frozen|other"}], '
    '"protein_g": number, "calories": number, "why": "short reason", "steps": "2-3 short steps"}]}]}')

SWAP_SYSTEM = (
    "You replace ONE meal in an existing weekly meal plan. Pick a fresh option that does NOT repeat any meal already "
    "planned on other days, reuses ingredients already planned or in stock where sensible, respects all goals, Avoid "
    "items, ALLERGY items strictly, and any user tweak request. Keep fields short. "
    'Reply with JSON only: {"slot": "dinner", "name": "short meal name", "uses": ["in-stock items used"], '
    '"need": [{"item": "ingredient", "amount": "qty + unit", "aisle": "produce|meat & seafood|dairy & eggs|pantry & dry|frozen|other"}], '
    '"protein_g": number, "calories": number, "why": "short reason", "steps": "2-3 short steps"}')


def guess_aisle(name, hint=""):
    h = (hint or "").strip().lower()
    for a in AISLE_ORDER:
        if h == a or (h and h in a):
            return a
    low = _norm(name)
    for a, kws in AISLE_HINTS.items():
        if any(re.search(rf"\b{re.escape(k)}s?\b", low) for k in kws):
            return a
    return "other"


def _stem(name):
    n = _norm(name)
    toks = [w[:-2] if w.endswith("es") and len(w) > 4 else (w[:-1] if w.endswith("s") and len(w) > 3 else w)
            for w in n.split()]
    return " ".join(toks)


def _parse_qty(amount):
    """Parse '2 lb', '1.5 cups', '1/2 dozen' -> (float, unit_stem) or None."""
    s = (amount or "").strip().lower()
    if not s:
        return None
    m = re.match(r"^(\d+(?:\.\d+)?|\d+/\d+)\s*(.*)$", s)
    if not m:
        return None
    raw_n, unit = m.group(1), m.group(2).strip()
    try:
        val = float(raw_n.split("/")[0]) / float(raw_n.split("/")[1]) if "/" in raw_n else float(raw_n)
    except (ValueError, ZeroDivisionError):
        return None
    u_norm = re.sub(r"s$", "", unit) if len(unit) > 2 and unit not in ("oz", "lbs") else unit
    if u_norm == "lbs":
        u_norm = "lb"
    return val, u_norm, unit


def _merge_amounts(amounts):
    clean = [str(a).strip() for a in amounts if str(a or "").strip()]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    parsed = [_parse_qty(a) for a in clean]
    if all(parsed) and len({p[1] for p in parsed}) == 1:
        total = sum(p[0] for p in parsed)
        unit = parsed[0][1]
        num_s = f"{total:g}"
        if not unit:
            return num_s
        if total != 1 and unit not in ("lb", "oz", "g", "kg", "ml", "l", "tbsp", "tsp") and not unit.endswith("s"):
            unit += "s"
        return f"{num_s} {unit}"
    # Different units: keep unique strings joined cleanly
    return " + ".join(dict.fromkeys(clean))


def _in_stock_match(item_name, stock_stems):
    st = _stem(item_name)
    if not st:
        return False
    if st in stock_stems:
        return True
    for s in stock_stems:
        if len(st) >= 4 and (f" {st} " in f" {s} " or f" {s} " in f" {st} "):
            return True
    return False


def build_shopping_list(days_list, in_stock_items=()):
    """Deterministically build a deduplicated, aisle-grouped shopping list from a weekly plan,
    subtracting fresh items already in stock and merging matching quantities."""
    stock_stems = {
        _stem(i.get("name") if isinstance(i, dict) else str(i))
        for i in (in_stock_items or ())
        if not (isinstance(i, dict) and i.get("days_left") is not None and i["days_left"] < 0)
    }
    stock_stems.discard("")

    grouped = {}
    for d in (days_list or ()):
        if not isinstance(d, dict):
            continue
        day_lbl = (d.get("day") or "").split()[0] or "Day"
        for m in (d.get("meals") or ()):
            if not isinstance(m, dict):
                continue
            m_name = (m.get("name") or "").strip()
            needs = m.get("need") if m.get("need") is not None else (m.get("missing") or [])
            for raw in needs:
                if isinstance(raw, dict):
                    item = str(raw.get("item") or raw.get("name") or "").strip()
                    amt = str(raw.get("amount") or "").strip()
                    aisle = raw.get("aisle") or ""
                else:
                    s = str(raw or "").strip()
                    if not s:
                        continue
                    q_m = re.match(r"^(\d+(?:[./]\d+)?\s*(?:lb|lbs|oz|g|kg|cup|cups|tbsp|tsp|can|cans|head|heads|bunch|bunches|clove|cloves|pack|packs|bag|bags|dozen|box|boxes|jar|jars|bottle|bottles)?)\s+(.+)$", s, re.I)
                    if q_m and len(q_m.group(2).strip()) >= 2:
                        amt, item = q_m.group(1).strip(), q_m.group(2).strip()
                    else:
                        amt, item = "", s
                    aisle = ""
                if not item or _in_stock_match(item, stock_stems):
                    continue
                key = _stem(item) or item.lower()
                ent = grouped.setdefault(key, {
                    "item": item,
                    "amounts": [],
                    "aisle": guess_aisle(item, aisle),
                    "meals": [],
                })
                if amt:
                    ent["amounts"].append(amt)
                tag = f"{day_lbl}: {m_name}" if m_name else day_lbl
                if tag not in ent["meals"]:
                    ent["meals"].append(tag)

    order_idx = {a: i for i, a in enumerate(AISLE_ORDER)}
    out = []
    for ent in grouped.values():
        amt = _merge_amounts(ent["amounts"])
        label = f"{amt} {ent['item']}".strip() if amt else ent["item"]
        out.append({
            "item": ent["item"],
            "amount": amt,
            "label": label,
            "aisle": ent["aisle"],
            "meals": ent["meals"],
        })
    out.sort(key=lambda x: (order_idx.get(x["aisle"], 99), x["item"].lower()))
    return out


def parse_week_plan(text):
    """Parse the weekly meal plan JSON cleanly (tolerating surrounding prose or markdown fences)."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {"summary": "", "days": []}
    try:
        obj = json.loads(m.group(0))
        if not isinstance(obj, dict):
            return {"summary": "", "days": []}
    except ValueError:
        return {"summary": "", "days": []}
    days_out = []
    for d in (obj.get("days") or ()):
        if not isinstance(d, dict):
            continue
        meals = []
        for ml in (d.get("meals") or ()):
            if isinstance(ml, dict) and str(ml.get("name") or "").strip():
                meals.append({
                    "slot": str(ml.get("slot") or ml.get("meal") or "dinner").lower(),
                    "name": str(ml["name"]).strip(),
                    "uses": [str(x).strip() for x in (ml.get("uses") or ()) if str(x or "").strip()],
                    "need": ml.get("need") if isinstance(ml.get("need"), list) else list(ml.get("missing") or ()),
                    "protein_g": ml.get("protein_g"),
                    "calories": ml.get("calories"),
                    "why": str(ml.get("why") or "").strip(),
                    "steps": ml.get("steps") or "",
                })
        if meals:
            days_out.append({"day": str(d.get("day") or f"Day {len(days_out) + 1}").strip(), "meals": meals})
    return {"summary": str(obj.get("summary") or "").strip(), "days": days_out}


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
        more = "".join(f", {vals[c]} g {c}" for c in ("carbs", "fiber", "sugar", "fat") if vals.get(c) is not None)
        por = n.get("portions") or []
        por = "; portions: " + ", ".join(f"{x['text']} = {x['g']:g} g" for x in por[:3]) if por else ""
        return f" [{src} match '{n['name']}': {vals['protein']} g protein{kcal}{more} {per}{por}]"

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
            "Estimate the nutrition in what someone ate. Use the nutrition given in [brackets] when it fits the food, "
            "scaled to the amount eaten (portion weights are given where known); otherwise use typical values for a "
            "normal portion. Reply with JSON only: {\"protein_g\": number, \"calories\": number, \"carbs_g\": "
            "number, \"fiber_g\": number, \"sugar_g\": number, \"fat_g\": number}",
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
        more = {}
        for k, hi in (("carbs_g", 600), ("fiber_g", 100), ("sugar_g", 300), ("fat_g", 300)):
            v = _f(est.get(k))
            if v is not None and 0 <= v <= hi:
                more[k] = round(v)
        if meta.get("protein_g") is not None and not meta.get("estimated"):
            p = meta["protein_g"]           # a number the person gave stays
        meta.update(protein_g=round(p), calories=round(c), estimated=True, **more)
        self.store.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(meta), int(iid)))
        self.store.db.commit()
        return {"id": int(iid), "protein_g": round(p), "calories": round(c), **more}

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
        name = f"pantry_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}{ext}"
        try:
            os.makedirs(media, exist_ok=True)
            with open(os.path.join(media, name), "wb") as f:
                f.write(data)
        except OSError as e:
            # Home Assistant reads the photo from its media folder: without it there's nothing to send
            raise ValueError(f"can't save the photo where Home Assistant can read it ({media}: {e.strerror}); "
                             "is the media folder mapped for this add-on?") from None
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
        async def fresh(eid):
            """Today's value only: fog_today still holds yesterday's rating until today's check-in."""
            s = await self.ha.state(eid)
            try:
                v = float((s or {}).get("state"))
            except (TypeError, ValueError):
                return None
            lc = (s or {}).get("last_changed") or (s or {}).get("last_updated")
            if lc:
                try:
                    if datetime.fromisoformat(str(lc).replace("Z", "+00:00")).astimezone().date() != datetime.now().date():
                        return None
                except ValueError:
                    pass
            return v
        sig = {k: await num(f"input_number.steady_{k}") for k in
               ("readiness", "sleep_hours", "recovery", "hrv", "stress_alerts", "steps")}
        sig["fog_today"] = await fresh("input_number.steady_fog_today")
        for k in ("readiness", "sleep_hours", "recovery", "hrv"):
            if sig[k] == 0:
                sig[k] = None           # 0 means the watch was off or hasn't synced, not a real reading
        # a sleep note from today beats the watch (it was off one night, or "slept from 12 to 7am" was said)
        note = self.store.db.execute("SELECT body, meta FROM items WHERE shelf='body/sleep' AND status='active' AND created > ? "
                                     "ORDER BY created DESC LIMIT 1", (time.time() - 20 * 3600,)).fetchone()
        if note:
            nm = json.loads(note["meta"] or "{}")
            m = re.search(r"\((\d+(?:\.\d+)?) hours?\)", note["body"])
            if isinstance(nm.get("sleep"), (int, float)):
                sig["sleep_hours"] = float(nm["sleep"])
            elif m:
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

    # ---- weekly meal plan + smart shopping list -----------------------------------------------------------------------
    WEEK_KV = "pantry_week_plan"

    def _week_model(self, model=None):
        names = [x for x in self.models.models if x != "mock"]
        chosen = model or ("haiku" if "haiku" in names else names[0] if names else None)
        if not chosen:
            raise RuntimeError("no model connected")
        return chosen

    def _week_prefs_context(self, viewer=None):
        """Return (avoid_items, taste_line) combining goals + optional MemGraph allergies/dislikes/tastes."""
        g = self.goals()
        avoid = list(g.get("avoid") or [])
        seen = {a.lower() for a in avoid}
        taste_parts = []
        mg = getattr(self, "memgraph", None)
        if mg is not None:
            from .speakers import VIEWER
            who = VIEWER.get() if viewer is None else viewer
            try:
                links = {}
                for lnk in mg.query(categories=["FOOD", "HEALTH"], viewer=who, include_dormant=False, limit=24, record_access=False):
                    links[lnk["id"]] = lnk
                for lnk in mg.query(relations=["ALLERGIC_TO"], viewer=who, include_dormant=False, limit=12, record_access=False):
                    links[lnk["id"]] = lnk
                direct_likes, inferred_likes = [], []
                for lnk in links.values():
                    if lnk.get("status") == "CONTESTED":
                        continue
                    tgt = (lnk.get("target_name") or "").strip()
                    if not tgt:
                        continue
                    rel = lnk.get("relation")
                    sv = float(lnk.get("effective_stability", lnk.get("stability_score", 0.0)))
                    if rel == "ALLERGIC_TO" and tgt.lower() not in seen:
                        avoid.insert(0, f"ALLERGY: {tgt}")
                        seen.add(tgt.lower())
                    elif rel in ("LIKES", "PREFERS") and lnk.get("status") in ("FACT", "IDEA"):
                        if lnk.get("signed") and sv < 0 and tgt.lower() not in seen:
                            avoid.append(f"dislike: {tgt}")
                            seen.add(tgt.lower())
                        elif sv > 0:
                            (direct_likes if lnk.get("evidence") == "direct" else inferred_likes).append(tgt)
                if direct_likes:
                    taste_parts.append("stated likes: " + ", ".join(dict.fromkeys(direct_likes[:6])))
                if inferred_likes:
                    taste_parts.append("observed cuisine/flavor patterns: " + ", ".join(dict.fromkeys(inferred_likes[:6])))
            except Exception:  # noqa: BLE001
                pass
        taste_line = (f"LEARNED TASTES: {'; '.join(taste_parts)}\n" if taste_parts else "")
        return avoid, taste_line

    def week_plan(self):
        """Saved weekly meal plan with shopping_list freshly recomputed against current in-stock pantry items."""
        saved = self.store.kv_get(self.WEEK_KV)
        if not isinstance(saved, dict) or not saved.get("days"):
            return None
        out = dict(saved)
        out["shopping_list"] = build_shopping_list(out.get("days") or [], self.items())
        out["shopping_items"] = [x["label"] for x in out["shopping_list"]]
        return out

    def clear_week_plan(self):
        self.store.kv_set(self.WEEK_KV, None)
        return {"cleared": True}

    async def plan_week(self, days=7, meals=None, request="", model=None, viewer=None, start_date=None):
        """Build and save a multi-day meal plan (1-7 days) plus a consolidated, aisle-grouped shopping list
        computed deterministically against current pantry stock."""
        n_days = max(1, min(7, int(days or 7)))
        valid_slots = ("breakfast", "lunch", "dinner", "snack")
        if isinstance(meals, str):
            raw_slots = [s.strip().lower() for s in re.split(r"[,&+/\s]+", meals) if s.strip()]
        else:
            raw_slots = [str(s).strip().lower() for s in (meals or ()) if str(s).strip()]
        slots = [s for s in dict.fromkeys(raw_slots) if s in valid_slots] or ["dinner"]

        items = self.items()
        day_info = await self.today()
        g = self.goals()
        chosen = self._week_model(model)

        base_dt = start_date or datetime.now()
        day_labels = [(base_dt + timedelta(days=i)).strftime("%a %b %d").replace(" 0", " ") for i in range(n_days)]

        stock_lines = []
        for i in items:
            dl = i["days_left"]
            when = ("past its date" if dl is not None and dl < 0 else
                    "use today" if dl is not None and dl < 1 else
                    f"{dl:.0f} days left" if dl is not None else "")
            stock_lines.append(f"- {i['name']}{(' (' + i['amount'] + ')') if i['amount'] else ''}, {i['location']}, {when}")

        avoid_items, taste_line = self._week_prefs_context(viewer=viewer)
        protein_hint = (f"Protein target about {day_info['protein_target_g']} g/day.\n"
                        if day_info.get("protein_target_g") else "")
        content = (
            f"Plan {n_days} days: {', '.join(day_labels)}.\n"
            f"Meals per day: {', '.join(slots)}.\n"
            f"Goals: {'; '.join(g.get('goals') or [])}\n"
            + protein_hint
            + (f"Avoid: {', '.join(avoid_items)}\n" if avoid_items else "")
            + taste_line
            + (f"Notes: {g['notes']}\n" if g.get("notes") else "")
            + (f"Request: {request.strip()}\n" if (request or "").strip() else "")
            + "\nIN THE HOUSE (soonest to expire first — use these on early days):\n"
            + ("\n".join(stock_lines) or "- nothing recorded")
        )
        max_tok = min(4096, 900 + n_days * len(slots) * 260)
        r = await self.models.get(chosen).chat(
            WEEK_SYSTEM, [{"role": "user", "content": content}], max_tokens=max_tok, temperature=0.5,
        )
        parsed = parse_week_plan(r["text"])
        if not parsed["days"]:
            return {"summary": "", "days": [], "shopping_list": [], "shopping_items": [],
                    "model": chosen, "raw": r["text"][:1500]}

        # Normalize day labels cleanly to match requested dates
        for idx, d in enumerate(parsed["days"][:n_days]):
            if idx < len(day_labels) and not d.get("day"):
                d["day"] = day_labels[idx]
        parsed["days"] = parsed["days"][:n_days]

        plan = {
            "summary": parsed["summary"],
            "days": parsed["days"],
            "slots": slots,
            "request": (request or "").strip(),
            "model": chosen,
            "created": time.time(),
        }
        self.store.kv_set(self.WEEK_KV, plan)
        out = dict(plan)
        out["shopping_list"] = build_shopping_list(out["days"], items)
        out["shopping_items"] = [x["label"] for x in out["shopping_list"]]
        return out

    async def swap_meal(self, day_index, slot="dinner", request="", model=None, viewer=None):
        """Replace ONE meal in the saved weekly meal plan and recompute the consolidated shopping list."""
        saved = self.store.kv_get(self.WEEK_KV)
        if not isinstance(saved, dict) or not saved.get("days"):
            raise ValueError("no weekly meal plan saved yet")
        idx = int(day_index)
        if idx < 0 or idx >= len(saved["days"]):
            raise ValueError(f"day_index must be between 0 and {len(saved['days']) - 1}")

        target_day = saved["days"][idx]
        slot_norm = str(slot or "dinner").strip().lower()
        existing_pos = next((i for i, m in enumerate(target_day.get("meals") or ())
                             if str(m.get("slot") or "").lower() == slot_norm), 0)
        old_meal = (target_day.get("meals") or [{}])[existing_pos] if target_day.get("meals") else {}
        other_meals = [
            f"{d['day']} ({m.get('slot', 'dinner')}): {m['name']}"
            for di, d in enumerate(saved["days"])
            for mi, m in enumerate(d.get("meals") or ())
            if not (di == idx and mi == existing_pos)
        ]

        items = self.items()
        g = self.goals()
        chosen = self._week_model(model or saved.get("model"))
        avoid_items, taste_line = self._week_prefs_context(viewer=viewer)
        stock_lines = [f"- {i['name']}{(' (' + i['amount'] + ')') if i['amount'] else ''} ({i['location']})"
                       for i in items[:30]]
        content = (
            f"Replace {target_day['day']} {slot_norm}: currently '{old_meal.get('name', '')}'.\n"
            + (f"User wants instead: {request.strip()}\n" if (request or "").strip() else "Give a different appealing option.\n")
            + f"Goals: {'; '.join(g.get('goals') or [])}\n"
            + (f"Avoid: {', '.join(avoid_items)}\n" if avoid_items else "")
            + taste_line
            + "Other meals already planned this week (do NOT repeat these):\n"
            + ("\n".join(f"- {x}" for x in other_meals) or "- none")
            + "\n\nIN THE HOUSE:\n" + ("\n".join(stock_lines) or "- nothing recorded")
        )
        r = await self.models.get(chosen).chat(
            SWAP_SYSTEM, [{"role": "user", "content": content}], max_tokens=650, temperature=0.6,
        )
        m_json = re.search(r"\{.*\}", r.get("text") or "", re.S)
        try:
            obj = json.loads(m_json.group(0)) if m_json else {}
        except ValueError:
            obj = {}
        if not isinstance(obj, dict) or not str(obj.get("name") or "").strip():
            raise RuntimeError("model did not return a replacement meal")
        new_meal = {
            "slot": str(obj.get("slot") or slot_norm).lower(),
            "name": str(obj["name"]).strip(),
            "uses": [str(x).strip() for x in (obj.get("uses") or ()) if str(x or "").strip()],
            "need": obj.get("need") if isinstance(obj.get("need"), list) else list(obj.get("missing") or ()),
            "protein_g": obj.get("protein_g"),
            "calories": obj.get("calories"),
            "why": str(obj.get("why") or "").strip(),
            "steps": obj.get("steps") or "",
        }
        days_copy = [dict(d, meals=list(d.get("meals") or ())) for d in saved["days"]]
        if days_copy[idx]["meals"]:
            days_copy[idx]["meals"][existing_pos] = new_meal
        else:
            days_copy[idx]["meals"].append(new_meal)
        updated = dict(saved, days=days_copy, updated=time.time())
        self.store.kv_set(self.WEEK_KV, updated)
        out = dict(updated)
        out["shopping_list"] = build_shopping_list(out["days"], items)
        out["shopping_items"] = [x["label"] for x in out["shopping_list"]]
        out["swapped"] = {"day_index": idx, "day": target_day["day"], "meal": new_meal}
        return out

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
            when = ("past its date" if dl is not None and dl < 0 else
                    "use today" if dl is not None and dl < 1 else
                    f"{dl:.0f} days left" if dl is not None else "")
            out.append(f"- {i['name']}{(' (' + i['amount'] + ')') if i['amount'] else ''}: {i['location']}, {when}, "
                       f"added {datetime.fromtimestamp(i['added']):%b %d}")
        return "\n".join(out)


PANTRY_Q = re.compile(
    r"\b(?:pantry|fridge|refrigerator|freezer|groceries|grocery|food|eat|eating|cook|cooking|recipe|recipes|"
    r"meal|meals|make (?:for|with)|dinner|lunch|breakfast|snack|expire|expiring|expires|expired|go bad|going bad|"
    r"use up|do (?:i|we) have|have any|left in|ingredients?)\b", re.I)

NOT_MEAL_IDEA_Q = re.compile(
    r"^\s*(?:remember|log|note|ate|i ate|i had|i bought|i used|i finished|i tossed|used|finished|tossed|"
    r"why|how many|how much)\b", re.I)

MEAL_IDEA_Q = re.compile(
    r"\bwhat (?:can|could|should) (?:i|we) (?:make|cook|eat|have|prepare|fix|whip up)\b|"
    r"\b(?:suggest|recommend|give me|got any|any)\b.{0,40}\b(?:meals?|recipes?|breakfast|lunch|dinner|snacks?|"
    r"dish(?:es)?|something to (?:eat|cook|make))\b|"
    r"\b(?:meal|dinner|lunch|breakfast|snack|cooking|recipe) ideas?\b|"
    r"\b(?:a )?recipes? (?:for|with|using)\b|"
    r"\bwhat to (?:make|cook|eat)\b", re.I)

MEAL_SLOT_Q = re.compile(r"\b(breakfast|lunch|dinner|snack)\b", re.I)

WEEK_PLAN_Q = re.compile(
    r"\b(?:weekly|week(?:'s)?)\s+(?:meal|dinner|lunch|breakfast|food)\s+plan\b|"
    r"\b(?:plan|build|make|create|draft|put together)\b.{0,35}\b(?:meals?|dinners?|lunches?|menus?)\b.{0,25}\b(?:for the week|this week|next week|for \d+ days?)\b|"
    r"\bmeal\s+plan\b.{0,25}\b(?:for the week|this week|next week|\d+\s+days?|shopping list|grocer(?:y|ies))\b|"
    r"\b(?:help me )?(?:plan|build|make)\b.{0,25}\b(?:a )?meal\s+plan\b",
    re.I,
)


def week_plan_request(question):
    """If the question asks to build a multi-day / weekly meal plan (and shopping list), return
    {'days', 'meals', 'request', 'auto_shop'}, else None."""
    q = (question or "").replace("’", "'").strip()
    if not q or len(q) > 280 or NOT_MEAL_IDEA_Q.search(q) or not WEEK_PLAN_Q.search(q):
        return None
    dm = re.search(r"\b([1-7])\s+days?\b", q, re.I)
    days = int(dm.group(1)) if dm else 7
    slots = [m.lower() for m in re.findall(r"\b(breakfast|lunch|dinner)\b", q, re.I)]
    slots = list(dict.fromkeys(slots)) or ["dinner"]
    auto_shop = bool(re.search(r"\b(?:add|send|put)\b.{0,25}\bshopping list\b", q, re.I))
    return {"days": days, "meals": slots, "request": q.rstrip("?!. "), "auto_shop": auto_shop}


def format_week_plan(plan, voice=False):
    """Format a weekly meal plan + consolidated shopping list for screen or voice."""
    days = [d for d in ((plan or {}).get("days") or ()) if isinstance(d, dict) and d.get("meals")]
    if not days:
        return None
    shop = (plan or {}).get("shopping_list") or []
    if voice:
        first = days[0]["meals"][0]["name"]
        second = days[1]["meals"][0]["name"] if len(days) > 1 else ""
        lead = f"I built a {len(days)}-day meal plan starting {days[0]['day']} with {first}"
        if second:
            lead += f", then {second} on {days[1]['day']}"
        lead += "."
        if shop:
            lead += f" Your shopping list has {len(shop)} items. Open the Food tab to swap any meal or send the list to Home Assistant."
        else:
            lead += " You already have everything you need in stock."
        return lead

    lines = []
    if (plan or {}).get("summary"):
        lines.append(plan["summary"])
    for d in days:
        m_bits = []
        for m in d["meals"]:
            tag = f"{m.get('slot', 'dinner').capitalize()}: " if len(d["meals"]) > 1 else ""
            macros = f" (~{round(m['protein_g'])} g protein)" if isinstance(m.get("protein_g"), (int, float)) else ""
            m_bits.append(f"{tag}{m['name']}{macros}")
        lines.append(f"• {d['day']} — " + " | ".join(m_bits))
    if shop:
        lines.append("")
        lines.append(f"Shopping list ({len(shop)} items needed beyond what's in stock):")
        by_aisle = {}
        for x in shop:
            by_aisle.setdefault(x.get("aisle") or "other", []).append(x["label"])
        for aisle in AISLE_ORDER:
            if aisle in by_aisle:
                lines.append(f"- {aisle.title()}: " + ", ".join(by_aisle[aisle]))
    else:
        lines.append("\nYou already have everything in stock for this plan.")
    return "\n".join(lines)


def meal_request(question):
    """If the question asks for meal/recipe ideas from what's at home, return {"meal", "request"}, else None."""
    q = (question or "").replace("’", "'").strip()
    if not q or len(q) > 240 or NOT_MEAL_IDEA_Q.search(q) or not MEAL_IDEA_Q.search(q):
        return None
    m = MEAL_SLOT_Q.search(q)
    return {"meal": m.group(1).lower() if m else None, "request": q.rstrip("?!. ")}


def format_plan(plan, voice=False):
    """Turn a Pantry.suggest() plan into a clear answer for the Home bar, Talk, or voice. Returns None if empty."""
    meals = [m for m in ((plan or {}).get("meals") or []) if isinstance(m, dict) and m.get("name")]
    if not meals:
        return None
    if voice:
        parts = []
        for m in meals[:2]:
            bits = [m["name"]]
            if isinstance(m.get("protein_g"), (int, float)):
                bits.append(f"about {round(m['protein_g'])} grams of protein")
            uses = [str(u) for u in (m.get("uses") or []) if u][:3]
            if uses:
                bits.append("using " + ", ".join(uses))
            parts.append(", ".join(bits))
        lead = f"You could make {parts[0]}." + (f" Or {parts[1]}." if len(parts) > 1 else "")
        return lead + " Want the steps?"
    blocks = []
    for i, m in enumerate(meals[:3], 1):
        macros = []
        if isinstance(m.get("protein_g"), (int, float)):
            macros.append(f"~{round(m['protein_g'])} g protein")
        if isinstance(m.get("calories"), (int, float)):
            macros.append(f"~{round(m['calories'])} kcal")
        head = f"{i}. {m['name']}" + (f" ({', '.join(macros)} est.)" if macros else "")
        lines = [head]
        uses = [str(u) for u in (m.get("uses") or []) if u]
        why = (m.get("why") or "").strip()
        if uses and why:
            lines.append(f"Uses: {', '.join(uses)} — {why}")
        elif uses:
            lines.append(f"Uses: {', '.join(uses)}")
        elif why:
            lines.append(why)
        missing = [str(x) for x in (m.get("missing") or []) if x]
        if missing:
            lines.append(f"Need: {', '.join(missing)}")
        steps = (m.get("steps") or "").strip()
        if steps:
            lines.append(f"Steps: {steps}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)

