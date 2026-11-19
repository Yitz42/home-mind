"""Set 8 (0.30): Telegram (#52), web lookups for current facts (#55), email ingestion (#64) and prompt caching (#45).
No network needed: the outside services are stand-ins.

  python tests/test_connect.py
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from email.message import EmailMessage

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "home_mind", "rootfs", "app"))
try:
    import aiohttp  # noqa: F401
except ImportError:
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = lambda **k: None
    m.ClientError = Exception
    sys.modules["aiohttp"] = m
from homemind import weblook  # noqa: E402
from homemind.weblook import Web  # noqa: E402
from homemind.messaging import Telegram, parse_chats  # noqa: E402
from homemind.mailbox import Mailbox, text_of  # noqa: E402
from homemind.photos import Photos  # noqa: E402
from homemind.docs import Docs  # noqa: E402
from homemind.models import Model  # noqa: E402
from homemind.mind import Mind  # noqa: E402
from homemind.speakers import VIEWER  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({extra})" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class Resp:
    def __init__(self, j):
        self.j = j

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        return self.j


class Session:
    def __init__(self, j):
        self.j, self.calls = j, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        return Resp(self.j)


async def web():
    check("current-fact questions are spotted", weblook.wants("What's the latest version of Home Assistant?", True) and
          weblook.wants("Is the Elm Street pharmacy open today?", True))
    check("not when the memory answered", not weblook.wants("What's the latest version of Home Assistant?", False))
    check("not for questions about the household", not weblook.wants("What is my current router password?", True))
    check("not for plain questions", not weblook.wants("Where is the ladder?", True))
    sx = Session({"results": [{"title": "Release 2026.11", "url": "https://example.org/r", "content": "<b>New</b> things",
                               "publishedDate": "2026-11-05T00:00:00"}]})
    w = Web({"web_search": "searxng", "web_search_url": "http://sx.local/", "people": [{"name": "Dana"}]}, lambda: sx)
    t = await w.lines("What's the latest version of Home Assistant?", True)
    check("SearXNG results come dated, with links, tags stripped", "FROM THE WEB" in t and "https://example.org/r" in t and
          "2026-11-05" in t and "<b>" not in t, t)
    check("only the question is sent", sx.calls[-1][0] == "http://sx.local/search" and
          sx.calls[-1][1]["q"] == "What's the latest version of Home Assistant?")
    n = len(sx.calls)
    await w.lines("What's the current price of Dana's medication?", True)
    await w.lines("What's the current price for the alarm code 4821?", True)
    check("a household name or a secret is never sent", len(sx.calls) == n)
    br = Session({"web": {"results": [{"title": "Pharmacy hours", "url": "https://ex.com", "description": "Open 9-5"}]}})
    wb = Web({"web_search": "brave", "web_search_key": "k"}, lambda: br)
    t = await wb.lines("Is the Elm Street pharmacy open today?", True)
    check("Brave works too, with its key", "Open 9-5" in t and br.calls[0][2]["X-Subscription-Token"] == "k", t)
    check("off by default", not Web({}, lambda: None).on and not Web({"web_search": "off"}, lambda: None).on)

    class Boom(Session):
        def get(self, *a, **k):
            raise RuntimeError("down")
    check("a failing search adds nothing", await Web({"web_search": "brave", "web_search_key": "k"}, lambda: Boom({})).lines(
        "Is the pharmacy open today?", True) == "")
    m = Mind(tempfile.mkdtemp(), None)
    m.web = w
    m.store.remember(text="The ladder is in the garage.")
    _, _, ctx = await m.context_for(m.store, "What's the latest version of Home Assistant?")
    check("the web lines join the context when the notes don't answer", "FROM THE WEB" in ctx, ctx[:300])
    _, _, ctx = await m.context_for(m.store, "Where is the ladder?")
    check("and stay out when they do", "FROM THE WEB" not in ctx)


class App:
    def __init__(self):
        self.mind = Mind(tempfile.mkdtemp(), None)
        self.photos = Photos(self.mind.store, tempfile.mkdtemp())
        self.docs = Docs(self.mind.store)
        self.opts = {"personal_rule": "first_person"}
        self.asked = []
        orig = self.mind.ask

        async def ask(q, model=None, **k):
            self.asked.append((q, k.get("speaker"), k.get("conversation_id")))
            return await orig(q, model, **k)
        self.mind.ask = ask

    def scope_meta(self, meta, text, viewer):
        from homemind.speakers import scope_for
        if viewer:
            scope, owner = scope_for(text, viewer, "first_person")
            meta = dict(meta, said_by=viewer, scope=scope, **({"owner": owner} if owner else {}))
        return meta


async def telegram():
    check("chats parse", parse_chats(["123: Dana", "-456", "bad"]) == {123: "Dana", -456: ""} and
          parse_chats({"7": "Sam"}) == {7: "Sam"})
    app = App()
    tg = Telegram(app, "tok", ["100: Dana", "200"])
    sent = []

    async def fake_file(fid):
        return b"\xff\xd8\xff\xe0" + b"x" * 50
    tg._file = fake_file
    r = await tg.handle({"chat": {"id": 999}, "text": "/start"})
    check("/start from a stranger only says the chat id", "999" in r and "telegram_chats" in r)
    check("a stranger's question is ignored", await tg.handle({"chat": {"id": 999}, "text": "What is the wifi password?"}) is None
          and not app.asked)
    r = await tg.handle({"chat": {"id": 100}, "text": "remember that my locker code is 3141"})
    hits = app.mind.store.recall("locker code")
    check("\"remember ...\" files a note, as the chat's person", r.startswith("Saved") and hits and
          hits[0]["meta"].get("said_by") == "Dana" and hits[0]["meta"].get("scope") == "personal", hits[:1])
    app.mind.store.remember(text="The ladder is in the garage.")
    r = await tg.handle({"chat": {"id": 100}, "text": "Where is the ladder?"})
    check("anything else is a question for that person, in its own conversation",
          app.asked[-1] == ("Where is the ladder?", "Dana", "telegram:100") and "garage" in r.lower(), (app.asked, r))
    await tg.handle({"chat": {"id": 200}, "text": "What's my locker code?"})
    check("a chat without a name asks as an unknown person", app.asked[-1][1] == "")
    r = await tg.handle({"chat": {"id": 100}, "photo": [{"file_id": "a"}, {"file_id": "b"}], "caption": "Boiler label in the basement"})
    hit = app.mind.store.recall("boiler label")[0]
    check("a photo with a caption is filed with the photo", r.startswith("Saved with the photo") and hit["meta"].get("photos"))
    r = await tg.handle({"chat": {"id": 100}, "photo": [{"file_id": "a"}]})
    check("a photo without a caption asks for one", "caption" in r)
    check("off without a token", not Telegram(app, "", []).on)


def mail(sender="me@example.com", subject="Fwd: Dishwasher warranty", body="Warranty until March 2028.", html=None,
         attach=(), mid="<1@x>", date="Mon, 02 Nov 2026 10:00:00 +0000"):
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"], m["Date"] = f"Me <{sender}>", subject, mid, date
    if html:
        m.set_content(html, subtype="html")
    else:
        m.set_content(body)
    for name, ctype, data in attach:
        mt, st_ = ctype.split("/")
        m.add_attachment(data, maintype=mt, subtype=st_, filename=name)
    return m.as_bytes()


def email_():
    app = App()
    mb = Mailbox(app, {"email_host": "h", "email_user": "u", "email_password": "p", "email_senders": ["me@example.com"], "email_require_verified": False})
    r = mb.file(mail())
    hit = app.mind.store.recall("When does the dishwasher warranty end?")[0]
    check("an email from an allowed sender is filed", r.get("id") and "2028" in hit["body"] and
          hit["title"] == "Dishwasher warranty" and hit["meta"]["from"] == "me@example.com" and hit["meta"].get("valid_from"),
          hit)
    check("the same email is read once", mb.file(mail())["skipped"] == "already read")
    check("other senders are ignored", "not allowed" in mb.file(mail(sender="spam@evil.com", mid="<2@x>"))["skipped"])
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
    r = mb.file(mail(mid="<3@x>", subject="Boiler manual", body="Manual attached.",
                     attach=[("boiler.txt", "text/plain", b"Bleed the radiators each autumn."),
                             ("photo.jpg", "image/jpeg", b"\xff\xd8\xff\xe0" + b"y" * 40)]))
    check("text attachments go to documents, images become photos", r["docs"] == ["boiler.txt"] and r["photos"] == ["photo.jpg"], r)
    t = text_of(__import__("email").message_from_bytes(
        mail(html="<p>Booking <b>confirmed</b></p><script>x()</script><br>Room 12"), policy=__import__("email").policy.default))
    check("HTML becomes text", "Booking confirmed" in t and "Room 12" in t and "x()" not in t, t)
    t = text_of(__import__("email").message_from_bytes(
        mail(body="The gate code changed to 5512.\n\nOn Mon, Nov 2, 2026 Dana wrote:\n> old stuff\n"),
        policy=__import__("email").policy.default))
    check("quoted replies are cut", "5512" in t and "old stuff" not in t, t)

    class IMAP:
        flagged = []

        def __init__(self, host):
            self.msgs = {b"1": mail(mid="<10@x>", subject="Fwd: Car service", body="Car serviced on Nov 1."),
                         b"2": mail(sender="x@evil.com", mid="<11@x>")}

        def login(self, u, p):
            self.who = (u, p)

        def select(self, f):
            pass

        def search(self, *a):
            return "OK", [b"1 2"]

        def fetch(self, num, what):
            return "OK", [(b"1 (RFC822 {n}", self.msgs[num]), b")"]

        def store(self, num, op, flag):
            IMAP.flagged.append(num)

        def logout(self):
            pass
    mb2 = Mailbox(app, {"email_host": "h", "email_user": "u", "email_password": "p", "email_senders": ["me@example.com"], "email_require_verified": False},
                  imap_factory=IMAP)
    r = mb2.run()
    check("a mailbox run reads, files, skips and marks as seen", r["read"] == 2 and r["filed"] == 1 and r["skipped"] == 1 and
          IMAP.flagged == [b"1", b"2"], r)
    check("off without a server", not Mailbox(app, {}).on and Mailbox(app, {}).run() == {"enabled": False})


async def caching():
    m = Model({"name": "c", "type": "anthropic", "api_key": "k", "model": "claude-haiku-5-5"}, None)
    sent = []

    async def post(url, payload, headers=None):
        sent.append(payload)
        return {"content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 5000, "output_tokens": 2}}
    m._post = post
    log = "LOG:\n" + "x " * 10000
    r = await m.chat("sys", [{"role": "user", "content": log + "\n\nQUESTION: where?"}], cache_until="\n\nQUESTION:")
    c = sent[-1]["messages"][0]["content"]
    check("a long repeated prefix is marked for caching", isinstance(c, list) and c[0].get("cache_control") and
          c[1]["text"].startswith("\n\nQUESTION:") and c[0]["text"] + c[1]["text"] == log + "\n\nQUESTION: where?")
    check("cached tokens are reported", r.get("tokens_cached") == 5000 and r["tokens_in"] == 5010, r)
    await m.chat("sys", [{"role": "user", "content": "LOG:\nshort\n\nQUESTION: where?"}], cache_until="\n\nQUESTION:")
    check("a short prefix isn't (below Claude's minimum)", isinstance(sent[-1]["messages"][0]["content"], str))
    await m.chat("S" * 20000, [{"role": "user", "content": "hi"}])
    check("a long system prompt is cached", isinstance(sent[-1]["system"], list) and sent[-1]["system"][0]["cache_control"])
    await m.chat("short system", [{"role": "user", "content": "hi"}])
    check("a short one isn't", sent[-1]["system"] == "short system")
    m2 = Model({"name": "o", "type": "openai", "url": "http://x/v1"}, None)
    check("other model types get the message unchanged", m2._with_cache([{"role": "user", "content": log + "\n\nQUESTION: q"}],
                                                                        "\n\nQUESTION:")[0]["content"].endswith("q"))
    m3 = Model({"name": "n", "type": "anthropic", "api_key": "k", "cache": False}, None)
    check("caching can be turned off per model", isinstance(m3._with_cache([{"role": "user", "content": log + "\n\nQUESTION: q"}],
                                                                           "\n\nQUESTION:")[0]["content"], str))


asyncio.run(web())
asyncio.run(telegram())
email_()
asyncio.run(caching())
print(f"\n{len(FAILS)} failed" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
