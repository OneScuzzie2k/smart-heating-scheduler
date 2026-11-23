"""HA-independent receiver schedule conversion and readback validation.

Only heating is supported. Wire temperatures are integer centi-degrees Celsius;
the generic scheduler's unit, reviewed limits and full-day coverage stay intact.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from .const import DAYS
from .engine import Limits, ValidationError, Week, number

DAY_BITS = {day: 1 << ((index + 1) % 7) for index, day in enumerate(DAYS)}
MAX_TRANSITIONS = 6


def _integer(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValidationError(f"Invalid receiver {label}")
    return int(value)


def validate_native(schedule, *, complete=True):
    """Validate native days without repairing or converting the Hive Off sentinel.

    Readback may contain empty days and non-midnight starts. That is different
    from an app Week; it must remain visible instead of inventing missing periods.
    """
    if not isinstance(schedule, dict) or set(schedule) - set(DAYS):
        raise ValidationError("Invalid receiver schedule days")
    if complete and set(schedule) != set(DAYS):
        raise ValidationError("A complete seven-day receiver read is required")
    result = {}
    for day in DAYS:
        if day not in schedule:
            continue
        periods = schedule[day]
        if not isinstance(periods, list) or len(periods) > MAX_TRANSITIONS:
            raise ValidationError("Receiver supports at most six transitions per day")
        seen = {}
        for period in periods:
            if not isinstance(period, dict) or set(period) != {"minute", "centi"}:
                raise ValidationError("Invalid receiver transition fields")
            minute = _integer(period["minute"], 0, 1439, "transition time")
            centi = _integer(period["centi"], 0, 3200, "heating setpoint")
            # Hive pads its six slots by repeating entries. Disagreeing repeats
            # are ambiguous and must not be silently normalised into a backup.
            if minute in seen and seen[minute] != centi:
                raise ValidationError("Conflicting repeated receiver transition")
            seen[minute] = centi
        result[day] = [{"minute": m, "centi": t} for m, t in sorted(seen.items())]
    return result


def from_week(raw, limits: Limits, unit: str):
    if unit != "°C":
        raise ValidationError("This receiver adapter requires a Celsius climate mapping")
    week = Week.parse(raw, limits).to_dict()
    result = {}
    for day, periods in week.items():
        if len(periods) > MAX_TRANSITIONS:
            raise ValidationError(f"{day}: receiver supports at most six periods")
        result[day] = []
        for period in periods:
            value = number(period["temperature"])
            if not 5 <= value <= 32 or abs(value * 2 - round(value * 2)) > 1e-6:
                raise ValidationError("Receiver targets require 5–32°C in half-degree steps")
            hour, minute = map(int, period["start"].split(":"))
            result[day].append({"minute": hour * 60 + minute, "centi": round(value * 100)})
    return validate_native(result)


def to_week(schedule, limits: Limits):
    """Return an importable app week only if no semantics would be lost."""
    schedule = validate_native(schedule)
    raw = {}
    for day, periods in schedule.items():
        if not periods or periods[0]["minute"] != 0:
            raise ValidationError(
                "Receiver overnight carry-over requires an explicit import review"
            )
        raw[day] = [
            {
                "start": f"{p['minute'] // 60:02d}:{p['minute'] % 60:02d}",
                "temperature": p["centi"] / 100,
                "label": "Receiver period",
            }
            for p in periods
        ]
    return Week.parse(raw, limits).to_dict()


def digest(schedule):
    canonical = validate_native(schedule)
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def changes(before, after):
    before, after = validate_native(before), validate_native(after)
    return [day for day in DAYS if before[day] != after[day]]


def command(day, periods):
    clean = validate_native({day: periods}, complete=False)[day]
    if not clean:
        raise ValidationError("Empty receiver days cannot be written safely")
    # OTR1 shares the last written sequence count across its entire week.
    # Always preserve six wire slots. Repeating the FINAL identical time and
    # target adds no new boundary; readback canonicalisation removes repeats.
    padded = [*clean, *[clean[-1]] * (MAX_TRANSITIONS - len(clean))]
    return {
        "num_transitions_for_sequence": MAX_TRANSITIONS,
        "day_of_week_for_sequence": DAY_BITS[day],
        "mode_for_sequence": 1,
        "values": [value for period in padded for value in (period["minute"], period["centi"])],
    }


class ScheduleReplies:
    """Correlate one request's responses by TSN, days, mode and advertised count."""

    def __init__(self, tsn):
        self.tsn = tsn
        self.days = {}
        self.counts = {}
        self.error = None

    def accept(self, tsn, command_id, args):
        if tsn != self.tsn or command_id != 0:
            return
        try:
            mode = _integer(args.mode_for_sequence, 0, 3, "sequence mode")
            if mode != 1:
                raise ValidationError("Receiver did not return a heating-only sequence")
            bits = _integer(args.day_of_week_for_sequence, 1, 255, "day bitmap")
            count = _integer(args.num_transitions_for_sequence, 0, MAX_TRANSITIONS, "count")
            values = list(args.values)
            if len(values) != count * 2:
                raise ValidationError("Receiver transition count does not match its payload")
            periods = [
                {
                    "minute": _integer(values[i], 0, 1439, "transition time"),
                    "centi": _integer(values[i + 1], 0, 3200, "heating setpoint"),
                }
                for i in range(0, len(values), 2)
            ]
            for day, bit in DAY_BITS.items():
                if not bits & bit:
                    continue
                clean = validate_native({day: periods}, complete=False)[day]
                if day in self.days and self.days[day] != clean:
                    raise ValidationError("Receiver sent conflicting replies for one day")
                self.days[day] = deepcopy(clean)
                self.counts[day] = count
        except (AttributeError, TypeError, ValueError) as err:
            self.error = str(err)

    @property
    def complete(self):
        return self.error is None and set(self.days) == set(DAYS)
