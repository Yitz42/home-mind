"""A read-only SQL view of the memory (#70): ask your notes anything SQL can ("how many notes per shelf", "what did I
file last March"), from the panel or a tool, without any way to change them.

Only two views can be read: `notes` (every note, secret text hidden) and `links`. The connection is opened
read-only, every statement is checked before it runs (SELECT only; no other tables, no ATTACH, no writes, no
PRAGMA), and a query stops after a few seconds or 500 rows.
"""
import sqlite3
import time

MAX_ROWS = 500
MAX_SECONDS = 3.0
VIEWS = """
CREATE TEMP VIEW notes AS SELECT id, kind, key, shelf, status, supersedes AS replaces, source,
  CASE WHEN secret IS NOT NULL OR meta LIKE '%"sensitive": true%' THEN '[secret]' ELSE title END AS title,
  CASE WHEN secret IS NOT NULL OR meta LIKE '%"sensitive": true%' THEN '[secret]' ELSE body END AS body,
  tags, datetime(created, 'unixepoch', 'localtime') AS created, datetime(updated, 'unixepoch', 'localtime') AS updated,
  created AS created_ts, use_count, strength,
  json_extract(meta, '$.category') AS category, json_extract(meta, '$.owner') AS owner,
  json_extract(meta, '$.scope') AS scope
FROM main.items;
CREATE TEMP VIEW links AS SELECT a, b, kind FROM main.links;
"""
HELP = {"notes": ["id", "kind", "key", "shelf", "status", "replaces", "source", "title", "body", "tags", "created",
                  "updated", "created_ts", "use_count", "strength", "category", "owner", "scope"],
        "links": ["a", "b", "kind"]}


class SqlError(ValueError):
    pass


def _connect(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2, check_same_thread=False)
    db.executescript(VIEWS)
    return db


ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
BLOCKED_FUNCS = {"load_extension", "readfile", "writefile", "edit", "fts5"}


def query(path, sql, params=()):
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise SqlError("give a query")
    if ";" in sql:
        raise SqlError("one statement at a time")
    if len(sql) > 4000:
        raise SqlError("query too long")
    db = _connect(path)
    started = time.monotonic()

    def auth(action, a1, a2, dbname, source):
        if action == sqlite3.SQLITE_READ:
            # a1 = table, a2 = column, source = the view reading it (None for the query itself)
            if dbname == "temp" and a1 in HELP:
                return sqlite3.SQLITE_OK
            if dbname == "main" and a1 in ("items", "links") and (source in HELP or a2 == ""):
                # through a view; or no column at all (count(*) after the view is flattened)
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_DENY if (a2 or "").lower() in BLOCKED_FUNCS else sqlite3.SQLITE_OK
        if action in ALLOWED:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_RECURSIVE:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    db.set_authorizer(auth)
    db.set_progress_handler(lambda: 1 if time.monotonic() - started > MAX_SECONDS else 0, 10000)
    try:
        cur = db.execute(sql, params)
        cols = [c[0] for c in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.DatabaseError as e:
        msg = str(e)
        if "interrupted" in msg:
            msg = f"stopped after {MAX_SECONDS:.0f} seconds"
        elif "not authorized" in msg or "prohibited" in msg:
            msg = "only SELECT on the notes and links views is allowed"
        raise SqlError(msg) from None
    finally:
        db.set_progress_handler(None, 0)
    db.close()
    more = len(rows) > MAX_ROWS
    return {"columns": cols, "rows": [list(r) for r in rows[:MAX_ROWS]], "truncated": more,
            "ms": round((time.monotonic() - started) * 1000)}
