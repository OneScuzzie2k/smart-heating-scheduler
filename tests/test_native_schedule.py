"""Wire conversion/replies and timeout cleanup without HA or Zigbee hardware."""

import asyncio
import unittest
from copy import deepcopy
from types import SimpleNamespace

from custom_components.smart_heating_scheduler.const import DAYS
from custom_components.smart_heating_scheduler.engine import Limits, ValidationError
from custom_components.smart_heating_scheduler.native_schedule import (
    DAY_BITS,
    ScheduleReplies,
    changes,
    command,
    digest,
    from_week,
    to_week,
    validate_native,
)
from custom_components.smart_heating_scheduler.receiver_zha import ZhaReceiver

LIMITS = Limits(16, 32, 0.5)


def week():
    return {
        day: [
            {"start": "00:00", "temperature": 16, "label": "Night"},
            {"start": "06:30" if index < 5 else "07:15", "temperature": 20},
            {"start": "08:30", "temperature": 16},
            {"start": "16:30", "temperature": 20},
            {"start": "22:00", "temperature": 16},
        ]
        for index, day in enumerate(DAYS)
    }


class NativeConversionTests(unittest.TestCase):
    def test_current_week_round_trip_and_weekend_times(self):
        native = from_week(week(), LIMITS, "°C")
        self.assertEqual(native["mon"][1], {"minute": 390, "centi": 2000})
        self.assertEqual(native["sun"][1], {"minute": 435, "centi": 2000})
        self.assertEqual(from_week(to_week(native, LIMITS), LIMITS, "°C"), native)
        self.assertEqual(changes(native, deepcopy(native)), [])

    def test_hardware_limits_and_units_never_silently_round(self):
        for unit, value in (("°F", 20), ("°C", 20.1), ("°C", 33), ("°C", True)):
            raw = week()
            raw["mon"][0]["temperature"] = value
            with self.assertRaises(ValidationError):
                from_week(raw, Limits(0, 100, None), unit)
        raw = week()
        raw["mon"] = [{"start": f"{i:02d}:00", "temperature": 20} for i in range(7)]
        with self.assertRaises(ValidationError):
            from_week(raw, LIMITS, "°C")

    def test_padding_deduplicates_only_identical_slots(self):
        raw = {"mon": [{"minute": 0, "centi": 1600}] * 6}
        self.assertEqual(len(validate_native(raw, complete=False)["mon"]), 1)
        raw["mon"][1] = {"minute": 0, "centi": 2000}
        with self.assertRaises(ValidationError):
            validate_native(raw, complete=False)

    def test_off_and_carryover_read_without_invented_app_temperatures(self):
        raw = from_week(week(), LIMITS, "°C")
        raw["mon"] = [{"minute": 390, "centi": 100}]
        self.assertEqual(validate_native(raw)["mon"][0]["centi"], 100)
        with self.assertRaises(ValidationError):
            to_week(raw, LIMITS)
        raw["mon"] = [{"minute": 0, "centi": 100}]
        with self.assertRaises(ValidationError):
            to_week(raw, LIMITS)

    def test_commands_only_touch_the_requested_day(self):
        raw = from_week(week(), LIMITS, "°C")
        self.assertEqual(DAY_BITS["sun"], 1)
        self.assertEqual(DAY_BITS["mon"], 2)
        self.assertEqual(DAY_BITS["sat"], 64)
        payload = command("mon", raw["mon"])
        self.assertEqual(payload["day_of_week_for_sequence"], 2)
        self.assertEqual(payload["mode_for_sequence"], 1)
        self.assertEqual(payload["num_transitions_for_sequence"], 6)
        self.assertEqual(payload["values"][-4:], [1320, 1600, 1320, 1600])
        self.assertEqual(payload["values"][:4], [0, 1600, 390, 2000])
        edited = deepcopy(raw)
        edited["tue"][1]["centi"] = 2100
        self.assertEqual(changes(raw, edited), ["tue"])
        self.assertNotEqual(digest(raw), digest(edited))

    def test_malformed_wire_values_fail_closed(self):
        for period in (
            {"minute": True, "centi": 2000},
            {"minute": 1440, "centi": 2000},
            {"minute": 0, "centi": float("nan")},
            {"minute": 0, "centi": 3300},
            {"minute": 0, "centi": 2000, "extra": 1},
        ):
            with self.assertRaises(ValidationError):
                validate_native({"mon": [period]}, complete=False)
        with self.assertRaises(ValidationError):
            validate_native({"mon": []})

    def test_replies_require_correlation_count_and_complete_week(self):
        replies = ScheduleReplies(17)
        response = SimpleNamespace(
            num_transitions_for_sequence=1,
            day_of_week_for_sequence=127,
            mode_for_sequence=1,
            values=[0, 1600],
        )
        replies.accept(16, 0, response)
        replies.accept(17, 2, response)
        self.assertFalse(replies.days)
        replies.accept(17, 0, response)
        self.assertTrue(replies.complete)
        response.values = [0, 2000]
        replies.accept(17, 0, response)
        self.assertFalse(replies.complete)
        self.assertIn("conflicting", replies.error)

    def test_malformed_or_cooling_replies_are_not_accepted(self):
        for count, mode, values in ((2, 1, [0, 2000]), (1, 3, [0, 2000]), (7, 1, [0, 2000])):
            replies = ScheduleReplies(1)
            replies.accept(
                1,
                0,
                SimpleNamespace(
                    num_transitions_for_sequence=count,
                    day_of_week_for_sequence=127,
                    mode_for_sequence=mode,
                    values=values,
                ),
            )
            self.assertIsNotNone(replies.error)
            self.assertFalse(replies.complete)


