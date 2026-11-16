"""Cold-room simulation without live sensors or devices."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.smart_heating_scheduler import frost, insights
from custom_components.smart_heating_scheduler.engine import (
    Decision,
    Limits,
    Observation,
    ValidationError,
)

NOW = datetime(2026, 11, 16, tzinfo=UTC)


def setup():
    r = insights.empty_record()
    r["settings"].update(frost_enabled=True, frost_target=7, frost_while_off=True)
    return (
        r,
        Decision(None, "off", "Off", NOW + timedelta(hours=1), 20, True),
        Observation(True, True, "off", 20),
    )


def run(r, d, o, room=6, age=0, enabled=True):
    return frost.resolve(
        d, r, o, room, NOW - timedelta(minutes=age), NOW, control_enabled=enabled, step=0.5
    )


def test_off_opt_in_persists_and_recovers_after_restart_then_restores_off():
    r, d, o = setup()
    result, status = run(r, d, o)
    assert result.target == 8 and not result.inhibited and status["state"] == "active"
    restored = deepcopy(r)
    result, _ = run(
        restored, replace(d, source="manual_off"), replace(o, mode="heat", target=8), 7.2
    )
    assert result.source == "frost" and result.target == 8
    result, _ = run(restored, d, replace(o, mode="heat", target=8), 7.5)
    assert result.source == "frost_return_off" and result.inhibited
    run(restored, d, o, 7.5)
    assert restored["frost_cycle"] is None


def test_off_respected_by_default_and_observe_only_never_claims_cycle():
    r, d, o = setup()
    r["settings"]["frost_while_off"] = False
    assert run(r, d, o)[0] == replace(d, next_evaluation=NOW + timedelta(minutes=1))
    assert r["frost_cycle"] is None
    r["settings"]["frost_while_off"] = True
    assert run(r, d, o, enabled=False)[1]["state"] == "proposed"
    assert r["frost_cycle"] is None


@pytest.mark.parametrize("room,age", [(None, 0), (float("nan"), 0), (6, 61), (6, -1)])
def test_unknown_old_and_future_readings_are_not_cold(room, age):
    r, d, o = setup()
    result, status = run(r, d, o, room, age)
    assert result.inhibited and status["state"] == "unavailable"
    assert r["frost_cycle"] is None


@pytest.mark.parametrize(
    "source",
    [
        "disabled",
        "unavailable",
        "unsupported",
        "unsupported_mode",
        "boost_unknown",
        "external_boost",
        "boost_handover",
        "error",
    ],
)
def test_guards_never_bypassed(source):
    r, d, o = setup()
    guarded = replace(d, source=source)
    assert run(r, guarded, o)[0] == guarded


def test_higher_targets_and_boost_remain_in_charge():
    r, d, o = setup()
    d = replace(d, target=20, source="schedule", inhibited=False)
    assert run(r, d, replace(o, mode="heat"))[0].source == "schedule"
    assert r["frost_cycle"] is None
    run(r, replace(d, target=None, source="off", inhibited=True), o)
    boosted = replace(d, target=24, source="boost")
    assert run(r, boosted, replace(o, mode="heat", target=24))[0] == boosted


def test_disabled_or_lost_reading_returns_owned_cycle_to_off():
    r, d, o = setup()
    run(r, d, o)
    r["settings"]["frost_enabled"] = False
    assert run(r, d, replace(o, mode="heat", target=8))[0].source == "frost_return_off"


def test_unit_aware_limits_are_never_silently_lowered():
    r, _, _ = setup()
    with pytest.raises(ValidationError):
        insights.validate_settings(r["settings"], Limits(16, 30, 0.5))
    r["settings"]["frost_target"] = 45
    insights.validate_settings(r["settings"], Limits(40, 85, 1))
    r["settings"]["frost_target"] = 45.5
    with pytest.raises(ValidationError):
        insights.validate_settings(r["settings"], Limits(40, 85, 1))


def test_off_recovery_target_must_have_headroom_within_reviewed_limits():
    r, _, _ = setup()
    r["settings"]["frost_target"] = 28
    with pytest.raises(ValidationError):
        insights.validate_settings(r["settings"], Limits(5, 28, 0.5))
