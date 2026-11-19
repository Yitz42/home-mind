"""Other assistants as front ends, the three registers, the four plain views, and Firefly III totals (design spec
steps 1, 14, 17 and 18, What you see; phase 15).

Other assistants (Alexa, Gemini for Home, Siri, Home Assistant's Assist on someone else's account): option
other_assistants lists how each reaches Home Mind (a front_end name, a Home Assistant user id or a device id) and its
name. They are treated like a guest: they can't say who is speaking, so they get open-level actions only (lights,
media) and never health, location, finance, room or things answers. A red flag is the exception: its fixed reply
(call 911, 988, Poison Control) is said there too, since it holds nothing private and "check Home Mind" helps nobody
in an emergency; it is logged, and the safety contact told, as anywhere else. Each person can turn them off (/api/assistants), and guest mode turns them all off. Their reworded requests
never count toward a kind of request the local model earns, nor toward a learned template.

Registers (step 17): one voice, three registers.
  normal  the persona as set
  quiet   on a low day, or when the room isn't sure who's there: the persona without remarks or wit
  plain   the "plain voice" switch (per person), a rough day, or right after something failed: no persona at all
No wit on bad days or after failures.

The four views (What you see): Today (energy left first, then at most 3 things Home Mind did), Asks (one at a time),
Memory (things I know about you, one topic at a time), Rules (the one ladder per device: ask me first, tell me
after, just do it). The same four by voice and on the panel.

Firefly III (finance, read-only): totals only (spent, earned, the balance), fetched every 15 minutes onto the now board
for its owner only, never while someone is speaking, with the time it was fetched. Said by code, never by a model,
so nothing about money goes to the cloud. Option firefly_url, firefly_token, firefly_owner.
"""
import re
import time
from datetime import date

OTHER_ANSWER = "Please check Home Mind for that."
PRIVATE_TOPICS = re.compile(
    r"\b(?:sleep|slept|weight|weigh|body fat|heart|hrv|readiness|health|symptom|pain|fog|medic\w*|meds|pills?|dose|"
    r"doctor|crash|tired|mood|money|spent|spend|budget|bank|balance|salary|bill|bills|finance|where (?:is|are|'s)|"
    r"who(?:'s| is) (?:home|in)|room|thing|drill|toolbox)\b", re.I)


# ---- other assistants --------------------------------------------------------------------------------------------
def other_assistant(b, opts, store=None):
    """The other assistant this request came through ({"name", "key"}), or None."""
    for a in (opts or {}).get("other_assistants") or []:
        if isinstance(a, str):
            k, _, n = a.partition(":")
            a = {"key": k.strip(), "name": (n or k).strip()}
        key = str(a.get("key") or "").strip()
        if key and key in (str(b.get("front_end") or ""), str(b.get("user_id") or b.get("user") or ""),
                           str(b.get("device_id") or "")):
            return {"key": key, "name": a.get("name") or key}
    return None


def assistants_off(store, person=None):
    """Turned off by guest mode, or by this person (or anyone, when nobody is named)."""
    from .channels import guest_on
    if guest_on(store):
        return True
    offs = store.kv_get("assistants_off") or {}
    return bool(offs.get((person or "").lower())) if person else any(offs.values())


def set_assistants(store, person, off):
    d = dict(store.kv_get("assistants_off") or {})
    d[(person or "_").lower()] = bool(off)
    store.kv_set("assistants_off", d)
    return d


def other_refuses(text):
    """A question an other assistant's speaker mustn't answer (health, location, finance, rooms, things)."""
    return bool(PRIVATE_TOPICS.search(text or ""))


# ---- registers -------------------------------------------------------------------------------------------------
def register(store, person=None, day=None, failed=False, room_unsure=False):
    plain = store.kv_get("plain_voice") or {}
    st = day.get(person) if day is not None else "unknown"
    if plain.get((person or "_").lower()) or st == "rough" or failed:
        return "plain"
    if st == "low" or room_unsure:
        return "quiet"
    return "normal"


def set_plain(store, person, on):
    d = dict(store.kv_get("plain_voice") or {})
    d[(person or "_").lower()] = bool(on)
    store.kv_set("plain_voice", d)
    return d


# ---- the four views ----------------------------------------------------------------------------------------------
async def view_today(app, person=None):
    from . import today as today_mod
    t = await today_mod.build(app)
    did = []
    dl = getattr(app, "decisions", None)
    if dl is not None:
        for d in dl.recent(10, status="done", since=time.time() - 86400):
            name = ((app.home.catalog or {}).get(d["entity_id"]) or {}).get("name") or d["entity_id"]
            did.append(f"{d['action']} the {name}")
    return {"energy": t.get("energy"), "checkin": t.get("checkin"), "asks_left": t.get("asks_left"),
            "did": did[:3], "travel": bool((app.mind.store.kv_get("travel_mode") or {}).get("on"))}


