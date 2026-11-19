"""Expert review fixes: each check is a case that failed before its fix. No network needed.

  python tests/test_review.py
"""
import asyncio
import logging
import os
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:                  # main.py imports aiohttp.web; these checks make no requests
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: None
    m.ClientError = Exception
    m.ClientSession = object
    w = types.ModuleType("aiohttp.web")
    w.middleware = lambda f: f
    m.web = w
    sys.modules["aiohttp"], sys.modules["aiohttp.web"] = m, w
from homemind import main as main_mod  # noqa: E402
from homemind.store import Store  # noqa: E402
from homemind.mind import Mind  # noqa: E402
from homemind.mdsync import MdSync  # noqa: E402
from homemind.mailbox import Mailbox  # noqa: E402

FAILS = []
logging.disable(logging.CRITICAL)


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


NS = types.SimpleNamespace


async def _noop(*a, **k):
    return None


def upkeep_app(tmp):
    """An App with just enough parts for the upkeep loop (Home Assistant away, every option off)."""
    a = object.__new__(main_mod.App)
    a.opts, a.tasks, a._bg = {}, [], {}
    a.ticks = 0

    async def journal_tick():
        a.ticks += 1
    st = Store(os.path.join(tmp, "m.db"))
    a.mind = NS(home=None, store=st, embeddings=None)
    a.ha = NS(available=False, sensor=_noop, flush_held=_noop)
    a.journal = NS(tick=journal_tick)
    a.overnight = NS(tick=_noop)
    a.docs = NS(scan=lambda: {})
    a.mdsync = NS(folder=None)
    a.mailbox = NS(on=False)
    a.compare = NS(due=lambda now: False)
    a.offsite = NS(due=lambda now: False)
    a.home = NS(catalog={}, notices=[])
    a.knowledge = NS(tick=_noop)
    a.updater = NS(check=_noop, apply=_noop)
    a.push_counts = _noop
    return a


async def run_loop(a, rounds=40):
    real = asyncio.sleep

    async def fast(s, *x, **k):
        await real(0)
    main_mod.asyncio.sleep = fast
    try:
        t = asyncio.create_task(a.upkeep())
        for _ in range(rounds):
            await real(0.01)        # real time too: steps that use a worker thread (documents) need it
        died = t.done()
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    finally:
        main_mod.asyncio.sleep = real
    return died


async def upkeep_checks():
    with tempfile.TemporaryDirectory() as tmp:
        # one step outside its own try (here the backup check, as on a 'database is locked') used to end the loop
        a = upkeep_app(tmp)
        calls = []

        def bad_due(now):
            calls.append(now)
            if len(calls) == 1:
                raise RuntimeError("database is locked")
            return False
        a.offsite = NS(due=bad_due)
        died = await run_loop(a)
        check("upkeep: an error in one step doesn't stop the loop", not died and a.ticks >= 3, (died, a.ticks))

        # a long comparison (or night's learning) used to hold the whole loop: comfort, heartbeat, notices waited
        a = upkeep_app(tmp)
        gate = asyncio.Event()
        runs = []

        async def slow_run(seed=None):
            runs.append(1)
            await gate.wait()
            return {"models": {}}
        a.compare = NS(due=lambda now: True, run=slow_run)
        a.overnight = NS(tick=gate.wait)
        died = await run_loop(a)
        check("upkeep: a long comparison or overnight run doesn't stall the loop", not died and a.ticks >= 3, a.ticks)
        check("upkeep: one comparison at a time", len(runs) == 1, runs)
        check("upkeep: background jobs are kept (so they can be cancelled at stop)",
              set(a._bg) == {"compare", "overnight"} and not any(t.done() for t in a._bg.values()), a._bg)
        a.models = NS(stop=_noop)
        await a.on_stop(None)
        check("stop cancels the background jobs", all(t.cancelled() for t in a._bg.values()))

        # a job that fails is logged by its done callback rather than lost
        a = upkeep_app(tmp)

        async def boom():
            raise ValueError("x")
        t = a._spawn("x", boom)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        check("spawn: a failed job is finished and the next one can start", t.done() and a._spawn("x", _noop) is not None)
        a.mind.store.db.close()


