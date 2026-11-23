"""Clock/calendar qualification and real target-transition workflow simulations."""

import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

try:
    from homeassistant.exceptions import HomeAssistantError  # noqa: F401
except ImportError as error:
    raise unittest.SkipTest("Install the target Home Assistant runtime") from error

from custom_components.smart_heating_scheduler.const import DAYS
from custom_components.smart_heating_scheduler.engine import ValidationError
from custom_components.smart_heating_scheduler.native_receiver import current_target
from custom_components.smart_heating_scheduler.native_schedule import command, validate_native
from custom_components.smart_heating_scheduler.receiver_clock import clock_report, expected_clock
from custom_components.smart_heating_scheduler.receiver_transition import ACK
from tests.ha.test_receiver import ReceiverFixture
from tests.ha.test_receiver_qualification import NativeReceiver

NOW = datetime(2026, 11, 23, 12, tzinfo=UTC)


class ClockTests(unittest.TestCase):
    def test_london_active_and_next_dst_including_gap_and_fold(self):
        for instant in (
            NOW,
            datetime(2027, 3, 28, 0, 59, tzinfo=UTC),
            datetime(2027, 3, 28, 1, tzinfo=UTC),
            datetime(2027, 10, 31, 0, 59, tzinfo=UTC),
            datetime(2027, 10, 31, 1, tzinfo=UTC),
        ):
            values = {**expected_clock("Europe/London", instant), "time_status": 2}
            self.assertTrue(clock_report(values, "Europe/London", instant)["verified"])
            expected_local = instant.replace(tzinfo=None) + (
                instant.astimezone(ZoneInfo("Europe/London")).utcoffset()
            )
            self.assertEqual(
                values["local_time"], int((expected_local - datetime(2000, 1, 1)).total_seconds())
            )
        winter = expected_clock("Europe/London", NOW)
        self.assertEqual(winter["dst_shift"], 3600)
        self.assertGreater(winter["dst_start"], winter["time"])

    def test_stale_clock_zero_dst_and_wrong_zone_are_not_verified(self):
        values = {**expected_clock("Europe/London", NOW), "time_status": 2}
        for changes in (
            {"time": values["time"] - 90},
            {"dst_start": 0, "dst_shift": 0},
            {"time_zone": 3600},
            {"time_status": 0},
            {"local_time": 0xFFFFFFFF},
        ):
            self.assertFalse(clock_report({**values, **changes}, "Europe/London", NOW)["verified"])
        with self.assertRaises(ValidationError):
            clock_report({**values, "time": True}, "Europe/London", NOW)


class BoundaryReceiver(NativeReceiver):
    now = NOW

    async def read_state(self):
        if self.state["mode"] == "schedule":
            self.state["target"] = current_target(
                self.schedule, self.now.astimezone(ZoneInfo("Europe/London"))
            )
        return await super().read_state()

    async def read_clock(self):
        return {**expected_clock("Europe/London", self.now), "time_status": 2}

    async def write_control(self, attrs):
        await super().write_control(attrs)
        if attrs.get("system_mode") == 0:
            self.state.update(mode="off", system=0, hold=0)


class SharedCountReceiver(BoundaryReceiver):
    """Observed OTR1 behaviour: last written count governs all seven reads."""

    def __init__(self, week):
        super().__init__(week)
        self.wire = {
            day: [*values, *[values[-1]] * (6 - len(values))] for day, values in week.items()
        }
        self.count = 6

    async def read(self):
        self.read_counts = dict.fromkeys(DAYS, self.count)
        self.schedule = validate_native(
            {day: values[: self.count] for day, values in self.wire.items()}
        )
        return deepcopy(self.schedule)

    async def write_day(self, day, periods):
        payload = command(day, periods)
        self.count = payload["num_transitions_for_sequence"]
        self.wire[day] = [
            {"minute": payload["values"][i], "centi": payload["values"][i + 1]}
            for i in range(0, len(payload["values"]), 2)
        ]
        self.writes.append(day)


