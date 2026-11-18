"""The database's safety net (0.28): daily snapshots, and at startup a damaged database is set aside (kept) and the
newest good snapshot put in its place, with a report for the user."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind import dbguard  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


import logging  # noqa: E402
logging.disable(logging.ERROR)
d = tempfile.mkdtemp()
path = os.path.join(d, "mind.db")
check("no database yet: nothing to do", dbguard.guard(path) is None)
st = Store(path)
st.remember(text="The drill is in the garage.", title="Drill")
check("a healthy database passes", dbguard.check(path) is None)
day = time.time() - 3 * 86400
for i in range(9):                                    # nine days of snapshots, the last 7 kept
    dbguard.snapshot(st.db, path, keep=7, now=day - (8 - i) * 86400)
snap = dbguard.snapshot(st.db, path, keep=7)
check("today's snapshot", snap and snap.endswith(".db") and os.path.exists(snap))
check("not twice a day", dbguard.snapshot(st.db, path, keep=7) is None)
check("only the last 7 kept", len(dbguard.snapshots(path)) == 7, len(dbguard.snapshots(path)))
st.remember(text="The ladder is in the shed.", title="Ladder")    # after the snapshot: lost on restore
st.db._c.close()

with open(path, "r+b") as f:                         # a power cut mid-write: the file's header is garbage
    f.seek(0)
    f.write(b"\x00garbage" * 64)
check("a damaged database is noticed", dbguard.check(path) is not None)
rep = dbguard.guard(path)
check("restored from today's snapshot", rep and rep["restored_from"] == time.strftime("%Y-%m-%d"), rep)
check("the damaged file is kept", os.path.exists(rep["kept"]))
check("the message says what's missing", "anything saved after that is missing" in rep["message"])
st2 = Store(path)
titles = {r["title"] for r in st2.db.execute("SELECT title FROM items")}
check("the snapshot's notes are back", "Drill" in titles and "Ladder" not in titles, titles)
st2.db._c.close()

for s in dbguard.snapshots(path):                    # every snapshot damaged too: start empty, say so
    with open(s, "r+b") as f:
        f.write(b"\x00bad" * 64)
with open(path, "r+b") as f:
    f.write(b"\x00garbage" * 64)
rep = dbguard.guard(path)
check("no good snapshot: an empty start, and it says so", rep and rep["restored_from"] is None and
      "started empty" in rep["message"] and not os.path.exists(path), rep)
st3 = Store(path)
check("and Home Mind opens a fresh database", st3.db.execute("SELECT count(*) FROM items").fetchone()[0] == 0)
check("status counts snapshots", dbguard.status(path)["snapshots"] == 7)
print(f"\n{len(FAILS)} failed" if FAILS else "\nall database safety checks passed")
sys.exit(1 if FAILS else 0)
