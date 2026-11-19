"""The house's time zone for every "today", "last night" and "this week".

Day boundaries (sleep logged for last night, today's protein, the weekly review, briefs) use the process's local
time. In the add-on that comes from the TZ variable Supervisor sets, but only if the image has time zone data:
without it the C library silently uses UTC and "today" rolls over at 7 or 8 PM in New York. The image now ships
tzdata, and at startup Home Mind also reads Home Assistant's own time zone and applies it, so the two always agree.
"""
import logging
import os
import time
from datetime import datetime

log = logging.getLogger("homemind.tz")
ZONEINFO = "/usr/share/zoneinfo"


def has_data(name):
    return bool(name) and os.path.isfile(os.path.join(ZONEINFO, name))


def apply(name):
    """Use this IANA time zone ('America/New_York') for local time. Returns status()."""
    if not name:
        return status()
    if not has_data(name):
        log.warning("time zone %s: no time zone data in the image, so times would be UTC", name)
        return status()
    if os.environ.get("TZ") != name:
        log.info("time zone: %s (was %s)", name, os.environ.get("TZ") or "unset")
    os.environ["TZ"] = name
    time.tzset()
    return status()


def status():
    name = os.environ.get("TZ") or ""
    now = datetime.now().astimezone()
    ok = has_data(name) or (not name and now.utcoffset().total_seconds() != 0)
    return {"time_zone": name or None, "utc_offset": now.strftime("%z"), "local_time": now.strftime("%Y-%m-%d %H:%M"),
            "ok": ok, "problem": None if ok else (
                "no time zone set: days end at midnight UTC" if not name else
                f"no time zone data for {name}: days end at midnight UTC")}
