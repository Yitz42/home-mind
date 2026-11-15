"""Home Mind's daily times (morning summary, overnight learning)."""
from datetime import time as dtime

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory

from .entity import HomeMindEntity, add_as_offered


async def async_setup_entry(hass, entry, async_add_entities):
    add_as_offered(entry, entry.runtime_data, async_add_entities, "settings", HomeMindTime, kinds=("time",))


class HomeMindTime(HomeMindEntity, TimeEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, key):
        super().__init__(coordinator, "settings", key)

    @property
    def native_value(self):
        v = (self.spec or {}).get("value")
        try:
            h, m = (int(x) for x in str(v).split(":")[:2])
            return dtime(h, m)
        except (TypeError, ValueError):
            return None

    async def async_set_value(self, value):
        await self.coordinator.async_set(self.key, value.strftime("%H:%M"))
