"""Profile precedence/DST, measured learning and preheating safety contracts."""

import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.smart_heating_scheduler import insights
from custom_components.smart_heating_scheduler.engine import Decision, Limits, ValidationError, Week

LIMITS = Limits(16, 28, 0.5)
TZ = ZoneInfo("Europe/London")
NOW = datetime(2026, 11, 16, 6, tzinfo=UTC)


def week():
    return Week.parse(
        {
            day: [
                {"start": "00:00", "temperature": 16},
                {"start": "06:30", "temperature": 20},
                {"start": "08:30", "temperature": 16},
            ]
            for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        },
        LIMITS,
    )


def trained():
    record = insights.empty_record()
    record["settings"]["preheat_enabled"] = True
    record["model"] = {"rate": 2, "samples": 12, "minutes": 60, "updated": NOW.isoformat()}
    return record


def decision():
    return Decision(16, "schedule", "Setback", NOW + timedelta(minutes=30), 16, False)


def test_calendar_first_match_overrides_workday_for_entire_day_and_end_is_exclusive():
    s = insights.defaults()
    s["profiles"] = {
        "Day off": [{"start": "00:00", "temperature": 19}],
        "Workday": [{"start": "00:00", "temperature": 18}],
    }
    s["workday_profile"] = "Workday"
    s["calendar_rules"] = [
        {"entity_id": "calendar.test", "match": "OFF", "profile": "Day off"},
        {"entity_id": "calendar.test", "match": "", "profile": "Workday"},
    ]
    events = {
        "calendar.test": [
            {
                "start": NOW + timedelta(hours=3),
                "end": NOW + timedelta(hours=4),
                "summary": "Day off",
            }
        ]
    }
    assert insights.day_profile(s, NOW.date(), TZ, True, events) == ("Day off", "calendar")
    events["calendar.test"][0]["end"] = NOW.replace(hour=0)
    assert insights.day_profile(s, NOW.date(), TZ, True, events) == ("Workday", "workday")
    assert insights.day_profile(s, NOW.date(), TZ, None, {}) == ("", "weekly")


def test_effective_week_keeps_saved_week_independent_and_checks_tomorrow():
    original = week().to_dict()
    s = insights.defaults()
    s["profiles"] = {"Off": [{"start": "00:00", "temperature": 19}]}
    s["non_workday_profile"] = "Off"
    result, days = insights.effective_week(week(), s, NOW, TZ, {"2026-11-17": False}, {})
    assert result["mon"] == original["mon"]
    assert result["tue"][0]["temperature"] == 19
    assert days[1]["profile"] == "Off"
    assert week().to_dict() == original


@pytest.mark.parametrize(
    "change",
    [
        {"profiles": {"Bad": [{"start": "06:00", "temperature": 20}]}},
        {"profiles": {"Bad": [{"start": "00:00", "temperature": 30}]}},
        {"workday_profile": "Missing"},
        {"preheat_max_minutes": True},
        {"preheat_max_minutes": 181},
        {"preheat_margin_minutes": -1},
        {"calendar_rules": [{"entity_id": "script.bad", "match": "", "profile": "Missing"}]},
    ],
)
def test_settings_reject_invalid_or_unreviewed_controls(change):
    with pytest.raises(ValidationError):
        insights.validate_settings({**insights.defaults(), **change}, LIMITS)


def test_learning_requires_reported_uninterrupted_heating_and_ignores_downtime():
    r = insights.empty_record()
    insights.observe(r, NOW, 18, 20, "heating", "schedule", "Morning")
    insights.observe(r, NOW + timedelta(minutes=1), 18.1, 20, "idle", "schedule", "Morning")
    insights.observe(r, NOW + timedelta(minutes=5), 18.2, 20, "heating", "schedule", "Morning")
    assert r["model"]["samples"] == 0
    insights.observe(r, NOW + timedelta(minutes=10), 18.4, 20, "heating", "schedule", "Morning")
    assert r["model"]["samples"] == 1
    insights.observe(r, NOW + timedelta(hours=2), 19, 20, "heating", "schedule", "Morning")
    assert r["model"]["samples"] == 1
    assert len(r["decisions"]) == 1


def test_learning_threshold_and_stale_model_disable_early_commands():
    r = trained()
    r["model"]["samples"] = 11
    assert insights.preheat(decision(), r, week(), NOW, TZ, 18)[0].target == 16
    r["model"]["samples"] = 12
    r["model"]["updated"] = (NOW - timedelta(days=15)).isoformat()
    assert insights.preheat(decision(), r, week(), NOW, TZ, 18)[1]["state"] == "learning"


