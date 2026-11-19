"""Ways in, held to account (design spec step 1 and Throughout; phase 10).

Each channel proves its sender and has its own limits:
  telegram   the chat must be on telegram_chats; in a group chat the sender's own user id must be one of the people
             listed there too (a group member nobody listed is ignored); at most 30 messages an hour per chat
  email      only from email_senders, and the message must say it passed its sender check (the receiving server's
             Authentication-Results, option email_authserv_id or else the topmost, with dkim, spf or dmarc =pass naming
             the sender's own domain) unless email_require_verified is off; at most
             50 a day. Mail is filed as notes only, and nothing from mail or documents ever becomes a learned record
             or an action
  mcp        each token as scoped (tokens.py); at most 300 calls an hour per token
  voice and the panel: as before

Guest mode: microphones off (each voice satellite's mute switch is turned on, where it has one), Home Mind answers
nothing by voice except safety phrases, and keeps no transcript of voices it doesn't know. Turned on and off from
the panel or /api/guest.

The spend cap: option monthly_spend_cap (dollars). Once this month's cloud spend (from usage.py, for models with
prices set) reaches it, cloud models aren't used until the month turns; answers come from local models or the notes,
and this is said once. It's a second line: the hard cap belongs on the API key itself.
"""
import re
import time
from datetime import date

LIMITS = {"telegram": (30, 3600), "email": (50, 86400), "mcp": (300, 3600)}
AUTH_OK = re.compile(r"\b(dkim|spf|dmarc)=pass\b[^;]*", re.I)


class Limits:
    def __init__(self, clock=None):
        self.clock = clock or time.time
        self.seen = {}

    def allow(self, channel, who="", limits=None):
        n, window = (limits or LIMITS).get(channel, (0, 0))
        if not n:
            return True
        now = self.clock()
        k = (channel, str(who))
        q = [t for t in self.seen.get(k, []) if now - t < window]
        if len(q) >= n:
            self.seen[k] = q
            return False
        q.append(now)
        self.seen[k] = q
        return True


def telegram_sender_ok(msg, chats):
    """The chat is listed; in a group the sender must be a listed person too (their private chat id is their user
    id). Returns (ok, why)."""
    chat = (msg.get("chat") or {}).get("id")
    if chat not in chats:
        return False, "chat not listed"
    if (msg.get("chat") or {}).get("type") in ("group", "supergroup") or (isinstance(chat, int) and chat < 0):
        uid = (msg.get("from") or {}).get("id")
        if uid not in chats:
            return False, "a group member who isn't listed"
    return True, ""


def email_verified(msg, sender, authserv=None):
    """Did the receiving server say this mail passed its sender check, for the sender's own domain?

    Only the receiving server's own Authentication-Results counts: the header whose authserv-id is the one named
    (option email_authserv_id, e.g. "mx.google.com"), or else the topmost one, which the last server added (a sender
    can write any header lower down). A pass counts only with the sender's domain named exactly in it (header.from,
    header.d, header.i or smtp.mailfrom), never a bare "dkim=pass" or a domain that merely contains it."""
    dom = (sender or "").rpartition("@")[2].lower().strip()
    if not dom:
        return False
    heads = [str(h) for h in (msg.get_all("Authentication-Results") or [])]
    if authserv:
        heads = [h for h in heads if h.split(";", 1)[0].strip().lower() == authserv.strip().lower()]
    else:
        heads = heads[:1]
    for h in heads:
        for m in AUTH_OK.finditer(h):
            for prop in re.findall(r"\b(?:header\.from|header\.d|header\.i|smtp\.mailfrom)=([^\s;]+)", m.group(0),
                                   re.I):
                d = prop.rpartition("@")[2].strip("<>\"'").lower()
                if d == dom or d.endswith("." + dom) and m.group(1).lower() != "spf":
                    return True
    return False


# ---- guest mode --------------------------------------------------------------------------------------------------
GUEST_KV = "guest_mode"


def guest_on(store):
    return bool((store.kv_get(GUEST_KV) or {}).get("on"))


async def set_guest(app, on):
    st = app.mind.store
    st.kv_set(GUEST_KV, {"on": bool(on), "at": time.time()})
    muted = []
    cat = getattr(getattr(app, "home", None), "catalog", None) or {}
    ha = getattr(app, "ha", None)
    for eid in cat:
        if eid.startswith("switch.") and eid.endswith("_mute"):
            try:
                await ha.call("switch", "turn_on" if on else "turn_off", {"entity_id": eid}, approved=True)
                muted.append(eid)
            except Exception:  # noqa: BLE001 - best effort: the switch may be gone
                pass
    return {"on": bool(on), "microphones": muted}


# ---- the spend cap -----------------------------------------------------------------------------------------------
def month_spend(usage):
    start = date.today().replace(day=1).isoformat()
    total = 0.0
    for r in usage.rows(31):
        if r["day"] >= start and r.get("cost") is not None:
            total += r["cost"]
    return round(total, 4)


def over_cap(usage, cap):
    try:
        cap = float(cap or 0)
    except (TypeError, ValueError):
        return False
    return cap > 0 and month_spend(usage) >= cap


def priced(usage):
    return set(getattr(usage, "prices", {}) or {})
