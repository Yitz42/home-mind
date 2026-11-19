"""Numbered, forward-only schema changes for a SQLite file (design spec: "Migrations").

  - A database's version is its PRAGMA user_version. Step n (1-based) of a list brings version n-1 to n.
  - Steps only ever go forward. A step is SQL (run as a script) or a function taking the connection.
  - Before a file on disk is changed, every pending step runs first on a copy of it (SQLite's backup API, so a
    live WAL database copies safely). If any step fails on the copy, the real file is left exactly as it was and
    the error is raised: a bad migration never half-applies to the real data.
  - On the real file the steps run one at a time, each in its own transaction with its version bump, so a stop
    part-way leaves a database at a known version that simply continues next time.
"""
import os
import sqlite3
import tempfile


class MigrationError(RuntimeError):
    pass


def version(db):
    return db.execute("PRAGMA user_version").fetchone()[0]


def _apply(db, steps, start):
    for n in range(start + 1, len(steps) + 1):
        step = steps[n - 1]
        try:
            db.execute("BEGIN")
            if callable(step):
                step(db)
            else:
                for stmt in _statements(step):
                    db.execute(stmt)
            db.execute(f"PRAGMA user_version = {n}")
            db.execute("COMMIT")
        except Exception as e:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise MigrationError(f"migration {n} failed: {e}") from e
    return len(steps)


def _statements(sql):
    """Split a script into statements (executescript would commit on its own and break the step's transaction)."""
    out, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def migrate(db, steps, path=None):
    """Bring db (a sqlite3 connection in autocommit mode: isolation_level=None) up to len(steps).
    path: the file behind db; when given, pending steps are tried on a copy first. Returns (from, to)."""
    have = version(db)
    if have > len(steps):
        raise MigrationError(f"database is at version {have}, newer than this code knows ({len(steps)}): "
                             "refusing to touch it (an older add-on after a newer one?)")
    if have == len(steps):
        return have, have
    if path and path != ":memory:" and os.path.exists(path):
        trial(db, steps)
    _apply(db, steps, have)
    return have, len(steps)


def trial(db, steps):
    """Run the pending steps on a throwaway copy of db. Raises MigrationError if any fails."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        copy = sqlite3.connect(tmp, isolation_level=None)
        try:
            db.backup(copy)
            _apply(copy, steps, version(copy))
            bad = copy.execute("PRAGMA integrity_check").fetchone()[0]
            if bad != "ok":
                raise MigrationError(f"integrity check after migrating a copy: {bad}")
        finally:
            copy.close()
    finally:
        for p in (tmp, tmp + "-wal", tmp + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass
