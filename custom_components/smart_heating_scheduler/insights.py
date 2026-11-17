"""Optional profiles, bounded local history and conservative measured preheating.

No device I/O. Existing weekly schedules remain the default and all new control
features are disabled until an administrator saves reviewed per-zone settings.
"""

import math
import re
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

from . import frost
from .const import DAYS
from .engine import ValidationError, Week, number, timestamp, utc


def defaults():
    return {
        **frost.defaults(),
        "profiles": {},
        "workday_entity": "",
        "workday_profile": "",
        "non_workday_profile": "",
        "calendar_rules": [],
        "preheat_enabled": False,
        "preheat_max_minutes": 90,
        "preheat_margin_minutes": 5,
        "history_enabled": True,
    }


def validate_settings(raw, limits):
    if not isinstance(raw, dict) or set(raw) != set(defaults()):
        raise ValidationError("Invalid profile/preheating settings")
    result = deepcopy(raw)
    frost.validate(raw, limits)
    if not isinstance(raw["profiles"], dict) or len(raw["profiles"]) > 12:
        raise ValidationError("Use at most 12 named day profiles")
    for name, periods in raw["profiles"].items():
        if not isinstance(name, str) or not name.strip() or len(name) > 40:
            raise ValidationError("Profile names need 1 to 40 characters")
        Week.parse({day: periods for day in DAYS}, limits)
    entity = raw["workday_entity"]
    if not isinstance(entity, str) or (
        entity and not re.fullmatch(r"binary_sensor\.[a-z0-9_]+", entity)
    ):
        raise ValidationError("Choose a Workday binary sensor")
    for key in ("workday_profile", "non_workday_profile"):
        if raw[key] and raw[key] not in raw["profiles"]:
            raise ValidationError("Workday rule refers to a missing profile")
    rules = raw["calendar_rules"]
    if not isinstance(rules, list) or len(rules) > 12:
        raise ValidationError("Use at most 12 ordered calendar rules")
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"entity_id", "match", "profile"}:
            raise ValidationError("Invalid calendar rule")
        if not isinstance(rule["entity_id"], str) or not re.fullmatch(
            r"calendar\.[a-z0-9_]+", rule["entity_id"]
        ):
            raise ValidationError("Choose a calendar entity")
        if not isinstance(rule["match"], str) or len(rule["match"]) > 80:
            raise ValidationError("Calendar title filter is too long")
        if rule["profile"] not in raw["profiles"]:
            raise ValidationError("Calendar rule refers to a missing profile")
    for key in ("preheat_enabled", "history_enabled"):
        if type(raw[key]) is not bool:
            raise ValidationError("Feature switches must be explicit")
    for key, low, high in (("preheat_max_minutes", 5, 180), ("preheat_margin_minutes", 0, 30)):
        if type(raw[key]) is not int or not low <= raw[key] <= high:
            raise ValidationError(f"{key} must be {low} to {high}")
    return result


def day_profile(settings, date, timezone, workday, events):
    """First matching calendar rule wins for the entire local day, then Workday.

    Calendar titles are used only in memory for matching, never in history/API.
    Missing/stale sources fall back to the ordinary saved weekly schedule.
    """
    start = datetime.combine(date, time(), timezone).astimezone(UTC)
    end = datetime.combine(date + timedelta(days=1), time(), timezone).astimezone(UTC)
    for rule in settings["calendar_rules"]:
        for event in events.get(rule["entity_id"], []):
            if (
                event["start"] < end
                and event["end"] > start
                and rule["match"].casefold() in event["summary"].casefold()
            ):
                return rule["profile"], "calendar"
    key = (
        "workday_profile"
        if workday is True
        else "non_workday_profile"
        if workday is False
        else None
    )
    return (settings[key], "workday") if key and settings[key] else ("", "weekly")


def effective_week(week, settings, now, timezone, workdays, events):
    data = week.to_dict()
    today = utc(now).astimezone(timezone).date()
    selected = []
    for offset in range(7):
        date = today + timedelta(days=offset)
        name, source = day_profile(settings, date, timezone, workdays.get(date.isoformat()), events)
        if name:
            data[DAYS[date.weekday()]] = deepcopy(settings["profiles"][name])
        selected.append(
            {"date": date.isoformat(), "profile": name or "Weekly schedule", "source": source}
        )
    return data, selected


def empty_record():
    return {
        "revision": 1,
        "settings": defaults(),
        "samples": [],
        "decisions": [],
        "model": {"rate": None, "minutes": 0, "samples": 0},
        "preheat": None,
        "frost_cycle": None,
        "warmup_result": None,
    }


class InsightsRepository:
    """Separate versioned store: existing scheduler storage is unchanged."""

    def __init__(self, store):
        self.store = store
        self.data = {"schema": 1, "zones": {}}

    async def load(self):
        raw = await self.store.async_load()
        if raw is not None:
            if (
                not isinstance(raw, dict)
                or raw.get("schema") != 1
                or not isinstance(raw.get("zones"), dict)
            ):
                raise ValidationError("Invalid insights storage")
            self.data = raw

    def read(self, zid, limits):
        record = deepcopy(self.data["zones"].get(zid, empty_record()))
        record["settings"] = {**frost.defaults(), **record["settings"]}
        record.setdefault("frost_cycle", None)
        record.setdefault("warmup_result", None)
        validate_settings(record["settings"], limits)
        frost.validate_cycle(record["frost_cycle"], limits)
        if type(record["revision"]) is not int or record["revision"] < 1:
            raise ValidationError("Invalid insights revision")
        if len(record["samples"]) > 2016 or len(record["decisions"]) > 500:
            raise ValidationError("Invalid history retention")
        for sample in record["samples"] + record["decisions"]:
            timestamp(sample["at"])
            for key in ("room", "target"):
                if sample.get(key) is not None:
                    number(sample[key])
        model = record["model"]
        if model["rate"] is not None and number(model["rate"]) <= 0:
            raise ValidationError("Invalid heating model")
        if (
            type(model["samples"]) is not int
            or model["samples"] < 0
            or number(model["minutes"]) < 0
        ):
            raise ValidationError("Invalid heating observations")
        if model.get("updated"):
            timestamp(model["updated"])
        if record.get("preheat"):
            active = record["preheat"]
            limits.validate(active["target"])
            if timestamp(active["until"]) <= timestamp(active["start"]):
                raise ValidationError("Invalid preheating deadline")
        return record

    def source_ids(self, zid):
        """Small read-only mapping; never copy history for every HA state event."""
        record = self.data["zones"].get(zid)
        if record is None:
            return set()
        settings = record["settings"]
        return {settings["workday_entity"]} | {r["entity_id"] for r in settings["calendar_rules"]}

    async def save(self, zid, record):
        candidate = deepcopy(self.data)
        candidate["zones"][zid] = deepcopy(record)
        await self.store.async_save(candidate)
        self.data = candidate


def observe(record, now, room, target, action, source, reason, *, unit="°C", decision_target=None):
    """Sample at five-minute intervals. Never infer heating from a target alone."""
    now = utc(now)
    cutoff = now - timedelta(days=7)
    record["samples"] = [s for s in record["samples"] if timestamp(s["at"]) >= cutoff][-2015:]
    record["decisions"] = [s for s in record["decisions"] if timestamp(s["at"]) >= cutoff][-499:]
    decision = {"source": source, "target": decision_target, "reason": reason}
    if record["settings"]["history_enabled"] and (
        not record["decisions"]
        or any(record["decisions"][-1][key] != value for key, value in decision.items())
    ):
        record["decisions"].append({"at": now.isoformat(), **decision})
    samples = record["samples"]
    previous = samples[-1] if samples else None
    valid = type(room) in (float, int) and math.isfinite(room)
    if previous and (action != "heating" or not valid):
        previous["interrupted"] = True
    elapsed = (now - timestamp(previous["at"])).total_seconds() / 60 if previous else None
    if elapsed is not None and elapsed < 5:
        return
    if (
        previous
        and not previous.get("interrupted")
        and elapsed <= 15
        and valid
        and previous["room"] is not None
        and action == previous["action"] == "heating"
    ):
        rate = (room - previous["room"]) * 60 / elapsed
        # Reject stationary/cooling observations and implausible jumps. Rates use
        # the climate's display unit; the Fahrenheit bound is converted explicitly.
        upper = 18 if unit == "°F" else 10
        if 0 < rate <= upper:
            model = record["model"]
            model["rate"] = rate if model["rate"] is None else model["rate"] * 0.8 + rate * 0.2
            model["minutes"] += elapsed
            model["samples"] += 1
            model["updated"] = now.isoformat()
    # Samples also support learning when chart retention is switched off, but
    # only the last sample is kept and no chart/decision history is exposed.
    sample = {
        "at": now.isoformat(),
        "room": room if valid else None,
        "target": target,
        "action": action,
    }
    if not record["settings"]["history_enabled"]:
        record["samples"] = [sample]
        record["decisions"] = []
    else:
        record["samples"].append(sample)


