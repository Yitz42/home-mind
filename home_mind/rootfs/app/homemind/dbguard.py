"""The database's safety net: daily snapshots, and a check at startup.

Everything Home Mind knows is one SQLite file. A power cut mid-write or a failing disk can leave it damaged, and a
damaged file used to mean Home Mind wouldn't start (or quietly lost what it couldn't read). Now:
  - once a day a snapshot is taken with SQLite's own backup (consistent even while it's in use); the last 7 are kept
  - at startup the file is checked (PRAGMA quick_check). If it's damaged, it's moved aside (kept, never deleted),
    the newest snapshot that checks out is put in its place, and you're told what happened and from when
  - with no good snapshot, Home Mind starts with an empty database and keeps the damaged one for recovery
Home Assistant's own backups still include the whole data folder; these snapshots are the quick way back.
"""
import glob
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime

log = logging.getLogger("homemind.dbguard")
KEEP = 7


def snapdir(path):
    return os.path.join(os.path.dirname(path) or ".", "snapshots")


def check(path):
    """None when the file is fine (or doesn't exist yet), else what's wrong."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            r = c.execute("PRAGMA quick_check").fetchone()
            c.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            c.close()
        return None if r and r[0] == "ok" else f"quick_check: {r[0] if r else 'no answer'}"
    except sqlite3.DatabaseError as e:
        return str(e)


def snapshots(path):
    return sorted(glob.glob(os.path.join(snapdir(path), "mind-*.db")), reverse=True)


def snapshot(conn, path, keep=KEEP, now=None):
    """Today's snapshot through the live connection (SQLite's backup API). Returns its path, or None if today's
    already exists."""
    d = snapdir(path)
    os.makedirs(d, exist_ok=True)
    day = datetime.fromtimestamp(now or time.time()).strftime("%Y-%m-%d")
    dest = os.path.join(d, f"mind-{day}.db")
    if os.path.exists(dest):
        return None
    tmp = dest + ".tmp"
    out = sqlite3.connect(tmp)
    try:
        conn.backup(out)
    finally:
        out.close()
    os.replace(tmp, dest)
    for old in snapshots(path)[keep:]:
        try:
            os.remove(old)
        except OSError:
            pass
    return dest


def guard(path):
    """At startup, before the database is opened. Returns None when all is well, else a report for the user."""
    problem = check(path)
    if problem is None:
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    kept = f"{path}.damaged-{stamp}"
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(path + ext):
            shutil.move(path + ext, kept + ext)
    log.error("the database was damaged (%s); kept it as %s", problem, kept)
    for snap in snapshots(path):
        if check(snap) is None:
            shutil.copy2(snap, path)
            when = os.path.basename(snap)[5:15]
            log.error("restored the database from the snapshot of %s", when)
            return {"problem": problem, "kept": kept, "restored_from": when,
                    "message": f"Home Mind's database was damaged ({problem}). It was restored from the snapshot of "
                               f"{when}: anything saved after that is missing. The damaged file is kept as "
                               f"{os.path.basename(kept)} in Home Mind's data folder."}
    log.error("no good snapshot: starting with an empty database")
    return {"problem": problem, "kept": kept, "restored_from": None,
            "message": f"Home Mind's database was damaged ({problem}) and there was no good snapshot, so it started "
                       f"empty. The damaged file is kept as {os.path.basename(kept)} in Home Mind's data folder; restore "
                       "a Home Assistant backup of Home Mind to get your notes back."}


def status(path):
    snaps = snapshots(path)
    return {"snapshots": len(snaps), "last_snapshot": os.path.basename(snaps[0])[5:15] if snaps else None}
