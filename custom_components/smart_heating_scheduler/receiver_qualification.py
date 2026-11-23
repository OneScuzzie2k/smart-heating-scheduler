"""Supervised, persisted two-minute hardware timer qualification.

This is not a schedule owner or an everyday boost adapter. It starts from an
indefinite Manual hold, uses the unchanged low target, observes native expiry,
then restores that exact hold. There is no arbitrary mode/setpoint API.
"""

import logging
import secrets
from copy import deepcopy
from datetime import timedelta
from zoneinfo import ZoneInfo

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .engine import ValidationError, timestamp
from .native_receiver import (
    boost_attributes,
    current_target,
    manual_attributes,
    same_control,
    test_preflight,
    validate_state,
)
from .native_schedule import digest, validate_native
from .repository import RevisionConflict

_LOGGER = logging.getLogger(__name__)
TEST_ACKNOWLEDGEMENT = "TEST RECEIVER TIMER"
ACTIVE = {"switching_schedule", "starting_boost", "waiting_expiry", "restoring"}
STATES = ACTIVE | {"prepared", "restored", "needs_review"}


def validate_qualification(q):
    if not isinstance(q, dict) or q["state"] not in STATES:
        raise ValidationError("Invalid receiver timer test record")
    manual_attributes(q["original"])
    validate_native(q["week"])
    if q["week_digest"] != digest(q["week"]) or not isinstance(q["identity"], dict):
        raise ValidationError("Invalid receiver timer test backup")
    if (
        not isinstance(q["id"], str)
        or not q["id"]
        or type(q["revision"]) is not int
        or q["revision"] < 1
        or not isinstance(q["mapping"], list)
        or len(q["mapping"]) != 3
        or not isinstance(q["mapping"][0], str)
        or not q["mapping"][0].startswith("climate.")
        or q["mapping"][2] != "°C"
        or not q["identity"]
    ):
        raise ValidationError("Invalid receiver timer test identity")
    ZoneInfo(q["timezone"])
    timestamp(q["updated_at"])
    if q.get("ignore_manual_until"):
        timestamp(q["ignore_manual_until"])
    if any(
        type(q[key]) is not bool
        for key in ("expiry_passed", "schedule_target_verified", "restoration_verified")
    ):
        raise ValidationError("Invalid receiver timer test result")
    if any(q.get(key) is not None and not isinstance(q[key], str) for key in ("error", "result")):
        raise ValidationError("Invalid receiver timer test explanation")
    timestamp(q["expires_at"])
    if q["state"] != "prepared":
        timestamp(q["deadline"])
    if not isinstance(q.get("observations", []), list) or len(q.get("observations", [])) > 40:
        raise ValidationError("Invalid receiver timer observations")
    for observation in q.get("observations", []):
        timestamp(observation["at"])
        validate_state(observation["hardware"])


