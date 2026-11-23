"""Heating-only native mode validation and conservative timer-test preflight."""

from datetime import UTC, timedelta

from .const import DAYS
from .engine import ValidationError
from .native_schedule import _integer, validate_native

STATE_ATTRIBUTES = (
    "system_mode",
    "temp_setpoint_hold",
    "temp_setpoint_hold_duration",
    "occupied_heating_setpoint",
    "local_temperature",
)
CONTROL_KEYS = ("system", "hold", "duration", "target")


def receiver_state(values):
    """Require fresh supported attributes; do not guess from HA's heat/off state."""
    try:
        system = _integer(values[STATE_ATTRIBUTES[0]], 0, 9, "system mode")
        hold = _integer(values[STATE_ATTRIBUTES[1]], 0, 1, "hold")
        duration = _integer(values[STATE_ATTRIBUTES[2]], 0, 65535, "duration")
        target = _integer(values[STATE_ATTRIBUTES[3]], 0, 3200, "target")
        room = _integer(values[STATE_ATTRIBUTES[4]], -5000, 6000, "room temperature")
    except (KeyError, TypeError) as err:
        raise ValidationError("A complete fresh receiver mode read is required") from err
    mode = (
        "off"
        if system == 0
        else "boost"
        if system == 5
        else ("manual" if hold else "schedule")
        if system == 4
        else "unknown"
    )
    if mode == "unknown" or (mode == "boost" and hold != 1):
        raise ValidationError("Receiver mode is not a reviewed heating state")
    return {
        "system": system,
        "hold": hold,
        "duration": duration,
        "target": target,
        "room": room,
        "mode": mode,
    }


def validate_state(state):
    if not isinstance(state, dict) or set(state) != {
        "system",
        "hold",
        "duration",
        "target",
        "room",
        "mode",
    }:
        raise ValidationError("Invalid saved receiver mode state")
    parsed = receiver_state(
        dict(zip(STATE_ATTRIBUTES, (state[k] for k in (*CONTROL_KEYS, "room")), strict=True))
    )
    if parsed != state:
        raise ValidationError("Saved receiver mode disagrees with its attributes")
    return parsed


def same_control(left, right):
    return all(left[key] == right[key] for key in CONTROL_KEYS)


def manual_attributes(original):
    state = validate_state(original)
    if state["mode"] != "manual" or state["duration"] not in {0, 65535}:
        raise ValidationError("Timer qualification starts from an indefinite Manual hold only")
    _target(state["target"])
    return dict(zip(STATE_ATTRIBUTES[:4], (state[k] for k in CONTROL_KEYS), strict=True))


def _target(target):
    _integer(target, 500, 3200, "heating target")
    if target % 50:
        raise ValidationError("Receiver targets use half-degree steps")


def boost_attributes(target):
    _target(target)
    return {
        "system_mode": 5,
        "temp_setpoint_hold": 1,
        "temp_setpoint_hold_duration": 2,
        "occupied_heating_setpoint": target,
    }


def control_attributes(attributes):
    """Only the three internal qualification operations can reach the radio."""
    if not isinstance(attributes, dict) or any(
        type(value) is not int for value in attributes.values()
    ):
        raise ValidationError("Invalid internal receiver mode write")
    if attributes == {"system_mode": 4, "temp_setpoint_hold": 0}:
        return dict(attributes)
    if set(attributes) != set(STATE_ATTRIBUTES[:4]):
        raise ValidationError("Invalid internal receiver mode write")
    _target(attributes["occupied_heating_setpoint"])
    if attributes["temp_setpoint_hold"] != 1 or not (
        attributes["system_mode"] == 5
        and attributes["temp_setpoint_hold_duration"] == 2
        or attributes["system_mode"] == 4
        and attributes["temp_setpoint_hold_duration"] in {0, 65535}
    ):
        raise ValidationError("Only a bounded test boost or exact Manual restoration is supported")
    return dict(attributes)


def current_target(schedule, local_now):
    """Native days may carry over midnight and may contain Off markers."""
    schedule = validate_native(schedule)
    for offset in range(8):
        date = local_now - timedelta(days=offset)
        minute = local_now.hour * 60 + local_now.minute if offset == 0 else 1440
        eligible = [p for p in schedule[DAYS[date.weekday()]] if p["minute"] <= minute]
        if eligible:
            return eligible[-1]["centi"]
    raise ValidationError("Receiver timetable has no usable transition")


def test_preflight(state, schedule, limits, local_now):
    manual_attributes(state)
    limits.validate(state["target"] / 100)
    if state["room"] < state["target"] + 100:
        raise ValidationError(
            "Timer test requires a fresh room reading at least 1°C above its unchanged target"
        )
    # Do not enter Schedule mode near a warmer transition. Include the grace /
    # restoration interval and real UTC minutes through a DST clock change.
    for minutes in range(6):
        instant = (local_now.astimezone(UTC) + timedelta(minutes=minutes)).astimezone(
            local_now.tzinfo
        )
        if current_target(schedule, instant) > state["target"]:
            raise ValidationError(
                "A warmer native period is too close; run the timer test during setback"
            )
