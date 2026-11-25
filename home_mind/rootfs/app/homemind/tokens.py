"""Scoped tokens (#27): one per client, each revocable, instead of one token that can do everything.

  read   ask, search and look (GET requests, /api/ask, /api/recall, MCP tools that only read)
  write  read, and remember, change, forget (not delete for good)
  admin  everything: the main token is admin, and so are tokens made with that scope

A token can also be tied to a person (option people): what it asks is answered as that person (personal notes, #48),
and what it files is theirs. A read or write token tied to no one is answered as a guest (household notes only); an
admin token sees everything, like the main token. Tokens expire (90 days by default; days=0 for never) and are
renewed by making a new one. Only a hash of each token is kept; the token itself is shown once, when it is made.
"""
import hashlib
import re
import secrets
import time

SCOPES = ("read", "write", "admin")
ADMIN_PATHS = [r"^/api/tokens", r"^/api/item/\d+/purge$", r"^/api/purge_about$", r"^/api/automations/(?!draft$)",
               r"^/api/house/suggestion/[^/]+/create$", r"^/api/updates/apply$", r"^/api/backup", r"^/api/restore",
               r"^/api/import", r"^/api/control/", r"^/api/export", r"^/api/vault/key$", r"^/api/compare/run$",
               r"^/api/control_model/", r"^/api/trainer/", r"^/api/email/check$", r"^/api/home/act$", r"^/api/do$",
               r"^/api/controls/",   # house control (and the Home tab's controls): admin, as mind_control/mind_do
               r"^/api/tiles/(?!catalog$|scenes$)",   # House tiles: adding one widens what the controls may switch; cameras
               r"^/api/decisions", r"^/api/replay$",   # the decision log holds everyone's requests: admin only
               r"^/api/access", r"^/api/safety/run$",   # who may move what, and the safety suite
               r"^/api/converse$",   # a voice turn can act on the house, like /api/do
               r"^/api/learned", r"^/api/check$", r"^/api/outbox$", r"^/api/restore_test$", r"^/api/consent", r"^/api/guest$", r"^/api/spend$", r"^/api/surprises", r"^/api/backoff$", r"^/api/spaces$", r"^/api/scenes/", r"^/api/trackers$", r"^/api/meds", r"^/api/significant$", r"^/api/nudges$", r"^/api/travel$", r"^/api/zones", r"^/api/experiments", r"^/api/proposals", r"^/api/plain/", r"^/api/assistants$", r"^/api/plain_voice$", r"^/api/modes", r"^/api/calibration$", r"^/api/now/propose$", r"^/api/worsening$",
               r"^/api/scorecard", r"^/api/labels", r"^/api/shadow", r"^/api/burden", r"^/api/timing"]   # measuring
READ_POSTS = [r"^/api/ask$", r"^/api/recall$", r"^/api/search$", r"^/api/lookup$", r"^/api/models/check$", r"^/api/sql$"]
MCP_READ = {"mind_recall", "mind_get", "mind_review", "mind_house", "mind_browse", "mind_tasks", "mind_ask",
            "mind_models", "mind_test_results", "mind_trend", "mind_home", "mind_automations", "mind_status",
            "mind_updates", "mind_meals", "mind_draft_automation", "mind_sql", "mind_view"}
MCP_ADMIN = {"mind_purge", "mind_purge_about", "mind_install_automation", "mind_control", "mind_do", "mind_run_test"}


def _hash(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


def needed(method, path):
    """The scope a request needs."""
    if any(re.search(p, path) for p in ADMIN_PATHS):
        return "admin"
    if method in ("GET", "HEAD") or any(re.search(p, path) for p in READ_POSTS) or path == "/mcp":
        return "read"           # MCP checks each tool itself
    return "write"


def tool_needs(name, args=None):
    """The scope an MCP tool call needs. A read tool whose action changes things needs what the same action needs over
    HTTP: running or taking over automations and installing updates are admin (/api/automations/..., /api/updates/apply)."""
    a = args if isinstance(args, dict) else {}
    if name == "mind_automations" and (a.get("action") or "list") != "list":
        return "admin"
    if name == "mind_updates" and a.get("apply"):
        return "admin"
    if name == "mind_phrasebook":
        return "read" if (a.get("action") or "list") in ("list", "try") else "write"
    if name == "mind_fitness":
        return "write" if (a.get("action") == "log" or (a.get("action") == "goals" and a.get("goals"))) else "read"
    if name == "mind_view" and (a.get("action") or "list") not in ("list", "run"):
        return "write"          # as POST/DELETE /api/views; listing and running one is a read (GET /api/views...)
    return "admin" if name in MCP_ADMIN else "read" if name in MCP_READ else "write"


def allows(have, need):
    return SCOPES.index(have) >= SCOPES.index(need)


class Tokens:
    def __init__(self, store):
        self.store = store

    def _all(self):
        return self.store.kv_get("tokens") or []

    DAYS = 90

    def make(self, name, scope="read", person=None, days=None):
        days = self.DAYS if days is None else days
        try:
            days = float(days)
        except (TypeError, ValueError):
            raise ValueError("days should be a number (0 = never expires)") from None
        if not 0 <= days <= 3650:
            raise ValueError("days is from 0 (never expires) to 3650")
        if scope not in SCOPES:
            raise ValueError(f"scope is one of {', '.join(SCOPES)}")
        if not (name or "").strip():
            raise ValueError("give the token a name (which client uses it)")
        tok = "hm_" + secrets.token_urlsafe(32)
        rec = {"id": secrets.token_hex(4), "name": name.strip(), "scope": scope, "person": person or None,
               "hash": _hash(tok), "created": time.time(), "last_used": None,
               "expires": time.time() + days * 86400 if days else None}
        self.store.kv_set("tokens", self._all() + [rec])
        return dict({k: v for k, v in rec.items() if k != "hash"}, token=tok)

    def list(self):
        return [{k: v for k, v in r.items() if k != "hash"} for r in self._all()]

    def revoke(self, tid):
        keep = [r for r in self._all() if r["id"] != tid]
        if len(keep) == len(self._all()):
            return False
        self.store.kv_set("tokens", keep)
        return True

    def check(self, tok):
        """The token's record, or None."""
        if not tok or not tok.startswith("hm_"):
            return None
        h = _hash(tok)
        rs = self._all()
        for r in rs:
            if secrets.compare_digest(r["hash"], h):
                if r.get("expires") and time.time() > r["expires"]:
                    return None             # expired: make a new one
                if not r.get("last_used") or time.time() - r["last_used"] > 300:
                    r["last_used"] = time.time()
                    self.store.kv_set("tokens", rs)
                return r
        return None
