"""Durable smart-mode policies, independent of Home Assistant and device I/O."""

import re
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

from .const import DAYS
from .engine import ValidationError, timestamp, utc


def integer(value, low=0, high=10080):
    if type(value) is not int or not low <= value <= high:
        raise ValidationError(f"Expected whole minutes from {low} to {high}")
    return value


def entity(value, domains, optional=False):
    if optional and value == "":
        return value
    if not isinstance(value, str) or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", value):
        raise ValidationError("Invalid entity ID")
    if value.split(".")[0] not in domains:
        raise ValidationError("Unsupported entity domain")
    return value


def defaults(timer=""):
    return {
        "policy": {
            "entities": [],
            "home_rule": "any",
            "away_minutes": 10,
            "return_minutes": 0,
            "unknown_minutes": 10,
            "unknown_fallback": "schedule",
            "away_target": None,
            "comfort_target": None,
            "comfort_days": list(DAYS[:5]),
            "comfort_start": "08:30",
            "comfort_end": "16:30",
            "boost_owner": "external" if timer else "builtin",
            "boost_target": None,
            "boost_minutes": 30,
            "boost_max_minutes": 120,
            "start_script": "",
            "cancel_script": "",
            "duration_helper": "",
            "timer": timer,
            "completion_event": "",
            "completion_field": "zone",
            "completion_value": "",
        },
        "presence": {"trusted": None, "candidate": None, "since": None, "unknown_since": None},
        "override": None,
        "boost": None,
        "holiday": None,
        "external_operation": None,
        "external_was_active": False,
        "policy_revision": 1,
    }


