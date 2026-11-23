"""Real HA objects with local simulated entities; never connect to a server.

Run with Python >=3.14.2 and homeassistant==2026.11.1 installed:
python -m unittest tests.ha.test_runtime -v
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    from homeassistant.config_entries import ConfigEntries, ConfigEntry
    from homeassistant.core import Context, HomeAssistant
    from homeassistant.exceptions import Unauthorized
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
except ImportError as error:
    raise unittest.SkipTest("Install the target Home Assistant runtime for HA tests") from error

from custom_components.smart_heating_scheduler import websocket as card_api
from custom_components.smart_heating_scheduler.config_flow import (
    SchedulerConfigFlow,
    ZoneSubentryFlow,
    validate_zone,
)
from custom_components.smart_heating_scheduler.const import DOMAIN
from custom_components.smart_heating_scheduler.engine import ValidationError
from custom_components.smart_heating_scheduler.runtime import Runtime, async_register_services
from custom_components.smart_heating_scheduler.sensor import async_setup_entry as setup_sensors


class HomeAssistantRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_owner_keeps_preheat_and_additive_occupancy_in_the_real_rule_pipeline(
        self,
    ):
        from datetime import UTC, datetime, timedelta

        from custom_components.smart_heating_scheduler.const import DAYS
        from custom_components.smart_heating_scheduler.engine import Week
        from custom_components.smart_heating_scheduler.native_schedule import digest, from_week
        from tests.ha.test_receiver_transition import BoundaryReceiver

        await self.configure_builtin()
        now = datetime(2026, 11, 23, 6, tzinfo=UTC)
        zone = self.runtime.zones["zone_a"]
        week = Week.parse(
            {
                d: [{"start": "00:00", "temperature": 16}, {"start": "06:30", "temperature": 20}]
                for d in DAYS
            },
            zone.limits,
        )
        await self.runtime.repository.save_week(zone.id, week.to_dict(), zone.limits, 1)
        native = from_week(week.to_dict(), zone.limits, "°C")
        adapter = BoundaryReceiver(native)
        adapter.state.update(mode="schedule", hold=0)
        adapter.now = now
        self.runtime.receivers.factory = lambda *_: adapter
        owner = {
            "state": "active",
            "week": native,
            "digest": digest(native),
            "identity": adapter.identity,
            "mapping": list(self.runtime.mappings[zone.id]),
            "revision": 2,
            "timezone": "Europe/London",
            "clock_checked": now.isoformat(),
            "at": now.isoformat(),
            "lease": None,
            "error": None,
        }
        await self.runtime.repository.native_save(zone.id, owner)
        record = self.runtime.insights.read(zone.id, zone.limits)
        record["settings"]["preheat_enabled"] = True
        record["model"] = {"rate": 2, "samples": 12, "minutes": 60, "updated": now.isoformat()}
        await self.runtime.insights.save(zone.id, record)
        self.hass.states.async_set(
            "climate.test_zone",
            "heat",
            {**self.attrs, "temperature": 16, "current_temperature": 19},
        )
        await self.runtime.repository.set_control(True)
        policy = self.runtime.smart_record(zone.id)
        policy["policy"].update(
            entities=[{"entity_id": "person.test", "home_state": "home", "away_state": "not_home"}],
            away_minutes=0,
            return_minutes=0,
            comfort_target=20,
            comfort_days=["mon"],
            comfort_start="08:30",
            comfort_end="16:30",
        )
        await self.runtime.repository.smart_save(zone.id, policy, zone.limits)
        with patch("homeassistant.util.dt.utcnow", side_effect=lambda: adapter.now):
            self.hass.states.async_set("person.test", "not_home")
            await self.runtime.async_evaluate()
            self.assertEqual(self.runtime.status[zone.id]["source"], "preheat")
            self.assertEqual(adapter.state["mode"], "boost")
            self.assertEqual(adapter.state["duration"], 2)
            # Move to daytime setback with the normal five-period baseline.
            periods = [{"start": "00:00", "temperature": 16}, {"start": "16:30", "temperature": 20}]
            await self.runtime.async_save_week(zone.id, {d: periods for d in DAYS}, 2)
            adapter.now = now.replace(hour=12)
            self.hass.states.async_set("person.test", "home")
            await self.runtime.async_evaluate()
            self.assertEqual(self.runtime.status[zone.id]["source"], "occupied_comfort")
            self.assertEqual(adapter.state["target"], 2000)
            self.hass.states.async_set("person.test", "not_home")
            adapter.now += timedelta(seconds=1)
            await self.runtime.async_evaluate()
            self.assertEqual(adapter.state["mode"], "schedule")
            self.assertEqual(adapter.state["target"], 1600)
            await self.runtime.async_control_action(
                "boost_start", zone.id, {"target": 22, "minutes": 10}
            )
            self.assertEqual(adapter.state["mode"], "boost")
            # Physical Off is already fresh on the radio while HA's climate
            # entity deliberately still says Heat. It must cancel this boost.
            adapter.state.update(mode="off", system=0, hold=0)
            await self.runtime.async_evaluate()
            self.assertEqual(adapter.state["mode"], "off")
            self.assertIsNone(self.runtime.smart_record(zone.id)["boost"])
            await self.runtime.async_control_action(
                "thermostat_mode", zone.id, {"mode": "manual", "target": 18}
            )
            self.assertEqual(adapter.state["mode"], "manual")
            self.assertEqual(adapter.state["target"], 1800)
            await self.runtime.async_control_action("resume", zone.id, {})
            self.assertEqual(adapter.state["mode"], "schedule")
            await self.runtime.async_control_action(
                "boost_start", zone.id, {"target": 22, "minutes": 10}
            )
            await self.runtime.async_control_action("thermostat_mode", zone.id, {"mode": "off"})
            self.assertEqual(adapter.state["mode"], "off")
            self.assertIsNone(self.runtime.smart_record(zone.id)["boost"])
            await self.runtime.async_control_action("resume", zone.id, {})
            self.assertEqual(adapter.state["mode"], "schedule")
        self.assertEqual(self.calls, [])  # No generic weekly climate writer was used.

    async def test_native_qualification_modes_are_not_recorded_as_manual_temperature_changes(self):
        from dataclasses import replace

        await self.configure_builtin()
        zone = self.runtime.zones["zone_a"]
        self.runtime.zones[zone.id] = replace(zone, enabled=False)
        with patch.object(
            self.runtime.receivers.qualification, "owns_observation", return_value=True
        ):
            self.hass.states.async_set(
                "climate.test_zone", "heat", {**self.attrs, "temperature": 1}
            )
            await self.runtime.async_evaluate()
            self.assertEqual(self.runtime.status[zone.id]["source"], "disabled")
            self.hass.states.async_set(
                "climate.test_zone", "heat", {**self.attrs, "temperature": 20}
            )
            await self.runtime.async_evaluate()
        self.assertIsNone(self.runtime.repository.read(zone.id, zone.limits)[1])
        self.assertEqual(self.calls, [])

    async def test_frost_input_bound_aligns_with_hardware_step_grid(self):
        await self.configure_builtin()
        self.hass.states.async_set("climate.test_zone", "off", {**self.attrs, "min_temp": 5.1})
        limits = self.runtime._frost_limits(self.runtime.zones["zone_a"])
        self.assertEqual(limits.minimum, 5.5)
        self.assertEqual(limits.validate(12), 12)
        self.assertEqual(self.calls, [])

    async def test_lower_frost_range_preserves_normal_limits_and_revalidates_device(self):
        await self.configure_builtin()
        self.simulated_receiver()
        self.hass.states.async_set(
            "climate.test_zone", "off", {**self.attrs, "current_temperature": 11.5}
        )
        await self.runtime.async_evaluate()
        zone = self.runtime.zones["zone_a"]
        settings = self.runtime.insights.read(zone.id, zone.limits)["settings"]
        settings.update(frost_enabled=True, frost_target=12, frost_while_off=True)
        await self.runtime.async_control_action(
            "insights", zone.id, {"revision": 1, "settings": settings}
        )
        await self.enable_control()
        await self.hass.async_block_till_done()
        self.assertEqual(self.runtime.status[zone.id]["target"], 13)
        self.assertEqual(zone.limits.minimum, 16)
        current = self.hass.states.get("climate.test_zone")
        self.hass.states.async_set(
            "climate.test_zone", "heat", {**current.attributes, "min_temp": 14}
        )
        await self.runtime.async_evaluate()
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get("climate.test_zone").state, "off")
        with self.assertRaises(ValidationError):
            await self.runtime.async_control_action(
                "insights", zone.id, {"revision": 2, "settings": settings}
            )

    async def test_frost_storage_failure_never_commands_cold_off_receiver(self):
        await self.configure_builtin()
        self.simulated_receiver()
        self.hass.states.async_set(
            "climate.test_zone", "off", {**self.attrs, "current_temperature": 17}
        )
        await self.runtime.async_evaluate()
        record = self.runtime.insights.read("zone_a", self.runtime.zones["zone_a"].limits)
        record["settings"].update(frost_enabled=True, frost_target=18, frost_while_off=True)
        await self.runtime.insights.save("zone_a", record)
        with patch.object(self.runtime.insights.store, "async_save", side_effect=OSError("disk")):
            await self.enable_control()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.hass.states.get("climate.test_zone").state, "off")
        self.assertEqual(self.runtime.status["zone_a"]["source"], "error")

    async def test_configuration_restore_requires_disabled_zone_checks_revision_and_rolls_back(
        self,
    ):
        from copy import deepcopy
        from dataclasses import replace

        await self.configure_builtin()
        zone = self.runtime.zones["zone_a"]
        old = self.runtime.insights.read(zone.id, zone.limits)
        week, _, revision = self.runtime.repository.read(zone.id, zone.limits)
        week = week.to_dict()
        week["mon"][0]["temperature"] = 18
        features = deepcopy(old["settings"])
        features.update(frost_enabled=True, frost_target=16)
        backup = {
            "format": "smart-heating-scheduler/zone",
            "version": 1,
            "unit": "°C",
            "week": week,
            "features": features,
            "policy": self.runtime.smart_record(zone.id)["policy"],
        }
        payload = {
            "backup": backup,
            "revision": revision,
            "features_revision": old["revision"],
            "policy_revision": self.runtime.smart_record(zone.id)["policy_revision"],
            "acknowledgement": "RESTORE ZONE SETTINGS",
        }
        with self.assertRaises(ValidationError):
            await self.runtime.async_control_action("restore_configuration", zone.id, payload)
        self.runtime.zones[zone.id] = replace(zone, enabled=False)
        with patch.object(
            self.runtime.repository, "restore_configuration", side_effect=OSError("disk")
        ):
            with self.assertRaises(OSError):
                await self.runtime.async_control_action("restore_configuration", zone.id, payload)
        self.assertEqual(self.runtime.insights.read(zone.id, zone.limits), old)
        await self.runtime.async_control_action("restore_configuration", zone.id, payload)
        self.assertEqual(
            self.runtime.repository.read(zone.id, zone.limits)[0].to_dict()["mon"][0][
                "temperature"
            ],
            18,
        )
        self.assertTrue(
            self.runtime.insights.read(zone.id, zone.limits)["settings"]["frost_enabled"]
        )
        with self.assertRaises(ValidationError):
            await self.runtime.async_control_action("restore_configuration", zone.id, payload)
        self.assertEqual(self.calls, [])

    async def test_frost_off_cycle_returns_off_and_survives_runtime_restart(self):
        await self.configure_builtin()
        self.simulated_receiver()
        self.hass.states.async_set(
            "climate.test_zone", "off", {**self.attrs, "current_temperature": 17}
        )
        await self.runtime.async_evaluate()
        settings = self.runtime.insights.read("zone_a", self.runtime.zones["zone_a"].limits)[
            "settings"
        ]
        settings.update(frost_enabled=True, frost_target=18, frost_while_off=True)
        await self.runtime.async_control_action(
            "insights", "zone_a", {"revision": 1, "settings": settings}
        )
        self.assertEqual(self.calls, [])
        await self.enable_control()
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get("climate.test_zone").state, "heat")
        self.assertEqual(self.hass.states.get("climate.test_zone").attributes["temperature"], 19)
        self.assertEqual(self.runtime.status["zone_a"]["source"], "frost")
        self.runtime.close()
        self.runtime = Runtime(self.hass, self.entry)
        self.entry.runtime_data = self.runtime
        await self.runtime.async_setup()
        self.assertEqual(self.runtime.status["zone_a"]["source"], "frost")
        state = self.hass.states.get("climate.test_zone")
        self.hass.states.async_set(
            "climate.test_zone", "heat", {**state.attributes, "current_temperature": 18.5}
        )
        await self.runtime.async_evaluate()
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get("climate.test_zone").state, "off")
        self.assertIsNone(
            self.runtime.insights.read("zone_a", self.runtime.zones["zone_a"].limits)["frost_cycle"]
        )

    async def test_guided_setup_reviews_features_and_creates_disabled_zone(self):
        self.hass.states.async_set("climate.new_zone", "heat", self.attrs)
        self.hass.states.async_set("person.test", "home")
        with patch.object(ZoneSubentryFlow, "_get_entry", return_value=self.entry):
            flow = ZoneSubentryFlow()
            flow.hass = self.hass
            flow.context = {"source": "user"}
            self.assertEqual((await flow.async_step_user())["type"], "menu")
            result = await flow.async_step_guided(
                {"name": "New room", "climate_entity": "climate.new_zone"}
            )
            self.assertEqual(result["step_id"], "limits")
            result = await flow.async_step_limits(
                {"minimum": 5, "maximum": 28, "baseline_temperature": 16}
            )
            self.assertEqual(result["step_id"], "options")
            options = {
                "boost_enabled": True,
                "boost_target": 23,
                "person": "person.test",
                "occupied_target": 20,
                "preheat_enabled": True,
                "frost_enabled": True,
                "frost_target": 7,
                "frost_while_off": False,
            }
            result = await flow.async_step_options(options)
            self.assertEqual(result["step_id"], "review")
            self.assertIn("New room", result["description_placeholders"]["summary"])
            self.assertEqual(self.calls, [])
            created = await flow.async_step_review({})
            self.assertEqual(created["type"], "create_entry")
            self.assertFalse(created["data"]["enabled"])
            self.assertEqual(created["data"]["initial_features"]["frost_target"], 7)
            self.assertEqual(created["data"]["initial_policy"]["comfort_target"], 20)
            self.assertEqual(len(created["data"]["initial_week"]), 7)

    async def test_slow_calendar_refresh_does_not_block_boost_or_flicker_profile(self):
        import asyncio
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        await self.configure_builtin()
        self.hass.states.async_set("calendar.test", "off")
        record = self.runtime.insights.read("zone_a", self.runtime.zones["zone_a"].limits)
        record["settings"]["profiles"] = {"Day off": [{"start": "00:00", "temperature": 19}]}
        record["settings"]["calendar_rules"] = [
            {"entity_id": "calendar.test", "match": "", "profile": "Day off"}
        ]
        await self.runtime.insights.save("zone_a", record)
        now = dt_util.utcnow()
        self.runtime._calendar_cache["calendar.test"] = (
            now - timedelta(seconds=1),
            [{"start": now - timedelta(hours=1), "end": now + timedelta(hours=1), "summary": ""}],
        )
        release = asyncio.Event()

        async def slow(*args):
            await release.wait()
            return {"calendar.test": {"events": []}}

        with patch.object(self.runtime, "_read_feature_service", slow):
            await asyncio.wait_for(self.runtime.async_evaluate(), 1)
            self.assertEqual(self.runtime.status["zone_a"]["target"], 19)
            self.assertTrue(self.runtime._feature_reads)
            await asyncio.wait_for(
                self.runtime.async_control_action(
                    "boost_start", "zone_a", {"minutes": 5, "target": 23}
                ),
                1,
            )
            self.assertEqual(self.runtime.status["zone_a"]["source"], "boost")
            release.set()
            await self.hass.async_block_till_done()
        self.assertEqual(self.runtime.status["zone_a"]["source"], "boost")
        self.assertEqual(self.calls, [])

    async def test_workday_future_dates_and_all_day_calendar_normalization(self):
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        from custom_components.smart_heating_scheduler.insight_controls import event_time

        await self.configure_builtin()
        self.hass.states.async_set("binary_sensor.test_workday", "on")
        record = self.runtime.insights.read("zone_a", self.runtime.zones["zone_a"].limits)
        settings = record["settings"]
        settings.update(
            profiles={
                "Workday": [{"start": "00:00", "temperature": 21}],
                "Day off": [{"start": "00:00", "temperature": 18}],
            },
            workday_entity="binary_sensor.test_workday",
            workday_profile="Workday",
            non_workday_profile="Day off",
        )
        reader = AsyncMock(return_value={"binary_sensor.test_workday": {"workday": False}})
        with patch.object(self.runtime, "_read_feature_service", reader):
            await self.runtime.async_control_action(
                "insights", "zone_a", {"revision": 1, "settings": settings}
            )
            await self.hass.async_block_till_done()
        self.assertEqual(reader.await_count, 6)
        self.assertEqual(self.runtime.status["zone_a"]["target"], 21)
        self.assertEqual(self.runtime.insight_status["zone_a"]["days"][1]["profile"], "Day off")
        tomorrow = (
            dt_util.utcnow().astimezone(self.runtime.controller.timezone).date() + timedelta(days=1)
        ).isoformat()
        self.assertIn(tomorrow, [call.args[2]["check_date"] for call in reader.call_args_list])
        start = event_time("2026-10-25", self.runtime.controller.timezone)
        end = event_time("2026-10-26", self.runtime.controller.timezone)
        self.assertEqual((end - start).total_seconds() / 3600, 25)
        self.assertEqual(self.calls, [])

    async def test_profile_settings_revision_restart_and_calendar_failure_fallback(self):
        await self.configure_builtin()
        self.hass.states.async_set("calendar.days_off", "off")
        record = self.runtime.insights.read("zone_a", self.runtime.zones["zone_a"].limits)
        settings = record["settings"]
        settings["profiles"] = {"Day off": [{"start": "00:00", "temperature": 19}]}
        settings["calendar_rules"] = [
            {"entity_id": "calendar.days_off", "match": "OFF", "profile": "Day off"}
        ]
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        now = dt_util.utcnow()
        response = {
            "calendar.days_off": {
                "events": [
                    {
                        "start": now.isoformat(),
                        "end": (now + timedelta(hours=1)).isoformat(),
                        "summary": "Private day OFF title",
                    }
                ]
            }
        }
        with patch.object(self.runtime, "_read_feature_service", AsyncMock(return_value=response)):
            await self.runtime.async_control_action(
                "insights", "zone_a", {"revision": 1, "settings": settings}
            )
            await self.hass.async_block_till_done()
        self.assertEqual(self.runtime.status["zone_a"]["target"], 19)
        self.assertEqual(self.runtime.insight_status["zone_a"]["active_profile"], "Day off")
        self.assertNotIn("Private day", json.dumps(self.runtime.insight_status))
        with self.assertRaises(ValidationError):
            await self.runtime.async_control_action(
                "insights", "zone_a", {"revision": 1, "settings": settings}
            )
            await self.hass.async_block_till_done()
        self.runtime.close()
        self.runtime = Runtime(self.hass, self.entry)
        self.entry.runtime_data = self.runtime
        self.hass.states.async_set("calendar.days_off", "unavailable")
        await self.runtime.async_setup()
        self.assertEqual(self.runtime.status["zone_a"]["target"], 20)
        self.assertTrue(self.runtime.insight_status["zone_a"]["warnings"])
        self.assertEqual(
            self.runtime.insights.read("zone_a", self.runtime.zones["zone_a"].limits)["revision"], 2
        )
        self.assertEqual(self.calls, [])

    async def test_preheat_follows_schedule_while_away_or_unknown_and_respects_explicit_away_target(
        self,
    ):
        from datetime import UTC, datetime, timedelta

        from custom_components.smart_heating_scheduler.const import DAYS
        from custom_components.smart_heating_scheduler.engine import Week

        await self.configure_builtin()
        now = (datetime.now(UTC) + timedelta(days=1)).replace(
            hour=6, minute=0, second=0, microsecond=0
        )
        zone = self.runtime.zones["zone_a"]
        periods = [{"start": "00:00", "temperature": 16}, {"start": "06:30", "temperature": 20}]
        await self.runtime.repository.save_week(
            zone.id, Week.parse({d: periods for d in DAYS}, zone.limits).to_dict(), zone.limits, 1
        )
        record = self.runtime.insights.read(zone.id, zone.limits)
        record["settings"]["preheat_enabled"] = True
        record["model"] = {"rate": 2, "samples": 12, "minutes": 60, "updated": now.isoformat()}
        await self.runtime.insights.save(zone.id, record)
        policy = self.runtime.smart_record(zone.id)
        policy["policy"].update(
            entities=[{"entity_id": "person.test", "home_state": "home", "away_state": "not_home"}],
            away_minutes=0,
            unknown_minutes=0,
            unknown_fallback="schedule",
        )
        await self.runtime.repository.smart_save(zone.id, policy, zone.limits)
        with patch(
            "custom_components.smart_heating_scheduler.runtime.dt_util.utcnow", return_value=now
        ):
            for state in ("not_home", "unknown"):
                self.hass.states.async_set("person.test", state)
                await self.runtime.async_evaluate()
                self.assertEqual(self.runtime.status[zone.id]["source"], "preheat")
                self.assertEqual(self.runtime.status[zone.id]["target"], 20)
            policy = self.runtime.smart_record(zone.id)
            policy["policy"]["away_target"] = 16
            await self.runtime.repository.smart_save(zone.id, policy, zone.limits)
            self.hass.states.async_set("person.test", "not_home")
            await self.runtime.async_evaluate()
            self.assertEqual(self.runtime.status[zone.id]["source"], "away")
            self.assertEqual(
                self.runtime.insight_status[zone.id]["preheat"]["pause_reason"],
                "Configured Away temperature is active",
            )
        self.assertEqual(self.calls, [])

    async def test_measured_preheating_writes_only_when_enabled_and_respects_off(self):
        from datetime import UTC, datetime, timedelta

        from custom_components.smart_heating_scheduler.const import DAYS
        from custom_components.smart_heating_scheduler.engine import Week

        self.simulated_receiver()
        await self.configure_builtin()
        now = (datetime.now(UTC) + timedelta(days=1)).replace(
            hour=6, minute=0, second=0, microsecond=0
        )
        periods = [{"start": "00:00", "temperature": 16}, {"start": "06:30", "temperature": 20}]
        zone = self.runtime.zones["zone_a"]
        await self.runtime.repository.save_week(
            zone.id, Week.parse({d: periods for d in DAYS}, zone.limits).to_dict(), zone.limits, 1
        )
        record = self.runtime.insights.read(zone.id, zone.limits)
        record["settings"]["preheat_enabled"] = True
        record["model"] = {"rate": 2, "samples": 12, "minutes": 60, "updated": now.isoformat()}
        await self.runtime.insights.save(zone.id, record)
        with patch(
            "custom_components.smart_heating_scheduler.runtime.dt_util.utcnow", return_value=now
        ):
            await self.runtime.async_evaluate()
        self.assertEqual(self.runtime.status[zone.id]["source"], "preheat")
        self.assertEqual(self.calls, [])  # observe-only still computes without commands
        await self.runtime.repository.set_control(True)
        self.hass.states.async_set(
            "climate.test_zone",
            "heat",
            {**self.attrs, "temperature": 16, "current_temperature": 19},
        )
        self.runtime.controller._previous.clear()
        with patch(
            "custom_components.smart_heating_scheduler.runtime.dt_util.utcnow",
            return_value=now + timedelta(minutes=1),
        ):
            await self.runtime.async_evaluate()
        self.assertEqual(self.hass.states.get("climate.test_zone").attributes["temperature"], 20)
        self.hass.states.async_set("climate.test_zone", "off", self.attrs)
        with patch(
            "custom_components.smart_heating_scheduler.runtime.dt_util.utcnow",
            return_value=now + timedelta(minutes=2),
        ):
            await self.runtime.async_evaluate()
        self.assertIn(self.runtime.status[zone.id]["source"], {"off", "manual_off"})
        self.assertEqual(self.hass.states.get("climate.test_zone").state, "off")

    async def test_insight_store_failure_never_sends_speculative_heat(self):
        from datetime import UTC, datetime, timedelta

        from custom_components.smart_heating_scheduler.const import DAYS
        from custom_components.smart_heating_scheduler.engine import Week

        await self.configure_builtin()
        zone = self.runtime.zones["zone_a"]
        now = (datetime.now(UTC) + timedelta(days=1)).replace(
            hour=6, minute=0, second=0, microsecond=0
        )
        periods = [{"start": "00:00", "temperature": 16}, {"start": "06:30", "temperature": 23}]
        await self.runtime.repository.save_week(
            zone.id, Week.parse({d: periods for d in DAYS}, zone.limits).to_dict(), zone.limits, 1
        )
        record = self.runtime.insights.read(zone.id, zone.limits)
        record["settings"]["preheat_enabled"] = True
        record["model"] = {"rate": 2, "samples": 12, "minutes": 60, "updated": now.isoformat()}
        await self.runtime.insights.save(zone.id, record)
        await self.runtime.repository.set_control(True)
        self.calls.clear()
        with (
            patch.object(self.runtime.insights, "save", AsyncMock(side_effect=OSError)),
            patch(
                "custom_components.smart_heating_scheduler.runtime.dt_util.utcnow", return_value=now
            ),
        ):
            await self.runtime.async_evaluate()
        self.assertEqual(self.runtime.status[zone.id]["target"], 16)
        self.assertTrue(self.runtime.insight_status[zone.id]["warnings"])
        self.assertTrue(all(call.data.get("temperature") != 23 for call in self.calls))
        policy = self.runtime.smart_record(zone.id)
        policy["policy"].update(
            entities=[{"entity_id": "person.test", "home_state": "home", "away_state": "not_home"}],
            comfort_target=18,
            comfort_days=list(DAYS),
            comfort_start="00:00",
            comfort_end="23:59",
        )
        await self.runtime.repository.smart_save(zone.id, policy, zone.limits)
        self.hass.states.async_set("person.test", "home")
        with (
            patch.object(self.runtime.insights, "save", AsyncMock(side_effect=OSError)),
            patch(
                "custom_components.smart_heating_scheduler.runtime.dt_util.utcnow", return_value=now
            ),
        ):
            await self.runtime.async_evaluate()
        self.assertEqual(self.runtime.status[zone.id]["target"], 18)
        self.assertEqual(self.runtime.status[zone.id]["source"], "occupied_comfort")

    async def test_reload_after_startup_does_not_unregister_consumed_listener(self):
        from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

        await self.runtime.async_setup()
        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await self.hass.async_block_till_done(wait_background_tasks=True)
        with self.assertNoLogs("homeassistant.core", level="ERROR"):
            self.runtime.close()
            self.runtime.close()

    async def test_zone_settings_clear_obsolete_timer_only_after_card_policy_migration(self):
        await self.configure_builtin()
        self.hass.states.async_remove("timer.test_boost")
        flow = ZoneSubentryFlow()
        flow.hass = self.hass
        subentry = self.entry.subentries["zone_a"]
        with (
            patch.object(ZoneSubentryFlow, "_get_entry", return_value=self.entry),
            patch.object(ZoneSubentryFlow, "_get_reconfigure_subentry", return_value=subentry),
            patch.object(
                ZoneSubentryFlow,
                "async_update_reload_and_abort",
                return_value={"type": "abort", "reason": "reconfigure_successful"},
            ) as update,
        ):
            form = await flow.async_step_settings()
            timer_field = next(
                key for key in form["data_schema"].schema if key.schema == "boost_timer"
            )
            self.assertEqual(timer_field.default(), "")
            requested = {**dict(subentry.data), "boost_timer": ""}
            result = await flow.async_step_settings(requested)
            self.assertEqual(result["reason"], "reconfigure_successful")
            self.assertEqual(update.call_args.kwargs["data"]["boost_timer"], "")
            update.reset_mock()
            self.hass.states.async_set("timer.unrelated", "idle")
            result = await flow.async_step_settings({**requested, "boost_timer": "timer.unrelated"})
            self.assertEqual(result["errors"]["base"], "invalid_zone")
            update.assert_not_called()
        self.assertEqual(self.calls, [])

    async def test_active_builtin_boost_restart_preserves_deadline_and_resumes_rules(self):
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        self.simulated_receiver()
        await self.configure_builtin()
        await self.enable_control()
        now = dt_util.utcnow()
        await self.runtime.async_control_action(
            "boost_start", "zone_a", {"target": 23, "minutes": 5}
        )
        deadline = self.runtime.smart_record("zone_a")["boost"]["until"]
        self.runtime.close()
        self.runtime = Runtime(self.hass, self.entry)
        self.entry.runtime_data = self.runtime
        with patch(
            "custom_components.smart_heating_scheduler.runtime.dt_util.utcnow",
            return_value=now + timedelta(minutes=2),
        ):
            await self.runtime.async_setup()
        self.assertTrue(self.runtime.repository.control_enabled)
        self.assertEqual(self.runtime.smart_record("zone_a")["boost"]["until"], deadline)
        self.assertEqual(self.runtime.status["zone_a"]["source"], "boost")
        self.assertEqual(self.hass.states.get("climate.test_zone").attributes["temperature"], 23)
        with patch(
            "custom_components.smart_heating_scheduler.runtime.dt_util.utcnow",
            return_value=now + timedelta(minutes=6),
        ):
            await self.runtime.async_evaluate()
        self.assertIsNone(self.runtime.smart_record("zone_a")["boost"])
        self.assertEqual(self.runtime.status["zone_a"]["source"], "schedule")
        self.assertEqual(self.hass.states.get("climate.test_zone").attributes["temperature"], 20)

    async def test_offline_holiday_return_waits_for_device_then_applies_current_rules(self):
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        self.simulated_receiver()
        await self.configure_builtin()
        await self.enable_control()
        now = dt_util.utcnow()
        await self.runtime.async_control_action(
            "holiday",
            "zone_a",
            {
                "start": now.isoformat(),
                "end": (now + timedelta(minutes=1)).isoformat(),
                "target": 17,
            },
        )
        self.assertEqual(self.hass.states.get("climate.test_zone").attributes["temperature"], 17)
        self.runtime.close()
        self.hass.states.async_set("climate.test_zone", "unavailable", self.attrs)
        self.calls.clear()
        self.runtime = Runtime(self.hass, self.entry)
        self.entry.runtime_data = self.runtime
        with patch(
            "custom_components.smart_heating_scheduler.runtime.dt_util.utcnow",
            return_value=now + timedelta(minutes=2),
        ):
            await self.runtime.async_setup()
            self.assertEqual(self.calls, [])
            self.assertEqual(self.runtime.status["zone_a"]["source"], "unavailable")
            self.hass.states.async_set(
                "climate.test_zone", "heat", {**self.attrs, "temperature": 17}
            )
            await self.runtime.async_evaluate()
        self.assertTrue(self.runtime.repository.control_enabled)
        self.assertIsNone(self.runtime.smart_record("zone_a")["holiday"])
        self.assertEqual(self.runtime.status["zone_a"]["source"], "schedule")
        self.assertEqual(self.hass.states.get("climate.test_zone").attributes["temperature"], 20)

    async def test_added_zone_subentry_triggers_integration_reload(self):
        from homeassistant.config_entries import ConfigSubentry

        from custom_components.smart_heating_scheduler import async_setup_entry

        self.hass.config_entries._entries[self.entry.entry_id] = self.entry
        with (
            patch.object(self.hass.config_entries, "async_forward_entry_setups", new=AsyncMock()),
            patch.object(
                self.hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
            ) as reload_entry,
        ):
            self.assertTrue(await async_setup_entry(self.hass, self.entry))
            self.runtime = self.entry.runtime_data
            added = ConfigSubentry(
                data=MappingProxyType(dict(self.entry.subentries["zone_a"].data)),
                subentry_type="zone",
                title="Second zone",
                unique_id="second_zone",
            )
            self.hass.config_entries.async_add_subentry(self.entry, added)
            await self.hass.async_block_till_done(wait_background_tasks=True)
            reload_entry.assert_awaited_once_with(self.entry.entry_id)

    async def test_card_boost_capability_tracks_availability_and_selected_owner(self):
        await self.configure_builtin()
        connection, _ = self.connection()
        self.assertTrue(
            card_api.dashboard(self.hass, self.entry, connection.user)["zones"][0]["boost"][
                "can_start"
            ]
        )
        self.hass.states.async_set("climate.test_zone", "unavailable", self.attrs)
        self.assertFalse(
            card_api.dashboard(self.hass, self.entry, connection.user)["zones"][0]["boost"][
                "can_start"
            ]
        )
        self.hass.states.async_set("climate.test_zone", "heat", self.attrs)
        p = self.runtime.smart_record("zone_a")["policy"]
        p["boost_owner"] = "none"
        await self.runtime.async_control_action("policy", "zone_a", {"policy": p, "revision": 2})
        self.assertFalse(
            card_api.dashboard(self.hass, self.entry, connection.user)["zones"][0]["boost"][
                "can_start"
            ]
        )

    async def test_off_while_controller_was_offline_does_not_resurrect_boost(self):
        self.simulated_receiver()
        await self.configure_builtin()
        await self.enable_control()
        await self.runtime.async_control_action("boost_start", "zone_a", {"target": 23})
        self.runtime.close()
        self.hass.states.async_set("climate.test_zone", "off", self.attrs)
        self.calls.clear()
        self.runtime = Runtime(self.hass, self.entry)
        self.entry.runtime_data = self.runtime
        await self.runtime.async_setup()
        self.assertIsNone(self.runtime.smart_record("zone_a")["boost"])
        self.assertEqual(self.hass.states.get("climate.test_zone").state, "off")
        self.assertEqual(self.calls, [])

    async def test_filtered_external_completion_releases_only_matching_idle_owner(self):
        await self.runtime.async_setup()
        p = self.runtime.smart_record("zone_a")["policy"]
        p.update(
            completion_event="test_boost_finished",
            completion_field="zone",
            completion_value="test_zone",
        )
        await self.runtime.async_control_action("policy", "zone_a", {"policy": p, "revision": 1})
        self.hass.states.async_set("timer.test_boost", "active")
        await self.runtime.async_evaluate()
        self.hass.bus.async_fire("test_boost_finished", {"zone": "test_zone"})
        await self.hass.async_block_till_done(wait_background_tasks=True)
        self.assertEqual(self.runtime.status["zone_a"]["source"], "external_boost")
        self.hass.states.async_set("timer.test_boost", "idle")
        await self.runtime.async_evaluate()
        self.hass.bus.async_fire("test_boost_finished", {"zone": "different"})
        await self.hass.async_block_till_done(wait_background_tasks=True)
        self.assertEqual(self.runtime.status["zone_a"]["source"], "boost_handover")
        self.hass.bus.async_fire("test_boost_finished", {"zone": "test_zone"})
        await self.hass.async_block_till_done(wait_background_tasks=True)
        self.assertEqual(self.runtime.status["zone_a"]["source"], "schedule")
        self.assertEqual(self.calls, [])

    async def test_external_ownership_survives_restart_when_timer_ended_offline(self):
        await self.runtime.async_setup()
        self.hass.states.async_set("timer.test_boost", "active")
        await self.runtime.async_evaluate()
        self.runtime.close()
        self.hass.states.async_set("timer.test_boost", "idle")
        self.runtime = Runtime(self.hass, self.entry)
        self.entry.runtime_data = self.runtime
        await self.runtime.async_setup()
        self.assertEqual(self.runtime.status["zone_a"]["source"], "boost_handover")
        self.assertEqual(self.calls, [])

    async def test_physical_off_cancels_builtin_boost_and_stays_off(self):
        self.simulated_receiver()
        await self.configure_builtin()
        await self.enable_control()
        await self.runtime.async_control_action("boost_start", "zone_a", {"target": 23})
        await self.hass.async_block_till_done(wait_background_tasks=True)
        self.hass.states.async_set("climate.test_zone", "off", self.attrs)
        await self.runtime.async_evaluate()
        self.assertIsNone(self.runtime.smart_record("zone_a")["boost"])
        self.assertEqual(self.runtime.status["zone_a"]["source"], "manual_off")
        self.assertEqual(self.hass.states.get("climate.test_zone").state, "off")

    async def test_simultaneous_boost_starts_are_serialized(self):
        import asyncio

        await self.configure_builtin()
        results = await asyncio.gather(
            self.runtime.async_control_action("boost_start", "zone_a", {"target": 23}),
            self.runtime.async_control_action("boost_start", "zone_a", {"target": 24}),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(r, ValidationError) for r in results), 1)
        self.assertIn(self.runtime.smart_record("zone_a")["boost"]["target"], (23, 24))
        self.assertEqual(self.calls, [])

    async def test_policy_revision_rejects_stale_editor_and_storage_failure_blocks_commands(self):
        await self.configure_builtin()
        p = self.runtime.smart_record("zone_a")["policy"]
        with self.assertRaises(ValidationError):
            await self.runtime.async_control_action(
                "policy", "zone_a", {"policy": p, "revision": 1}
            )
        original = self.runtime.repository.snapshot()
        with patch.object(
            self.runtime.repository._persistence, "async_save", side_effect=OSError("test failure")
        ):
            with self.assertRaises(OSError):
                await self.runtime.async_control_action("boost_start", "zone_a", {"target": 23})
        self.assertEqual(self.runtime.repository.snapshot(), original)
        self.assertEqual(self.calls, [])

    async def configure_builtin(self):
        await self.runtime.async_setup()
        policy = self.runtime.smart_record("zone_a")["policy"]
        policy.update(boost_owner="builtin", timer="", boost_target=23)
        await self.runtime.async_control_action(
            "policy",
            "zone_a",
            {"policy": policy, "revision": self.runtime.smart_record("zone_a")["policy_revision"]},
        )

    async def test_card_modes_are_guarded_and_generic_manual_off_resume_work(self):
        await self.configure_builtin()
        self.simulated_receiver()
        with self.assertRaises(ValidationError):
            await self.runtime.async_control_action(
                "thermostat_mode", "zone_a", {"mode": "manual", "target": 20}
            )
        await self.enable_control()
        await self.runtime.async_control_action(
            "thermostat_mode", "zone_a", {"mode": "manual", "target": 20}
        )
        self.assertEqual(self.hass.states.get("climate.test_zone").attributes["temperature"], 20)
        self.assertEqual(
            self.runtime.repository.read("zone_a", self.runtime.zones["zone_a"].limits)[1].kind,
            "manual",
        )
        await self.runtime.async_control_action("boost_start", "zone_a", {"target": 23})
        await self.runtime.async_control_action("thermostat_mode", "zone_a", {"mode": "off"})
        self.assertEqual(self.hass.states.get("climate.test_zone").state, "off")
        self.assertIsNone(self.runtime.smart_record("zone_a")["boost"])
        await self.runtime.async_control_action("resume", "zone_a", {})
        self.assertEqual(self.hass.states.get("climate.test_zone").state, "heat")

    async def test_card_mode_invalid_target_never_commands_a_thermostat(self):
        await self.configure_builtin()
        self.simulated_receiver()
        await self.enable_control()
        self.calls.clear()
        for payload in ({"mode": "bogus"}, {"mode": "manual", "target": 99}, {"mode": "manual"}):
            with self.assertRaises(ValidationError):
                await self.runtime.async_control_action("thermostat_mode", "zone_a", payload)
        self.assertEqual(self.calls, [])

    async def enable_control(self):
        await self.runtime.async_control_action(
            "mode", "zone_a", {"enabled": True, "acknowledgement": "ENABLE HEATING CONTROL"}
        )

    def simulated_receiver(self):
        async def apply(call):
            self.calls.append(call)
            current = self.hass.states.get("climate.test_zone")
            attrs = dict(current.attributes)
            mode = current.state
            if call.service == "set_temperature":
                attrs["temperature"] = call.data["temperature"]
            else:
                mode = call.data["hvac_mode"]
            self.hass.states.async_set("climate.test_zone", mode, attrs, context=call.context)

        self.hass.services.async_register("climate", "set_temperature", apply)
        self.hass.services.async_register("climate", "set_hvac_mode", apply)

    async def test_smart_boost_preview_persists_original_deadline_and_never_writes(self):
        await self.configure_builtin()
        await self.runtime.async_control_action(
            "boost_start", "zone_a", {"minutes": 30, "target": 23}
        )
        self.assertEqual(self.runtime.status["zone_a"]["source"], "boost")
        until = self.runtime.smart_record("zone_a")["boost"]["until"]
        await self.runtime.async_evaluate()
        self.assertEqual(self.runtime.smart_record("zone_a")["boost"]["until"], until)
        self.assertEqual(self.calls, [])
        await self.runtime.async_control_action("boost_cancel", "zone_a", {})
        self.assertEqual(self.runtime.status["zone_a"]["source"], "schedule")

    async def test_builtin_writer_own_context_does_not_create_manual_hold(self):
        self.simulated_receiver()
        await self.configure_builtin()
        await self.enable_control()
        await self.runtime.async_control_action(
            "boost_start", "zone_a", {"target": 23, "minutes": 30}
        )
        await self.hass.async_block_till_done(wait_background_tasks=True)
        self.assertEqual(self.hass.states.get("climate.test_zone").attributes["temperature"], 23)
        self.assertIsNone(
            self.runtime.repository.read("zone_a", self.runtime.zones["zone_a"].limits)[1]
        )
        await self.runtime.async_control_action("boost_cancel", "zone_a", {})
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        with patch(
            "custom_components.smart_heating_scheduler.runtime.dt_util.utcnow",
            return_value=dt_util.utcnow() + timedelta(seconds=6),
        ):
            await self.runtime.async_evaluate()
        self.assertEqual(self.hass.states.get("climate.test_zone").attributes["temperature"], 20)

    async def test_boost_from_off_returns_off_after_expiry_and_restart(self):
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        self.simulated_receiver()
        await self.configure_builtin()
        self.hass.states.async_set("climate.test_zone", "off", self.attrs)
        await self.runtime.async_evaluate()
        await self.enable_control()
        await self.runtime.async_control_action(
            "boost_start", "zone_a", {"target": 23, "minutes": 1}
        )
        self.assertEqual(self.hass.states.get("climate.test_zone").state, "heat")
        self.runtime.close()
        # Re-create runtime against the same HA Store; deadlines are not restarted.
        self.runtime = Runtime(self.hass, self.entry)
        self.entry.runtime_data = self.runtime
        with patch(
            "custom_components.smart_heating_scheduler.runtime.dt_util.utcnow",
            return_value=dt_util.utcnow() + timedelta(minutes=2),
        ):
            await self.runtime.async_setup()
        self.assertEqual(self.hass.states.get("climate.test_zone").state, "off")
        self.assertIsNone(self.runtime.smart_record("zone_a")["boost"])

    async def test_holiday_supersedes_manual_hold_and_respects_off(self):
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        from custom_components.smart_heating_scheduler.engine import Hold

        await self.configure_builtin()
        limits = self.runtime.zones["zone_a"].limits
        await self.runtime.repository.save_hold("zone_a", Hold("manual", 24, None), limits)
        now = dt_util.utcnow()
        await self.runtime.async_control_action(
            "holiday",
            "zone_a",
            {"start": now.isoformat(), "end": (now + timedelta(days=1)).isoformat(), "target": 17},
        )
        self.assertEqual(self.runtime.status["zone_a"]["target"], 17)
        self.assertIsNone(self.runtime.repository.read("zone_a", limits)[1])
        with self.assertRaises(ValidationError):
            await self.runtime.async_control_action("boost_start", "zone_a", {})
        self.hass.states.async_set("climate.test_zone", "off", self.attrs)
        await self.runtime.async_evaluate()
        self.assertEqual(self.runtime.status["zone_a"]["source"], "manual_off")
        await self.runtime.async_control_action("holiday", "zone_a", {"cancel": True})
        self.assertIsNone(self.runtime.status["zone_a"]["target"])
        self.assertEqual(self.calls, [])

    async def test_presence_forcing_unknown_delay_and_deadline_expiry(self):
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        await self.configure_builtin()
        self.hass.states.async_set(
            "person.test", "home", {"device_trackers": ["device_tracker.test"]}
        )
        p = self.runtime.smart_record("zone_a")["policy"]
        p.update(
            away_target=16,
            entities=[{"entity_id": "person.test", "home_state": "home", "away_state": "not_home"}],
        )
        await self.runtime.async_control_action(
            "policy",
            "zone_a",
            {"policy": p, "revision": self.runtime.smart_record("zone_a")["policy_revision"]},
        )
        self.assertEqual(self.runtime.smart_status["zone_a"]["occupancy"], "home")
        await self.runtime.async_control_action(
            "presence", "zone_a", {"mode": "away", "minutes": 1}
        )
        self.assertEqual(self.runtime.status["zone_a"]["target"], 16)
        with patch(
            "custom_components.smart_heating_scheduler.runtime.dt_util.utcnow",
            return_value=dt_util.utcnow() + timedelta(minutes=2),
        ):
            await self.runtime.async_evaluate()
        self.assertEqual(self.runtime.smart_status["zone_a"]["occupancy"], "home")
        self.assertIsNone(self.runtime.smart_record("zone_a")["override"])

    async def test_control_action_denies_non_admin_and_requires_mode_acknowledgement(self):
        await self.configure_builtin()
        connection, messages = self.connection(admin=False)
        card_api.control_action(self.hass, connection, {"id": 1, "action": "mode"})
        await self.hass.async_block_till_done(wait_background_tasks=True)
        self.assertEqual(messages, [("error", "unauthorized")])
        with self.assertRaises(ValidationError):
            await self.runtime.async_control_action("mode", "zone_a", {"enabled": True})
        self.assertFalse(self.runtime.repository.control_enabled)
        self.assertEqual(self.calls, [])

    async def test_external_scripts_are_awaited_and_automatic_release_inhibits_writer(self):
        await self.runtime.async_setup()
        self.hass.states.async_set("script.test_start", "off")
        self.hass.states.async_set("script.test_stop", "off")
        p = self.runtime.smart_record("zone_a")["policy"]
        p.update(
            boost_target=25, start_script="script.test_start", cancel_script="script.test_stop"
        )
        await self.runtime.async_control_action(
            "policy",
            "zone_a",
            {"policy": p, "revision": self.runtime.smart_record("zone_a")["policy_revision"]},
        )

        async def start(call):
            self.hass.states.async_set("timer.test_boost", "active")

        async def stop(call):
            self.hass.states.async_set("timer.test_boost", "idle")

        self.hass.services.async_register("script", "test_start", start)
        self.hass.services.async_register("script", "test_stop", stop)
        with self.assertRaises(ValidationError):
            await self.runtime.async_control_action("boost_start", "zone_a", {})
        await self.enable_control()
        await self.runtime.async_control_action("boost_start", "zone_a", {})
        self.assertEqual(self.runtime.status["zone_a"]["source"], "external_boost")
        await self.runtime.async_control_action("boost_cancel", "zone_a", {})
        self.assertIsNone(self.runtime.smart_record("zone_a")["external_operation"])
        await self.runtime.async_control_action("boost_start", "zone_a", {})
        self.hass.states.async_set("timer.test_boost", "idle")
        await self.runtime.async_evaluate()
        self.assertEqual(self.runtime.status["zone_a"]["source"], "boost_handover")
        await self.runtime.async_control_action("reconcile", "zone_a", {"confirmed": True})
        self.assertEqual(self.runtime.status["zone_a"]["source"], "schedule")

    async def test_unconfirmed_writer_stops_after_three_attempts(self):
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        await self.configure_builtin()
        await self.enable_control()
        await self.runtime.async_control_action("boost_start", "zone_a", {"target": 23})
        now = dt_util.utcnow()
        for seconds in (6, 22, 55, 100):
            with patch(
                "custom_components.smart_heating_scheduler.runtime.dt_util.utcnow",
                return_value=now + timedelta(seconds=seconds),
            ):
                await self.runtime.async_evaluate()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.runtime.status["zone_a"]["error"], "command_not_confirmed")

    def setUp(self):
        Path(".ha-check").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=".ha-check")
        self.config_dir = str(Path(self.temp.name).resolve())

    async def asyncSetUp(self):
        self.hass = HomeAssistant(self.config_dir)
        await self.hass.config.async_set_time_zone("Europe/London")
        self.hass.config_entries = ConfigEntries(self.hass, {})
        await self.hass.config_entries.async_initialize()
        dr.async_setup(self.hass)
        await dr.async_load(self.hass)
        await er.async_load(self.hass)
        self.attrs = {
            "hvac_modes": ["off", "heat"],
            "supported_features": 385,
            "min_temp": 5,
            "max_temp": 32,
            "temperature_unit": "°C",
            "target_temp_step": 0.5,
            "temperature": 20,
            "current_temperature": 19,
        }
        self.hass.states.async_set("climate.test_zone", "heat", self.attrs)
        self.hass.states.async_set("timer.test_boost", "idle")
        data = {
            "name": "Test zone",
            "climate_entity": "climate.test_zone",
            "minimum": 16,
            "maximum": 28,
            "step": 0.5,
            "unit": "°C",
            "baseline_temperature": 20,
            "enabled": True,
            "manual_policy": "next_period",
            "manual_minutes": 120,
            "boost_timer": "timer.test_boost",
            "climate_registry_id": None,
        }
        self.entry = ConfigEntry(
            domain=DOMAIN,
            title="Test scheduler",
            data={},
            options={},
            source="user",
            unique_id=None,
            version=1,
            minor_version=1,
            discovery_keys=MappingProxyType({}),
            subentries_data=[
                {
                    "subentry_id": "zone_a",
                    "subentry_type": "zone",
                    "title": "Test zone",
                    "unique_id": "climate.test_zone",
                    "data": data,
                }
            ],
        )
        self.runtime = Runtime(self.hass, self.entry)
        self.entry.runtime_data = self.runtime
        self.calls = []

        async def capture(call):
            self.calls.append(call)

        self.hass.services.async_register("climate", "set_temperature", capture)
        self.hass.services.async_register("climate", "set_hvac_mode", capture)

    async def asyncTearDown(self):
        self.runtime.close()
        await self.hass.async_block_till_done(wait_background_tasks=True)
        await self.hass.async_stop(force=True)
        # Stop listeners can schedule a final atomic Store write. Drain it before
        # removing the temporary config directory, especially on Windows.
        await self.hass.async_block_till_done(wait_background_tasks=True)
        # A cancelled evaluation can leave its already-running disk worker alive.
        # IsolatedAsyncioTestCase owns this loop, so join its executor before
        # deleting the fixture; the normal runner shutdown happens too late.
        await asyncio.get_running_loop().shutdown_default_executor()
        self.temp.cleanup()

    async def test_preview_setup_storage_sensors_and_unload_never_actuate(self):
        await self.runtime.async_setup()
        self.assertEqual(self.runtime.status["zone_a"]["target"], 20)
        self.assertTrue(self.runtime.status["zone_a"]["observe_only"])
        added = []

        def add(entities, **kwargs):
            added.extend(entities)
            self.assertEqual(kwargs["config_subentry_id"], "zone_a")

        await setup_sensors(self.hass, self.entry, add)
        self.assertEqual(len(added), 3)
        self.assertEqual(added[0].native_value, 20)
        self.assertTrue(added[0].extra_state_attributes["writes_inhibited"])
        stored = Path(self.temp.name, ".storage", f"{DOMAIN}.{self.entry.entry_id}")
        text = await self.hass.async_add_executor_job(stored.read_text)
        self.assertEqual(json.loads(text)["data"]["schema"], 2)
        self.runtime.close()
        self.assertEqual(self.calls, [])

    async def test_external_boost_and_missing_signal_block_schedule(self):
        await self.runtime.async_setup()
        self.hass.states.async_set("timer.test_boost", "active")
        await self.runtime.async_evaluate()
        self.assertEqual(self.runtime.status["zone_a"]["source"], "external_boost")
        self.hass.states.async_set("timer.test_boost", "unavailable")
        await self.runtime.async_evaluate()
        self.assertEqual(self.runtime.status["zone_a"]["source"], "boost_unknown")
        self.assertEqual(self.calls, [])

    async def test_config_forms_and_zone_validation_use_real_ha_selectors(self):
        flow = SchedulerConfigFlow()
        flow.hass = self.hass
        initial = await flow.async_step_user()
        self.assertEqual(initial["step_id"], "user")
        with patch.object(ZoneSubentryFlow, "_get_entry", return_value=self.entry):
            zone_flow = ZoneSubentryFlow()
            zone_flow.hass = self.hass
            form = await zone_flow.async_step_user()
            self.assertEqual(form["step_id"], "user")
            invalid = dict(self.entry.subentries["zone_a"].data)
            invalid["climate_entity"] = "climate.not_present"
            self.assertEqual(
                (await zone_flow.async_step_advanced(invalid))["errors"]["base"], "invalid_zone"
            )
        with self.assertRaises(ValidationError):
            validate_zone(self.hass, self.entry.subentries["zone_a"].data, self.entry.subentries)

    async def test_admin_action_rejects_non_admin_before_save(self):
        await self.runtime.async_setup()
        # Provide only a local fake auth user, while exercising HA's actual
        # administrator-service wrapper and service registry.
        self.hass.auth = SimpleNamespace(
            async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False))
        )
        async_register_services(self.hass)
        with self.assertRaises(Unauthorized):
            await self.hass.services.async_call(
                DOMAIN,
                "resume_preview",
                {"entry_id": self.entry.entry_id, "zone_id": "zone_a"},
                blocking=True,
                context=Context(user_id="not-an-admin"),
            )
        self.assertEqual(self.calls, [])

    def connection(self, *, admin=True, read=True, control=True, denied=()):
        messages = []
        user = SimpleNamespace(
            is_admin=admin,
            permissions=SimpleNamespace(
                check_entity=lambda entity_id, policy: (
                    entity_id not in denied and (read if policy == "read" else control)
                )
            ),
        )
        connection = SimpleNamespace(
            user=user,
            subscriptions={},
            send_result=lambda message_id, data=None: messages.append(("result", data)),
            send_event=lambda message_id, data: messages.append(("event", data)),
            send_error=lambda message_id, code, message: messages.append(("error", code)),
        )
        return connection, messages

    async def test_household_boost_start_cancel_without_configuration_access(self):
        await self.configure_builtin()
        connection, messages = self.connection(admin=False)
        snapshot = card_api.dashboard(self.hass, self.entry, connection.user)
        self.assertFalse(snapshot["can_edit"])
        self.assertTrue(snapshot["zones"][0]["can_boost"])
        with patch.object(card_api, "loaded_entry", return_value=self.entry):
            for action in ("boost_start", "boost_cancel"):
                card_api.control_action(
                    self.hass,
                    connection,
                    {
                        "id": 1,
                        "entry_id": "test",
                        "zone_id": "zone_a",
                        "action": action,
                        "payload": {"target": 23, "minutes": 15},
                    },
                )
                await self.hass.async_block_till_done(wait_background_tasks=True)
                self.assertEqual(messages[-1][0], "result")
                self.assertEqual(
                    bool(self.runtime.smart_record("zone_a")["boost"]), action == "boost_start"
                )
        self.assertEqual(self.calls, [])  # observe-only still never actuates

    async def test_household_boost_denies_missing_entity_permissions_and_unknown_zone(self):
        await self.configure_builtin()
        for permissions in (
            {"control": False},
            {"read": False},
            {"denied": ("climate.test_zone",)},
        ):
            connection, messages = self.connection(admin=False, **permissions)
            snapshot = card_api.dashboard(self.hass, self.entry, connection.user)
            self.assertTrue(not snapshot["zones"] or not snapshot["zones"][0]["can_boost"])
            with patch.object(card_api, "loaded_entry", return_value=self.entry):
                for zone_id in ("zone_a", "other_zone"):
                    for action in ("boost_start", "boost_cancel"):
                        card_api.control_action(
                            self.hass,
                            connection,
                            {
                                "id": 1,
                                "entry_id": "test",
                                "zone_id": zone_id,
                                "action": action,
                                "payload": {"target": 23, "minutes": 15},
                            },
                        )
                        await self.hass.async_block_till_done(wait_background_tasks=True)
                        self.assertEqual(messages[-1], ("error", "unauthorized"))
        self.assertIsNone(self.runtime.smart_record("zone_a")["boost"])
        self.assertEqual(self.calls, [])

    async def test_household_cannot_forge_configuration_or_unbounded_boost(self):
        await self.configure_builtin()
        connection, messages = self.connection(admin=False)
        with patch.object(card_api, "loaded_entry", return_value=self.entry):
            for action in (
                "policy",
                "presence",
                "holiday",
                "reconcile",
                "resume",
                "retry",
                "mode",
                "insights",
                "reset_learning",
                "clear_history",
                "restore_configuration",
            ):
                card_api.control_action(
                    self.hass,
                    connection,
                    {
                        "id": 1,
                        "entry_id": "test",
                        "zone_id": "zone_a",
                        "action": action,
                        "payload": {},
                    },
                )
                await self.hass.async_block_till_done(wait_background_tasks=True)
                self.assertEqual(messages[-1], ("error", "unauthorized"))
            card_api.control_action(
                self.hass,
                connection,
                {
                    "id": 1,
                    "entry_id": "test",
                    "zone_id": "zone_a",
                    "action": "boost_start",
                    "payload": {"minutes": 999999, "target": 99},
                },
            )
            await self.hass.async_block_till_done(wait_background_tasks=True)
            self.assertEqual(messages[-1], ("error", "invalid_control"))
        self.assertIsNone(self.runtime.smart_record("zone_a")["boost"])
        self.assertEqual(self.calls, [])

    async def test_external_boost_requires_adapter_permissions(self):
        await self.runtime.async_setup()
        policy = self.runtime.smart_record("zone_a")["policy"]
        policy.update(
            boost_owner="external",
            boost_target=25,
            start_script="script.test_start",
            cancel_script="script.test_stop",
            duration_helper="input_number.duration",
        )
        with patch.object(self.runtime, "smart_record", return_value={"policy": policy}):
            for denied in ("script.test_start", "script.test_stop", "input_number.duration"):
                connection, messages = self.connection(admin=False, denied=(denied,))
                self.assertFalse(card_api.can_boost(self.runtime, connection.user, "zone_a"))
                with patch.object(card_api, "loaded_entry", return_value=self.entry):
                    card_api.control_action(
                        self.hass,
                        connection,
                        {
                            "id": 1,
                            "entry_id": "test",
                            "zone_id": "zone_a",
                            "action": "boost_start",
                            "payload": {},
                        },
                    )
                    await self.hass.async_block_till_done(wait_background_tasks=True)
                    self.assertEqual(messages[-1], ("error", "unauthorized"))
            connection, _ = self.connection(admin=False)
            self.assertTrue(card_api.can_boost(self.runtime, connection.user, "zone_a"))
        self.assertEqual(self.calls, [])

    async def test_card_snapshot_filters_entity_permissions_and_has_no_location_data(self):
        await self.runtime.async_setup()
        connection, _ = self.connection()
        data = card_api.dashboard(self.hass, self.entry, connection.user)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["zones"][0]["current_temperature"], 19)
        self.assertEqual(data["zones"][0]["revision"], 1)
        self.assertFalse(data["zones"][0]["boost"]["can_start"])
        self.assertNotIn("latitude", json.dumps(data))
        restricted, _ = self.connection(admin=False, read=False)
        self.assertEqual(card_api.dashboard(self.hass, self.entry, restricted.user)["zones"], [])

    async def test_card_subscription_sends_updates_and_disposes(self):
        await self.runtime.async_setup()
        connection, messages = self.connection()
        with patch.object(card_api, "loaded_entry", return_value=self.entry):
            card_api.subscribe_dashboard(self.hass, connection, {"id": 1, "entry_id": "test"})
        self.assertEqual([kind for kind, _ in messages], ["result", "event"])
        await self.runtime.async_evaluate()
        self.assertEqual(messages[-1][0], "event")
        connection.subscriptions[1]()
        self.assertEqual(len(self.runtime.listeners), 0)

    async def test_card_write_endpoint_rejects_non_admin(self):
        connection, messages = self.connection(admin=False)
        with self.assertRaises(Unauthorized):
            card_api.save_schedule(self.hass, connection, {"id": 1})
        self.assertEqual(messages, [])

    async def test_receiver_endpoint_rejects_household_users_before_hardware_lookup(self):
        connection, messages = self.connection(admin=False)
        with self.assertRaises(Unauthorized):
            card_api.receiver_action(self.hass, connection, {"id": 1})
        self.assertEqual(messages, [])
        self.assertEqual(self.calls, [])

    async def test_receiver_write_endpoint_rechecks_control_permissions(self):
        await self.runtime.async_setup()
        connection, messages = self.connection(control=False)
        with patch.object(card_api, "loaded_entry", return_value=self.entry):
            card_api.receiver_action(
                self.hass,
                connection,
                {
                    "id": 1,
                    "entry_id": "test",
                    "zone_id": "zone_a",
                    "action": "write",
                    "payload": {"plan_id": "test", "acknowledgement": "WRITE RECEIVER TIMETABLE"},
                },
            )
            await self.hass.async_block_till_done(wait_background_tasks=True)
        self.assertEqual(messages[-1], ("error", "unauthorized"))
        self.assertEqual(self.calls, [])

    async def test_card_schedule_save_checks_revision_and_never_actuates(self):
        await self.runtime.async_setup()
        connection, messages = self.connection()
        week = self.runtime.repository.read("zone_a", self.runtime.zones["zone_a"].limits)[
            0
        ].to_dict()
        week["mon"][0]["temperature"] = 22
        with patch.object(card_api, "loaded_entry", return_value=self.entry):
            card_api.save_schedule(
                self.hass,
                connection,
                {"id": 1, "entry_id": "test", "zone_id": "zone_a", "revision": 1, "week": week},
            )
            await self.hass.async_block_till_done(wait_background_tasks=True)
            self.assertEqual(messages[-1][0], "result")
            self.assertEqual(messages[-1][1]["zones"][0]["revision"], 2)
            card_api.save_schedule(
                self.hass,
                connection,
                {"id": 2, "entry_id": "test", "zone_id": "zone_a", "revision": 1, "week": week},
            )
            await self.hass.async_block_till_done(wait_background_tasks=True)
        self.assertEqual(messages[-1], ("error", "revision_conflict"))
        self.assertEqual(self.calls, [])
