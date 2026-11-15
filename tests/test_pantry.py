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
    st = Store(tempfile.mktemp())
    ha, models = FakeHA(), Models()
    p = Pantry(st, ha, models, tempfile.mkdtemp())
    parsed = p.parse_list("I bought chicken, 2 lb ground beef and a dozen eggs")
    assert parsed == [("chicken", ""), ("ground beef", "2 lb"), ("eggs", "a dozen")], parsed
    assert p.parse_list("a bag of baby carrots and some hummus") == [("baby carrots", "1 bag"), ("hummus", "")]
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
    print("PANTRY OK")


asyncio.run(main())
