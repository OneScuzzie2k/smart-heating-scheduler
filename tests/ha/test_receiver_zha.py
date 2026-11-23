"""Check real target ZHA identifiers and Zigpy wire schemas without a radio."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    from homeassistant.helpers import entity_registry
    from zigpy.zcl.clusters.hvac import Thermostat
except ImportError as error:
    raise unittest.SkipTest("Install the target Home Assistant and ZHA libraries") from error

from custom_components.smart_heating_scheduler.engine import ValidationError
from custom_components.smart_heating_scheduler.native_schedule import ScheduleReplies, command
from custom_components.smart_heating_scheduler.receiver_zha import find_receiver


class ZhaSchemaTests(unittest.TestCase):
    def test_actual_command_and_response_wire_roundtrip(self):
        schema = Thermostat.ServerCommandDefs.set_weekly_schedule.schema
        payload = command("mon", [{"minute": 0, "centi": 1600}, {"minute": 390, "centi": 2000}])
        encoded = schema(**payload).serialize()
        response_schema = Thermostat.ClientCommandDefs.get_weekly_schedule_response.schema
        response, extra = response_schema.deserialize(encoded)
        self.assertFalse(extra)
        replies = ScheduleReplies(42)
        replies.accept(42, 0, response)
        self.assertIsNone(replies.error)
        self.assertEqual(
            replies.days["mon"], [{"minute": 0, "centi": 1600}, {"minute": 390, "centi": 2000}]
        )

    def test_only_exact_registered_heating_endpoint_is_bound(self):
        ieee = "00:11:22:33:44:55:66:77"
        cluster = object()
        device = SimpleNamespace(
            model="OTR1",
            manufacturer="Computime",
            device=SimpleNamespace(
                ieee=ieee,
                endpoints={5: SimpleNamespace(in_clusters={0x201: cluster})},
            ),
        )
        entity = SimpleNamespace(platform="zha", device_id="registered", unique_id=f"{ieee}-5")
        registry = SimpleNamespace(async_get=lambda _: entity)
        helpers = SimpleNamespace(
            async_get_zha_device_proxy=lambda *_: SimpleNamespace(device=device)
        )
        with (
            patch.object(entity_registry, "async_get", return_value=registry),
            patch.dict("sys.modules", {"homeassistant.components.zha.helpers": helpers}),
        ):
            for suffix in ("5", "5-513"):
                entity.unique_id = f"{ieee}-{suffix}"
                self.assertIs(find_receiver(None, "climate.test", "°C").cluster, cluster)
            for suffix in ("6", "6-513", "5-513-foreign"):
                entity.unique_id = f"{ieee}-{suffix}"
                with self.assertRaises(ValidationError):
                    find_receiver(None, "climate.test", "°C")
            with self.assertRaises(ValidationError):
                find_receiver(None, "climate.test", "°F")
