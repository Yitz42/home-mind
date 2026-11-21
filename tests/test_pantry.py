"""Pantry: parsing what was bought, use-by estimates, using things up, shopping-list sync, meal suggestions."""
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.store import Store  # noqa: E402
from homemind.pantry import Pantry, estimate, guess_location  # noqa: E402
from homemind import nutrition  # noqa: E402


class FakeHA:
    def __init__(self):
        self.calls = []
        self.st = {"input_number.steady_readiness": "42", "input_number.steady_sleep_hours": "7"}

    async def state(self, eid):
        return {"state": self.st.get(eid, "unknown")}

    async def post_json(self, path, data, timeout=30):
        if path.startswith("services/todo/get_items"):
            return {"service_response": {"todo.shopping_list": {"items": [
                {"uid": "a", "summary": "2 lb ground beef", "status": "completed"},
                {"uid": "b", "summary": "baby spinach", "status": "completed"}]}}}
        return {}

    async def call(self, d, s, data):
        self.calls.append((d, s, data))


class Chef:
    type, cfg = "mock", {}

    async def chat(self, system, msgs, **k):
        self.last = msgs[0]["content"]
        return {"text": json.dumps({"focus": "tired: lower-carb", "meals": [
            {"name": "Beef and spinach skillet", "meal": "dinner", "uses": ["ground beef", "baby spinach"],
             "missing": ["feta"], "why": "uses the spinach before it turns", "protein_g": 45, "calories": 600,
             "steps": "Brown beef; wilt spinach."}]}), "ms": 1}


class Models:
    def __init__(self):
        self.chef = Chef()
        self.models = {"haiku": self.chef}

    def get(self, n):
        return self.chef


