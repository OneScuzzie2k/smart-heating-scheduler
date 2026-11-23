"""Native mode validation, no-heat preflight, and atomic write transport."""

import unittest
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from custom_components.smart_heating_scheduler.const import DAYS
from custom_components.smart_heating_scheduler.engine import Limits, ValidationError
from custom_components.smart_heating_scheduler.native_receiver import (
    STATE_ATTRIBUTES,
    boost_attributes,
    control_attributes,
    current_target,
    manual_attributes,
    receiver_state,
)
from custom_components.smart_heating_scheduler.native_receiver import (
    test_preflight as qualification_preflight,
)
from custom_components.smart_heating_scheduler.receiver_zha import ZhaReceiver


def manual():
    return receiver_state(dict(zip(STATE_ATTRIBUTES, (4, 1, 65535, 1600, 1950), strict=True)))


class NativeStateTests(unittest.TestCase):
    def test_actual_modes_keep_schedule_separate_from_heat(self):
        for system, hold, expected in (
            (4, 1, "manual"),
            (4, 0, "schedule"),
            (0, 1, "off"),
            (5, 1, "boost"),
        ):
            values = dict(zip(STATE_ATTRIBUTES, (system, hold, 2, 1600, 1950), strict=True))
            self.assertEqual(receiver_state(values)["mode"], expected)

    def test_unknown_missing_inconsistent_and_invalid_room_readings_are_rejected(self):
        for system, hold, room in ((3, 1, 1950), (5, 0, 1950), (4, True, 1950), (4, 1, -32768)):
            values = dict(zip(STATE_ATTRIBUTES, (system, hold, 2, 1600, room), strict=True))
            with self.assertRaises(ValidationError):
                receiver_state(values)
        with self.assertRaises(ValidationError):
            receiver_state({})

    def test_only_reviewed_control_operations_are_allowed(self):
        self.assertEqual(manual_attributes(manual())["temp_setpoint_hold_duration"], 65535)
        self.assertEqual(
            control_attributes(boost_attributes(1600))["temp_setpoint_hold_duration"], 2
        )
        for attrs in (
            {"system_mode": 1, "temp_setpoint_hold": 0},
            {"system_mode": 4, "temp_setpoint_hold": False},
            {**boost_attributes(1600), "temp_setpoint_hold_duration": 65535},
            {**boost_attributes(1600), "occupied_heating_setpoint": 1655},
            {**boost_attributes(1600), "unreviewed": 1},
        ):
            with self.assertRaises(ValidationError):
                control_attributes(attrs)

    def test_native_midnight_carryover_and_off_markers_remain_native(self):
        week = {
            day: [{"minute": 390, "centi": 2000}, {"minute": 1320, "centi": 100}] for day in DAYS
        }
        self.assertEqual(current_target(week, datetime(2026, 11, 23, 2, tzinfo=UTC)), 100)
        self.assertEqual(current_target(week, datetime(2026, 11, 23, 7, tzinfo=UTC)), 2000)
        week = {day: [] for day in DAYS}
        with self.assertRaises(ValidationError):
            current_target(week, datetime(2026, 11, 23, 2, tzinfo=UTC))

    def test_preflight_blocks_upcoming_warm_period_and_cold_room(self):
        week = {day: [{"minute": 0, "centi": 100}, {"minute": 720, "centi": 2000}] for day in DAYS}
        limits = Limits(16, 32, 0.5)
        qualification_preflight(manual(), week, limits, datetime(2026, 11, 23, 10, tzinfo=UTC))
        with self.assertRaises(ValidationError):
            qualification_preflight(
                manual(), week, limits, datetime(2026, 11, 23, 11, 58, tzinfo=UTC)
            )
        state = manual()
        state["room"] = 1650
        with self.assertRaises(ValidationError):
            qualification_preflight(state, week, limits, datetime(2026, 11, 23, 10, tzinfo=UTC))

    def test_both_dst_folds_resolve_native_wall_clock_without_unit_conversion(self):
        week = {day: [{"minute": 0, "centi": 1600}, {"minute": 90, "centi": 2000}] for day in DAYS}
        tz = ZoneInfo("Europe/London")
        for fold in (0, 1):
            self.assertEqual(
                current_target(week, datetime(2026, 10, 25, 1, 45, fold=fold, tzinfo=tz)), 2000
            )


class NativeModeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_attributes_and_one_combined_boost_frame(self):
        calls = []

        async def read(attrs, **kwargs):
            calls.append(("read", attrs, kwargs))
            return dict(zip(STATE_ATTRIBUTES, (4, 1, 65535, 1600, 1950), strict=True)), {}

        async def write(attrs):
            calls.append(("write", deepcopy(attrs)))
            return ([SimpleNamespace(status=0)],)

        adapter = ZhaReceiver(SimpleNamespace(read_attributes=read, write_attributes=write), {})
        self.assertEqual((await adapter.read_state())["mode"], "manual")
        self.assertEqual(calls[0][2], {"allow_cache": False})
        await adapter.write_control(boost_attributes(1600))
        self.assertEqual([c[0] for c in calls], ["read", "write"])
        self.assertEqual(len(calls[1][1]), 4)
        with self.assertRaises(ValidationError):
            await adapter.write_control({"system_mode": 5})
        self.assertEqual(len(calls), 2)

    async def test_receiver_rejection_and_unsupported_attributes_are_not_success(self):
        async def read(*args, **kwargs):
            return {}, {"temp_setpoint_hold_duration": 0x86}

        async def write(attrs):
            return ([SimpleNamespace(status=0x86)],)

        adapter = ZhaReceiver(SimpleNamespace(read_attributes=read, write_attributes=write), {})
        with self.assertRaises(ValidationError):
            await adapter.read_state()
        with self.assertRaises(ValidationError):
            await adapter.write_control(boost_attributes(1600))
