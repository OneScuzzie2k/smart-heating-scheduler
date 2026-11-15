"""Persistence failures, concurrency, restart and manual/boost ownership."""

import asyncio
import unittest
from copy import deepcopy

from custom_components.smart_heating_scheduler.controller import PreviewController, Zone
from custom_components.smart_heating_scheduler.engine import (
    Hold,
    Limits,
    Observation,
    ValidationError,
    Week,
)
from custom_components.smart_heating_scheduler.repository import Repository, RevisionConflict
from tests.test_engine import HEAT, LIMITS, instant, raw_week


class MemoryStore:
    def __init__(self):
        self.data = None
        self.fail = False
        self.writes = 0

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data):
        if self.fail:
            raise OSError("Simulated disk failure")
        await asyncio.sleep(0)  # Allow simultaneous editor tasks to contend.
        self.data = deepcopy(data)
        self.writes += 1


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryStore()
        self.repo = Repository(self.store)
        await self.repo.load()
        self.zone = Zone("zone_a", LIMITS)
        await self.repo.initialise(self.zone.id, Week.parse(raw_week(), LIMITS))
        self.controller = PreviewController(self.repo, "Europe/London")
        self.now = instant("2026-11-16T07:00:00+00:00")

    async def test_failed_save_does_not_publish_new_schedule(self):
        before = self.repo.snapshot()
        self.store.fail = True
        raw = raw_week()
        raw["mon"][1]["temperature"] = 22
        with self.assertRaises(OSError):
            await self.repo.save_week(self.zone.id, raw, LIMITS, 1)
        self.assertEqual(before, self.repo.snapshot())

    async def test_two_editors_cannot_overwrite_each_other(self):
        results = await asyncio.gather(
            self.repo.save_week(self.zone.id, raw_week(), LIMITS, 1),
            self.repo.save_week(self.zone.id, raw_week(), LIMITS, 1),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, RevisionConflict) for result in results), 1)
        self.assertEqual(self.repo.read(self.zone.id, LIMITS)[2], 2)

    async def test_startup_is_not_manual_change_and_repeated_evaluation_is_idempotent(self):
        before = self.store.writes
        result = await self.controller.evaluate(self.zone, HEAT, self.now)
        for _ in range(5):
            self.assertEqual(result, await self.controller.evaluate(self.zone, HEAT, self.now))
        self.assertEqual(self.store.writes, before)
        self.assertEqual(len(self.controller.history), 1)
        self.assertEqual(result.source, "schedule")

    async def test_native_adjustment_expires_at_next_period(self):
        await self.controller.evaluate(self.zone, HEAT, self.now)
        changed = Observation(True, True, "heat", 23)
        result = await self.controller.evaluate(self.zone, changed, self.now)
        self.assertEqual(result.source, "manual")
        self.assertEqual(result.target, 23)
        result = await self.controller.evaluate(
            self.zone, changed, instant("2026-11-16T08:30:00+00:00")
        )
        self.assertEqual(result.target, 16)
        self.assertEqual(result.source, "schedule")

    async def test_off_hold_survives_restart(self):
        await self.controller.evaluate(self.zone, HEAT, self.now)
        off = Observation(True, True, "off", 20)
        await self.controller.evaluate(self.zone, off, self.now)
        restored = Repository(self.store)
        await restored.load()
        controller = PreviewController(restored, "Europe/London")
        result = await controller.evaluate(self.zone, off, instant("2026-11-17T12:00:00+00:00"))
        self.assertEqual(result.source, "manual_off")
        self.assertIsNone(result.target)

    async def test_external_boost_and_restore_are_not_manual_holds(self):
        await self.controller.evaluate(self.zone, HEAT, self.now)
        boosted = Observation(True, True, "heat", 30, True, instant("2026-11-16T08:00:00+00:00"))
        self.assertEqual(
            (await self.controller.evaluate(self.zone, boosted, self.now)).source, "external_boost"
        )
        result = await self.controller.evaluate(
            self.zone, Observation(True, True, "heat", 16), instant("2026-11-16T08:00:00+00:00")
        )
        self.assertEqual(result.source, "schedule")
        self.assertIsNone(self.repo.read(self.zone.id, LIMITS)[1])

    async def test_recovery_is_not_manual_and_zones_are_independent(self):
        await self.controller.evaluate(self.zone, HEAT, self.now)
        await self.controller.evaluate(self.zone, Observation(False, False, None, None), self.now)
        result = await self.controller.evaluate(
            self.zone, Observation(True, True, "heat", 22), self.now
        )
        self.assertEqual(result.source, "schedule")
        second = Zone("zone_b", Limits(16, 28, 0.5))
        await self.repo.initialise(second.id, Week.baseline(18, LIMITS))
        self.assertEqual((await self.controller.evaluate(second, HEAT, self.now)).target, 18)
        self.assertEqual(self.controller.decisions[self.zone.id].target, 20)

    async def test_boost_timer_cancel_before_restoration_does_not_create_hold(self):
        await self.controller.evaluate(self.zone, HEAT, self.now)
        await self.controller.evaluate(
            self.zone, Observation(True, True, "heat", 30, True), self.now
        )
        await self.controller.evaluate(
            self.zone, Observation(True, True, "heat", 30), instant("2026-11-16T08:00:00+00:00")
        )
        result = await self.controller.evaluate(
            self.zone, Observation(True, True, "heat", 16), instant("2026-11-16T08:00:01+00:00")
        )
        self.assertEqual(result.source, "schedule")
        self.assertIsNone(self.repo.read(self.zone.id, LIMITS)[1])

    async def test_invalid_hold_write_does_not_consume_manual_observation(self):
        await self.controller.evaluate(self.zone, HEAT, self.now)
        changed = Observation(True, True, "heat", 23)
        self.store.fail = True
        with self.assertRaises(OSError):
            await self.controller.evaluate(self.zone, changed, self.now)
        self.store.fail = False
        result = await self.controller.evaluate(self.zone, changed, self.now)
        self.assertEqual(result.source, "manual")

    async def test_expired_hold_is_discarded_after_restart(self):
        await self.repo.save_hold(
            self.zone.id, Hold("manual", 22, instant("2026-11-16T06:00:00+00:00")), LIMITS
        )
        result = await self.controller.evaluate(self.zone, HEAT, self.now)
        self.assertEqual(result.source, "schedule")
        self.assertIsNone(self.repo.read(self.zone.id, LIMITS)[1])

    async def test_unrecognised_storage_version_fails_closed(self):
        self.store.data = {"schema": 2, "zones": {}}
        with self.assertRaises(ValidationError):
            await Repository(self.store).load()

    async def test_history_is_bounded_and_does_not_contain_entity_or_location_data(self):
        await self.controller.evaluate(self.zone, HEAT, self.now)
        for index in range(100):
            await self.controller.evaluate(
                self.zone, Observation(True, True, "heat", 20 if index % 2 else 21), self.now
            )
        self.assertEqual(len(self.controller.history), 50)
        self.assertEqual(set(self.controller.history[0]), {"at", "zone_id", "source", "target"})
