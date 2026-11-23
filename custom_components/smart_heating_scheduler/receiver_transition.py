"""Supervised low-temperature clock-boundary test with durable restoration."""

import secrets
from copy import deepcopy
from datetime import timedelta
from zoneinfo import ZoneInfo

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import DAYS
from .engine import ValidationError, timestamp
from .native_receiver import manual_attributes, same_control
from .native_schedule import validate_native
from .receiver_clock import clock_report
from .repository import RevisionConflict

ACK = "TEST RECEIVER TRANSITIONS"
ACTIVE = {"writing", "waiting", "restoring"}


def validate_transition(q):
    if q["state"] not in ACTIVE | {"prepared", "restored", "needs_review"}:
        raise ValidationError("Invalid transition test state")
    manual_attributes(q["original"])
    validate_native(q["before"])
    validate_native(q["test_week"])
    for key in ("first", "second", "expires_at", "updated_at"):
        timestamp(q[key])
    if q["day"] not in DAYS or not isinstance(q["observations"], list):
        raise ValidationError("Invalid transition test backup")
    if len(q["observations"]) > 80 or any(
        type(q[k]) is not bool
        for k in ("first_observed", "second_observed", "restoration_verified")
    ):
        raise ValidationError("Invalid transition test observations")


class ReceiverTransition:
    def __init__(self, manager):
        self.manager = manager
        self.timers = {}

    def active(self, zid):
        return self.manager.data["zones"].get(zid, {}).get("transition", {}).get("state") in ACTIVE

    def close(self):
        for cancel in self.timers.values():
            cancel()
        self.timers.clear()

    def resume(self):
        for zid in self.manager.runtime.zones:
            if self.active(zid):
                self.schedule(zid, 1)

    def schedule(self, zid, seconds=10):
        if cancel := self.timers.pop(zid, None):
            cancel()
        runtime = self.manager.runtime
        if runtime._closed or runtime.hass is None:
            return

        async def check(_now):
            self.timers.pop(zid, None)
            try:
                await self.manager.execute(zid, "check_transition", {}, lambda: None)
            except (ValidationError, OSError, HomeAssistantError):
                if self.active(zid):
                    self.schedule(zid)

        self.timers[zid] = async_track_point_in_utc_time(
            runtime.hass, check, dt_util.utcnow() + timedelta(seconds=seconds)
        )

    def check(self, zid, authorize, q=None, write=False):
        zone = self.manager._check(zid, authorize, write=write)
        adapter = self.manager._adapter(zid)
        if q and (
            adapter.identity != q["identity"]
            or list(self.manager.runtime.mappings[zid]) != q["mapping"]
        ):
            raise RevisionConflict("Transition test receiver mapping changed")
        return zone, adapter

    async def execute(self, zid, action, payload, authorize):
        if action == "prepare_transition":
            if self.manager.qualification.active(zid):
                raise ValidationError("Finish the current hardware test first")
            zone, adapter = self.check(zid, authorize)
            self.manager._check(zid, authorize, revision=payload["revision"])
            record = self.manager._record(zid)
            timer = record.get("qualification", {})
            if not timer.get("expiry_passed") or not timer.get("restoration_verified"):
                raise ValidationError("Verify the native timer and original hold first")
            original = await adapter.read_state()
            manual_attributes(original)
            if original["room"] < original["target"] + 100 or original["target"] < 550:
                raise ValidationError("Transition test needs a room at least 1°C above its hold")
            before = validate_native(await adapter.read())
            now = dt_util.utcnow()
            timezone = self.manager.runtime.hass.config.time_zone
            clock = clock_report(await adapter.read_clock(), timezone, dt_util.utcnow())
            if not clock["verified"]:
                raise ValidationError("Synchronize and verify the receiver clock first")
            first = now.replace(second=0, microsecond=0) + timedelta(minutes=3)
            second = first + timedelta(minutes=2)
            local = first.astimezone(ZoneInfo(timezone))
            if (
                second.astimezone(ZoneInfo(timezone)).date()
                != now.astimezone(ZoneInfo(timezone)).date()
            ):
                raise ValidationError("Run this short test away from midnight")
            day = DAYS[local.weekday()]
            test_week = deepcopy(before)
            test_week[day] = [
                {"minute": 0, "centi": original["target"]},
                {"minute": local.hour * 60 + local.minute, "centi": original["target"] - 50},
                {"minute": local.hour * 60 + local.minute + 2, "centi": original["target"]},
            ]
            self.manager._check(zid, authorize, revision=payload["revision"])
            record["transition"] = {
                "id": secrets.token_hex(16),
                "state": "prepared",
                "original": original,
                "before": before,
                "test_week": test_week,
                "day": day,
                "first": first.isoformat(),
                "second": second.isoformat(),
                "expires_at": (first - timedelta(seconds=45)).isoformat(),
                "identity": adapter.identity,
                "mapping": list(self.manager.runtime.mappings[zid]),
                "revision": payload["revision"],
                "timezone": timezone,
                "first_observed": False,
                "second_observed": False,
                "restoration_verified": False,
                "observations": [],
                "error": None,
            }
            await self.manager._save(zid, record)
            return self.manager.snapshot(zid, details=True)
        record = self.manager._record(zid)
        q = record.get("transition")
        if not q:
            raise ValidationError("Prepare a transition test first")
        if action == "start_transition":
            if self.manager.data["zones"].get(zid, {}).get("qualification", {}).get("state") in {
                "switching_schedule",
                "starting_boost",
                "waiting_expiry",
                "restoring",
            }:
                raise ValidationError("Finish the native timer test first")
            if q["state"] != "prepared" or payload.get("test_id") != q["id"]:
                raise RevisionConflict("Prepare a fresh transition test")
            if payload.get("acknowledgement") != ACK:
                raise ValidationError(f"Type {ACK} to confirm")
            _zone, adapter = self.check(zid, authorize, q, write=True)
            self.manager._check(zid, authorize, revision=q["revision"])
            current = await adapter.read_state()
            week = await adapter.read()
            clock = clock_report(await adapter.read_clock(), q["timezone"], dt_util.utcnow())
            if (
                dt_util.utcnow() >= timestamp(q["expires_at"])
                or not same_control(current, q["original"])
                or week != q["before"]
                or current["room"] < current["target"] + 100
                or not clock["verified"]
            ):
                raise RevisionConflict("Receiver or test window changed; prepare again")
            q["state"] = "writing"
            await self.manager._save(zid, record)
            self.schedule(zid)
            try:
                self.check(zid, authorize, q, write=True)
                for day in DAYS:
                    self.check(zid, authorize, q, write=True)
                    await adapter.write_day(day, q["test_week"][day])
                if await adapter.read() != q["test_week"]:
                    raise ValidationError("Temporary timetable readback did not match")
                self.check(zid, authorize, q, write=True)
                await adapter.write_control({"system_mode": 4, "temp_setpoint_hold": 0})
                hardware = await adapter.read_state()
                if hardware["mode"] != "schedule" or hardware["target"] != q["original"]["target"]:
                    raise ValidationError("Receiver did not enter the low test Schedule")
                q["state"] = "waiting"
                q["observations"].append({"at": dt_util.utcnow().isoformat(), "hardware": hardware})
                await self.manager._save(zid, record)
            except (ValidationError, OSError, HomeAssistantError) as err:
                q["error"] = str(err)
                await self.manager._save(zid, record)
                await self.restore(zid, record, authorize)
                raise
        elif action == "restore_transition":
            await self.restore(zid, record, authorize)
        elif action == "check_transition" and self.active(zid):
            _zone, adapter = self.check(zid, authorize, q)
            hardware = await adapter.read_state()
            now = dt_util.utcnow()
            q["observations"] = [*q["observations"], {"at": now.isoformat(), "hardware": hardware}][
                -80:
            ]
            if q["state"] != "waiting":
                q["error"] = "Recovered interrupted transition operation; test is not proven"
                await self.manager._save(zid, record)
                await self.restore(zid, record, authorize)
                return self.manager.snapshot(zid, details=True)
            if hardware["mode"] != "schedule":
                q.update(
                    state="needs_review",
                    error="Receiver mode changed externally; restore was not forced",
                )
            elif q["state"] == "waiting":
                first, second = timestamp(q["first"]), timestamp(q["second"])
                if first <= now < second and hardware["target"] == q["original"]["target"] - 50:
                    q["first_observed"] = True
                if second <= now and hardware["target"] == q["original"]["target"]:
                    q["second_observed"] = True
                if q["first_observed"] and q["second_observed"]:
                    await self.manager._save(zid, record)
                    await self.restore(zid, record, authorize)
                    return self.manager.snapshot(zid, details=True)
                if now > second + timedelta(seconds=60):
                    q["error"] = "Both clock-driven target transitions were not observed"
                    await self.manager._save(zid, record)
                    await self.restore(zid, record, authorize)
                    return self.manager.snapshot(zid, details=True)
            else:
                q["error"] = "Recovered an interrupted transition operation; test is not proven"
                await self.manager._save(zid, record)
                await self.restore(zid, record, authorize)
                return self.manager.snapshot(zid, details=True)
            await self.manager._save(zid, record)
            if self.active(zid):
                self.schedule(zid)
        return self.manager.snapshot(zid, details=True)

    async def restore(self, zid, record, authorize):
        q = record["transition"]
        _zone, adapter = self.check(zid, authorize, q, write=True)
        week, hardware = await adapter.read(), await adapter.read_state()
        if week not in (q["test_week"], q["before"]) or not (
            hardware["mode"] == "schedule" or same_control(hardware, q["original"])
        ):
            q.update(
                state="needs_review",
                error="Receiver changed outside the test; original state was not forced",
            )
        else:
            q["state"] = "restoring"
            await self.manager._save(zid, record)
            self.check(zid, authorize, q, write=True)
            if week != q["before"]:
                for day in DAYS:
                    self.check(zid, authorize, q, write=True)
                    await adapter.write_day(day, q["before"][day])
            if await adapter.read() != q["before"]:
                raise ValidationError("Original timetable restoration is unverified")
            self.check(zid, authorize, q, write=True)
            if not same_control(hardware, q["original"]):
                await adapter.write_control(manual_attributes(q["original"]))
            restored = await adapter.read_state()
            q["restoration_verified"] = same_control(restored, q["original"])
            q["state"] = "restored" if q["restoration_verified"] else "needs_review"
            q["observations"].append({"at": dt_util.utcnow().isoformat(), "hardware": restored})
        await self.manager._save(zid, record)
        if cancel := self.timers.pop(zid, None):
            cancel()
