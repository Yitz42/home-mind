"""Expert review (round 3, network integrations): each check is a failure mode that went wrong before its fix.
No network needed: the outside services are stand-ins (and a local socket that never answers).

  python tests/test_review_ext.py
"""
import asyncio
import imaplib
import logging
import os
import socket
import sys
import tempfile
import threading
import types
from email.message import EmailMessage

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: types.SimpleNamespace(total=k.get("total"))
    m.ClientError = type("ClientError", (Exception,), {})
    m.BasicAuth = lambda *a: a
    sys.modules["aiohttp"] = m
from homemind import mailbox as mailbox_mod  # noqa: E402
from homemind import messaging, offsite  # noqa: E402
from homemind.mailbox import Mailbox  # noqa: E402
from homemind.messaging import Telegram  # noqa: E402
from homemind.models import Model, ModelError  # noqa: E402
from homemind.store import Store  # noqa: E402

FAILS = []
logging.disable(logging.CRITICAL)
NS = types.SimpleNamespace


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class Resp:
    def __init__(self, status=200, body="", headers=None, j=None):
        self.status, self.body, self.headers, self.j = status, body, headers or {}, j

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self.body

    async def json(self, content_type=None):
        return self.j

    async def read(self):
        return self.body.encode() if isinstance(self.body, str) else self.body


class Session:
    """Answers each request with the next of `answers` (a Resp, or an exception to raise)."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def _next(self, method, url, kw):
        self.calls.append((method, url, kw))
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a

    def post(self, url, **kw):
        return self._next("post", url, kw)

    def get(self, url, **kw):
        return self._next("get", url, kw)

    def put(self, url, **kw):
        return self._next("put", url, kw)

    def delete(self, url, **kw):
        return self._next("delete", url, kw)


def mail(mid, subject="Fwd: Boiler service", body="Boiler serviced on Nov 3.", sender="me@example.com"):
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"] = f"Me <{sender}>", subject, mid
    m["Date"] = "Mon, 02 Nov 2026 10:00:00 +0000"
    m.set_content(body)
    return m.as_bytes()


def mail_app(tmp):
    st = Store(os.path.join(tmp, "m.db"))
    return NS(mind=NS(store=st), photos=None, docs=None)


# ---- email -------------------------------------------------------------------------------------------------------
def email_hang():
    """A mail server that accepts the connection and then never answers: imaplib has no timeout of its own, so the
    run (and the mail lock, and the upkeep step awaiting it) waited forever."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    real = imaplib.IMAP4_SSL
    old_t = mailbox_mod.TIMEOUT if hasattr(mailbox_mod, "TIMEOUT") else None
    mailbox_mod.TIMEOUT = 1
    mailbox_mod.imaplib.IMAP4_SSL = lambda h, **k: real(h, port, **k)     # the default factory, to a local port
    out = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mb = Mailbox(mail_app(tmp), {"email_host": "127.0.0.1", "email_user": "u", "email_password": "p",
                                         "email_senders": ["me@example.com"], "email_require_verified": False})

            def go():
                try:
                    out["r"] = mb.run()
                except Exception as e:  # noqa: BLE001
                    out["e"] = e
            t = threading.Thread(target=go, daemon=True)
            t.start()
            t.join(15)
            check("a mail server that never answers times out instead of hanging the mail run",
                  not t.is_alive() and "e" in out, out)
            check("... and frees the lock for the next run", mb._running.acquire(timeout=1))
    finally:
        mailbox_mod.imaplib.IMAP4_SSL = real
        if old_t is not None:
            mailbox_mod.TIMEOUT = old_t
        srv.close()


class ServerIMAP:
    """Server-like flags: fetching RFC822 or BODY[] sets \\Seen (RFC 3501); BODY.PEEK[] doesn't."""
    msgs, seen = {}, set()

    def __init__(self, host):
        pass

    def login(self, u, p):
        pass

    def select(self, f):
        pass

    def search(self, *a):
        return "OK", [b" ".join(n for n in sorted(self.msgs) if n not in self.seen)]

    def fetch(self, num, what):
        if "PEEK" not in what.upper():
            ServerIMAP.seen.add(num)
        return "OK", [(b"1 (BODY[] {n}", self.msgs[num]), b")"]

    def store(self, num, op, flag):
        ServerIMAP.seen.add(num)

    def logout(self):
        pass


