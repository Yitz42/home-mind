"""Set 6 (0.28): export and import (#35), two-way Markdown sync (#67), the read-only SQL view (#70), saved views (#61)
and model comparison on a schedule (#34). No network needed.

  python tests/test_share.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: None
    sys.modules["aiohttp"] = m
from homemind import portable, sqlview  # noqa: E402
from homemind.mdsync import MdSync, parse  # noqa: E402
from homemind.views import Views  # noqa: E402
from homemind.compare import Compare  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.mind import Mind  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def new():
    return Store(os.path.join(tempfile.mkdtemp(), "m.db"))


def export_import():
    a = new()
    a.remember(text="The Brisk padlock is on the Tarn shed.", shelf="house")
    a.remember(text="The Tarn shed holds the lawn mower.", shelf="house")
    v1 = a.remember(text="The router is in the hall.", key="router|place", kind="fact")
    a.remember(text="The router is in the study.", key="router|place", kind="fact")
    a.remember(text="The bank PIN is 4821.")
    a.remember(text="Kitchen temperature 21.5 C.", source="home")
    data = portable.export(a, app_version="t")
    s = json.dumps(data)
    check("export is JSON with a format tag", data["format"] == "home-mind-export" and data["counts"]["items"] >= 5)
    check("secrets leave masked", "4821" not in s, [i["body"] for i in data["items"] if "PIN" in i["body"]])
    check("house readings are left out", "Kitchen temperature" not in s)
    check("links travel with the notes", data["counts"]["links"] >= 1, data["counts"])
    check("secrets can be asked for", "4821" in json.dumps(portable.export(a, secrets=True)))
    b = new()
    b.remember(text="Something already here.")
    r = portable.import_(b, json.loads(s))
    check("import adds every note", r["added"] == data["counts"]["items"] and r["bad"] == 0, r)
    old = b.db.execute("SELECT id FROM items WHERE body='The router is in the hall.'").fetchone()[0]
    newer = b.db.execute("SELECT supersedes, status FROM items WHERE body='The router is in the study.'").fetchone()
    check("versions follow the new ids", newer[0] == old and
          b.db.execute("SELECT status FROM items WHERE id=?", (old,)).fetchone()[0] == "superseded", (newer[0], old, v1))
    check("links follow the new ids", b.db.execute("SELECT COUNT(*) FROM links").fetchone()[0] >= 1)
    hits = b.recall("Where is the router?")
    check("imported notes are found, newest version first", hits and "study" in hits[0]["body"], [h["body"] for h in hits])
    check("masked secrets aren't imported twice into their own memory", portable.import_(a, json.loads(s))["added"] == 0)
    again = portable.import_(b, json.loads(s))
    check("importing twice adds nothing", again["added"] == 0 and again["skipped"] == data["counts"]["items"], again)
    check("a dry run changes nothing", portable.import_(new(), json.loads(s), dry_run=True)["added"] == data["counts"]["items"])
    for bad, why in [({}, "no format"), ({"format": "home-mind-export", "version": 99, "items": []}, "newer version"),
                     ({"format": "home-mind-export", "version": 1, "items": "x"}, "items not a list"), ([], "a list")]:
        try:
            portable.import_(new(), bad)
            check(f"bad import refused: {why}", False)
        except ValueError:
            check(f"bad import refused: {why}", True)
    junk = {"format": "home-mind-export", "version": 1, "items": [None, {"body": 5}, {"body": "x", "created": "no"},
                                                                   {"body": "ok", "created": time.time(), "id": 9}],
            "links": [{"a": 9}, "x", {"a": 9, "b": 100}]}
    r = portable.import_(new(), junk)
    check("broken items are counted, good ones kept", r["added"] == 1 and r["bad"] == 3, r)


def markdown():
    st = new()
    fold = tempfile.mkdtemp()
    a = st.remember(text="The spare key is under the blue pot.", shelf="house/keys")
    st.remember(text="The wifi password is lemon11.")
    st.remember(text="Kitchen temperature 21.5 C.", source="home")
    sync = MdSync(st, fold)
    r = sync.run()
    files = {os.path.relpath(os.path.join(d, f), fold): open(os.path.join(d, f)).read()
             for d, _, fs in os.walk(fold) for f in fs}
    check("notes are written one folder per shelf", any(p.startswith(os.path.join("house", "keys")) for p in files), files)
    check("secrets and house readings aren't written", not any("lemon11" in t or "Kitchen" in t for t in files.values()))
    check("a second run writes nothing", sync.run()["written"] == 0)
    p = os.path.join(fold, [k for k in files if "spare" in k][0])
    t = open(p).read().replace("blue pot", "green pot")
    open(p, "w").write(t)
    r = sync.run()
    hits = st.recall("Where is the spare key?")
    check("editing a file makes a new version", r["updated_notes"] == 1 and "green pot" in hits[0]["body"] and
          st.get(a["id"])["status"] == "superseded", (r, [h["body"] for h in hits]))
    head, _, _ = parse(open([os.path.join(d, f) for d, _, fs in os.walk(fold) for f in fs if "spare" in f][0]).read())
    check("the file now names the new version", int(head["id"]) != a["id"], head)
    os.makedirs(os.path.join(fold, "garden"), exist_ok=True)
    open(os.path.join(fold, "garden", "tomatoes.md"), "w").write("# Tomatoes\n\nWater the tomatoes every second day.\n")
    r = sync.run()
    hit = st.recall("How often do I water the tomatoes?")
    check("a new file becomes a note on its folder's shelf", r["new_notes"] == 1 and hit and hit[0]["shelf"] == "garden",
          (r, hit[:1]))
    check("... and gets its id", not os.path.exists(os.path.join(fold, "garden", "tomatoes.md")) and
          any(f.startswith(str(hit[0]["id"])) for f in os.listdir(os.path.join(fold, "garden"))))
    open(os.path.join(fold, "secret.md"), "w").write("The safe combination is 2468.")
    sync.run()
    check("a secret written in a file is kept but not written back",
          not any("2468" in open(os.path.join(d, f)).read() for d, _, fs in os.walk(fold) for f in fs) and
          st.db.execute("SELECT COUNT(*) FROM items WHERE body LIKE '%safe combination%'").fetchone()[0] == 1)
    gfile = [os.path.join(fold, "garden", f) for f in os.listdir(os.path.join(fold, "garden"))][0]
    os.remove(gfile)
    r = sync.run()
    check("deleting a file archives the note", r["archived"] == 1 and st.get(hit[0]["id"])["status"] == "archived", r)
    check("... and its empty folder goes", not os.path.exists(os.path.join(fold, "garden")))
    k = st.recall("Where is the spare key?")[0]
    st.set_status(k["id"], "archived")
    r = sync.run()
    check("archiving a note deletes its file", r["removed_files"] == 1 and
          not any("spare" in f for _, _, fs in os.walk(fold) for f in fs), r)
    b = st.remember(text="The ladder is in the garage.", shelf="garage")
    sync.run()
    bf = [os.path.join(d, f) for d, _, fs in os.walk(fold) for f in fs if "ladder" in f][0]
    st.revise(b["id"], body="The ladder is in the shed.")
    t = open(bf).read()
    open(bf, "w").write(t.replace("garage.", "attic."))
    r = sync.run()
    files = [os.path.join(d, f) for d, _, fs in os.walk(fold) for f in fs if "ladder" in f]
    check("both changed: the note wins and the file's text is kept aside", r["conflicts"] == 1 and
          any(f.endswith(".conflict.md") and "attic" in open(f).read() for f in files) and
          any(not f.endswith(".conflict.md") and "shed" in open(f).read() for f in files), (r, files))
    c = st.remember(text="The hose is by the back door.", shelf="garden")
    sync.run()
    st.revise(c["id"], body="The hose is in the shed.")
    sync.run()
    cf = [os.path.join(d, f) for d, _, fs in os.walk(fold) for f in fs if "hose" in f and not f.endswith("conflict.md")]
    check("a note changed elsewhere rewrites its file", len(cf) == 1 and "in the shed" in open(cf[0]).read(), cf)
    import shutil
    shutil.copy(cf[0], cf[0].replace(".md", "-copy.md"))
    r = sync.run()
    open(cf[0], "w").write("")
    sync.run()
    check("an emptied file is written again from its note", "in the shed" in open(cf[0]).read() and
          st.recall("Where is the hose?")[0]["body"] == "The hose is in the shed.")
    check("a copied file becomes its own note", r["new_notes"] == 1, r)
    check("an unset folder does nothing", MdSync(st, None).run() == {"enabled": False})


def sql():
    st = new()
    st.remember(text="The shed code is 4412.")
    st.remember(text="Milk is in the fridge.", shelf="kitchen")
    r = sqlview.query(st.path, "SELECT shelf, count(*) n FROM notes GROUP BY shelf ORDER BY shelf")
    check("SQL counts notes per shelf", r["rows"] == [["inbox", 1], ["kitchen", 1]], r)
    r = sqlview.query(st.path, "SELECT body FROM notes")
    check("secret text is hidden", all("4412" not in str(x) for x in r["rows"]), r)
    for bad in ["SELECT * FROM items", "SELECT * FROM kv", "DELETE FROM notes", "UPDATE notes SET body='x'",
                "SELECT 1; DROP TABLE items", "ATTACH 'x' AS y", "PRAGMA table_info(items)", "SELECT * FROM main.items",
                "SELECT * FROM sqlite_master", "SELECT (SELECT secret FROM items)", "SELECT load_extension('x')",
                "INSERT INTO links VALUES(1,2,'x')", "", "CREATE TABLE t(x)"]:
        try:
            sqlview.query(st.path, bad)
            check(f"refused: {bad!r}", False)
        except sqlview.SqlError:
            check(f"refused: {bad!r}", True)
    check("nothing was changed", st.db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2)
    for i in range(600):
        st.db.execute("INSERT INTO links VALUES(?,?,'x')", (i, i + 1))
    st.db.commit()
    r = sqlview.query(st.path, "SELECT * FROM links")
    check("rows are capped", len(r["rows"]) == 500 and r["truncated"])
    old = sqlview.MAX_SECONDS
    sqlview.MAX_SECONDS = 0.2
    try:
        sqlview.query(st.path, "SELECT count(*) FROM links a, links b, links c")
        check("a slow query is stopped", False)
    except sqlview.SqlError as e:
        check("a slow query is stopped", "stopped" in str(e), e)
    sqlview.MAX_SECONDS = old


async def views():
    st = new()
    st.remember(text="Warranty: dishwasher until 2028.", tags=["warranty"], shelf="papers")
    st.remember(text="Warranty: drill until 2027.", tags=["warranty"], shelf="papers")
    st.remember(text="Receipt for the lamp.", shelf="papers")
    st.remember(text="Buy more coffee.", shelf="kitchen")
    v = Views(st)
    v.save("warranties", shelf="papers", tags=["warranty"], sort="title")
    r = v.run("Warranties")
    check("a view filters by shelf and tags", [i["body"] for i in r["items"]] ==
          ["Warranty: dishwasher until 2028.", "Warranty: drill until 2027."], r["items"])
    v.save("coffee", query="coffee")
    check("a view can be a search", v.run("coffee")["items"][0]["body"] == "Buy more coffee.")
    v.save("per shelf", sql="SELECT shelf, count(*) FROM notes GROUP BY shelf")
    check("a view can be SQL", ["kitchen", 1] in v.run("per shelf")["rows"])
    for args, why in [({"name": ""}, "no name"), ({"name": "x"}, "nothing to do"),
                      ({"name": "x", "query": "a", "sort": "weird"}, "bad sort"),
                      ({"name": "x", "sql": "DELETE FROM notes"}, "unsafe SQL")]:
        try:
            v.save(**args)
            check(f"bad view refused: {why}", False)
        except ValueError:
            check(f"bad view refused: {why}", True)
    check("spoken names find views", v.find("show my warranties") == "warranties" and v.find("Open the warranty view")
          == "warranties" and v.find("what's the weather") is None)
    m = Mind(tempfile.mkdtemp(), None)
    m.views = Views(m.store)
    m.store.remember(text="Warranty: TV until 2029.", tags=["warranty"])
    m.views.save("warranties", tags=["warranty"])
    r = await m.ask("Show me my warranties")
    check("asking for a view answers from it, no model", r.get("view") == "warranties" and "TV" in r["answer"], r)
    check("removing a view", v.remove("coffee") and "coffee" not in v.all())
    try:
        v.run("coffee")
        check("a missing view is an error", False)
    except KeyError:
        check("a missing view is an error", True)


async def compare():
    class FakeMind:
        def __init__(self):
            self.store = new()
            self.models = types.SimpleNamespace(models={"a": 1, "b": 1, "mock": 1})
            self.acc = {"a": 0.9, "b": 0.7}

        async def run_test_now(self, suite, arm, model=None, seed=None, n=None):
            if model == "b" and suite == "hard":
                raise RuntimeError("model down")
            return {"status": "done", "summary": {"accuracy": self.acc[model], "ms_avg": 100 if model == "a" else 300}}
    fm = FakeMind()
    c = Compare(fm, [], 7)
    check("due when never run", c.due())
    r = await c.run(seed=5)
    check("every connected model but mock is compared", set(r["models"]) == {"a", "b"} and r["best"] == "a", r)
    check("a failing suite is counted, not fatal", r["models"]["b"]["errors"] == 1 and r["models"]["b"]["accuracy"] == 0.7)
    check("not due again for a week", not c.due())
    check("due after the period", c.due(time.time() + 8 * 86400))
    fm.acc["a"] = 0.8
    r = await c.run(seed=6)
    check("a drop of 5 points or more is flagged", r["dropped"] and r["dropped"][0]["model"] == "a", r["dropped"])
    t = c.table()
    check("the table shows each model's change", t["models"][0]["model"] == "a" and t["models"][0]["change"] == -0.1 and
          t["trend"]["a"] == [0.9, 0.8], t)
    check("off when days is 0", not Compare(fm, [], 0).due())
    check("only the chosen models", Compare(fm, ["b", "zzz"], 1).names() == ["b"])


export_import()
markdown()
sql()
asyncio.run(views())
asyncio.run(compare())
print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
