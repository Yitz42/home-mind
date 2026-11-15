"""Shared by every Home Mind entity: one device, names and icons from the app, and entities added as the app offers
them (so a control added in a later version of the app shows up without changing the bridge)."""
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class HomeMindEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, group, key):
        super().__init__(coordinator)
        self.group, self.key = group, key
        self._attr_unique_id = f"home_mind_{group}_{key}"
        spec = self.spec or {}
        self._attr_name = spec.get("label") or key.replace("_", " ").capitalize()
        version = (((coordinator.data or {}).get("sensors") or {}).get("version") or {}).get("value")
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, "home_mind")}, name="Home Mind",
                                            manufacturer="Home Mind", model="Home Assistant app", sw_version=version)

    @property
    def spec(self):
        return ((self.coordinator.data or {}).get(self.group) or {}).get(self.key)

    @property
    def available(self):
        return super().available and self.spec is not None

    @property
    def icon(self):
        return (self.spec or {}).get("icon")


def add_as_offered(entry, coordinator, async_add_entities, group, make, kinds=None):
    """Add an entity for each control in `group` (of one of `kinds`), now and whenever the app offers a new one."""
    known = set()

    @callback
    def _add():
        new = []
        for key, spec in ((coordinator.data or {}).get(group) or {}).items():
            if key in known or (kinds and spec.get("kind") not in kinds):
                continue
            known.add(key)
            new.append(make(coordinator, key))
        if new:
            async_add_entities(new)

    _add()
    entry.async_on_unload(coordinator.async_add_listener(_add))
