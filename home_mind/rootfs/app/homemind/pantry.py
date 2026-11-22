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
    "Never include any listed ALLERGY item; lean toward LEARNED TASTES when they fit what's in stock. "
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

WEEK_STYLES = ("fresh", "twice", "prep")
EFFORT_LEVELS = ("reheat", "quick", "normal", "big", "skip")
WEEK_SLOTS = ("breakfast", "lunch", "dinner")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

CAL_MEAL_RE = re.compile(
    r"\b(?:dinner|supper|lunch|brunch|breakfast|restaurant|bistro|reservation|potluck|banquet|party|"
    r"wedding|happy hour|flight|travel|out of town|vacation|eating out|takeout)\b",
    re.I,
)

WEEK_SYSTEM = (
    "You build a practical multi-day meal plan for a household from what they already have at home plus a concise "
    "grocery list. Put foods expiring soonest on the earliest days. Overlap fresh ingredients across days so nothing "
    "goes to waste. Respect their goals, Avoid list, and any ALLERGY items strictly (never include an allergen). "
    "Lean toward LEARNED TASTES when they fit. Honor each meal's requested effort tier (reheat <=5 min microwave or "
    "packable no-cook ONLY — NEVER require stove/oven cooking on a reheat slot, reuse an earlier cook/prep batch with "
    "is_cook: false and the same batch_id or give a cold grab-and-go meal; quick <=20 min; normal <=40 min; "
    "big 60+ min makes extra) and cooking style:\n"
    "- fresh: cook a fresh meal each time (except slots explicitly marked reheat, which must be no-cook/packable or reheated leftovers).\n"
    "- twice: cook once, eat twice — make double batches on cook sessions (batch_id 'A','B','C','D', is_cook: true, "
    "servings: 2-4) and reuse them as quick 5-min leftover/reheat meals on later slots (same batch_id, is_cook: false, "
    "short batch_label like 'Mon chili').\n"
    "- prep: one big prep session covers multiple meals through the week (include prep_plan with title and batch items).\n"
    "In 'need', list ONLY non-staple items to buy as compact 'qty unit item' strings (skip salt, pepper, oil, and anything in stock). "
    "For leftover/reheat meals (is_cook: false), ONLY list extra add-ons not already bought for the parent cook session (usually []). "
    "Be ultra-compact (no prose, no steps, short meal names under 5 words) so the JSON never hits output token limits. "
    'Reply with JSON only: {"summary": "short 1-line theme", '
    '"prep_plan": {"title": "optional e.g. Sun prep · ~2 h", "items": [{"batch_id": "A", "text": "short line"}]}, '
    '"days": [{"day": "Mon Nov 23", "meals": [{"slot": "dinner", "name": "short name", '
    '"minutes": 25, "effort": "reheat|quick|normal|big", "batch_id": "optional A", "batch_label": "optional Mon chili", '
    '"is_cook": true, "servings": 2, "uses": ["stock item"], "need": ["2 lb chicken", "1 head broccoli"], '
    '"protein_g": 40, "calories": 550}]}]}')

