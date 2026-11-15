"""Minimal diagnostics without entity IDs, names, schedules or location data."""

from .const import VERSION


async def async_get_config_entry_diagnostics(hass, entry):
    runtime = entry.runtime_data
    return {
        "version": VERSION,
        "observe_only": not runtime.repository.control_enabled,
        "zones": [
            {
                "index": index,
                "unit": runtime.mappings[zone_id][2],
                "source": runtime.status.get(zone_id, {}).get("source"),
                "error": runtime.status.get(zone_id, {}).get("error"),
                "has_external_boost_timer": bool(runtime.smart_record(zone_id)["policy"]["timer"]),
            }
            for index, zone_id in enumerate(runtime.zones, 1)
        ],
    }
