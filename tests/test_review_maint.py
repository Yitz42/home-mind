"""Expert review regressions (maintainability): copies of the same rule that had drifted apart.

  python tests/test_review_maint.py
"""
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    sys.modules["aiohttp"] = types.ModuleType("aiohttp")
from homemind import speakers as S  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def as_viewer(who, fn, *a, **k):
    tok = S.VIEWER.set(who)
    try:
        return fn(*a, **k)
    finally:
        S.VIEWER.reset(tok)


# speakers.visible() (one note at a time: get/seen) and speakers.sql() (search, browse, tasks, views...) are the same
# rule written twice. sql() matched the owner with a LIKE on the stored JSON, which escapes accents ("Zoë") and
# which SQLite lowercases in ASCII only: Zoë's own personal notes were missing from her searches while seen() showed
# them, and an underscore in a name was a LIKE wildcard.
st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
owners = ["Zoë", "José", "Bob", "ÉLODIE", "bo_b"]
ids = {o: st.remember(f"{o} gym locker code is 44{i}2", meta={"scope": "personal", "owner": o})["id"]
       for i, o in enumerate(owners)}
shared = st.remember("The shared gym locker code is 9090")["id"]
for viewer in ["zoë", "José", "bob", "élodie", "Élodie", "bo_b", "boxb", "Zoe", "Ann", ""]:
    found = {r["id"] for r in as_viewer(viewer, st.recall, "gym locker code", limit=20)}
    want = {i for o, i in ids.items() if viewer and o.lower() == viewer.lower()} | {shared}
    check(f"search shows {viewer or 'an unknown caller'} exactly the notes seen() does", found == want,
          f"found {sorted(found)}, want {sorted(want)}")
    by_one = {i for i in list(ids.values()) + [shared] if as_viewer(viewer, st.seen, i)}
    check(f"seen() and search agree for {viewer or 'an unknown caller'}", by_one == found)
    vs, va = as_viewer(viewer, S.sql)
    rows = {r["id"] for r in st.db.execute(f"SELECT id FROM items WHERE {vs}", va)}
    check(f"sql() and visible() agree row by row for {viewer or 'an unknown caller'}",
          rows == {r["id"] for r in st.db.execute("SELECT id, meta FROM items")
                   if S.visible(json.loads(r["meta"] or "{}"), viewer)})
check("everyone sees everything with no viewer set", len(st.recall("gym locker code", limit=20)) == len(owners) + 1)
check("hm_seen tolerates bad meta", S.seen_sql("{not json", "bob") == 0 and S.seen_sql(None, "bob") == 1)

print(f"\n{len(FAILS)} failed" if FAILS else "\nALL OK")
sys.exit(1 if FAILS else 0)