SWAP_SYSTEM = (
    "You replace ONE meal in an existing weekly meal plan. Pick a fresh option that does NOT repeat any meal already "
    "planned on other days, reuses ingredients already planned or in stock where sensible, respects the requested effort "
    "tier (for 'reheat', give ONLY a microwave-reheat leftover or cold packable no-cook meal <=5 min — zero stove/oven cooking), "
    "goals, Avoid items, ALLERGY items strictly, and any user tweak request. Keep fields short. "
    'Reply with JSON only: {"slot": "dinner", "name": "short meal name", "minutes": 25, "effort": "reheat|quick|normal|big", '
    '"uses": ["in-stock items used"], '
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


def _parse_need_item(raw):
    if isinstance(raw, dict):
        return (str(raw.get("item") or raw.get("name") or "").strip(),
                str(raw.get("amount") or "").strip(),
                str(raw.get("aisle") or "").strip())
    s = str(raw or "").strip()
    if not s:
        return "", "", ""
    q_m = re.match(r"^(\d+(?:[./]\d+)?\s*(?:lb|lbs|oz|g|kg|cup|cups|tbsp|tsp|can|cans|head|heads|bunch|bunches|clove|cloves|pack|packs|bag|bags|dozen|box|boxes|jar|jars|bottle|bottles)?)\s+(.+)$", s, re.I)
    if q_m and len(q_m.group(2).strip()) >= 2:
        return q_m.group(2).strip(), q_m.group(1).strip(), ""
    return s, "", ""


def build_shopping_list(days_list, in_stock_items=()):
    """Deterministically build a deduplicated, aisle-grouped shopping list from a weekly plan,
    subtracting fresh items already in stock, skipping skipped meals, avoiding double-counting
    ingredients already included in a batch's cook session, and merging matching quantities."""
    stock_stems = {
        _stem(i.get("name") if isinstance(i, dict) else str(i))
        for i in (in_stock_items or ())
        if not (isinstance(i, dict) and i.get("days_left") is not None and i["days_left"] < 0)
    }
    stock_stems.discard("")

    # Pass 1: record ingredient stems already bought for each batch's cook session (is_cook != False)
    batch_cook_stems = {}
    for d in (days_list or ()):
        if not isinstance(d, dict):
            continue
        for m in (d.get("meals") or ()):
            if not isinstance(m, dict) or m.get("skipped"):
                continue
            bid = str(m.get("batch_id") or "").strip().upper()
            if bid and m.get("is_cook", True) is not False:
                needs = m.get("need") if m.get("need") is not None else (m.get("missing") or [])
                stems = batch_cook_stems.setdefault(bid, set())
                for raw in needs:
                    item, _, _ = _parse_need_item(raw)
                    st = _stem(item)
                    if st:
                        stems.add(st)

    grouped = {}
    for d in (days_list or ()):
        if not isinstance(d, dict):
            continue
        day_lbl = (d.get("day") or "").split()[0] or "Day"
        for m in (d.get("meals") or ()):
            if not isinstance(m, dict) or m.get("skipped"):
                continue
            m_name = (m.get("name") or "").strip()
            bid = str(m.get("batch_id") or "").strip().upper()
            is_leftover = bool(bid) and (m.get("is_cook") is False)
            parent_stems = batch_cook_stems.get(bid, set()) if is_leftover else set()
            needs = m.get("need") if m.get("need") is not None else (m.get("missing") or [])
            for raw in needs:
                item, amt, aisle = _parse_need_item(raw)
                if not item or _in_stock_match(item, stock_stems):
                    continue
                key = _stem(item) or item.lower()
                # Leftover/reheat cells shouldn't double-count ingredients already in the parent cook session
                if is_leftover and key in parent_stems:
                    continue
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


def _clean_meal_dict(ml, default_slot="dinner"):
    bid = str(ml.get("batch_id") or "").strip().upper()[:2] or None
    is_cook = ml.get("is_cook")
    eff = str(ml.get("effort") or "").strip().lower()
    if eff not in ("reheat", "quick", "normal", "big"):
        eff = ""
    if is_cook is None:
        is_cook = False if eff == "reheat" else (True if not bid else bool(ml.get("cook", True)))
    else:
        is_cook = bool(is_cook)
    mins = ml.get("minutes")
    try:
        mins = int(round(float(mins))) if mins is not None and str(mins).strip() != "" else None
    except (TypeError, ValueError):
        mins = None
    sv = ml.get("servings")
    try:
        sv = int(round(float(sv))) if sv is not None and str(sv).strip() != "" else None
    except (TypeError, ValueError):
        sv = None
    out = {
        "slot": str(ml.get("slot") or ml.get("meal") or default_slot).strip().lower(),
        "name": str(ml["name"]).strip(),
        "uses": [str(x).strip() for x in (ml.get("uses") or ()) if str(x or "").strip()],
        "need": ml.get("need") if isinstance(ml.get("need"), list) else list(ml.get("missing") or ()),
        "protein_g": ml.get("protein_g"),
        "calories": ml.get("calories"),
        "why": str(ml.get("why") or "").strip(),
        "steps": ml.get("steps") or "",
    }
    if mins is not None and mins > 0:
        out["minutes"] = mins
    if eff:
        out["effort"] = eff
    if bid:
        out["batch_id"] = bid
        out["is_cook"] = is_cook
        if ml.get("batch_label"):
            out["batch_label"] = str(ml["batch_label"]).strip()
    if sv is not None and sv > 0:
        out["servings"] = sv
    return out


def parse_week_plan(text):
    """Parse the weekly meal plan JSON cleanly (tolerating surrounding prose or markdown fences)."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {"summary": "", "days": [], "prep_plan": None}
    try:
        obj = json.loads(m.group(0))
        if not isinstance(obj, dict):
            return {"summary": "", "days": [], "prep_plan": None}
    except ValueError:
        return {"summary": "", "days": [], "prep_plan": None}
    days_out = []
    for d in (obj.get("days") or ()):
        if not isinstance(d, dict):
            continue
        meals = []
        for ml in (d.get("meals") or ()):
            if isinstance(ml, dict) and str(ml.get("name") or "").strip():
                meals.append(_clean_meal_dict(ml))
        if meals or d.get("day"):
            days_out.append({"day": str(d.get("day") or f"Day {len(days_out) + 1}").strip(), "meals": meals})
    prep_raw = obj.get("prep_plan")
    prep_plan = None
    if isinstance(prep_raw, dict) and (prep_raw.get("items") or prep_raw.get("title")):
        pitems = []
        for it in (prep_raw.get("items") or ()):
            if isinstance(it, dict) and str(it.get("text") or "").strip():
                pitems.append({
                    "batch_id": str(it.get("batch_id") or "").strip().upper()[:2],
                    "text": str(it["text"]).strip(),
                })
            elif isinstance(it, str) and it.strip():
                pitems.append({"batch_id": "", "text": it.strip()})
        if pitems or prep_raw.get("title"):
            prep_plan = {"title": str(prep_raw.get("title") or "Prep session").strip(), "items": pitems}
    return {"summary": str(obj.get("summary") or "").strip(), "days": days_out, "prep_plan": prep_plan}


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
            if d["use_by"] is None:
                d["days_left"] = None
            else:
                raw = (d["use_by"] - now) / 86400
                d["days_left"] = min(0.9, round(raw, 1)) if 0 <= raw < 1 else round(raw, 1)
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
    def _graph_prefs(self, viewer=None):
        """Pull active FOOD/HEALTH preferences, dislikes, and hard allergies from MemGraph (respecting viewer privacy)."""
        mg = getattr(self, "memgraph", None)
        if mg is None:
            return [], [], [], [], []
        from .speakers import VIEWER
        who = VIEWER.get() if viewer is None else viewer
        seen = {}
        try:
            for lnk in mg.query(categories=["FOOD", "HEALTH"], viewer=who, include_dormant=False, limit=24, record_access=False):
                seen[lnk["id"]] = lnk
            for lnk in mg.query(relations=["ALLERGIC_TO"], viewer=who, include_dormant=False, limit=12, record_access=False):
                seen[lnk["id"]] = lnk
        except Exception:  # noqa: BLE001 - graph lookup never breaks meal planning
            return [], [], [], [], []

        allergies, dislikes, likes_direct, likes_inferred, used_links = [], [], [], [], []
        for lnk in seen.values():
            rel = lnk.get("relation")
            tgt = (lnk.get("target_name") or "").strip()
            if not tgt or lnk.get("status") == "CONTESTED":
                continue
            s_val = float(lnk.get("effective_stability", lnk.get("stability_score", 0.0)))
            if rel == "ALLERGIC_TO":
                if tgt not in allergies:
                    allergies.append(tgt)
                used_links.append(lnk)
            elif rel in ("LIKES", "PREFERS") and lnk.get("status") in ("FACT", "IDEA"):
                if lnk.get("signed") and s_val < 0:
                    if tgt not in dislikes:
                        dislikes.append(tgt)
                    used_links.append(lnk)
                elif s_val > 0:
                    if lnk.get("evidence") == "direct":
                        if tgt not in likes_direct:
                            likes_direct.append(tgt)
                    else:
                        if tgt not in likes_inferred:
                            likes_inferred.append(tgt)
                    used_links.append(lnk)
        return allergies[:8], dislikes[:8], likes_direct[:6], likes_inferred[:6], used_links

    async def suggest(self, meal=None, model=None, n=3, request="", viewer=None):
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

        allergies, dislikes, likes_direct, likes_inferred, g_links = self._graph_prefs(viewer=viewer)
        avoid_items = list(g.get("avoid") or [])
        seen_avoid = {a.lower() for a in avoid_items}
        for al in allergies:
            if al.lower() not in seen_avoid:
                avoid_items.insert(0, f"ALLERGY: {al}")
                seen_avoid.add(al.lower())
        for dl in dislikes:
            if dl.lower() not in seen_avoid:
                avoid_items.append(f"dislike: {dl}")
                seen_avoid.add(dl.lower())

        taste_parts = []
        if likes_direct:
            taste_parts.append("stated likes: " + ", ".join(likes_direct))
        if likes_inferred:
            taste_parts.append("observed cuisine/flavor patterns: " + ", ".join(likes_inferred))
        taste_line = (f"LEARNED TASTES (from memory graph — lean toward these when they fit): {'; '.join(taste_parts)}\n"
                      if taste_parts else "")

        content = (f"Today: {datetime.now():%A %b %d, %I:%M %p}\n"
                   f"Today's focus: {'; '.join(focus)}\n"
                   f"Goals: {'; '.join(g.get('goals') or [])}\n"
                   + (f"Avoid: {', '.join(avoid_items)}\n" if avoid_items else "")
                   + taste_line
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
        elif g_links and getattr(self, "memgraph", None) is not None:
            try:
                mg = self.memgraph
                plan_text = json.dumps(plan["meals"])
                shown = set(mg.used_links(plan_text, g_links))
                # Always credit hard safety constraints (allergies/dislikes) that shaped the meal plan.
                for lnk in g_links:
                    if lnk.get("relation") == "ALLERGIC_TO" or float(lnk.get("stability_score", 0.0)) < 0:
                        shown.add(lnk["id"])
                if shown:
                    mg.mark_shown(list(shown))
            except Exception:  # noqa: BLE001
                pass
        return plan

    # ---- weekly meal plan + smart shopping list -----------------------------------------------------------------------
    WEEK_KV = "pantry_week_plan"
    WEEK_LAYOUT_LAST_KV = "pantry_week_layout_last"
    WEEK_LAYOUT_USUAL_KV = "pantry_week_layout_usual"

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
        allergies, dislikes, likes_direct, likes_inferred, _ = self._graph_prefs(viewer=viewer)
        for al in allergies:
            if al.lower() not in seen:
                avoid.insert(0, f"ALLERGY: {al}")
                seen.add(al.lower())
        for dl in dislikes:
            if dl.lower() not in seen:
                avoid.append(f"dislike: {dl}")
                seen.add(dl.lower())
        taste_parts = []
        if likes_direct:
            taste_parts.append("stated likes: " + ", ".join(likes_direct))
        if likes_inferred:
            taste_parts.append("observed cuisine/flavor patterns: " + ", ".join(likes_inferred))
        taste_line = (f"LEARNED TASTES: {'; '.join(taste_parts)}\n" if taste_parts else "")
        return avoid, taste_line

    def calendar_skips(self, days=7, start_date=None):
        """Scan synced Home Assistant calendars over the next `days` for dining/travel conflicts.
        Returns {'day_index|slot': 'Event title', ...}."""
        cals = getattr(self, "calendars", None)
        if cals is None:
            return {}
        base = start_date.date() if isinstance(start_date, datetime) else (start_date or datetime.now().date())
        skips = {}
        for i in range(max(1, min(7, int(days or 7)))):
            d = base + timedelta(days=i)
            try:
                evs = cals.events_on(d) or []
            except Exception:  # noqa: BLE001 - calendar lookup never breaks meal planning
                continue
            for ev in evs:
                meta = ev.get("meta") or {}
                title = (ev.get("title") or "").strip()
                body = (ev.get("body") or "").strip()
                if not title and body:
                    m = re.search(r"\):\s*([^(\n]+)", body)
                    title = (m.group(1) if m else body).strip()
                hay = f"{title} {body}"
                if not CAL_MEAL_RE.search(hay):
                    continue
                low = hay.lower()
                slots_hit = []
                if re.search(r"\bbreakfast\b", low):
                    slots_hit.append("breakfast")
                if re.search(r"\b(?:lunch|brunch)\b", low):
                    slots_hit.append("lunch")
                if re.search(r"\b(?:dinner|supper)\b", low):
                    slots_hit.append("dinner")
                if not slots_hit:
                    if meta.get("all_day") and re.search(r"\b(?:flight|travel|out of town|vacation)\b", low):
                        slots_hit = list(WEEK_SLOTS)
                    elif meta.get("start"):
                        try:
                            st = datetime.fromisoformat(meta["start"])
                            hm = st.hour * 60 + st.minute
                            if 360 <= hm < 630:
                                slots_hit.append("breakfast")
                            elif 660 <= hm <= 870:
                                slots_hit.append("lunch")
                            elif 1020 <= hm <= 1290:
                                slots_hit.append("dinner")
                        except ValueError:
                            pass
                clean_lbl = re.sub(r"\s+", " ", title or "Calendar event")[:48]
                for sl in slots_hit:
                    skips.setdefault(f"{i}|{sl}", clean_lbl)
        return skips

    def week_layouts(self):
        """Return saved 'last' and 'usual' weekly picker layouts (keyed by weekday so they map to any start day)."""
        last = self.store.kv_get(self.WEEK_LAYOUT_LAST_KV)
        usual = self.store.kv_get(self.WEEK_LAYOUT_USUAL_KV)
        return {
            "last": last if isinstance(last, dict) and last.get("grid") else None,
            "usual": usual if isinstance(usual, dict) and usual.get("grid") else None,
        }

    def save_week_layout(self, kind="usual", style="twice", grid=None, start_date=None, prep_day=None, prep_time=None):
        """Persist a weekly effort/skip layout ('usual' or 'last'), normalizing index keys to weekday names."""
        if kind not in ("usual", "last"):
            raise ValueError("kind must be 'usual' or 'last'")
        st_norm = str(style or "twice").strip().lower()
        if st_norm not in WEEK_STYLES:
            raise ValueError(f"style must be one of {', '.join(WEEK_STYLES)}")
        if not isinstance(grid, dict) or not grid:
            raise ValueError("grid must be a non-empty object")
        base = start_date.date() if isinstance(start_date, datetime) else (start_date or datetime.now().date())
        by_wd = {}
        for k, v in grid.items():
            if not isinstance(k, str) or "|" not in k:
                continue
            day_part, sl = k.split("|", 1)
            sl = sl.strip().lower()
            val = str(v or "").strip().lower()
            if sl not in WEEK_SLOTS or val not in EFFORT_LEVELS:
                continue
            if day_part.isdigit():
                idx = int(day_part)
                if 0 <= idx < 7:
                    wd = WEEKDAYS[(base + timedelta(days=idx)).weekday()]
                    by_wd[f"{wd}|{sl}"] = val
            elif day_part.lower() in WEEKDAYS:
                by_wd[f"{day_part.lower()}|{sl}"] = val
        if not by_wd:
            raise ValueError("grid had no valid meal cells")
        end_d = base + timedelta(days=6)
        sub = (f"{base.strftime('%b %d').replace(' 0', ' ')}–{end_d.strftime('%b %d').replace(' 0', ' ')}"
               if kind == "last" else f"saved {datetime.now().strftime('%b %d').replace(' 0', ' ')}")
        pd_norm = self._norm_prep_day(prep_day, base)[0] if prep_day not in (None, "") else None
        pt_norm = self._norm_prep_time(prep_time) if prep_time not in (None, "") else None
        rec = {"kind": kind, "style": st_norm, "grid": by_wd, "sub": sub, "saved_at": time.time()}
        if pd_norm:
            rec["prep_day"] = pd_norm
        if pt_norm:
            rec["prep_time"] = pt_norm
        self.store.kv_set(self.WEEK_LAYOUT_USUAL_KV if kind == "usual" else self.WEEK_LAYOUT_LAST_KV, rec)
        return rec

    @staticmethod
    def _norm_prep_day(prep_day, base_date, n_days=7):
        """Resolve prep_day ('sun', '0', '2026-11-22', 'Sun Nov 22') -> (weekday, date_iso, label) or (None, None, None)."""
        if prep_day in (None, ""):
            return None, None, None
        raw = str(prep_day).strip().lower()
        target_d = None
        if raw.isdigit():
            idx = int(raw)
            if 0 <= idx < n_days:
                target_d = base_date + timedelta(days=idx)
        elif raw[:3] in WEEKDAYS:
            wd_idx = WEEKDAYS.index(raw[:3])
            for i in range(n_days):
                cand = base_date + timedelta(days=i)
                if cand.weekday() == wd_idx:
                    target_d = cand
                    break
            if target_d is None:
                delta = (wd_idx - base_date.weekday()) % 7
                target_d = base_date + timedelta(days=delta)
        else:
            try:
                target_d = datetime.fromisoformat(raw[:10]).date()
            except ValueError:
                return None, None, None
        if target_d is None:
            return None, None, None
        return (
            WEEKDAYS[target_d.weekday()],
            target_d.isoformat(),
            target_d.strftime("%a %b %d").replace(" 0", " "),
        )

    @staticmethod
    def _norm_prep_time(prep_time):
        m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(prep_time or ""))
        if not m:
            return "15:00"
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return f"{h:02d}:{mn:02d}"
        return "15:00"

    def week_calendar(self, days=7, start_date=None):
        """Metadata for the 7x3 Food tab meal calendar picker: dates, calendar auto-skips, and saved layouts."""
        base = start_date.date() if isinstance(start_date, datetime) else (start_date or datetime.now().date())
        n = max(1, min(7, int(days or 7)))
        day_rows = []
        for i in range(n):
            d = base + timedelta(days=i)
            day_rows.append({
                "index": i,
                "date": d.isoformat(),
                "weekday": WEEKDAYS[d.weekday()],
                "short": d.strftime("%a"),
                "day_num": d.day,
                "label": d.strftime("%a %b %d").replace(" 0", " "),
            })
        return {
            "start_date": base.isoformat(),
            "days": day_rows,
            "calendar_skips": self.calendar_skips(days=n, start_date=base),
            "layouts": self.week_layouts(),
        }

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

    def _infer_style(self, style, request):
        if style:
            s = str(style).strip().lower()
            if s not in WEEK_STYLES:
                raise ValueError(f"style must be one of {', '.join(WEEK_STYLES)}")
            return s
        low = (request or "").lower()
        if re.search(r"\b(?:prep day|meal prep|batch prep|sunday prep)\b", low):
            return "prep"
        if re.search(r"\b(?:cook once|eat twice|leftover|double batch)\b", low):
            return "twice"
        return "fresh"

    @staticmethod
    def _is_token_limit_err(exc):
        low = str(exc or "").lower()
        return any(k in low for k in ("token limit", "max_tokens", "max tokens", "output length", "truncated"))

    @staticmethod
    def _chunk_day_indices(day_indices, meal_counts, max_meals=6):
        """Partition day indices into sequential chunks each containing <= max_meals (at least 1 day per chunk)."""
        chunks, cur, cur_n = [], [], 0
        for idx in day_indices:
            mc = meal_counts.get(idx, 1)
            if cur and cur_n + mc > max_meals:
                chunks.append(cur)
                cur, cur_n = [idx], mc
            else:
                cur.append(idx)
                cur_n += mc
        if cur:
            chunks.append(cur)
        return chunks

    async def plan_week(self, days=7, meals=None, request="", model=None, viewer=None, start_date=None,
                        style=None, grid=None, prep_day=None, prep_time=None):
        """Build and save a multi-day meal plan (1-7 days) plus a consolidated, aisle-grouped shopping list
        computed deterministically against current pantry stock. Supports either simple (days, meals)
        or the full 7x3 effort grid + cooking style ('fresh' | 'twice' | 'prep').
        Automatically chunks large weeks (>6 meals) and splits on output-token-limit errors so
        constrained endpoints (like Home Assistant AI Task) complete reliably."""
        chosen_style = self._infer_style(style, request)
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

        base_dt = start_date if isinstance(start_date, datetime) else (
            datetime.combine(start_date, datetime.min.time()) if start_date else datetime.now()
        )
        day_labels = [(base_dt + timedelta(days=i)).strftime("%a %b %d").replace(" 0", " ") for i in range(n_days)]

        # Normalize optional 7x3 effort grid ({'0|lunch': 'quick', '0|dinner': 'big', ...})
        norm_grid = {}
        day_sched = {}
        day_meal_counts = {}
        active_meal_count = 0
        if isinstance(grid, dict) and grid:
            for i in range(n_days):
                day_parts = []
                for sl in WEEK_SLOTS:
                    val = str(grid.get(f"{i}|{sl}") or "skip").strip().lower()
                    if val not in EFFORT_LEVELS:
                        raise ValueError(f"invalid effort '{val}' for {i}|{sl}")
                    norm_grid[f"{i}|{sl}"] = val
                    if val != "skip":
                        eff_desc = {
                            "reheat": "REHEAT/PACK ONLY <=5m (NO cooking — leftover from prior batch with is_cook: false OR cold grab-and-go)",
                            "quick": "quick <=20m",
                            "normal": "normal <=40m",
                            "big": "big cook 60m+",
                        }[val]
                        day_parts.append(f"{sl} ({eff_desc})")
                        active_meal_count += 1
                if day_parts:
                    day_sched[i] = f"- {day_labels[i]}: {', '.join(day_parts)}"
                    day_meal_counts[i] = len(day_parts)
            if active_meal_count == 0:
                raise ValueError("pick at least one meal to plan")
            slots = [sl for sl in WEEK_SLOTS if any(norm_grid.get(f"{i}|{sl}") != "skip" for i in range(n_days))]
            active_days = [i for i in range(n_days) if i in day_sched]
        else:
            active_days = list(range(n_days))
            for i in active_days:
                day_meal_counts[i] = len(slots)
            active_meal_count = n_days * len(slots)

        # Keep stock prompt crisp even for 90+ item pantries: detail top 40 expiring/fridge/freezer items, summarize rest
        stock_lines, extra_pantry = [], []
        for idx_i, i in enumerate(items):
            dl = i["days_left"]
            if idx_i < 40 or (dl is not None and dl <= 7) or i.get("location") in ("fridge", "freezer"):
                if len(stock_lines) < 48:
                    when = ("past its date" if dl is not None and dl < 0 else
                            "use today" if dl is not None and dl < 1 else
                            f"{dl:.0f} days left" if dl is not None else "")
                    stock_lines.append(f"- {i['name']}{(' (' + i['amount'] + ')') if i['amount'] else ''}, {i['location']}, {when}")
                    continue
            extra_pantry.append(i["name"])
        if extra_pantry:
            stock_lines.append("Also in pantry: " + ", ".join(extra_pantry[:50]))

        avoid_items, taste_line = self._week_prefs_context(viewer=viewer)
        protein_hint = (f"Protein target about {day_info['protein_target_g']} g/day.\n"
                        if day_info.get("protein_target_g") else "")
        style_desc = {
            "fresh": "fresh (cook a fresh meal each time)",
            "twice": "twice (cook once, eat twice — make double batches on cook sessions and reuse as 5-min leftovers on later slots)",
            "prep": "prep (one big prep session on a big-cook slot covers most meals of the week; include prep_plan)",
        }[chosen_style]

        prep_wd, prep_date_iso, prep_label = self._norm_prep_day(prep_day, base_dt.date(), n_days)
        if not prep_wd and chosen_style == "prep":
            # Default to first day with a big-cook slot, else Sunday if in range, else day 0
            big_idx = next((i for i in range(n_days) if any(norm_grid.get(f"{i}|{s}") == "big" for s in WEEK_SLOTS)), None)
            sun_idx = next((i for i in range(n_days) if (base_dt + timedelta(days=i)).weekday() == 6), None)
            pick_i = big_idx if big_idx is not None else (sun_idx if sun_idx is not None else 0)
            prep_wd, prep_date_iso, prep_label = self._norm_prep_day(str(pick_i), base_dt.date(), n_days)
        prep_time_norm = self._norm_prep_time(prep_time) if (prep_wd or chosen_style == "prep") else None
        prep_hint = (
            f"Prep day: {prep_label} at {prep_time_norm} — schedule the main batch-cooking prep_plan on {prep_label} "
            f"(title like '{prep_label.split()[0]} prep · ~90 min') so reheatable meals later in the week draw from batches prepped on {prep_label}.\n"
            if prep_label else ""
        )

        shared_ctx = (
            f"Cooking style: {style_desc}.\n"
            + prep_hint
            + f"Goals: {'; '.join(g.get('goals') or [])}\n"
            + protein_hint
            + (f"Avoid: {', '.join(avoid_items)}\n" if avoid_items else "")
            + taste_line
            + (f"Notes: {g['notes']}\n" if g.get("notes") else "")
            + (f"Request: {request.strip()}\n" if (request or "").strip() else "")
            + "\nIN THE HOUSE (soonest to expire first — use these on early days):\n"
            + ("\n".join(stock_lines) or "- nothing recorded")
        )

        model_obj = self.models.get(chosen)
        collected_days = []
        summaries = []
        prep_plan = None
        last_raw = ""
        prior_lines = []
        active_batches = []

        async def _run_slice(d_indices):
            nonlocal prep_plan, last_raw
            if day_sched:
                n_m = sum(day_meal_counts[i] for i in d_indices)
                sched_block = (
                    f"Plan ONLY these {n_m} requested slots across {len(d_indices)} day(s) (leave skipped slots out):\n"
                    + "\n".join(day_sched[i] for i in d_indices) + "\n"
                )
            else:
                lbls = [day_labels[i] for i in d_indices]
                sched_block = f"Plan {len(lbls)} day(s): {', '.join(lbls)}.\nMeals per day: {', '.join(slots)}.\n"

            prior_block = ""
            if prior_lines:
                prior_block = "Already planned earlier this week (do NOT repeat; reuse batches where fitting):\n" + "\n".join(prior_lines[-14:]) + "\n"
            if active_batches:
                prior_block += "Active batches you can reuse as is_cook=false leftovers: " + "; ".join(active_batches[-6:]) + "\n"

            content = sched_block + prior_block + shared_ctx
            n_m = sum(day_meal_counts[i] for i in d_indices)
            max_tok = min(4096, 800 + n_m * 180)
            try:
                r = await model_obj.chat(
                    WEEK_SYSTEM, [{"role": "user", "content": content}], max_tokens=max_tok, temperature=0.5,
                )
            except Exception as e:
                if len(d_indices) > 1 and self._is_token_limit_err(e):
                    mid = len(d_indices) // 2
                    log.warning("plan_week hit token limit on %d days; splitting %d + %d", len(d_indices), mid, len(d_indices) - mid)
                    await _run_slice(d_indices[:mid])
                    await _run_slice(d_indices[mid:])
                    return
                raise
            last_raw = r.get("text") or ""
            parsed = parse_week_plan(last_raw)
            if not parsed["days"] and len(d_indices) > 1:
                # Truncated JSON from a silent token cap -> split in half and retry
                mid = len(d_indices) // 2
                log.warning("plan_week unparseable on %d days; splitting %d + %d", len(d_indices), mid, len(d_indices) - mid)
                await _run_slice(d_indices[:mid])
                await _run_slice(d_indices[mid:])
                return
            if parsed["summary"]:
                summaries.append(parsed["summary"])
            if parsed.get("prep_plan") and not prep_plan:
                prep_plan = parsed["prep_plan"]
            for d in parsed["days"]:
                collected_days.append(d)
                for m in d.get("meals") or ():
                    prior_lines.append(f"- {d['day']} {m.get('slot', 'dinner')}: {m['name']}")
                    if m.get("batch_id") and m.get("is_cook", True) is not False:
                        active_batches.append(f"{m['batch_id']}={m['name']} (from {d['day']})")

        chunks = self._chunk_day_indices(active_days, day_meal_counts, max_meals=6) if active_meal_count > 6 else [active_days]
        for ch in chunks:
            await _run_slice(ch)

        if not collected_days:
            return {"summary": "", "days": [], "shopping_list": [], "shopping_items": [],
                    "model": chosen, "raw": last_raw[:1500]}

        if norm_grid:
            # Align returned days to exact 0..n_days-1 date slots so skipped days remain in grid order
            by_lbl = {str(d.get("day") or "").strip().lower(): d for d in collected_days}
            aligned = []
            for idx in range(n_days):
                lbl = day_labels[idx]
                matched = by_lbl.get(lbl.lower())
                if matched is None:
                    wd_prefix = lbl.split()[0].lower()
                    matched = next((d for d in collected_days
                                    if str(d.get("day") or "").strip().lower().startswith(wd_prefix)), None)
                meals_for_day = []
                if matched:
                    for ml in matched.get("meals") or ():
                        sl = str(ml.get("slot") or "dinner").lower()
                        req_eff = norm_grid.get(f"{idx}|{sl}", "skip")
                        if req_eff != "skip":
                            if req_eff == "reheat":
                                ml = dict(ml, effort="reheat", is_cook=False)
                            elif not ml.get("effort"):
                                ml = dict(ml, effort=req_eff)
                            meals_for_day.append(ml)
                aligned.append({"day": lbl, "date": (base_dt + timedelta(days=idx)).date().isoformat(), "meals": meals_for_day})
            final_days = aligned
            try:
                self.save_week_layout("last", chosen_style, norm_grid, start_date=base_dt.date(),
                                      prep_day=prep_wd, prep_time=prep_time_norm)
            except Exception:  # noqa: BLE001
                pass
        else:
            for idx, d in enumerate(collected_days[:n_days]):
                if idx < len(day_labels) and not d.get("day"):
                    d["day"] = day_labels[idx]
                if idx < n_days:
                    d.setdefault("date", (base_dt + timedelta(days=idx)).date().isoformat())
            final_days = collected_days[:n_days]
            for idx, d in enumerate(final_days):
                present = {str(m.get("slot") or "dinner").lower(): m.get("effort") or "normal" for m in d.get("meals") or ()}
                for sl in WEEK_SLOTS:
                    norm_grid[f"{idx}|{sl}"] = present.get(sl, "skip")

        plan = {
            "summary": summaries[0] if summaries else "",
            "days": final_days,
            "slots": slots,
            "style": chosen_style,
            "grid": norm_grid,
            "start_date": base_dt.date().isoformat(),
            "prep_plan": prep_plan,
            "prep_day": prep_wd,
            "prep_date": prep_date_iso,
            "prep_time": prep_time_norm,
            "calendar_skips": self.calendar_skips(days=n_days, start_date=base_dt.date()),
            "request": (request or "").strip(),
            "model": chosen,
            "created": time.time(),
        }
        self.store.kv_set(self.WEEK_KV, plan)
        out = dict(plan)
        out["shopping_list"] = build_shopping_list(out["days"], items)
        out["shopping_items"] = [x["label"] for x in out["shopping_list"]]
        return out

    async def add_to_calendar(self, include="prep", entity=None, prep_time=None):
        """Create Home Assistant calendar event(s) for the saved weekly meal plan ('prep' session only, or 'all' meals)."""
        saved = self.week_plan()
        if not saved:
            raise ValueError("no weekly meal plan saved yet")
        if not (self.ha and getattr(self.ha, "available", False)):
            raise RuntimeError("Home Assistant is not reachable")
        inc = str(include or "prep").strip().lower()
        if inc not in ("prep", "all"):
            raise ValueError("include must be 'prep' or 'all'")

        cal_eid = (entity or "").strip() or None
        cal_name = cal_eid
        if not cal_eid:
            states = await self.ha.states() or []
            cals = [(s["entity_id"], (s.get("attributes") or {}).get("friendly_name") or s["entity_id"])
                    for s in states if str(s.get("entity_id", "")).startswith("calendar.")]
            if not cals:
                raise RuntimeError("no Home Assistant calendar entity found")
            skip = re.compile(r"journal|holiday|birthday|week_number|trash|garbage", re.I)
            picked = next(((e, n) for e, n in cals if not skip.search(f"{e} {n}")), cals[0])
            cal_eid, cal_name = picked

        created = []
        days_list = saved.get("days") or []
        p_date = saved.get("prep_date") or (days_list[0].get("date") if days_list else datetime.now().date().isoformat())
        p_time = self._norm_prep_time(prep_time or saved.get("prep_time") or "15:00")
        pp = saved.get("prep_plan") if isinstance(saved.get("prep_plan"), dict) else None

        # 1. Prep session event
        if inc == "prep" or pp or saved.get("prep_day") or saved.get("style") == "prep":
            title_raw = (pp or {}).get("title") or "Weekly meal prep"
            items_txt = [str(it.get("text") or "").strip() for it in ((pp or {}).get("items") or ()) if str(it.get("text") or "").strip()]
            if not items_txt:
                cooks = [f"{d.get('day', '')}: {m['name']}" for d in days_list for m in (d.get("meals") or ())
                         if not m.get("skipped") and m.get("effort") != "reheat" and m.get("is_cook", True) is not False]
                items_txt = cooks[:8]
            dur_min = 60
            mh = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hours?)\b", title_raw, re.I)
            mm = re.search(r"(\d+)\s*(?:m|min|mins|minutes?)\b", title_raw, re.I)
            if mh:
                dur_min = max(30, min(240, int(round(float(mh.group(1)) * 60))))
            elif mm:
                dur_min = max(20, min(240, int(mm.group(1))))
            elif items_txt:
                dur_min = max(45, min(150, 30 * len(items_txt)))
            h, mn = map(int, p_time.split(":"))
            start_dt = datetime.fromisoformat(p_date).replace(hour=h, minute=mn, second=0)
            end_dt = start_dt + timedelta(minutes=dur_min)
            summary = f"Meal prep: {title_raw}" if not title_raw.lower().startswith("meal prep") else title_raw
            payload = {
                "entity_id": cal_eid,
                "summary": summary[:200],
                "start_date_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "end_date_time": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if items_txt:
                payload["description"] = "\n".join(f"• {x}" for x in items_txt)
            await self.ha.call("calendar", "create_event", payload)
            created.append({"summary": payload["summary"], "start": payload["start_date_time"]})

        # 2. Optional daily meals
        if inc == "all":
            slot_clock = {"breakfast": (8, 0), "lunch": (12, 30), "dinner": (18, 30)}
            for d in days_list:
                d_iso = d.get("date")
                if not d_iso:
                    continue
                for m in d.get("meals") or ():
                    if m.get("skipped") or not m.get("name"):
                        continue
                    sl = str(m.get("slot") or "dinner").lower()
                    sh, sm = slot_clock.get(sl, (18, 30))
                    mins = max(15, min(90, int(m.get("minutes") or (15 if m.get("effort") == "reheat" else 30))))
                    s_dt = datetime.fromisoformat(d_iso).replace(hour=sh, minute=sm, second=0)
                    e_dt = s_dt + timedelta(minutes=mins)
                    prefix = "Reheat" if (m.get("effort") == "reheat" or m.get("is_cook") is False) else sl.capitalize()
                    desc_parts = []
                    if m.get("batch_label"):
                        desc_parts.append(f"Batch: {m['batch_label']}")
                    if m.get("protein_g"):
                        desc_parts.append(f"~{m['protein_g']}g protein")
                    if m.get("uses"):
                        desc_parts.append("Uses: " + ", ".join(m["uses"][:6]))
                    payload = {
                        "entity_id": cal_eid,
                        "summary": f"{prefix}: {m['name']}"[:200],
                        "start_date_time": s_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "end_date_time": e_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    if desc_parts:
                        payload["description"] = " · ".join(desc_parts)
                    await self.ha.call("calendar", "create_event", payload)
                    created.append({"summary": payload["summary"], "start": payload["start_date_time"]})

        return {"calendar": cal_eid, "calendar_name": cal_name, "created": created, "count": len(created)}

    def set_plan_cell(self, day_index, slot, action="skip"):
        """Instantly skip or restore a single meal cell in the saved plan and recompute the shopping list."""
        saved = self.store.kv_get(self.WEEK_KV)
        if not isinstance(saved, dict) or not saved.get("days"):
            raise ValueError("no weekly meal plan saved yet")
        idx = int(day_index)
        if idx < 0 or idx >= len(saved["days"]):
            raise ValueError(f"day_index must be between 0 and {len(saved['days']) - 1}")
        slot_norm = str(slot or "").strip().lower()
        if slot_norm not in ("breakfast", "lunch", "dinner", "snack"):
            raise ValueError("slot must be breakfast, lunch, dinner, or snack")
        act = str(action or "skip").strip().lower()
        if act not in ("skip", "restore"):
            raise ValueError("action must be 'skip' or 'restore'")

        days_copy = [dict(d, meals=[dict(m) for m in (d.get("meals") or ())]) for d in saved["days"]]
        grid_copy = dict(saved.get("grid") or {})
        found = next((m for m in days_copy[idx]["meals"] if str(m.get("slot") or "").lower() == slot_norm), None)
        if act == "skip":
            if found:
                found["skipped"] = True
            grid_copy[f"{idx}|{slot_norm}"] = "skip"
        else:
            if found:
                found.pop("skipped", None)
                grid_copy[f"{idx}|{slot_norm}"] = found.get("effort") or "normal"
            else:
                raise ValueError("no saved meal in that slot to restore — use swap to generate one")

        updated = dict(saved, days=days_copy, grid=grid_copy, updated=time.time())
        self.store.kv_set(self.WEEK_KV, updated)
        out = dict(updated)
        out["shopping_list"] = build_shopping_list(out["days"], self.items())
        out["shopping_items"] = [x["label"] for x in out["shopping_list"]]
        return out

    async def swap_meal(self, day_index, slot="dinner", request="", model=None, viewer=None):
        """Replace (or fill a skipped) meal in the saved weekly meal plan and recompute the shopping list."""
        saved = self.store.kv_get(self.WEEK_KV)
        if not isinstance(saved, dict) or not saved.get("days"):
            raise ValueError("no weekly meal plan saved yet")
        idx = int(day_index)
        if idx < 0 or idx >= len(saved["days"]):
            raise ValueError(f"day_index must be between 0 and {len(saved['days']) - 1}")

        target_day = saved["days"][idx]
        slot_norm = str(slot or "dinner").strip().lower()
        existing_pos = next((i for i, m in enumerate(target_day.get("meals") or ())
                             if str(m.get("slot") or "").lower() == slot_norm), None)
        old_meal = target_day["meals"][existing_pos] if existing_pos is not None else {}
        other_meals = [
            f"{d['day']} ({m.get('slot', 'dinner')}): {m['name']}"
            for di, d in enumerate(saved["days"])
            for mi, m in enumerate(d.get("meals") or ())
            if not m.get("skipped") and not (di == idx and mi == existing_pos)
        ]

        items = self.items()
        g = self.goals()
        chosen = self._week_model(model or saved.get("model"))
        avoid_items, taste_line = self._week_prefs_context(viewer=viewer)
        stock_lines = [f"- {i['name']}{(' (' + i['amount'] + ')') if i['amount'] else ''} ({i['location']})"
                       for i in items[:30]]
        eff_hint = old_meal.get("effort") or (saved.get("grid") or {}).get(f"{idx}|{slot_norm}")
        if eff_hint == "skip":
            eff_hint = "normal"
        eff_line = (
            "Target effort: REHEAT / PACK ONLY (<=5 min microwave leftover or cold grab-and-go, ZERO stove/oven cooking).\n"
            if eff_hint == "reheat"
            else (f"Target effort: {eff_hint}.\n" if eff_hint else "")
        )
        content = (
            (f"Replace {target_day['day']} {slot_norm}: currently '{old_meal.get('name', '')}'.\n"
             if old_meal.get("name") else f"Add a new {slot_norm} for {target_day['day']}.\n")
            + eff_line
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
        new_meal = _clean_meal_dict(obj, default_slot=slot_norm)
        if not new_meal.get("effort") and eff_hint:
            new_meal["effort"] = eff_hint

        days_copy = [dict(d, meals=list(d.get("meals") or ())) for d in saved["days"]]
        if existing_pos is not None:
            days_copy[idx]["meals"][existing_pos] = new_meal
        else:
            days_copy[idx]["meals"].append(new_meal)
        grid_copy = dict(saved.get("grid") or {})
        grid_copy[f"{idx}|{slot_norm}"] = new_meal.get("effort") or "normal"
        updated = dict(saved, days=days_copy, grid=grid_copy, updated=time.time())
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

