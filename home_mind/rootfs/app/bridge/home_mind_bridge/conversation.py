"""Home Mind as a conversation agent: pick "Home Mind" as the agent of a voice assistant (Settings > Voice
assistants). What is said goes to the app's /api/converse and comes back as a stream, so speech can start on the
first sentence; if the answer takes more than 1.5 seconds, the app sends a short acknowledgement first.

Turn on "Prefer handling commands locally" on the same voice assistant: Home Assistant's own commands (lights,
timers, shopping list) are then handled by Home Assistant first, and only the rest reaches Home Mind.
"""
import json
import logging

import aiohttp
from homeassistant.components import conversation
from homeassistant.components.conversation import ChatLog, ConversationEntity, ConversationInput
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import conn as _conn
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
TIMEOUT = 90


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    async_add_entities([HomeMindAgent(hass, entry)])


def body_for(user_input):
    """What the app needs from one turn."""
    ctx = getattr(user_input, "context", None)
    return {"text": user_input.text, "conversation_id": getattr(user_input, "conversation_id", None),
            "user_id": getattr(ctx, "user_id", None), "device_id": getattr(user_input, "device_id", None),
            "language": getattr(user_input, "language", None), "front_end": "assist"}


async def deltas(lines, done):
    """The app's events (lines of JSON) as chat-log deltas: one assistant message, its text in pieces. done
    collects the final event."""
    yield {"role": "assistant"}
    first = True
    async for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if ev.get("type") == "delta" and ev.get("text"):
            yield {"content": ("" if first else " ") + ev["text"]}
            first = False
        elif ev.get("type") == "done":
            done.update(ev)


class HomeMindAgent(ConversationEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supports_streaming = True
    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_agent"
        from homeassistant.helpers.device_registry import DeviceInfo
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, "home_mind")}, name="Home Mind")

    @property
    def supported_languages(self):
        return "*"

    async def _lines(self, body):
        c = await _conn(self.hass)
        s = async_get_clientsession(self.hass)
        async with s.post(c["url"] + "/api/converse", json=body,
                          headers={"X-Home-Mind-Token": c["token"], "Accept": "application/x-ndjson"},
                          timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
            if r.status != 200:
                raise RuntimeError(f"Home Mind answered {r.status}")
            async for line in r.content:
                yield line.decode("utf-8", "replace")

    async def _async_handle_message(self, user_input: ConversationInput, chat_log: ChatLog):
        done = {}
        try:
            async for _ in chat_log.async_add_delta_content_stream(self.entity_id,
                                                                    deltas(self._lines(body_for(user_input)), done)):
                pass
        except Exception as e:  # noqa: BLE001 - the person hears that something went wrong, not silence
            _LOGGER.warning("Home Mind: %s", e)
            if not done:
                chat_log.async_add_assistant_content_without_tools(conversation.AssistantContent(
                    agent_id=self.entity_id, content="Sorry, I can't reach Home Mind right now."))
                done["answer"] = "Sorry, I can't reach Home Mind right now."
        speech = done.get("answer") or ""
        getter = getattr(conversation, "async_get_result_from_chat_log", None)
        if getter is not None:
            result = getter(user_input, chat_log)
        else:
            response = intent.IntentResponse(language=user_input.language)
            response.async_set_speech(speech)
            result = conversation.ConversationResult(response=response, conversation_id=chat_log.conversation_id)
        if done.get("continue"):
            try:
                result.continue_conversation = True     # keep listening: it asked a question
            except AttributeError:
                pass
        return result
