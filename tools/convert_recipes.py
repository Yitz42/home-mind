"""Work out nutrition for a recipe set that has none (RecipeNLG / the Kaggle "over 2M" set) from USDA data.

Each ingredient line ("1 c. firmly packed brown sugar") is read as amount + unit + food, the food is matched in a
Home Mind food database (USDA FoodData Central: SR Legacy, Foundation, FNDDS survey foods, with portion weights), the
grams are worked out (weight units directly; cups, spoons and "2 eggs" from USDA's portion weights; a density of 1 g/ml
as a last resort), and the lines are added up. RecipeNLG has no servings, so servings are estimated from the total
(about 450 kcal a serving for a main dish, less for baking and drinks) and marked as estimated.

Runs anywhere with Python 3.9+ and nothing else installed. Meant for a spare computer overnight, not Home Assistant.

  # 1. a food database: either copy /data/knowledge/food.db from the Home Mind app, or build one from FDC zips
  python tools/convert_recipes.py build-food --out food.db sr_legacy=FoodData_Central_sr_legacy_food_csv_2018-04.zip \
      survey=FoodData_Central_survey_food_csv_2024-10-31.zip foundation=FoodData_Central_foundation_food_csv_....zip
  # 2. convert (RecipeNLG CSV: title, ingredients, directions, link, source, NER); --limit for a trial run
  python tools/convert_recipes.py convert --food food.db --csv RecipeNLG_dataset.csv --out recipes.db --limit 100000
  # 3. how good is it? compare with a set that has real nutrition (datahiveai/recipes-with-nutrition CSV)
  python tools/convert_recipes.py compare --recipes recipes.db --truth recipes_with_nutrition.csv

Non-commercial data (RecipeNLG): keep the output for your own use, don't publish it.
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
csv.field_size_limit(10 ** 8)

NUTS = ("kcal", "protein", "carbs", "fiber", "sugar", "fat")
FRACS = {"½": .5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": .25, "¾": .75, "⅛": .125, "⅜": .375, "⅝": .625, "⅞": .875}
# unit words -> (kind, grams or ml per unit). kind: g = weight, ml = volume, n = a thing counted by its own size
UNITS = [
    (r"(?:lbs?|pounds?|#)", "g", 453.6), (r"(?:oz|ounces?)", "g", 28.35), (r"(?:kg|kilograms?)", "g", 1000),
    (r"(?:g|grams?|gr)", "g", 1), (r"(?:mg)", "g", 0.001),
    (r"(?:c|cups?)", "ml", 240), (r"(?:tbsps?|tbls?|tablespoons?|tbs|T)", "ml", 15),
    (r"(?:tsps?|teaspoons?|t)", "ml", 5), (r"(?:pints?|pts?)", "ml", 473), (r"(?:quarts?|qts?)", "ml", 946),
    (r"(?:gallons?|gal)", "ml", 3785), (r"(?:ml|milliliters?)", "ml", 1), (r"(?:l|liters?|litres?)", "ml", 1000),
    (r"(?:fl\.? ?oz)", "ml", 29.6), (r"(?:sticks?)", "g", 113), (r"(?:cans?)", "can", 0), (r"(?:jars?)", "can", 0),
    (r"(?:pkgs?|packages?|pkts?|packets?|envelopes?|boxes|box)", "pkg", 0), (r"(?:cloves?)", "g", 3),
    (r"(?:slices?)", "slice", 0), (r"(?:dash(?:es)?|pinch(?:es)?)", "g", 0.4), (r"(?:heads?)", "n", 0),
    (r"(?:bunch(?:es)?)", "g", 100), (r"(?:sprigs?)", "g", 1),
]
UNIT_RE = re.compile(r"^(" + "|".join(u for u, _, _ in UNITS) + r")\.?(?=\s|$)", re.I)
SIZE_IN_PARENS = re.compile(r"\(\s*(\d+(?:\.\d+)?)\s*(oz|ounce|g|gram|lb|ml)\.?\s*\)", re.I)
SKIP = re.compile(r"\b(to taste|optional|for (?:garnish|serving|frying|greasing)|as needed|water)\b", re.I)
SWEET = re.compile(r"\b(cookies?|brownies?|cake|cupcakes?|muffins?|fudge|candy|bars|pie|frosting|icing|dessert|"
                   r"bread|rolls|biscuits)\b", re.I)
DRINK = re.compile(r"\b(punch|smoothie|shake|lemonade|tea|coffee|cocktail|martini|margarita|eggnog)\b", re.I)


def amount(text):
    """Leading amount: '1 1/2', '1-1/2', '1½', '2.5', '3 to 4' (the middle), '1/2'. Returns (value, rest)."""
    t = text.strip()
    for k, v in FRACS.items():
        t = t.replace(k, f" {v}")
    t = re.sub(r"(\d)-(\d+/\d+)", r"\1 \2", t)
    m = re.match(r"^\s*(\d+(?:\.\d+)?)?(?:\s+(\d+)/(\d+)|\s*/\s*(\d+))?(?:\s*(?:to|-)\s*(\d+(?:\.\d+)?))?\s*", t)
    if not m or not m.group(0).strip():
        return None, t
    a = float(m.group(1)) if m.group(1) else 0.0
    if m.group(2):
        a += float(m.group(2)) / float(m.group(3))
    elif m.group(4) and m.group(1):
        a = float(m.group(1)) / float(m.group(4))
    if m.group(5):
        a = (a + float(m.group(5))) / 2
    return (a or None), t[m.end():]


def parse_line(line):
    """'1 c. firmly packed brown sugar' -> {'n': 1.0, 'unit': ('ml', 240), 'rest': 'firmly packed brown sugar'}."""
    n, rest = amount(line)
    size = SIZE_IN_PARENS.search(rest)
    unit = None
    m = UNIT_RE.match(rest.strip())
    if m:
        word = m.group(1)
        for u, kind, per in UNITS:
            if re.fullmatch(u, word, re.I if len(word) > 1 else 0):
                unit = (kind, per)
                break
        rest = rest.strip()[m.end():]
    if size and unit and unit[0] in ("can", "pkg"):        # '1 (10 oz.) can soup' -> 10 oz
        v, u = float(size.group(1)), size.group(2).lower()
        unit = ("g", v * {"oz": 28.35, "ounce": 28.35, "g": 1, "gram": 1, "lb": 453.6, "ml": 1}[u])
    elif size and not unit:
        v, u = float(size.group(1)), size.group(2).lower()
        unit = ("g", v * {"oz": 28.35, "ounce": 28.35, "g": 1, "gram": 1, "lb": 453.6, "ml": 1}[u])
        rest = SIZE_IN_PARENS.sub("", rest)
    return {"n": n, "unit": unit, "rest": rest.strip(" .,")}


class Foods:
    """Lookups in a Home Mind food.db, cached by name (a big recipe set repeats the same few thousand foods)."""

    def __init__(self, path):
        from homemind.knowledge import Knowledge
        self.k = Knowledge.__new__(Knowledge)
        self.k.food_path = path
        self.db = sqlite3.connect(path)
        self.has_portions = bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='portions'").fetchone())
        self.cache = {}

    def get(self, name):
        key = name.lower().strip()
        if key not in self.cache:
            hits = [h for h in self.k.food(key, limit=3) if h["source"] != "label"] or self.k.food(key, limit=1)
            self.cache[key] = hits[0] if hits else None
        return self.cache[key]


def portion_g(food, words, default):
    """Grams for one 'cup' / 'tbsp' / 'large' / 'slice' from USDA's portions, else None."""
    for p in food.get("portions") or []:
        t = p["text"].lower()
        if any(re.search(rf"\b{w}\b", t) for w in words):
            m = re.match(r"^\s*(\d+(?:\.\d+)?)?(?:\s*/\s*(\d+))?", t)
            q = float(m.group(1)) / float(m.group(2)) if m and m.group(1) and m.group(2) else \
                float(m.group(1)) if m and m.group(1) else 1.0
            return p["g"] / (q or 1)
    return default


