"""Durable schedule-copy workflows using simulated receivers, never live hardware."""

import asyncio
import unittest
from copy import deepcopy
from types import SimpleNamespace

try:
    from homeassistant.exceptions import Unauthorized
except ImportError as error:
    raise unittest.SkipTest("Install the target Home Assistant runtime") from error

from custom_components.smart_heating_scheduler.const import DAYS
from custom_components.smart_heating_scheduler.engine import Limits, ValidationError, Week
from custom_components.smart_heating_scheduler.native_schedule import from_week
from custom_components.smart_heating_scheduler.receiver import (
    WRITE_ACKNOWLEDGEMENT,
    ReceiverSchedules,
)
from custom_components.smart_heating_scheduler.repository import Repository, RevisionConflict


class MemoryStore:
    def __init__(self):
        self.data = None
        self.fail = False
        self.callback = None

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data):
        if self.fail:
            raise OSError("disk unavailable")
        if self.callback:
            self.callback()
        self.data = deepcopy(data)


class SimulatedReceiver:
    def __init__(self, schedule):
        self.identity = {"ieee": "test", "endpoint": 5, "transport": "zha"}
        self.schedule = deepcopy(schedule)
        self.writes = []
        self.read_hook = None
        self.write_hook = None

    async def read(self):
        if self.read_hook:
            self.read_hook()
        return deepcopy(self.schedule)

    async def write_day(self, day, periods):
        self.writes.append(day)
        if self.write_hook:
            await self.write_hook()
        self.schedule[day] = deepcopy(periods)


class ReceiverFixture:
    async def asyncSetUp(self):
        self.limits = Limits(16, 32, 0.5)
        self.week = {
            day: [{"start": "00:00", "temperature": 20, "label": "All day"}] for day in DAYS
        }
        self.repository = Repository(MemoryStore())
        await self.repository.initialise("a", Week.parse(self.week, self.limits))
        self.zone = SimpleNamespace(id="a", limits=self.limits, enabled=True)
        self.observation = SimpleNamespace(external_boost=False, boost_unknown=False)
        self.smart = {"boost": None, "external_operation": None}
        self.runtime = SimpleNamespace(
            hass=None,
            _closed=False,
            zones={"a": self.zone},
            repository=self.repository,
            mappings={"a": ("climate.test", None, "°C")},
            smart_record=lambda _id: self.smart,
            _observation=lambda _id: self.observation,
        )
        self.adapter = SimulatedReceiver(from_week(self.week, self.limits, "°C"))
        self.adapter.schedule["mon"][0]["centi"] = 1600
        self.original = deepcopy(self.adapter.schedule)
        self.store = MemoryStore()
        self.manager = ReceiverSchedules(self.runtime, self.store, lambda *_: self.adapter)
        self.allowed = True

    def authorize(self):
        if not self.allowed:
            raise Unauthorized()

    async def run_action(self, action, **payload):
        return await self.manager.execute("a", action, payload, self.authorize)

    async def preview(self):
        return (await self.run_action("prepare", revision=1))["plan"]

    async def write(self, plan):
        return await self.run_action(
            "write", plan_id=plan["id"], acknowledgement=WRITE_ACKNOWLEDGEMENT
        )


