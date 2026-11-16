"""Files and filing (0.26): documents and manuals (#50), learning where notes go (#58), the inbox review (#59),
Homebox and Paperless at question time (#62), and pronoun links checked again (#13). No network needed.

  python tests/test_files.py
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
from homemind import docs as D, apps as AP  # noqa: E402
from homemind.filing import Filer  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.mind import Mind  # noqa: E402
from homemind.overnight import Overnight, ConvLog  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def pdf(pages):
    """A minimal real PDF with one line of text per page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for t in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({t}) Tj ET"
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {len(objs)} 0 R >>")
        kids.append(len(objs))
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offs).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    return out


def fresh():
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    for t in ["The drill is in the red toolbox.", "Mia waters the ferns on Sunday.", "The boiler was serviced in May."]:
        st.remember(text=t)
    return st


async def documents():
    st = fresh()
    dc = D.Docs(st)
    r = dc.add("Dishwasher manual.pdf", pdf(["Installing the dishwasher: level the feet first.",
                                             "Error E24 means the drain hose is kinked or the filter is blocked.",
                                             "Warranty: two years from the date of purchase."]))
    check("a PDF is read page by page", r["sections"] == 3)
    check("the same file twice is kept once", dc.add("copy.pdf", pdf(["Installing the dishwasher: level the feet first.",
                                                                     "Error E24 means the drain hose is kinked or the filter is blocked.",
                                                                     "Warranty: two years from the date of purchase."])).get("already"))
    hit = dc.search("What does error E24 mean on the dishwasher?")
    check("search finds the right page", hit and hit[0]["page"] == 2, hit)
    lines = dc.lines("What does error E24 mean on the dishwasher?")
    check("the model is told the document and page", "Dishwasher manual.pdf, page 2" in lines and "say which document" in lines)
    check("an unrelated question gets no documents", dc.lines("When is Mia's birthday?") == "")
    md = "# Pizza dough\nFlour, water, salt and yeast. Rest it overnight.\n# Tomato sauce\nSimmer the tomatoes with garlic."
    dc.add("recipes.md", md.encode())
    h = dc.search("How long does the pizza dough rest?")
    check("Markdown is split by headings", h and h[0]["heading"] == "Pizza dough", h)
    dc.add("tips.html", b"<html><script>x()</script><h2>Grout</h2><p>Use a stiff brush &amp; vinegar.</p></html>")
    h = dc.search("How do I clean the grout with vinegar?")
    check("HTML is read as text", h and "stiff brush & vinegar" in h[0]["text"], h)
    long = ("This sentence is about the furnace filter and it repeats. " * 80).encode()
    dc.add("long.txt", long)
    check("long text is cut into sections", st.db.execute("SELECT COUNT(*) FROM doc_sections s JOIN docs d ON d.id=s.doc "
                                                         "WHERE d.name='long.txt'").fetchone()[0] >= 3)
    try:
        dc.add("empty.txt", b"   ")
        check("a file with no text is refused", False)
    except ValueError:
        check("a file with no text is refused", True)
    m = Mind(tempfile.mkdtemp(), None)
    m.store, m.docs = st, dc
    _, _, ctx = await m.context_for(st, "What does error E24 mean on the dishwasher?")
    check("documents come after the notes on the page", "FROM YOUR DOCUMENTS" in ctx and
          ctx.index("FROM YOUR DOCUMENTS") > 0)
    check("documents never become notes", st.db.execute("SELECT COUNT(*) FROM items WHERE body LIKE '%E24%'").fetchone()[0] == 0)
    folder = tempfile.mkdtemp()
    open(os.path.join(folder, "router.txt"), "w").write("Router reset: hold the button for 10 seconds.")
    dc2 = D.Docs(fresh(), folder)
    check("the docs folder is read", dc2.scan()["added"] == 1 and dc2.scan()["added"] == 0)
    os.remove(os.path.join(folder, "router.txt"))
    check("a file taken out of the folder is dropped", dc2.scan()["removed"] == 1 and not dc2.list())


