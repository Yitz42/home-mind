"""tools/convert_recipes.py: reading ingredient lines and adding a recipe up from a food database."""
import csv
import io
import json
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
from convert_recipes import Foods, amount, parse_line, recipe  # noqa: E402

assert amount("1 1/2 cups")[0] == 1.5 and amount("1-1/2 c")[0] == 1.5 and amount("½ cup")[0] == 0.5
assert amount("3 to 4 lb")[0] == 3.5 and amount("1/2 tsp")[0] == 0.5 and amount("salt")[0] is None
assert parse_line("1 c. firmly packed brown sugar") == {"n": 1.0, "unit": ("ml", 240), "rest": "firmly packed brown sugar"}
assert parse_line("1 (10 oz.) can cream of mushroom soup")["unit"] == ("g", 283.5)
assert parse_line("2 Tbsp. butter")["unit"] == ("ml", 15) and parse_line("1 t. vanilla")["unit"] == ("ml", 5)
assert parse_line("1 stick butter")["unit"] == ("g", 113) and parse_line("4 large eggs")["unit"] is None

d = tempfile.mkdtemp()
z = os.path.join(d, "sr.zip")
foods = [("1", "Sugars, brown", 380, 0.1), ("3", "Egg, whole, raw, fresh", 143, 12.6), ("4", "Butter, salted", 717, 0.9)]
with zipfile.ZipFile(z, "w") as zf:
    def put(name, rows):
        b = io.StringIO()
        csv.writer(b).writerows(rows)
        zf.writestr(name, b.getvalue())
    put("food.csv", [("fdc_id", "data_type", "description")] + [(f[0], "sr_legacy_food", f[1]) for f in foods])
    put("food_nutrient.csv", [("id", "fdc_id", "nutrient_id", "amount")] +
        [(str(i * 2), f[0], "1008", str(f[2])) for i, f in enumerate(foods)] +
        [(str(i * 2 + 1), f[0], "1003", str(f[3])) for i, f in enumerate(foods)])
    put("food_portion.csv", [("id", "fdc_id", "seq_num", "amount", "measure_unit_id", "portion_description", "modifier",
                              "gram_weight"), ("1", "1", "1", "1", "1000", "", "cup packed", "220"),
                             ("2", "3", "1", "1", "9999", "", "large", "50"), ("3", "4", "1", "1", "1000", "", "tbsp", "14.2")])
from homemind.knowledge import Knowledge  # noqa: E402
k = Knowledge.__new__(Knowledge)
k.food_path = os.path.join(d, "food.db")
assert k._build_food_db({"sr_legacy": z}) == 3
F = Foods(k.food_path)
eggs = recipe("Scrambled eggs", ["4 large eggs", "1 Tbsp. butter", "salt and pepper to taste"], ["eggs", "butter"], F)
assert eggs["matched"] == 1.0 and eggs["total_kcal"] == 388 and eggs["protein"] == 25.3, eggs     # 200 g egg + 14.2 g butter
sweet = recipe("Brown sugar fudge", ["2 c. brown sugar", "1/2 c. butter"], json.loads('["brown sugar", "butter"]'), F)
assert sweet["servings_est"] > 1 and sweet["matched"] == 1.0, sweet                              # baking: ~250 kcal a serving
print("CONVERT RECIPES OK")
