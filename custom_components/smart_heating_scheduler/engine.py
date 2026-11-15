"""HA-independent validation and deterministic local-time scheduling.

All instants are aware. Compare instants in UTC: Python comparisons between
datetimes sharing a ZoneInfo can otherwise ignore the repeated-hour fold.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .const import DAYS


class ValidationError(ValueError):
    """Invalid user input or stored data; never silently repair it."""


def number(value: object) -> float:
    """Accept finite JSON numbers, excluding booleans and numeric strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("Expected a numeric temperature")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError("Temperature must be finite")
    return result


def utc(instant: datetime) -> datetime:
    """Require an actual timezone, not a naive local timestamp."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValidationError("Timestamp requires a timezone")
    return instant.astimezone(UTC)


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValidationError("Timestamp must be an ISO string")
    try:
        return utc(datetime.fromisoformat(value))
    except ValueError as err:
        raise ValidationError("Invalid timestamp") from err


@dataclass(frozen=True)
class Limits:
    """Application limits in the selected climate entity's displayed unit."""

    minimum: float
    maximum: float
    step: float | None = None

    def __post_init__(self) -> None:
        low, high = number(self.minimum), number(self.maximum)
        if low >= high:
            raise ValidationError("Minimum must be below maximum")
        if self.step is not None and number(self.step) <= 0:
            raise ValidationError("Step must be positive")

    def validate(self, value: object) -> float:
        result = number(value)
        if not self.minimum <= result <= self.maximum:
            raise ValidationError("Target outside reviewed limits")
        if self.step is not None:
            steps = result / self.step
            if not math.isclose(steps, round(steps), abs_tol=1e-6):
                raise ValidationError("Target does not match the configured increment")
        return result


@dataclass(frozen=True)
class Period:
    start: int
    temperature: float
    label: str = ""

    def to_dict(self) -> dict:
        return {
            "start": f"{self.start // 60:02}:{self.start % 60:02}",
            "temperature": self.temperature,
            "label": self.label,
        }


