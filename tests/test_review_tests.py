"""Expert review round 4 (tests): checks for behavior that hand mutation testing showed no test would notice breaking:
vault unseal/reseal, sealing everything at once, purge of unlinked versions and saved corrections, owner names in any
case, which models count as cloud, garage doors found by name, and the automation trigger edges.

  python tests/test_review_tests.py
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: None
    m.WSMsgType = types.SimpleNamespace(TEXT=1)
    sys.modules["aiohttp"] = m
from homemind import automations as A, privacy, speakers  # noqa: E402
from homemind.cases import Cases  # noqa: E402
from homemind.home import Home  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def fresh(path=None):
    return Store(path or os.path.join(tempfile.mkdtemp(), "m.db"))


# ---- vault: unsealing with the wrong key, sealing twice, sealing everything at once -------------------------------
try:
    from homemind import vault
    HAVE_VAULT = vault.available()
except Exception:  # noqa: BLE001
    HAVE_VAULT = False

if HAVE_VAULT:
    st = fresh()
    key = os.urandom(32)
    st.vault_key = key
    a = st.remember(text="The Ostrava safe code is 7314.", kind="fact")
    secret0 = st.db.execute("SELECT secret FROM items WHERE id=?", (a["id"],)).fetchone()[0]
    check("a code note is sealed when remembered", bool(secret0) and "7314" in st.get(a["id"])["body"])
    # sealing a sealed note again would seal its masked copy over the real text
    check("sealing a sealed note again does nothing", st.seal_item(a["id"]) is False and
          st.db.execute("SELECT secret FROM items WHERE id=?", (a["id"],)).fetchone()[0] == secret0)
    check("... and the real text still opens", "7314" in st.get(a["id"])["body"])
    # vault turned off while another key is loaded: the masked copy must not be written back as the plain text
    st.vault_key = os.urandom(32)
    check("unsealing with the wrong key unseals nothing", st.unseal_all() == 0)
    row = st.db.execute("SELECT secret, body FROM items WHERE id=?", (a["id"],)).fetchone()
    check("... and keeps the sealed text", row[0] == secret0 and "7314" not in row[1], tuple(row))
    st.vault_key = key
    check("with the right key it unseals to the real text", st.unseal_all() == 1 and "7314" in st.db.execute(
        "SELECT body FROM items WHERE id=?", (a["id"],)).fetchone()[0])

    # turning the vault on over existing notes: seal_all must not leave the clear text in the index or the file
    p = os.path.join(tempfile.mkdtemp(), "m.db")
    st = fresh(p)
    for i in range(30):
        st.remember(text=f"filler note {i} about the porch light and the hallway")
    st.remember(text="The Brindle alarm code is qx8841z.", kind="fact")
    st.vault_key = os.urandom(32)
    check("seal_all seals the existing secret", st.seal_all() == 1)
    st.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blob = open(p, "rb").read() + (open(p + "-wal", "rb").read() if os.path.exists(p + "-wal") else b"")
    check("seal_all leaves no clear text in the index or the file", b"qx8841z" not in blob, blob.count(b"qx8841z"))
    st.close()
else:
    print("skip vault checks (cryptography not installed)")

# ---- purge: versions that share a key but aren't linked, and saved corrections holding the value -----------------
st = fresh()
a = st.remember(text="The Ostrava gate code is 1111.", kind="fact", key="ostrava gate|code")
st.set_status(a["id"], "archived")      # archived: the next version doesn't supersede it, only shares the key
b = st.remember(text="The Ostrava gate code is 2222.", kind="fact", key="ostrava gate|code")
cases = Cases(st)
cases.add("what is the ostrava gate code", "2222", wrong="1111")         # a correction kept with no item id
keep = cases.add("where is the drill", "red toolbox")
r = st.purge([b["id"]])
check("purge takes an archived version with the same key", r["deleted"] == 2 and not st.get(a["id"]), r)
check("purge removes saved corrections holding the values", [c["id"] for c in cases.all()] == [keep], cases.all())

# ---- personal notes: the owner's name in any case --------------------------------------------------------------
meta = {"scope": "personal", "owner": "Sam"}
check("a personal note is seen by its owner in any case", speakers.visible(meta, "sam") and speakers.visible(meta, "SAM"))
check("... and not by someone else or an unknown voice", not speakers.visible(meta, "Dana") and not speakers.visible(meta, ""))


# ---- which models are cloud models -------------------------------------------------------------------------------
def model(t, url):
    return types.SimpleNamespace(type=t, cfg={"url": url})


check("an OpenAI-style server on the internet is cloud", privacy.is_cloud(model("openai", "https://api.openai.com/v1")))
check("an Ollama on a public address is cloud", privacy.is_cloud(model("ollama", "http://8.8.8.8:11434")))
check("an OpenAI-style server on the LAN is local", not privacy.is_cloud(model("openai", "http://192.168.1.20:8080/v1")))
check("Ollama on localhost or a .local name is local", not privacy.is_cloud(model("ollama", "http://localhost:11434"))
      and not privacy.is_cloud(model("ollama", "http://gpu.local:11434")))

# ---- garage doors and gates need a yes, however Home Assistant names them --------------------------------------
h = Home(fresh(), None, {"home_controls": ["cover.*"]})
h.catalog = {"cover.big_one": {"name": "Garage door", "attrs": {}}, "cover.x2": {"name": "Side gate", "attrs": {}},
             "cover.garage": {"name": "Cover", "attrs": {}}, "cover.blinds": {"name": "Living room blinds", "attrs": {}},
             "cover.y": {"name": "Y", "attrs": {"device_class": "garage"}}}
check("a cover named a garage door or gate is sensitive (no device class)",
      h.sensitive("cover.big_one") and h.sensitive("cover.x2") and h.sensitive("cover.garage") and h.sensitive("cover.y"))
check("blinds are not", not h.sensitive("cover.blinds"))

# ---- automation trigger edges --------------------------------------------------------------------------------------
eng = A.Engine(fresh(), types.SimpleNamespace(token="t", session=None), tempfile.mkdtemp())


def s(state, **attrs):
    return {"state": state, "attributes": attrs}


below = {"trigger": "numeric_state", "entity_id": "sensor.b", "below": 20}
check("numeric_state fires on crossing the limit", eng._state_matches(below, "sensor.b", s("21"), s("19")))
check("... not again while it stays past it", not eng._state_matches(below, "sensor.b", s("19"), s("18")))
nf = {"trigger": "state", "entity_id": "sensor.p", "not_from": ["unavailable", "unknown"]}
check("not_from: a change out of 'unavailable' doesn't fire", not eng._state_matches(nf, "sensor.p", s("unavailable"), s("on")))
check("... a real change does", eng._state_matches(nf, "sensor.p", s("off"), s("on")))
eng.states["zone.home"] = s("0", friendly_name="Home", latitude=5, longitude=5, radius=300)
leave = {"trigger": "zone", "entity_id": "device_tracker.p", "zone": "zone.home", "event": "leave"}
check("zone leave fires on leaving", eng._state_matches(leave, "device_tracker.p", s("Home", latitude=5, longitude=5),
                                                        s("not_home", latitude=9, longitude=9)))
check("... not on arriving or staying", not eng._state_matches(leave, "device_tracker.p", s("not_home", latitude=9, longitude=9),
                                                               s("Home", latitude=5, longitude=5)) and
      not eng._state_matches(leave, "device_tracker.p", s("Home", latitude=5, longitude=5), s("Home", latitude=5, longitude=5.0001)))

print(f"\n{len(FAILS)} failed" if FAILS else "\nall ok")
sys.exit(1 if FAILS else 0)
