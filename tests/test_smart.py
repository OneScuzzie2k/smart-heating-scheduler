"""Policy priorities, unknown occupancy, deadlines and storage migration."""

import unittest
from copy import deepcopy
from datetime import timedelta
from zoneinfo import ZoneInfo

from custom_components.smart_heating_scheduler import smart
from custom_components.smart_heating_scheduler.engine import Decision, ValidationError, Week
from custom_components.smart_heating_scheduler.repository import Repository
from tests.test_controller import MemoryStore
from tests.test_engine import LIMITS, instant, raw_week

NOW = instant("2026-11-16T09:00:00+00:00")
ZONE = ZoneInfo("Europe/London")


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.data = smart.defaults()
        self.p = self.data["policy"]
        self.p.update(away_target=16, boost_target=21)
        self.p["entities"] = [
            {"entity_id": "person.example", "home_state": "home", "away_state": "not_home"}
        ]
        self.base = Decision(16, "schedule", "Schedule", NOW + timedelta(hours=12), 16, False)

    def test_departure_delay_survives_state_reload_and_return_is_immediate(self):
        effective, _, deadline = smart.occupancy(self.data, {"person.example": "not_home"}, NOW)
        self.assertEqual(effective, "schedule")
        self.assertEqual(deadline, NOW + timedelta(minutes=10))
        loaded = smart.validate(deepcopy(self.data), LIMITS)
        effective, _, _ = smart.occupancy(loaded, {"person.example": "work"}, deadline)
        self.assertEqual(effective, "away")
        self.assertEqual(smart.occupancy(loaded, {"person.example": "home"}, deadline)[0], "home")

    def test_unknown_never_starts_away_and_falls_back_after_grace(self):
        self.assertEqual(smart.occupancy(self.data, {}, NOW)[0], "schedule")
        smart.occupancy(self.data, {"person.example": "home"}, NOW)
        self.assertEqual(smart.occupancy(self.data, {}, NOW)[0], "home")
        self.assertEqual(smart.occupancy(self.data, {}, NOW + timedelta(minutes=11))[0], "schedule")
        self.assertIsNone(self.data["presence"]["candidate"])

    def test_any_all_partial_unknown_and_explicit_binary_mapping(self):
        self.p["entities"].append(
            {"entity_id": "binary_sensor.room", "home_state": "on", "away_state": "off"}
        )
        self.assertEqual(smart.sample_presence(self.p, {"person.example": "home"}), "home")
        self.assertEqual(smart.sample_presence(self.p, {"person.example": "not_home"}), "unknown")
        self.p["home_rule"] = "all"
        self.assertEqual(smart.sample_presence(self.p, {"person.example": "not_home"}), "away")
        self.assertEqual(
            smart.sample_presence(self.p, {"person.example": "home", "binary_sensor.room": "bad"}),
            "unknown",
        )

    def test_manual_presence_expiry_recomputes_current_inputs(self):
        self.data["override"] = {"mode": "away", "until": (NOW + timedelta(minutes=2)).isoformat()}
        self.assertEqual(smart.occupancy(self.data, {"person.example": "home"}, NOW)[0], "away")
        self.assertEqual(
            smart.occupancy(self.data, {"person.example": "home"}, NOW + timedelta(minutes=2))[0],
            "home",
        )
        self.assertIsNone(self.data["override"])

    def test_holiday_boost_manual_off_and_adapter_priorities(self):
        self.data["holiday"] = {
            "start": NOW.isoformat(),
            "end": (NOW + timedelta(days=1)).isoformat(),
            "target": 17,
            "exceptions": False,
            "applied": True,
        }
        self.data["boost"] = {
            "until": (NOW + timedelta(minutes=30)).isoformat(),
            "target": 23,
            "return_off": False,
            "holiday_exception": True,
        }
        decision = smart.resolve_smart(self.base, self.data, "away", NOW, ZONE)
        self.assertEqual((decision.source, decision.target), ("holiday", 17))
        self.data["holiday"]["exceptions"] = True
        self.assertEqual(
            smart.resolve_smart(self.base, self.data, "away", NOW, ZONE).source, "boost"
        )
        self.data["boost"] = None
        off = Decision(None, "manual_off", "Off", self.base.next_evaluation, 16, True)
        self.assertEqual(
            smart.resolve_smart(off, self.data, "away", NOW, ZONE).source, "manual_off"
        )
        self.data["external_operation"] = "uncertain"
        self.assertTrue(smart.resolve_smart(self.base, self.data, "home", NOW, ZONE).inhibited)

    def test_comfort_floor_window_boundary_and_higher_schedule(self):
        self.p["comfort_target"] = 20
        result = smart.resolve_smart(self.base, self.data, "home", NOW, ZONE)
        self.assertEqual(result.target, 20)
        self.assertEqual(result.next_evaluation, instant("2026-11-16T16:30:00+00:00"))
        base = Decision(22, "schedule", "Schedule", self.base.next_evaluation, 22, False)
        self.assertEqual(smart.resolve_smart(base, self.data, "home", NOW, ZONE).target, 22)
        self.assertEqual(smart.resolve_smart(self.base, self.data, "away", NOW, ZONE).target, 16)

    def test_validation_rejects_mixed_owners_invalid_targets_and_naive_dates(self):
        for mutate in (
            lambda d: d["policy"].update(timer="timer.external"),
            lambda d: d["policy"].update(away_target=99),
            lambda d: d.update(override={"mode": "home", "until": "2026-11-16T10:00"}),
            lambda d: d["policy"].update(boost_minutes=True),
            lambda d: d["policy"].update(start_script="climate.wrong"),
        ):
            candidate = deepcopy(self.data)
            mutate(candidate)
            with self.assertRaises(ValidationError):
                smart.validate(candidate, LIMITS)


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_one_upgrades_without_enabling_control_or_losing_week(self):
        store = MemoryStore()
        store.data = {
            "schema": 1,
            "zones": {"zone": {"week": raw_week(), "hold": None, "revision": 4}},
        }
        repo = Repository(store)
        await repo.load()
        self.assertFalse(repo.control_enabled)
        self.assertEqual(repo.read("zone", LIMITS)[2], 4)
        await repo.smart_save("zone", smart.defaults(), LIMITS)
        self.assertEqual(store.data["schema"], 2)
        self.assertEqual(store.data["zones"]["zone"]["week"], raw_week())

    async def test_failed_policy_save_and_control_mode_are_atomic(self):
        store = MemoryStore()
        repo = Repository(store)
        await repo.initialise("zone", Week.parse(raw_week(), LIMITS))
        original = repo.snapshot()
        store.fail = True
        with self.assertRaises(OSError):
            await repo.set_control(True)
        self.assertEqual(repo.snapshot(), original)
        with self.assertRaises(OSError):
            await repo.smart_save("zone", smart.defaults(), LIMITS)
        self.assertEqual(repo.snapshot(), original)