@dataclass(frozen=True)
class Week:
    days: tuple[tuple[Period, ...], ...]

    @classmethod
    def parse(cls, data: object, limits: Limits) -> Week:
        if not isinstance(data, dict) or set(data) != set(DAYS):
            raise ValidationError("Schedule needs exactly mon through sun")
        result = []
        for day in DAYS:
            raw = data[day]
            if not isinstance(raw, list) or not 1 <= len(raw) <= 96:
                raise ValidationError("Each day needs 1 to 96 periods")
            periods = []
            for item in raw:
                if (
                    not isinstance(item, dict)
                    or set(item) - {"start", "temperature", "label"}
                    or not {"start", "temperature"} <= set(item)
                ):
                    raise ValidationError("Invalid period fields")
                start = item["start"]
                if not isinstance(start, str) or not re.fullmatch(
                    r"(?:[01]\d|2[0-3]):[0-5]\d", start
                ):
                    raise ValidationError("Start must be HH:MM from 00:00 to 23:59")
                minute = int(start[:2]) * 60 + int(start[3:])
                label = item.get("label", "")
                if not isinstance(label, str) or len(label) > 80:
                    raise ValidationError("Label must be at most 80 characters")
                if periods and minute <= periods[-1].start:
                    raise ValidationError("Periods must be ordered with unique start times")
                periods.append(Period(minute, limits.validate(item["temperature"]), label))
            if periods[0].start != 0:
                raise ValidationError("Every day requires a 00:00 baseline")
            result.append(tuple(periods))
        return cls(tuple(result))

    @classmethod
    def baseline(cls, value: float, limits: Limits) -> Week:
        return cls.parse({day: [{"start": "00:00", "temperature": value}] for day in DAYS}, limits)

    def to_dict(self) -> dict:
        return {day: [p.to_dict() for p in self.days[index]] for index, day in enumerate(DAYS)}

    def current(self, now: datetime, zone: ZoneInfo) -> Period:
        local = utc(now).astimezone(zone)
        minute = local.hour * 60 + local.minute
        return next(p for p in reversed(self.days[local.weekday()]) if p.start <= minute)

    def next_boundary(self, now: datetime, zone: ZoneInfo) -> datetime:
        """Next period start OR UTC-offset transition, including fold rewind.

        A spring gap uses the period at the first valid clock time. A fall
        rewind can re-enter an earlier period even without a start at 01:00.
        Search UTC minutes only around possible offset transitions; no runtime
        polling is involved. Include midnight even when targets are equal.
        """
        instant = utc(now)
        local_date = instant.astimezone(zone).date()
        candidates: set[datetime] = set()
        for offset in range(9):
            date = local_date + timedelta(days=offset)
            for period in self.days[date.weekday()]:
                naive = datetime.combine(date, time(period.start // 60, period.start % 60))
                for fold in (0, 1):
                    candidate = naive.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                    # Round-trip rejects nonexistent spring-gap wall times.
                    if (
                        candidate.astimezone(zone).replace(tzinfo=None) == naive
                        and candidate > instant
                    ):
                        candidates.add(candidate)
        stop = min(candidates)
        cursor = instant.replace(second=0, microsecond=0) + timedelta(minutes=1)
        previous_offset = instant.astimezone(zone).utcoffset()
        # The next midnight is always a candidate, bounding this to <=25 hours.
        while cursor < stop:
            offset = cursor.astimezone(zone).utcoffset()
            if offset != previous_offset:
                return cursor
            previous_offset = offset
            cursor += timedelta(minutes=1)
        return stop


@dataclass(frozen=True)
class Hold:
    kind: str
    target: float | None
    expires_at: datetime | None

    def __post_init__(self) -> None:
        if self.kind not in {"manual", "off"}:
            raise ValidationError("Unsupported Phase 1 hold")
        if self.kind == "manual" and self.target is None:
            raise ValidationError("Manual hold needs a target")
        if self.target is not None:
            number(self.target)
        if self.expires_at is not None:
            utc(self.expires_at)

    def active(self, now: datetime) -> bool:
        return self.expires_at is None or utc(self.expires_at) > utc(now)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "target": self.target,
            "expires_at": utc(self.expires_at).isoformat() if self.expires_at else None,
        }

    @classmethod
    def parse(cls, data: object, limits: Limits) -> Hold:
        if not isinstance(data, dict) or set(data) != {"kind", "target", "expires_at"}:
            raise ValidationError("Invalid hold record")
        target = None if data["target"] is None else limits.validate(data["target"])
        return cls(
            data["kind"],
            target,
            timestamp(data["expires_at"]) if data["expires_at"] is not None else None,
        )


@dataclass(frozen=True)
class Observation:
    available: bool
    supported: bool
    mode: str | None
    target: float | None
    external_boost: bool = False
    boost_until: datetime | None = None
    boost_unknown: bool = False


@dataclass(frozen=True)
class Decision:
    target: float | None
    source: str
    reason: str
    next_evaluation: datetime
    schedule_target: float
    inhibited: bool


def resolve(
    week: Week,
    limits: Limits,
    now: datetime,
    zone: ZoneInfo,
    observation: Observation,
    hold: Hold | None = None,
    *,
    enabled: bool = True,
) -> Decision:
    """Resolve Phase 1 schedule/manual/external-boost guards without device I/O."""
    period = week.current(now, zone)
    deadline = week.next_boundary(now, zone)
    active_hold = hold if hold and hold.active(now) else None
    for expiry in (
        active_hold.expires_at if active_hold else None,
        observation.boost_until if observation.external_boost else None,
    ):
        if expiry is not None and utc(expiry) > utc(now):
            deadline = min(deadline, utc(expiry))

    def result(target, source, reason, inhibited=True):
        return Decision(target, source, reason, deadline, period.temperature, inhibited)

    if not enabled:
        return result(None, "disabled", "Zone scheduling is disabled")
    if not observation.available:
        return result(None, "unavailable", "Thermostat is unavailable; no commands permitted")
    if not observation.supported:
        return result(None, "unsupported", "Thermostat capabilities or limits need review")
    if observation.boost_unknown:
        return result(None, "boost_unknown", "External boost state is unknown")
    if observation.external_boost:
        return result(observation.target, "external_boost", "Existing boost owns this zone")
    if active_hold and active_hold.kind == "off":
        return result(None, "manual_off", "Off hold remains until its configured resume")
    if observation.mode == "off":
        return result(None, "off", "Thermostat is off; explicit resume is required")
    if active_hold:
        limits.validate(active_hold.target)
        return result(active_hold.target, "manual", "User temperature hold is active")
    if observation.mode != "heat":
        return result(None, "unsupported_mode", "Current HVAC mode is outside heating control")
    return result(
        limits.validate(period.temperature),
        "schedule",
        period.label or "Following the current weekly period",
        inhibited=False,
    )
