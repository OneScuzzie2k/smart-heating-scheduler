"""Behavioral scheduling tests; no HA instance or real thermostat involved."""

from copy import deepcopy
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.smart_heating_scheduler.const import DAYS
from custom_components.smart_heating_scheduler.engine import (
    Hold,
    Limits,
    Observation,
    ValidationError,
    Week,
    resolve,
    timestamp,
)

LIMITS = Limits(16, 28, 0.5)
LONDON = ZoneInfo("Europe/London")
HEAT = Observation(True, True, "heat", 20)


def instant(value):
    return timestamp(value)


def raw_week():
    return {
        day: [
            {"start": "00:00", "temperature": 16},
            {"start": "06:30", "temperature": 20},
            {"start": "08:30", "temperature": 16},
            {"start": "16:30", "temperature": 21},
            {"start": "22:00", "temperature": 16},
        ]
        for day in DAYS
    }


@pytest.mark.parametrize(
    ("at", "target", "next_at"),
    [
        ("2026-11-16T06:29:59+00:00", 16, "2026-11-16T06:30:00+00:00"),
        ("2026-11-16T06:30:00+00:00", 20, "2026-11-16T08:30:00+00:00"),
        ("2026-11-22T23:59:00+00:00", 16, "2026-11-23T00:00:00+00:00"),
    ],
)
def test_boundaries_and_week_wrap(at, target, next_at):
    result = resolve(Week.parse(raw_week(), LIMITS), LIMITS, instant(at), LONDON, HEAT)
    assert result.target == target
    assert result.next_evaluation == instant(next_at)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda w: w.pop("sun"),
        lambda w: w["mon"].pop(0),
        lambda w: w["mon"].append({"start": "06:30", "temperature": 20}),
        lambda w: w["mon"].append({"start": "24:00", "temperature": 20}),
        lambda w: w["mon"][0].update(temperature=15),
        lambda w: w["mon"][0].update(temperature=20.1),
        lambda w: w["mon"][0].update(temperature=float("nan")),
        lambda w: w["mon"][0].update(temperature=True),
        lambda w: w["mon"][0].update(temperature="20"),
        lambda w: w["mon"][0].update(end="12:00"),
        lambda w: w["mon"][0].update(label="x" * 81),
    ],
)
def test_invalid_schedule_rejected(mutation):
    raw = raw_week()
    mutation(raw)
    with pytest.raises(ValidationError):
        Week.parse(raw, LIMITS)


def test_copy_is_independent_and_roundtrips():
    raw = raw_week()
    raw["sun"] = deepcopy(raw["mon"])
    raw["sun"][1]["temperature"] = 22
    week = Week.parse(raw, LIMITS)
    assert week.days[0][1].temperature == 20
    assert week.days[6][1].temperature == 22
    assert Week.parse(week.to_dict(), LIMITS) == week


def test_spring_gap_uses_current_period_at_clock_jump():
    raw = {
        day: [{"start": "00:00", "temperature": 16}, {"start": "01:30", "temperature": 21}]
        for day in DAYS
    }
    week = Week.parse(raw, LIMITS)
    before = instant("2026-03-29T00:59:00+00:00")
    assert week.next_boundary(before, LONDON) == instant("2026-03-29T01:00:00+00:00")
    assert week.current(instant("2026-03-29T01:00:00+00:00"), LONDON).temperature == 21


def test_fall_fold_reenters_earlier_period_without_explicit_boundary():
    raw = {
        day: [{"start": "00:00", "temperature": 16}, {"start": "01:30", "temperature": 21}]
        for day in DAYS
    }
    week = Week.parse(raw, LIMITS)
    before = instant("2026-10-25T00:45:00+00:00")
    assert week.current(before, LONDON).temperature == 21
    assert week.next_boundary(before, LONDON) == instant("2026-10-25T01:00:00+00:00")
    assert week.current(instant("2026-10-25T01:00:00+00:00"), LONDON).temperature == 16
    assert week.next_boundary(instant("2026-10-25T01:00:00+00:00"), LONDON) == instant(
        "2026-10-25T01:30:00+00:00"
    )


def test_elapsed_hold_deadline_works_across_fold():
    hold = Hold("manual", 23, instant("2026-10-25T01:15:00+00:00"))
    week = Week.parse(raw_week(), LIMITS)
    assert (
        resolve(week, LIMITS, instant("2026-10-25T00:45:00+00:00"), LONDON, HEAT, hold).source
        == "manual"
    )
    assert (
        resolve(week, LIMITS, instant("2026-10-25T01:15:00+00:00"), LONDON, HEAT, hold).source
        == "schedule"
    )


@pytest.mark.parametrize(
    ("observation", "source"),
    [
        (Observation(False, True, None, None), "unavailable"),
        (Observation(True, False, "heat", 20), "unsupported"),
        (Observation(True, True, "off", 20), "off"),
        (Observation(True, True, "auto", 20), "unsupported_mode"),
        (Observation(True, True, "heat", 30, True), "external_boost"),
        (Observation(True, True, "heat", 20, boost_unknown=True), "boost_unknown"),
    ],
)
def test_guards_prevent_schedule_ownership(observation, source):
    result = resolve(
        Week.parse(raw_week(), LIMITS),
        LIMITS,
        instant("2026-11-16T07:00:00+00:00"),
        LONDON,
        observation,
    )
    assert result.source == source
    assert result.inhibited


def test_no_naive_timestamps_or_unsupported_storage_holds():
    with pytest.raises(ValidationError):
        Week.parse(raw_week(), LIMITS).current(datetime(2026, 1, 1), LONDON)
    with pytest.raises(ValidationError):
        Hold("holiday", 16, datetime.now(UTC))


def test_fahrenheit_has_no_celsius_defaults():
    limits = Limits(60, 80, 1)
    week = Week.baseline(68, limits)
    assert week.current(datetime(2026, 1, 1, tzinfo=UTC), ZoneInfo("UTC")).temperature == 68


def test_off_state_cannot_be_overridden_by_stale_manual_target():
    week = Week.parse(raw_week(), LIMITS)
    result = resolve(
        week,
        LIMITS,
        instant("2026-11-16T07:00:00+00:00"),
        LONDON,
        Observation(True, True, "off", 23),
        Hold("manual", 23, None),
    )
    assert result.target is None
    assert result.source == "off"


def test_capability_minimum_is_not_a_step_grid_origin():
    assert Limits(5.1, 30, 0.5).validate(16) == 16
