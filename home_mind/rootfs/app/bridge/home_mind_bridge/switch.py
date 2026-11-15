"""Home Mind's on/off settings (comfort brain, watching the house, overnight learning, morning summary...)."""
from homeassistant.components.switch import SwitchEntity

from .entity import HomeMindEntity, add_as_offered


async def async_setup_entry(hass, entry, async_add_entities):
    add_as_offered(entry, entry.runtime_data, async_add_entities, "settings", HomeMindSwitch, kinds=("switch",))


class HomeMindSwitch(HomeMindEntity, SwitchEntity):
    def __init__(self, coordinator, key):
        super().__init__(coordinator, "settings", key)

    @property
    def is_on(self):
        spec = self.spec
        return None if spec is None else bool(spec.get("value"))

    @property
    def extra_state_attributes(self):
        spec = self.spec or {}
        return {"changed_here": spec.get("overridden", False),
                **({"configuration_tab": spec["configuration"]} if "configuration" in spec else {})}

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_set(self.key, True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_set(self.key, False)
