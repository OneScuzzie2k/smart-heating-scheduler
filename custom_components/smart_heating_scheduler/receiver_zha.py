"""Optional, heating-only Hive/ZHA schedule adapter. Imports ZHA lazily.

Schedule response collection follows Hive Local's MIT-licensed transport design;
see THIRD_PARTY_NOTICES.md. Mode writes are restricted to the guarded timer test;
reporting configuration is never changed.
"""

from __future__ import annotations

import asyncio

from .engine import ValidationError
from .native_receiver import STATE_ATTRIBUTES, control_attributes, receiver_state
from .native_schedule import ScheduleReplies, command
from .receiver_clock import CLOCK_ATTRIBUTES, validate_clock


class ZhaReceiver:
    def __init__(self, cluster, identity, *, timeout=20):
        self.cluster = cluster
        self.identity = identity
        self.timeout = timeout

    async def read_clock(self):
        try:
            clock = self.cluster.endpoint.in_clusters[0x000A]
            async with asyncio.timeout(self.timeout):
                values, failed = await clock.read_attributes(
                    list(CLOCK_ATTRIBUTES), allow_cache=False
                )
            if failed:
                raise ValidationError("Receiver cannot provide a complete fresh clock")
            return validate_clock(values)
        except ValidationError:
            raise
        except Exception as err:
            raise ValidationError("Fresh receiver clock read failed") from err

    async def synchronize_clock(self, values):
        # LocalTime is computed/read-only. Do not touch reporting/bindings.
        values = validate_clock({**values, "local_time": values.get("local_time", 0)})
        attributes = {key: value for key, value in values.items() if key != "local_time"}
        try:
            async with asyncio.timeout(self.timeout):
                result = await self.cluster.endpoint.in_clusters[0x000A].write_attributes(
                    attributes
                )
            if not result or any(record.status != 0 for record in result[0]):
                raise ValidationError("Receiver rejected clock synchronization; read it again")
        except ValidationError:
            raise
        except Exception as err:
            raise ValidationError("Receiver clock synchronization is unverified") from err

    async def read_state(self):
        try:
            async with asyncio.timeout(self.timeout):
                values, failed = await self.cluster.read_attributes(
                    list(STATE_ATTRIBUTES), allow_cache=False
                )
            if failed:
                raise ValidationError("Receiver does not support all timer-test attributes")
            return receiver_state(values)
        except ValidationError:
            raise
        except Exception as err:
            raise ValidationError("Fresh receiver mode read failed") from err

    async def write_control(self, attributes):
        """Internal validated mode operations, sent in ONE Write Attributes frame."""
        attributes = control_attributes(attributes)
        try:
            async with asyncio.timeout(self.timeout):
                result = await self.cluster.write_attributes(attributes)
            if not result or any(record.status != 0 for record in result[0]):
                raise ValidationError("Receiver rejected the mode command; verify its state")
        except ValidationError:
            raise
        except Exception as err:
            raise ValidationError("Receiver mode write is unverified") from err

    async def read(self):
        # Use the device's allocator (not an arbitrary constant). Ignore replies
        # from other request sequences, including earlier timed-out reads.
        tsn = self.cluster.endpoint.device.get_sequence()
        replies = ScheduleReplies(tsn)
        done = asyncio.Event()

        class Listener:
            def cluster_command(self, sequence, command_id, args):
                replies.accept(sequence, command_id, args)
                if replies.complete or replies.error:
                    done.set()

        listener = Listener()
        self.cluster.add_listener(listener)
        try:
            async with asyncio.timeout(self.timeout):
                await self.cluster.get_weekly_schedule(
                    days_to_return=127, mode_to_return=1, tsn=tsn, expect_reply=False
                )
                await done.wait()
        except TimeoutError as err:
            raise ValidationError(
                f"Receiver read incomplete: {len(replies.days)}/7 days; no schedule was written"
            ) from err
        except Exception as err:
            raise ValidationError(
                "Receiver schedule read failed; no timetable was written"
            ) from err
        finally:
            self.cluster.remove_listener(listener)
        if not replies.complete:
            raise ValidationError(replies.error or "Incomplete receiver read")
        return replies.days

    async def write_day(self, day, periods):
        payload = command(day, periods)
        try:
            async with asyncio.timeout(self.timeout):
                # A send acknowledgement is not persisted-schedule confirmation;
                # the caller must perform and compare a fresh complete readback.
                await self.cluster.set_weekly_schedule(**payload, expect_reply=False)
        except Exception as err:
            raise ValidationError("Receiver day write failed; read back before retrying") from err


def find_receiver(hass, entity_id, unit):
    """Bind only the mapped, registered ZHA climate on a known single-channel Hive."""
    if unit != "°C":
        raise ValidationError("Native Hive scheduling requires a Celsius zone")
    try:
        from homeassistant.components.zha.helpers import async_get_zha_device_proxy
        from homeassistant.helpers import entity_registry as er
    except ImportError as err:
        raise ValidationError("ZHA is not available; ordinary HA scheduling is unchanged") from err
    entity = er.async_get(hass).async_get(entity_id)
    if entity is None or entity.platform != "zha" or not entity.device_id:
        raise ValidationError("This zone does not map to a registered ZHA thermostat")
    try:
        device = async_get_zha_device_proxy(hass, entity.device_id).device
        model = (device.model or "").upper()
        manufacturer = (device.manufacturer or "").casefold()
        # Start with the verified single-channel family; never infer hot water
        # support from the presence of a second thermostat endpoint on an OTR1.
        if model not in {"OTR1", "SLR1", "SLR1B", "SLR1C", "SLR1D"} or manufacturer not in {
            "computime",
            "hive",
        }:
            raise ValidationError("Receiver model is outside the reviewed heating-only adapter")
        cluster = device.device.endpoints[5].in_clusters[0x0201]
        ieee = str(device.device.ieee)
        # Thermostat endpoints use IEEE-endpoint; other endpoint device types
        # retain IEEE-endpoint-cluster. Match either complete identifier.
        if entity.unique_id not in {f"{ieee}-5", f"{ieee}-5-513"}:
            raise ValidationError("The mapped climate is not the receiver's heating endpoint")
        return ZhaReceiver(
            cluster,
            {
                "device_id": entity.device_id,
                "ieee": ieee,
                "endpoint": 5,
                "model": model,
                "transport": "zha",
            },
        )
    except ValidationError:
        raise
    except (KeyError, ValueError, StopIteration, AttributeError) as err:
        raise ValidationError("The mapped Hive heating receiver is not available in ZHA") from err
