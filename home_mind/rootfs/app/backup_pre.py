"""Run by Home Assistant just before it copies /data into a backup (config.yaml backup_pre; the add-on keeps running).

The memory is SQLite in WAL mode: new writes are appended to mind.db-wal (each frame checksummed, so a half-copied
tail is ignored on restore), while mind.db itself changes only when a checkpoint moves the log into it. A checkpoint
while the backup is copying mind.db leaves a torn copy. Checkpointing (and truncating the log) right before the copy
makes that very unlikely: the next automatic checkpoint is ~1000 pages of new writes away.

Never fails: a non-zero exit would make Home Assistant abort the add-on's backup.
"""
import os
import sqlite3
import sys

DATA = os.environ.get("HOME_MIND_DATA", "/data")


def main(data=DATA):
    path = os.path.join(data, "mind.db")
    if not os.path.exists(path):
        return "no mind.db"
    try:
        db = sqlite3.connect(path, timeout=10)
        try:
            busy, _log, _done = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            db.close()
        return "checkpointed" if not busy else "busy"
    except Exception as e:  # noqa: BLE001
        return f"skipped: {e}"


if __name__ == "__main__":
    print("home mind backup_pre:", main(sys.argv[1] if len(sys.argv) > 1 else DATA))
    sys.exit(0)
