"""Home Mind's number settings (voice time limit, days before notes fade, hours between brain tests)."""
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory

from .entity import HomeMindEntity, add_as_offered


async def async_setup_entry(hass, entry, async_add_entities):
    add_as_offered(entry, entry.runtime_data, async_add_entities, "settings", HomeMindNumber, kinds=("number",))


class HomeMindNumber(HomeMindEntity, NumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, key):
        super().__init__(coordinator, "settings", key)
        spec = self.spec or {}
        self._attr_native_min_value = spec.get("min", 0)
        self._attr_native_max_value = spec.get("max", 100)
        self._attr_native_step = spec.get("step", 1)
        self._attr_native_unit_of_measurement = spec.get("unit")

    @property
    def native_value(self):
        spec = self.spec
        return None if spec is None else spec.get("value")

    @property
    def extra_state_attributes(self):
        spec = self.spec or {}
        return {"changed_here": spec.get("overridden", False),
                **({"configuration_tab": spec["configuration"]} if "configuration" in spec else {})}

    async def async_set_native_value(self, value):
        await self.coordinator.async_set(self.key, value)
