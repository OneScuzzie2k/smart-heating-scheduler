"""Schedule and manual-hold calculator; device I/O lives in controls.py."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .engine import Decision, Hold, Limits, Observation, ValidationError, resolve, utc
from .repository import Repository


@dataclass(frozen=True)
class Zone:
    id: str
    limits: Limits
    enabled: bool = True
    manual_policy: str = "next_period"
    manual_minutes: int = 120

    def __post_init__(self) -> None:
        if self.manual_policy not in {"next_period", "duration", "until_resume"}:
            raise ValidationError("Invalid manual hold policy")
        if type(self.manual_minutes) is not int or not 1 <= self.manual_minutes <= 10080:
            raise ValidationError("Hold duration must be 1 to 10080 minutes")


class PreviewController:
    """All mutation paths affect only this integration's persistent state."""

    def __init__(self, repository: Repository, timezone: str) -> None:
        self.repository = repository
        self.timezone = ZoneInfo(timezone)
        self.decisions: dict[str, Decision] = {}
        self.history: deque[dict] = deque(maxlen=50)
        self._previous: dict[str, Observation] = {}
        self._boost_release_until: dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def evaluate(
        self, zone: Zone, observation: Observation, now: datetime, *, detect_manual=True
    ) -> Decision:
        async with self._lock:
            record = self.repository.read(zone.id, zone.limits)
            if record is None:
                raise ValidationError("Zone has no reviewed schedule")
            week, hold, _revision = record
            if hold is not None and not hold.active(now):
                hold = None
                await self.repository.save_hold(zone.id, None, zone.limits)
            previous = self._previous.get(zone.id)
            if previous and previous.external_boost and not observation.external_boost:
                # Legacy scripts commonly cancel a timer before restoring the
                # thermostat. Ignore that short multi-event restoration burst.
                self._boost_release_until[zone.id] = utc(now) + timedelta(seconds=5)
            settling = self._boost_release_until.get(zone.id)
            if settling is not None and settling <= utc(now):
                self._boost_release_until.pop(zone.id)
                settling = None
            # Startup/recovery snapshots are not evidence of a new manual action.
            # While boost owns a zone, all target changes and its completion
            # restoration are excluded from manual-adjustment detection.
            if (
                previous
                and detect_manual
                and previous.available
                and observation.available
                and previous.supported
                and observation.supported
                and not previous.external_boost
                and not observation.external_boost
                and not previous.boost_unknown
                and not observation.boost_unknown
                and settling is None
            ):
                kind = None
                if observation.mode == "off" and previous.mode != "off":
                    kind = "off"
                elif (
                    observation.mode == "heat"
                    and (previous.mode != "heat" or observation.target != previous.target)
                    and observation.target is not None
                ):
                    kind = "manual"
                if kind is not None:
                    # Phase 1 implements the conservative off policy only.
                    # Timed off-to-heat resumption belongs with the live adapter.
                    policy = "until_resume" if kind == "off" else zone.manual_policy
                    minutes = zone.manual_minutes
                    expiry = (
                        week.next_boundary(now, self.timezone)
                        if policy == "next_period"
                        else utc(now) + timedelta(minutes=minutes)
                        if policy == "duration"
                        else None
                    )
                    hold = Hold(
                        kind,
                        zone.limits.validate(observation.target) if kind == "manual" else None,
                        expiry,
                    )
                    await self.repository.save_hold(zone.id, hold, zone.limits)
            # Do not consume the observation before any required persistence succeeds.
            self._previous[zone.id] = observation
            decision = resolve(
                week, zone.limits, now, self.timezone, observation, hold, enabled=zone.enabled
            )
            if self.decisions.get(zone.id) != decision:
                self.history.append(
                    {
                        "at": utc(now).isoformat(),
                        "zone_id": zone.id,
                        "source": decision.source,
                        "target": decision.target,
                    }
                )
            self.decisions[zone.id] = decision
            return decision

    async def resume(self, zone: Zone) -> None:
        """Clear preview hold only; never turn on a physically off thermostat."""
        async with self._lock:
            await self.repository.save_hold(zone.id, None, zone.limits)
            self._previous.pop(zone.id, None)
