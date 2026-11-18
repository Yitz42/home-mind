"""The house's time zone (0.28): every "today", "last night" and "this week" ends when the house's day does. Without
time zone data the add-on silently ran on UTC, so after 7 PM in New York a meal counted for tomorrow."""
import os
import sys
import tempfile
import time
from datetime import datetime
import logging
logging.disable(logging.WARNING)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
from homemind import tz  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
check("the image ships time zone data", "apk add --no-cache tzdata" in open(os.path.join(root, "home_mind",
                                                                                          "Dockerfile")).read())
if not tz.has_data("America/New_York"):
    print("skip: no time zone data on this machine")
    sys.exit(0)

os.environ.pop("TZ", None)
time.tzset()
st = tz.apply("Nowhere/Atlantis")
check("an unknown zone isn't applied", os.environ.get("TZ") is None)
os.environ["TZ"] = "Nowhere/Atlantis"
check("a zone without data is reported, not silent", tz.status()["problem"] and "UTC" in tz.status()["problem"])

st = tz.apply("America/New_York")
check("applied: Eastern, -0500 in November", st["time_zone"] == "America/New_York" and st["ok"] and not st["problem"])
late = datetime(2026, 11, 17, 22, 30).timestamp()          # 10:30 PM Eastern = 03:30 UTC the next day
from datetime import timezone  # noqa: E402
check("10:30 PM Eastern is still the 17th (UTC already says the 18th)", datetime.fromtimestamp(late).day == 17 and
      datetime.fromtimestamp(late, timezone.utc).day == 18)

from homemind.store import Store  # noqa: E402
from homemind.fitness import Fitness  # noqa: E402
from homemind.sleeplog import parse_sleep  # noqa: E402
st_ = Store(tempfile.mktemp(), clock=lambda: late)
st_.remember(text="dinner", title="dinner", shelf="body/meals", meta={"at": late, "protein_g": 40}, at=late)
fi = Fitness(st_)
d = fi.tally(now=late)
check("a 10:30 PM dinner counts for that day", d["day"] == "2026-11-17" and d["protein_g"] == 40, d)
morning = datetime(2026, 11, 18, 7, 30).timestamp()
s = parse_sleep("slept from 12 to 7", morning)
check("last night is the night that ended this morning", s["night"].isoformat() == "2026-11-18" and
      s["start"].hour == 0 and s["end"].hour == 7, s)

tz.apply("Europe/London")
check("another house, another day", tz.status()["utc_offset"] == "+0000" and datetime.fromtimestamp(late).day == 18)
tz.apply("America/New_York")
print(f"\n{len(FAILS)} failed" if FAILS else "\nall time zone checks passed")
sys.exit(1 if FAILS else 0)
