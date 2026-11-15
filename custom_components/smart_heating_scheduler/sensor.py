"""Proposed targets and reasons, with explicit control-mode attributes."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import callback


async def async_setup_entry(hass, entry, async_add_entities):
    for zone_id in entry.runtime_data.zones:
        subentry = entry.subentries[zone_id]
        async_add_entities(
            [
                PreviewSensor(entry, subentry, "target", "Proposed target"),
                PreviewSensor(entry, subentry, "source", "Control source"),
                PreviewSensor(entry, subentry, "next_evaluation", "Next evaluation"),
            ],
            config_subentry_id=zone_id,
        )


class PreviewSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(self, entry, subentry, key, name):
        self.runtime = entry.runtime_data
        self.zone_id, self.key = subentry.subentry_id, key
        self._attr_unique_id = f"{entry.entry_id}_{self.zone_id}_{key}"
        self._attr_name = f"{subentry.title} {name}"
        self._attr_icon = "mdi:calendar-search"
        if key == "target":
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = subentry.data["unit"]
        elif key == "next_evaluation":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    async def async_added_to_hass(self):
        self.async_on_remove(self.runtime.subscribe(self._updated))

    @callback
    def _updated(self):
        self.async_write_ha_state()

    @property
    def native_value(self):
        return self.runtime.status.get(self.zone_id, {}).get(self.key)

    @property
    def extra_state_attributes(self):
        status = self.runtime.status.get(self.zone_id, {})
        return {
            "observe_only": not self.runtime.repository.control_enabled,
            "reason": status.get("reason"),
            "schedule_target": status.get("schedule_target"),
            "writes_inhibited": not self.runtime.repository.control_enabled
            or status.get("inhibited", True),
            "rule_inhibited": status.get("inhibited", True),
            "error": status.get("error"),
        }
