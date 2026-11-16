"""Expert review findings (documents, shelves, places, home apps): each check reproduces a bug found in review and
fixed. No network needed.

  python tests/test_review_docs.py
"""
import asyncio
import io
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind import profiles, structure as S  # noqa: E402
from homemind.apps import Apps  # noqa: E402
from homemind.docs import Docs, pages_of  # noqa: E402
from homemind.store import Store, clean_shelf  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def pdf(pages):
    """A minimal real PDF with one line of text per page (as in test_files.py)."""
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


def new():
    return Store(os.path.join(tempfile.mkdtemp(), "m.db"))


def documents():
    # a household with one short document: bm25 scores every word ~0, so MIN_SCORE hid it for ever
    dc = Docs(new())
    dc.add("boiler.txt", b"Boiler pressure should be 1.5 bar when cold. Top up with the filling loop.")
    h = dc.search("What should the boiler pressure be?")
    check("a lone short document can be found", h and "1.5 bar" in h[0]["text"], h)
    # the word check after FTS must match word forms FTS matched (porter): 'filters', 'cleaning'
    dc.add("dish.md", b"# Cleaning\nClean the filter monthly under warm water.\n# Salt\nRefill the salt.")
    h = dc.search("How do I clean the filters?")
    check("'filters' finds a section about the filter", h and h[0]["heading"] == "Cleaning", h)
    check("an unrelated question still gets nothing", dc.search("When is Mia's birthday party?") == [])
    # encodings
    check("a UTF-8 BOM doesn't hide the first heading",
          pages_of("w.md", "﻿# Warranty\nTwo years.".encode("utf-8"))[0][1] == "Warranty")
    check("Windows-1252 text keeps its accents", "Café crème" in pages_of("r.txt", "Café crème".encode("cp1252"))[0][2])
    check("UTF-16 text (Notepad) is read", "Hello filter" in pages_of("n.txt", "Hello filter".encode("utf-16"))[0][2])
    # PDFs: encrypted with no user password reads; with a password a clear message
    from pypdf import PdfReader, PdfWriter
    src = pdf(["Error E24 means the drain hose is kinked.", "Warranty two years."])
    for pw in ("", "secret"):
        w = PdfWriter(clone_from=PdfReader(io.BytesIO(src)))
        w.encrypt(user_password=pw, owner_password="own", algorithm="AES-128")
        b = io.BytesIO()
        w.write(b)
        try:
            r = Docs(new()).add(f"m{pw}.pdf", b.getvalue())
            check(f"encrypted PDF (user password {pw!r})", pw == "" and r["sections"] == 2, r)
        except ValueError as e:
            check(f"encrypted PDF (user password {pw!r})", pw and "password" in str(e), e)


def folder_scan():
    fold = tempfile.mkdtemp()
    dc = Docs(new(), fold)
    a = os.path.join(fold, "a.txt")
    with open(a, "w") as f:
        f.write("Boiler pressure should be 1.5 bar when cold.")
    dc.scan()
    os.rename(a, os.path.join(fold, "b.txt"))
    r = dc.scan()
    check("a renamed file is kept, not dropped", r["removed"] == 0 and [d["name"] for d in dc.list()] == ["b.txt"],
          (r, dc.list()))
    check("... and not added again next time", dc.scan()["added"] == 0 and len(dc.list()) == 1)
    time.sleep(0.02)
    with open(os.path.join(fold, "b.txt"), "w") as f:
        f.write("Boiler pressure should be 1.2 bar when cold.")
    r = dc.scan()
    h = dc.search("boiler pressure cold")
    check("an edited file is read again", r["changed"] == 1 and h and "1.2 bar" in h[0]["text"] and len(dc.list()) == 1,
          (r, h))
    # a sibling folder whose name starts like the docs folder isn't the docs folder
    other = fold + "2"
    os.makedirs(other)
    p = os.path.join(other, "x.txt")
    with open(p, "w") as f:
        f.write("The heat pump defrosts every hour.")
    dc.add("x.txt", open(p, "rb").read(), p)
    check("a document outside the folder isn't dropped by the scan", dc.scan()["removed"] == 0 and
          any(d["path"] == p for d in dc.list()))


def places():
    check("'next to' is not 'in'", S.place("The ladder is next to the shed.") == {"thing": "ladder", "in": "shed",
                                                                                  "rel": "next to"})
    check("'in' unchanged", S.place("The drill is in the red toolbox.") == {"thing": "drill", "in": "red toolbox"})
    st = new()
    for t in ["The ladder is next to the shed.", "The shed is in the garden.", "The rake is in the shed.",
              "The cat bed is under the stairs."]:
        st.remember(text=t)
    now = time.time()
    inside = " ".join(S.place_lines(st, "What's in the shed?", [], now))
    check("what's in the shed: the rake, not the ladder beside it", "rake" in inside and "ladder" not in inside, inside)
    where = " ".join(S.place_lines(st, "Where is the ladder?", [], now))
    check("where is the ladder: next to the shed, which is in the garden",
          "is next to the shed" in where and "which is in the garden" in where, where)
    where = " ".join(S.place_lines(st, "Where is the cat bed?", [], now))
    check("where is the cat bed: under the stairs", "is under the stairs" in where, where)
    ov = {p["thing"]: p["chain"] for p in S.overview(st, now)["places"]}
    check("the panel says 'next to', not 'is in'", ov.get("ladder", [""])[0] == "next to shed", ov)
    pic = " ".join(profiles.picture(st, "shed", now))
    check("the shed's profile doesn't keep the ladder in it", "Kept there: rake." in pic, pic)


def shelves():
    check("accented shelf names survive", clean_shelf("Küche/Rezepte") == "küche/rezepte", clean_shelf("Küche/Rezepte"))
    check("non-Latin shelf names aren't the inbox", clean_shelf("台所") == "台所", clean_shelf("台所"))
    check("shelf paths still can't climb", clean_shelf("../etc/./x") == "etc/x", clean_shelf("../etc/./x"))


def apps():
    calls = []

    class A(Apps):
        async def _get(self, url, headers, params):
            calls.append(params)
            return {"items": [{"name": "Random thing"}]}
    a = A({"homebox_url": "http://hb", "homebox_token": "t"}, None)
    got = asyncio.run(a.lines("Where is it?"))
    check("a question with no search words doesn't list arbitrary Homebox items", got == "" and not calls, (got, calls))
    got = asyncio.run(a.lines("Where is the spare drill bit?"))
    check("... a real question still asks Homebox", "Random thing" in got and calls)


documents()
folder_scan()
places()
shelves()
apps()
print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