def preheat(decision, record, week, now, timezone, room, *, allowed=True):
    settings, model = record["settings"], record["model"]
    info = {
        "state": "disabled",
        "ready_at": None,
        "starts_at": None,
        "rate": model["rate"],
        "samples": model["samples"],
        "minutes": model["minutes"],
        "required_samples": 12,
        "required_minutes": 60,
        "learning_percent": min(
            100, round(min(model["samples"] / 12, model["minutes"] / 60) * 100)
        ),
        "last_result": record.get("warmup_result"),
        "estimated_minutes": None,
        "pause_reason": None,
    }
    if not settings["preheat_enabled"]:
        record["preheat"] = None
        return decision, info
    info["state"] = "learning"
    if not allowed or decision.inhibited or decision.source not in {"schedule", "occupied_comfort"}:
        record["preheat"] = None
        info["state"] = "paused"
        info["pause_reason"] = (
            "Waiting for calendar or Workday data"
            if not allowed
            else {
                "off": "Paused while thermostat is Off",
                "manual_off": "Paused while thermostat is Off",
                "manual": "Manual temperature is active",
                "boost": "Timed boost is active",
                "external_boost": "External boost is active",
                "holiday": "Holiday is active",
                "away": "Configured Away temperature is active",
                "unavailable": "Waiting for thermostat",
                "disabled": "Zone scheduling is disabled",
            }.get(decision.source, "Another control rule is active")
        )
        return decision, info
    if type(room) not in (int, float) or not math.isfinite(room):
        record["preheat"] = None
        info["state"] = "no_temperature"
        return decision, info
    active = record.get("preheat")
    if active:
        until = timestamp(active["until"])
        if (
            until > utc(now)
            and active["target"]
            > max(decision.schedule_target, decision.target or decision.schedule_target)
            and week.next_boundary(now, timezone) == until
            and week.current(until, timezone).temperature == active["target"]
        ):
            info.update(state="preheating", ready_at=active["until"], starts_at=active["start"])
            return replace(
                decision,
                target=active["target"],
                source="preheat",
                reason=f"Preheating for {until.astimezone(timezone):%H:%M}; measured warm-up rate",
                next_evaluation=min(decision.next_evaluation, until),
            ), info
        if until <= utc(now) and utc(now) - until <= timedelta(minutes=10):
            record["warmup_result"] = {
                "at": until.isoformat(),
                "target": active["target"],
                "room": room,
                "on_time": room >= active["target"] - 0.3,
            }
            info["last_result"] = record["warmup_result"]
        record["preheat"] = None
    if (
        model["samples"] < 12
        or model["minutes"] < 60
        or not model["rate"]
        or not model.get("updated")
        or utc(now) - timestamp(model["updated"]) > timedelta(days=14)
    ):
        return decision, info
    info["state"] = "ready"
    # Search real UTC instants to retain the scheduler's DST gap/fold semantics.
    cursor = utc(now)
    horizon = cursor + timedelta(minutes=settings["preheat_max_minutes"])
    for _ in range(96):
        boundary = week.next_boundary(cursor, timezone)
        if boundary > horizon:
            break
        target = week.current(boundary, timezone).temperature
        if (
            target > max(decision.schedule_target, decision.target or decision.schedule_target)
            and target > room
        ):
            minutes = min(
                settings["preheat_max_minutes"],
                (target - room) / model["rate"] * 60 + settings["preheat_margin_minutes"],
            )
            start = boundary - timedelta(minutes=minutes)
            info.update(
                ready_at=boundary.isoformat(),
                starts_at=start.isoformat(),
                estimated_minutes=round(minutes),
            )
            if start <= utc(now):
                info["state"] = "preheating"
                record["preheat"] = {
                    "until": boundary.isoformat(),
                    "start": start.isoformat(),
                    "target": target,
                }
                return replace(
                    decision,
                    target=target,
                    source="preheat",
                    reason=(
                        f"Preheating for {boundary.astimezone(timezone):%H:%M}; "
                        "measured warm-up rate"
                    ),
                    next_evaluation=min(decision.next_evaluation, boundary),
                ), info
            return replace(decision, next_evaluation=min(decision.next_evaluation, start)), info
        cursor = boundary
    return decision, info
