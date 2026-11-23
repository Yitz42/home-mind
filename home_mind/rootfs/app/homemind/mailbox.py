"""Email ingestion (#64): forward a receipt, a warranty, a booking or a note to a mailbox Home Mind reads, and it is
filed. Options: `email_host` (IMAP over TLS), `email_user`, `email_password`, `email_folder` (default INBOX) and
`email_senders` (only mail from these addresses is read; empty = none). Checked every `email_minutes`.

- The subject becomes the title and the text the note (HTML is turned into text; quoted replies and signatures are
  cut); the note is filed by the usual learned filing and dated by the email's date.
- PDF and text attachments go to Documents (#50); image attachments become photos of the note (#36).
- Each message is read once (by Message-ID) and marked as seen.
"""
import email
import email.policy
import html
import imaplib
import logging
import re
import ssl
import threading
import time
from email.utils import parseaddr, parsedate_to_datetime

log = logging.getLogger("homemind.mail")
MAX_BODY = 6000
KV = "mail_seen"
TIMEOUT = 60        # seconds for any one IMAP step: imaplib waits forever by default, and a server that stops
                    # answering would hold the mail lock (and an upkeep step) until the add-on restarts


def text_of(msg):
    """The readable text of an email: the plain part, or the HTML part as text."""
    plain, htm = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ct = part.get_content_type()
        try:
            body = part.get_content()
        except Exception:  # noqa: BLE001 - a part in an odd charset
            body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        if ct == "text/plain" and plain is None:
            plain = body
        elif ct == "text/html" and htm is None:
            htm = body
    if plain is None and htm:
        t = re.sub(r"(?is)<(script|style).*?</\1>", "", htm)
        t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", t)
        plain = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = plain or ""
    t = re.split(r"\n-- \n|\n--\s*\n|\nOn .{5,80} wrote:\n|\n-----Original Message-----", t)[0]
    t = "\n".join(ln for ln in t.splitlines() if not ln.startswith(">"))
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:MAX_BODY]


def attachments(msg):
    out = []
    for part in msg.walk():
        fn = part.get_filename()
        if not fn:
            continue
        data = part.get_payload(decode=True) or b""
        out.append((fn, part.get_content_type(), data))
    return out


class Mailbox:
    def __init__(self, app, opts, imap_factory=None):
        self.app = app
        self.host = opts.get("email_host") or ""
        self.user = opts.get("email_user") or ""
        self.password = opts.get("email_password") or ""
        self.folder = opts.get("email_folder") or "INBOX"
        self.senders = {s.strip().lower() for s in (opts.get("email_senders") or []) if s and s.strip()}
        self.minutes = float(opts.get("email_minutes") or 10)
        self.require_verified = opts.get("email_require_verified", True) is not False
        self.authserv = (opts.get("email_authserv_id") or "").strip() or None
        self.imap_factory = imap_factory or (lambda h: imaplib.IMAP4_SSL(h, ssl_context=ssl.create_default_context(),
                                                                          timeout=TIMEOUT))
        self.last = None
        self._running = threading.Lock()

    @property
    def on(self):
        return bool(self.host and self.user and self.password)

    def file(self, raw):
        """One raw email -> what was filed (or why not)."""
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        sender = parseaddr(msg.get("From", ""))[1].lower()
        mid = (msg.get("Message-ID") or "").strip()
        st = self.app.mind.store
        seen = st.kv_get(KV) or []
        if mid and mid in seen:
            return {"skipped": "already read"}
        if sender not in self.senders:
            return {"skipped": f"sender {sender or '?'} not allowed"}
        from .channels import email_verified
        if self.require_verified and not email_verified(msg, sender, self.authserv):
            # anyone can write any From line: only mail the receiving server vouched for (DKIM or SPF) is read
            return {"skipped": f"mail from {sender} didn't pass its sender check (email_require_verified)"}
        lim = getattr(self.app, "limits", None)
        if lim is not None and not lim.allow("email", "all"):
            return {"skipped": "over today's limit of emails", "deferred": True}
        subject = re.sub(r"^\s*(?:(?:fwd?|fw|re)\s*:\s*)+", "", str(msg.get("Subject") or ""), flags=re.I).strip()
        body = text_of(msg)
        try:
            when = parsedate_to_datetime(msg.get("Date")).timestamp()
        except Exception:  # noqa: BLE001
            when = None
        meta = {"from": sender, "message_id": mid[:200], "email_subject": subject[:200]}
        if when:
            meta["valid_from"] = when
        text = body or subject
        out = {"from": sender, "subject": subject, "docs": [], "photos": []}
        if text:
            r = st.remember(text=text, title=subject[:120], source="email", meta=meta)
            out["id"] = r["id"]
        for fn, ct, data in attachments(msg):
            low = fn.lower()
            try:
                if ct.startswith("image/") and out.get("id"):
                    self.app.photos.add(out["id"], data, subject)
                    out["photos"].append(fn)
                elif low.endswith((".pdf", ".txt", ".md", ".html", ".htm")) and getattr(self.app, "docs", None):
                    self.app.docs.add(fn, data)
                    out["docs"].append(fn)
            except Exception as e:  # noqa: BLE001
                log.warning("mail attachment %s: %s", fn, e)
        if mid:
            st.kv_set(KV, (seen + [mid])[-2000:])
        st.log("email", out.get("id"), sender=sender, subject=subject[:120], docs=out["docs"], photos=out["photos"])
        return out

    def run(self):
        """Read new mail (blocking: call in a thread)."""
        if not self.on:
            return {"enabled": False}
        with self._running:         # the upkeep loop and "Check now" run in threads: one at a time, or an email that
            return self._run()      # both see as unseen is filed twice

    def _run(self):
        res = {"read": 0, "filed": 0, "skipped": 0, "at": time.time()}
        box = self.imap_factory(self.host)
        try:
            box.login(self.user, self.password)
            box.select(self.folder)
            typ, data = box.search(None, "UNSEEN")
            for num in (data[0].split() if data and data[0] else [])[:50]:
                # PEEK: fetching RFC822 marks the mail as seen on the server before it is filed, so a mail whose
                # filing failed was never read again. It is marked seen only once filed (or skipped on purpose).
                typ, parts = box.fetch(num, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts or () if isinstance(p, tuple)), None)
                if not raw:
                    continue
                res["read"] += 1
                try:
                    r = self.file(raw)
                except (imaplib.IMAP4.abort, OSError):
                    raise                       # the connection is gone: the rest waits for the next run
                except Exception as e:  # noqa: BLE001 - one bad mail doesn't stop the others; it stays unseen
                    log.warning("mail %s: %s", num, e)
                    res["failed"] = res.get("failed", 0) + 1
                    continue
                res["filed" if "id" in r or r.get("docs") else "skipped"] += 1
                if not r.get("deferred"):
                    box.store(num, "+FLAGS", "\\Seen")
        finally:
            try:
                box.logout()
            except Exception:  # noqa: BLE001
                pass
        self.last = res
        return res
