"""A picture of each thing (#20): "tell me everything about the garage" gathers what memory knows about it, not
only the notes that happen to say "garage".

  kept there   things whose place is inside it, at any depth (the drill in the red toolbox in the garage)
  notes        notes that name it, and the notes linked to them (#12)
  chores       chores and maintenance for it
  fields       typed fields (model, serial, warranty) of it
  where it is  its own place, and what that is in

Built at question time from the notes, so it is never out of date. Shown as one block before the notes.
"""
import re

from . import structure
from .store import STOP, singular, words

ABOUT = re.compile(r"\b(?:everything|all|anything)\s+(?:you know\s+|we know\s+|I know\s+)?about\s+(?:the |my |our )?(.+?)[?.!]*$|"
                   r"\btell me about\s+(?:the |my |our )?(.+?)[?.!]*$|"
                   r"\bwhat (?:do|does) (?:you|we|I) know about\s+(?:the |my |our )?(.+?)[?.!]*$|"
                   r"\b(?:summari[sz]e|overview of)\s+(?:the |my |our )?(.+?)[?.!]*$", re.I)


def thing_of(question):
    m = ABOUT.search(question or "")
    if not m:
        return None
    t = next(g for g in m.groups() if g).strip().lower()
    return t if t and t not in STOP and len(t) <= 40 else None


def _has(text, thing):
    tw = [singular(w) for w in words(thing)]
    have = {singular(w) for w in words(text)}
    return bool(tw) and all(w in have for w in tw)


def picture(store, thing, now, cap=14):
    """Lines about a thing, grouped; [] when memory knows nothing about it."""
    cur = structure._places(store)
    above, y = set(), next((x for x in cur if _has(x, thing)), None)
    while y in cur and y not in above and len(above) < 6:     # where the thing itself is, all the way up
        above.add(y)
        y = cur[y][0]
        above.add(y)
    inside = []                                   # everything whose chain of places reaches the thing
    for x in cur:
        if x in above:
            continue        # "the garage is next to the toolbox" while the toolbox is in the garage: not inside it
        chain, y, seen = [], x, set()
        while y in cur and y not in seen and len(chain) < 6 and structure._held(cur, y):   # "next to" doesn't hold
            seen.add(y)
            y = cur[y][0]
            chain.append(y)
        if any(_has(c, thing) for c in chain) and not _has(x, thing):
            via = chain[:chain.index(next(c for c in chain if _has(c, thing)))]
            inside.append(x + (f" (in the {' in the '.join(via)})" if via else ""))
    own = [f"{cur[x][0]}" for x in cur if _has(x, thing)]
    tw = [w for w in words(thing) if w not in STOP]
    rows = [store._row(r) for r in store.db.execute(
        "SELECT * FROM items WHERE status='active' AND kind != 'procedure' AND source NOT IN ('home','automations',"
        "'calendar','ticktick','habits','tidy','evening') AND " + " AND ".join("(title || ' ' || body) LIKE ?" for _ in tw) +
        " ORDER BY created DESC LIMIT 40", [f"%{w}%" for w in tw])] if tw else []
    from .speakers import visible
    rows = [d for d in rows if visible(d.get("meta")) and _has(d["title"] + " " + d["body"], thing)]
    linked = []
    ids = {d["id"] for d in rows}
    for d in rows[:8]:
        for o in store.linked(d["id"]):
            if o not in ids:
                x = store.get(o)
                if x and x["status"] == "active" and visible(x.get("meta")):
                    linked.append(x)
                    ids.add(o)
    # only this thing's chores are worked out (each one reads the notes for when it was last done)
    chores = structure.chore_states(store, now, [h for h in structure._rows(store, "meta LIKE '%\"chore\"%'")
                                                 if (h.get("meta") or {}).get("chore")
                                                 and _has(h["meta"]["chore"]["thing"], thing)])
    fields = [(d["meta"].get("fields") or {}) for d in rows if (d.get("meta") or {}).get("fields")]
    out = []
    if own:
        out.append(f"Where it is: in the {own[0]}.")
    if inside:
        out.append("Kept there: " + "; ".join(sorted(inside)[:cap]) + ".")
    for f in fields[:2]:
        out.append("Details: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in f.items()) + ".")
    for c in chores[:3]:
        out.append(f"Chore: {c['verb']} the {c['thing']} (every {c['every']} days; {structure._state(c['left'])}).")
    seen = set()
    for d in rows + linked:
        b = (d["body"] or d["title"]).strip()
        if b not in seen:
            seen.add(b)
            out.append(f"Note #{d['id']}: {b[:160]}")
        if len(out) >= cap:
            break
    return out


def lines(store, question, now):
    t = thing_of(question)
    if not t:
        return ""
    p = picture(store, t, now)
    return (f"EVERYTHING KNOWN ABOUT THE {t.upper()} (gathered from all notes, places and links):\n" +
            "\n".join(f"- {x}" for x in p)) if p else ""
