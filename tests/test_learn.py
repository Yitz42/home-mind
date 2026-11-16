"""Set 7 (0.29): learned settings for lights, fans, blinds and speakers (#30), reshelving over time (#32), photos
with notes (#36), QR labels (#63) and a conversation that follows you to another device (#53). No network needed.

  python tests/test_learn.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: None
    sys.modules["aiohttp"] = m
from homemind import prefs as P, qr, filing  # noqa: E402
from homemind.photos import Photos  # noqa: E402
from homemind.filing import Filer  # noqa: E402
from homemind.home import Home  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.mind import Mind  # noqa: E402
from homemind.speakers import VIEWER  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def new(clock=None):
    return Store(os.path.join(tempfile.mkdtemp(), "m.db"), clock=clock)


def at(day, hour):
    return datetime(2026, 11, day, hour, 15).timestamp()      # Nov 2 2026 is a Monday


def lamp(b, state="on", ctx=None):
    return {"light.desk": {"domain": "light", "state": state, "attrs": {"brightness": b}, "ctx": ctx or {}}}


async def preferences():
    st = new()
    pf = P.Prefs(st)
    check("situations", P.band(at(2, 19)) == "weekday evening" and P.band(at(7, 9)) == "weekend morning" and
          P.band(at(3, 2)) == "weekday night" and P.band(at(3, 23)) == "weekday night")
    pf.observe(lamp(255), now=at(2, 18))
    check("the first sight isn't a setting", st.db.execute("SELECT COUNT(*) FROM pref_obs").fetchone()[0] == 0)
    for d in range(2, 7):                     # five weekday evenings: dimmed to about 40%
        pf.observe(lamp(255), now=at(d, 18))
        pf.observe(lamp(100 + d), now=at(d, 19))
    pf.observe(lamp(30, ctx={"parent_id": "auto1"}), now=at(6, 20))
    pf.observe(lamp(200), now=at(6, 20) + 60, own={"light.desk": at(6, 20)})
    learned = pf.learned(now=at(7, 12))
    ev = [p for p in learned if p["band"] == "weekday evening"]
    check("a preference is learned from five agreeing settings", ev and 39 <= ev[0]["value"] <= 42 and ev[0]["n"] >= 5,
          learned)
    check("automation and Home Mind changes don't count", all(r["value"] > 35 for r in st.db.execute(
        "SELECT value FROM pref_obs WHERE band='weekday evening'")) and not any(
        abs(r["value"] - 78) < 1 for r in st.db.execute("SELECT value FROM pref_obs")))
    check("turning off isn't a setting", pf.observe(lamp(0, "off"), now=at(7, 19)) == 0)
    n = pf.file_notes({"light.desk": "Desk lamp"}, now=at(7, 12))
    hits = st.recall("How bright do I like the desk lamp in the evening?")
    check("filed as a note the model can find", n >= 1 and hits and "usually" in hits[0]["body"], [h["body"] for h in hits])
    check("filing again adds nothing new", pf.file_notes({"light.desk": "Desk lamp"}, now=at(7, 12)) == 0)
    st2 = new()
    pf2 = P.Prefs(st2)
    for k, v in enumerate([20, 90, 45, 70, 10, 100]):
        pf2.observe(lamp(255 if k % 2 else 0), now=at(2, 19) + k * 600)
        pf2.observe(lamp(v * 2.55), now=at(2, 19) + k * 600 + 60)
    check("settings that don't agree teach nothing", not pf2.learned(now=at(3, 12)))

    class HA:
        calls = []

        async def state(self, eid):
            return {"state": "on"}

        async def call(self, dom, svc, data):
            self.calls.append((dom, svc, data))
            return "ok"
    ha = HA()
    h = Home(st, ha, {"home_controls": ["light.*"]})
    h.catalog = {"light.desk": {"name": "Desk lamp", "domain": "light", "state": "off", "attrs": {}, "kind": "device"}}
    h.prefs = types.SimpleNamespace(preferred=lambda eid: {"value": 40} if eid == "light.desk" else None)
    await h.act("light.desk", "on")
    check("turning on without a level uses the learned one", ha.calls[-1][2].get("brightness_pct") == 40, ha.calls)
    await h.act("light.desk", "brightness", 80)
    check("a level you say wins", ha.calls[-1][2].get("brightness_pct") == 80)
    check("Home Mind's own change is remembered", "light.desk" in h.own)


def reshelving():
    now = [time.time() - 30 * 86400]
    st = new(clock=lambda: now[0])
    for k in range(8):
        st.remember(text=f"Bake the sourdough bread with rye flour and yeast, batch {k}.", shelf="kitchen/recipes")
        st.remember(text=f"Car oil change and tire pressure check at the garage, visit {k}.", shelf="car")
    a = st.remember(text="Rye flour sourdough bread with extra yeast.", shelf="car", meta={"filed_by": "learned"})
    b = st.remember(text="Rye flour sourdough bread for Sunday.", shelf="car")
    c = st.remember(text="Sourdough bread starter with rye flour and yeast.", shelf="car", meta={"moved_from": "inbox"})
    d = st.remember(text="Rye sourdough bread loaf with yeast and flour.", shelf="inbox")
    e = st.remember(text="Car tire pressure at the garage.", shelf="car", meta={"filed_by": "learned"})
    fresh_now = time.time()
    now[0] = fresh_now
    f = st.remember(text="Rye flour sourdough bread with yeast again.", shelf="car", meta={"filed_by": "learned"})
    fl = Filer(st)
    dry = filing.reshelve(fl, apply=False)
    check("a dry run moves nothing", st.get(a["id"])["shelf"] == "car" and dry["moved"])
    r = filing.reshelve(fl)
    check("a note filed by the model is moved when filing now knows better",
          st.get(a["id"])["shelf"] == "kitchen/recipes" and st.get(a["id"])["meta"]["reshelved_from"] == "car", r)
    check("an old inbox note is filed", st.get(d["id"])["shelf"] == "kitchen/recipes")
    check("a note you placed gets a suggestion, not a move", st.get(b["id"])["shelf"] == "car" and
          st.get(b["id"])["meta"].get("suggested_shelf") == "kitchen/recipes")
    check("a note you moved is left alone", st.get(c["id"])["shelf"] == "car" and
          "suggested_shelf" not in st.get(c["id"])["meta"])
    check("a note in the right place stays", st.get(e["id"])["shelf"] == "car")
    check("a recent note waits", st.get(f["id"])["shelf"] == "car")
    m = st.get(b["id"])["meta"]
    m.pop("suggested_shelf")
    m["keep_shelf"] = True
    st.db.execute("UPDATE items SET meta=? WHERE id=?", (json.dumps(m), b["id"]))
    st.db.commit()
    filing.reshelve(fl)
    check("kept where it is: no suggestion again", "suggested_shelf" not in st.get(b["id"])["meta"])
    check("running again moves nothing more", not filing.reshelve(fl)["moved"])


def photos():
    st = new()
    folder = tempfile.mkdtemp()
    ph = Photos(st, folder)
    n = st.remember(text="The spare key is hidden behind the downspout.")
    jpg = b"\xff\xd8\xff\xe0" + os.urandom(200)
    r = ph.add(n["id"], jpg, "rusty downspout by the porch")
    check("a photo is kept with its note", r["file"].endswith(".jpg") and os.path.exists(os.path.join(folder, r["file"])) and
          st.get(n["id"])["meta"]["photos"][0]["caption"] == "rusty downspout by the porch")
    hits = st.recall("Which note has the porch photo?")
    check("the caption makes the note findable", hits and hits[0]["id"] == n["id"], [h["body"] for h in hits])
    check("the same photo twice is kept once", ph.add(n["id"], jpg)["file"] == r["file"] and
          len(st.get(n["id"])["meta"]["photos"]) == 1)
    for data, why in [(b"", "empty"), (b"MZ\x90\x00 not an image", "not a photo"), (b"\xff\xd8\xff" + b"0" * (13 << 20), "too big")]:
        try:
            ph.add(n["id"], data)
            check(f"refused: {why}", False)
        except ValueError:
            check(f"refused: {why}", True)
    try:
        ph.add(99999, jpg)
        check("refused: no such note", False)
    except KeyError:
        check("refused: no such note", True)
    png = b"\x89PNG\r\n\x1a\n" + os.urandom(50)
    webp = b"RIFF\x00\x00\x00\x00WEBPVP8 " + os.urandom(20)
    check("PNG and WebP are photos too", ph.add(n["id"], png)["file"].endswith(".png") and
          ph.add(n["id"], webp)["file"].endswith(".webp"))
    for bad in ["../m.db", "x.jpg", f"{n['id']}-abc.jpg/../../etc", "/etc/passwd"]:
        try:
            ph.path(bad)
            check(f"bad photo name refused: {bad}", False)
        except (ValueError, KeyError):
            check(f"bad photo name refused: {bad}", True)
    check("removing a photo deletes its file", ph.remove(n["id"], r["file"]) and not os.path.exists(os.path.join(folder, r["file"])))
    st.purge([n["id"]])
    check("purging the note sweeps its photos", ph.sweep() == 2 and not os.listdir(folder), os.listdir(folder))


def labels():
    for t in ["Garage shelf 3", "https://ha.local:8123/hassio/ingress/local_home_mind?hm_thing=Basement%20freezer", "é" * 50]:
        m = qr.encode(t)
        n = len(m)
        finder = all(m[y][x] == (max(abs(x - 3), abs(y - 3)) != 2) for y in range(7) for x in range(7))
        check(f"QR for {t[:20]!r} has the right size and finders", (n - 17) % 4 == 0 and finder)
        try:
            import cv2
            import numpy as np
            s, b = 6, 4
            img = np.full(((n + 2 * b) * s,) * 2, 255, np.uint8)
            for y in range(n):
                for x in range(n):
                    if m[y][x]:
                        img[(y + b) * s:(y + b + 1) * s, (x + b) * s:(x + b + 1) * s] = 0
            check(f"QR for {t[:20]!r} decodes back", cv2.QRCodeDetector().detectAndDecode(img)[0] == t)
        except ImportError:
            pass
    svg = qr.svg("Garage shelf 3", label="Garage <shelf> 3")
    check("label SVG has the text, escaped", svg.startswith("<svg") and "Garage &lt;shelf&gt; 3" in svg)
    try:
        qr.encode("x" * 400)
        check("too long for a label is refused", False)
    except ValueError:
        check("too long for a label is refused", True)


async def handoff():
    d = tempfile.mkdtemp()
    m = Mind(d, None)
    m.store.remember(text="The Velmar locker code is 5512.", kind="fact")
    m.store.remember(text="The Velmar locker is in the basement.", kind="fact")
    tok = VIEWER.set("Dana")
    try:
        await m.ask("What is the code for the Velmar locker?", conversation_id="kitchen")
        r = await m.ask("Where is it?", conversation_id="phone")
    finally:
        VIEWER.reset(tok)
    check("a follow-up on another device knows what came before",
          "basement" in r["answer"].lower() or any("basement" in (h.get("body") or "") for h in r.get("hits", [])[:2]),
          r.get("answer"))
    tok = VIEWER.set("Sam")
    try:
        r2 = await m.ask("Where is it?", conversation_id="tablet")
    finally:
        VIEWER.reset(tok)
    check("another person doesn't pick it up", "basement" not in (r2.get("answer") or "").lower() or
          "Velmar" not in m.resolve("Where is it?", "tablet2"))
    m2 = Mind(d, None)
    check("conversations survive a restart", "kitchen" in m2._conversations() or "phone" in m2._conversations())
    old = m2.HANDOFF_SECONDS
    m2.HANDOFF_SECONDS = -1
    tok = VIEWER.set("Dana")
    try:
        m2._handoff("car", m2._conversations())
    finally:
        VIEWER.reset(tok)
    check("an old conversation isn't handed on", "car" not in m2._conversations())
    m2.HANDOFF_SECONDS = old


asyncio.run(preferences())
reshelving()
photos()
labels()
asyncio.run(handoff())
print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
