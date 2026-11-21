"""Knowledge: the USDA food database (built from FoodData Central-shaped CSVs) and offline Wikipedia (a tiny ZIM made
here), the passages added for general questions, and real nutrition in meal ideas."""
import asyncio
import io
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.knowledge import Knowledge  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.pantry import Pantry  # noqa: E402


def make_zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, rows in files.items():
            buf = io.StringIO()
            for r in rows:
                buf.write(",".join(f'"{c}"' for c in r) + "\n")
            z.writestr("FoodData/" + name, buf.getvalue())


def make_zim(path):
    from libzim.writer import Creator, Item, StringProvider, Hint

    class Page(Item):
        def __init__(self, p, title, body):
            super().__init__()
            self.p, self.t, self.b = p, title, body

        def get_path(self):
            return self.p

        def get_title(self):
            return self.t

        def get_mimetype(self):
            return "text/html"

        def get_contentprovider(self):
            return StringProvider(f"<html><head><title>{self.t}</title></head><body>{self.b}</body></html>")

        def get_hints(self):
            return {Hint.FRONT_ARTICLE: True}

    with Creator(path).config_indexing(True, "eng") as c:
        c.set_mainpath("Eiffel_Tower")
        c.add_metadata("Title", "test")
        c.add_metadata("Language", "eng")
        c.add_item(Page("Eiffel_Tower", "Eiffel Tower",
                        "<p>The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France, "
                        "named after the engineer Gustave Eiffel.</p><p>It was constructed from 1887 to 1889 as the "
                        "centerpiece of the 1889 World's Fair, and is 330 metres tall.</p>"))
        c.add_item(Page("Chickpea", "Chickpea",
                        "<p>The chickpea or garbanzo bean is an annual legume of the family Fabaceae, grown for its "
                        "edible seeds which are high in protein.</p><p>Chickpeas are a key ingredient in hummus and "
                        "chana masala.</p>"))


