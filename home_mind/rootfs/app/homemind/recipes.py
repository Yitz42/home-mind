"""Saved recipes, favorite meals, and barcode/UPC nutrition quick-log.

Stores personal/household recipes with exact macros, ingredients, and prep notes in SQLite.
Supports:
- One-tap logging to today (or any day) on `body/meals` with exact scaled macros (no LLM wait).
- Barcode/UPC lookup via the local USDA branded-food database (`knowledge.by_upc`), ready to log or save.
- One-tap missing-ingredients push to the Home Assistant shopping list (subtracting in-stock pantry items).
"""
import json
import math
import re
import time
from datetime import datetime

from .speakers import VIEWER, visible

MACROS = (("calories", 6000), ("protein_g", 400), ("carbs_g", 600),
          ("fat_g", 300), ("fiber_g", 150), ("sugar_g", 300))
SLOTS = {"breakfast", "lunch", "dinner", "snack", "any"}


def _num_opt(v, name, hi):
    if v in (None, ""):
        return None
    if isinstance(v, bool):
        raise ValueError(f"{name} must be a number")
    try:
        fv = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(fv) or not (0 <= fv <= hi):
        raise ValueError(f"{name} must be between 0 and {hi}")
    return round(fv)


class Recipes:
    def __init__(self, store, pantry=None, knowledge=None):
        self.store = store
        self.pantry = pantry
        self.knowledge = knowledge
        with store.lock:
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS recipes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "name TEXT NOT NULL,"
                "slot TEXT DEFAULT 'any',"
                "servings REAL DEFAULT 1,"
                "minutes INTEGER,"
                "calories INTEGER,"
                "protein_g INTEGER,"
                "carbs_g INTEGER,"
                "fat_g INTEGER,"
                "fiber_g INTEGER,"
                "sugar_g INTEGER,"
                "ingredients TEXT DEFAULT '[]',"
                "steps TEXT DEFAULT '',"
                "upc TEXT DEFAULT '',"
                "owner TEXT,"
                "scope TEXT DEFAULT 'household',"
                "created REAL NOT NULL)"
            )
            store.db.commit()

    @staticmethod
    def _row(r):
        try:
            ing = json.loads(r["ingredients"] or "[]")
            if not isinstance(ing, list):
                ing = []
        except ValueError:
            ing = []
        return {
            "id": r["id"],
            "name": r["name"],
            "slot": r["slot"] or "any",
            "servings": r["servings"] or 1,
            "minutes": r["minutes"],
            "calories": r["calories"],
            "protein_g": r["protein_g"],
            "carbs_g": r["carbs_g"],
            "fat_g": r["fat_g"],
            "fiber_g": r["fiber_g"],
            "sugar_g": r["sugar_g"],
            "ingredients": ing,
            "steps": r["steps"] or "",
            "upc": r["upc"] or "",
            "owner": r["owner"],
            "scope": r["scope"] or "household",
            "created": r["created"],
        }

    def _visible_row(self, r):
        return visible({"scope": r["scope"] or "household", "owner": r["owner"]})

    def list(self, q=None, slot=None):
        rows = self.store.db.execute("SELECT * FROM recipes ORDER BY name COLLATE NOCASE").fetchall()
        out = []
        ql = (q or "").strip().lower()
        sl = (slot or "").strip().lower()
        for r in rows:
            if not self._visible_row(r):
                continue
            item = self._row(r)
            if sl and sl != "all" and item["slot"] not in (sl, "any"):
                continue
            if ql and ql not in item["name"].lower() and not any(ql in str(x).lower() for x in item["ingredients"]):
                continue
            out.append(item)
        return out

    def get(self, rid):
        r = self.store.db.execute("SELECT * FROM recipes WHERE id=?", (int(rid),)).fetchone()
        if not r or not self._visible_row(r):
            raise ValueError(f"no recipe {rid}")
        return self._row(r)

    def save(self, data):
        if not isinstance(data, dict):
            raise ValueError("give recipe object")
        name = str(data.get("name") or "").strip()[:120]
        if not name:
            raise ValueError("give recipe name")
        slot = str(data.get("slot") or "any").strip().lower()
        if slot not in SLOTS:
            slot = "any"
        serv = data.get("servings", 1)
        if isinstance(serv, bool):
            raise ValueError("servings must be a number")
        try:
            serv_f = float(serv if serv not in (None, "") else 1)
        except (TypeError, ValueError):
            raise ValueError("servings must be a number") from None
        if not math.isfinite(serv_f) or not (0.25 <= serv_f <= 50):
            raise ValueError("servings must be between 0.25 and 50")
        mins = _num_opt(data.get("minutes"), "minutes", 600)
        macs = {k: _num_opt(data.get(k), k, hi) for k, hi in MACROS}
        raw_ing = data.get("ingredients")
        if isinstance(raw_ing, str):
            ing = [x.strip() for x in raw_ing.replace("\n", ",").split(",") if x.strip()][:40]
        elif isinstance(raw_ing, list):
            ing = [str(x).strip() for x in raw_ing if str(x).strip()][:40]
        else:
            ing = []
        steps = str(data.get("steps") or "").strip()[:2000]
        upc = str(data.get("upc") or "").strip()[:32]
        scope = "personal" if data.get("scope") == "personal" else "household"
        owner = VIEWER.get() or None if scope == "personal" else None
        rid = data.get("id")
        now = time.time()
        with self.store.lock:
            if rid is not None and rid != "":
                cur = self.get(int(rid))
                next_owner = (owner or cur.get("owner")) if scope == "personal" else None
                self.store.db.execute(
                    "UPDATE recipes SET name=?, slot=?, servings=?, minutes=?, calories=?, protein_g=?, carbs_g=?, "
                    "fat_g=?, fiber_g=?, sugar_g=?, ingredients=?, steps=?, upc=?, scope=?, owner=? WHERE id=?",
                    (name, slot, round(serv_f, 2), mins, macs["calories"], macs["protein_g"], macs["carbs_g"],
                     macs["fat_g"], macs["fiber_g"], macs["sugar_g"], json.dumps(ing), steps, upc,
                     scope, next_owner, int(rid)),
                )
                self.store.db.commit()
                return self.get(int(rid))
            cur = self.store.db.execute(
                "INSERT INTO recipes (name, slot, servings, minutes, calories, protein_g, carbs_g, fat_g, fiber_g, "
                "sugar_g, ingredients, steps, upc, owner, scope, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, slot, round(serv_f, 2), mins, macs["calories"], macs["protein_g"], macs["carbs_g"],
                 macs["fat_g"], macs["fiber_g"], macs["sugar_g"], json.dumps(ing), steps, upc, owner, scope, now),
            )
            self.store.db.commit()
            return self.get(cur.lastrowid)

    def delete(self, rid):
        self.get(int(rid))
        with self.store.lock:
            self.store.db.execute("DELETE FROM recipes WHERE id=?", (int(rid),))
            self.store.db.commit()
        return {"removed": int(rid)}

    def lookup_barcode(self, upc):
        code = "".join(ch for ch in str(upc or "") if ch.isdigit())
        if len(code) < 6 or len(code) > 20:
            raise ValueError("barcode/UPC should be 6–20 digits")
        if self.knowledge is None:
            return {"upc": code, "match": None}
        hit = self.knowledge.by_upc(code)
        if not hit:
            return {"upc": code, "match": None}
        src = hit.get("serving") or hit.get("per_100g") or {}
        label = hit["name"] + (f" ({hit['brand']})" if hit.get("brand") else "")
        return {
            "upc": code,
            "match": {
                "name": label,
                "serving": hit.get("serving", {}).get("text") or (
                    f"{hit['serving']['g']:g} g" if hit.get("serving", {}).get("g") else "100 g"
                ),
                "calories": round(src["kcal"]) if src.get("kcal") is not None else None,
                "protein_g": round(src["protein"]) if src.get("protein") is not None else None,
                "carbs_g": round(src["carbs"]) if src.get("carbs") is not None else None,
                "fat_g": round(src["fat"]) if src.get("fat") is not None else None,
                "fiber_g": round(src["fiber"]) if src.get("fiber") is not None else None,
                "sugar_g": round(src["sugar"]) if src.get("sugar") is not None else None,
            },
        }

    def log_recipe(self, rid, servings=1.0, day=None, when=None, source="web"):
        rec = self.get(int(rid))
        if isinstance(servings, bool):
            raise ValueError("servings must be a number")
        try:
            mult = float(servings if servings not in (None, "") else 1.0)
        except (TypeError, ValueError):
            raise ValueError("servings must be a number") from None
        if not math.isfinite(mult) or not (0.25 <= mult <= 10):
            raise ValueError("servings must be between 0.25 and 10")
        ts = self.pantry.meal_day_time(day, when) if (day and self.pantry) else time.time()
        dt = datetime.fromtimestamp(ts)
        txt = rec["name"] if abs(mult - 1.0) < 0.01 else f"{rec['name']} ({mult:g}×)"
        meta = {"at": ts, "estimated": False, "recipe_id": rec["id"]}
        who = VIEWER.get()
        if who:
            meta.update(scope="personal", owner=who)
        for k, _ in MACROS:
            if rec.get(k) is not None:
                meta[k] = round(rec[k] * mult)
        r = self.store.remember(
            text=f"Ate {txt} at {dt:%I:%M %p}".replace(" 0", " ") + f" on {dt:%a %b %d}.",
            title=f"Ate: {txt}"[:80], kind="fact", shelf="body/meals",
            source=source, tags=["meal", "recipe"], meta=meta, at=ts,
        )
        return {"id": r.get("id"), "logged": txt, "calories": meta.get("calories"), "protein_g": meta.get("protein_g")}

    async def add_to_shopping(self, rid):
        rec = self.get(int(rid))
        if not rec["ingredients"]:
            return {"added": [], "skipped": []}
        if self.pantry is None:
            return {"added": [], "skipped": rec["ingredients"]}
        in_stock = [i["name"].strip().lower() for i in self.pantry.items("in") if i.get("name", "").strip()]

        def _has_ing(item):
            xl = item.strip().lower()
            if not xl:
                return False
            for s in in_stock:
                if s == xl or re.search(rf"\b{re.escape(s)}\b", xl) or re.search(rf"\b{re.escape(xl)}\b", s):
                    return True
            return False

        need = [x for x in rec["ingredients"] if not _has_ing(x)]
        skipped = [x for x in rec["ingredients"] if x not in need]
        added = await self.pantry.add_to_shopping(need) if need else []
        return {"added": added, "skipped": skipped}


def register_routes(r, A, J, read_body, whole):
    @r.get("/api/recipes")
    async def recipes_list(q):
        return J({"recipes": A.recipes.list(q=q.query.get("q"), slot=q.query.get("slot"))})

    @r.post("/api/recipes/{op}")
    async def recipes_op(q):
        op = q.match_info["op"]
        b = await read_body(q, optional=True, name=str, slot=str, steps=str, upc=str, scope=str, day=str, when=str)
        if op == "save":
            return J({"recipe": A.recipes.save(b)})
        if op == "delete":
            return J(A.recipes.delete(whole(b.get("id"), "id")))
        if op == "log":
            return J(A.recipes.log_recipe(
                whole(b.get("id"), "id"),
                servings=b.get("servings", 1.0),
                day=b.get("day") or None,
                when=b.get("when") or None,
            ))
        if op == "shop":
            return J(await A.recipes.add_to_shopping(whole(b.get("id"), "id")))
        if op == "barcode":
            return J(A.recipes.lookup_barcode(b.get("upc") or ""))
        return J({"error": f"unknown {op}"}, status=404)