def email_unseen_on_failure():
    ServerIMAP.msgs = {b"1": mail("<a@x>", "Fwd: Boiler service"), b"2": mail("<b@x>", "Fwd: Car tyres", "New tyres Nov 4.")}
    ServerIMAP.seen = set()
    with tempfile.TemporaryDirectory() as tmp:
        app = mail_app(tmp)
        st = app.mind.store
        real = st.remember
        state = {"fail": True}

        def flaky(**kw):
            if state["fail"] and "Boiler" in (kw.get("title") or ""):
                raise RuntimeError("database is locked")
            return real(**kw)
        st.remember = flaky
        mb = Mailbox(app, {"email_host": "h", "email_user": "u", "email_password": "p",
                           "email_senders": ["me@example.com"], "email_require_verified": False}, imap_factory=ServerIMAP)
        try:
            r = mb.run()
        except Exception as e:  # noqa: BLE001
            r = {"error": repr(e)}
        check("a mail that fails to file stays unseen on the server", b"1" not in ServerIMAP.seen, ServerIMAP.seen)
        check("... and the next mail is still filed", b"2" in ServerIMAP.seen and r.get("filed") == 1, r)
        state["fail"] = False
        r = mb.run()
        n = st.db.execute("SELECT COUNT(*) FROM items WHERE source='email'").fetchone()[0]
        check("... and is filed by the next run", r.get("filed") == 1 and n == 2 and b"1" in ServerIMAP.seen, (r, n))


# ---- telegram ----------------------------------------------------------------------------------------------------
TOKEN = "123456:ABCdefSECRETtoken"


def tg_app(session):
    return NS(models=NS(session=session))


async def telegram_token_hidden():
    class ClientResponseError(Exception):       # aiohttp's own errors carry the request URL in their text
        pass
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    tg = Telegram(tg_app(Session([ClientResponseError(f"502, message='Bad Gateway', url='{url}'")])), TOKEN, [])
    sleeps = []
    real_sleep = asyncio.sleep

    async def stop(s):
        sleeps.append(s)
        raise asyncio.CancelledError
    messaging.asyncio.sleep = stop
    try:
        await tg.run_forever()
    except asyncio.CancelledError:
        pass
    finally:
        messaging.asyncio.sleep = real_sleep
    check("the bot token is kept out of the error /api/connections shows", tg.last_error and TOKEN not in tg.last_error
          and "Bad Gateway" in tg.last_error, tg.last_error)


async def telegram_long_and_busy():
    sent = []

    class S(Session):
        def post(self, url, **kw):
            text = kw["json"].get("text", "")
            units = len(text.encode("utf-16-le")) // 2
            if self.answers:                    # first: rate limited
                self.answers.pop(0)
                return Resp(j={"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 1",
                               "parameters": {"retry_after": 1}})
            if units > 4096:
                return Resp(j={"ok": False, "description": "Bad Request: message is too long"})
            sent.append(text)
            return Resp(j={"ok": True, "result": {}})
    tg = Telegram(tg_app(S([1])), TOKEN, [])
    real_sleep = asyncio.sleep

    async def no_sleep(s):
        return None
    messaging.asyncio.sleep = no_sleep
    try:
        long = "\U0001F600" * 3000 + " done"
        try:
            await tg.send(1, long)
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = repr(e)
    finally:
        messaging.asyncio.sleep = real_sleep
    check("a long answer full of emoji is sent (Telegram counts UTF-16 units) after a 429 wait",
          ok is True and "".join(sent) == long, ok)


