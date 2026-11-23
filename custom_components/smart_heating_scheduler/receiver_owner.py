"""Explicit receiver baseline ownership and finite HA overlay leases.

The ownership marker lives in the MAIN scheduler store. Corrupt optional receiver
backups cannot silently turn the old HA weekly writer back on. Every radio write
has a durable intent and fresh readback. Physical Off/Manual/unknown boosts win.
"""

import asyncio
from copy import deepcopy
from datetime import timedelta

from homeassistant.util import dt as dt_util

from .engine import ValidationError, Week, timestamp
from .native_receiver import boost_attributes, current_target, validate_state
from .native_schedule import changes, digest, from_week, to_week, validate_native
from .receiver_clock import clock_report, expected_clock
from .repository import RevisionConflict

OWNER_ACK = "USE RECEIVER SCHEDULE"


def validate_owner(owner):
    if not isinstance(owner, dict) or owner["state"] not in {
        "activating",
        "active",
        "writing_week",
        "uncertain",
        "returning",
    }:
        raise ValidationError("Invalid native receiver ownership marker")
    validate_native(owner["week"])
    if digest(owner["week"]) != owner["digest"] or not owner["identity"]:
        raise ValidationError("Native ownership timetable or identity is invalid")
    if not isinstance(owner["mapping"], list) or len(owner["mapping"]) != 3:
        raise ValidationError("Native receiver mapping is invalid")
    if type(owner["revision"]) is not int or owner["revision"] < 1:
        raise ValidationError("Native receiver revision is invalid")
    timestamp(owner["at"])
    timestamp(owner["clock_checked"])
    if lease := owner.get("lease"):
        if (
            type(lease["target"]) is not int
            or not 500 <= lease["target"] <= 3200
            or lease["target"] % 50
        ):
            raise ValidationError("Invalid native overlay lease")
        timestamp(lease["sent_at"])
    if fallback := owner.get("fallback"):
        validate_state(fallback["original"])
        if type(fallback["target"]) is not int or not 500 <= fallback["target"] <= 3200:
            raise ValidationError("Invalid native Manual/Off boost recovery")