def view_asks(app, person=None):
    """Everything waiting for you, one at a time (the first is the one to answer)."""
    out = []
    c = getattr(app, "consent", None)
    if c is not None:
        for x in c.to_ask(person):
            out.append({"kind": "consent", "id": x["id"], "ask": x["title"] + "?"})
    tk = getattr(app, "tracking", None)
    if tk is not None:
        for e in tk.to_confirm(person):
            out.append({"kind": "tracker", "id": e["id"], "ask": f"Anything to add to '{e['word']}'?"})
    ap = getattr(app, "approvals", None)
    if ap is not None:
        for aid, p in list(getattr(ap, "pending", {}).items()):
            out.append({"kind": "approval", "id": aid, "ask": f"{p['verb'].capitalize()} the {p['name']}?"})
    th = getattr(app, "things", None)
    if th is not None:
        for n in th.noticed()[:2]:
            out.append({"kind": "thing", "id": n, "ask": f"Keep track of the {n}?"})
    return {"next": out[0] if out else None, "waiting": len(out)}


def view_memory(app, person=None, topic=None):
    """Things I know about you: the topics, or one topic's notes (newest first), only what you may see."""
    from .speakers import visible
    st = app.mind.store
    rows = st.db.execute("SELECT id, title, body, shelf, meta FROM items WHERE status='active' AND kind!='thing' "
                         "ORDER BY updated DESC LIMIT 2000").fetchall()
    import json
    seen = [dict(r) for r in rows if visible(json.loads(r["meta"] or "{}"), person)]
    if not topic:
        counts = {}
        for r in seen:
            t = (r["shelf"] or "inbox").split("/")[0]
            counts[t] = counts.get(t, 0) + 1
        return {"topics": [{"topic": k, "notes": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]}
    notes = [{"id": r["id"], "title": r["title"], "text": (r["body"] or "")[:200]} for r in seen
             if (r["shelf"] or "inbox").split("/")[0] == topic]
    return {"topic": topic, "notes": notes[:10], "more": max(0, len(notes) - 10)}


def view_rules(app):
    """The one ladder per device (ask me first, tell me after, just do it); the machinery stays hidden."""
    home = app.home
    words = {"ask": "ask me first", "tell": "tell me after", "do": "just do it"}
    out = []
    for e, c in sorted((home.catalog or {}).items()):
        try:
            if home.access_of(e) == "none":
                continue
            out.append({"entity_id": e, "name": c.get("name") or e, "rule": words.get(home.rung(e), home.rung(e))})
        except Exception:  # noqa: BLE001
            continue
    return {"rules": out}


# ---- Firefly III ---------------------------------------------------------------------------------------------------
async def firefly_totals(session, url, token, on=None):
    """This month's totals from Firefly III's summary: {"spent", "earned", "balance", "currency"}."""
    import aiohttp
    on = on or date.today()
    start = on.replace(day=1).isoformat()
    async with session.get(url.rstrip("/") + "/api/v1/summary/basic", params={"start": start, "end": on.isoformat()},
                           headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                           timeout=aiohttp.ClientTimeout(total=10)) as r:
        j = await r.json(content_type=None)
    return summarise_firefly(j)


def summarise_firefly(j):
    """Only the three totals, numbers only: no payee, description or note text ever comes through."""
    out = {}
    for k, v in (j or {}).items():
        if not isinstance(v, dict):
            continue
        key = str(v.get("key") or k)
        try:
            val = float(v.get("monetary_value"))
        except (TypeError, ValueError):
            continue
        cur = re.sub(r"[^A-Z]", "", str(v.get("currency_code") or ""))[:3]
        if key.startswith("spent-in"):
            out["spent"], out["currency"] = abs(val), cur
        elif key.startswith("earned-in"):
            out["earned"], out["currency"] = val, cur
        elif key.startswith("balance-in"):
            out["balance"], out["currency"] = val, cur
    return out


def say_money(t, fetched_at):
    if not t:
        return None
    cur = t.get("currency") or ""
    parts = []
    if "spent" in t:
        parts.append(f"spent {t['spent']:,.2f} {cur}".strip())
    if "earned" in t:
        parts.append(f"earned {t['earned']:,.2f} {cur}".strip())
    if "balance" in t:
        parts.append(f"balance {t['balance']:,.2f} {cur}".strip())
    mins = int((time.time() - fetched_at) // 60)
    return "This month: " + ", ".join(parts) + f" (from Firefly III, {mins} min ago)."


MONEY_Q = re.compile(r"\b(?:how much (?:have i|did i|did we|have we) (?:spent|earned)|what(?:'s| is) my balance|"
                     r"(?:spending|spent|money) this month|my budget)\b", re.I)
