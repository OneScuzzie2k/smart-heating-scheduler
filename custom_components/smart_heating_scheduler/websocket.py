"""Versioned card API. Entity permissions gate boost; configuration edits are admin-only."""

import math

import probatio as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.components import websocket_api
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, Unauthorized
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


def can_boost(runtime, user, zone_id):
    """Authorize household boost without delegating configuration or arbitrary services."""
    if zone_id not in runtime.zones:
        return False
    climate = runtime.mappings[zone_id][0]
    if not all(
        user.permissions.check_entity(climate, policy) for policy in (POLICY_READ, POLICY_CONTROL)
    ):
        return False
    policy = runtime.smart_record(zone_id)["policy"]
    if policy["boost_owner"] == "external":
        # Script/helper calls execute in the runtime, so check their permissions here.
        return all(
            user.permissions.check_entity(entity_id, POLICY_CONTROL)
            for entity_id in (
                policy["start_script"],
                policy["cancel_script"],
                policy["duration_helper"],
            )
            if entity_id
        )
    return True


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
        frost_limits = runtime._frost_limits(zone)
        result["zones"].append(
            {
                "id": zone_id,
                "name": entry.subentries[zone_id].title,
                "climate_entity": entity_id,
                "can_boost": can_boost(runtime, user, zone_id),
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
                "frost_limits": {
                    "minimum": frost_limits.minimum,
                    "maximum": frost_limits.maximum,
                    "step": frost_limits.step,
                }
                if frost_limits
                else None,
                "receiver": runtime.receivers.snapshot(zone_id),
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
        receiver_action,
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
                "restore_configuration",
            ]
        ),
        vol.Required("payload"): dict,
    }
)
@websocket_api.async_response
async def control_action(hass, connection, msg):
    boost = msg["action"] in {"boost_start", "boost_cancel"}
    if not boost and not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Administrator access required")
        return
    try:
        entry = loaded_entry(hass, msg["entry_id"])

        def authorize():
            # Run inside the control lock: an adapter may change while waiting.
            if boost and not can_boost(entry.runtime_data, connection.user, msg["zone_id"]):
                raise Unauthorized()
            if not boost and not connection.user.is_admin:
                raise Unauthorized()

        await entry.runtime_data.async_control_action(
            msg["action"], msg["zone_id"], msg["payload"], authorize=authorize
        )
    except Unauthorized:
        connection.send_error(
            msg["id"],
            "unauthorized",
            "Control permission required for this zone and its boost adapter",
        )
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


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/receiver",
        vol.Required("entry_id"): str,
        vol.Required("zone_id"): str,
        vol.Required("action"): vol.In(
            [
                "read",
                "prepare",
                "prepare_restore",
                "write",
                "inspect",
                "prepare_test",
                "start_test",
                "check_test",
                "restore_test",
            ]
        ),
        vol.Required("payload"): dict,
    }
)
@websocket_api.async_response
async def receiver_action(hass, connection, msg):
    try:
        entry = loaded_entry(hass, msg["entry_id"])
        runtime = entry.runtime_data
        zone_id, action, payload = msg["zone_id"], msg["action"], msg["payload"]
        allowed = {
            "read": set(),
            "prepare": {"revision"},
            "prepare_restore": {"revision", "backup_id"},
            "write": {"plan_id", "acknowledgement"},
            "inspect": set(),
            "prepare_test": {"revision"},
            "start_test": {"test_id", "acknowledgement"},
            "check_test": set(),
            "restore_test": set(),
        }
        if set(payload) != allowed[action]:
            raise ValidationError("Invalid receiver request fields")

        def authorize():
            if not connection.user.is_admin:
                raise Unauthorized()
            if zone_id not in runtime.mappings:
                raise ValidationError("Unknown zone")
            entity_id = runtime.mappings[zone_id][0]
            policies = (
                (POLICY_READ, POLICY_CONTROL)
                if action
                in {
                    "write",
                    "start_test",
                    "check_test",
                    "restore_test",
                }
                else (POLICY_READ,)
            )
            if not all(connection.user.permissions.check_entity(entity_id, p) for p in policies):
                raise Unauthorized()

        result = await runtime.receivers.execute(zone_id, action, payload, authorize)
    except Unauthorized:
        connection.send_error(
            msg["id"],
            "unauthorized",
            "Receiver operation requires administrator entity permissions",
        )
    except RevisionConflict as err:
        connection.send_error(msg["id"], "receiver_conflict", str(err))
    except (ValidationError, ValueError, KeyError, TypeError) as err:
        connection.send_error(msg["id"], "receiver_error", str(err))
    except OSError:
        connection.send_error(
            msg["id"], "save_failed", "Receiver backup could not be saved; do not switch ownership"
        )
    except (HomeAssistantError, TimeoutError):
        connection.send_error(
            msg["id"], "receiver_error", "Receiver operation failed; read back before retrying"
        )
    else:
        connection.send_result(msg["id"], result)