def filing():
    st = fresh()
    for k in range(8):
        st.remember(text=f"Bake the bread at 220 with flour and yeast, batch {k}.", shelf="kitchen/recipes")
        st.remember(text=f"The car tire pressure and oil were checked, visit {k}.", shelf="car")
    st.filer = Filer(st, rebuild_seconds=0)
    a = st.remember(text="Use rye flour for the next bread bake.")
    check("a note like the recipes is filed there", st.get(a["id"])["shelf"] == "kitchen/recipes" and
          st.get(a["id"])["meta"].get("filed_by") == "learned")
    b = st.remember(text="Call back about it later.")
    check("a vague note stays in the inbox", st.get(b["id"])["shelf"] == "inbox")
    c = st.remember(text="Tomato seeds go in the bed by the fence.", shelf="garden")
    check("a note given a shelf keeps it", st.get(c["id"])["shelf"] == "garden")
    d = st.remember(text="New brake pads.", source="home")
    check("machine notes aren't filed by it", st.get(d["id"])["shelf"] == "inbox")
    for k in range(6):
        st.remember(text=f"Seeds, soil and compost for bed {k}.", shelf="garden")
    st.filer._model = None
    e = st.remember(text="More compost for the soil in the seed bed.")
    check("moving notes onto a shelf teaches it", st.get(e["id"])["shelf"] == "garden")
    st.filer = None


async def apps():
    calls = []

    class R:
        def __init__(self, data, status=200):
            self.data, self.status = data, status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return self.data

    class S:
        def get(self, url, headers=None, params=None, timeout=None):
            calls.append((url, headers, params))
            if "homebox" in url:
                return R({"items": [{"name": "Extension cord", "quantity": 2, "location": {"name": "Garage shelf"}}]})
            if "paper" in url:
                return R({"results": [{"id": 41, "title": "Dishwasher receipt", "created_date": "2025-03-02"}]})
            return R({}, 500)
    a = AP.Apps({"homebox_url": "http://homebox:7745", "homebox_token": "tok", "paperless_url": "http://paper:8000",
                 "paperless_token": "pt"}, lambda: S())
    lines = await a.lines("Where is the extension cord?")
    check("Homebox is asked about things", "FROM HOMEBOX" in lines and "Extension cord (x2), in Garage shelf" in lines)
    check("... with its token", calls[-1][1]["Authorization"] == "Bearer tok" and calls[-1][2]["q"] == "extension cord")
    lines = await a.lines("Do we still have the dishwasher receipt?")
    check("Paperless is asked about papers", "Dishwasher receipt, dated 2025-03-02 (Paperless document 41)" in lines)
    n = len(calls)
    check("an unrelated question asks neither", await a.lines("What's Dana's favourite colour?") == "" and len(calls) == n)

    class Bad:
        def get(self, *a, **k):
            raise RuntimeError("down")
    b = AP.Apps({"homebox_url": "http://x", "homebox_token": "t"}, lambda: Bad())
    check("an app that's down doesn't break the answer", await b.lines("Where is the drill?") == "")
    check("nothing set up, nothing asked", AP.Apps({}, lambda: S()).configured == [])


async def pronouns():
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    shed = st.remember(text="The Nimsk shed code is 4417.", kind="fact")
    st.remember(text="The Bexvok bike is red.")
    its = st.remember(text="Its code changed to 9002.", kind="fact")
    check("'its code' links to the recent note with a code, not the last note",
          st.get(its["id"])["meta"].get("refers_to") == shed["id"])
    st.remember(text="The Orvil gate code is 1111.", kind="fact")
    lamp = st.remember(text="The Tamsin lamp is in the hall.")
    it2 = st.remember(text="It's in the attic now.")
    check("with no attribute, the last note", st.get(it2["id"])["meta"].get("refers_to") == lamp["id"])

    class Model:
        def __init__(self, ans):
            self.ans = ans

        async def chat(self, system, messages, **k):
            return {"text": self.ans}

    class Models:
        models = {"big": None}

        def get(self, n):
            return Model("2")

        def list(self):
            return []
    o = Overnight(st, Models(), ConvLog(st), model="big")
    moved = await o.check_links(Model("2"))
    m = st.get(it2["id"])["meta"]
    check("the overnight check moves a link the model disagrees with", moved >= 1 and m["refers_by"] == "model" and
          m.get("refers_was") == lamp["id"], m)
    check("... and the title follows", "[re: The Orvil gate" in st.get(it2["id"])["title"] or
          "[re: The Tamsin" not in st.get(it2["id"])["title"], st.get(it2["id"])["title"])
    check("a checked link isn't asked again", await o.check_links(Model("1")) == 0)


asyncio.run(documents())
filing()
asyncio.run(apps())
asyncio.run(pronouns())
print(f"\n{len(FAILS)} failed" if FAILS else "\nALL FILES AND FILING CHECKS PASSED")
sys.exit(1 if FAILS else 0)