def grams(parsed, food):
    n, unit = parsed["n"] or 1.0, parsed["unit"]
    if unit is None:                                          # '2 eggs', '1 onion'
        size = re.search(r"\b(small|medium|large|extra large|jumbo)\b", parsed["rest"], re.I)
        words = [size.group(1).lower()] if size else ["medium", "large", "whole", "item", "each", "fruit"]
        g = portion_g(food, words, None) or portion_g(food, ["1 "], None)
        return n * (g or 100)
    kind, per = unit
    if kind == "g":
        return n * per
    if kind == "ml":
        words = {240: ["cup"], 15: ["tbsp", "tablespoon"], 5: ["tsp", "teaspoon"]}.get(per)
        if words:
            g = portion_g(food, words, None)
            if g:
                return n * g
        return n * per                                        # water-like density
    if kind == "can":
        return n * 400
    if kind == "pkg":
        return n * 250
    if kind == "slice":
        return n * (portion_g(food, ["slice"], None) or 28)
    return n * (portion_g(food, ["head", "medium", "whole"], None) or 500)


def food_name(line, ner):
    """The NER food the line is about (longest that appears in it), else the line without its amount and unit."""
    low = line.lower()
    hits = [x for x in ner if x and x.lower() in low]
    return max(hits, key=len) if hits else None


def recipe(title, lines, ner, foods):
    tot = {k: 0.0 for k in NUTS}
    matched = counted = 0
    for line in lines:
        if SKIP.search(line) and not re.match(r"^\s*\d", line):
            continue
        counted += 1
        p = parse_line(line)
        name = food_name(line, ner) or p["rest"]
        if not name or SKIP.fullmatch(name.strip()):
            continue
        f = foods.get(name)
        if not f:
            continue
        g = grams(p, f)
        if g <= 0 or g > 5000:
            continue
        per = f["per_100g"]
        for k in NUTS:
            tot[k] += (per.get(k) or 0) * g / 100
        matched += 1
    if not counted:
        return None
    target = 250 if SWEET.search(title) else 150 if DRINK.search(title) else 450
    servings = max(1, min(24, round(tot["kcal"] / target))) if tot["kcal"] else 1
    return {"servings_est": servings, "matched": round(matched / counted, 2),
            **{k: round(v / servings, 1) for k, v in tot.items()}, "total_kcal": round(tot["kcal"])}


