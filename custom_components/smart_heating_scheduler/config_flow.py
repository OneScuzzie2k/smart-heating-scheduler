"""UI-only setup, per-zone subentries and preview schedule editing."""

from __future__ import annotations

import json

import probatio as vol
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.config_entries import ConfigFlow, ConfigSubentryFlow
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import DOMAIN
from .engine import Limits, ValidationError, number


def validate_zone(hass, data, existing, exclude=None):
    """Resolve duplicate mappings and hardware constraints without commands."""
    entity_id = data["climate_entity"]
    registry = er.async_get(hass)
    registered = registry.async_get(entity_id)
    for subentry in existing.values():
        if subentry.subentry_id == exclude:
            continue
        other = subentry.data
        if other["climate_entity"] == entity_id or (
            registered and other.get("climate_registry_id") == registered.id
        ):
            raise ValidationError("Thermostat already belongs to another zone")
    state = hass.states.get(entity_id)
    if state is None or state.state in {"unknown", "unavailable"}:
        raise ValidationError("Select an available thermostat to verify its capabilities")
    attrs = state.attributes
    if (
        HVACMode.HEAT not in attrs.get("hvac_modes", [])
        or not int(attrs.get("supported_features", 0)) & ClimateEntityFeature.TARGET_TEMPERATURE
    ):
        raise ValidationError("A single-target heating thermostat is required")
    unit = attrs.get("temperature_unit")
    if unit not in {"°C", "°F"}:
        raise ValidationError("Unsupported temperature unit")
    low, high = number(attrs.get("min_temp")), number(attrs.get("max_temp"))
    step = attrs.get("target_temp_step")
    if step is not None:
        step = number(step)
    limits = Limits(number(data["minimum"]), number(data["maximum"]), step)
    if limits.minimum < low or limits.maximum > high:
        raise ValidationError("Reviewed limits must fit the thermostat's limits")
    # Minimum is a bound, not a step-grid origin (some thermostats report 5.1C).
    hardware = Limits(low, high, step)
    hardware.validate(limits.minimum)
    hardware.validate(limits.maximum)
    baseline = limits.validate(data["baseline_temperature"])
    return {
        **data,
        "minimum": limits.minimum,
        "maximum": limits.maximum,
        "step": step,
        "unit": unit,
        "baseline_temperature": baseline,
        "climate_registry_id": registered.id if registered else None,
    }


def zone_schema(defaults=None):
    defaults = defaults or {}

    def field(name):
        return (
            vol.Required(name, default=defaults[name]) if name in defaults else vol.Required(name)
        )

    return vol.Schema(
        {
            field("name"): selector.TextSelector(),
            field("climate_entity"): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="climate")
            ),
            field("minimum"): selector.NumberSelector(
                selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, step=0.1)
            ),
            field("maximum"): selector.NumberSelector(
                selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, step=0.1)
            ),
            field("baseline_temperature"): selector.NumberSelector(
                selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, step=0.1)
            ),
            vol.Required("enabled", default=defaults.get("enabled", True)): bool,
            vol.Required(
                "manual_policy", default=defaults.get("manual_policy", "next_period")
            ): vol.In(["next_period", "duration", "until_resume"]),
            vol.Required("manual_minutes", default=defaults.get("manual_minutes", 120)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=10080)
            ),
            vol.Optional("boost_timer", default=defaults.get("boost_timer", "")): str,
        }
    )


class SchedulerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="Heating Scheduler Preview", data={})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry):
        return {"zone": ZoneSubentryFlow}