def test_preheat_starts_at_measured_deadline_and_holds_until_boundary_without_oscillation():
    r = trained()
    early = NOW - timedelta(minutes=45)
    d, info = insights.preheat(decision(), r, week(), early, TZ, 18)
    assert d.source == "schedule"
    assert info["starts_at"] == "2026-11-16T05:25:00+00:00"
    d, info = insights.preheat(decision(), r, week(), NOW, TZ, 18)
    assert d.source == "preheat" and d.target == 20
    restart = deepcopy(r)
    # Reaching target early should not immediately restore the setback.
    d, info = insights.preheat(decision(), restart, week(), NOW + timedelta(minutes=5), TZ, 20)
    assert d.target == 20 and info["ready_at"] == "2026-11-16T06:30:00+00:00"
    after = NOW + timedelta(minutes=30)
    d, info = insights.preheat(
        replace(decision(), target=20, schedule_target=20), restart, week(), after, TZ, 20
    )
    assert d.source == "schedule" and restart["preheat"] is None


@pytest.mark.parametrize(
    "source",
    [
        "manual_off",
        "off",
        "manual",
        "holiday",
        "boost",
        "away",
        "occupied_comfort",
        "unavailable",
        "external_boost",
    ],
)
def test_preheat_never_overrides_other_owners(source):
    r = trained()
    d = replace(decision(), source=source)
    assert insights.preheat(d, r, week(), NOW, TZ, 18)[0] == d
    assert insights.preheat(decision(), r, week(), NOW, TZ, 18, allowed=False)[0].target == 16


def test_maximum_lead_and_unknown_temperature_are_respected():
    r = trained()
    r["settings"]["preheat_max_minutes"] = 15
    assert insights.preheat(decision(), r, week(), NOW, TZ, 16)[0].source == "schedule"
    assert (
        insights.preheat(decision(), trained(), week(), NOW, TZ, None)[1]["state"]
        == "no_temperature"
    )


def test_history_is_bounded_pruned_and_excludes_calendar_titles():
    r = insights.empty_record()
    r["samples"] = [
        {"at": (NOW - timedelta(days=8)).isoformat(), "room": 18, "target": 20, "action": "idle"}
    ]
    insights.observe(r, NOW, 18, 20, "heating", "schedule", "Workday: Morning")
    assert len(r["samples"]) == 1
    r["settings"]["history_enabled"] = False
    insights.observe(r, NOW + timedelta(minutes=5), 18.2, 20, "heating", "schedule", "Morning")
    assert len(r["samples"]) == 1 and r["decisions"] == []


def test_dst_calendar_day_uses_local_midnight_not_24_hours():
    s = insights.defaults()
    s["profiles"] = {"Sunday": [{"start": "00:00", "temperature": 20}]}
    s["calendar_rules"] = [{"entity_id": "calendar.test", "match": "", "profile": "Sunday"}]
    now = datetime(2026, 10, 25, 23, 30, tzinfo=UTC)
    events = {"calendar.test": [{"start": now, "end": now + timedelta(minutes=15), "summary": ""}]}
    assert insights.day_profile(s, now.date(), TZ, None, events) == ("Sunday", "calendar")


def test_fahrenheit_learning_and_missing_measurement_cancel_active_preheat():
    r = insights.empty_record()
    insights.observe(r, NOW, 65, 70, "heating", "schedule", "Morning", unit="°F")
    insights.observe(
        r, NOW + timedelta(minutes=5), 65.5, 70, "heating", "schedule", "Morning", unit="°F"
    )
    assert r["model"]["rate"] == 6
    r = trained()
    insights.preheat(decision(), r, week(), NOW, TZ, 18)
    assert r["preheat"] is not None
    d, state = insights.preheat(decision(), r, week(), NOW + timedelta(minutes=1), TZ, None)
    assert d.source == "schedule" and state["state"] == "no_temperature"
    assert r["preheat"] is None


def test_decision_target_is_separate_from_reported_device_sample():
    r = insights.empty_record()
    insights.observe(r, NOW, 18, 16, "idle", "boost", "Timed boost", decision_target=23)
    assert r["samples"][0]["target"] == 16
    assert r["decisions"][0]["target"] == 23


class InsightsPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_commit_and_reload_do_not_change_active_settings(self):
        from tests.test_controller import MemoryStore

        store = MemoryStore()
        repository = insights.InsightsRepository(store)
        r = repository.read("zone", LIMITS)
        await repository.save("zone", r)
        r["settings"]["preheat_enabled"] = True
        store.fail = True
        with self.assertRaises(OSError):
            await repository.save("zone", r)
        self.assertFalse(repository.read("zone", LIMITS)["settings"]["preheat_enabled"])
        restored = insights.InsightsRepository(store)
        await restored.load()
        self.assertFalse(restored.read("zone", LIMITS)["settings"]["preheat_enabled"])
