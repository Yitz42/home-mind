"""Upkeep: model use and cost are counted; the event log is rolled up and trimmed; phone notifications wait out quiet
hours and stop at the daily limit; the morning summary asks about one old fact, hides secret values, and the Still
true / It changed buttons are recorded.

  python tests/test_upkeep.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:                   # ha.py only needs it for real requests; this test makes none
    sys.modules["aiohttp"] = types.ModuleType("aiohttp")
from homemind.store import Store  # noqa: E402
from homemind.usage import Usage  # noqa: E402
from homemind.ha import HA  # noqa: E402
from homemind.morning import Morning  # noqa: E402
from homemind.control import quiet_window  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeHA(HA):
    def __init__(self):
        super().__init__(None)
        self.token = "x"
        self.calls, self.notes = [], []

    async def call(self, domain, service, data):
        self.calls.append((domain, service, data))
        return 200

    async def notify(self, title, message, nid="home_mind"):
        self.notes.append((title, message))

    async def get_json(self, path, params=None):
        return [{"domain": "notify", "services": {"mobile_app_pixel": {}}}]


async def main():
    d = tempfile.mkdtemp()
    st = Store(os.path.join(d, "m.db"))

    # ---- model use ---------------------------------------------------------------------------------------------
    u = Usage(st, {"claude": (1.0, 5.0)})
    u.record("claude", True, 1000, 200, 900)
    u.record("claude", True, 3000, 100, 1100)
    u.record("claude", False, None, None, 30000)
    u.record("haiku", True, None, None, 2000)
    s = u.summary(30)
    c = next(m for m in s["models"] if m["model"] == "claude")
    check("calls and failures counted", c["calls"] == 3 and c["failures"] == 1, c)
    check("tokens in and out added", c["tokens_in"] == 4000 and c["tokens_out"] == 300, c)
    check("cost from the configured prices", c["cost"] == round((4000 * 1 + 300 * 5) / 1e6, 4), c)
    h = next(m for m in s["models"] if m["model"] == "haiku")
    check("no price, no tokens: calls and time only", h["cost"] is None and h["tokens_in"] == 0 and h["ms"] == 2000, h)
    check("today's totals", s["today"]["calls"] == 4 and s["today"]["tokens"] == 4300, s["today"])

    # ---- event log: roll up and trim ---------------------------------------------------------------------------
    now = time.time()
    st.db.executemany("INSERT INTO events(ts,type,item_id,data) VALUES(?,?,?,?)",
                      [(now - 200 * 86400, "recall", None, "{}")] * 5 + [(now - 120 * 86400, "ask", None, "{}")] * 3 +
                      [(now - 10 * 86400, "recall", None, "{}")] * 2)
    st.db.commit()
    keep = st.remember(text="The Kosk locker code is 4417.", kind="fact")
    before_items = st.db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    n = st.prune_events(90)
    check("old events removed", n == 8, n)
    left = st.db.execute("SELECT COUNT(*) FROM events WHERE ts < ?", (now - 90 * 86400,)).fetchone()[0]
    check("nothing older than the limit left", left == 0)
    rolled = dict(st.db.execute("SELECT type, SUM(n) FROM event_days GROUP BY type").fetchall())
    check("rolled up into daily counts", rolled == {"recall": 5, "ask": 3}, rolled)
    check("recent events kept", st.db.execute("SELECT COUNT(*) FROM events WHERE type='recall' AND ts > ?",
                                              (now - 30 * 86400,)).fetchone()[0] >= 2)
    check("notes untouched", st.db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == before_items)
    st.db.execute("INSERT INTO events(ts,type,item_id,data) VALUES(?,?,?,?)", (now - 300 * 86400, "recall", None, "{}"))
    st.prune_events(90)
    rolled = dict(st.db.execute("SELECT type, SUM(n) FROM event_days GROUP BY type").fetchall())
    check("a second trim adds to the counts", rolled["recall"] == 6, rolled)
    check("0 keeps everything", st.prune_events(0) == 0)

    # ---- quiet hours and the daily limit ----------------------------------------------------------------------
    ha = FakeHA()
    check("quiet window from options", quiet_window({"quiet_start": "22:00", "quiet_end": "07:00"}) == ((22, 0), (7, 0)))
    check("quiet off", quiet_window({"quiet_hours": False, "quiet_start": "22:00", "quiet_end": "07:00"}) is None)
    ha.quiet = ((22, 0), (7, 0))
    at = lambda h, m=0: datetime(2026, 11, 15, h, m)
    check("23:00 is quiet", ha.in_quiet(at(23)))
    check("03:00 is quiet", ha.in_quiet(at(3)))
    check("07:00 is not", not ha.in_quiet(at(7)))
    check("noon is not", not ha.in_quiet(at(12)))
    ha.quiet = ((13, 0), (15, 0))
    check("a daytime window", ha.in_quiet(at(14)) and not ha.in_quiet(at(15, 30)))

    # hold during quiet hours, send after
    now_h = datetime.now().hour
    ha.quiet = ((now_h, 0), ((now_h + 1) % 24, 0))           # quiet right now
    r = await ha.push("notify.mobile_app_pixel", {"message": "one"})
    check("held during quiet hours", r == "held" and not ha.calls and len(ha.held) == 1)
    check("not sent while still quiet", await ha.flush_held() == 0 and not ha.calls)
    ha.quiet = None
    check("sent when quiet hours end", await ha.flush_held() == 1 and ha.calls[0][2]["message"] == "one" and not ha.held)
    ha.cap = 2
    r = [await ha.push("notify.mobile_app_pixel", {"message": str(i)}) for i in range(3)]
    check("daily limit", r == ["sent", "capped", "capped"] and len(ha.calls) == 2, r)

    # ---- morning: one old fact, secrets hidden, buttons ----------------------------------------------------------
    ha = FakeHA()
    old = time.time() - 400 * 86400
    st.clock = lambda: old
    shed = st.remember(text="The shed padlock code is 3381.", kind="fact")
    door = st.remember(text="The back door key is in the blue mug.", kind="fact")
    st.remember(text="Living room light turned on.", kind="fact", source="home")
    st.clock = time.time
    st.db.execute("UPDATE items SET use_count=5 WHERE id=?", (door["id"],))
    st.db.execute("UPDATE items SET use_count=9 WHERE id=?", (shed["id"],))
    st.db.execute("DELETE FROM items WHERE id=?", (keep["id"],))
    st.db.commit()
    m = Morning(st, ha, None)
    text = m.build()
    check("asks about the most-used old fact", m._checkin and m._checkin["id"] == shed["id"], m._checkin)
    check("the secret value is not shown", "3381" not in text and "Still true?" in text, text)
    sent = await m.send(text)
    push = ha.calls[-1][2]
    acts = [a["action"] for a in push["data"]["actions"]]
    check("phone gets Still true / It changed", acts == [f"HM_STILL_{shed['id']}", f"HM_CHANGED_{shed['id']}"], push)
    check("asked fact is marked", (st.get(shed["id"])["meta"] or {}).get("checked"))
    m.build()
    check("next morning asks about another", m._checkin and m._checkin["id"] == door["id"], m._checkin)
    check("non-secret fact shown as it is", "blue mug" in m._checkin_line(m._checkin))
    await m.on_action(f"HM_STILL_{door['id']}")
    dd = st.get(door["id"])
    check("Still true: confirmed and strengthened", dd["meta"].get("confirmed") and dd["strength"] > 1.0, dd)
    await m.on_action(f"HM_CHANGED_{shed['id']}")
    sd = st.get(shed["id"])
    check("It changed: doubted, weakened, and asks for the new value",
          sd["meta"].get("doubted") and sd["strength"] < 1.0 and any("changed" in t.lower() for t, _ in ha.notes), sd)
    m.build()
    check("nothing left to ask", m._checkin is None)
    m.checkin_on = False
    st.db.execute("UPDATE items SET meta='{}'")
    st.db.commit()
    m.build()
    check("switch off: no question", m._checkin is None)
    check("house readings are never asked", all("light" not in (x or "") for x in [m._checkin and m._checkin["title"]]))
    st.remember(text="Your guest wifi password is sunflower88.", title="Guest wifi password sunflower88", kind="fact")
    text = m.build()
    check("secret values hidden in the summary's list", "sunflower88" not in text, text)

    print("UPKEEP OK" if not FAILS else f"UPKEEP FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
