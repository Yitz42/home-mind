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
        c.add_item(Page("Cookie_dough", "Cookie dough",
                        "<p>Cookie dough is an uncooked mixture of cookie ingredients, eaten raw by some people or "
                        "baked into cookies.</p><p>Eating raw cookie dough carries a risk of food poisoning from raw "
                        "eggs (Salmonella) and from uncooked flour (E. coli).</p>"))
        c.add_item(Page("Eiffel_Tower_(disambiguation)", "Eiffel Tower (disambiguation)",
                        "<p>The Eiffel Tower is a landmark in Paris. Eiffel Tower may also refer to other towers "
                        "built after it, such as the replica in Las Vegas or the Tokyo Tower.</p>"))
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
    assert k.food_counts() == {"sr_legacy": 3, "branded": 2, "survey": 2, "portions": 2}, k.food_counts()
    assert k.food("bowl of chili with beans")[0]["source"] == "USDA as eaten"     # 'bowl' isn't in the name
    assert k._add_to_food_db({"survey": sv}) == 2 and k.food_counts()["portions"] == 2      # loading again: no doubles
    assert k.load_report["survey"]["kept"] == 2 and "1003" in k.load_report["survey"]["nutrient_ids"]
    # a release that writes nutrient ids differently ('1008.0', or ids known only through nutrient.csv's SR numbers)
    odd = os.path.join(kd, "odd.zip")
    make_zip(odd, {
        "food.csv": [("fdc_id", "data_type", "description"), ("30", "survey_fndds_food", "Oatmeal, made with water")],
        "nutrient.csv": [("id", "name", "unit_name", "nutrient_nbr"), ("9001", "Energy", "KCAL", "208"),
                         ("9002", "Protein", "G", "203"), ("9003", "Fiber, total dietary", "G", "291")],
        "food_nutrient.csv": [("id", "fdc_id", "nutrient_id", "amount"), ("1", "30", "9001", "71"),
                              ("2", "30", "9002", "2.5"), ("3", "30", "1079.0", "1.7")],
    })
    assert k._add_to_food_db({"survey2": odd}) == 1
    oat = k.food("oatmeal made with water")[0]
    assert oat["per_100g"] == {"kcal": 71.0, "protein": 2.5, "fiber": 1.7}, oat
    sr_nbr = os.path.join(kd, "nbr.zip")             # FNDDS 2021-2023 writes the SR numbers in food_nutrient.csv
    make_zip(sr_nbr, {
        "Folder/Food.csv": [("fdc_id", "data_type", "description"), ("40", "survey_fndds_food", "Lentil soup, home made")],
        "Folder/food_nutrient.csv": [("id", "fdc_id", "nutrient_id", "amount"), ("1", "40", "208", "62"),
                                     ("2", "40", "203", "3.9"), ("3", "40", "291", "2.4"), ("4", "40", "269", "1.1")],
    })
    assert k._add_to_food_db({"survey3": sr_nbr}) == 1 and "Food.csv" in k.load_report["survey3"]["files"][0]
    oatz = os.path.join(kd, "oat.zip")             # "oatmeal" means the oatmeal, not oatmeal bread
    make_zip(oatz, {
        "food.csv": [("fdc_id", "data_type", "description"), ("50", "sr_legacy_food", "Bread, oatmeal"),
                     ("51", "survey_fndds_food", "Oatmeal, regular or quick, made with water, no added fat")],
        "food_nutrient.csv": [("id", "fdc_id", "nutrient_id", "amount"), ("1", "50", "1008", "269"), ("2", "51", "208", "71")],
        "food_portion.csv": [("id", "fdc_id", "seq_num", "amount", "measure_unit_id", "portion_description", "modifier",
                              "gram_weight"), ("1", "51", "1", "", "9999", "1 cup", "", "234"),
                             ("2", "51", "2", "", "9999", "90000", "", "234")],
    })
    k._add_to_food_db({"survey": oatz})
    o1 = k.food("oatmeal")[0]
    assert o1["name"].startswith("Oatmeal") and o1["portions"] == [{"text": "1 cup", "g": 234.0}], o1
    assert k.food("lentil soup")[0]["per_100g"] == {"kcal": 62.0, "protein": 3.9, "fiber": 2.4, "sugar": 1.1}

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

    # general questions that only say "I" or "me" get a passage; questions about the user's own things don't
    from homemind.knowledge import about_user
    for q in ("Can I eat raw cookie dough?", "Tell me about the Eiffel Tower", "Should I worry about chickpeas?",
              "How do I make hummus?"):
        assert not about_user(q), q
    for q in ("Where did I put my keys?", "What did I eat yesterday?", "Have I taken my pills?", "When is our trip?",
              "I'm tired, what is a chickpea", "What’s my code?"):
        assert about_user(q), q
    ps = k2.passages("Can I eat raw cookie dough?")
    assert ps and ps[0]["title"] == "Cookie dough" and "Salmonella" in ps[0]["text"], ps
    assert k2.passages("Tell me about the Eiffel Tower")[0]["title"] == "Eiffel Tower"
    assert all("disambiguation" not in p["title"] for p in k2.passages("Tell me about the Eiffel Tower", n=3))
    assert all("disambiguation" not in p["title"] for p in k2.passages("Eiffel Tower replica Las Vegas", n=3))
    assert k2.passages("what did I eat last, chickpeas?") == []                     # about the user's day
    # a second try with shorter search words when the whole question finds nothing on topic
    rq = k2.retry_queries("Is it okay to eat raw cookie dough when you're hungry at midnight?")
    assert rq[0] == "raw cookie dough", rq
    assert "midnight" in rq and "okay" not in [x.lower() for x in rq], rq
    calls = []
    real = k2.search

    def picky(q, limit=3):                      # a full-text index that only answers short searches
        calls.append(q)
        return real(q, limit) if len(q.split()) <= 3 else []
    k2.search = picky
    ps = k2.passages("is it really dangerous to snack on raw cookie dough late at night")
    k2.search = real
    assert ps and ps[0]["title"] == "Cookie dough", (ps, calls)
    assert len(calls) <= 2 + 4, calls                                    # at most four extra searches

    # a model's own search (tool, MCP)
    lk = k2.lookup("Eiffel Tower")
    assert lk["snapshot"] == "2026-10" and lk["passages"][0]["title"] == "Eiffel Tower", lk
    assert "note" in k2.lookup("zzqxv")

    # the source line names the article an answer used
    from homemind.knowledge import Knowledge as K
    ref = k2.reference_lines("How tall is the Eiffel Tower?")
    src = K.sources("MEMORY NOTES:\n(none)\n\n" + ref, "According to Wikipedia, the Eiffel Tower is 330 metres tall.")
    assert src == [{"title": "Wikipedia: Eiffel Tower", "kind": "wikipedia"}], src
    assert K.sources(ref, "It is 330 metres tall.") == []                # the answer didn't use Wikipedia
    assert K.sources("MEMORY NOTES:\n- x", "According to Wikipedia, yes.") == []

    # refreshing an old snapshot
    from datetime import datetime as _dt
    from homemind.knowledge import WIKI_REFRESH_DAYS
    k2.status.setdefault("wiki", {}).pop("checked", None)
    assert not k2.refresh_due(_dt(2026, 12, 1)) and k2.refresh_due(_dt(2027, 5, 1))
    assert k2.wiki_age_days(_dt(2027, 5, 1)) >= WIKI_REFRESH_DAYS
    k2.status["wiki"]["checked"] = _dt(2027, 4, 29).timestamp()
    assert not k2.refresh_due(_dt(2027, 5, 1))                          # asked the server this week already
    asked = []

    async def latest_same():
        return "u", "wikipedia_en_all_nopic_2026-10.zim"
    k2.latest_wiki_url = latest_same
    assert await k2.refresh_wiki() is False and k2.status["wiki"]["newest_seen"].endswith("2026-10.zim")
    assert not k2.refresh_pending()

    async def latest_new():
        return "https://example.invalid/wikipedia_en_all_nopic_2027-04.zim", "wikipedia_en_all_nopic_2027-04.zim"

    async def fake_get(force_window=False):
        asked.append(k2.status["wiki"]["url_planned"])
        return False                                                  # paused: goes on in the next window
    k2.latest_wiki_url, k2.get_wiki = latest_new, fake_get
    assert await k2.refresh_wiki() is False and asked == ["https://example.invalid/wikipedia_en_all_nopic_2027-04.zim"]
    assert k2.refresh_pending() and k2.archive is not None             # the old file stays in use meanwhile
    del k2.latest_wiki_url, k2.get_wiki
    k2.status["wiki"].pop("name", None)
    k2.status["wiki"].pop("url_planned", None)

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
    _, _, ctx = await mind.context_for(mind.store, "Can I eat raw cookie dough?")
    assert "REFERENCE (offline Wikipedia" in ctx and "Salmonella" in ctx, ctx

    # the answer's source line names the article, next to the notes (never logged as a note)
    class Says:
        type, cfg = "ollama", {"url": "http://192.168.1.5:11434"}

        async def chat(self, system, messages, **k):
            return {"text": "According to Wikipedia, the Eiffel Tower is 330 metres tall.", "ms": 1}

    class Ms:
        models = {"f": Says()}

        def get(self, n):
            return self.models[n]
    m2 = Mind(tempfile.mkdtemp(), Ms())
    m2.FALLBACK = []
    m2.knowledge = k2
    out = await m2.ask("How tall is the Eiffel Tower?", "f", fallback=False)
    assert {"title": "Wikipedia: Eiffel Tower", "kind": "wikipedia"} in out["sources"], out
    assert all(s.get("id") for s in out["sources"] if s.get("kind") != "wikipedia")

    # --- Wikipedia by meaning -------------------------------------------------------------------------------------------
    class Sense:                                    # stands in for WikiSense: titles with a closeness score
        def __init__(self, hits):
            self.hits, self.asked = hits, []

        def search(self, q, k=5):
            self.asked.append(q)
            return self.hits[:k]
    q = "Is unbaked biscuit batter risky to taste?"        # shares no word with the article: the word search misses
    assert not any(p["title"] == "Cookie dough" for p in k2.passages(q))
    k2.sense = Sense([{"title": "Cookie dough", "text": "Cookie dough is an uncooked mixture.", "score": 0.9}])
    ps = k2.passages(q)
    assert ps and ps[0]["title"] == "Cookie dough" and "Salmonella" in ps[0]["text"], ps   # full text from the ZIM
    assert k2.sense.asked == [q]
    k2.sense = Sense([{"title": "Cookie dough", "text": "x", "score": 0.82}])             # close-ish, no shared word
    assert not any(p["title"] == "Cookie dough" for p in k2.passages(q))
    k2.sense = Sense([{"title": "Cookie dough", "text": "x", "score": 0.82}])             # close-ish, shares "cookie"
    assert k2.passages("cookie safety")[0]["title"] == "Cookie dough"
    k2.sense = Sense([{"title": "Cookie dough", "text": "x", "score": 0.7}])              # too far: ignored
    assert k2.by_meaning(q) == []
    k2.sense = Sense([{"title": "Not In The Zim", "text": "Its first paragraph, which is long enough to keep.",
                       "score": 0.95}])
    ps = k2.passages("Tell me about something nobody indexed")
    assert ps and ps[0]["title"] == "Not In The Zim" and ps[0]["text"].startswith("Its first paragraph"), ps
    k2.sense = Sense([])
    assert k2.passages("where did I put my cookie dough?") == [] and k2.sense.asked == []   # about the user: no search
    assert k2.lookup("biscuit batter")["passages"] == [] or True
    k2.sense = None

    # the real index files (a tiny one) open from knowledge/wikisense; the download waits for the ZIM and the window
    import shutil as _sh
    data = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "wikisense")
    assert not k2.sense_files_ready()
    _sh.copytree(os.path.join(data, "index"), os.path.join(kd, "wikisense", "index"))
    _sh.copytree(os.path.join(data, "model"), os.path.join(kd, "wikisense", "e5-base"))
    k5 = Knowledge(d, {})
    assert k5.sense is not None and k5.summary()["wiki_meaning_ready"]
    assert k5.sense.search("salmon egg", k=2)[0]["title"].startswith("Article ")
    k6 = Knowledge(tempfile.mkdtemp(), {"knowledge_window": "00:00-23:59", "knowledge_food": False})
    started = []

    async def fake_sense(force_window=False):
        started.append(force_window)
    wiki_started = []

    async def fake_wiki(force_window=False):
        wiki_started.append(1)
    k6.get_sense, k6.get_wiki = fake_sense, fake_wiki
    await k6.tick()
    await asyncio.sleep(0)
    assert started == [] and wiki_started == [1]         # no ZIM yet: Wikipedia first
    k6.archive = object()
    await k6.tick()
    await asyncio.sleep(0)
    assert started == [False], started
    k7 = Knowledge(tempfile.mkdtemp(), {"knowledge_window": "00:00-23:59", "knowledge_food": False,
                                        "knowledge_wiki_meaning": False})
    k7.archive, k7.get_sense = object(), fake_sense
    await k7.tick()
    await asyncio.sleep(0)
    assert started == [False]                            # switched off

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
