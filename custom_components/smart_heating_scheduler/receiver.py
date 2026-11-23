"""Staged receiver read/write and bounded timer qualification with durable backups.

An administrator may inspect a running zone. Hardware writes require a disabled
zone, a fresh reviewed preview and a matching read of the original timetable.
Permanent receiver ownership and everyday timed overlays are not enabled here.
"""

from __future__ import annotations

import asyncio
import secrets
from copy import deepcopy
from datetime import timedelta

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from . import native_schedule as native
from .engine import ValidationError, timestamp
from .native_receiver import validate_state
from .receiver_clock import clock_report, expected_clock, validate_clock
from .receiver_owner import ReceiverOwner
from .receiver_qualification import ReceiverQualification, validate_qualification
from .receiver_transition import ReceiverTransition, validate_transition
from .receiver_zha import find_receiver
from .repository import RevisionConflict

WRITE_ACKNOWLEDGEMENT = "WRITE RECEIVER TIMETABLE"


class ReceiverSchedules:
    def __init__(self, runtime, store, factory=find_receiver):
        self.runtime, self.store, self.factory = runtime, store, factory
        self.data = {"schema": 1, "zones": {}}
        self.error = None
        self.locks = {}
        self.tasks = set()
        self._storage_lock = asyncio.Lock()
        self.qualification = ReceiverQualification(self)
        self.transition = ReceiverTransition(self)
        self.owner = ReceiverOwner(self)

    async def load(self):
        try:
            data = await self.store.async_load()
            if data is None:
                return
            if (
                not isinstance(data, dict)
                or set(data) != {"schema", "zones"}
                or type(data["schema"]) is not int
                or data["schema"] != 1
                or not isinstance(data["zones"], dict)
            ):
                raise ValidationError("Invalid receiver backup storage")
            for record in data["zones"].values():
                if not isinstance(record, dict) or set(record) - {
                    "snapshot",
                    "plan",
                    "transactions",
                    "hardware",
                    "qualification",
                    "clock",
                    "transition",
                }:
                    raise ValidationError("Invalid receiver backup record")
                if any(
                    key in record and not isinstance(record[key], dict)
                    for key in ("snapshot", "plan")
                ):
                    raise ValidationError("Invalid receiver snapshot or preview")
                if record.get("snapshot"):
                    snapshot = record["snapshot"]
                    native.validate_native(snapshot["schedule"])
                    timestamp(snapshot["at"])
                    if snapshot["digest"] != native.digest(snapshot["schedule"]):
                        raise ValidationError("Receiver snapshot digest mismatch")
                if record.get("plan"):
                    self._validate_plan(record["plan"])
                transactions = record.get("transactions", [])
                if not isinstance(transactions, list) or len(transactions) > 5:
                    raise ValidationError("Invalid receiver transaction history")
                for transaction in transactions:
                    self._validate_plan(transaction)
                    if transaction["state"] not in {"writing", "verified", "uncertain"}:
                        raise ValidationError("Invalid receiver transaction state")
                    if not isinstance(transaction["attempted_days"], list) or any(
                        day not in transaction["changed_days"]
                        for day in transaction["attempted_days"]
                    ):
                        raise ValidationError("Invalid attempted receiver days")
                if "hardware" in record:
                    timestamp(record["hardware"]["at"])
                    validate_state(record["hardware"]["state"])
                if "qualification" in record:
                    validate_qualification(record["qualification"])
                if "clock" in record:
                    validate_clock(record["clock"]["values"])
                if "transition" in record:
                    validate_transition(record["transition"])
            self.data = deepcopy(data)
        except (ValidationError, KeyError, TypeError, OSError, AttributeError, HomeAssistantError):
            # Optional receiver inspection must not break existing HA schedules.
            # Do not overwrite unrecognised backup storage on a later UI action.
            self.error = "Receiver backup storage needs review; existing HA scheduling is unchanged"

    @staticmethod
    def _validate_plan(plan):
        native.validate_native(plan["before"])
        native.validate_native(plan["after"])
        timestamp(plan["expires_at"])
        if (
            not isinstance(plan["id"], str)
            or not plan["id"]
            or not isinstance(plan["identity"], dict)
            or not plan["identity"]
            or type(plan["revision"]) is not int
            or plan["revision"] < 1
            or plan["changed_days"] != native.changes(plan["before"], plan["after"])
        ):
            raise ValidationError("Invalid receiver preview metadata")

    def snapshot(self, zone_id, *, details=False):
        record = deepcopy(self.data["zones"].get(zone_id, {}))
        transactions = record.get("transactions", [])
        state = transactions[-1]["state"] if transactions else "not_read"
        if record.get("plan"):
            state = "preview"
        elif record.get("snapshot") and not transactions:
            state = "read_complete"
        result = {
            "state": "error" if self.error else state,
            "error": self.error,
            "schedule_owner": "receiver" if self.owner.record(zone_id) else "home_assistant",
            "handover_available": bool(
                record.get("transition", {}).get("first_observed")
                and record.get("transition", {}).get("second_observed")
                and record.get("transition", {}).get("restoration_verified")
                and not record.get("transition", {}).get("error")
                and record.get("qualification", {}).get("expiry_passed")
                and record.get("qualification", {}).get("restoration_verified")
            ),
            "ownership": self.owner.summary(zone_id),
            "read_at": record.get("snapshot", {}).get("at"),
            "backup_count": len(transactions),
        }
        q = record.get("qualification")
        result["clock"] = record.get("clock")
        result["transition"] = record.get("transition")
        result["qualification"] = (
            {
                key: q.get(key)
                for key in (
                    "state",
                    "result",
                    "error",
                    "expiry_passed",
                    "schedule_target_verified",
                    "restoration_verified",
                    "deadline",
                    "updated_at",
                )
            }
            if q
            else None
        )
        if q and q.get("observations"):
            result["qualification"]["latest"] = q["observations"][-1]
        if details:
            result.update(record)
        return result

    def _check(self, zone_id, authorize, *, write=False, revision=None, mapping=None):
        authorize()
        runtime = self.runtime
        if self.error:
            raise ValidationError(self.error)
        if runtime._closed or zone_id not in runtime.zones:
            raise ValidationError("Scheduler zone is no longer loaded")
        zone = runtime.zones[zone_id]
        if mapping is not None and runtime.mappings.get(zone_id) != mapping:
            raise RevisionConflict("Receiver mapping changed during the operation")
        if revision is not None and runtime.repository.read(zone_id, zone.limits)[2] != revision:
            raise RevisionConflict("HA schedule changed; prepare a fresh receiver preview")
        if write:
            smart = runtime.smart_record(zone_id)
            observed = runtime._observation(zone_id)
            if zone.enabled:
                raise ValidationError(
                    "Disable this zone's HA scheduling before writing its receiver"
                )
            if (
                smart["boost"]
                or smart["external_operation"]
                or observed.external_boost
                or observed.boost_unknown
            ):
                raise ValidationError("Finish all boost operations before writing the receiver")
        return zone

    def _adapter(self, zone_id):
        entity_id, _timer, unit = self.runtime.mappings[zone_id]
        return self.factory(self.runtime.hass, entity_id, unit)

    async def _save(self, zone_id, record):
        async with self._storage_lock:
            if q := record.get("qualification"):
                q["updated_at"] = dt_util.utcnow().isoformat()
            if q := record.get("transition"):
                q["updated_at"] = dt_util.utcnow().isoformat()
            candidate = deepcopy(self.data)
            candidate["zones"][zone_id] = deepcopy(record)
            await self.store.async_save(candidate)
            self.data = candidate

    def _record(self, zone_id):
        return deepcopy(self.data["zones"].get(zone_id, {"transactions": []}))

    async def execute(self, zone_id, action, payload, authorize):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            if action in {"enable_owner", "disable_owner", "adopt_owner_week"}:
                return await self.owner.action(zone_id, action, payload, authorize)
            async with self.locks.setdefault(zone_id, asyncio.Lock()):
                self._check(zone_id, authorize)
                if self.owner.record(zone_id) and action not in {"read", "inspect", "read_clock"}:
                    raise ValidationError(
                        "Return to HA ownership before qualification or manual timetable copying"
                    )
                if self.transition.active(zone_id) and action in {
                    "prepare_test",
                    "start_test",
                    "check_test",
                    "restore_test",
                }:
                    raise ValidationError("Finish the active transition test first")
                if action in {"read_clock", "synchronize_clock"}:
                    if self.qualification.active(zone_id):
                        raise ValidationError("Finish the current hardware test first")
                    if action == "synchronize_clock":
                        self._check(zone_id, authorize, write=True)
                        if payload.get("acknowledgement") != "SYNCHRONIZE RECEIVER CLOCK":
                            raise ValidationError("Type SYNCHRONIZE RECEIVER CLOCK to confirm")
                    adapter = self._adapter(zone_id)
                    timezone = self.runtime.hass.config.time_zone
                    before = await adapter.read_clock()
                    record = self._record(zone_id)
                    record["clock"] = clock_report(before, timezone, dt_util.utcnow())
                    if action == "synchronize_clock":
                        record["clock"]["before_synchronization"] = before
                        await self._save(zone_id, record)
                        self._check(zone_id, authorize, write=True)
                        expected = expected_clock(timezone, dt_util.utcnow())
                        await adapter.synchronize_clock({**expected, "time_status": 2})
                        values = await adapter.read_clock()
                        record["clock"] = {
                            **clock_report(values, timezone, dt_util.utcnow()),
                            "before_synchronization": before,
                        }
                    await self._save(zone_id, record)
                    return self.snapshot(zone_id, details=True)
                if action in {
                    "prepare_transition",
                    "start_transition",
                    "check_transition",
                    "restore_transition",
                }:
                    return await self.transition.execute(zone_id, action, payload, authorize)
                if action in {
                    "inspect",
                    "prepare_test",
                    "start_test",
                    "check_test",
                    "restore_test",
                }:
                    return await self.qualification.execute(zone_id, action, payload, authorize)
                if self.qualification.active(zone_id) and action != "read":
                    raise ValidationError("Finish or restore the active receiver timer test first")
                if action == "read":
                    return await self._read(zone_id, authorize)
                if action in {"prepare", "prepare_restore"}:
                    return await self._prepare(
                        zone_id, payload, authorize, restore=action == "prepare_restore"
                    )
                if action == "write":
                    return await self._write(zone_id, payload, authorize)
                raise ValidationError("Unknown receiver action")
        finally:
            self.tasks.discard(task)

    async def _read(self, zone_id, authorize):
        mapping = self.runtime.mappings[zone_id]
        adapter = self._adapter(zone_id)
        schedule = native.validate_native(await adapter.read())
        zone = self._check(zone_id, authorize, mapping=mapping)
        record = self._record(zone_id)
        snapshot = {
            "at": dt_util.utcnow().isoformat(),
            "identity": adapter.identity,
            "schedule": schedule,
            "digest": native.digest(schedule),
        }
        try:
            snapshot["import_week"] = native.to_week(schedule, zone.limits)
            snapshot["import_warning"] = None
        except ValidationError as err:
            snapshot["import_week"] = None
            snapshot["import_warning"] = str(err)
        record["snapshot"] = snapshot
        record.pop("plan", None)
        await self._save(zone_id, record)
        return self.snapshot(zone_id, details=True)

    async def _prepare(self, zone_id, payload, authorize, *, restore):
        revision = payload.get("revision")
        if type(revision) is not int:
            raise ValidationError("A current HA schedule revision is required")
        zone = self._check(zone_id, authorize, revision=revision)
        record = self._record(zone_id)
        if restore:
            backup = next(
                (t for t in record.get("transactions", []) if t["id"] == payload.get("backup_id")),
                None,
            )
            if backup is None:
                raise ValidationError("Unknown receiver backup")
            after = native.validate_native(backup["before"])
            expected_identity = backup["identity"]
        else:
            week = self.runtime.repository.read(zone_id, zone.limits)[0].to_dict()
            after = native.from_week(week, zone.limits, self.runtime.mappings[zone_id][2])
            expected_identity = None
        mapping = self.runtime.mappings[zone_id]
        adapter = self._adapter(zone_id)
        if expected_identity is not None and adapter.identity != expected_identity:
            raise ValidationError("Backup belongs to a different receiver mapping")
        before = native.validate_native(await adapter.read())
        self._check(zone_id, authorize, revision=revision, mapping=mapping)
        if any(
            not day or any(p["centi"] != 100 and (p["centi"] < 500 or p["centi"] % 50) for p in day)
            for day in before.values()
        ):
            raise ValidationError(
                "Original receiver days cannot yet be safely restored; no writes permitted"
            )
        now = dt_util.utcnow()
        record["plan"] = {
            "id": secrets.token_hex(16),
            "at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=10)).isoformat(),
            "identity": adapter.identity,
            "revision": revision,
            "source": "backup" if restore else "ha_schedule",
            "before": before,
            "after": after,
            "changed_days": native.changes(before, after),
        }
        await self._save(zone_id, record)
        return self.snapshot(zone_id, details=True)

    async def _write(self, zone_id, payload, authorize):
        if payload.get("acknowledgement") != WRITE_ACKNOWLEDGEMENT:
            raise ValidationError(f"Type {WRITE_ACKNOWLEDGEMENT} to confirm the reviewed copy")
        record = self._record(zone_id)
        plan = record.get("plan")
        if not plan or payload.get("plan_id") != plan["id"]:
            raise RevisionConflict("Receiver preview is no longer current")
        if timestamp(plan["expires_at"]) <= dt_util.utcnow():
            raise RevisionConflict("Receiver preview expired; read and prepare again")
        self._check(zone_id, authorize, write=True, revision=plan["revision"])
        mapping = self.runtime.mappings[zone_id]
        adapter = self._adapter(zone_id)
        if adapter.identity != plan["identity"]:
            raise RevisionConflict("Receiver mapping changed; no schedule was written")
        current = native.validate_native(await adapter.read())
        self._check(zone_id, authorize, write=True, revision=plan["revision"], mapping=mapping)
        if current != plan["before"]:
            raise RevisionConflict(
                "Receiver timetable changed since preview; no schedule was written"
            )
        transaction = {
            **deepcopy(plan),
            "state": "writing",
            "attempted_days": [],
            "started_at": dt_util.utcnow().isoformat(),
            "error": None,
        }
        # Persist both sides BEFORE the first radio command. Interrupted writes
        # remain visibly unresolved after restart, never implicitly successful.
        record.pop("plan", None)
        record["transactions"] = [*record.get("transactions", []), transaction][-5:]
        await self._save(zone_id, record)
        try:
            for day in plan["changed_days"]:
                self._check(
                    zone_id, authorize, write=True, revision=plan["revision"], mapping=mapping
                )
                if timestamp(plan["expires_at"]) <= dt_util.utcnow():
                    raise RevisionConflict("Receiver preview expired during the write")
                transaction["attempted_days"].append(day)
                await self._save(zone_id, record)
                self._check(
                    zone_id, authorize, write=True, revision=plan["revision"], mapping=mapping
                )
                await adapter.write_day(day, plan["after"][day])
            readback = native.validate_native(await adapter.read())
            self._check(zone_id, authorize, write=True, revision=plan["revision"], mapping=mapping)
            if readback != plan["after"]:
                raise ValidationError("Receiver readback differs; timetable is not verified")
            transaction["state"] = "verified"
            record["snapshot"] = {
                "at": dt_util.utcnow().isoformat(),
                "identity": adapter.identity,
                "schedule": readback,
                "digest": native.digest(readback),
            }
            await self._save(zone_id, record)
        except (ValidationError, OSError, TimeoutError, HomeAssistantError) as err:
            transaction["state"] = "uncertain"
            transaction["error"] = str(err) or "Authorization changed during receiver operation"
            await self._save(zone_id, record)
            raise
        return self.snapshot(zone_id, details=True)

    def close(self):
        self.qualification.close()
        self.transition.close()
        for task in tuple(self.tasks):
            task.cancel()