async def telegram_photo_download_fails():
    with tempfile.TemporaryDirectory() as tmp:
        st = Store(os.path.join(tmp, "m.db"))
        app = NS(mind=NS(store=st), models=NS(session=Session([Resp(j={"ok": True, "result": {"file_path": "p/1.jpg"}}),
                                                               Resp(status=404, body='{"ok":false}')])),
                 scope_meta=lambda meta, note, who: meta, photos=NS(add=lambda *a: None))
        tg = Telegram(app, TOKEN, ["100: Dana"])
        try:
            await tg.handle({"chat": {"id": 100}, "photo": [{"file_id": "a"}], "caption": "Fuse box label"})
            raised = False
        except Exception:  # noqa: BLE001
            raised = True
        n = st.db.execute("SELECT COUNT(*) FROM items WHERE source='telegram'").fetchone()[0]
        check("a photo whose download fails isn't filed as a note with an error page for a photo", raised and n == 0, n)


# ---- models ------------------------------------------------------------------------------------------------------
async def model_retry():
    import homemind.models as models_mod
    real_sleep = models_mod.asyncio.sleep
    waits = []

    async def fake_sleep(s):
        waits.append(s)
    models_mod.asyncio.sleep = fake_sleep
    ok = {"content": [{"type": "text", "text": "fine"}], "usage": {"input_tokens": 1, "output_tokens": 1}}
    import json
    try:
        s = Session([Resp(529, '{"type":"error","error":{"type":"overloaded_error"}}'), Resp(200, json.dumps(ok))])
        m = Model({"name": "c", "type": "anthropic", "api_key": "sk-ant-SECRETKEY123"}, s)
        r = await m.chat("sys", [{"role": "user", "content": "hi"}])
        check("an overloaded (529) answer is asked once more and succeeds", r["text"] == "fine" and len(s.calls) == 2, r)
        s = Session([Resp(503, "busy"), Resp(503, "busy"), Resp(200, json.dumps(ok))])
        m = Model({"name": "c", "type": "anthropic", "api_key": "sk-ant-SECRETKEY123"}, s)
        try:
            await m.chat("sys", [{"role": "user", "content": "hi"}])
            e = None
        except ModelError as x:
            e = x
        check("... but only once (no retry storm)", e is not None and len(s.calls) == 2, len(s.calls))
        s = Session([Resp(429, "slow down", {"retry-after": "3600"})])
        m = Model({"name": "c", "type": "anthropic", "api_key": "sk-ant-SECRETKEY123"}, s)
        try:
            await m.chat("sys", [{"role": "user", "content": "hi"}])
        except ModelError:
            pass
        check("... and not when told to wait an hour", len(s.calls) == 1 and 3600 not in waits, waits)
        s = Session([Resp(401, '{"error":"Incorrect API key provided: sk-ant-SECRETKEY123"}')])
        m = Model({"name": "o", "type": "openai", "url": "http://x/v1", "api_key": "sk-ant-SECRETKEY123"}, s)
        res = await m.check()
        check("an API key echoed by the server is kept out of the error /api/models/check shows",
              not res["ok"] and "SECRETKEY" not in res["error"] and "401" in res["error"], res)
    finally:
        models_mod.asyncio.sleep = real_sleep


# ---- off-site backups ------------------------------------------------------------------------------------------
async def offsite_prune():
    with tempfile.TemporaryDirectory() as tmp:
        st = Store(os.path.join(tmp, "m.db"))
        st.remember(text="The boiler was serviced in March.")
        st.kv_set("offsite_files", ["home-mind-old1.hmbk", "home-mind-old2.hmbk"])
        s = Session([Resp(201), Resp(500), Resp(204)])          # PUT ok, first DELETE fails, second ok
        o = offsite.Offsite(st, {"backup_target": "webdav", "backup_url": "https://dav.example/hm",
                                 "backup_passphrase": "pw", "backup_keep": 1}, s)
        offsite.vault.seal_file = lambda pw, data: b"sealed"
        await o.run(now=1_800_000_000)
        files = st.kv_get("offsite_files")
        check("an old backup the server refused to delete is remembered and pruned next time",
              "home-mind-old1.hmbk" in files and "home-mind-old2.hmbk" not in files and len(files) == 2, files)


email_hang()
email_unseen_on_failure()
asyncio.run(telegram_token_hidden())
asyncio.run(telegram_long_and_busy())
asyncio.run(telegram_photo_download_fails())
asyncio.run(model_retry())
asyncio.run(offsite_prune())
print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
