"""Knowledge: reference data kept on the Home Assistant machine, for facts that aren't about the user.

Food (USDA FoodData Central)
  Foundation Foods and SR Legacy (lab-measured generic foods), Branded Foods (label data with UPC barcodes for
  about 400,000 US products) and FNDDS survey foods (foods and mixed dishes as people eat them, "chili with beans",
  with gram weights for portions like "1 cup") go into knowledge/food.db: per 100 g and per serving - calories,
  protein, carbs, fat, fiber, sugar, sodium. The pantry and meal logging use it so protein and calories are real numbers, not guesses.

Wikipedia (Kiwix ZIM, English, text only)
  Downloaded once (about 50 GB, resumable, only inside the download window - default overnight), searched with the
  full-text index built into the file. For general questions (not about the user) Home Mind adds the best two
  passages to what the model sees, labelled with the snapshot date; the user's own notes always win.

Everything lives in /data/knowledge, which is left out of Home Assistant backups (it can be downloaded again).
"""
import asyncio
import csv
import html
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import zipfile
from datetime import datetime

log = logging.getLogger("homemind.knowledge")

FDC = "https://fdc.nal.usda.gov/fdc-datasets/"
FDC_FILES = {           # newest at the time of writing; refresh() looks for newer ones on the download page
    "sr_legacy": "FoodData_Central_sr_legacy_food_csv_2018-04.zip",
    "foundation": "FoodData_Central_foundation_food_csv_2026-10-29.zip",
    "branded": "FoodData_Central_branded_food_csv_2026-10-29.zip",
    "survey": "FoodData_Central_survey_food_csv_2024-10-31.zip",     # FNDDS: foods and dishes as eaten, portions
}
FOOD_VERSION = 2        # 2: FNDDS survey foods and portion weights
KIWIX_DIR = "https://download.kiwix.org/zim/wikipedia/"
WIKI_FLAVOUR = "wikipedia_en_all_nopic"
NUTRIENTS = {           # FoodData Central nutrient ids
    "1008": "kcal", "2047": "kcal_atwater", "2048": "kcal_atwater2", "1003": "protein", "1005": "carbs",
    "1004": "fat", "1079": "fiber", "2000": "sugar", "1063": "sugar_alt", "1093": "sodium",
}
COLS = ["kcal", "protein", "carbs", "fat", "fiber", "sugar", "sodium"]
PERSONAL = re.compile(r"\b(my|me|i|i'm|i've|i'd|mine|our|we|us|myself)\b", re.I)
STOP = set("the a an of in on at to for is are was were be what who when where which how why does do did can could "
           "and or with from by about as it its that this than then there their into much many".split())


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class Knowledge:
    def __init__(self, data_dir, opts=None, http=None):
        o = opts or {}
        self.dir = os.path.join(data_dir, "knowledge")
        os.makedirs(self.dir, exist_ok=True)
        self.status_path = os.path.join(self.dir, "status.json")
        self.food_path = os.path.join(self.dir, "food.db")
        self.want_wiki = o.get("knowledge_wikipedia", True) is not False
        self.want_food = o.get("knowledge_food", True) is not False
        win = (o.get("knowledge_window") or "00:00-07:00").split("-")
        self.window = tuple(tuple(int(x) for x in w.split(":")) for w in win) if len(win) == 2 else None
        self.http = http                 # an aiohttp.ClientSession factory (set by the app)
        self.archive = None
        self._searcher = None
        self._task = None
        self.status = self._load()
        self._open_wiki()

    # ---- status --------------------------------------------------------------------------------------------------
    def _load(self):
        try:
            with open(self.status_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"food": {"state": "not started"}, "wiki": {"state": "not started"}}

    def _save(self):
        tmp = self.status_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.status, f)
        os.replace(tmp, self.status_path)

    def summary(self):
        s = json.loads(json.dumps(self.status))
        w = s.get("wiki", {})
        if w.get("total") and w.get("done") is not None:
            w["percent"] = round(100 * w["done"] / w["total"], 1)
        du = shutil.disk_usage(self.dir)
        s["disk_free_gb"] = round(du.free / 1e9, 1)
        s["food_ready"] = self.food_ready()
        s["wiki_ready"] = self.archive is not None
        return s

    def in_window(self, now=None):
        if not self.window:
            return True
        n = (now or datetime.now())
        t = (n.hour, n.minute)
        a, b = self.window
        return a <= t < b if a <= b else (t >= a or t < b)

    # ---- downloading -----------------------------------------------------------------------------------------------
    async def _download(self, url, dest, key, respect_window=False):
        """Resumable download to dest (via dest.part). Returns True when complete, False if paused."""
        import aiohttp
        part = dest + ".part"
        st = self.status.setdefault(key, {})
        have = os.path.getsize(part) if os.path.exists(part) else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        timeout = aiohttp.ClientTimeout(total=None, sock_read=120)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, headers=headers, allow_redirects=True) as r:
                if r.status == 416:                    # already complete
                    os.replace(part, dest)
                    return True
                if r.status not in (200, 206):
                    raise RuntimeError(f"{url}: HTTP {r.status}")
                if r.status == 200 and have:           # server ignored the range: start over
                    have = 0
                total = (r.content_length or 0) + have
                st.update(total=total, done=have, url=str(r.url), state="downloading", _t0done=have)
                free = shutil.disk_usage(self.dir).free
                if total - have > free - 5e9:
                    st["state"] = f"not enough disk: needs {(total - have) / 1e9:.1f} GB, {free / 1e9:.1f} GB free"
                    self._save()
                    return False
                self._save()
                t0, last = time.time(), time.time()
                with open(part, "ab" if have else "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 20):
                        f.write(chunk)
                        have += len(chunk)
                        if time.time() - last > 15:
                            st.update(done=have, speed_mbps=round((have - st.get("_t0done", 0)) * 8 / 1e6 /
                                                                  max(time.time() - t0, 1), 1))
                            self._save()
                            last = time.time()
                            if respect_window and not self.in_window():
                                st["state"] = "paused until the download window"
                                self._save()
                                return False
                st["done"] = have
        if total and have < total:
            return False
        os.replace(part, dest)
        return True

    # ---- food -----------------------------------------------------------------------------------------------------
    def food_ready(self):
        st = self.status.get("food", {})
        return os.path.exists(self.food_path) and st.get("state") == "ready" and (st.get("version") or 1) >= FOOD_VERSION

    async def build_food(self):
        st = self.status.setdefault("food", {})
        # a food.db from before FNDDS: add the survey foods to it instead of downloading everything again
        add = os.path.exists(self.food_path) and st.get("state") == "ready" and (st.get("version") or 1) < FOOD_VERSION
        files = {"survey": FDC_FILES["survey"]} if add else FDC_FILES
        st["state"] = "downloading"
        self._save()
        paths = {}
        for k, fn in files.items():
            dest = os.path.join(self.dir, fn)
            if not os.path.exists(dest):
                ok = await self._download(FDC + fn, dest, "food_dl")
                if not ok:
                    st["state"] = "download stopped"
                    self._save()
                    return False
            paths[k] = dest
        st["state"] = "building"
        self._save()
        if add:
            n = (st.get("foods") or 0) + await asyncio.to_thread(self._add_to_food_db, paths)
        else:
            n = await asyncio.to_thread(self._build_food_db, paths)
        st.update(state="ready", foods=n, built=time.time(), version=FOOD_VERSION, source="USDA FoodData Central "
                  + ", ".join(f[:-4].split("_csv_")[-1] for f in FDC_FILES.values()))
        self._save()
        for p in paths.values():          # keep the database, drop the zips
            try:
                os.remove(p)
            except OSError:
                pass
        return True

    def _build_food_db(self, paths):
        tmp = self.food_path + ".build"
        if os.path.exists(tmp):
            os.remove(tmp)
        db = sqlite3.connect(tmp)
        db.executescript("""
            CREATE TABLE foods (fdc_id INTEGER PRIMARY KEY, kind TEXT, name TEXT, brand TEXT, upc TEXT,
              serving_g REAL, serving_text TEXT, kcal REAL, protein REAL, carbs REAL, fat REAL, fiber REAL,
              sugar REAL, sodium REAL);
            CREATE VIRTUAL TABLE foods_fts USING fts5(name, brand, content='foods', content_rowid='fdc_id',
              tokenize='porter unicode61');
            CREATE INDEX foods_upc ON foods(upc);""")
        self._portion_table(db)
        total = 0
        for kind, path in paths.items():
            total += self._load_zip(db, kind, path)
        db.execute("INSERT INTO foods_fts(rowid, name, brand) SELECT fdc_id, name, coalesce(brand, '') FROM foods")
        db.commit()
        db.close()
        os.replace(tmp, self.food_path)
        return total

    @staticmethod
    def _portion_table(db):
        db.execute("CREATE TABLE IF NOT EXISTS portions (fdc_id INTEGER, text TEXT, g REAL)")
        db.execute("CREATE INDEX IF NOT EXISTS portions_id ON portions(fdc_id)")

    def _add_to_food_db(self, paths):
        """Load more of FoodData Central into the existing food.db (copied, then swapped in)."""
        tmp = self.food_path + ".build"
        shutil.copyfile(self.food_path, tmp)
        db = sqlite3.connect(tmp)
        self._portion_table(db)
        total = 0
        for kind, path in paths.items():
            db.execute("DELETE FROM foods WHERE kind=?", (kind,))
            total += self._load_zip(db, kind, path)
        db.execute("INSERT INTO foods_fts(foods_fts) VALUES('rebuild')")
        db.commit()
        db.close()
        os.replace(tmp, self.food_path)
        return total

    @staticmethod
    def _rows(z, suffix):
        name = next((n for n in z.namelist() if n.endswith("/" + suffix) or n == suffix), None)
        if not name:
            return iter(())
        return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8", newline=""))

    def _load_zip(self, db, kind, path):
        with zipfile.ZipFile(path) as z:
            foods = {}
            for r in self._rows(z, "food.csv"):
                foods[r["fdc_id"]] = {"name": r["description"], "kind": kind}
            if kind == "branded":
                for r in self._rows(z, "branded_food.csv"):
                    f = foods.get(r["fdc_id"])
                    if f:
                        unit = (r.get("serving_size_unit") or "").lower()
                        size = _num(r.get("serving_size"))
                        f.update(brand=(r.get("brand_name") or r.get("brand_owner") or "").strip() or None,
                                 upc=(r.get("gtin_upc") or "").lstrip("0") or None,
                                 serving_g=size if unit in ("g", "grm", "ml", "mlt") else None,
                                 serving_text=(r.get("household_serving_fulltext") or "").strip() or None)
            portions = []
            if kind != "branded":
                for r in self._rows(z, "food_portion.csv"):
                    if r["fdc_id"] not in foods:
                        continue
                    g = _num(r.get("gram_weight"))
                    text = (r.get("portion_description") or "").strip()
                    if not text or text.lower().startswith("quantity not specified"):
                        amt = _num(r.get("amount"))
                        text = " ".join(x for x in ((f"{amt:g}" if amt else ""), (r.get("modifier") or "").strip()) if x)
                    if g and text and not text.lower().startswith("quantity not specified"):
                        portions.append((int(r["fdc_id"]), text[:80], g))
            for r in self._rows(z, "food_nutrient.csv"):
                f = foods.get(r["fdc_id"])
                col = NUTRIENTS.get(r["nutrient_id"])
                if f is not None and col:
                    v = _num(r["amount"])
                    if v is not None:
                        f[col] = v
        n = 0
        rows = []
        for fid, f in foods.items():
            kcal = f.get("kcal") or f.get("kcal_atwater") or f.get("kcal_atwater2")
            if kcal is None and f.get("protein") is None:
                continue
            rows.append((int(fid), kind, f["name"].strip(), f.get("brand"), f.get("upc"), f.get("serving_g"),
                         f.get("serving_text"), kcal, f.get("protein"), f.get("carbs"), f.get("fat"), f.get("fiber"),
                         f.get("sugar") if f.get("sugar") is not None else f.get("sugar_alt"), f.get("sodium")))
            if len(rows) >= 5000:
                db.executemany("INSERT OR REPLACE INTO foods VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                n += len(rows)
                rows = []
        if rows:
            db.executemany("INSERT OR REPLACE INTO foods VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            n += len(rows)
        if portions:
            db.execute("DELETE FROM portions WHERE fdc_id IN (SELECT fdc_id FROM foods WHERE kind=?)", (kind,))
            db.executemany("INSERT INTO portions VALUES (?,?,?)", portions)
        db.commit()
        return n

    def food(self, name, limit=3):
        """Best matches for a food name: brand words favour branded products, otherwise lab data comes first."""
        if not os.path.exists(self.food_path):
            return []
        words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in STOP and len(w) > 1]
        if not words:
            return []
        db = sqlite3.connect(self.food_path)
        db.row_factory = sqlite3.Row
        try:
            q = " ".join(f'"{w}"' for w in words)
            rows = db.execute("""SELECT f.*, bm25(foods_fts, 4.0, 2.0) AS score FROM foods_fts
                                 JOIN foods f ON f.fdc_id = foods_fts.rowid WHERE foods_fts MATCH ?
                                 ORDER BY score LIMIT 60""", (q,)).fetchall()
            if not rows:              # loosen: any of the words
                rows = db.execute("""SELECT f.*, bm25(foods_fts, 4.0, 2.0) AS score FROM foods_fts
                                     JOIN foods f ON f.fdc_id = foods_fts.rowid WHERE foods_fts MATCH ?
                                     ORDER BY score LIMIT 60""", (" OR ".join(f'"{w}"' for w in words),)).fetchall()
        finally:
            db.close()
        out = []
        for r in rows:
            d = dict(r)
            text = f"{d['name']} {d.get('brand') or ''}".lower()
            hit = sum(1 for w in words if re.search(rf"\b{re.escape(w.rstrip('s') if len(w) > 3 else w)}", text))
            extra = len(re.findall(r"[a-z]+", d["name"].lower())) - hit
            brand_hit = bool(d.get("brand")) and any(w in (d["brand"] or "").lower() for w in words)
            generic = d["kind"] in ("foundation", "sr_legacy", "survey")
            d["rank"] = (hit * 3 + (4 if brand_hit else 0) + (2 if generic and not brand_hit else 0)
                         - 0.4 * max(extra, 0) - (0.5 if "raw" not in d["name"].lower() and generic and
                                                  "raw" in (name or "").lower() else 0))
            out.append(d)
        out.sort(key=lambda d: -d["rank"])
        seen, uniq = set(), []
        for d in out:                     # the same product is often listed more than once
            key = (d["name"].lower(), round(d.get("protein") or 0, 1), round(d.get("kcal") or 0))
            if key not in seen:
                seen.add(key)
                uniq.append(d)
        best = uniq[:limit]
        if best:
            db = sqlite3.connect(self.food_path)
            try:
                has = db.execute("SELECT 1 FROM sqlite_master WHERE name='portions'").fetchone()
                for d in best:
                    d["portions"] = [{"text": t, "g": g} for t, g in db.execute(
                        "SELECT text, g FROM portions WHERE fdc_id=? ORDER BY g LIMIT 8", (d["fdc_id"],))] if has else []
            finally:
                db.close()
        return [self._portion(d) for d in best]

    @staticmethod
    def _portion(d):
        res = {"name": d["name"].title() if d["name"].isupper() else d["name"], "brand": d.get("brand"),
               "source": {"branded": "label", "foundation": "USDA lab", "sr_legacy": "USDA lab",
                          "survey": "USDA as eaten"}.get(d["kind"], d["kind"]),
               "per_100g": {c: round(d[c], 1) for c in COLS if d.get(c) is not None}}
        if d.get("portions"):
            res["portions"] = d["portions"]
        if d.get("serving_g"):
            k = d["serving_g"] / 100
            res["serving"] = {"g": d["serving_g"], "text": d.get("serving_text"),
                              **{c: round(d[c] * k, 1) for c in COLS if d.get(c) is not None}}
        return res

    def by_upc(self, upc):
        if not os.path.exists(self.food_path):
            return None
        db = sqlite3.connect(self.food_path)
        db.row_factory = sqlite3.Row
        try:
            r = db.execute("SELECT * FROM foods WHERE upc=?", ((upc or "").lstrip("0"),)).fetchone()
        finally:
            db.close()
        return self._portion(dict(r)) if r else None

    # ---- Wikipedia --------------------------------------------------------------------------------------------------
    def _zim_files(self):
        return sorted(f for f in os.listdir(self.dir) if f.startswith(WIKI_FLAVOUR) and f.endswith(".zim"))

    def _open_wiki(self):
        files = self._zim_files()
        if not files:
            return
        try:
            from libzim.reader import Archive
            self.archive = Archive(os.path.join(self.dir, files[-1]))
            self.wiki_date = re.search(r"(\d{4}-\d{2})", files[-1]).group(1)
            self.status.setdefault("wiki", {}).update(state="ready", file=files[-1])
        except Exception as e:  # noqa: BLE001
            log.warning("wikipedia not opened: %s", e)
            self.archive = None

    async def latest_wiki_url(self):
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(KIWIX_DIR) as r:
                page = await r.text()
        names = sorted(set(re.findall(rf'({WIKI_FLAVOUR}_\d{{4}}-\d{{2}}\.zim)"', page)))
        if not names:
            raise RuntimeError("no Wikipedia file found on the Kiwix server")
        return KIWIX_DIR + names[-1], names[-1]

    async def get_wiki(self, force_window=False):
        st = self.status.setdefault("wiki", {})
        url, name = (st["url_planned"], st["name"]) if st.get("url_planned") else await self.latest_wiki_url()
        st.update(url_planned=url, name=name)
        dest = os.path.join(self.dir, name)
        if os.path.exists(dest):
            self._open_wiki()
            return True
        ok = await self._download(url, dest, "wiki", respect_window=not force_window)
        if ok:
            for old in self._zim_files():
                if old != name:
                    os.remove(os.path.join(self.dir, old))
            self._open_wiki()
            st.update(state="ready", finished=time.time())
            self._save()
        return ok

    def search(self, query, limit=3):
        """Articles for a query: a title match first, then the full-text index."""
        if not self.archive:
            return []
        from libzim.search import Query, Searcher
        from libzim.suggestion import SuggestionSearcher
        paths = []
        try:
            sug = SuggestionSearcher(self.archive).suggest(query)
            paths += list(sug.get_results(0, 2))
        except Exception:  # noqa: BLE001
            pass
        try:
            res = Searcher(self.archive).search(Query().set_query(query))
            paths += list(res.get_results(0, limit + 2))
        except Exception:  # noqa: BLE001
            pass
        out, seen = [], set()
        for p in paths:
            try:
                e = self.archive.get_entry_by_path(p)
                if e.is_redirect:
                    e = e.get_redirect_entry()
                if e.path in seen:
                    continue
                seen.add(e.path)
                out.append({"title": e.title, "path": e.path, "text": self._text(bytes(e.get_item().content))})
            except Exception:  # noqa: BLE001
                continue
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _text(raw):
        h = raw.decode("utf-8", "replace")
        h = re.sub(r"(?is)<(script|style|table|sup)[^>]*>.*?</\1>", " ", h)
        paras = re.findall(r"(?is)<p[^>]*>(.*?)</p>", h)
        txt = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", p))).strip() for p in paras]
        return [t for t in txt if len(t) > 40]

    def passages(self, question, n=2, chars=700):
        """The best paragraphs for a general question: each article's intro, plus its paragraph that shares the most
        words with the question."""
        if not self.archive or PERSONAL.search(question or ""):
            return []
        terms = {w for w in re.findall(r"[a-z0-9]+", question.lower()) if w not in STOP and len(w) > 2}
        if not terms:
            return []
        out = []
        words = [w for w in re.findall(r"[A-Za-z0-9'-]+", question) if w.lower() not in STOP and len(w) > 2]
        caps = " ".join(w for w in words if w[0].isupper())          # names are the best title search
        arts = (self.search(caps, limit=2) if caps else []) + self.search(" ".join(words), limit=n + 1)
        seen = set()
        for art in arts:
            if art["path"] in seen:
                continue
            seen.add(art["path"])
            paras = art["text"]
            if not paras:
                continue
            title_terms = set(re.findall(r"[a-z0-9]+", art["title"].lower()))
            body = " ".join(paras[:3]).lower()
            if not (terms & title_terms) and sum(1 for t in terms if t in body) < max(1, len(terms) // 2):
                continue                               # off-topic hit
            best = max(paras[1:12] or [""], key=lambda p: sum(1 for t in terms if t in p.lower()))
            text = paras[0] if not best or best == paras[0] else paras[0] + " ... " + best
            out.append({"title": art["title"], "text": text[:chars]})
            if len(out) >= n:
                break
        return out

    def reference_lines(self, question):
        ps = self.passages(question)
        if not ps:
            return ""
        return (f"REFERENCE (offline Wikipedia, {getattr(self, 'wiki_date', '')} snapshot; general facts, not about "
                f"the user):\n" + "\n".join(f"- {p['title']}: {p['text']}" for p in ps))

    # ---- the clock --------------------------------------------------------------------------------------------------
    async def tick(self):
        """Called from the upkeep loop: build the food database once, fetch Wikipedia inside the window."""
        if self._task and not self._task.done():
            return
        if self.want_food and not self.food_ready() and self.status.get("food", {}).get("state") not in ("failed",):
            self._task = asyncio.create_task(self._run(self.build_food(), "food"))
            return
        if self.want_wiki and not self.archive and self.in_window():
            self._task = asyncio.create_task(self._run(self.get_wiki(), "wiki"))

    def start_now(self, what):
        if self._task and not self._task.done():
            return "busy"
        coro = self.build_food() if what == "food" else self.get_wiki(force_window=True)
        self._task = asyncio.create_task(self._run(coro, what))
        return "started"

    async def _run(self, coro, key):
        try:
            await coro
        except Exception as e:  # noqa: BLE001
            log.exception("knowledge %s", key)
            st = self.status.setdefault(key, {})
            st.update(state="error (will retry)", error=str(e)[:200])
            self._save()
