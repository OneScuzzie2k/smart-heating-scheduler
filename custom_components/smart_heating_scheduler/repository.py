"""Versioned, transactional state. Persistence is injected (HA Store at runtime)."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Protocol

from . import smart
from .engine import Hold, Limits, ValidationError, Week


class Persistence(Protocol):
    async def async_load(self) -> dict | None: ...
    async def async_save(self, data: dict) -> None: ...


class RevisionConflict(ValidationError):
    """An editor is saving an obsolete version."""


class Repository:
    def __init__(self, persistence: Persistence) -> None:
        self._persistence = persistence
        self._lock = asyncio.Lock()
        self._data: dict = {"schema": 2, "zones": {}, "smart": {}, "control_enabled": False}

    async def load(self) -> None:
        raw = await self._persistence.async_load()
        if raw is None:
            return
        if (
            isinstance(raw, dict)
            and set(raw) == {"schema", "zones"}
            and type(raw["schema"]) is int
            and raw["schema"] == 1
        ):
            raw = {**raw, "schema": 2, "smart": {}, "control_enabled": False}
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema", "zones", "smart", "control_enabled"}
            or type(raw["schema"]) is not int
            or raw["schema"] != 2
            or not isinstance(raw["zones"], dict)
            or not isinstance(raw["smart"], dict)
            or type(raw["control_enabled"]) is not bool
        ):
            raise ValidationError("Unsupported or malformed stored scheduler data")
        self._data = deepcopy(raw)

    def snapshot(self) -> dict:
        return deepcopy(self._data)

    def read(self, zone_id: str, limits: Limits) -> tuple[Week, Hold | None, int] | None:
        raw = self._data["zones"].get(zone_id)
        if raw is None:
            return None
        if (
            not isinstance(raw, dict)
            or set(raw) != {"week", "hold", "revision"}
            or type(raw["revision"]) is not int
            or raw["revision"] < 1
        ):
            raise ValidationError("Invalid stored zone record")
        return (
            Week.parse(raw["week"], limits),
            Hold.parse(raw["hold"], limits) if raw["hold"] is not None else None,
            raw["revision"],
        )

    async def initialise(self, zone_id: str, week: Week) -> None:
        async with self._lock:
            if zone_id in self._data["zones"]:
                return
            candidate = self.snapshot()
            candidate["zones"][zone_id] = {"week": week.to_dict(), "hold": None, "revision": 1}
            await self._commit(candidate)

    async def save_week(
        self, zone_id: str, data: object, limits: Limits, expected_revision: int
    ) -> int:
        week = Week.parse(data, limits)
        async with self._lock:
            current = self.read(zone_id, limits)
            if current is None:
                raise ValidationError("Unknown zone")
            if type(expected_revision) is not int or current[2] != expected_revision:
                raise RevisionConflict("Schedule changed; reload before saving")
            candidate = self.snapshot()
            record = candidate["zones"][zone_id]
            record["week"] = week.to_dict()
            record["revision"] += 1
            await self._commit(candidate)
            return record["revision"]

    async def save_hold(self, zone_id: str, hold: Hold | None, limits: Limits) -> None:
        if hold is not None:
            Hold.parse(hold.to_dict(), limits)
        async with self._lock:
            if self.read(zone_id, limits) is None:
                raise ValidationError("Unknown zone")
            candidate = self.snapshot()
            candidate["zones"][zone_id]["hold"] = hold.to_dict() if hold else None
            await self._commit(candidate)

    async def _commit(self, candidate: dict) -> None:
        # Keep memory unchanged if the atomic storage write fails.
        await self._persistence.async_save(candidate)
        self._data = candidate

    @property
    def control_enabled(self):
        return self._data["control_enabled"]

    async def set_control(self, enabled):
        if type(enabled) is not bool:
            raise ValidationError("Control mode must be explicit")
        async with self._lock:
            candidate = self.snapshot()
            candidate["control_enabled"] = enabled
            await self._commit(candidate)

    def smart_read(self, zone_id, limits, timer=""):
        return smart.validate(self._data["smart"].get(zone_id, smart.defaults(timer)), limits)

    async def smart_save(self, zone_id, data, limits, *, clear_manual=False):
        data = smart.validate(data, limits)
        async with self._lock:
            record = self.read(zone_id, limits)
            if record is None:
                raise ValidationError("Unknown zone")
            candidate = self.snapshot()
            candidate["smart"][zone_id] = data
            if clear_manual and record[1] and record[1].kind == "manual":
                candidate["zones"][zone_id]["hold"] = None
            await self._commit(candidate)