class ReceiverOwner:
    def __init__(self, manager):
        self.manager = manager
        self.hardware = {}

    @property
    def runtime(self):
        return self.manager.runtime

    def record(self, zid):
        return self.runtime.repository.native_read(zid)

    def summary(self, zid):
        owner = self.record(zid)
        if not owner:
            return None
        return {
            key: deepcopy(owner.get(key))
            for key in ("state", "revision", "timezone", "error", "lease", "clock_checked", "at")
        } | {"hardware": deepcopy(self.hardware.get(zid))}

    def check(self, zid, owner=None):
        if self.runtime._closed:
            raise ValidationError("Scheduler is unloading")
        if self.manager.error:
            raise ValidationError(self.manager.error)
        adapter = self.manager._adapter(zid)
        owner = owner or self.record(zid)
        if owner and (
            adapter.identity != owner["identity"]
            or list(self.runtime.mappings[zid]) != owner["mapping"]
        ):
            raise RevisionConflict("Native receiver mapping changed; heating writes are paused")
        return adapter

    async def save(self, zid, owner):
        owner["at"] = dt_util.utcnow().isoformat()
        await self.runtime.repository.native_save(zid, owner)

    async def observe(self, zone, now):
        owner = self.record(zone.id)
        if not owner:
            return None
        async with self.manager.locks.setdefault(zone.id, asyncio.Lock()):
            adapter = self.check(zone.id, owner)
            state = validate_state(await adapter.read_state())
            self.hardware[zone.id] = state
            if owner.get("lease") and (
                state["mode"] in {"off", "manual"}
                or state["mode"] == "boost"
                and not self.owned_boost(owner, state)
            ):
                owner["lease"] = None
                await self.save(zone.id, owner)
            return state

    async def action(self, zid, action, payload, authorize):
        # Always runtime lock -> receiver lock, matching evaluation/schedule-save.
        async with self.runtime._lock:
            async with self.manager.locks.setdefault(zid, asyncio.Lock()):
                zone = self.manager._check(zid, authorize, write=True)
                if not self.runtime.repository.control_enabled:
                    raise ValidationError(
                        "Enable heating control before changing receiver ownership"
                    )
                adapter = self.check(zid)
                if self.manager.qualification.active(zid):
                    raise ValidationError("Finish the hardware test before changing ownership")
                if action == "enable_owner":
                    if self.record(zid):
                        raise ValidationError("This zone already has a receiver ownership marker")
                    if payload.get("acknowledgement") != OWNER_ACK:
                        raise ValidationError(
                            f"Type {OWNER_ACK} after pausing competing controllers"
                        )
                    self.manager._check(zid, authorize, revision=payload["revision"])
                    record = self.manager._record(zid)
                    timer, transition = (
                        record.get("qualification", {}),
                        record.get("transition", {}),
                    )
                    if not (
                        timer.get("expiry_passed")
                        and timer.get("restoration_verified")
                        and transition.get("first_observed")
                        and transition.get("second_observed")
                        and transition.get("restoration_verified")
                        and not transition.get("error")
                        and timer.get("identity") == adapter.identity == transition.get("identity")
                    ):
                        raise ValidationError(
                            "Verify native timer and both clock transitions for this receiver first"
                        )
                    week, hold, revision = self.runtime.repository.read(zid, zone.limits)
                    if self.runtime.smart_record(zid)["policy"]["boost_owner"] == "external":
                        raise ValidationError(
                            "Select built-in or disabled boosts before native handover"
                        )
                    if hold or self.runtime.smart_record(zid)["boost"]:
                        raise ValidationError(
                            "Clear manual holds and boosts before receiver handover"
                        )
                    native = from_week(week.to_dict(), zone.limits, self.runtime.mappings[zid][2])
                    actual = validate_native(await adapter.read())
                    if actual != native:
                        raise ValidationError(
                            "Copy and verify the saved HA week on this receiver before handover"
                        )
                    hardware = await adapter.read_state()
                    if hardware["mode"] != "manual":
                        raise ValidationError(
                            "Receiver handover starts from its reviewed Manual hold"
                        )
                    timezone = self.runtime.hass.config.time_zone
                    clock = clock_report(await adapter.read_clock(), timezone, dt_util.utcnow())
                    if not clock["verified"]:
                        raise ValidationError(
                            "Receiver clock and daylight-saving rules need verification"
                        )
                    owner = {
                        "state": "activating",
                        "week": native,
                        "digest": digest(native),
                        "identity": adapter.identity,
                        "mapping": list(self.runtime.mappings[zid]),
                        "revision": revision,
                        "timezone": timezone,
                        "clock_checked": dt_util.utcnow().isoformat(),
                        "lease": None,
                        "error": None,
                        "original": hardware,
                    }
                    await self.save(zid, owner)  # Fail-closed marker BEFORE any mode command.
                    try:
                        self.manager._check(zid, authorize, write=True, revision=revision)
                        await adapter.write_control({"system_mode": 4, "temp_setpoint_hold": 0})
                        current = await adapter.read_state()
                        if current["mode"] != "schedule" or current["target"] != current_target(
                            native, dt_util.utcnow().astimezone(self.runtime.controller.timezone)
                        ):
                            raise ValidationError("Native Schedule activation is not verified")
                        self.hardware[zid] = current
                        owner.update(state="active", error=None)
                        await self.save(zid, owner)
                    except Exception:
                        owner.update(
                            state="uncertain",
                            error="Activation unverified; HA baseline writes remain blocked",
                        )
                        await self.save(zid, owner)
                        raise
                elif action == "adopt_owner_week":
                    owner = self.record(zid)
                    snapshot = self.manager._record(zid).get("snapshot")
                    if not owner or not snapshot or snapshot["identity"] != adapter.identity:
                        raise ValidationError("Read this receiver timetable before importing it")
                    if (
                        payload.get("acknowledgement") != "USE RECEIVER TIMETABLE"
                        or payload.get("snapshot_digest") != snapshot["digest"]
                    ):
                        raise ValidationError("Confirm the current receiver timetable preview")
                    self.manager._check(zid, authorize, revision=payload["revision"])
                    actual = validate_native(await adapter.read())
                    if digest(actual) != snapshot["digest"]:
                        raise RevisionConflict("Receiver timetable changed since it was read")
                    imported = to_week(actual, zone.limits)
                    owner.update(
                        state="active",
                        week=actual,
                        digest=digest(actual),
                        revision=payload["revision"] + 1,
                        error=None,
                    )
                    self.manager._check(zid, authorize, revision=payload["revision"])
                    await self.runtime.repository.native_save(
                        zid, owner, week=imported, revision=payload["revision"], limits=zone.limits
                    )
                elif action == "disable_owner":
                    owner = self.record(zid)
                    if not owner:
                        raise ValidationError("This zone is already HA-owned")
                    if payload.get("acknowledgement") != "USE HA SCHEDULE":
                        raise ValidationError("Type USE HA SCHEDULE to return ownership")
                    hardware = await adapter.read_state()
                    if hardware["mode"] == "boost":
                        raise ValidationError("Finish the active native overlay or boost first")
                    owner["state"] = "returning"
                    await self.save(zid, owner)
                    if hardware["mode"] != "off":
                        target = int(
                            round(
                                self.runtime.repository.read(zid, zone.limits)[0]
                                .current(dt_util.utcnow(), self.runtime.controller.timezone)
                                .temperature
                                * 100
                            )
                        )
                        attrs = {
                            "system_mode": 4,
                            "temp_setpoint_hold": 1,
                            "temp_setpoint_hold_duration": 65535,
                            "occupied_heating_setpoint": target,
                        }
                        self.manager._check(zid, authorize, write=True)
                        await adapter.write_control(attrs)
                        confirmed = await adapter.read_state()
                        if confirmed["mode"] != "manual" or confirmed["target"] != target:
                            raise ValidationError("HA Manual hold handback is unverified")
                    await self.runtime.repository.native_save(zid, None)
        return self.manager.snapshot(zid, details=True)

    async def resume(self, zid):
        owner = self.record(zid)
        if not owner:
            return
        async with self.manager.locks.setdefault(zid, asyncio.Lock()):
            adapter = self.check(zid, owner)
            week, _hold, revision = self.runtime.repository.read(
                zid, self.runtime.zones[zid].limits
            )
            if (
                revision != owner["revision"]
                or from_week(
                    week.to_dict(), self.runtime.zones[zid].limits, self.runtime.mappings[zid][2]
                )
                != owner["week"]
            ):
                raise ValidationError(
                    "Review and import the receiver week before resuming; saved schedules differ"
                )
            if await adapter.read() != owner["week"]:
                raise ValidationError(
                    "Receiver week differs from its verified copy; reconcile the timetable first"
                )
            current = await adapter.read_state()
            if not clock_report(
                await adapter.read_clock(), self.runtime.hass.config.time_zone, dt_util.utcnow()
            )["verified"]:
                raise ValidationError("Receiver clock needs review before resuming Schedule")
            if current["mode"] == "boost" and not self.owned_boost(owner, current):
                raise ValidationError("An external native boost must finish before resuming")
            # Explicit Resume is permission to leave physical Off/Manual.
            owner.update(state="activating", error=None, lease=None)
            await self.save(zid, owner)
            await adapter.write_control({"system_mode": 4, "temp_setpoint_hold": 0})
            actual = await adapter.read_state()
            if actual["mode"] != "schedule":
                raise ValidationError("Receiver did not confirm Schedule mode")
            owner["state"] = "active"
            await self.save(zid, owner)
            self.hardware[zid] = actual

    @staticmethod
    def owned_boost(owner, hardware):
        lease = owner.get("lease")
        return bool(
            lease
            and hardware["mode"] == "boost"
            and hardware["target"] == lease["target"]
            and hardware["duration"] in {0, 1, 2}
        )

    async def write(self, zone, decision, status, now):
        zid = zone.id
        owner = self.record(zid)
        if not owner:
            return False  # Existing generic HA writer.
        status["next_evaluation"] = min(status["next_evaluation"], now + timedelta(seconds=30))
        if not zone.enabled or decision.source in {
            "disabled",
            "unavailable",
            "unsupported",
            "error",
        }:
            return True
        async with self.manager.locks.setdefault(zid, asyncio.Lock()):
            adapter = self.check(zid, owner)
            hardware = self.hardware.get(zid) or await adapter.read_state()
            if owner["state"] != "active":
                status.update(
                    error="receiver_ownership_uncertain",
                    source="receiver_review",
                    reason=owner.get("error")
                    or "Receiver ownership requires explicit reconciliation",
                )
                return True
            cycle = self.runtime.insights.read(zid, zone.limits).get("frost_cycle")
            # These existing special cases retain their HA-dependent return
            # semantics. Only Schedule-origin overlays promise native fallback.
            if decision.source in {"frost", "frost_return_off"} and cycle:
                if decision.source == "frost_return_off":
                    await adapter.write_control({"system_mode": 0, "temp_setpoint_hold": 0})
                    actual = await adapter.read_state()
                    if actual["mode"] != "off":
                        raise ValidationError("Original Off restoration is unverified")
                else:
                    target = int(round(decision.target * 100))
                    if hardware["mode"] == "manual" and hardware["target"] == target:
                        return True
                    await adapter.write_control(
                        {
                            "system_mode": 4,
                            "temp_setpoint_hold": 1,
                            "temp_setpoint_hold_duration": 65535,
                            "occupied_heating_setpoint": target,
                        }
                    )
                    actual = await adapter.read_state()
                    if actual["mode"] != "manual" or actual["target"] != target:
                        raise ValidationError("Temporary native frost heating is unverified")
                self.hardware[zid] = actual
                return True
            smart = self.runtime.smart_record(zid)
            fallback = owner.get("fallback")
            if fallback and not smart["boost"]:
                if hardware["mode"] == "manual" and hardware["target"] == fallback["target"]:
                    original = fallback["original"]
                    attrs = (
                        {"system_mode": 0, "temp_setpoint_hold": 0}
                        if original["mode"] == "off"
                        else {
                            "system_mode": 4,
                            "temp_setpoint_hold": 1,
                            "temp_setpoint_hold_duration": original["duration"],
                            "occupied_heating_setpoint": original["target"],
                        }
                    )
                    await adapter.write_control(attrs)
                    actual = await adapter.read_state()
                    if (
                        actual["mode"] != original["mode"]
                        or original["mode"] == "manual"
                        and actual["target"] != original["target"]
                    ):
                        raise ValidationError("Original Manual/Off boost restoration is unverified")
                    self.hardware[zid] = hardware = actual
                owner.pop("fallback", None)
                await self.save(zid, owner)
                fallback = None
            if (
                decision.source == "boost"
                and smart["boost"]
                and hardware["mode"] in {"manual", "off"}
            ):
                target = int(round(decision.target * 100))
                if not fallback:
                    owner["fallback"] = fallback = {"original": hardware, "target": target}
                    await self.save(zid, owner)
                    if hardware["mode"] == "off":
                        await adapter.write_control(
                            {
                                "system_mode": 4,
                                "temp_setpoint_hold": 1,
                                "temp_setpoint_hold_duration": 65535,
                                "occupied_heating_setpoint": target,
                            }
                        )
                if hardware["mode"] == "off" and fallback["original"]["mode"] != "off":
                    return True  # Physical Off during an existing Manual-origin boost.
                if hardware["target"] not in {fallback["original"]["target"], target}:
                    return True  # A different manual dial setting belongs to its user.
                return False
            if hardware["mode"] in {"off", "manual"}:
                status.update(
                    source="off" if hardware["mode"] == "off" else "manual",
                    target=None if hardware["mode"] == "off" else hardware["target"] / 100,
                    reason="Physical receiver mode; choose Resume schedule to return",
                )
                return True
            owned = self.owned_boost(owner, hardware)
            if hardware["mode"] == "boost" and not owned:
                status.update(
                    source="external_boost",
                    target=hardware["target"] / 100,
                    reason="A native boost outside this scheduler owns the receiver",
                )
                return True
            if (
                timestamp(owner["clock_checked"]) + timedelta(hours=12) <= now
                or owner["timezone"] != self.runtime.hass.config.time_zone
            ):
                timezone = self.runtime.hass.config.time_zone
                clock = clock_report(await adapter.read_clock(), timezone, dt_util.utcnow())
                if not clock["verified"]:
                    await adapter.synchronize_clock(
                        {**expected_clock(timezone, dt_util.utcnow()), "time_status": 2}
                    )
                    clock = clock_report(await adapter.read_clock(), timezone, dt_util.utcnow())
                    if not clock["verified"]:
                        status.update(
                            error="receiver_clock",
                            reason="Receiver clock verification failed; no new overrides sent",
                        )
                        return True
                owner.update(clock_checked=dt_util.utcnow().isoformat(), timezone=timezone)
                await self.save(zid, owner)
            baseline = current_target(
                owner["week"], now.astimezone(self.runtime.controller.timezone)
            )
            # A dial change on native Schedule is an override until its next
            # hardware boundary. Never stamp it out with a weekly target write.
            if (
                hardware["mode"] == "schedule"
                and hardware["target"] != baseline
                and not owner.get("lease")
            ):
                status.update(
                    source="manual",
                    target=hardware["target"] / 100,
                    reason="Receiver schedule temperature adjusted; native boundary will resume it",
                )
                return True
            overlay = (
                not decision.inhibited
                and decision.target is not None
                and (int(round(decision.target * 100)) != baseline or decision.source == "boost")
            )
            if not overlay:
                if owned:
                    await adapter.write_control({"system_mode": 4, "temp_setpoint_hold": 0})
                    actual = await adapter.read_state()
                    if actual["mode"] != "schedule" or actual["target"] != baseline:
                        raise ValidationError("Native overlay release is unverified")
                    owner["lease"] = None
                    await self.save(zid, owner)
                    self.hardware[zid] = actual
                elif owner.get("lease") and hardware["mode"] == "schedule":
                    owner["lease"] = None
                    await self.save(zid, owner)
                return True
            target = int(round(decision.target * 100))
            if (
                owned
                and owner["lease"]["target"] == target
                and now < timestamp(owner["lease"]["sent_at"]) + timedelta(seconds=55)
            ):
                return True
            owner["lease"] = {
                "target": target,
                "source": decision.source,
                "sent_at": now.isoformat(),
            }
            await self.save(zid, owner)  # Intent is durable before bounded radio write.
            try:
                await adapter.write_control(boost_attributes(target))
                actual = await adapter.read_state()
                if not self.owned_boost(owner, actual) or actual["duration"] not in {1, 2}:
                    raise ValidationError("Finite native override countdown is unverified")
                self.hardware[zid] = actual
            except Exception:
                owner.update(
                    state="uncertain",
                    error="Native override unverified; hardware expiry may still occur",
                )
                await self.save(zid, owner)
                raise
        return True

    async def save_week(self, zid, data, revision):
        async with self.runtime._lock:
            return await self.save_week_locked(zid, data, revision)

    async def save_week_locked(self, zid, data, revision):
        zone = self.runtime.zones[zid]
        parsed = Week.parse(data, zone.limits)
        after = from_week(parsed.to_dict(), zone.limits, self.runtime.mappings[zid][2])
        async with self.manager.locks.setdefault(zid, asyncio.Lock()):
            owner = self.record(zid)
            if not owner:
                raise RevisionConflict("Receiver ownership changed; reload before saving")
            adapter = self.check(zid, owner)
            if (
                owner["state"] != "active"
                or self.runtime.repository.read(zid, zone.limits)[2] != revision
            ):
                raise RevisionConflict("Receiver or schedule needs review; reload before saving")
            before = await adapter.read()
            if before != owner["week"]:
                raise RevisionConflict("Receiver week changed elsewhere; read and compare it first")
            owner.update(state="writing_week", pending_week=after)
            await self.save(zid, owner)
            try:
                for day in changes(before, after):
                    self.check(zid, owner)
                    await adapter.write_day(day, after[day])
                if await adapter.read() != after:
                    raise ValidationError(
                        "Receiver timetable readback differs; saved week is unverified"
                    )
                owner.update(
                    state="active",
                    week=after,
                    digest=digest(after),
                    revision=revision + 1,
                    error=None,
                )
                owner.pop("pending_week", None)
                await self.runtime.repository.native_save(
                    zid, owner, week=parsed.to_dict(), revision=revision, limits=zone.limits
                )
            except Exception:
                owner.update(
                    state="uncertain",
                    error="Receiver write interrupted; HA baseline fallback remains disabled",
                )
                await self.save(zid, owner)
                raise
        return revision + 1