def validate(data, limits):
    """Reject malformed storage and all unsupported values rather than guessing."""
    result = deepcopy(data)
    if not isinstance(result, dict) or set(result) != set(defaults()):
        raise ValidationError("Invalid smart-mode record")
    p = result["policy"]
    if not isinstance(p, dict) or set(p) != set(defaults()["policy"]):
        raise ValidationError("Invalid policy fields")
    if not isinstance(p["entities"], list) or len(p["entities"]) > 20:
        raise ValidationError("Select at most 20 presence entities")
    seen = set()
    for item in p["entities"]:
        if not isinstance(item, dict) or set(item) != {"entity_id", "home_state", "away_state"}:
            raise ValidationError("Presence needs an entity and explicit state meanings")
        eid = entity(item["entity_id"], {"person", "device_tracker", "binary_sensor"})
        if eid in seen:
            raise ValidationError("Presence entities must be unique")
        seen.add(eid)
        for key in ("home_state", "away_state"):
            if not isinstance(item[key], str) or not item[key] or len(item[key]) > 80:
                raise ValidationError("Invalid presence state meaning")
        if item["home_state"] == item["away_state"]:
            raise ValidationError("Home and away states must differ")
    if p["home_rule"] not in {"any", "all"} or p["unknown_fallback"] not in {
        "schedule",
        "home",
        "away",
    }:
        raise ValidationError("Invalid presence policy")
    for key in ("away_minutes", "return_minutes", "unknown_minutes"):
        integer(p[key])
    for key in ("away_target", "comfort_target", "boost_target"):
        if p[key] is not None:
            p[key] = limits.validate(p[key])
    if not isinstance(p["comfort_days"], list) or not set(p["comfort_days"]) <= set(DAYS):
        raise ValidationError("Invalid comfort days")
    for key in ("comfort_start", "comfort_end"):
        if not isinstance(p[key], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", p[key]):
            raise ValidationError("Comfort window needs HH:MM")
    if p["comfort_start"] >= p["comfort_end"]:
        raise ValidationError("Comfort window must end later on the same day")
    if p["boost_owner"] not in {"none", "builtin", "external"}:
        raise ValidationError("Invalid boost owner")
    integer(p["boost_max_minutes"], 1, 1440)
    integer(p["boost_minutes"], 1, p["boost_max_minutes"])
    for key in ("start_script", "cancel_script"):
        entity(p[key], {"script"}, optional=True)
    entity(p["duration_helper"], {"input_number"}, optional=True)
    entity(p["timer"], {"timer"}, optional=True)
    for key in ("completion_event", "completion_field", "completion_value"):
        if not isinstance(p[key], str) or len(p[key]) > 128:
            raise ValidationError("Invalid external completion signal")
    if p["completion_event"] and (
        not re.fullmatch(r"[a-zA-Z0-9_.:-]+", p["completion_event"])
        or p["completion_event"]
        in {
            "state_changed",
            "call_service",
            "homeassistant_start",
            "homeassistant_started",
            "homeassistant_stop",
        }
        or not p["completion_field"]
        or not p["completion_value"]
    ):
        raise ValidationError("Completion needs a custom event and an exact field/value filter")
    if p["boost_owner"] == "builtin" and p["timer"]:
        raise ValidationError("Remove the external timer before choosing built-in boost")
    if p["boost_owner"] == "external" and not p["timer"]:
        raise ValidationError("External boost needs a timer ownership signal")
    presence = result["presence"]
    if not isinstance(presence, dict) or set(presence) != set(defaults()["presence"]):
        raise ValidationError("Invalid presence state")
    for key in ("trusted", "candidate"):
        if presence[key] not in {None, "home", "away"}:
            raise ValidationError("Invalid trusted occupancy")
    for key in ("since", "unknown_since"):
        if presence[key] is not None:
            timestamp(presence[key])
    if (presence["candidate"] is None) != (presence["since"] is None):
        raise ValidationError("Presence transition needs a start time")
    for key, fields in (
        ("override", {"mode", "until"}),
        ("boost", {"target", "until", "return_off", "holiday_exception"}),
        ("holiday", {"start", "end", "target", "exceptions", "applied"}),
    ):
        item = result[key]
        if item is None:
            continue
        if not isinstance(item, dict) or set(item) != fields:
            raise ValidationError(f"Invalid {key} state")
        if key == "holiday":
            if timestamp(item["start"]) >= timestamp(item["end"]):
                raise ValidationError("Holiday end must follow its start")
            if type(item["exceptions"]) is not bool or type(item["applied"]) is not bool:
                raise ValidationError("Invalid holiday conflict policy")
        elif item["until"] is not None:
            timestamp(item["until"])
        if key == "boost":
            if item["until"] is None or any(
                type(item[k]) is not bool for k in ("return_off", "holiday_exception")
            ):
                raise ValidationError("Boost needs a deadline and explicit off/holiday intent")
        if key == "override" and item["mode"] not in {"home", "away"}:
            raise ValidationError("Invalid presence override")
        if "target" in item:
            item["target"] = limits.validate(item["target"])
    if result["external_operation"] not in {None, "starting", "stopping", "uncertain"}:
        raise ValidationError("Invalid external boost handover")
    if type(result["external_was_active"]) is not bool:
        raise ValidationError("Invalid external boost ownership memory")
    integer(result["policy_revision"], 1, 2147483647)
    if result["boost"] is not None and p["boost_owner"] != "builtin":
        raise ValidationError("Built-in boost cannot coexist with external ownership")
    return result


def active_holiday(data, now):
    h = data["holiday"]
    return bool(h and timestamp(h["start"]) <= utc(now) < timestamp(h["end"]))


def sample_presence(policy, states):
    values = []
    for item in policy["entities"]:
        state = states.get(item["entity_id"])
        if state is None or state in {"unknown", "unavailable"}:
            values.append(None)
        elif state == item["home_state"]:
            values.append(True)
        elif state == item["away_state"] or (
            item["away_state"] == "not_home"
            and item["entity_id"].split(".")[0] in {"person", "device_tracker"}
        ):
            values.append(False)
        else:
            values.append(None)
    if not values:
        return "schedule"
    if policy["home_rule"] == "any":
        return "home" if True in values else "unknown" if None in values else "away"
    return "away" if False in values else "unknown" if None in values else "home"


def occupancy(data, states, now):
    """Advance persisted debounce/grace state; return occupancy and next deadline."""
    now = utc(now)
    p, s = data["policy"], data["presence"]
    raw = sample_presence(p, states)
    deadline = None
    if raw == "unknown":
        s["candidate"], s["since"] = None, None
        s["unknown_since"] = s["unknown_since"] or now.isoformat()
        end = timestamp(s["unknown_since"]) + timedelta(minutes=p["unknown_minutes"])
        effective = s["trusted"] or "schedule"
        if end > now:
            deadline = end
        else:
            effective = p["unknown_fallback"]
    else:
        s["unknown_since"] = None
        if raw == "schedule":
            s.update(trusted=None, candidate=None, since=None)
            effective = "schedule"
        elif raw == s["trusted"]:
            s["candidate"], s["since"] = None, None
            effective = raw
        else:
            if raw != s["candidate"]:
                s["candidate"], s["since"] = raw, now.isoformat()
            end = timestamp(s["since"]) + timedelta(
                minutes=p["away_minutes" if raw == "away" else "return_minutes"]
            )
            if end <= now:
                s.update(trusted=raw, candidate=None, since=None)
                effective = raw
            else:
                effective, deadline = s["trusted"] or "schedule", end
    override = data["override"]
    if override and (override["until"] is None or timestamp(override["until"]) > now):
        effective = override["mode"]
        if override["until"]:
            end = timestamp(override["until"])
            deadline = min(deadline, end) if deadline else end
    elif override:
        data["override"] = None
    return effective, raw, deadline


def resolve_smart(base, data, effective, now, timezone):
    """Apply named priorities; physical off and adapter guards remain explicit."""
    now = utc(now)
    p, b, h = data["policy"], data["boost"], data["holiday"]
    deadlines = [base.next_evaluation]
    for item, keys in ((b, ("until",)), (h, ("start", "end"))):
        if item:
            deadlines.extend(timestamp(item[k]) for k in keys if timestamp(item[k]) > now)
    result = replace(base, next_evaluation=min(deadlines))
    if data["external_operation"]:
        return replace(
            result,
            target=None,
            source="boost_handover",
            reason="External boost handover needs confirmation",
            inhibited=True,
        )
    if base.source in {
        "disabled",
        "unavailable",
        "unsupported",
        "unsupported_mode",
        "boost_unknown",
        "external_boost",
    }:
        return result
    holiday = active_holiday(data, now)
    if (
        b
        and timestamp(b["until"]) > now
        and (not holiday or (h["exceptions"] and b["holiday_exception"]))
    ):
        return replace(
            result,
            target=b["target"],
            source="boost",
            reason="Timed boost; original deadline is preserved across restarts",
            inhibited=False,
        )
    if base.source in {"manual_off", "off"}:
        return result
    if holiday:
        return replace(
            result,
            target=h["target"],
            source="holiday",
            reason="Holiday target; current rules resume on return",
            inhibited=False,
        )
    if base.source == "manual":
        return result
    if effective == "away" and p["away_target"] is not None:
        return replace(
            result,
            target=p["away_target"],
            source="away",
            reason="Away target after configured presence delay",
            inhibited=False,
        )
    # Resolve window edges in UTC, including repeated-hour transitions.
    if p["comfort_target"] is not None:
        cursor = now.replace(second=0, microsecond=0) + timedelta(minutes=1)

        def in_window(at):
            wall = at.astimezone(timezone)
            return (
                DAYS[wall.weekday()] in p["comfort_days"]
                and p["comfort_start"] <= wall.strftime("%H:%M") < p["comfort_end"]
            )

        initial = in_window(now)
        while cursor < result.next_evaluation:
            if in_window(cursor) != initial:
                result = replace(result, next_evaluation=cursor)
                break
            cursor += timedelta(minutes=1)
        if effective == "home" and in_window(now):
            result = replace(
                result,
                target=max(result.target, p["comfort_target"]),
                source="occupied_comfort",
                reason="Occupied comfort minimum during the configured window",
                inhibited=False,
            )
    return result
