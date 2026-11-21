"""Check-ins by time (0.31): several a day, backdated ones, the day's series, the patterns' fog, water and coffee."""
import os
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind.store import Store  # noqa: E402
from homemind.checkins import Checkins, state_for  # noqa: E402

fails = 0


def check(name, ok, extra=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f"  {extra}" if extra and not ok else ""))
    fails += 0 if ok else 1


def raises(f):
    try:
        f()
    except ValueError:
        return True
    return False


d = tempfile.mkdtemp()
noon = datetime.now().replace(hour=13, minute=46, second=0, microsecond=0).timestamp()   # a fixed midday clock
clock = [noon]
st = Store(os.path.join(d, "t.db"), clock=lambda: clock[0])
ck = Checkins(st, clock=lambda: clock[0])

a = ck.add(fog=3, energy="low", tags=["Tired eyes"], minutes_ago=330)          # 8:16 AM
check("a backdated check-in keeps its time", abs(a["at"] - (noon - 330 * 60)) < 1 and a["time"].startswith("8:16"),
      a["time"])
check("the first one that day", a["nth"] == 1)
b = ck.add(fog=2, energy="ok", tags=["Headache"], note="Long meeting block, no break until 2")
check("a second one the same day", b["nth"] == 2)
c = ck.add(energy="high")                                                      # no fog: its own shelf
check("energy only is fine", c["fog"] is None and c["nth"] == 3)
es = ck.entries(day=a["day"])
check("newest first", [e["fog"] for e in es] == [None, 2, 3], [e["fog"] for e in es])
check("the note is kept", es[1]["note"].startswith("Long meeting"))
fog_items = st.db.execute("SELECT meta FROM items WHERE shelf='body/fog' AND status='active' ORDER BY created DESC").fetchall()
check("only fog ratings go on body/fog (the patterns read the newest)", len(fog_items) == 2 and '"fog": 2' in fog_items[0]["meta"])
check("latest is the newest", ck.latest()["energy"] == "high")
s = ck.series(7)
check("seven days, today last", len(s) == 7 and s[-1]["day"] == a["day"])
check("today: highest fog 3, last fog 2, lowest energy low, 3 check-ins",
      s[-1]["fog"] == 3 and s[-1]["fog_last"] == 2 and s[-1]["energy"] == 1 and s[-1]["n"] == 3, s[-1])
check("bad fog refused", raises(lambda: ck.add(fog=9)))
check("bad energy refused", raises(lambda: ck.add(energy="meh")))
check("an empty check-in refused", raises(lambda: ck.add()))
check("not for later", raises(lambda: ck.add(fog=1, at=noon + 3600)))
check("not for days ago", raises(lambda: ck.add(fog=1, minutes_ago=60 * 48)))
check("state: fog 4 is rough", state_for(4, "ok") == "rough")
check("state: low energy is low", state_for(1, "low") == "low")
check("state: fog 1, ok is good", state_for(1, "ok") == "good")

check("water counts up", ck.log("water")["water"] == 1 and ck.log("water")["water"] == 2)
check("water takes one back", ck.log("water", -1)["water"] == 1)
check("coffee is filed as a drink", ck.log("coffee")["coffee"] == 1)
row = st.db.execute("SELECT meta FROM items WHERE shelf='body/meals' AND status='active'").fetchone()
check("coffee has caffeine set (the patterns' factor)", '"caffeine": true' in row["meta"])
check("coffee taken back", ck.log("coffee", -1)["coffee"] == 0)
check("unknown log refused", raises(lambda: ck.log("tea")))
check("n out of range refused", raises(lambda: ck.log("water", 9)))

print(f"\n{'all passed' if not fails else str(fails) + ' failed'}")
sys.exit(1 if fails else 0)
