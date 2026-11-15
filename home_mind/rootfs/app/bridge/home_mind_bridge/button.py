"""Home Mind's jobs, started by hand (overnight learning, morning summary, calendar sync, model check, tests...).
They run in the background in the app; each button shows its job's last result."""
from homeassistant.components.button import ButtonEntity

from .entity import HomeMindEntity, add_as_offered


async def async_setup_entry(hass, entry, async_add_entities):
    add_as_offered(entry, entry.runtime_data, async_add_entities, "actions", HomeMindButton)


class HomeMindButton(HomeMindEntity, ButtonEntity):
    def __init__(self, coordinator, key):
        super().__init__(coordinator, "actions", key)

    @property
    def extra_state_attributes(self):
        spec = self.spec or {}
        last = spec.get("last") or {}
        out = {"running": spec.get("running", False)}
        if last:
            out.update({"last_run": last.get("at"), "last_ok": last.get("ok")})
            if last.get("error"):
                out["last_error"] = last["error"]
            if last.get("result") is not None:
                out["last_result"] = last["result"]
        return out

    async def async_press(self):
        await self.coordinator.async_run(self.key)
