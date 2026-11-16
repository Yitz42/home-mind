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


def pages_of(name, data):
    """[(page, heading, text)] from a file's bytes."""
    low = name.lower()
    if low.endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except ImportError as e:  # pragma: no cover - pypdf is in the image
            raise RuntimeError("reading PDFs needs pypdf") from e
        out = []
        for i, pg in enumerate(PdfReader(io.BytesIO(data)).pages, 1):
            t = re.sub(r"[ \t]+", " ", pg.extract_text() or "").strip()
            if t:
                first = next((x.strip() for x in t.splitlines() if len(x.strip()) > 3), "")[:80]
                out.append((i, first, t))
        return out
    text = data.decode("utf-8", "replace")
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
        sha = hashlib.sha256(data).hexdigest()
        have = self.store.db.execute("SELECT id FROM docs WHERE sha=?", (sha,)).fetchone()
        if have:
            return {"id": have[0], "name": name, "already": True}
        secs = pages_of(name, data)
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
        added, seen = 0, set()
        for root, _, files in os.walk(self.folder):
            for f in files:
                if not f.lower().endswith(EXTS):
                    continue
                p = os.path.join(root, f)
                seen.add(p)
                if self.store.db.execute("SELECT 1 FROM docs WHERE path=?", (p,)).fetchone():
                    continue
                try:
                    with open(p, "rb") as fh:
                        r = self.add(f, fh.read(), p)
                    added += not r.get("already")
                except Exception:  # noqa: BLE001 - one bad file doesn't stop the rest
                    continue
        gone = [r[0] for r in self.store.db.execute("SELECT id, path FROM docs WHERE path IS NOT NULL")
                if r[1].startswith(self.folder) and r[1] not in seen]
        for g in gone:
            self.remove(g)
        return {"added": added, "removed": len(gone)}

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
        out = []
        for r in rows:
            hit = sum(1 for w in set(ws) if w in (r["text"] + " " + (r["heading"] or "")).lower())
            if r["score"] >= MIN_SCORE and hit >= min(2, len(set(ws))):
                out.append(dict(r))
        return out[:k]

    def lines(self, question):
        hs = self.search(question)
        if not hs:
            return ""
        return ("FROM YOUR DOCUMENTS (say which document and page you used):\n" +
                "\n".join(f"- {h['name']}, {'page' if h['name'].lower().endswith('.pdf') else 'section'} {h['page']}: "
                          f"{h['text'][:700]}" for h in hs))