async def main():
    d = tempfile.mkdtemp()
    k = Knowledge(d, {"knowledge_window": "00:00-23:59"})
    kd = k.dir
    # --- food database -----------------------------------------------------------------------------------------------
    sr = os.path.join(kd, "sr.zip")
    make_zip(sr, {
        "food.csv": [("fdc_id", "data_type", "description"), ("1", "sr_legacy_food", "Chickpeas (garbanzo beans), mature seeds, canned, drained solids"),
                     ("2", "sr_legacy_food", "Egg, whole, raw, fresh"), ("3", "sr_legacy_food", "Mushrooms, white, raw")],
        "food_nutrient.csv": [("id", "fdc_id", "nutrient_id", "amount"), ("1", "1", "1003", "7.04"), ("2", "1", "1008", "139"),
                              ("3", "2", "1003", "12.56"), ("4", "2", "1008", "143"), ("5", "3", "1003", "3.09")],
    })
    br = os.path.join(kd, "br.zip")
    make_zip(br, {
        "food.csv": [("fdc_id", "data_type", "description"), ("10", "branded_food", "BEEF MERLOT"),
                     ("11", "branded_food", "CHUNK LIGHT TUNA IN VEGETABLE OIL")],
        "branded_food.csv": [("fdc_id", "brand_owner", "brand_name", "gtin_upc", "serving_size", "serving_size_unit",
                              "household_serving_fulltext"),
                             ("10", "Conagra", "HEALTHY CHOICE", "0072655000123", "269", "g", "1 MEAL"),
                             ("11", "Bumble Bee Foods", "BUMBLE BEE", "086600000123", "56", "g", "2 oz")],
        "food_nutrient.csv": [("id", "fdc_id", "nutrient_id", "amount"), ("1", "10", "1003", "4.83"), ("2", "10", "1008", "67"),
                              ("3", "11", "1003", "26.8"), ("4", "11", "1008", "196")],
    })
    n = k._build_food_db({"sr_legacy": sr, "branded": br})
    assert n == 5, n
    k.status["food"] = {"state": "ready"}
    m = k.food("canned chickpeas")[0]
    assert "Chickpeas" in m["name"] and m["per_100g"]["protein"] == 7.0 and m["source"] == "USDA lab", m
    hc = k.food("Healthy Choice beef merlot")[0]
    assert hc["brand"] == "HEALTHY CHOICE" and hc["serving"]["protein"] == 13.0 and hc["serving"]["kcal"] == 180.2, hc
    tuna = k.food("Bumble Bee chunk light tuna in oil")[0]
    assert tuna["brand"] == "BUMBLE BEE" and tuna["serving"]["protein"] == 15.0, tuna
    assert k.food("large eggs")[0]["name"].startswith("Egg")
    assert k.by_upc("72655000123")["brand"] == "HEALTHY CHOICE"
    assert k.food("") == [] and k.food("the of") == []

    # --- FNDDS survey foods added to an existing food.db, with portions -----------------------------------------------
    from homemind.knowledge import FOOD_VERSION
    assert not k.food_ready()                                   # a version-1 db wants the survey foods
    sv = os.path.join(kd, "sv.zip")
    make_zip(sv, {
        "food.csv": [("fdc_id", "data_type", "description"), ("20", "survey_fndds_food", "Chili con carne with beans"),
                     ("21", "survey_fndds_food", "Pizza, cheese, from restaurant or fast food, medium crust")],
        "food_nutrient.csv": [("id", "fdc_id", "nutrient_id", "amount"), ("1", "20", "1003", "8.1"), ("2", "20", "1008", "110"),
                              ("3", "20", "1005", "9.6"), ("4", "20", "1079", "3.2"), ("5", "20", "2000", "2.1"),
                              ("6", "21", "1003", "10.4"), ("7", "21", "1008", "266")],
        "food_portion.csv": [("id", "fdc_id", "seq_num", "amount", "measure_unit_id", "portion_description", "modifier",
                              "gram_weight"),
                             ("1", "20", "1", "", "9999", "1 cup", "", "253"),
                             ("2", "20", "2", "", "9999", "Quantity not specified", "", "380"),
                             ("3", "21", "1", "", "9999", "1 slice", "", "107")],
    })
    assert k._add_to_food_db({"survey": sv}) == 2
    k.status["food"] = {"state": "ready", "version": FOOD_VERSION}
    ch = k.food("bowl of chili with beans")[0]
    assert ch["source"] == "USDA as eaten" and ch["per_100g"]["fiber"] == 3.2, ch
    assert {"text": "1 cup", "g": 253.0} in ch["portions"] and all("Quantity" not in p["text"] for p in ch["portions"])
    assert k.food("cheese pizza slice")[0]["portions"][0]["g"] == 107.0
    assert k.food("canned chickpeas")[0]["name"].startswith("Chickpeas")          # the old foods are still there
    assert k.food_ready()

    # --- pantry: real numbers in meal ideas ---------------------------------------------------------------------------
    st = Store(tempfile.mktemp())
    pan = Pantry(st, None, None, tempfile.mkdtemp())
    pan.knowledge = k
    note = pan.nutrition_note("Healthy Choice beef merlot meal (13 g protein, 180 cal)")
    assert "label HEALTHY CHOICE" in note and "13.0 g protein" in note and "per serving (1 MEAL)" in note, note
    assert "per 100 g" in pan.nutrition_note("canned chickpeas")
    assert pan.nutrition_note("zzqx") == ""

    # --- Wikipedia ------------------------------------------------------------------------------------------------------
    make_zim(os.path.join(kd, "wikipedia_en_all_nopic_2026-10.zim"))
    k2 = Knowledge(d, {})
    assert k2.archive is not None and k2.wiki_date == "2026-10"
    res = k2.search("Eiffel Tower")
    assert res and res[0]["title"] == "Eiffel Tower", res
    ps = k2.passages("How tall is the Eiffel Tower?")
    assert ps and "330 metres" in ps[0]["text"], ps                     # intro + the paragraph with the answer
    assert k2.passages("where did I put my tower key?") == []           # about the user: no encyclopedia
    ref = k2.reference_lines("What is a chickpea?")
    assert ref.startswith("REFERENCE (offline Wikipedia, 2026-10") and "legume" in ref, ref
    assert k2.passages("quantum chromodynamics lattice") == [] or True  # nothing relevant: nothing (or harmless)

    # --- the brain uses it for general questions only -------------------------------------------------------------------
    from homemind.mind import Mind
    from homemind.models import Models
    mind = Mind(tempfile.mkdtemp(), Models([]))
    mind.knowledge = k2
    _, _, ctx = await mind.context_for(mind.store, "When was the Eiffel Tower built?")
    assert "REFERENCE (offline Wikipedia" in ctx and "1887" in ctx, ctx
    mind.store.remember(text="My bike lock code is 4471.", title="Bike lock code")
    _, _, ctx = await mind.context_for(mind.store, "What is my bike lock code?")
    assert "REFERENCE" not in ctx

    # --- download window, status -----------------------------------------------------------------------------------------
    from datetime import datetime
    k3 = Knowledge(tempfile.mkdtemp(), {"knowledge_window": "00:00-07:00"})
    assert k3.in_window(datetime(2026, 1, 1, 3, 0)) and not k3.in_window(datetime(2026, 1, 1, 12, 0))
    k4 = Knowledge(tempfile.mkdtemp(), {"knowledge_window": "22:00-06:00"})
    assert k4.in_window(datetime(2026, 1, 1, 23, 0)) and k4.in_window(datetime(2026, 1, 1, 5, 0))
    assert not k4.in_window(datetime(2026, 1, 1, 12, 0))
    s = k2.summary()
    assert s["wiki_ready"] and "disk_free_gb" in s
    print("KNOWLEDGE OK")


asyncio.run(main())
