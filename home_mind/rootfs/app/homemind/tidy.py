"""Overnight tidying, flag-only (#14): pairs of notes that say the same thing, for you to keep one of. Nothing is merged
or forgotten automatically: a wrong merge would lose a fact silently.

Two notes are duplicates when they are the same kind, both current, and either say the same words (case, articles and
punctuation aside), or share almost all their words (DUP_SHARE) with exactly the same numbers and names, so "the code
is 4417" and "the code is 4471" are never paired.
"""
import re

from .store import names_in, words

DUP_SHARE = 0.85
SKIP = ("home", "calendar", "automations", "journal", "scale", "morning", "patterns", "check-in", "nutrition", "pantry")


def _norm(s):
    return " ".join(w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in ("the", "a", "an", "my", "our"))


def duplicates(store, limit=20):
    rows = store.db.execute(
        f"SELECT id, kind, title, body, source FROM items WHERE status='active' AND kind != 'procedure' AND source NOT IN "
        f"({','.join('?' * len(SKIP))}) AND source NOT LIKE 'pack:%' ORDER BY id", SKIP).fetchall()
    seen, pairs = {}, []
    info = []
    for r in rows:
        text = r["body"] or r["title"]
        n = _norm(text)
        if n in seen:
            pairs.append((seen[n], r["id"]))
            continue
        seen[n] = r["id"]
        info.append((r["id"], r["kind"], set(words(text)), set(re.findall(r"\d+", text)), names_in(text)))
    by_word = {}
    for i, (rid, kind, ws, nums, nms) in enumerate(info):
        for w in ws:
            by_word.setdefault(w, []).append(i)
    done = {frozenset(p) for p in pairs}
    for i, (rid, kind, ws, nums, nms) in enumerate(info):
        if not ws:
            continue
        cand = {j for w in ws for j in by_word.get(w, ()) if j > i}
        for j in cand:
            rj, kj, wj, nj, mj = info[j]
            if kj != kind or nj != nums or mj != nms:
                continue
            share = len(ws & wj) / max(len(ws), len(wj))
            if share >= DUP_SHARE and frozenset((rid, rj)) not in done:
                pairs.append((rid, rj))
                done.add(frozenset((rid, rj)))
    return pairs[:limit]


def run(store):
    """Find the pairs and keep them for the morning summary and the Memory page."""
    pairs = duplicates(store)
    store.kv_set("tidy_pairs", pairs)
    store.log("tidy", None, pairs=len(pairs))
    return pairs


def summary_line(store, short=lambda d: d["title"][:40]):
    pairs = store.kv_get("tidy_pairs") or []
    live = []
    for a, b in pairs:
        da, db = store.get(a), store.get(b)
        if da and db and da["status"] == "active" and db["status"] == "active":
            live.append((da, db))
    if not live:
        return None
    shown = "; ".join(f"#{a['id']} and #{b['id']} (\"{short(a)}\")" for a, b in live[:3])
    return f"Possible duplicates {len(live)}: {shown}. Forget one in Memory if they are the same."