async def main():
    assert estimate("baby spinach", "fridge") == 5 and estimate("chicken breast", "freezer") == 270
    assert guess_location("chicken thighs") == "fridge" and guess_location("frozen peas") == "freezer"
    assert guess_location("rolled oats") == "pantry"
    assert estimate("Chobani strawberry banana yogurt", "fridge") == 14     # the last word names the food
    assert estimate("canned low-sodium sweet peas", "pantry") == 730 and estimate("mango", "pantry") == 5
    assert estimate("Twinings ginger mango green tea", "pantry") == 730    # tea, not mango
    assert estimate("iodized salt", "pantry") > 1000 and estimate("instant coffee", "pantry") >= 730
    assert estimate("Stur fruit punch water enhancer", "pantry") == 365
    assert guess_location("Maruchan chicken ramen") == "pantry" and guess_location("Britannia Milk Bikis biscuits") == "pantry"
    assert guess_location("chicken breast") == "fridge" and guess_location("milk") == "fridge"
    assert guess_location("Maruchan roast chicken ramen cups") == "pantry" and guess_location("pesto mac and cheese") == "pantry"
    assert guess_location("canned peas") == "pantry" and guess_location("cheese") == "fridge"
    assert guess_location("Rice-A-Roni beef") == "pantry" and guess_location("Swiss Miss milk chocolate hot cocoa mix") == "pantry"
    assert guess_location("Bissli pizza wheat snacks") == "pantry" and guess_location("milk chocolate candy") == "pantry"
    assert guess_location("Cheese Club macaroni and cheese") == "pantry" and estimate("Quaker butter instant grits", "pantry") == 365
    st = Store(tempfile.mktemp())
    ha, models = FakeHA(), Models()
    p = Pantry(st, ha, models, tempfile.mkdtemp())
    parsed = p.parse_list("I bought chicken, 2 lb ground beef and a dozen eggs")
    assert parsed == [("chicken", ""), ("ground beef", "2 lb"), ("eggs", "a dozen")], parsed
    assert p.parse_list("a bag of baby carrots and some hummus") == [("baby carrots", "1 bag"), ("hummus", "")]
    assert p.parse_list("1 can peas and carrots, a pint blueberries") == [("peas and carrots", "1 can"), ("blueberries", "1 pint")], \
        p.parse_list("1 can peas and carrots, a pint blueberries")
    for n, a in parsed:
        p.add(n, a)
    p.add("milk", "1 gallon", use_by="2020-01-01")
    items = {i["name"]: i for i in p.items()}
    assert items["chicken"]["location"] == "fridge" and 1.5 < items["chicken"]["days_left"] <= 2.01
    assert items["milk"]["days_left"] < 0
    assert p.use("the eggs")["found"] and "eggs" not in {i["name"] for i in p.items()}
    assert not p.use("caviar")["found"]
    p.add("chicken", "3 lb")                       # topping up keeps one line
    assert sum(1 for i in p.items() if i["name"] == "chicken") == 1
    assert await p.sync_shopping() == 2 and ("todo", "remove_completed_items", {"entity_id": "todo.shopping_list"}) in ha.calls
    assert await p.sync_shopping() == 0            # the same ticked items aren't added twice
    day = await p.today()
    assert day["tired"] and "readiness 42" in day["tired_why"]
    plan = await p.suggest("dinner")
    assert plan["meals"][0]["name"].startswith("Beef") and "Tired day" in models.chef.last
    assert "mushrooms" in models.chef.last and "spinach" in models.chef.last
    soon = [i["name"] for i in p.soon(2)]
    assert "milk" in soon and "chicken" in soon, soon
    assert "past its date" in p.lines()
    # nutrition evidence: the curated studies, the ones that apply, the protein target
    studies = nutrition.load()
    assert len(studies) >= 40 and all(s["study"] and s["evidence"] for s in studies)
    rel = [s["topic"] for s in nutrition.relevant(["canned chickpeas", "almonds", "root beer"])]
    assert "Protein" in rel and "Pulses & LDL cholesterol" in rel and "Sugar-sweetened beverages" in rel, rel
    assert "Eggs & heart disease" not in rel
    assert "STUDIES THAT APPLY" in models.chef.last and "Portion size" in models.chef.last
    assert nutrition.protein_target(200) == 145 and nutrition.protein_target(None) is None
    st.remember(text="200 lb", title="Weigh-in", shelf="body/weight", meta={"weight": 200.0})
    assert (await p.today())["protein_target_g"] == 145
    n1 = nutrition.file_into(st)
    assert n1 == len(studies) and nutrition.file_into(st) == 0        # filed once, not again on the next start
    assert any("Pulses" in r["title"] for r in st.recall("do chickpeas help cholesterol", limit=5))
    # a reply cut off mid-meal still gives the meals that finished, and the prompt asks for compact output
    from homemind.pantry import parse_plan, MEAL_SYSTEM
    whole = '{"focus": "tired day", "meals": [{"name": "Egg rice bowl", "uses": ["eggs", "rice"], "protein_g": 25}, ' \
            '{"name": "Bean tacos", "uses": ["beans"], "protein_g": 20}, {"name": "Chicken so'
    cut = parse_plan(whole)
    assert cut["focus"] == "tired day" and [m["name"] for m in cut["meals"]] == ["Egg rice bowl", "Bean tacos"], cut
    assert cut["cut_off"] and parse_plan("no json here") == {}
    assert parse_plan('{"focus": "x", "meals": []}') == {"focus": "x", "meals": []}
    assert "under 35 words" in MEAL_SYSTEM
    # a logged meal gets a protein / calorie estimate saved on it, which Today adds up
    from homemind import today
    from homemind.main import meal_time
    from datetime import datetime as _dt
    models.chef.chat = None

    class Est:
        type, cfg = "mock", {}

        async def chat(self, system, msgs, **k):
            Est.prompt, Est.system = msgs[0]["content"], system
            return {"text": '{"protein_g": 24.6, "calories": 410, "carbs_g": 52.4, "fiber_g": 9, "sugar_g": 4, '
                            '"fat_g": 8, "junk": 1}', "ms": 1}
    models.models["haiku"] = Est()
    models.get = lambda n: models.models["haiku"]
    p.add("Bumble Bee tuna", "2 cans", "pantry")
    # a given time is used as given (meal_time puts a time still ahead today on yesterday, so it's checked apart
    # from Today: this test runs at any hour)
    early = p.ate("breakfast: oats", when=meal_time("12:30pm", _dt(2026, 11, 16, 13)))
    assert early["id"] and early["at"] == "12:30 PM", early
    lg = p.ate("lunch: tuna and chickpea penne")          # now: always today
    assert lg["id"], lg
    e = await p.estimate(lg["id"])
    assert e == {"id": lg["id"], "protein_g": 25, "calories": 410, "carbs_g": 52, "fiber_g": 9, "sugar_g": 4,
                 "fat_g": 8}, e
    assert "fiber_g" in Est.system
    assert "Bumble Bee tuna" in Est.prompt, Est.prompt
    class _M:
        store = st

    class _A:
        mind, pantry, patterns, ha = _M, p, None, None
    day = await today.build(_A)
    mine = [m for m in day["meals"] if m["id"] == lg["id"]][0]
    assert mine["protein_g"] == 25 and mine["estimated"] and day["protein_g"] >= 25, day
    assert mine["fiber_g"] == 9 and mine["carbs_g"] == 52
    from homemind import hub
    tot = hub.food_totals({"meals": [mine, {"protein_g": 10, "calories": 200}], "protein_g": 35})
    assert tot == {"protein": 35, "calories": 610, "carbs": 52, "fiber": 9, "sugar": 4, "fat": 8, "full": 1}, tot
    gl = hub.food_goals({"calories": 2000}, 120)
    assert gl["fiber"] == [30, "min"] and gl["sugar"] == [50, "max"] and gl["carbs"] == [225, "max"] \
        and gl["fat"] == [67, "max"] and gl["protein"] == [120, "min"], gl
    assert set(hub.food_goals({}, None)) == {"fiber", "sugar"}
    Est.chat = lambda self, *a, **k: asyncio.sleep(0, {"text": "no idea", "ms": 1})
    assert await p.estimate(p.ate("snack: something")["id"]) is None     # a bad reply saves nothing
    assert meal_time("8am", _dt(2026, 11, 16, 13)) == _dt(2026, 11, 16, 8).timestamp()
    assert meal_time("11pm", _dt(2026, 11, 16, 13)) == _dt(2026, 11, 15, 23).timestamp() and meal_time("lunch") is None
    # meal_request, format_plan, pantry context in Mind, and App.answer meal routing
    from homemind.pantry import meal_request, format_plan, PANTRY_Q
    from homemind.mind import Mind, OPEN_ENDED_SYSTEM
    from homemind.main import App
    r_eggs = meal_request("What can I make with the large eggs?")
    assert r_eggs == {"meal": None, "request": "What can I make with the large eggs"}, r_eggs
    assert meal_request("What should I cook for dinner?")["meal"] == "dinner"
    assert meal_request("Suggest a quick breakfast")["meal"] == "breakfast"
    assert meal_request("Give me a recipe with chicken") is not None
    for non_meal in ("Remember I bought eggs", "I ate eggs", "Do I have any eggs?",
                     "How much protein did I eat today?", "Why am I bloated after eating eggs?",
                     "What is the wifi password?"):
        assert meal_request(non_meal) is None, non_meal
    assert PANTRY_Q.search("Do I have any milk in the fridge?") and not PANTRY_Q.search("What is the wifi password?")
    sample_plan = {"focus": "normal", "meals": [
        {"name": "Egg & Spinach Scramble", "uses": ["large eggs", "baby spinach"], "missing": ["feta"],
         "why": "uses eggs due today", "protein_g": 28, "calories": 340, "steps": "Whisk and scramble."},
        {"name": "Egg Rice Bowl", "uses": ["large eggs", "rice"], "protein_g": 24, "calories": 420,
         "steps": "Fry egg over warm rice."}]}
    screen_txt = format_plan(sample_plan, voice=False)
    assert "1. Egg & Spinach Scramble (~28 g protein, ~340 kcal est.)" in screen_txt, screen_txt
    assert "Need: feta" in screen_txt and "Steps: Whisk and scramble." in screen_txt, screen_txt
    voice_txt = format_plan(sample_plan, voice=True)
    assert "\n" not in voice_txt and "about 28 grams of protein" in voice_txt and voice_txt.endswith("Want the steps?"), voice_txt
    assert format_plan({"meals": []}) is None

    # Mind.context_for includes PANTRY for food questions and Mind._ask relaxes 1-sentence limit for open-ended asks
    seen_sys = []

    class CapModel:
        type, cfg = "mock", {}

        async def chat(self, system, msgs, **k):
            seen_sys.append((system, msgs[-1]["content"], list(msgs)))
            if "Reply with JSON only" in system:
                return {"text": json.dumps(sample_plan), "ms": 1}
            return {"text": "Scramble them with spinach.", "ms": 1}

    class CapModels:
        def __init__(self):
            self.m = CapModel()
            self.models = {"haiku": self.m}

        def get(self, n):
            return self.m

    cm = CapModels()
    mind = Mind(tempfile.mkdtemp(), cm)
    p2 = Pantry(mind.store, ha, cm, tempfile.mkdtemp())
    p2.add("large eggs", "6", "fridge", use_by=_dt.now().strftime("%Y-%m-%d"))
    p2.add("baby spinach", "1 bag", "fridge")
    mind.pantry = p2
    _, _, ctx_food = await mind.context_for(mind.store, "Do I have large eggs in the fridge?")
    assert "PANTRY (" in ctx_food and "large eggs" in ctx_food and "use today" in ctx_food, ctx_food
    _, _, ctx_other = await mind.context_for(mind.store, "What is the wifi password?")
    assert "PANTRY (" not in ctx_other, ctx_other

    await mind.ask("What is the wifi password?", model="haiku")
    assert "Answer in one short sentence" in seen_sys[-1][0]
    await mind.ask("How do I organize my garage?", model="haiku")
    assert OPEN_ENDED_SYSTEM in seen_sys[-1][0] and "Answer in one short sentence" not in seen_sys[-1][0]

    # App.answer routes meal ideas (like the Home screen's 'Meal idea' button) to Pantry.suggest()
    class MiniApp:
        opts = {"connected_answers": True}
        ha = FakeHA()
        models = cm
        pantry = p2
        HANDOFF_LATE = 60
        DEEP_VOICE_SECONDS = 6.0
        speaker_for = lambda self, b: (b.get("speaker"), "given") if b.get("speaker") is not None else (None, "none")
        has_pending = lambda self, cid: False
        _style = lambda self, res, *a: asyncio.sleep(0, res)
        meal_ok = App.meal_ok
        meal_answer = App.meal_answer
        _deep_chain = App._deep_chain
        answer = App.answer

    ma = MiniApp()
    ma.mind = mind
    ans = await ma.answer({"question": "What can I make with the large eggs?", "conversation_id": "conv-meal"})
    assert ans.get("pantry") and "Egg & Spinach Scramble" in ans["answer"] and "large eggs" in ans["answer"], ans
    # a follow-up in the same conversation receives the prior meal suggestion in messages
    await ma.answer({"question": "How do I make the first one without spinach?", "conversation_id": "conv-meal"})
    last_msgs = seen_sys[-1][2]
    assert len(last_msgs) == 3 and last_msgs[1]["role"] == "assistant" and "Egg & Spinach Scramble" in last_msgs[1]["content"], last_msgs
    # a guest does not trigger Pantry.suggest and does not receive prior conversation turns
    guest_ans = await ma.answer({"question": "What can I make with the large eggs?", "speaker": "", "conversation_id": "conv-meal"})
    assert not guest_ans.get("pantry") and len(seen_sys[-1][2]) == 1, (guest_ans, seen_sys[-1][2])

    # ---- weekly meal plan + smart shopping list + single-meal swap ----
    from homemind.pantry import build_shopping_list, week_plan_request, format_week_plan

    assert week_plan_request("Plan my meals for the week") == {
        "days": 7, "meals": ["dinner"], "request": "Plan my meals for the week", "auto_shop": False}
    w5 = week_plan_request("Build a 5 day lunch and dinner meal plan and add to my shopping list")
    assert w5 and w5["days"] == 5 and w5["meals"] == ["lunch", "dinner"] and w5["auto_shop"] is True, w5
    assert week_plan_request("What is the wifi password?") is None

    # Deterministic shopping list: merges matching quantities, subtracts fresh stock, groups by aisle!
    sample_days = [
        {"day": "Mon Nov 23", "meals": [
            {"slot": "dinner", "name": "Chicken Broccoli Bowl", "uses": ["large eggs"],
             "need": [{"item": "broccoli", "amount": "1 head", "aisle": "produce"},
                      {"item": "chicken breast", "amount": "1 lb", "aisle": "meat & seafood"},
                      {"item": "baby spinach", "amount": "1 bag", "aisle": "produce"}],
             "protein_g": 45, "calories": 520, "why": "high protein", "steps": "Cook and serve."}]},
        {"day": "Tue Nov 24", "meals": [
            {"slot": "dinner", "name": "Spicy Chicken Stir Fry", "uses": [],
             "need": [{"item": "broccoli", "amount": "2 heads", "aisle": "produce"},
                      {"item": "chicken breast", "amount": "1 lb", "aisle": "meat & seafood"},
                      {"item": "soy sauce", "amount": "1 bottle", "aisle": "pantry & dry"}],
             "protein_g": 42, "calories": 490, "why": "overlaps broccoli & chicken", "steps": "Stir fry."}]},
    ]
    sl = build_shopping_list(sample_days, p2.items())
    by_item = {x["item"].lower(): x for x in sl}
    assert "baby spinach" not in by_item, sl          # already in stock -> subtracted!
    assert by_item["broccoli"]["amount"] == "3 heads" and len(by_item["broccoli"]["meals"]) == 2, by_item
    assert by_item["chicken breast"]["amount"] == "2 lb", by_item
    assert [x["aisle"] for x in sl] == ["produce", "meat & seafood", "pantry & dry"], sl

    # End-to-end plan_week, live recomputation after buying an item, swap_meal, and App.answer routing
    week_reply = {"summary": "High-protein week using up eggs and spinach.", "days": sample_days}
    swap_reply = {
        "slot": "dinner", "name": "Garlic Salmon & Asparagus", "uses": [],
        "need": [{"item": "salmon", "amount": "1 lb", "aisle": "meat & seafood"},
                 {"item": "asparagus", "amount": "1 bunch", "aisle": "produce"}],
        "protein_g": 40, "calories": 480, "why": "lighter fish night", "steps": "Roast 15 min.",
    }

    class WeekModel:
        type, cfg = "mock", {}

        async def chat(self, system, msgs, **k):
            seen_sys.append((system, msgs[-1]["content"], list(msgs)))
            if "replace ONE meal" in system:
                return {"text": json.dumps(swap_reply), "ms": 1}
            if "multi-day meal plan" in system:
                return {"text": json.dumps(week_reply), "ms": 1}
            return {"text": json.dumps(sample_plan), "ms": 1}

    class WeekModels:
        def __init__(self):
            self.m = WeekModel()
            self.models = {"haiku": self.m}

        def get(self, n):
            return self.m

    wm = WeekModels()
    p3 = Pantry(mind.store, ha, wm, tempfile.mkdtemp())
    p3.add("large eggs", "6", "fridge")
    p3.add("baby spinach", "1 bag", "fridge")
    wp = await p3.plan_week(days=2, meals=["dinner"], request="high protein")
    assert len(wp["days"]) == 2 and len(wp["shopping_list"]) == 3, wp
    assert p3.week_plan()["shopping_items"] == ["3 heads broccoli", "2 lb chicken breast", "1 bottle soy sauce"], p3.week_plan()

    # Adding soy sauce to pantry automatically removes it from saved week_plan()'s shopping list!
    p3.add("soy sauce", "1 bottle", "pantry")
    assert "1 bottle soy sauce" not in p3.week_plan()["shopping_items"], p3.week_plan()["shopping_items"]

    # Swap day 1 dinner -> updates meal AND recomputes shopping list cleanly!
    sw = await p3.swap_meal(1, slot="dinner", request="something lighter with fish")
    assert sw["days"][1]["meals"][0]["name"] == "Garlic Salmon & Asparagus", sw
    sw_items = {x["item"].lower() for x in sw["shopping_list"]}
    assert "salmon" in sw_items and "asparagus" in sw_items and "soy sauce" not in sw_items, sw["shopping_list"]
    assert sw["shopping_list"][0]["aisle"] == "produce"

    # Formatters (screen + voice)
    w_scr = format_week_plan(sw, voice=False)
    w_voc = format_week_plan(sw, voice=True)
    assert "Chicken Broccoli Bowl" in w_scr and "Garlic Salmon & Asparagus" in w_scr and "Shopping list (" in w_scr, w_scr
    assert "\n" not in w_voc and "2-day meal plan" in w_voc, w_voc

    # App.answer routes weekly meal plan asks and populates missing for Talk's shopping chip!
    ma.models = wm
    ma.pantry = p3
    w_ans = await ma.answer({"question": "Help me build a 2 day meal plan for the week", "conversation_id": "conv-week"})
    assert w_ans.get("pantry") and w_ans.get("week_plan") and w_ans.get("missing"), w_ans
    assert "Chicken Broccoli Bowl" in w_ans["answer"] and "Shopping list" in w_ans["answer"], w_ans

    p3.clear_week_plan()
    assert p3.week_plan() is None
    print("PANTRY OK")


asyncio.run(main())