def cmd_build_food(a):
    from homemind.knowledge import Knowledge
    paths = dict(x.split("=", 1) for x in a.zips)
    k = Knowledge.__new__(Knowledge)
    k.food_path = a.out
    n = k._build_food_db(paths)
    print(f"{n} foods in {a.out}")


def cmd_convert(a):
    foods = Foods(a.food)
    out = sqlite3.connect(a.out)
    out.execute("""CREATE TABLE IF NOT EXISTS recipes (id INTEGER PRIMARY KEY, title TEXT, link TEXT, ingredients TEXT,
                   ner TEXT, servings_est INTEGER, matched REAL, kcal REAL, protein REAL, carbs REAL, fiber REAL,
                   sugar REAL, fat REAL, total_kcal REAL)""")
    done = {r[0] for r in out.execute("SELECT id FROM recipes")}
    t0, n, rows = time.time(), 0, []
    with open(a.csv, newline="", encoding="utf-8") as fh:
        for i, r in enumerate(csv.DictReader(fh)):
            if a.limit and i >= a.limit:
                break
            if i in done:
                continue
            try:
                lines, ner = json.loads(r["ingredients"]), json.loads(r.get("NER") or "[]")
            except (ValueError, KeyError):
                continue
            res = recipe(r["title"], lines, ner, foods)
            if not res or res["matched"] < a.min_match:
                continue
            rows.append((i, r["title"].strip(), r.get("link"), r["ingredients"], r.get("NER"), res["servings_est"],
                         res["matched"], *[res[k] for k in NUTS], res["total_kcal"]))
            n += 1
            if len(rows) >= 2000:
                out.executemany("INSERT OR REPLACE INTO recipes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                out.commit()
                rows = []
                print(f"{i + 1} read, {n} kept, {len(foods.cache)} foods, {(i + 1) / (time.time() - t0):.0f}/s",
                      flush=True)
    if rows:
        out.executemany("INSERT OR REPLACE INTO recipes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    out.execute("CREATE INDEX IF NOT EXISTS recipes_title ON recipes(title)")
    out.commit()
    print(f"done: {n} kept in {time.time() - t0:.0f} s")


def _norm(t):
    return " ".join(re.findall(r"[a-z]+", (t or "").lower()))


def cmd_compare(a):
    """Recipes in both sets (same title): how far off the estimated per-serving numbers are."""
    db = sqlite3.connect(a.recipes)
    mine = {}
    for t, s, *vals in db.execute("SELECT title, servings_est, " + ", ".join(NUTS) + ", total_kcal FROM recipes"):
        mine.setdefault(_norm(t), (s, vals))
    pairs = []
    with open(a.truth, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            m = mine.get(_norm(r.get("recipe_name") or r.get("label") or r.get("name")))
            if not m:
                continue
            try:
                serv = float(r.get("servings") or r.get("yield") or 1) or 1
                tn = json.loads(r.get("total_nutrients") or r.get("totalNutrients") or "{}")
            except ValueError:
                continue
            q = lambda code: (tn.get(code) or {}).get("quantity")  # noqa: E731
            truth_total = q("ENERC_KCAL") or float(r.get("calories") or 0)
            truth = {"kcal": truth_total / serv, "protein": (q("PROCNT") or 0) / serv, "carbs": (q("CHOCDF") or 0) / serv,
                     "fiber": (q("FIBTG") or 0) / serv, "sugar": (q("SUGAR") or 0) / serv, "fat": (q("FAT") or 0) / serv}
            pairs.append((m, truth, truth_total))
    if not pairs:
        print("no recipes in common")
        return
    print(f"{len(pairs)} recipes in common")

    def med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else None
    tot_err = [abs(m[1][-1] - t) / t for m, _, t in pairs if t]
    print(f"whole recipe kcal: median error {med(tot_err):.0%}  (ingredient matching alone)")
    for i, k in enumerate(NUTS):
        errs = [abs(m[1][i] - tr[k]) / tr[k] for m, tr, _ in pairs if tr[k] > (50 if k == "kcal" else 2)]
        if errs:
            print(f"per serving {k:8s}: median error {med(errs):.0%}, within 25%: "
                  f"{sum(e <= .25 for e in errs) / len(errs):.0%}  (n={len(errs)})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build-food")
    b.add_argument("--out", required=True)
    b.add_argument("zips", nargs="+", help="kind=path, kind one of sr_legacy, foundation, survey, branded")
    c = sub.add_parser("convert")
    c.add_argument("--food", required=True)
    c.add_argument("--csv", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--limit", type=int, default=0)
    c.add_argument("--min-match", type=float, default=0.75, help="share of lines matched to keep a recipe")
    m = sub.add_parser("compare")
    m.add_argument("--recipes", required=True)
    m.add_argument("--truth", required=True)
    a = ap.parse_args()
    {"build-food": cmd_build_food, "convert": cmd_convert, "compare": cmd_compare}[a.cmd](a)


if __name__ == "__main__":
    main()