class TransitionTests(ReceiverFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.adapter = SharedCountReceiver({d: [{"minute": 0, "centi": 1600}] for d in DAYS})
        self.manager.factory = lambda *_: self.adapter
        self.runtime.hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/London"))
        self.manager.transition.schedule = lambda *_: None
        self.manager.qualification._schedule = lambda *_args, **_kwargs: None
        self.patch = patch("homeassistant.util.dt.utcnow", side_effect=lambda: self.adapter.now)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        # Use a valid durable timer record from the normal preparation path.
        await self.run_action("prepare_test", revision=1)
        q = self.manager.data["zones"]["a"]["qualification"]
        q.update(
            state="restored",
            deadline=NOW.isoformat(),
            expiry_passed=True,
            restoration_verified=True,
        )
        self.original = deepcopy(self.adapter.schedule)

    async def start(self):
        result = await self.run_action("prepare_transition", revision=1)
        self.zone.enabled = False
        return await self.run_action(
            "start_transition", test_id=result["transition"]["id"], acknowledgement=ACK
        )

    async def test_observes_two_targets_before_exact_restoration(self):
        await self.start()
        self.assertTrue(self.manager.qualification.active("a"))
        self.adapter.now = NOW + timedelta(minutes=3, seconds=10)
        result = await self.run_action("check_transition")
        self.assertTrue(result["transition"]["first_observed"])
        self.assertFalse(result["transition"]["second_observed"])
        self.adapter.now = NOW + timedelta(minutes=5, seconds=10)
        result = await self.run_action("check_transition")
        q = result["transition"]
        self.assertTrue(q["second_observed"] and q["restoration_verified"])
        self.assertEqual(q["state"], "restored")
        self.assertEqual(self.adapter.schedule, self.original)
        self.assertEqual(self.adapter.state["mode"], "manual")

    async def test_six_slot_padding_preserves_longer_other_days_during_short_test(self):
        original = [
            {"minute": m, "centi": t}
            for m, t in ((390, 2000), (510, 100), (720, 100), (840, 100), (990, 2000), (1320, 100))
        ]
        self.adapter = SharedCountReceiver(dict.fromkeys(DAYS, original))
        self.adapter.now = NOW
        await self.start()
        read = await self.adapter.read()
        self.assertEqual(read["tue"], original)
        self.assertEqual(len(read["mon"]), 3)
        self.assertEqual(self.adapter.count, 6)
        await self.run_action("restore_transition")
        self.assertEqual(await self.adapter.read(), dict.fromkeys(DAYS, original))

    async def test_missed_first_transition_never_passes_on_recovery(self):
        await self.start()
        self.adapter.now = NOW + timedelta(minutes=7)
        result = await self.run_action("check_transition")
        self.assertFalse(result["transition"]["first_observed"])
        self.assertTrue(result["transition"]["restoration_verified"])
        self.assertIsNotNone(result["transition"]["error"])

    async def test_changed_physical_off_and_week_are_not_overwritten(self):
        await self.start()
        count = len(self.adapter.mode_writes)
        self.adapter.state.update(mode="off", system=0)
        result = await self.run_action("check_transition")
        self.assertEqual(result["transition"]["state"], "needs_review")
        await self.run_action("restore_transition")
        self.assertEqual(len(self.adapter.mode_writes), count)
        self.assertEqual(self.adapter.state["mode"], "off")

    async def test_disabled_zone_acknowledgement_and_expiring_plan_required(self):
        result = await self.run_action("prepare_transition", revision=1)
        payload = {"test_id": result["transition"]["id"], "acknowledgement": ACK}
        with self.assertRaises(ValidationError):
            await self.run_action("start_transition", **payload)
        self.zone.enabled = False
        self.adapter.now = NOW + timedelta(minutes=3)
        with self.assertRaises(ValidationError):
            await self.run_action("start_transition", **payload)
        self.assertEqual(self.adapter.writes, [])
