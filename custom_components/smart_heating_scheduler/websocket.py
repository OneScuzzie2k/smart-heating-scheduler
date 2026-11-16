"""Versioned card API. Read permissions follow mapped climates; edits are admin-only."""

import math

import probatio as vol
from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components import websocket_api
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .engine import ValidationError
from .repository import RevisionConflict


def loaded_entry(hass, entry_id):
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or getattr(entry, "runtime_data", None) is None:
        raise ValidationError("Scheduler is not loaded")
    if entry.runtime_data._closed:
        raise ValidationError("Scheduler is unloading")
    return entry


def finite(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def dashboard(hass, entry, user):
    runtime = entry.runtime_data
    result = {
        "schema_version": 1,
        "entry_id": entry.entry_id,
        "available": not runtime._closed,
        "observe_only": not runtime.repository.control_enabled,
        "can_edit": user.is_admin,
        "timezone": hass.config.time_zone,
        "generated_at": dt_util.utcnow().isoformat(),
        "zones": [],
    }
    if runtime._closed:
        return result
    for zone_id, zone in runtime.zones.items():
        entity_id, timer_id, unit = runtime.mappings[zone_id]
        if not user.permissions.check_entity(entity_id, POLICY_READ):
            continue
        week, _hold, revision = runtime.repository.read(zone_id, zone.limits)
        status = runtime.status.get(zone_id, {})
        state = hass.states.get(entity_id)
        attrs = state.attributes if state else {}
        smart = runtime.smart_record(zone_id)
        timer_id = smart["policy"]["timer"]
        timer = hass.states.get(timer_id) if timer_id else None
        builtin = smart["boost"]
        observed = runtime._observation(zone_id)
        result["zones"].append(
            {
                "id": zone_id,
                "name": entry.subentries[zone_id].title,
                "climate_entity": entity_id,
                "unit": unit,
                "current_temperature": finite(attrs.get("current_temperature")),
                "device_target": finite(attrs.get("temperature")),
                "hvac_action": attrs.get("hvac_action"),
                "mode": state.state if state else "unavailable",
                "limits": {
                    "minimum": zone.limits.minimum,
                    "maximum": zone.limits.maximum,
                    "step": zone.limits.step,
                },
                "manual_policy": zone.manual_policy,
                "manual_minutes": zone.manual_minutes,
                "enabled": zone.enabled,
                "revision": revision,
                "week": week.to_dict(),
                "proposed_target": finite(status.get("target")),
                "source": status.get("source", "unknown"),
                "reason": status.get("reason", ""),
                "next_evaluation": status["next_evaluation"].isoformat()
                if status.get("next_evaluation")
                else None,
                "error": status.get("error"),
                "smart": smart,
                "insights": {
                    "revision": runtime.insights.read(zone_id, zone.limits)["revision"],
                    "settings": runtime.insights.read(zone_id, zone.limits)["settings"],
                    **runtime.insight_status.get(zone_id, {}),
                },
                "presence": runtime.smart_status.get(zone_id, {}).get("occupancy", "schedule"),
                "presence_raw": runtime.smart_status.get(zone_id, {}).get(
                    "presence_raw", "schedule"
                ),
                "boost": {
                    "owner": smart["policy"]["boost_owner"],
                    "active": bool(builtin or (timer and timer.state in {"active", "paused"})),
                    "until": builtin["until"]
                    if builtin
                    else timer.attributes.get("finishes_at")
                    if timer
                    else None,
                    "can_start": bool(
                        zone.enabled
                        and observed.available
                        and observed.supported
                        and not observed.boost_unknown
                        and smart["policy"]["boost_target"] is not None
                        and (
                            smart["policy"]["boost_owner"] == "builtin"
                            or (
                                smart["policy"]["boost_owner"] == "external"
                                and runtime.repository.control_enabled
                                and smart["policy"]["start_script"]
                                and smart["policy"]["cancel_script"]
                            )
                        )
                        and not smart["external_operation"]
                        and not builtin
                        and not (timer and timer.state != "idle")
                    ),
                    "can_cancel": bool(
                        builtin
                        or (
                            runtime.repository.control_enabled
                            and smart["policy"]["cancel_script"]
                            and timer
                            and timer.state in {"active", "paused"}
                        )
                    ),
                },
            }
        )
    return result


@callback
def async_register_api(hass):
    if hass.data.setdefault(f"{DOMAIN}_websocket_registered", False):
        return
    for command in (
        get_dashboard,
        subscribe_dashboard,
        save_schedule,
        list_schedulers,
        control_action,
    ):
        websocket_api.async_register_command(hass, command)
    hass.data[f"{DOMAIN}_websocket_registered"] = True


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/list"})
@callback
def list_schedulers(hass, connection, msg):
    entries = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if getattr(entry, "runtime_data", None) is None:
            continue
        snapshot = dashboard(hass, entry, connection.user)
        if snapshot["zones"] or connection.user.is_admin:
            entries.append({"entry_id": entry.entry_id, "name": entry.title})
    connection.send_result(msg["id"], entries)


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/dashboard", vol.Required("entry_id"): str}
)
@callback
def get_dashboard(hass, connection, msg):
    try:
        snapshot = dashboard(hass, loaded_entry(hass, msg["entry_id"]), connection.user)
    except ValidationError as err:
        connection.send_error(msg["id"], "not_loaded", str(err))
        return
    connection.send_result(msg["id"], snapshot)


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/subscribe", vol.Required("entry_id"): str}
)
@callback
def subscribe_dashboard(hass, connection, msg):
    try:
        entry = loaded_entry(hass, msg["entry_id"])
    except ValidationError as err:
        connection.send_error(msg["id"], "not_loaded", str(err))
        return

    @callback
    def publish():
        try:
            snapshot = dashboard(hass, entry, connection.user)
        except ValidationError:
            connection.send_event(msg["id"], {"schema_version": 1, "available": False})
        else:
            connection.send_event(msg["id"], snapshot)

    connection.subscriptions[msg["id"]] = entry.runtime_data.subscribe(publish)
    connection.send_result(msg["id"])
    publish()


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/save_schedule",
        vol.Required("entry_id"): str,
        vol.Required("zone_id"): str,
        vol.Required("revision"): vol.All(int, vol.Range(min=1)),
        vol.Required("week"): dict,
    }
)
@websocket_api.async_response
async def save_schedule(hass, connection, msg):
    try:
        entry = loaded_entry(hass, msg["entry_id"])
        runtime = entry.runtime_data
        if msg["zone_id"] not in runtime.zones:
            raise ValidationError("Unknown zone")
        await runtime.async_save_week(msg["zone_id"], msg["week"], msg["revision"])
        snapshot = dashboard(hass, entry, connection.user)
    except RevisionConflict as err:
        connection.send_error(msg["id"], "revision_conflict", str(err))
    except ValidationError as err:
        connection.send_error(msg["id"], "invalid_schedule", str(err))
    except OSError:
        connection.send_error(msg["id"], "save_failed", "Storage could not be saved")
    else:
        connection.send_result(msg["id"], snapshot)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/control",
        vol.Required("entry_id"): str,
        vol.Required("zone_id"): str,
        vol.Required("action"): vol.In(
            [
                "policy",
                "presence",
                "holiday",
                "boost_start",
                "boost_cancel",
                "reconcile",
                "resume",
                "retry",
                "mode",
                "insights",
                "reset_learning",
                "clear_history",
            ]
        ),
        vol.Required("payload"): dict,
    }
)
@websocket_api.async_response
async def control_action(hass, connection, msg):
    try:
        entry = loaded_entry(hass, msg["entry_id"])
        await entry.runtime_data.async_control_action(msg["action"], msg["zone_id"], msg["payload"])
    except (ValidationError, ValueError, TypeError, KeyError) as err:
        connection.send_error(msg["id"], "invalid_control", str(err))
    except HomeAssistantError:
        connection.send_error(
            msg["id"],
            "command_failed",
            "Command failed; check current receiver state before retrying",
        )
    except OSError:
        connection.send_error(
            msg["id"],
            "save_failed",
            "Storage could not be saved; check current state before retrying",
        )
    else:
        connection.send_result(msg["id"], dashboard(hass, entry, connection.user))
