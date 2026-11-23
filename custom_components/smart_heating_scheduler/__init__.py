"""Smart Heating Scheduler. New installations start in observe-only mode."""

from __future__ import annotations


async def async_setup_entry(hass, entry):
    """Set up the runtime and restore its explicitly selected control mode."""
    from homeassistant.const import Platform

    from .runtime import Runtime, async_register_services
    from .websocket import async_register_api

    runtime = Runtime(hass, entry)
    try:
        await runtime.async_setup()
        entry.runtime_data = runtime
        async_register_services(hass)
        async_register_api(hass)
        await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
        entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    except BaseException:
        runtime.close()
        raise
    return True


async def _async_reload_entry(hass, entry):
    """Pick up added/removed zone subentries as well as entry settings."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    from homeassistant.const import Platform

    if await hass.config_entries.async_unload_platforms(entry, [Platform.SENSOR]):
        entry.runtime_data.close()
        return True
    return False


async def async_remove_entry(hass, entry):
    """Remove only this integration's stored preview data on explicit removal."""
    from homeassistant.helpers.storage import Store

    from .const import DOMAIN, STORAGE_VERSION

    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.insights").async_remove()

    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.receivers").async_remove()
