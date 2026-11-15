"""Home Mind readings (memory size, conversation turns waiting, last overnight run, automations it runs, version)."""
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.util import dt as dt_util

from .entity import HomeMindEntity, add_as_offered


async def async_setup_entry(hass, entry, async_add_entities):
    add_as_offered(entry, entry.runtime_data, async_add_entities, "sensors", HomeMindSensor)


class HomeMindSensor(HomeMindEntity, SensorEntity):
    def __init__(self, coordinator, key):
        super().__init__(coordinator, "sensors", key)
        spec = self.spec or {}
        self._timestamp = spec.get("device_class") == "timestamp"
        if self._timestamp:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        elif spec.get("unit"):
            self._attr_native_unit_of_measurement = spec["unit"]
            self._attr_state_class = SensorStateClass.MEASUREMENT
        if key == "version":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        v = (self.spec or {}).get("value")
        if self._timestamp:
            return dt_util.parse_datetime(v) if isinstance(v, str) else None
        return v

    @property
    def extra_state_attributes(self):
        return (self.spec or {}).get("attrs") or None