class ReceiverQualification:
    def __init__(self, manager):
        self.manager = manager
        self.timers = {}
        self.recovery = set()

    @property
    def runtime(self):
        return self.manager.runtime

    def active(self, zone_id):
        return (
            self.manager.data["zones"].get(zone_id, {}).get("qualification", {}).get("state")
            in ACTIVE
        )

    def owns_observation(self, zone_id, now):
        q = self.manager.data["zones"].get(zone_id, {}).get("qualification", {})
        return self.active(zone_id) or bool(
            q.get("ignore_manual_until") and timestamp(q["ignore_manual_until"]) > now
        )

    def resume(self):
        if self.manager.error:
            return
        for zone_id in self.runtime.zones:
            if self.active(zone_id):
                self.recovery.add(zone_id)
                self._schedule(zone_id, seconds=1)

    def close(self):
        for cancel in self.timers.values():
            cancel()
        self.timers.clear()

    def _schedule(self, zone_id, *, seconds=15):
        if cancel := self.timers.pop(zone_id, None):
            cancel()
        if self.runtime._closed or self.runtime.hass is None:
            return

        async def tick(_now):
            self.timers.pop(zone_id, None)
            try:
                await self.manager.execute(zone_id, "check_test", {}, lambda: None)
            except (ValidationError, OSError, HomeAssistantError):
                _LOGGER.warning("Receiver timer qualification needs a fresh state read")
                if self.active(zone_id):
                    self._schedule(zone_id)

        self.timers[zone_id] = async_track_point_in_utc_time(
            self.runtime.hass, tick, dt_util.utcnow() + timedelta(seconds=seconds)
        )

    def _check(self, zone_id, authorize, q=None, *, write=False):
        zone = self.manager._check(zone_id, authorize, write=write)
        adapter = self.manager._adapter(zone_id)
        if q is not None:
            if (
                list(self.runtime.mappings[zone_id]) != q["mapping"]
                or adapter.identity != q["identity"]
            ):
                raise RevisionConflict("Timer test belongs to a different receiver mapping")
        return zone, adapter

    async def execute(self, zone_id, action, payload, authorize):
        if action == "inspect":
            return await self._inspect(zone_id, authorize)
        if action == "prepare_test":
            return await self._prepare(zone_id, payload, authorize)
        if action == "start_test":
            return await self._start(zone_id, payload, authorize)
        if action in {"check_test", "restore_test"}:
            return await self._check_test(zone_id, authorize, restore=action == "restore_test")
        raise ValidationError("Unknown receiver qualification action")

    async def _inspect(self, zone_id, authorize):
        _zone, adapter = self._check(zone_id, authorize)
        mapping = self.runtime.mappings[zone_id]
        hardware = await adapter.read_state()
        self.manager._check(zone_id, authorize, mapping=mapping)
        record = self.manager._record(zone_id)
        record["hardware"] = {"at": dt_util.utcnow().isoformat(), "state": hardware}
        await self.manager._save(zone_id, record)
        return self.manager.snapshot(zone_id, details=True)

    async def _prepare(self, zone_id, payload, authorize):
        if self.active(zone_id):
            raise ValidationError("Finish or restore the current receiver timer test first")
        zone, adapter = self._check(zone_id, authorize)
        revision = payload.get("revision")
        if type(revision) is not int:
            raise ValidationError("A current saved schedule revision is required")
        mapping = list(self.runtime.mappings[zone_id])
        original, week = await adapter.read_state(), validate_native(await adapter.read())
        self.manager._check(zone_id, authorize, revision=revision, mapping=tuple(mapping))
        now = dt_util.utcnow()
        timezone = self.runtime.hass.config.time_zone if self.runtime.hass is not None else "UTC"
        test_preflight(original, week, zone.limits, now.astimezone(ZoneInfo(timezone)))
        record = self.manager._record(zone_id)
        record["qualification"] = {
            "id": secrets.token_hex(16),
            "state": "prepared",
            "revision": revision,
            "identity": deepcopy(adapter.identity),
            "mapping": mapping,
            "timezone": timezone,
            "original": original,
            "week": week,
            "week_digest": digest(week),
            "expires_at": (now + timedelta(minutes=10)).isoformat(),
            "observations": [],
            "result": None,
            "error": None,
            "expiry_passed": False,
            "schedule_target_verified": False,
            "restoration_verified": False,
        }
        await self.manager._save(zone_id, record)
        return self.manager.snapshot(zone_id, details=True)

    async def _start(self, zone_id, payload, authorize):
        record = self.manager._record(zone_id)
        q = record.get("qualification")
        if not q or q["state"] != "prepared" or payload.get("test_id") != q["id"]:
            raise RevisionConflict("Prepare a fresh receiver timer test")
        if payload.get("acknowledgement") != TEST_ACKNOWLEDGEMENT:
            raise ValidationError(f"Type {TEST_ACKNOWLEDGEMENT} to confirm the two-minute test")
        zone, adapter = self._check(zone_id, authorize, q, write=True)
        self.manager._check(zone_id, authorize, revision=q["revision"])
        original, week = await adapter.read_state(), validate_native(await adapter.read())
        self._check(zone_id, authorize, q, write=True)
        now = dt_util.utcnow()
        if now >= timestamp(q["expires_at"]):
            raise RevisionConflict("Receiver timer test preview expired")
        if not same_control(original, q["original"]) or digest(week) != q["week_digest"]:
            raise RevisionConflict("Receiver mode or timetable changed; prepare the test again")
        test_preflight(original, week, zone.limits, now.astimezone(ZoneInfo(q["timezone"])))
        q.update(
            state="switching_schedule",
            started_at=now.isoformat(),
            deadline=(now + timedelta(minutes=2)).isoformat(),
        )
        # The exact original hold and timetable are durable before any mode command.
        await self.manager._save(zone_id, record)
        self._schedule(zone_id)
        try:
            self._check(zone_id, authorize, q, write=True)
            await adapter.write_control({"system_mode": 4, "temp_setpoint_hold": 0})
            scheduled = await adapter.read_state()
            self._check(zone_id, authorize, q, write=True)
            self._observe(q, scheduled, recovery=False)
            if scheduled["mode"] != "schedule" or scheduled["target"] > original["target"]:
                raise ValidationError("Receiver did not enter the reviewed low Schedule state")
            q["state"] = "starting_boost"
            await self.manager._save(zone_id, record)
            self._check(zone_id, authorize, q, write=True)
            await adapter.write_control(boost_attributes(original["target"]))
            boosted = await adapter.read_state()
            self._check(zone_id, authorize, q, write=True)
            self._observe(q, boosted, recovery=False)
            if (
                boosted["mode"] != "boost"
                or boosted["target"] != original["target"]
                or not 1 <= boosted["duration"] <= 2
            ):
                raise ValidationError(
                    "Receiver did not confirm a bounded two-minute hardware countdown"
                )
            q["state"] = "waiting_expiry"
            # Deadline starts when the boost was sent, not before Schedule readback.
            q["deadline"] = (dt_util.utcnow() + timedelta(minutes=2)).isoformat()
            await self.manager._save(zone_id, record)
        except (ValidationError, OSError, HomeAssistantError) as err:
            q.update(result="start_failed", error=str(err))
            await self.manager._save(zone_id, record)
            await self._restore(zone_id, record, authorize)
            raise
        return self.manager.snapshot(zone_id, details=True)

    @staticmethod
    def _observe(q, hardware, *, recovery):
        q["observations"] = [
            *q["observations"],
            {
                "at": dt_util.utcnow().isoformat(),
                "hardware": deepcopy(hardware),
                "after_reload": recovery,
            },
        ][-40:]

    async def _check_test(self, zone_id, authorize, *, restore):
        record = self.manager._record(zone_id)
        q = record.get("qualification")
        if not q or q["state"] == "prepared":
            raise ValidationError("There is no started receiver timer test")
        if restore:
            q["result"] = q["result"] or "cancelled"
            await self._restore(zone_id, record, authorize)
            return self.manager.snapshot(zone_id, details=True)
        if q["state"] not in ACTIVE:
            return self.manager.snapshot(zone_id, details=True)
        _zone, adapter = self._check(zone_id, authorize, q)
        hardware = await adapter.read_state()
        self._check(zone_id, authorize, q)
        recovery = zone_id in self.recovery
        self.recovery.discard(zone_id)
        self._observe(q, hardware, recovery=recovery)
        now = dt_util.utcnow()
        if q["state"] != "waiting_expiry":
            q.update(
                result=q["result"] or "interrupted",
                error=q["error"] or "Resuming an interrupted mode operation",
            )
        elif hardware["mode"] == "schedule":
            q["expiry_passed"] = now >= timestamp(q["deadline"]) - timedelta(seconds=65)
            expected = current_target(q["week"], now.astimezone(ZoneInfo(q["timezone"])))
            q["schedule_target_verified"] = hardware["target"] == expected
            q["result"] = "returned_to_schedule" if q["expiry_passed"] else "ended_early"
        elif (
            hardware["mode"] == "manual"
            and hardware["target"] == q["original"]["target"]
            and hardware["duration"] in {0, 65535}
        ):
            q.update(
                result="returned_to_manual",
                error=(
                    "Receiver returned to Manual, not Schedule; "
                    "native schedule expiry is not proven"
                ),
            )
        elif (
            hardware["mode"] != "boost"
            or hardware["target"] != q["original"]["target"]
            or hardware["duration"] not in {0, 1, 2, 65535}
        ):
            q.update(
                state="needs_review",
                result="changed_externally",
                error="Receiver changed outside this test; no restoration command was sent",
            )
            await self.manager._save(zone_id, record)
            return self.manager.snapshot(zone_id, details=True)
        elif not 0 <= hardware["duration"] <= 2:
            q.update(result="countdown_invalid", error="Hardware countdown became unbounded")
        elif now > timestamp(q["deadline"]) + timedelta(seconds=90):
            q.update(
                result="expiry_failed",
                error="Hardware boost outlived the two-minute test and grace period",
            )
        else:
            await self.manager._save(zone_id, record)
            self._schedule(zone_id)
            return self.manager.snapshot(zone_id, details=True)
        await self.manager._save(zone_id, record)
        await self._restore(zone_id, record, authorize)
        return self.manager.snapshot(zone_id, details=True)

    async def _restore(self, zone_id, record, authorize):
        q = record["qualification"]
        _zone, adapter = self._check(zone_id, authorize, q, write=True)
        current = await adapter.read_state()
        self._check(zone_id, authorize, q, write=True)
        if same_control(current, q["original"]):
            q.update(state="restored", restoration_verified=True)
        elif current["mode"] == "schedule" or (
            current["mode"] in {"boost", "manual"}
            and current["target"] == q["original"]["target"]
            and current["duration"]
            in ({0, 1, 2, 65535} if current["mode"] == "boost" else {0, 65535})
        ):
            q["state"] = "restoring"
            await self.manager._save(zone_id, record)
            self._check(zone_id, authorize, q, write=True)
            await adapter.write_control(manual_attributes(q["original"]))
            restored = await adapter.read_state()
            self._observe(q, restored, recovery=False)
            self._check(zone_id, authorize, q, write=True)
            if not same_control(restored, q["original"]):
                q.update(
                    state="needs_review", error="Original receiver hold restoration is unverified"
                )
            else:
                q.update(state="restored", restoration_verified=True)
        else:
            q.update(
                state="needs_review",
                error="Receiver changed outside this test; original hold was not forced",
            )
        if q["restoration_verified"]:
            q["ignore_manual_until"] = (dt_util.utcnow() + timedelta(seconds=10)).isoformat()
        await self.manager._save(zone_id, record)
        if cancel := self.timers.pop(zone_id, None):
            cancel()
