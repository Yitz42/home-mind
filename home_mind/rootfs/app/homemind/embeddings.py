"""Search by meaning alongside keywords (#10): "planter" finds "flower box". A small embedding model (option
embed_model: one of your models, type ollama or openai, with an embedding model such as nomic-embed-text or
all-minilm) turns each note into a vector; a question's nearest notes are fused with the keyword results by rank,
like the model word-expansion already is. Without embed_model nothing changes.

Vectors are kept in table vectors (float32, normalised). New and changed notes are embedded in the background, a
batch at a time. numpy is used when it's there; otherwise plain Python (slower, fine for a few thousand notes).
"""
import array
import logging
import math
import time

log = logging.getLogger("homemind.embeddings")

try:
    import numpy as np
except ImportError:  # pragma: no cover - the add-on image has numpy
    np = None

SKIP_SOURCES = ("home", "automations", "calendar", "habits", "tidy", "evening")
MIN_SIM = 0.35              # nearer than this isn't "about the same thing"


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Embeddings:
    def __init__(self, store, model, is_up=None):
        self.store, self.model = store, model
        # is_up() is False while the embedding model's computer is known to be asleep: questions use keywords alone
        # at once (no connect timeout), and indexing waits until it's back
        self.is_up = is_up or (lambda: True)
        self._cache, self._mat, self._ids, self._stamp = {}, None, [], None
        with store.lock:
            store.db.execute("CREATE TABLE IF NOT EXISTS vectors (item_id INTEGER PRIMARY KEY, model TEXT, dim INTEGER,"
                             " vec BLOB, updated REAL)")
            store.db.commit()

    @property
    def name(self):
        return getattr(self.model, "name", "") or ""

    @staticmethod
    def text_of(r):
        return f"{r['title']}\n{(r['body'] or '')[:1200]}"

    async def index(self, batch=32):
        """Embed notes with no vector (or an older one, or another model's). Returns how many."""
        rows = self.store.db.execute(
            f"SELECT i.id, i.title, i.body FROM items i LEFT JOIN vectors v ON v.item_id = i.id "
            f"WHERE i.status IN ('active','faded') AND i.source NOT IN ({','.join('?' * len(SKIP_SOURCES))}) "
            f"AND (v.item_id IS NULL OR v.updated < i.updated OR v.model != ?) ORDER BY i.id DESC LIMIT ?",
            (*SKIP_SOURCES, self.name, batch)).fetchall()
        if not rows or not self.is_up():
            return 0
        vecs = await self.model.embed([self.text_of(r) for r in rows])
        now = time.time()
        with self.store.lock:
            for r, v in zip(rows, vecs):
                v = _norm(v)
                self.store.db.execute("INSERT OR REPLACE INTO vectors(item_id, model, dim, vec, updated) VALUES(?,?,?,?,?)",
                                      (r["id"], self.name, len(v), array.array("f", v).tobytes(), now))
            self.store.db.commit()
        self._stamp = None
        return len(rows)

    def _load(self):
        stamp = self.store.db.execute("SELECT COUNT(*), MAX(updated) FROM vectors WHERE model=?", (self.name,)).fetchone()
        stamp = tuple(stamp)
        if stamp == self._stamp:
            return
        ids, vecs = [], []
        for r in self.store.db.execute("SELECT item_id, vec FROM vectors WHERE model=?", (self.name,)):
            ids.append(r[0])
            vecs.append(array.array("f", r[1]))
        self._ids = ids
        self._mat = (np.array(vecs, dtype="float32") if vecs else None) if np is not None else vecs
        self._stamp = stamp

    async def nearest(self, question, k=8):
        """[(item_id, similarity)] for the notes nearest a question, nearest first."""
        q = self._cache.get(question)
        if q is None and not self.is_up():
            return []
        if q is None:
            q = _norm((await self.model.embed([question]))[0])
            if len(self._cache) > 200:
                self._cache.clear()
            self._cache[question] = q
        self._load()
        if not self._ids:
            return []
        if np is not None:
            sims = self._mat @ np.array(q, dtype="float32")
            top = np.argsort(-sims)[:k * 3]
            pairs = [(self._ids[i], float(sims[i])) for i in top]
        else:
            sims = [sum(a * b for a, b in zip(v, q)) for v in self._mat]
            pairs = sorted(zip(self._ids, sims), key=lambda x: -x[1])[:k * 3]
        return [(i, s) for i, s in pairs if s >= MIN_SIM]

    def forget(self, ids):
        with self.store.lock:
            self.store.db.executemany("DELETE FROM vectors WHERE item_id=?", [(int(i),) for i in ids])
            self.store.db.commit()
        self._stamp = None


def fuse(keyword_hits, vector_hits, k, rank_k=20):
    """Rank fusion: each note scores 1/(rank+rank_k) in each list; keyword order breaks ties."""
    score, byid = {}, {}
    for lst in (keyword_hits, vector_hits):
        for rank, h in enumerate(lst):
            score[h["id"]] = score.get(h["id"], 0) + 1 / (rank + rank_k)
            byid.setdefault(h["id"], h)
    order = sorted(score, key=lambda i: -score[i])
    return [byid[i] for i in order[:k]]
