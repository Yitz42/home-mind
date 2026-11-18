"""Ask and tell from a messaging app (#52): a Telegram bot that answers from your memory and files what you tell it.

Option `telegram_token` (from @BotFather) and `telegram_chats`: the chats allowed to use it, as "chat id: name"
(the name is a person from the `people` option, so their personal notes stay theirs). Send /start to the bot to
learn your chat id; any other chat is ignored.

- "remember ...", "note: ..." or "save ..." files a note; anything else is a question.
- A photo with a caption files the caption as a note with the photo attached.
- Each chat is its own conversation, so follow-ups work ("and the shed?").
"""
import asyncio
import logging
import re

log = logging.getLogger("homemind.telegram")
API = "https://api.telegram.org"
TELL = re.compile(r"^\s*(?:/?remember|/?note|/?save|remember that)\b[:,]?\s*(?:that\s+)?(?:to self\b[:,]?\s*)?"
                  r"([^\s:,].*\w.*|\w)$", re.I | re.S)
# "Remember where I put the drill?" is a question about memory, not something to file
ASKS = re.compile(r"\?\s*$|^\s*/?remember\s+(?:what|where|which|who|whose|whether|how|if)\b", re.I)


def tell(text):
    """The note to file when a message tells something ("remember the bins go out Tuesday"), else None."""
    m = TELL.match(text or "")
    if not m or (ASKS.search(text) and not text.lstrip().startswith("/")):
        return None
    return m.group(1).strip()
MAX_LEN = 4000      # Telegram allows 4096, counted in UTF-16 units (an emoji is two)


def _chunks(text, size=MAX_LEN):
    """text in pieces of at most size UTF-16 units, so a long answer full of emoji isn't refused as too long."""
    out, cur, n = [], [], 0
    for ch in text:
        w = 2 if ord(ch) > 0xFFFF else 1
        if n + w > size:
            out.append("".join(cur))
            cur, n = [], 0
        cur.append(ch)
        n += w
    if cur:
        out.append("".join(cur))
    return out


class TelegramError(RuntimeError):
    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


def parse_chats(spec):
    """["12345: Dana", "678"] or {"12345": "Dana"} -> {12345: "Dana", 678: ""}"""
    out = {}
    if isinstance(spec, dict):
        spec = [f"{k}: {v}" for k, v in spec.items()]
    for s in spec or []:
        m = re.match(r"\s*(-?\d+)\s*(?::\s*(.*))?$", str(s))
        if m:
            out[int(m.group(1))] = (m.group(2) or "").strip()
    return out


class Telegram:
    def __init__(self, app, token, chats):
        self.app, self.token, self.chats = app, token or "", parse_chats(chats)
        self.offset = 0
        self.last_error = None

    @property
    def on(self):
        return bool(self.token)

    def _clean(self, e):
        """An error as text without the bot token (aiohttp errors can carry the request URL, which holds it)."""
        t = f"{type(e).__name__}: {e}" if isinstance(e, BaseException) else str(e)
        return t.replace(self.token, "<token>") if self.token else t

    async def _api(self, method, payload=None, timeout=35):
        import aiohttp
        async with self.app.models.session.post(f"{API}/bot{self.token}/{method}", json=payload or {},
                                                timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            j = await r.json(content_type=None)
        if not j.get("ok"):
            raise TelegramError(j.get("description") or f"telegram {method} failed",
                                (j.get("parameters") or {}).get("retry_after"))
        return j["result"]

    async def _file(self, file_id):
        import aiohttp
        f = await self._api("getFile", {"file_id": file_id})
        async with self.app.models.session.get(f"{API}/file/bot{self.token}/{f['file_path']}",
                                               timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                raise TelegramError(f"photo download: HTTP {r.status}")
            return await r.read()

    async def send(self, chat, text):
        text = text or "(no answer)"
        for part in _chunks(text):
            try:
                await self._api("sendMessage", {"chat_id": chat, "text": part})
            except TelegramError as e:      # 429 "Too Many Requests: retry after N": wait once, then try again
                if not e.retry_after or float(e.retry_after) > 60:
                    raise
                await asyncio.sleep(float(e.retry_after))
                await self._api("sendMessage", {"chat_id": chat, "text": part})

    async def handle(self, msg):
        """One incoming message -> the reply text (or None to stay silent)."""
        chat = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if chat is None:
            return None
        if text.startswith("/start"):
            if chat in self.chats:
                return "Hi! Ask me anything you've told Home Mind, or say \"remember ...\" to add a note."
            return f"Your chat id is {chat}. Add it to the telegram_chats option to use this bot."
        if chat not in self.chats:
            log.info("telegram: ignored a message from chat %s (not in telegram_chats)", chat)
            return None
        who = self.chats[chat]
        st = self.app.mind.store
        if msg.get("photo"):
            if not text:
                return "Add a caption so I know what the photo is of."
            note = tell(text) or text.strip()
            data = await self._file(msg["photo"][-1]["file_id"])     # first: a failed download files nothing
            meta = self.app.scope_meta({}, note, who or None)
            r = st.remember(text=note, source="telegram", meta=meta or None)
            self.app.photos.add(r["id"], data, note)
            return f"Saved with the photo: {note}"
        if not text:
            return None
        note = tell(text)
        if note:
            meta = self.app.scope_meta({}, note, who or None)
            st.remember(text=note, source="telegram", meta=meta or None)
            return f"Saved: {note}"
        res = await self.app.mind.ask(text, None, conversation_id=f"telegram:{chat}", speaker=who or "")
        styled = getattr(self.app, "styled", None)
        if styled is not None and getattr(self.app, "persona", None) is not None:
            res = await styled(res, text, who or None)          # the persona's voice, as in the Ask box
        return res.get("answer") or "I don't have anything on that."

    async def run_forever(self):
        backoff = 5
        while True:
            try:
                ups = await self._api("getUpdates", {"offset": self.offset, "timeout": 25,
                                                     "allowed_updates": ["message"]}, timeout=35)
                backoff = 5
                for u in ups:
                    self.offset = max(self.offset, u["update_id"] + 1)
                    msg = u.get("message")
                    if not msg:
                        continue
                    try:
                        reply = await self.handle(msg)
                    except Exception as e:  # noqa: BLE001
                        log.warning("telegram message: %s", self._clean(e),
                                    exc_info=self.token not in f"{e!s}{e!r}")
                        reply = f"Sorry, that failed: {type(e).__name__}"
                    if reply:
                        await self.send(msg["chat"]["id"], reply)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = self._clean(e)[:200]        # shown by /api/connections
                log.warning("telegram: %s", self.last_error)
                await asyncio.sleep(backoff)
                backoff = min(300, backoff * 2)
