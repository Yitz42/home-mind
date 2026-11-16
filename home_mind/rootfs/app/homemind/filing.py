"""Learn where notes go from where you put them (#58, like Paperless-ngx's auto matching), with no model.

A small naive Bayes model over the words of notes already on a shelf (not the inbox, not machine-made notes). A new
note that would land in the inbox is filed on the shelf it most likely belongs to when the model is sure (at least
CONFIDENT, and that shelf has at least MIN_NOTES notes); otherwise it stays in the inbox with that shelf suggested,
for the inbox review (#59). Moving a note to another shelf teaches it: the next model is built from the shelves as
they are. Rebuilt at most every few minutes.
"""
import json
import math
import time
from collections import Counter, defaultdict

CONFIDENT = 0.85
MIN_NOTES = 5
SKIP = ("home", "automations", "calendar", "habits", "tidy", "evening", "morning", "patterns", "journal", "scale",
        "comfort", "test-extract", "lme")


def trains(meta):
    """Whether a note on a shelf teaches filing: a person put it there (or moved or kept it). A note the model filed
    itself and no one has touched since is the model's own guess: learning from it would make filing ever surer of its
    own mistakes (a shelf grows with what it pulled in and pulls in more)."""
    m = meta if isinstance(meta, dict) else {}
    if isinstance(meta, str):
        try:
            m = json.loads(meta or "{}")
        except ValueError:
            m = {}
    return not (m.get("filed_by") in ("learned", "reshelved") and m.get("moved_from") is None
                and not m.get("keep_shelf"))


class Filer:
    def __init__(self, store, rebuild_seconds=300):
        self.store, self.rebuild_seconds = store, rebuild_seconds
        self._model, self._at, self._n = None, 0, -1

    def _words(self, text):
        from .store import STOP, words, singular
        return [singular(w) for w in words(text) if w not in STOP and len(w) > 2 and not w.isdigit()]

    def build(self):
        rows = self.store.db.execute(
            f"SELECT shelf, title, body, meta FROM items WHERE status IN ('active','faded') AND shelf != 'inbox' AND "
            f"kind NOT IN ('procedure') AND shelf NOT LIKE 'home/%' AND shelf NOT LIKE 'comfort/%' AND "
            f"shelf NOT LIKE 'summaries%' AND shelf NOT LIKE 'procedures/%' AND source NOT LIKE 'pack:%' AND "
            f"source NOT IN ({','.join('?' * len(SKIP))})", SKIP).fetchall()
        docs, words = Counter(), defaultdict(Counter)
        rows = [r for r in rows if trains(r["meta"])]
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

    def guess(self, text, without=None):
        """(shelf, probability) or (None, 0). without: the shelf the note is on now, to leave the note itself out of
        what the model learned (reshelving, #32)."""
        m = self.model()
        if not m["docs"]:
            return None, 0.0
        ws = set(self._words(text))
        if not ws:
            return None, 0.0
        logp = {}
        total = m["total"] - (1 if without in m["docs"] else 0)
        for shelf, n in m["docs"].items():
            own = shelf == without
            n = n - 1 if own else n
            if n <= 0:
                continue
            lp = math.log(n / total)
            wc = m["words"][shelf]
            for w in ws:
                lp += math.log((wc[w] - (1 if own else 0) + 0.5) / (n + 1.0))   # in how many of this shelf's notes
            logp[shelf] = lp
        if not logp:
            return None, 0.0
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


RESHELVE_P = 0.95
RESHELVE_DAYS = 7


def reshelve(filer, now=None, apply=True, limit=500):
    """Reshelve over time (#32). As filing learns, a note that was filed by the model (or left in the inbox) a while
    ago may now clearly belong elsewhere: it is moved (and says where it came from). A note you placed yourself is
    never moved; if it clearly looks like another shelf's, that shelf is suggested instead. A note you moved, or kept
    where it is, is left alone."""
    st = filer.store
    now = now or time.time()
    m = filer.model()
    rows = st.db.execute(
        f"SELECT id, shelf, title, body, meta, source FROM items WHERE status='active' AND kind NOT IN ('procedure','task') "
        f"AND created < ? AND shelf NOT LIKE 'home/%' AND shelf NOT LIKE 'comfort/%' AND shelf NOT LIKE 'summaries%' "
        f"AND shelf NOT LIKE 'procedures/%' AND source NOT LIKE 'pack:%' AND source NOT IN ({','.join('?' * len(SKIP))}) "
        f"ORDER BY id DESC LIMIT ?", (now - RESHELVE_DAYS * 86400, *SKIP, limit)).fetchall()
    moved, suggested = [], []
    for r in rows:
        meta = json.loads(r["meta"] or "{}")
        if meta.get("moved_from") is not None or meta.get("keep_shelf") or meta.get("sensitive"):
            continue
        # leave the note itself out of what the model learned, if it taught it at all
        best, p = filer.guess(f"{r['title']} {r['body']}", without=r["shelf"] if trains(meta) else None)
        if not best or best == r["shelf"] or p < RESHELVE_P or m["docs"].get(best, 0) < MIN_NOTES + 1:
            continue
        machine = r["shelf"] == "inbox" or meta.get("filed_by") in ("learned", "reshelved")
        if machine:
            moved.append({"id": r["id"], "from": r["shelf"], "to": best, "p": round(p, 2)})
            if apply:
                meta.update(filed_by="reshelved", reshelved_from=r["shelf"], filed_p=round(p, 2))
                meta.pop("suggested_shelf", None)
                meta.pop("suggested_p", None)
                with st.lock:
                    st.db.execute("UPDATE items SET shelf=?, meta=? WHERE id=?", (best, json.dumps(meta), r["id"]))
                    st.db.commit()
        elif meta.get("suggested_shelf") != best:
            suggested.append({"id": r["id"], "from": r["shelf"], "to": best, "p": round(p, 2)})
            if apply:
                meta.update(suggested_shelf=best, suggested_p=round(p, 2))
                with st.lock:
                    st.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(meta), r["id"]))
                    st.db.commit()
    if apply and moved:
        st.log("reshelve", None, moved=moved[:50])
        filer._model = None
    return {"moved": moved, "suggested": suggested}
