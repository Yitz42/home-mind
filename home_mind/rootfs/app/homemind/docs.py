"""Documents and manuals (#50): appliance manuals, warranties, recipes, any PDF, text, Markdown or HTML file.

Each document is split into sections (a PDF page each; text by headings, about 1,500 characters at most) and kept
in its own tables with its own search index, apart from the notes, so a 200-page manual never crowds out what you
told Home Mind. For a question, the best two sections are added to what the model reads, after the notes, labelled
with the document's name and page, and the model is asked to say which page it used.

Add documents by dropping files into the docs folder (option docs_folder, default /share/home-mind/docs; checked every
hour), or POST /api/docs (a file upload, or JSON with name and text). GET /api/docs lists them; DELETE removes one.
"""
import hashlib
import html
import io
import os
import re
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (id INTEGER PRIMARY KEY, name TEXT, path TEXT, sha TEXT UNIQUE, pages INTEGER,
                                 added REAL);
CREATE TABLE IF NOT EXISTS doc_sections (id INTEGER PRIMARY KEY, doc INTEGER, page INTEGER, heading TEXT, text TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(heading, text, content='doc_sections', content_rowid='id',
                                                      tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS doc_ai AFTER INSERT ON doc_sections BEGIN
  INSERT INTO doc_fts(rowid, heading, text) VALUES (new.id, new.heading, new.text);
END;
CREATE TRIGGER IF NOT EXISTS doc_ad AFTER DELETE ON doc_sections BEGIN
  INSERT INTO doc_fts(doc_fts, rowid, heading, text) VALUES('delete', old.id, old.heading, old.text);
END;
"""
EXTS = (".pdf", ".txt", ".md", ".markdown", ".html", ".htm")
MAX_SECTION = 1500
MIN_SCORE = 2.0          # bm25 (negated): weaker matches than this aren't shown


def decode(data):
    """Text from a file's bytes: UTF-8 (a BOM dropped), UTF-16 with a BOM (Windows Notepad), else Windows-1252 /
    Latin-1, as older manuals and recipes are saved; never the replacement character for every accented letter."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _stem(w):
    """A rough stem, to tell whether a section has a question's word in some form (FTS matches 'filters' to 'filter'
    and 'cleaning' to 'clean'; the check after it must too)."""
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w


def pages_of(name, data):
    """[(page, heading, text)] from a file's bytes."""
    low = name.lower()
    if low.endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except ImportError as e:  # pragma: no cover - pypdf is in the image
            raise RuntimeError("reading PDFs needs pypdf") from e
        rd = PdfReader(io.BytesIO(data))
        if rd.is_encrypted:
            try:
                ok = rd.decrypt("")                 # many manuals are "encrypted" only against editing
            except Exception:  # noqa: BLE001
                ok = 0
            if not ok:
                raise ValueError(f"{name}: the PDF is password-protected (save an unlocked copy)")
        out = []
        for i, pg in enumerate(rd.pages, 1):
            try:
                t = pg.extract_text() or ""
            except Exception:  # noqa: BLE001 - one broken page doesn't lose the rest (page numbers stay right)
                continue
            t = re.sub(r"[ \t]+", " ", t).strip()
            if t:
                first = next((x.strip() for x in t.splitlines() if len(x.strip()) > 3), "")[:80]
                out.append((i, first, t))
        return out
    text = decode(data)
    if low.endswith((".html", ".htm")):
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<(h[1-4])[^>]*>", "\n# ", text)
        text = re.sub(r"(?i)</h[1-4]\s*>|<br\s*/?>|</p\s*>|</li\s*>", "\n", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    sections, head, buf = [], "", []

    def flush():
        body = re.sub(r"\s+", " ", " ".join(buf)).strip()
        while body:
            cut = body[:MAX_SECTION]
            if len(body) > MAX_SECTION and ". " in cut[MAX_SECTION // 2:]:
                cut = cut[:cut.rindex(". ") + 1]
            sections.append((len(sections) + 1, head, cut.strip()))
            body = body[len(cut):].strip()
    for line in text.splitlines():
        m = re.match(r"\s*#{1,4}\s+(.+)", line)
        if m:
            flush()
            head, buf = m.group(1).strip()[:80], []
        else:
            buf.append(line)
    flush()
    return sections


class Docs:
    def __init__(self, store, folder=None):
        self.store, self.folder = store, folder
        with store.lock:
            store.db.executescript(SCHEMA)
            store.db.commit()

    def add(self, name, data, path=None):
        name = os.path.basename(str(name or "").replace("\\", "/")).strip() or "document"   # never a path (bug-hunt set)
        if not data:
            raise ValueError(f"{name}: empty file")
        sha = hashlib.sha256(data).hexdigest()
        have = self.store.db.execute("SELECT id, path FROM docs WHERE sha=?", (sha,)).fetchone()
        if have:
            if path and have[1] and have[1] != path and not os.path.exists(have[1]):
                with self.store.lock:           # the file was renamed or moved (or a copy is all that is left)
                    self.store.db.execute("UPDATE docs SET name=?, path=? WHERE id=?", (name, path, have[0]))
                    self.store.db.commit()
            return {"id": have[0], "name": name, "already": True}
        try:
            secs = pages_of(name, data)
        except ValueError:
            raise
        except Exception as e:  # noqa: BLE001 - a broken PDF is the file's problem, not a crash
            raise ValueError(f"{name}: can't read it ({type(e).__name__})") from None
        if not secs:
            raise ValueError(f"{name}: no text found" + (" (a scanned PDF needs OCR first)" if name.lower().endswith(".pdf")
                                                         else ""))
        with self.store.lock:
            cur = self.store.db.execute("INSERT INTO docs(name, path, sha, pages, added) VALUES(?,?,?,?,?)",
                                        (name, path, sha, len(secs), time.time()))
            did = cur.lastrowid
            self.store.db.executemany("INSERT INTO doc_sections(doc, page, heading, text) VALUES(?,?,?,?)",
                                      [(did, p, h, t) for p, h, t in secs])
            self.store.db.commit()
        self.store.log("doc", None, name=name, sections=len(secs))
        return {"id": did, "name": name, "sections": len(secs)}

    def remove(self, did):
        with self.store.lock:
            self.store.db.execute("DELETE FROM doc_sections WHERE doc=?", (int(did),))
            n = self.store.db.execute("DELETE FROM docs WHERE id=?", (int(did),)).rowcount
            self.store.db.commit()
        return bool(n)

    def list(self):
        return [dict(r) for r in self.store.db.execute("SELECT id, name, pages, added, path FROM docs ORDER BY name")]

    def scan(self):
        """Add new files from the docs folder; drop documents whose file is gone."""
        if not self.folder or not os.path.isdir(self.folder):
            return {"added": 0}
        added, changed, seen = 0, 0, set()
        top = os.path.join(self.folder, "")
        for root, _, files in os.walk(self.folder):
            for f in files:
                if not f.lower().endswith(EXTS):
                    continue
                p = os.path.join(root, f)
                seen.add(p)
                old = self.store.db.execute("SELECT id, sha, added FROM docs WHERE path=?", (p,)).fetchone()
                try:
                    if old and os.path.getmtime(p) <= old[2]:
                        continue
                    with open(p, "rb") as fh:
                        data = fh.read()
                    if old and hashlib.sha256(data).hexdigest() == old[1]:
                        continue
                    r = self.add(f, data, p)
                    if old:                     # the file was edited: its new text replaces the old
                        self.remove(old[0])
                        changed += 1
                    else:
                        added += not r.get("already")
                except Exception:  # noqa: BLE001 - one bad file doesn't stop the rest
                    continue
        gone = [r[0] for r in self.store.db.execute("SELECT id, path FROM docs WHERE path IS NOT NULL")
                if r[1].startswith(top) and r[1] not in seen]
        for g in gone:
            self.remove(g)
        return {"added": added, "removed": len(gone), "changed": changed}

    def search(self, question, k=2):
        from .store import STOP, words
        ws = [w for w in words(question) if w not in STOP and len(w) > 2]
        if not ws:
            return []
        q = " OR ".join(f'"{w}"' for w in ws[:12])
        try:
            rows = self.store.db.execute(
                "SELECT s.id, s.page, s.heading, s.text, d.name, -bm25(doc_fts, 2.0, 1.0) AS score FROM doc_fts "
                "JOIN doc_sections s ON s.id = doc_fts.rowid JOIN docs d ON d.id = s.doc WHERE doc_fts MATCH ? "
                "ORDER BY bm25(doc_fts, 2.0, 1.0) LIMIT ?", (q, k * 3)).fetchall()
        except Exception:  # noqa: BLE001 - an odd question for FTS: no documents
            return []
        # bm25 scores words by how rare they are among all sections: with only a few short documents every word is
        # "common" and scores near 0, so the floor applies once there are enough sections for scores to mean something
        n = self.store.db.execute("SELECT COUNT(*) FROM doc_sections").fetchone()[0]
        floor = MIN_SCORE if n >= 20 else 0.0
        out = []
        for r in rows:
            body = (r["text"] + " " + (r["heading"] or "")).lower()
            hit = sum(1 for w in set(ws) if _stem(w) in body)
            if r["score"] >= floor and hit >= min(2, len(set(ws))):
                out.append(dict(r))
        return out[:k]

    def lines(self, question):
        hs = self.search(question)
        if not hs:
            return ""
        return ("FROM YOUR DOCUMENTS (say which document and page you used):\n" +
                "\n".join(f"- {h['name']}, {'page' if h['name'].lower().endswith('.pdf') else 'section'} {h['page']}: "
                          f"{h['text'][:700]}" for h in hs))
