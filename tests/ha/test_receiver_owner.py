"""Native baseline and finite overlay arbitration; never commands a real device."""

import asyncio
import unittest
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

try:
    from homeassistant.exceptions import Unauthorized
except ImportError as error:
    raise unittest.SkipTest("Install the target Home Assistant runtime") from error

from custom_components.smart_heating_scheduler.const import DAYS
from custom_components.smart_heating_scheduler.engine import Decision, ValidationError, Week
from custom_components.smart_heating_scheduler.receiver import ReceiverSchedules
from custom_components.smart_heating_scheduler.receiver_owner import OWNER_ACK
from custom_components.smart_heating_scheduler.repository import Repository
from tests.ha.test_receiver import ReceiverFixture
from tests.ha.test_receiver_transition import NOW, BoundaryReceiver


class OwnerTests(ReceiverFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.week = {d: [{"start": "00:00", "temperature": 16}] for d in DAYS}
        await self.repository.save_week("a", self.week, self.limits, 1)
        await self.repository.set_control(True)
        self.adapter = BoundaryReceiver({d: [{"minute": 0, "centi": 1600}] for d in DAYS})
        self.manager.factory = lambda *_: self.adapter
        self.runtime.hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/London"))
        self.smart["policy"] = {"boost_owner": "builtin"}
        self.runtime._lock = asyncio.Lock()
        self.runtime.controller = SimpleNamespace(timezone=ZoneInfo("Europe/London"))
        self.runtime.insights = SimpleNamespace(read=lambda *_: {"frost_cycle": None})
        self.manager.transition.schedule = lambda *_: None
        self.manager.qualification._schedule = lambda *_args, **_kwargs: None
        self.timepatch = patch("homeassistant.util.dt.utcnow", side_effect=lambda: self.adapter.now)
        self.timepatch.start()
        self.addCleanup(self.timepatch.stop)
        await self.run_action("prepare_test", revision=2)
        q = self.manager.data["zones"]["a"]["qualification"]
        q.update(
            state="restored",
            deadline=NOW.isoformat(),
            expiry_passed=True,
            restoration_verified=True,
        )
        await self.run_action("prepare_transition", revision=2)
        t = self.manager.data["zones"]["a"]["transition"]
        t.update(
            state="restored", first_observed=True, second_observed=True, restoration_verified=True
        )

    async def enable(self):
        self.zone.enabled = False
        result = await self.run_action("enable_owner", revision=2, acknowledgement=OWNER_ACK)
        self.zone.enabled = True
        return result

    async def write(self, target=16, source="schedule", seconds=0):
        self.adapter.now = NOW + timedelta(seconds=seconds)
        await self.manager.owner.observe(self.zone, self.adapter.now)
        decision = Decision(target, source, source, NOW + timedelta(hours=1), 16, False)
        status = {"next_evaluation": decision.next_evaluation}
        handled = await self.manager.owner.write(self.zone, decision, status, self.adapter.now)
        return handled, status

    async def test_activation_is_explicit_durable_and_ordinary_week_never_writes_targets(self):
        with self.assertRaises(ValidationError):
            await self.run_action("enable_owner", revision=2, acknowledgement=OWNER_ACK)
        result = await self.enable()
        self.assertEqual(result["schedule_owner"], "receiver")
        self.assertEqual(self.adapter.state["mode"], "schedule")
        writes = len(self.adapter.mode_writes)
        for seconds in (0, 30, 60, 120):
            handled, _status = await self.write(seconds=seconds)
            self.assertTrue(handled)
        self.assertEqual(len(self.adapter.mode_writes), writes)

    async def test_occupancy_and_preheat_use_two_minute_leases_then_current_schedule(self):
        await self.enable()
        for source in ("occupied_comfort", "preheat"):
            await self.write(20, source)
            self.assertEqual(self.adapter.state["duration"], 2)
            self.assertEqual(self.adapter.state["target"], 2000)
            count = len(self.adapter.mode_writes)
            await self.write(20, source, seconds=30)
            self.assertEqual(len(self.adapter.mode_writes), count)
            await self.write(20, source, seconds=60)
            self.assertEqual(len(self.adapter.mode_writes), count + 1)
            await self.write(16, seconds=65)
            self.assertEqual(self.adapter.state["mode"], "schedule")
            self.assertEqual(self.adapter.state["target"], 1600)
            self.adapter.now = NOW

    async def test_restart_reuses_lease_but_external_manual_off_and_changed_boost_win(self):
        await self.enable()
        await self.write(20, "preheat")
        recovered = ReceiverSchedules(self.runtime, self.store, lambda *_: self.adapter)
        await recovered.load()
        self.manager = recovered
        await self.write(20, "preheat", seconds=65)
        for state in (
            {"mode": "manual", "system": 4, "hold": 1},
            {"mode": "off", "system": 0},
            {"mode": "boost", "system": 5, "target": 2100, "duration": 30},
        ):
            self.adapter.state.update(state)
            count = len(self.adapter.mode_writes)
            await self.write(20, "occupied_comfort", seconds=90)
            self.assertEqual(len(self.adapter.mode_writes), count)

    async def test_storage_or_radio_failure_never_enables_ha_fallback(self):
        await self.enable()
        self.repository._persistence.fail = True
        count = len(self.adapter.mode_writes)
        with self.assertRaises(OSError):
            await self.write(20, "occupied_comfort")
        self.assertEqual(len(self.adapter.mode_writes), count)
        self.repository._persistence.fail = False

        async def fail(_attrs):
            raise ValidationError("radio timeout")

        self.adapter.after_write = fail
        with self.assertRaises(ValidationError):
            await self.write(20, "preheat")
        self.assertEqual(self.manager.owner.record("a")["state"], "uncertain")
        self.adapter.after_write = None
        count = len(self.adapter.mode_writes)
        handled, status = await self.write(20, "preheat", seconds=60)
        self.assertTrue(handled)
        self.assertEqual(len(self.adapter.mode_writes), count)
        self.assertEqual(status["error"], "receiver_ownership_uncertain")

    async def test_external_week_and_partial_save_are_not_silently_successful(self):
        await self.enable()
        changed = deepcopy(self.week)
        changed["mon"].append({"start": "15:00", "temperature": 20})
        revision = await self.manager.owner.save_week("a", changed, 2)
        self.assertEqual(revision, 3)
        self.assertEqual(
            self.repository.read("a", self.limits)[0].to_dict(),
            Week.parse(changed, self.limits).to_dict(),
        )
        self.assertEqual(self.manager.owner.record("a")["week"], self.adapter.schedule)
        self.adapter.schedule["tue"][0]["centi"] = 1700
        with self.assertRaises(ValidationError):
            await self.manager.owner.save_week("a", self.week, 3)
        self.assertEqual(self.repository.read("a", self.limits)[2], 3)

    async def test_explicit_resume_and_handback_are_required_to_leave_physical_modes(self):
        await self.enable()
        self.adapter.state.update(mode="off", system=0)
        await self.write()
        self.assertEqual(self.adapter.state["mode"], "off")
        await self.manager.owner.resume("a")
        self.assertEqual(self.adapter.state["mode"], "schedule")
        self.zone.enabled = False
        await self.run_action("disable_owner", acknowledgement="USE HA SCHEDULE")
        self.assertIsNone(self.repository.native_read("a"))
        self.assertEqual(self.adapter.state["mode"], "manual")
        self.assertEqual(self.adapter.state["target"], 1600)

    async def test_owner_marker_survives_optional_backup_failure_and_corruption_fails_closed(self):
        await self.enable()
        self.manager.error = "Receiver backups are invalid"
        with self.assertRaises(ValidationError):
            await self.write(20, "occupied_comfort")
        self.assertIsNotNone(self.repository.native_read("a"))
        recovered = Repository(self.repository._persistence)
        await recovered.load()
        self.assertEqual(recovered.native_read("a")["state"], "active")
        self.repository._persistence.data["native"]["a"]["digest"] = "corrupt"
        with self.assertRaises(ValidationError):
            await recovered.load()

    async def test_revoked_permission_prevents_handover(self):
        self.zone.enabled = False
        self.allowed = False
        with self.assertRaises(Unauthorized):
            await self.run_action("enable_owner", revision=2, acknowledgement=OWNER_ACK)
        self.assertEqual(self.adapter.mode_writes, [])

    async def test_manual_boost_returns_exact_original_manual_target(self):
        await self.enable()
        self.adapter.state.update(mode="manual", system=4, hold=1, duration=65535, target=1700)
        self.smart["boost"] = {"target": 20}
        handled, _ = await self.write(20, "boost")
        self.assertFalse(handled)  # Existing HA boost writer, original backed up first.
        self.adapter.state["target"] = 2000
        self.smart["boost"] = None
        await self.write()
        self.assertEqual(self.adapter.state["mode"], "manual")
        self.assertEqual(self.adapter.state["target"], 1700)

    async def test_reviewed_native_edits_can_be_adopted_without_radio_writes(self):
        await self.enable()
        self.adapter.schedule["mon"][0]["centi"] = 1700
        snapshot = (await self.run_action("read"))["snapshot"]
        self.zone.enabled = False
        count = len(self.adapter.mode_writes), len(self.adapter.writes)
        await self.run_action(
            "adopt_owner_week",
            revision=2,
            snapshot_digest=snapshot["digest"],
            acknowledgement="USE RECEIVER TIMETABLE",
        )
        self.assertEqual(
            self.repository.read("a", self.limits)[0].to_dict()["mon"][0]["temperature"], 17
        )
        self.assertEqual((len(self.adapter.mode_writes), len(self.adapter.writes)), count)

    async def test_frost_while_off_uses_explicit_manual_cycle_then_restores_off(self):
        await self.enable()
        self.adapter.state.update(mode="off", system=0, hold=0)
        self.runtime.insights.read = lambda *_: {"frost_cycle": {"target": 12}}
        handled, _ = await self.write(12, "frost")
        self.assertTrue(handled)
        self.assertEqual(self.adapter.state["mode"], "manual")
        self.assertEqual(self.adapter.state["target"], 1200)
        await self.write(None, "frost_return_off", seconds=60)
        self.assertEqual(self.adapter.state["mode"], "off")