class ReceiverWorkflowTests(ReceiverFixture, unittest.IsolatedAsyncioTestCase):
    async def test_read_is_readonly_and_preserves_app_schedule(self):
        result = await self.run_action("read")
        self.assertEqual(result["state"], "read_complete")
        self.assertEqual(result["snapshot"]["import_week"]["mon"][0]["temperature"], 16)
        self.assertEqual(self.repository.read("a", self.limits)[0].to_dict(), self.week)
        self.assertEqual(self.adapter.writes, [])
        self.assertFalse(result["handover_available"])

    async def test_verified_copy_and_reviewed_backup_restore(self):
        plan = await self.preview()
        self.assertEqual(plan["changed_days"], ["mon"])
        self.assertEqual(self.adapter.writes, [])
        self.zone.enabled = False
        result = await self.write(plan)
        self.assertEqual(result["state"], "verified")
        self.assertEqual(self.adapter.writes, ["mon"])
        self.assertEqual(result["transactions"][0]["before"], self.original)
        restore = await self.run_action("prepare_restore", revision=1, backup_id=plan["id"])
        await self.write(restore["plan"])
        self.assertEqual(self.adapter.schedule, self.original)
        restarted = ReceiverSchedules(self.runtime, self.store)
        await restarted.load()
        self.assertIsNone(restarted.error)
        self.assertEqual(restarted.snapshot("a")["backup_count"], 2)

    async def test_enabled_zone_and_boost_block_all_writes(self):
        plan = await self.preview()
        with self.assertRaises(ValidationError):
            await self.write(plan)
        self.zone.enabled = False
        self.smart["boost"] = {"active": True}
        with self.assertRaises(ValidationError):
            await self.write(plan)
        self.assertEqual(self.adapter.writes, [])

    async def test_changed_receiver_rejects_stale_preview(self):
        plan = await self.preview()
        self.zone.enabled = False
        self.adapter.schedule["tue"][0]["centi"] = 2100
        with self.assertRaises(RevisionConflict):
            await self.write(plan)
        self.assertEqual(self.adapter.writes, [])

    async def test_revision_expiry_identity_permission_and_ack_guards(self):
        plan = await self.preview()
        self.zone.enabled = False
        with self.assertRaises(ValidationError):
            await self.run_action("write", plan_id=plan["id"], acknowledgement="yes")
        self.allowed = False
        with self.assertRaises(Unauthorized):
            await self.write(plan)
        self.allowed = True
        self.adapter.identity = {"ieee": "different"}
        with self.assertRaises(RevisionConflict):
            await self.write(plan)
        self.adapter.identity = plan["identity"]
        self.manager.data["zones"]["a"]["plan"]["expires_at"] = "2000-01-01T00:00:00+00:00"
        with self.assertRaises(RevisionConflict):
            await self.write(plan)
        plan = await self.preview()
        await self.repository.save_week("a", self.week, self.limits, 1)
        with self.assertRaises(RevisionConflict):
            await self.write(plan)
        self.assertEqual(self.adapter.writes, [])

    async def test_storage_failure_before_first_command_never_writes(self):
        plan = await self.preview()
        self.zone.enabled = False
        self.store.fail = True
        with self.assertRaises(OSError):
            await self.write(plan)
        self.assertEqual(self.adapter.writes, [])

    async def test_permission_revocation_during_backup_commit_stops_radio(self):
        plan = await self.preview()
        self.zone.enabled = False
        self.store.callback = lambda: setattr(self, "allowed", False)
        with self.assertRaises(Unauthorized):
            await self.write(plan)
        self.assertEqual(self.adapter.writes, [])
        self.assertEqual(self.manager.snapshot("a")["state"], "uncertain")

    async def test_mapping_change_during_read_is_not_saved(self):
        self.adapter.read_hook = lambda: self.runtime.mappings.update(
            a=("climate.other", None, "°C")
        )
        with self.assertRaises(RevisionConflict):
            await self.run_action("read")
        self.assertIsNone(self.store.data)

    async def test_failed_write_is_durably_uncertain(self):
        plan = await self.preview()
        self.zone.enabled = False

        async def fail():
            raise ValidationError("radio failed")

        self.adapter.write_hook = fail
        with self.assertRaises(ValidationError):
            await self.write(plan)
        self.assertEqual(self.manager.snapshot("a")["state"], "uncertain")
        self.assertEqual(
            self.store.data["zones"]["a"]["transactions"][0]["attempted_days"], ["mon"]
        )

    async def test_mismatching_readback_never_claims_success(self):
        plan = await self.preview()
        self.zone.enabled = False

        async def tamper():
            self.adapter.schedule["tue"][0]["centi"] = 2100

        self.adapter.write_hook = tamper
        with self.assertRaisesRegex(ValidationError, "readback differs"):
            await self.write(plan)
        self.assertEqual(self.manager.snapshot("a")["state"], "uncertain")

    async def test_interrupted_write_recovers_as_unresolved(self):
        plan = await self.preview()
        self.zone.enabled = False
        entered = asyncio.Event()

        async def block():
            entered.set()
            await asyncio.Event().wait()

        self.adapter.write_hook = block
        task = asyncio.create_task(self.write(plan))
        await entered.wait()
        self.manager.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        restarted = ReceiverSchedules(self.runtime, self.store)
        await restarted.load()
        self.assertEqual(restarted.snapshot("a")["state"], "writing")
        self.assertEqual(
            restarted.snapshot("a", details=True)["transactions"][0]["before"], self.original
        )

    async def test_unrestorable_empty_day_blocks_preview(self):
        self.adapter.schedule["mon"] = []
        with self.assertRaisesRegex(ValidationError, "safely restored"):
            await self.preview()
        self.assertEqual(self.adapter.writes, [])

    async def test_unreviewed_native_setpoints_are_readable_but_not_copied(self):
        for centi in (0, 50, 234, 2055):
            self.adapter.schedule["mon"][0]["centi"] = centi
            result = await self.run_action("read")
            self.assertEqual(result["snapshot"]["schedule"]["mon"][0]["centi"], centi)
            with self.assertRaisesRegex(ValidationError, "safely restored"):
                await self.preview()
        self.assertEqual(self.adapter.writes, [])

    async def test_malformed_optional_storage_is_quarantined(self):
        self.store.data = {
            "schema": 1,
            "zones": {"a": {"transactions": [{"before": self.original}]}},
        }
        await self.manager.load()
        self.assertIsNotNone(self.manager.error)
        self.assertEqual(self.manager.snapshot("a")["state"], "error")
        with self.assertRaises(ValidationError):
            await self.run_action("read")
        self.assertEqual(self.repository.read("a", self.limits)[2], 1)
