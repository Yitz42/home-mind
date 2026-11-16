"""Overnight tidying (#14). Flag-only for facts: pairs of notes that say the same thing, and notes that contradict each
other (two people disagreeing, a guess against what was said), for you to settle. Nothing is merged or forgotten
automatically: a wrong merge would lose a fact silently. Logs of what happened (comfort and house actions, automation
runs) older than ROLL_DAYS are rolled into one note per month; the originals are archived, so they can be restored.

Two notes are duplicates when they are the same kind, both current, and either say the same words (case, articles and
punctuation aside), or share almost all their words (DUP_SHARE) with exactly the same numbers and names, so "the code
is 4417" and "the code is 4471" are never paired.
"""
import json
import re
import time
from datetime import datetime

from .store import HEDGE, SPEAKER, names_in, words

DUP_SHARE = 0.85
SKIP = ("home", "calendar", "automations", "journal", "scale", "morning", "patterns", "check-in", "nutrition", "pantry",
        "habits", "tidy", "evening")


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


def _nums(text):
    return set(re.findall(r"\d[\d:.-]*\d|\d", text or ""))


def contradictions(store, limit=20):
    """Pairs (a, b, why) of current notes that the search would show side by side as disagreeing: what two people said
    differently about one thing ("Dana says ... 4417" / "Sam said ... 9002"), and a guess whose values differ from what
    was stated. Each such note is searched for by its own title, so the pairs are exactly what answers would see."""
    rows = store.db.execute(
        f"SELECT id, title, body FROM items WHERE status='active' AND kind != 'procedure' AND source NOT IN "
        f"({','.join('?' * len(SKIP))}) AND source NOT LIKE 'pack:%' ORDER BY id DESC LIMIT 3000", SKIP).fetchall()
    out, seen = [], set()
    for r in rows:
        text = (r["title"] or "") + " " + (r["body"] or "")
        said, guess = SPEAKER.search(text), HEDGE.search(r["body"] or "")
        if not (said or guess):
            continue
        hits = store.recall(r["title"] or r["body"], limit=6, touch=False, quiet=True)
        me = next((h for h in hits if h["id"] == r["id"]), None)
        if not me:
            continue
        if me.get("disagrees"):
            other = store.get(me["disagrees"])
            pair = frozenset((r["id"], me["disagrees"]))
            if other and _nums(other["body"]) == _nums(r["body"]):
                continue            # two people saying the same thing agree
            if pair not in seen:
                seen.add(pair)
                out.append((min(pair), max(pair), "two people said different things"))
            continue
        if me.get("guess"):
            mine = _nums(r["body"])
            for h in hits:
                if h["id"] == r["id"] or h.get("guess") or not mine:
                    continue
                theirs = _nums(h["body"])
                shared = names_in(text) & names_in(h["title"] + " " + h["body"])
                if theirs and shared and theirs != mine:
                    pair = frozenset((r["id"], h["id"]))
                    if pair not in seen:
                        seen.add(pair)
                        out.append((h["id"], r["id"], "a guess differs from what was said"))
                    break
        if len(out) >= limit:
            break
    return out


ROLL_DAYS = 90
ROLL = (("comfort/actions", None, "Comfort actions"), ("home/actions", None, "House actions"),
        ("home/automations", "Ran %", "Automation runs"))


def roll_up(store, days=ROLL_DAYS, now=None):
    """One note per shelf per month for logs older than `days`; the originals are archived (meta rolled_into)."""
    now = now or store.clock()
    cut, made = now - days * 86400, []
    for shelf, title_like, label in ROLL:
        q = "SELECT id, title, body, created FROM items WHERE shelf=? AND status IN ('active','faded') AND created < ?"
        args = [shelf, cut]
        if title_like:
            q += " AND title LIKE ?"
            args.append(title_like)
        by_month = {}
        for r in store.db.execute(q + " ORDER BY created", args).fetchall():
            by_month.setdefault(datetime.fromtimestamp(r["created"]).strftime("%Y-%m"), []).append(r)
        for month, rs in by_month.items():
            if len(rs) < 2:
                continue
            name = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
            lines = [f"{datetime.fromtimestamp(r['created']):%a %b %d %H:%M}: {r['title']}" for r in rs]
            res = store.remember(text="\n".join(lines), title=f"{label}, {name} ({len(rs)})", kind="note",
                                 shelf=shelf + "/monthly", tags=["rolled up"], source="tidy",
                                 at=rs[-1]["created"], meta={"rolled_up": [r["id"] for r in rs], "month": month})
            with store.lock:
                for r in rs:
                    m = json.loads(store.db.execute("SELECT meta FROM items WHERE id=?", (r["id"],)).fetchone()[0] or "{}")
                    m["rolled_into"] = res["id"]
                    store.db.execute("UPDATE items SET status='archived', updated=?, meta=? WHERE id=?",
                                     (time.time(), json.dumps(m), r["id"]))
                store.db.commit()
            store._vocab_n = None
            made.append({"id": res["id"], "shelf": shelf, "month": month, "notes": len(rs)})
    return made


def run(store):
    """Find duplicate pairs and contradictions (kept for the morning summary and the Worked out tab), and roll up
    old logs."""
    pairs = duplicates(store)
    store.kv_set("tidy_pairs", pairs)
    conf = contradictions(store)
    store.kv_set("tidy_conflicts", conf)
    rolled = roll_up(store)
    store.log("tidy", None, pairs=len(pairs), conflicts=len(conf), rolled=sum(x["notes"] for x in rolled))
    return pairs


def _shown(d):
    from .speakers import visible
    return visible(d.get("meta"))      # the morning summary is built for anyone: no one's personal notes


def conflict_line(store, short=lambda d: d["title"][:40]):
    live = []
    for a, b, why in store.kv_get("tidy_conflicts") or []:
        da, db = store.get(a), store.get(b)
        if da and db and da["status"] == "active" and db["status"] == "active" and _shown(da) and _shown(db):
            live.append((da, db, why))
    if not live:
        return None
    shown = "; ".join(f"#{a['id']} \"{short(a)}\" vs #{b['id']} \"{short(b)}\"" for a, b, _ in live[:3])
    return f"Disagreeing notes {len(live)}: {shown}. Forget the wrong one in Memory."


def summary_line(store, short=lambda d: d["title"][:40]):
    pairs = store.kv_get("tidy_pairs") or []
    live = []
    for a, b in pairs:
        da, db = store.get(a), store.get(b)
        if da and db and da["status"] == "active" and db["status"] == "active" and _shown(da) and _shown(db):
            live.append((da, db))
    if not live:
        return None
    shown = "; ".join(f"#{a['id']} and #{b['id']} (\"{short(a)}\")" for a, b in live[:3])
    return f"Possible duplicates {len(live)}: {shown}. Forget one in Memory if they are the same."
