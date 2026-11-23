"""UI-only setup, per-zone subentries and preview schedule editing."""

from __future__ import annotations

import json
import math

import probatio as vol
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.config_entries import ConfigFlow, ConfigSubentryFlow
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from . import frost, insights, smart
from .const import DOMAIN
from .engine import Limits, ValidationError, Week, number


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
            return self.async_create_entry(title="Heating Scheduler", data={})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry):
        return {"zone": ZoneSubentryFlow}


class ZoneSubentryFlow(ConfigSubentryFlow):
    async def async_step_user(self, user_input=None):
        return self.async_show_menu(step_id="user", menu_options=["guided", "advanced"])

    async def async_step_advanced(self, user_input=None):
        return await self._zone_form("advanced", user_input)

    async def async_step_guided(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                eid = user_input["climate_entity"]
                state = self.hass.states.get(eid)
                attrs = state.attributes if state else {}
                low, high = number(attrs.get("min_temp")), number(attrs.get("max_temp"))
                step = attrs.get("target_temp_step")
                if type(step) in (int, float) and step > 0:
                    low = math.ceil(low / step - 1e-8) * step
                    high = math.floor(high / step + 1e-8) * step
                baseline = attrs.get("temperature")
                if type(baseline) not in (int, float):
                    baseline = low
                baseline = max(low, min(high, baseline))
                if type(step) in (int, float) and step > 0:
                    baseline = round(baseline / step) * step
                candidate = {
                    "name": user_input["name"].strip(),
                    "climate_entity": eid,
                    "minimum": low,
                    "maximum": high,
                    "baseline_temperature": baseline,
                    "enabled": False,
                    "manual_policy": "next_period",
                    "manual_minutes": 120,
                    "boost_timer": "",
                }
                if not candidate["name"] or len(candidate["name"]) > 80:
                    raise ValidationError("Choose a zone name")
                self._guided = validate_zone(self.hass, candidate, self._get_entry().subentries)
                return await self.async_step_limits()
            except (ValidationError, TypeError, ValueError):
                errors["base"] = "invalid_zone"
        return self.async_show_form(
            step_id="guided",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("name"): str,
                    vol.Required("climate_entity"): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="climate")
                    ),
                }
            ),
        )

    async def async_step_limits(self, user_input=None):
        errors = {}
        d = self._guided
        if user_input is not None:
            if user_input.get("back"):
                return await self.async_step_guided()
            try:
                candidate = {
                    **d,
                    **{k: user_input[k] for k in ("minimum", "maximum", "baseline_temperature")},
                }
                self._guided = validate_zone(self.hass, candidate, self._get_entry().subentries)
                return await self.async_step_options()
            except (ValidationError, TypeError, ValueError):
                errors["base"] = "invalid_zone"
        return self.async_show_form(
            step_id="limits",
            errors=errors,
            description_placeholders={"unit": d["unit"]},
            data_schema=vol.Schema(
                {
                    **{
                        vol.Required(k, default=d[k]): vol.Coerce(float)
                        for k in ("minimum", "maximum", "baseline_temperature")
                    },
                    vol.Optional("back", default=False): bool,
                }
            ),
        )

    async def async_step_options(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._guided_options = {k: v for k, v in user_input.items() if k != "back"}
            if user_input.get("back"):
                return await self.async_step_limits()
            try:
                d = self._guided
                limits = Limits(d["minimum"], d["maximum"], d["step"])
                features = insights.defaults()
                features.update(
                    {
                        k: user_input[k]
                        for k in ("preheat_enabled", "frost_enabled", "frost_while_off")
                    }
                )
                features["frost_target"] = (
                    user_input["frost_target"] if features["frost_enabled"] else None
                )
                d["initial_features"] = insights.validate_settings(features, limits)
                attrs = self.hass.states.get(d["climate_entity"]).attributes
                frost.validate(
                    features,
                    limits,
                    hardware_limits=Limits(
                        attrs["min_temp"], min(attrs["max_temp"], limits.maximum), limits.step
                    ),
                    require_hardware=True,
                )
                policy = smart.defaults()["policy"]
                policy["boost_owner"] = "builtin" if user_input["boost_enabled"] else "none"
                policy["boost_target"] = (
                    limits.validate(user_input["boost_target"])
                    if user_input["boost_enabled"]
                    else None
                )
                eid = user_input.get("person", "")
                if eid:
                    if not eid.startswith("person.") or self.hass.states.get(eid) is None:
                        raise ValidationError("Choose an existing person")
                    policy["entities"] = [
                        {"entity_id": eid, "home_state": "home", "away_state": "not_home"}
                    ]
                    policy["comfort_target"] = limits.validate(user_input["occupied_target"])
                checked = smart.defaults()
                checked["policy"] = policy
                smart.validate(checked, limits)
                d["initial_policy"] = policy
                d["initial_week"] = Week.baseline(d["baseline_temperature"], limits).to_dict()
                return await self.async_step_review()
            except (ValidationError, ValueError, TypeError, KeyError):
                errors["base"] = "invalid_zone"
        d = self._guided
        result = self.async_show_form(
            step_id="options",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("boost_enabled", default=True): bool,
                    vol.Required("boost_target", default=d["baseline_temperature"]): vol.Coerce(
                        float
                    ),
                    vol.Optional("person", default=""): str,
                    vol.Required("occupied_target", default=d["baseline_temperature"]): vol.Coerce(
                        float
                    ),
                    vol.Required("preheat_enabled", default=False): bool,
                    vol.Required("frost_enabled", default=False): bool,
                    vol.Required("frost_target", default=d["minimum"]): vol.Coerce(float),
                    vol.Required("frost_while_off", default=False): bool,
                    vol.Optional("back", default=False): bool,
                }
            ),
        )

        result["data_schema"] = self.add_suggested_values_to_schema(
            result["data_schema"], getattr(self, "_guided_options", {})
        )
        return result

    async def async_step_review(self, user_input=None):
        d = self._guided
        if user_input is not None:
            if user_input.get("back"):
                return await self.async_step_options()
            # Re-check capabilities and duplicates at the final action.
            try:
                d = validate_zone(self.hass, d, self._get_entry().subentries)
            except (ValidationError, ValueError, TypeError):
                return await self.async_step_guided()
            return self.async_create_entry(
                title=d["name"], data=d, unique_id=d["climate_registry_id"] or d["climate_entity"]
            )
        p, f = d["initial_policy"], d["initial_features"]
        occupancy = (
            "Mon–Fri 08:30–16:30 minimum " + str(p["comfort_target"]) if p["entities"] else "off"
        )
        summary = (
            f"{d['name']} → {d['climate_entity']}; "
            f"{d['minimum']}–{d['maximum']} {d['unit']}; "
            f"all seven days {d['baseline_temperature']} {d['unit']}. "
            f"Boost: {p['boost_owner']}; occupancy: {occupancy}; "
            f"preheating: {f['preheat_enabled']}; "
            f"frost: {f['frost_target'] if f['frost_enabled'] else 'off'}, "
            f"while Off: {f['frost_while_off']}."
        )
        return self.async_show_form(
            step_id="review",
            description_placeholders={"summary": summary},
            data_schema=vol.Schema({vol.Optional("back", default=False): bool}),
        )

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
                    if data["enabled"] and entry.runtime_data.receivers.qualification.active(
                        subentry.subentry_id
                    ):
                        raise ValidationError(
                            "Restore the active receiver timer test before enabling this zone"
                        )
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
                    entry.runtime_data.insights.read(
                        subentry.subentry_id,
                        Limits(data["minimum"], data["maximum"], data["step"]),
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
