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
TELL = re.compile(r"^\s*(?:/?remember|/?note|/?save|remember that)\b[:,]?\s*(?:that\s+)?([^\s:,].*\w.*|\w)$",
                  re.I | re.S)
MAX_LEN = 4000


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

    async def _api(self, method, payload=None, timeout=35):
        import aiohttp
        async with self.app.models.session.post(f"{API}/bot{self.token}/{method}", json=payload or {},
                                                timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            j = await r.json(content_type=None)
        if not j.get("ok"):
            raise RuntimeError(j.get("description") or f"telegram {method} failed")
        return j["result"]

    async def _file(self, file_id):
        import aiohttp
        f = await self._api("getFile", {"file_id": file_id})
        async with self.app.models.session.get(f"{API}/file/bot{self.token}/{f['file_path']}",
                                               timeout=aiohttp.ClientTimeout(total=30)) as r:
            return await r.read()

    async def send(self, chat, text):
        text = text or "(no answer)"
        for i in range(0, len(text), MAX_LEN):
            await self._api("sendMessage", {"chat_id": chat, "text": text[i:i + MAX_LEN]})

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
            m = TELL.match(text)
            note = (m.group(1) if m else text).strip()
            meta = self.app.scope_meta({}, note, who or None)
            r = st.remember(text=note, source="telegram", meta=meta or None)
            data = await self._file(msg["photo"][-1]["file_id"])
            self.app.photos.add(r["id"], data, note)
            return f"Saved with the photo: {note}"
        if not text:
            return None
        m = TELL.match(text)
        if m:
            note = m.group(1).strip()
            meta = self.app.scope_meta({}, note, who or None)
            st.remember(text=note, source="telegram", meta=meta or None)
            return f"Saved: {note}"
        res = await self.app.mind.ask(text, None, conversation_id=f"telegram:{chat}", speaker=who or "")
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
                        log.exception("telegram message")
                        reply = f"Sorry, that failed: {type(e).__name__}"
                    if reply:
                        await self.send(msg["chat"]["id"], reply)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)[:200]
                log.warning("telegram: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(300, backoff * 2)
