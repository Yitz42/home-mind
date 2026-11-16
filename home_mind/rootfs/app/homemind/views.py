"""Saved views (#61): a named search or filter you come back to ("open projects", "warranties", "what I filed this
week"). A view is a search (words), filters (shelf, kind, tags, days back) and an order, or a read-only SQL query on
the notes view (#70). Saved in the database; run from the panel, the API, a tool or by voice ("show my warranties").
"""
import re
import time

KV = "saved_views"
SORTS = ("relevance", "newest", "oldest", "used", "title")
SAY = re.compile(r"^\s*(?:please\s+)?(?:show|open|list|read|run)(?:\s+me)?\s+(?:my|the)\s+(.+?)(?:\s+view)?\s*[.?!]*$", re.I)


def _name(n):
    n = re.sub(r"\s+", " ", str(n or "").strip().lower())
    if not n or len(n) > 60:
        raise ValueError("a view needs a name (up to 60 characters)")
    return n


class Views:
    def __init__(self, store):
        self.store = store

    def all(self):
        v = self.store.kv_get(KV) or {}
        return {k: x for k, x in v.items() if isinstance(x, dict)} if isinstance(v, dict) else {}

    def save(self, name, query="", shelf=None, kind=None, tags=None, days=None, sort=None, limit=20, sql=None):
        name = _name(name)
        if sort and sort not in SORTS:
            raise ValueError(f"sort must be one of {', '.join(SORTS)}")
        if not (query or shelf or kind or tags or days or sql):
            raise ValueError("a view needs a search, a filter or a query")
        if sql:
            from . import sqlview
            sqlview.query(self.store.path, sql)          # check it now, not when it's first opened
        v = {"query": str(query or "")[:300], "shelf": shelf or None, "kind": kind or None,
             "tags": [str(t).lower() for t in (re.split(r"[,\s]+", tags) if isinstance(tags, str) else tags or []) if t][:10],
             "days": float(days) if days else None,
             "sort": sort or ("relevance" if query else "newest"), "limit": max(1, min(int(limit or 20), 200)),
             "sql": sql or None, "saved": time.time()}
        allv = self.all()
        allv[name] = v
        self.store.kv_set(KV, allv)
        return {"name": name, **v}

    def remove(self, name):
        allv = self.all()
        gone = allv.pop(_name(name), None) is not None
        self.store.kv_set(KV, allv)
        return gone

    def find(self, text):
        """The saved view a spoken request names ("show my warranties" -> "warranties"), or None."""
        m = SAY.match(text or "")
        if not m:
            return None
        want = _name(m.group(1))
        allv = self.all()
        if want in allv:
            return want
        stem = lambda x: re.sub(r"(?:ies|y)$", "y", x) if re.search(r"(?:ies|y)$", x) else x.rstrip("s")
        for n in allv:
            if stem(n) == stem(want):
                return n
        return None

    def run(self, name):
        name = _name(name)
        v = self.all().get(name)
        if v is None:
            raise KeyError(f"no view {name}")
        st = self.store
        if v.get("sql"):
            from . import sqlview
            return {"name": name, "view": v, **sqlview.query(st.path, v["sql"])}
        since = st.clock() - v["days"] * 86400 if v.get("days") else None
        if v.get("query"):
            hits = st.recall(v["query"], shelf=v.get("shelf"), kind=v.get("kind"), limit=max(v["limit"] * 3, 30),
                             touch=False, quiet=True)
        else:
            sql, args = "SELECT * FROM items WHERE status IN ('active','faded') AND kind != 'procedure'", []
            if v.get("shelf"):
                sql += " AND (shelf=? OR shelf LIKE ?)"
                args += [v["shelf"], v["shelf"] + "/%"]
            if v.get("kind"):
                sql += " AND kind=?"
                args.append(v["kind"])
            if since:
                sql += " AND created >= ?"
                args.append(since)
            sql += " ORDER BY created DESC LIMIT 2000"
            hits = [st._row(r) for r in st.db.execute(sql, args)]
        from .speakers import visible
        out = []
        for h in hits:
            if not visible(h.get("meta")):
                continue            # someone else's personal note (#48), as in every other search
            if since and h["created"] < since:
                continue
            if v.get("tags") and not set(v["tags"]) <= set(h.get("tags") or []):
                continue
            if h.get("newer"):
                continue            # an older version: the view shows what's true now
            out.append(h)
        key = {"newest": lambda h: -h["created"], "oldest": lambda h: h["created"],
               "used": lambda h: -(h.get("use_count") or 0), "title": lambda h: (h.get("title") or "").lower()}
        if v["sort"] in key:
            out.sort(key=key[v["sort"]])
        items = [{k: h.get(k) for k in ("id", "title", "body", "shelf", "kind", "tags", "created")} for h in out[:v["limit"]]]
        return {"name": name, "view": v, "items": items, "count": len(out)}

    def say(self, name, n=8):
        """A short spoken version of a view."""
        r = self.run(name)
        if "rows" in r:
            if not r["rows"]:
                return f"Your {name} view is empty."
            return f"Your {name} view: " + "; ".join(", ".join(str(c) for c in row) for row in r["rows"][:n]) + "."
        if not r["items"]:
            return f"Nothing in your {name} view right now."
        lines = [(i["title"] or i["body"] or "").strip().rstrip(".") for i in r["items"][:n]]
        more = f" And {r['count'] - n} more." if r["count"] > n else ""
        return f"Your {name} ({r['count']}): " + "; ".join(lines) + "." + more
