"""Bounded native timer, restore and interrupted-operation regression tests."""

import asyncio
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

try:
    from homeassistant.exceptions import Unauthorized
except ImportError as error:
    raise unittest.SkipTest("Install the target Home Assistant runtime") from error

from custom_components.smart_heating_scheduler.const import DAYS
from custom_components.smart_heating_scheduler.engine import ValidationError
from custom_components.smart_heating_scheduler.receiver import ReceiverSchedules
from custom_components.smart_heating_scheduler.receiver_qualification import (
    TEST_ACKNOWLEDGEMENT,
)
from custom_components.smart_heating_scheduler.repository import RevisionConflict
from tests.ha.test_receiver import ReceiverFixture, SimulatedReceiver

NOW = datetime(2026, 11, 23, 12, tzinfo=UTC)


class NativeReceiver(SimulatedReceiver):
    def __init__(self, week):
        super().__init__(week)
        self.state = {
            "system": 4,
            "hold": 1,
            "duration": 65535,
            "target": 1600,
            "room": 2000,
            "mode": "manual",
        }
        self.mode_writes = []
        self.after_write = None
        self.state_error = False

    async def read_state(self):
        if self.state_error:
            raise ValidationError("radio unavailable")
        return deepcopy(self.state)

    async def write_control(self, attrs):
        self.mode_writes.append(deepcopy(attrs))
        if len(attrs) == 2:
            self.expire()
        else:
            self.state.update(
                system=attrs["system_mode"],
                hold=attrs["temp_setpoint_hold"],
                duration=attrs["temp_setpoint_hold_duration"],
                target=attrs["occupied_heating_setpoint"],
                mode="boost" if attrs["system_mode"] == 5 else "manual",
            )
        if self.after_write:
            await self.after_write(attrs)

    def expire(self):
        self.state.update(system=4, hold=0, duration=0, target=100, mode="schedule")


