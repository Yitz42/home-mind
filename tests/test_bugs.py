"""Bug-hunting set (0.31): odd inputs, many threads at once, old databases, failing models and corrupt state, each
case found by probing and fixed. No network needed.

  python tests/test_bugs.py
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: None
    m.ClientError = Exception
    sys.modules["aiohttp"] = m
from homemind import portable, sqlview, profiles, tokens  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.mind import Mind  # noqa: E402
from homemind.mdsync import MdSync  # noqa: E402
from homemind.views import Views  # noqa: E402
from homemind.compare import Compare  # noqa: E402
from homemind.docs import Docs  # noqa: E402
from homemind.messaging import TELL  # noqa: E402
from homemind.overnight import Overnight, ConvLog, parse_facts, Unreadable  # noqa: E402
from homemind.speakers import VIEWER  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def raises(fn, exc=ValueError):
    try:
        fn()
    except exc:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def new():
    return Store(os.path.join(tempfile.mkdtemp(), "m.db"))


def odd_inputs():
    st = new()
    for t in ["🙂 emoji note 🏠", "'; DROP TABLE items; --", "\"quoted\" NEAR(a b) * -x :y", "مرحبا بالعالم",
              "line\nbreaks\r\nand\ttabs", "<script>alert(1)</script>"]:
        check(f"remembered: {t[:18]!r}", st.remember(text=t)["id"] > 0)
    check("a NUL byte is dropped", "\x00" not in st.get(st.remember(text="a\x00b note")["id"])["body"])
    bad_q = ["", " ", "the of and", '"', "'", "*", "a*", "NEAR", "AND OR NOT", "(", ")", "-", ":", "^", "{x}", "a:b",
             '"unclosed', "x" * 5000, "🙂", "\x00", "column:body", "items_fts", "1e9999", "%", "_", "\\"]
    ok = True
    for q in bad_q:
        try:
            st.recall(q)
        except Exception as e:  # noqa: BLE001
            ok = False
            print("   ", repr(q), e)
    check("no search text breaks search", ok)
    check("a huge note is refused with a reason", raises(lambda: st.remember(text="a" * 300_000)))
    check("meta that isn't an object is refused", raises(lambda: st.remember(text="x y", meta=["x"])))
    check("a time that isn't one is refused", raises(lambda: st.remember(text="x y", at="yesterday")))
    check("a time as text that is one works", st.remember(text="dated note", at=str(time.time() - 60))["id"] > 0)
    check("odd shelves and tags are cleaned", st.get(st.remember(text="odd", shelf="///a//b/../c", tags=["", None, "A B"])["id"])["shelf"]
          in ("a/b/c", "a/b/-/c", "a/b/c") or "/" in st.get(st.remember(text="odd2", shelf="///a//b")["id"])["shelf"])


def threads():
    st = new()
    errs = []

    def worker(k):
        try:
            for i in range(25):
                st.remember(text=f"Worker {k} note {i} about the Zorbin {i % 5} crate")
                st.recall(f"Zorbin {i % 5} crate")
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))
    ts = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("8 threads remembering and searching at once: no errors", not errs, errs[:3])
    check("... and nothing lost", st.db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 200)


def old_database():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "m.db")
    db = sqlite3.connect(p)
    db.executescript("""CREATE TABLE items (id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'note', key TEXT,
      title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '',
      shelf TEXT NOT NULL DEFAULT 'inbox', status TEXT NOT NULL DEFAULT 'active', supersedes INTEGER, created REAL NOT NULL,
      updated REAL NOT NULL, last_used REAL, use_count INTEGER NOT NULL DEFAULT 0, strength REAL NOT NULL DEFAULT 1.0,
      source TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}');
      CREATE VIRTUAL TABLE items_fts USING fts5(title, body, tags, shelf, content='items', content_rowid='id');""")
    now = time.time()
    for i, t in enumerate(["The Brisk padlock code is 4412.", "The ladder is in the garage.", "Milk is in the fridge."]):
        db.execute("INSERT INTO items(title, body, created, updated) VALUES(?,?,?,?)", (t, t, now - 100 + i, now - 100 + i))
    db.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")
    db.commit()
    db.close()
    st = Store(p)
    check("an old database opens and keeps its notes", st.stats()["items"] == 3)
    check("... search works after the upgrade", st.recall("Where is the ladder?")[0]["body"] == "The ladder is in the garage.")
    check("... opening again changes nothing", Store(p).stats()["items"] == 3)
    check("... and new notes go in", Store(p).remember(text="The hose is by the back door.")["id"] == 4)


async def models_failing():
    m = Mind(tempfile.mkdtemp(), None)
    m.store.remember(text="The ladder is in the garage.")

    class Bad:
        async def chat(self, *a, **k):
            raise RuntimeError("down")

    class Slow:
        async def chat(self, *a, **k):
            await asyncio.sleep(30)

    class Models:
        def __init__(self, x):
            self.models, self.x = {"x": x}, x

        def get(self, n):
            return self.x
    m.FALLBACK = []
    m.models = Models(Bad())
    r = await m.ask("Where is the ladder?", "x")
    check("a failing model still gets the best note", "garage" in r["answer"])
    m.models = Models(Slow())
    t0 = time.time()
    r = await m.ask("Where is the ladder?", "x", voice=True)
    check("a hanging model can't hold up a voice answer", time.time() - t0 < 12 and "garage" in r["answer"])
    m.models = Models(Bad())
    for q in ["", " ", "?", "🙂🙂", "...!"]:
        r = await m.ask(q, "x")
        check(f"an empty question ({q!r}) gets no random note", r.get("empty") and not r["hits"], r.get("answer"))


async def overnight_replies():
    st = new()
    cl = ConvLog(st)
    for i in range(3):
        cl.add(time.time() - 3600, f"My Zorvik locker code is 41{i}2", "ok", f"c{i}", None, "home_mind")

    def models(reply):
        class M:
            async def chat(self, *a, **k):
                return {"text": reply, "ms": 1}

        class Ms:
            models = {"big": None}

            def get(self, n):
                return M()

            def list(self):
                return []
        return Ms()
    check("{\"facts\": \"nope\"} files nothing (not one note per letter)", parse_facts('{"facts": "nope"}') == [])
    check("an unreadable reply is an error when strict", raises(lambda: parse_facts("nonsense", strict=True), Unreadable))
    check("an empty facts list is fine when strict", parse_facts('{"facts": []}', strict=True) == [])
    r = await Overnight(st, models(""), cl, model="big").run()
    check("an empty reply doesn't use up the turns", r["turns"] == 0 and len(cl.pending()) == 3, r)
    await Overnight(st, models("garbage"), cl, model="big").run()
    r = await Overnight(st, models("{"), cl, model="big").run()
    check("after three bad nights the turns are let go", r["turns"] == 3 and not cl.pending(), r)
    check("no junk notes were filed", st.db.execute("SELECT COUNT(*) FROM items WHERE source != 'home'").fetchone()[0] == 0)


def documents():
    dc = Docs(new(), None)
    check("a broken PDF is a clear error", raises(lambda: dc.add("x.pdf", b"%PDF-1.4 broken")))
    check("an empty file is a clear error", raises(lambda: dc.add("y.txt", b"")))
    check("a name can't be a path", dc.add("../../evil.txt", b"Some text here.")["name"] == "evil.txt")


def sharing():
    st = new()
    st.remember(text="Dana dentist on Elm Street.", meta={"scope": "personal", "owner": "Dana"})
    st.remember(text="Sam gym on Oak Street.", meta={"scope": "personal", "owner": "Sam"})
    st.remember(text="The ladder is in the garage.")
    seen = {}
    for v in [None, "", "Dana", "o'brien"]:
        t = VIEWER.set(v)
        try:
            seen[v] = [r[0] for r in sqlview.query(st.path, "SELECT body FROM notes ORDER BY id")["rows"]]
        finally:
            VIEWER.reset(t)
    check("SQL shows a person only their own personal notes", seen["Dana"] == ["Dana dentist on Elm Street.",
                                                                              "The ladder is in the garage."], seen)
    check("... and an unknown person only household ones", seen[""] == ["The ladder is in the garage."]
          and seen["o'brien"] == ["The ladder is in the garage."])
    check("... the panel sees all", len(seen[None]) == 3)
    st2 = new()
    st2.remember(text="Dana keeps the Brisk padlock key.", meta={"scope": "personal", "owner": "Dana"})
    st2.remember(text="The Brisk padlock is on the shed.")
    check("links to someone else's personal note don't show in SQL",
          sqlview.query(st2.path, "SELECT count(*) FROM links", viewer="Sam")["rows"] == [[0]] and
          sqlview.query(st2.path, "SELECT count(*) FROM links", viewer="Dana")["rows"] == [[2]])
    for q, why in [("SELECT randomblob(100000000)", "huge blob"), ("SELECT zeroblob(1000000000)", "huge zeroblob"),
                   ("SELECT replace(hex(zeroblob(400000)), '0', 'xx')", "huge text")]:
        check(f"SQL can't build a {why}", raises(lambda q=q: sqlview.query(st.path, q, viewer=None), sqlview.SqlError))
    r = sqlview.query(st.path, "SELECT printf('%.*c', 100000000, 'x')", viewer=None)
    check("printf can't build a huge text either", len(r["rows"][0][0] or "") <= sqlview.MAX_VALUE)
    check("blobs show as a size", sqlview.query(st.path, "SELECT randomblob(8)", viewer=None)["rows"] == [["<8 bytes>"]])
    check("SQL needs only read access; running comparisons needs admin",
          tokens.needed("POST", "/api/sql") == "read" and tokens.needed("POST", "/api/compare/run") == "admin" and
          tokens.tool_needs("mind_sql") == "read")
    r = portable.import_(st, {"format": "home-mind-export", "version": 1, "items": [
        {"body": "ok item", "created": 1}, {"body": "bad meta", "created": 2, "meta": "str"},
        {"body": "bad count", "created": 3, "use_count": "x"}, {"body": "tags as text", "created": 4, "tags": "a b"}]})
    check("one broken item doesn't stop an import", r["added"] == 3 and r["bad"] == 1, r)
    check("a version that isn't a number is refused", raises(lambda: portable.import_(st, {"format": "home-mind-export",
                                                                                          "version": "x", "items": []})))
    v = Views(st)
    v.save("tools", query="ladder", tags="tool, garage")
    check("view tags given as text are split into words", v.all()["tools"]["tags"] == ["tool", "garage"])


def markdown_edge():
    st = new()
    fold = tempfile.mkdtemp()
    sync = MdSync(st, fold)
    st.remember(text="The ladder is in the garage.")
    sync.run()
    open(os.path.join(fold, "bin.md"), "wb").write(b"\xff\xfe\x00garbage")
    open(os.path.join(fold, "huge.md"), "w").write("big " * 100_000)
    open(os.path.join(fold, "other.md"), "w").write("---\nid: 99999\n---\nCopied from another memory.")
    r = sync.run()
    check("a file naming a note that was never here becomes a new note", r["new_notes"] == 1 and
          st.recall("Copied from another memory")[0]["source"] == "markdown", r)
    check("unreadable and huge files are left alone", os.path.exists(os.path.join(fold, "bin.md")) and
          os.path.exists(os.path.join(fold, "huge.md")) and not st.recall("big big big"))
    check("a second run is quiet", sync.run()["new_notes"] == 0)


def corrupt_state():
    d = tempfile.mkdtemp()
    m = Mind(d, None)
    m.store.kv_set("conversations", {"convs": "x", "people": 5})
    check("unreadable saved conversations start fresh", Mind(d, None)._conversations() == {})
    m.store.kv_set("model_compare", [{"ts": "x"}, 5])
    c = Compare(m, [], 7)
    c.mind.models = types.SimpleNamespace(models={"a": 1})
    check("an unreadable comparison history is ignored", c.history() == [] and c.table()["runs"] == 0)
    m.store.kv_set("saved_views", ["notadict"])
    check("unreadable saved views are ignored", Views(m.store).all() == {})


def telegram_words():
    for t, want in [("note:", None), ("remember", None), ("remember: :", None), ("note: buy milk", "buy milk"),
                    ("remember that my code is 1", "my code is 1")]:
        m = TELL.match(t)
        check(f"telegram {t!r} -> {want!r}", (m.group(1) if m else None) == want)


def profile_cycle():
    st = new()
    for t in ["The Halvor drill is in the blue toolbox.", "The blue toolbox is in the garage.",
              "The garage is next to the blue toolbox."]:
        st.remember(text=t)
    p = profiles.lines(st, "Tell me everything about the blue toolbox", time.time())
    check("a place that holds the thing isn't listed as kept in it", "Kept there: halvor drill." in p and
          "Kept there: garage" not in p, p)


odd_inputs()
threads()
old_database()
asyncio.run(models_failing())
asyncio.run(overnight_replies())
documents()
sharing()
markdown_edge()
corrupt_state()
telegram_words()
profile_cycle()
print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