class Models:
    models = {}

    def get(self, m):
        raise KeyError(m)


async def voice_checks():
    with tempfile.TemporaryDirectory() as tmp:
        mind = Mind(tmp, Models())
        for t in ("The spare batteries are in the hall drawer.", "Batteries for the remote are in the kitchen.",
                  "The battery charger is in the garage."):
            mind.store.remember(text=t)
        mind.rerank_model = "r"
        mind.embeddings = object()
        gate = asyncio.Event()
        reranked = []

        async def meaning(store, q, hits, k):
            if "voice" in q:
                await gate.wait()           # the voice question is still looking when the typed one comes in
            return hits

        async def rerank(rr, q, hits):
            reranked.append(q)
            return hits
        mind._with_meaning, mind._rerank = meaning, rerank
        va = asyncio.create_task(mind.ask("voice: where are the batteries", None, voice=True))
        await asyncio.sleep(0.05)
        await mind.ask("typed: where are the batteries", None)
        gate.set()
        await va
        check("voice: a typed question at the same time doesn't turn the reranker on for the voice one",
              not any(q.startswith("voice") for q in reranked), reranked)
        check("voice: the typed one is reranked", any(q.startswith("typed") for q in reranked), reranked)
        await mind.ask("voice: and the charger", None, voice=True)
        reranked.clear()
        await mind.search(mind.store, "where are the batteries")
        check("voice: a search after a voice question is reranked again", reranked, reranked)

        # finished test runs used to stay in memory forever (with every question's details)
        for _ in range(3):
            await mind.run_test_now("recall", "brain", seed=1, n=2)
        check("test runs: finished runs are not kept in memory", mind.jobs == {}, list(mind.jobs))
        mind.store.db.close()


def mdsync_checks():
    with tempfile.TemporaryDirectory() as tmp:
        st = Store(os.path.join(tmp, "m.db"))
        fold = os.path.join(tmp, "md")
        sync = MdSync(st, fold)
        sync.run()
        with open(os.path.join(fold, "garage.md"), "w") as f:
            f.write("# Garage code\n\nThe garage code is 7731.\n")
        real = st.remember

        def slow(*a, **k):
            time.sleep(0.2)
            return real(*a, **k)
        st.remember = slow
        errs = []

        def go():
            try:
                sync.run()
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        ts = [threading.Thread(target=go) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        n = st.db.execute("SELECT COUNT(*) FROM items WHERE source='markdown' AND status='active'").fetchone()[0]
        check("markdown sync: two runs at once file a new file once", n == 1 and not errs, (n, errs))
        st.db.close()


def mailbox_checks():
    from email.message import EmailMessage
    with tempfile.TemporaryDirectory() as tmp:
        st = Store(os.path.join(tmp, "m.db"))
        app = NS(mind=NS(store=st), photos=None, docs=None)
        m = EmailMessage()
        m["From"], m["Subject"], m["Message-ID"] = "me@example.com", "Boiler service", "<1@x>"
        m.set_content("The boiler was serviced on 3 November.")
        raw = m.as_bytes()

        class IMAP:
            def __init__(self, host):
                pass

            def login(self, u, p):
                pass

            def select(self, f):
                pass

            def search(self, *a):
                return "OK", [b"1"]

            def fetch(self, num, what):
                time.sleep(0.1)
                return "OK", [(b"1", raw)]

            def store(self, *a):
                pass

            def logout(self):
                pass
        mb = Mailbox(app, {"email_host": "h", "email_user": "u", "email_password": "p",
                           "email_senders": ["me@example.com"], "email_require_verified": False}, imap_factory=IMAP)
        real = st.remember

        def slow(*a, **k):
            time.sleep(0.2)
            return real(*a, **k)
        st.remember = slow
        ts = [threading.Thread(target=mb.run) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        n = st.db.execute("SELECT COUNT(*) FROM items WHERE source='email'").fetchone()[0]
        check("email: two checks at once file an email once", n == 1, n)
        st.db.close()


async def main():
    await upkeep_checks()
    await voice_checks()
    mdsync_checks()
    mailbox_checks()
    print("REVIEW OK" if not FAILS else f"REVIEW FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