class ReceiverQualificationTests(ReceiverFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        week = {
            day: [
                {"minute": 390, "centi": 2000},
                {"minute": 510, "centi": 100},
                {"minute": 990, "centi": 2000},
                {"minute": 1320, "centi": 100},
            ]
            for day in DAYS
        }
        self.adapter = NativeReceiver(week)
        self.manager.factory = lambda *_: self.adapter
        self.time_patch = patch(
            "custom_components.smart_heating_scheduler.receiver_qualification.dt_util.utcnow",
            return_value=NOW,
        )
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)

    async def prepared(self):
        return (await self.run_action("prepare_test", revision=1))["qualification"]

    async def start(self):
        q = await self.prepared()
        self.zone.enabled = False
        return await self.run_action(
            "start_test", test_id=q["id"], acknowledgement=TEST_ACKNOWLEDGEMENT
        )

    async def test_inspection_and_preparation_never_write_modes(self):
        result = await self.run_action("inspect")
        self.assertEqual(result["hardware"]["state"]["mode"], "manual")
        q = await self.prepared()
        self.assertEqual(q["original"]["target"], 1600)
        self.assertEqual(self.adapter.mode_writes, [])

    async def test_native_expiry_is_observed_before_original_hold_is_restored(self):
        result = await self.start()
        self.assertEqual(result["qualification"]["state"], "waiting_expiry")
        self.assertEqual(self.adapter.mode_writes[0], {"system_mode": 4, "temp_setpoint_hold": 0})
        self.assertEqual(len(self.adapter.mode_writes[1]), 4)
        self.assertEqual(self.adapter.mode_writes[1]["temp_setpoint_hold_duration"], 2)
        self.adapter.expire()
        with patch(
            "custom_components.smart_heating_scheduler.receiver_qualification.dt_util.utcnow",
            return_value=NOW + timedelta(seconds=125),
        ):
            result = await self.run_action("check_test")
        q = result["qualification"]
        self.assertEqual(q["result"], "returned_to_schedule")
        self.assertTrue(q["expiry_passed"])
        self.assertTrue(q["schedule_target_verified"])
        self.assertTrue(q["restoration_verified"])
        self.assertEqual(self.adapter.state["mode"], "manual")
        self.assertEqual(self.adapter.state["target"], 1600)
        self.assertEqual(self.adapter.writes, [])

    async def test_all_write_guards_apply_before_radio(self):
        q = await self.prepared()
        payload = {"test_id": q["id"], "acknowledgement": TEST_ACKNOWLEDGEMENT}
        with self.assertRaises(ValidationError):
            await self.run_action("start_test", **payload)
        self.zone.enabled = False
        self.allowed = False
        with self.assertRaises(Unauthorized):
            await self.run_action("start_test", **payload)
        self.allowed = True
        with self.assertRaises(ValidationError):
            await self.run_action("start_test", test_id=q["id"], acknowledgement="yes")
        self.adapter.identity = {"ieee": "replaced"}
        with self.assertRaises(RevisionConflict):
            await self.run_action("start_test", **payload)
        self.assertEqual(self.adapter.mode_writes, [])

    async def test_changed_control_or_week_rejects_preview(self):
        for kind in ("control", "week"):
            q = await self.prepared()
            self.zone.enabled = False
            if kind == "control":
                self.adapter.state["target"] = 1700
            else:
                self.adapter.schedule["tue"][0]["centi"] = 2100
            with self.assertRaises(RevisionConflict):
                await self.run_action(
                    "start_test", test_id=q["id"], acknowledgement=TEST_ACKNOWLEDGEMENT
                )
            self.adapter.state["target"] = 1600
        self.assertEqual(self.adapter.mode_writes, [])

    async def test_no_heat_preflight_rejects_cold_room_or_warmer_period(self):
        self.adapter.state["room"] = 1650
        with self.assertRaises(ValidationError):
            await self.prepared()
        self.adapter.state["room"] = 2000
        self.adapter.schedule["mon"] = [{"minute": 0, "centi": 2000}]
        with self.assertRaises(ValidationError):
            await self.prepared()
        self.assertEqual(self.adapter.mode_writes, [])

    async def test_backup_storage_failure_never_changes_mode(self):
        q = await self.prepared()
        self.zone.enabled = False
        self.store.fail = True
        with self.assertRaises(OSError):
            await self.run_action(
                "start_test", test_id=q["id"], acknowledgement=TEST_ACKNOWLEDGEMENT
            )
        self.assertEqual(self.adapter.mode_writes, [])

    async def test_invalid_hardware_countdown_restores_original_and_never_passes(self):
        await self.start()
        self.adapter.state["duration"] = 65535
        result = await self.run_action("check_test")
        self.assertEqual(result["qualification"]["result"], "countdown_invalid")
        self.assertTrue(result["qualification"]["restoration_verified"])
        self.assertFalse(result["qualification"]["expiry_passed"])

    async def test_stuck_boost_gets_bounded_watchdog_restoration_not_false_success(self):
        await self.start()
        with patch(
            "custom_components.smart_heating_scheduler.receiver_qualification.dt_util.utcnow",
            return_value=NOW + timedelta(minutes=4),
        ):
            result = await self.run_action("check_test")
        self.assertEqual(result["qualification"]["result"], "expiry_failed")
        self.assertTrue(result["qualification"]["restoration_verified"])
        self.assertFalse(result["qualification"]["expiry_passed"])

    async def test_physical_off_or_new_manual_target_is_not_overwritten(self):
        await self.start()
        before = len(self.adapter.mode_writes)
        self.adapter.state.update(system=0, mode="off")
        result = await self.run_action("check_test")
        self.assertEqual(result["qualification"]["state"], "needs_review")
        self.assertEqual(len(self.adapter.mode_writes), before)
        result = await self.run_action("restore_test")
        self.assertFalse(result["qualification"]["restoration_verified"])
        self.assertEqual(len(self.adapter.mode_writes), before)

    async def test_cancel_restores_without_claiming_natural_expiry(self):
        await self.start()
        result = await self.run_action("restore_test")
        self.assertEqual(result["qualification"]["result"], "cancelled")
        self.assertTrue(result["qualification"]["restoration_verified"])
        self.assertFalse(result["qualification"]["expiry_passed"])

    async def test_different_physical_boost_duration_is_not_cancelled(self):
        await self.start()
        self.adapter.state["duration"] = 30
        count = len(self.adapter.mode_writes)
        result = await self.run_action("check_test")
        self.assertEqual(result["qualification"]["state"], "needs_review")
        await self.run_action("restore_test")
        self.assertEqual(len(self.adapter.mode_writes), count)

    async def test_return_to_manual_is_restored_but_does_not_pass_schedule_expiry(self):
        await self.start()
        self.adapter.state.update(system=4, hold=1, duration=0, mode="manual")
        result = await self.run_action("check_test")
        self.assertEqual(result["qualification"]["result"], "returned_to_manual")
        self.assertTrue(result["qualification"]["restoration_verified"])
        self.assertFalse(result["qualification"]["expiry_passed"])

    async def test_active_test_blocks_timetable_writes_and_second_test(self):
        await self.start()
        for action, payload in (("prepare", {"revision": 1}), ("prepare_test", {"revision": 1})):
            with self.assertRaises(ValidationError):
                await self.run_action(action, **payload)

    async def test_unavailable_read_never_forces_original_over_unknown_hardware(self):
        await self.start()
        self.adapter.state_error = True
        count = len(self.adapter.mode_writes)
        with self.assertRaises(ValidationError):
            await self.run_action("check_test")
        self.assertEqual(len(self.adapter.mode_writes), count)
        self.assertEqual(self.manager.snapshot("a")["qualification"]["state"], "waiting_expiry")

    async def test_reenabled_zone_or_remapped_device_blocks_restoration(self):
        await self.start()
        self.zone.enabled = True
        count = len(self.adapter.mode_writes)
        with self.assertRaises(ValidationError):
            await self.run_action("restore_test")
        self.assertEqual(len(self.adapter.mode_writes), count)
        self.zone.enabled = False
        self.runtime.mappings["a"] = ("climate.replacement", None, "°C")
        with self.assertRaises(RevisionConflict):
            await self.run_action("restore_test")
        self.assertEqual(len(self.adapter.mode_writes), count)

    async def test_restart_reads_native_expiry_before_restoring_the_saved_hold(self):
        await self.start()
        self.manager.close()
        self.manager = ReceiverSchedules(self.runtime, self.store, lambda *_: self.adapter)
        await self.manager.load()
        self.assertIsNone(self.manager.error)
        self.manager.qualification.resume()
        self.adapter.expire()
        with patch(
            "custom_components.smart_heating_scheduler.receiver_qualification.dt_util.utcnow",
            return_value=NOW + timedelta(minutes=4),
        ):
            result = await self.run_action("check_test")
        q = result["qualification"]
        self.assertTrue(q["expiry_passed"])
        self.assertTrue(
            any(
                o["after_reload"] and o["hardware"]["mode"] == "schedule" for o in q["observations"]
            )
        )
        self.assertTrue(q["restoration_verified"])

    async def test_interrupted_schedule_write_is_reconciled_and_restored_after_load(self):
        entered = asyncio.Event()

        async def interrupt(attrs):
            entered.set()
            await asyncio.Event().wait()

        self.adapter.after_write = interrupt
        task = asyncio.create_task(self.start())
        await entered.wait()
        self.manager.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.adapter.after_write = None
        self.manager = ReceiverSchedules(self.runtime, self.store, lambda *_: self.adapter)
        await self.manager.load()
        self.assertIsNone(self.manager.error)
        result = await self.run_action("check_test")
        self.assertEqual(result["qualification"]["result"], "interrupted")
        self.assertTrue(result["qualification"]["restoration_verified"])
        self.assertFalse(result["qualification"]["expiry_passed"])

    async def test_restore_readback_mismatch_is_not_claimed_verified(self):
        await self.start()
        self.adapter.expire()

        async def mismatch(attrs):
            self.adapter.state["target"] = 1700

        self.adapter.after_write = mismatch
        with patch(
            "custom_components.smart_heating_scheduler.receiver_qualification.dt_util.utcnow",
            return_value=NOW + timedelta(minutes=3),
        ):
            result = await self.run_action("check_test")
        self.assertEqual(result["qualification"]["state"], "needs_review")
        self.assertFalse(result["qualification"]["restoration_verified"])

    async def test_no_matching_scheduled_target_does_not_claim_clock_validation(self):
        await self.start()
        self.adapter.expire()
        self.adapter.state["target"] = 1600
        with patch(
            "custom_components.smart_heating_scheduler.receiver_qualification.dt_util.utcnow",
            return_value=NOW + timedelta(minutes=3),
        ):
            result = await self.run_action("check_test")
        self.assertTrue(result["qualification"]["expiry_passed"])
        self.assertFalse(result["qualification"]["schedule_target_verified"])
        self.assertTrue(result["qualification"]["restoration_verified"])