class ZoneSubentryFlow(ConfigSubentryFlow):
    async def async_step_user(self, user_input=None):
        return await self._zone_form("user", user_input)

    async def async_step_reconfigure(self, user_input=None):
        return self.async_show_menu(
            step_id="reconfigure", menu_options=["settings", "schedule", "resume"]
        )

    async def async_step_resume(self, user_input=None):
        if user_input is not None:
            runtime = self._get_entry().runtime_data
            zone = runtime.zones[self._get_reconfigure_subentry().subentry_id]
            try:
                await runtime.controller.resume(zone)
                await runtime.async_evaluate()
            except OSError:
                return self.async_show_form(
                    step_id="resume", data_schema=vol.Schema({}), errors={"base": "save_failed"}
                )
            return self.async_abort(reason="preview_resumed")
        return self.async_show_form(step_id="resume", data_schema=vol.Schema({}))

    async def async_step_settings(self, user_input=None):
        return await self._zone_form("settings", user_input)

    async def _zone_form(self, step_id, user_input):
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry() if step_id == "settings" else None
        defaults = dict(subentry.data) if subentry else {}
        if subentry and getattr(entry, "runtime_data", None):
            # The card policy owns boost mappings after initial setup. Showing
            # its current timer also lets a migrated zone shed a deleted helper.
            defaults["boost_timer"] = entry.runtime_data.smart_record(subentry.subentry_id)[
                "policy"
            ]["timer"]
        if defaults.get("climate_registry_id"):
            resolved = er.async_resolve_entity_id(
                er.async_get(self.hass), defaults["climate_registry_id"]
            )
            if resolved:
                defaults["climate_entity"] = resolved
        errors = {}
        if user_input is not None:
            defaults.update(user_input)
            try:
                if not user_input["name"].strip() or len(user_input["name"]) > 80:
                    raise ValidationError("Name must contain 1-80 characters")
                timer = user_input.get("boost_timer", "")
                if timer and (
                    not timer.startswith("timer.") or self.hass.states.get(timer) is None
                ):
                    raise ValidationError("Boost timer must be an existing timer entity")
                data = validate_zone(
                    self.hass,
                    user_input,
                    entry.subentries,
                    subentry.subentry_id if subentry else None,
                )
                if subentry:
                    # A device or units change must not reuse old numeric schedules.
                    old_identity = (
                        subentry.data.get("climate_registry_id") or subentry.data["climate_entity"]
                    )
                    new_identity = data.get("climate_registry_id") or data["climate_entity"]
                    if old_identity != new_identity or data["unit"] != subentry.data["unit"]:
                        raise ValidationError("Create a new zone when replacing the thermostat")
                    record = entry.runtime_data.repository.read(
                        subentry.subentry_id, Limits(data["minimum"], data["maximum"], data["step"])
                    )
                    if record is None:
                        raise ValidationError("Zone storage is unavailable")
                    smart_record = entry.runtime_data.repository.smart_read(
                        subentry.subentry_id,
                        Limits(data["minimum"], data["maximum"], data["step"]),
                        subentry.data.get("boost_timer", ""),
                    )
                    if data.get("boost_timer", "") != smart_record["policy"]["timer"]:
                        raise ValidationError(
                            "Manage boost ownership and timer mappings in the card Settings"
                        )
                    return self.async_update_reload_and_abort(
                        entry, subentry, data=data, title=data["name"]
                    )
                return self.async_create_entry(
                    title=data["name"],
                    data=data,
                    unique_id=data["climate_registry_id"] or data["climate_entity"],
                )
            except (ValidationError, ValueError, TypeError):
                errors["base"] = "invalid_zone"
        return self.async_show_form(
            step_id=step_id, data_schema=zone_schema(defaults), errors=errors
        )

    async def async_step_schedule(self, user_input=None):
        entry, subentry = self._get_entry(), self._get_reconfigure_subentry()
        runtime = entry.runtime_data
        zone = runtime.zones[subentry.subentry_id]
        week, _hold, revision = runtime.repository.read(zone.id, zone.limits)
        # Retain the initial revision across form redisplays/concurrent edits.
        if not hasattr(self, "_schedule_revision"):
            self._schedule_revision = revision
        errors = {}
        text = json.dumps(week.to_dict(), indent=2)
        if user_input is not None:
            text = user_input["week_json"]
            try:
                await runtime.async_save_week(zone.id, json.loads(text), self._schedule_revision)
            except (ValidationError, ValueError, TypeError):
                errors["base"] = "invalid_schedule"
            except OSError:
                errors["base"] = "save_failed"
            else:
                return self.async_abort(reason="schedule_saved")
        return self.async_show_form(
            step_id="schedule",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("week_json", default=text): selector.TextSelector(
                        selector.TextSelectorConfig(multiline=True)
                    )
                }
            ),
        )