class FakeCluster:
    def __init__(self, *, reply=True, partial=False):
        self.endpoint = SimpleNamespace(device=SimpleNamespace(get_sequence=lambda: 42))
        self.listeners = []
        self.reply, self.partial = reply, partial
        self.commands = []

    def add_listener(self, listener):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        self.listeners.remove(listener)

    async def get_weekly_schedule(self, **kwargs):
        self.commands.append(kwargs)
        if self.reply:
            response = SimpleNamespace(
                num_transitions_for_sequence=1,
                day_of_week_for_sequence=2 if self.partial else 127,
                mode_for_sequence=1,
                values=[0, 1600],
            )
            for listener in self.listeners:
                listener.cluster_command(kwargs["tsn"], 0, response)

    async def set_weekly_schedule(self, **kwargs):
        self.commands.append(kwargs)


class NativeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_all_days_and_cleans_up_listener(self):
        cluster = FakeCluster()
        result = await ZhaReceiver(cluster, {}, timeout=0.1).read()
        self.assertEqual(set(result), set(DAYS))
        self.assertFalse(cluster.listeners)
        self.assertFalse(cluster.commands[0]["expect_reply"])
        self.assertEqual(cluster.commands[0]["tsn"], 42)

    async def test_partial_or_missing_reply_never_becomes_success(self):
        for cluster in (FakeCluster(partial=True), FakeCluster(reply=False)):
            with self.assertRaisesRegex(ValidationError, "incomplete"):
                await ZhaReceiver(cluster, {}, timeout=0.01).read()
            self.assertFalse(cluster.listeners)

    async def test_cancellation_removes_listener(self):
        cluster = FakeCluster(reply=False)
        task = asyncio.create_task(ZhaReceiver(cluster, {}).read())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(cluster.listeners)

    async def test_write_has_single_day_and_no_mode_or_clear_command(self):
        cluster = FakeCluster()
        await ZhaReceiver(cluster, {}).write_day("sat", [{"minute": 0, "centi": 1600}])
        self.assertEqual(len(cluster.commands), 1)
        self.assertEqual(cluster.commands[0]["day_of_week_for_sequence"], 64)
        self.assertEqual(
            set(cluster.commands[0]),
            {
                "num_transitions_for_sequence",
                "day_of_week_for_sequence",
                "mode_for_sequence",
                "values",
                "expect_reply",
            },
        )
