"""Learn where notes go from where you put them (#58, like Paperless-ngx's auto matching), with no model.

A small naive Bayes model over the words of notes already on a shelf (not the inbox, not machine-made notes). A new
note that would land in the inbox is filed on the shelf it most likely belongs to when the model is sure (at least
CONFIDENT, and that shelf has at least MIN_NOTES notes); otherwise it stays in the inbox with that shelf suggested,
for the inbox review (#59). Moving a note to another shelf teaches it: the next model is built from the shelves as
they are. Rebuilt at most every few minutes.
"""
import math
import time
from collections import Counter, defaultdict

CONFIDENT = 0.85
MIN_NOTES = 5
SKIP = ("home", "automations", "calendar", "habits", "tidy", "evening", "morning", "patterns", "journal", "scale",
        "comfort", "test-extract", "lme")


class Filer:
    def __init__(self, store, rebuild_seconds=300):
        self.store, self.rebuild_seconds = store, rebuild_seconds
        self._model, self._at, self._n = None, 0, -1

    def _words(self, text):
        from .store import STOP, words, singular
        return [singular(w) for w in words(text) if w not in STOP and len(w) > 2 and not w.isdigit()]

    def build(self):
        rows = self.store.db.execute(
            f"SELECT shelf, title, body FROM items WHERE status IN ('active','faded') AND shelf != 'inbox' AND "
            f"kind NOT IN ('procedure') AND shelf NOT LIKE 'home/%' AND shelf NOT LIKE 'comfort/%' AND "
            f"shelf NOT LIKE 'summaries%' AND shelf NOT LIKE 'procedures/%' AND source NOT LIKE 'pack:%' AND "
            f"source NOT IN ({','.join('?' * len(SKIP))})", SKIP).fetchall()
        docs, words = Counter(), defaultdict(Counter)
        for r in rows:
            docs[r["shelf"]] += 1
            words[r["shelf"]].update(set(self._words(f"{r['title']} {r['body']}")))
        vocab = {w for c in words.values() for w in c}
        self._model = {"docs": docs, "words": words, "total": sum(docs.values()), "vocab": len(vocab) or 1}
        self._at, self._n = time.time(), len(rows)
        return self._model

    def model(self):
        if self._model is None or time.time() - self._at > self.rebuild_seconds:
            self.build()
        return self._model

    def guess(self, text):
        """(shelf, probability) or (None, 0)."""
        m = self.model()
        if not m["docs"]:
            return None, 0.0
        ws = set(self._words(text))
        if not ws:
            return None, 0.0
        logp = {}
        for shelf, n in m["docs"].items():
            lp = math.log(n / m["total"])
            wc = m["words"][shelf]
            for w in ws:
                lp += math.log((wc[w] + 0.5) / (n + 1.0))     # each word: in how many of this shelf's notes
            logp[shelf] = lp
        top = max(logp.values())
        z = sum(math.exp(v - top) for v in logp.values())
        best = max(logp, key=logp.get)
        return best, math.exp(logp[best] - top) / z

    def place(self, text):
        """("shelf", how) for a note that would go to the inbox: filed when sure, else ("inbox", suggestion)."""
        shelf, p = self.guess(text)
        if shelf and p >= CONFIDENT and self.model()["docs"][shelf] >= MIN_NOTES:
            return shelf, {"filed_by": "learned", "filed_p": round(p, 2)}
        if shelf and p >= 0.5:
            return "inbox", {"suggested_shelf": shelf, "suggested_p": round(p, 2)}
        return "inbox", {}
