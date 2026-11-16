"""Opt-in frost floor and durable, explicitly authorised Off recovery."""

import math
from dataclasses import replace
from datetime import timedelta

from .engine import ValidationError, number, timestamp, utc


def defaults():
    return {
        "frost_enabled": False,
        "frost_target": None,
        "frost_while_off": False,
        "frost_hysteresis": 0.5,
        "frost_max_age_minutes": 60,
    }


def validate_target(value, limits, hardware_limits=None):
    """Normal lower bounds do not apply to an explicitly configured frost floor."""
    value = number(value)
    if value > limits.maximum:
        raise ValidationError("Frost target exceeds the reviewed maximum")
    if limits.step and not math.isclose(
        value / limits.step, round(value / limits.step), abs_tol=1e-6
    ):
        raise ValidationError("Frost target does not match the thermostat increment")
    if hardware_limits is not None:
        hardware_limits.validate(value)


def validate(settings, limits, *, hardware_limits=None, require_hardware=False):
    for key in ("frost_enabled", "frost_while_off"):
        if type(settings[key]) is not bool:
            raise ValidationError("Frost switches must be explicit")
    if settings["frost_target"] is not None:
        validate_target(settings["frost_target"], limits, hardware_limits)
    if settings["frost_enabled"] and settings["frost_target"] is None:
        raise ValidationError("Review a supported frost target first")
    if settings["frost_enabled"] and require_hardware and hardware_limits is None:
        raise ValidationError("An available thermostat is required to verify the frost range")
    if not 0.1 <= number(settings["frost_hysteresis"]) <= 3:
        raise ValidationError("Frost recovery margin must be 0.1 to 3 temperature units")
    if settings["frost_enabled"] and settings["frost_while_off"]:
        validate_target(
            heat_target(settings, hardware_limits.step if hardware_limits else limits.step),
            limits,
            hardware_limits,
        )
    age = settings["frost_max_age_minutes"]
    if type(age) is not int or not 5 <= age <= 180:
        raise ValidationError("Frost reading age must be 5 to 180 minutes")


def heat_target(settings, step):
    """Heat above the release threshold so thermostat hysteresis can recover."""
    step = step or 0.1
    return round(
        (math.ceil((settings["frost_target"] + settings["frost_hysteresis"]) / step - 1e-8) + 1)
        * step,
        6,
    )


def resolve(
    decision,
    record,
    observation,
    room,
    reported_at,
    now,
    *,
    control_enabled,
    settling=False,
    step=None,
    hardware_limits=None,
    reviewed_limits=None,
):
    """Never bypass device/zone/boost guards or lower another owner's target.

    An Off cycle is persisted before commanding heat. Its original Off hold is
    left intact, allowing restart recovery and return to Off after warming.
    Observe-only mode computes proposals but never claims device ownership.
    """
    s = record["settings"]
    cycle = record.get("frost_cycle")
    info = {"state": "disabled", "target": s["frost_target"], "while_off": s["frost_while_off"]}
    deadline = min(decision.next_evaluation, utc(now) + timedelta(minutes=1))
    guarded = decision.source in {
        "disabled",
        "unavailable",
        "unsupported",
        "unsupported_mode",
        "boost_unknown",
        "external_boost",
        "boost_handover",
        "error",
    }
    if guarded:
        info["state"] = "unavailable" if s["frost_enabled"] or cycle else "disabled"
        return decision, info
    if (
        cycle
        and not settling
        and observation.mode == "heat"
        and observation.target != cycle["target"]
    ):
        # A different thermostat target belongs to the user/another controller.
        record["frost_cycle"] = cycle = None
    if decision.source == "boost":
        info["state"] = "waiting" if s["frost_enabled"] else "disabled"
        return decision, info
    if hardware_limits is not None:
        step = hardware_limits.step
    hardware_valid = hardware_limits is not None
    if hardware_valid:
        try:
            validate(s, reviewed_limits or hardware_limits, hardware_limits=hardware_limits)
            if cycle:
                hardware_limits.validate(cycle["target"])
        except ValidationError:
            hardware_valid = False
    valid = hardware_valid and type(room) in (int, float)
    if valid:
        try:
            number(room)
            valid = reported_at is not None and timedelta(0) <= utc(now) - utc(
                reported_at
            ) <= timedelta(minutes=s["frost_max_age_minutes"])
        except (ValidationError, TypeError):
            valid = False
    if cycle and (
        not s["frost_enabled"]
        or not s["frost_while_off"]
        or not valid
        or room >= cycle["release_at"]
    ):
        info["state"] = "returning_off"
        if observation.mode == "off":
            record["frost_cycle"] = None
            info["state"] = "waiting" if s["frost_enabled"] else "disabled"
            return decision, info
        # A writer handles this explicit persisted restoration even without a hold.
        return replace(
            decision,
            target=None,
            source="frost_return_off",
            reason="Frost cycle complete; restoring the original Off state",
            inhibited=True,
            next_evaluation=deadline,
        ), info
    if not s["frost_enabled"]:
        return decision, info
    info["state"] = "waiting" if valid else "unavailable"
    decision = replace(decision, next_evaluation=deadline)
    if not valid:
        return decision, info
    target = s["frost_target"]
    off = decision.source in {"off", "manual_off"}
    if decision.inhibited and not off:
        return decision, info
    if off and not s["frost_while_off"]:
        info["state"] = "paused_off"
        return decision, info
    if decision.target is not None and decision.target >= target and not cycle:
        return decision, info
    if room < target or cycle:
        command_target = cycle["target"] if cycle else heat_target(s, step) if off else target
        info["heating_target"] = command_target
        if off and control_enabled and not cycle:
            record["frost_cycle"] = {
                "target": command_target,
                "release_at": target + s["frost_hysteresis"],
                "since": utc(now).isoformat(),
            }
        info["state"] = "active" if control_enabled else "proposed"
        return replace(
            decision,
            target=max(command_target, decision.target or command_target),
            source="frost",
            reason="Frost minimum temperature" + ("; temporary heating while Off" if off else ""),
            inhibited=False,
        ), info
    return decision, info


def validate_cycle(cycle, limits):
    if cycle is not None:
        if not isinstance(cycle, dict) or set(cycle) != {"target", "release_at", "since"}:
            raise ValidationError("Invalid frost recovery intent")
        validate_target(cycle["target"], limits)
        if not number(cycle["release_at"]) < cycle["target"]:
            raise ValidationError("Invalid frost recovery threshold")
        timestamp(cycle["since"])
