"""Fresh Time-cluster validation. Clock writes are explicit and read back."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .engine import ValidationError

EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
CLOCK_ATTRIBUTES = (
    "time",
    "time_status",
    "time_zone",
    "dst_start",
    "dst_end",
    "dst_shift",
    "local_time",
)


def expected_clock(timezone, now):
    # Use the same timezone-aware ZCL implementation as the current Zigpy Time
    # server, including the ACTIVE or NEXT DST interval and inclusive DstEnd.
    from zigpy.time import TimeSnapshot

    snapshot = TimeSnapshot.compute(ZoneInfo(timezone), now)
    return {key: int(getattr(snapshot, key)) for key in CLOCK_ATTRIBUTES if key != "time_status"}


def validate_clock(values):
    if not isinstance(values, dict) or set(values) != set(CLOCK_ATTRIBUTES):
        raise ValidationError("Complete fresh receiver clock attributes are required")
    result = {}
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError("Invalid receiver clock attribute")
        low, high = (-86400, 86400) if key in {"time_zone", "dst_shift"} else (0, 0xFFFFFFFF)
        if not low <= value <= high or key == "time_status" and value > 15:
            raise ValidationError("Receiver clock attribute is out of range")
        result[key] = int(value)
    return result


def clock_report(values, timezone, now):
    values = validate_clock(values)
    expected = expected_clock(timezone, now)
    utc_error = values["time"] - expected["time"]
    local_error = values["local_time"] - expected["local_time"]
    calendar_match = all(
        values[key] == expected[key] for key in ("time_zone", "dst_start", "dst_end", "dst_shift")
    )
    return {
        "at": now.isoformat(),
        "timezone": timezone,
        "values": values,
        "utc_error_seconds": utc_error,
        "local_error_seconds": local_error,
        "calendar_match": calendar_match,
        "verified": abs(utc_error) <= 30
        and abs(local_error) <= 30
        and calendar_match
        and bool(values["time_status"] & 3),
        "expected": expected,
        "next_dst_start": (EPOCH + timedelta(seconds=expected["dst_start"])).isoformat()
        if expected["dst_start"] != 0xFFFFFFFF
        else None,
    }
