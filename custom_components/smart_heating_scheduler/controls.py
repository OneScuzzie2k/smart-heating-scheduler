"""Serialized smart-mode actions and optional climate writer. Default: observe only."""

import asyncio
from collections import deque
from dataclasses import asdict, replace
from datetime import timedelta

from homeassistant.core import Context, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from . import smart
from .engine import Hold, ValidationError, timestamp
from .insight_controls import InsightControls
from .native_receiver import current_target


class Controls(InsightControls):
    def _init_controls(self):
        self._contexts = deque(maxlen=100)
        self._command_state = {}
        self._external_active = {}
        self.smart_status = {}

    def smart_record(self, zone_id):
        return self.repository.smart_read(
            zone_id, self.zones[zone_id].limits, self.mappings[zone_id][1]
        )

    def _refresh_completion_listeners(self):
        for unsubscribe in getattr(self, "_completion_unsubscribers", []):
            unsubscribe()
        self._completion_unsubscribers = []
        names = {self.smart_record(zid)["policy"]["completion_event"] for zid in self.zones}
        for name in names - {""}:
            self._completion_unsubscribers.append(
                self.hass.bus.async_listen(name, self._completion_event)
            )

    @callback
    def _completion_event(self, event):
        for zid in self.zones:
            p = self.smart_record(zid)["policy"]
            if (
                event.event_type == p["completion_event"]
                and event.data.get(p["completion_field"]) == p["completion_value"]
            ):
                self.hass.async_create_task(self._complete_external(zid))

    async def _complete_external(self, zone_id):
        async with self._lock:
            if self._closed:
                return
            data = self.smart_record(zone_id)
            observation = self._observation(zone_id)
            if (
                observation.external_boost
                or observation.boost_unknown
                or not (data["external_was_active"] or data["external_operation"])
            ):
                return
            data["external_operation"] = None
            data["external_was_active"] = False
            try:
                await self.repository.smart_save(zone_id, data, self.zones[zone_id].limits)
            except OSError:
                return  # Remain inhibited; the card still offers explicit reconciliation.
            self.controller._previous.pop(zone_id, None)
            self._command_state.pop(zone_id, None)
        await self.async_evaluate()

    async def _smart_evaluate(self, zone, observation, now):
        native_owner = self.receivers.owner.record(zone.id)
        if native_owner:
            native_state = await self.receivers.owner.observe(zone, now)
            # The climate entity snapshot was captured BEFORE this radio read.
            # A just-pressed physical Off/Manual must win even if HA's entity
            # callback has not run yet. Never reinterpret it as a new HA boost
            # deliberately requested from Off or Manual.
            interrupted = (
                native_owner.get("lease")
                and native_state["mode"] != "schedule"
                and not self.receivers.owner.owned_boost(native_owner, native_state)
            )
            if interrupted:
                interrupted_data = self.smart_record(zone.id)
                if interrupted_data["boost"]:
                    interrupted_data["boost"] = None
                    await self.repository.smart_save(zone.id, interrupted_data, zone.limits)
            observation = replace(
                observation,
                mode="off" if native_state["mode"] == "off" else "heat",
                target=native_state["target"] / 100,
            )
        data = self.smart_record(zone.id)
        original = self.smart_record(zone.id)
        policy = data["policy"]
        entity_state = self.hass.states.get(self.mappings[zone.id][0])
        context = entity_state.context if entity_state else None
        owned = context and (context.id in self._contexts or context.parent_id in self._contexts)
        if native_owner and native_state["mode"] == "off":
            owned = False
        pending = self._command_state.get(zone.id)
        if pending and pending[0] == (
            observation.mode,
            observation.target if observation.mode == "heat" else None,
        ):
            # Some device integrations report confirmations with a fresh context.
            # An exact response to our pending command is not a new manual hold.
            owned = True
        frost_record = self.insights.read(zone.id, zone.limits)
        cycle = frost_record.get("frost_cycle")
        if cycle and (
            (pending and pending[2] > now)
            or (observation.mode == "heat" and observation.target == cycle["target"])
        ):
            owned = True
        previous = self.controller._previous.get(zone.id)
        if (
            data["boost"]
            and not owned
            and (previous is None or previous.mode == "heat")
            and observation.mode == "off"
        ):
            # An off device at startup may have been switched off while HA was
            # offline. Never resurrect an old boost over that explicit state.
            data["boost"] = None
            await self.repository.save_hold(zone.id, Hold("off", None, None), zone.limits)
        boost = data["boost"]
        if boost and timestamp(boost["until"]) <= now:
            if boost["return_off"]:
                await self.repository.save_hold(zone.id, Hold("off", None, None), zone.limits)
            data["boost"] = None
        holiday = data["holiday"]
        clear_manual = False
        if smart.active_holiday(data, now) and not holiday["applied"]:
            holiday["applied"] = True
            clear_manual = True
            # Existing temporary boosts are superseded, never resurrected later.
            if data["boost"] and not (holiday["exceptions"] and data["boost"]["holiday_exception"]):
                if data["boost"]["return_off"]:
                    await self.repository.save_hold(zone.id, Hold("off", None, None), zone.limits)
                data["boost"] = None
        if holiday and timestamp(holiday["end"]) <= now:
            data["holiday"] = None
        states = {
            i["entity_id"]: self.hass.states.get(i["entity_id"]).state
            if self.hass.states.get(i["entity_id"])
            else None
            for i in policy["entities"]
        }
        effective, raw, deadline = smart.occupancy(data, states, now)
        previously_active = data["external_was_active"]
        if not observation.boost_unknown:
            data["external_was_active"] = observation.external_boost
        # A timer becoming idle alone does not prove restoration succeeded.
        if previously_active and not observation.external_boost and not observation.boost_unknown:
            data["external_operation"] = "uncertain"
        if data != original or clear_manual:
            await self.repository.smart_save(zone.id, data, zone.limits, clear_manual=clear_manual)
        feature_week, feature_record, profile_days, feature_warnings = await self._profile_week(
            zone, now
        )
        base = await self.controller.evaluate(
            zone,
            observation,
            now,
            detect_manual=(
                not owned
                and not native_owner
                and not data["boost"]
                and not data["external_operation"]
                and not self.receivers.qualification.owns_observation(zone.id, now)
            ),
            week_override=feature_week,
        )
        if base.source == "schedule" and profile_days[0]["source"] != "weekly":
            base = replace(base, reason=f"{profile_days[0]['profile']}: {base.reason}")
        if native_owner and not self.insights.read(zone.id, zone.limits).get("frost_cycle"):
            native_state = self.receivers.owner.hardware[zone.id]
            native_mode = native_state["mode"]
            adjusted = (
                native_mode == "schedule"
                and not native_owner.get("lease")
                and native_state["target"]
                != current_target(native_owner["week"], now.astimezone(self.controller.timezone))
            )
            if native_mode == "off":
                base = replace(
                    base, source="off", target=None, inhibited=True, reason="Receiver is Off"
                )
            elif native_mode == "manual" or adjusted:
                base = replace(
                    base,
                    source="manual",
                    target=native_state["target"] / 100,
                    inhibited=False,
                    reason="Receiver temperature override",
                )
            elif native_mode == "boost" and not self.receivers.owner.owned_boost(
                native_owner, native_state
            ):
                base = replace(
                    base,
                    source="external_boost",
                    target=native_state["target"] / 100,
                    inhibited=True,
                    reason="Native boost outside this scheduler",
                )
        decision = smart.resolve_smart(base, data, effective, now, self.controller.timezone)
        decision = await self._finish_insights(
            zone,
            feature_record,
            profile_days,
            feature_warnings,
            feature_week,
            decision,
            effective,
            now,
        )
        if deadline:
            decision = replace(decision, next_evaluation=min(deadline, decision.next_evaluation))
        self.smart_status[zone.id] = {"occupancy": effective, "presence_raw": raw, **data}
        status = {
            **asdict(decision),
            "observe_only": not self.repository.control_enabled,
            "error": None,
        }
        if self.repository.control_enabled:
            await self._write(zone, observation, decision, status, now)
        return status

    async def _call(self, domain, service, data):
        if self._closed:
            raise HomeAssistantError("Scheduler is unloading; no new commands are permitted")
        context = Context()
        self._contexts.append(context.id)
        async with asyncio.timeout(30):
            await self.hass.services.async_call(
                domain, service, data, blocking=True, context=context
            )

    async def _write(self, zone, observation, decision, status, now):
        if await self.receivers.owner.write(zone, decision, status, now):
            return
        if not observation.available:
            self._command_state.pop(zone.id, None)
        if decision.inhibited:
            # Return to an explicit off suspension after a built-in boost.
            _, hold, _ = self.repository.read(zone.id, zone.limits)
            if (
                decision.source != "frost_return_off"
                and (decision.source != "manual_off" or not hold)
            ) or observation.mode == "off":
                return
            command = ("off", None)
        else:
            command = ("heat", decision.target)
            if observation.mode == "heat" and observation.target == decision.target:
                self._command_state.pop(zone.id, None)
                return
        previous = self._command_state.get(zone.id)
        signature, attempts, after = previous if previous else (None, 0, now)
        if signature != command:
            attempts, after = 0, max(after, now)
        if after > now:
            status["next_evaluation"] = min(status["next_evaluation"], after)
            return
        if attempts >= 3:
            status.update(
                error="command_not_confirmed",
                reason="No confirmation after three attempts; check device, then Retry controls",
            )
            return
        self._command_state[zone.id] = (
            command,
            attempts + 1,
            now + timedelta(seconds=(5, 15, 30)[attempts]),
        )
        try:
            eid = self.mappings[zone.id][0]
            if command[0] != observation.mode:
                await self._call(
                    "climate", "set_hvac_mode", {"entity_id": eid, "hvac_mode": command[0]}
                )
            if command[1] is not None:
                await self._call(
                    "climate", "set_temperature", {"entity_id": eid, "temperature": command[1]}
                )
        except (HomeAssistantError, TimeoutError):
            status.update(
                error="command_failed", reason="Heating command failed; bounded retry is pending"
            )
        status["next_evaluation"] = min(status["next_evaluation"], self._command_state[zone.id][2])

    def _validate_entities(self, data):
        p = data["policy"]
        keys = [i["entity_id"] for i in p["entities"]]
        keys += [
            p[k] for k in ("start_script", "cancel_script", "duration_helper", "timer") if p[k]
        ]
        for eid in keys:
            state = self.hass.states.get(eid)
            if state is None:
                raise ValidationError(f"Entity does not exist: {eid}")
        persons = [self.hass.states.get(eid) for eid in keys if eid.startswith("person.")]
        for person in persons:
            if set(person.attributes.get("device_trackers", [])) & set(keys):
                raise ValidationError("Select a person or their trackers, not both")
        if (
            len({p[k] for k in ("start_script", "cancel_script") if p[k]}) == 1
            and p["start_script"]
            and p["cancel_script"]
        ):
            raise ValidationError("Start and cancel must be separate scripts")

    async def async_control_action(self, action, zone_id, payload, *, authorize=None):
        """Authorized WebSocket handlers invoke this method. Never arbitrary services."""
        async with self._lock:
            if authorize is not None:
                authorize()
            if self._closed:
                raise ValidationError("Scheduler is unloading")
            if zone_id not in self.zones:
                raise ValidationError("Unknown zone")
            zone = self.zones[zone_id]
            data = self.smart_record(zone_id)
            p, now = data["policy"], dt_util.utcnow()
            observation = self._observation(zone_id)
            if action == "restore_configuration":
                await self._restore_configuration(zone, payload)
            elif action in {"insights", "reset_learning", "clear_history"}:
                await self._insights_action(zone, action, payload)
            elif action == "policy":
                if data["boost"] or observation.external_boost or data["external_operation"]:
                    raise ValidationError("Finish boost handover before changing its configuration")
                if (
                    type(payload.get("revision")) is not int
                    or payload["revision"] != data["policy_revision"]
                ):
                    raise ValidationError(
                        "Policy changed elsewhere; reload the rules before saving"
                    )
                data["policy"] = payload["policy"]
                data["policy_revision"] += 1
                data["presence"] = smart.defaults()["presence"]
                data = smart.validate(data, zone.limits)
                if (
                    self.receivers.owner.record(zone_id)
                    and data["policy"]["boost_owner"] == "external"
                ):
                    raise ValidationError("Receiver scheduling uses built-in or disabled boosts")
                self._validate_entities(data)
                for other_id in self.zones:
                    if other_id == zone_id:
                        continue
                    other = self.smart_record(other_id)["policy"]
                    new = data["policy"]
                    keys = ("timer", "start_script", "cancel_script", "duration_helper")
                    if ({new[key] for key in keys} & {other[key] for key in keys}) - {""}:
                        raise ValidationError(
                            "External boost mappings must belong to one zone only"
                        )
            elif action == "presence":
                mode = payload.get("mode")
                if mode == "auto":
                    data["override"] = None
                else:
                    duration = payload.get("minutes", 120)
                    end = (
                        None
                        if duration is None
                        else (now + timedelta(minutes=smart.integer(duration, 1))).isoformat()
                    )
                    if payload.get("until_next_period") is True:
                        week, _, _ = self.repository.read(zone_id, zone.limits)
                        end = week.next_boundary(now, self.controller.timezone).isoformat()
                    data["override"] = {"mode": mode, "until": end}
            elif action == "holiday":
                if payload.get("cancel") is True:
                    data["holiday"] = None
                else:
                    data["holiday"] = {
                        "start": payload["start"],
                        "end": payload["end"],
                        "target": payload["target"],
                        "exceptions": payload.get("exceptions", False),
                        "applied": False,
                    }
                    if timestamp(data["holiday"]["end"]) <= now:
                        raise ValidationError("Holiday must end in the future")
                    if observation.external_boost or data["external_operation"]:
                        raise ValidationError(
                            "Finish the existing external boost before scheduling a holiday"
                        )
            elif action == "boost_start":
                if not zone.enabled or not observation.available or not observation.supported:
                    raise ValidationError("An enabled, available heating zone is required")
                if (
                    observation.external_boost
                    or observation.boost_unknown
                    or data["external_operation"]
                    or data["boost"]
                ):
                    raise ValidationError("Boost already active or its ownership needs review")
                if p["boost_owner"] == "none" or p["boost_target"] is None:
                    raise ValidationError("Review boost ownership and target in Settings first")
                duration = smart.integer(
                    payload.get("minutes", p["boost_minutes"]), 1, p["boost_max_minutes"]
                )
                exception = payload.get("holiday_exception") is True
                if smart.active_holiday(data, now) and not (
                    data["holiday"]["exceptions"] and exception
                ):
                    raise ValidationError(
                        "Holiday blocks boost; enable and acknowledge a timed exception"
                    )
                if p["boost_owner"] == "builtin":
                    target = zone.limits.validate(payload.get("target", p["boost_target"]))
                    _, hold, _ = self.repository.read(zone_id, zone.limits)
                    data["boost"] = {
                        "target": target,
                        "until": (now + timedelta(minutes=duration)).isoformat(),
                        "return_off": observation.mode == "off"
                        or bool(hold and hold.kind == "off"),
                        "holiday_exception": exception,
                    }
                else:
                    if not self.repository.control_enabled:
                        raise ValidationError("External scripts cannot run in observe-only mode")
                    if not p["start_script"] or not p["cancel_script"]:
                        raise ValidationError("Map both external scripts first")
                    if payload.get("target", p["boost_target"]) != p["boost_target"]:
                        raise ValidationError("External boost uses its declared fixed target")
                    if not p["duration_helper"] and duration != p["boost_minutes"]:
                        raise ValidationError("External boost has a fixed duration")
                    if smart.active_holiday(data, now):
                        raise ValidationError(
                            "External holiday exceptions need an adapter-specific handover"
                        )
                    if p["duration_helper"]:
                        helper = self.hass.states.get(p["duration_helper"])
                        attrs = helper.attributes if helper else {}
                        low, high, step = (
                            attrs.get("min", 0),
                            attrs.get("max", 0),
                            attrs.get("step", 1),
                        )
                        if (
                            not low <= duration <= high
                            or step <= 0
                            or abs((duration - low) / step - round((duration - low) / step)) > 1e-6
                        ):
                            raise ValidationError("Duration does not fit the selected helper")
                    data["external_operation"] = "starting"
                    await self.repository.smart_save(zone_id, data, zone.limits)
                    try:
                        if p["duration_helper"]:
                            await self._call(
                                "input_number",
                                "set_value",
                                {"entity_id": p["duration_helper"], "value": duration},
                            )
                        await self._call("script", p["start_script"].split(".", 1)[1], {})
                        if not self._observation(zone_id).external_boost:
                            raise ValidationError("Start script did not confirm timer ownership")
                        data["external_operation"] = None
                        data["external_was_active"] = True
                    except (HomeAssistantError, TimeoutError, ValidationError):
                        data["external_operation"] = "uncertain"
                        await self.repository.smart_save(zone_id, data, zone.limits)
                        raise ValidationError(
                            "External start uncertain; check the receiver and reconcile ownership"
                        ) from None
            elif action == "boost_cancel":
                if data["boost"]:
                    if data["boost"]["return_off"]:
                        await self.repository.save_hold(
                            zone_id, Hold("off", None, None), zone.limits
                        )
                    data["boost"] = None
                elif (
                    p["boost_owner"] == "external"
                    and self.repository.control_enabled
                    and p["cancel_script"]
                ):
                    if not observation.external_boost and not data["external_operation"]:
                        raise ValidationError("No active or unresolved external boost to cancel")
                    data["external_operation"] = "stopping"
                    await self.repository.smart_save(zone_id, data, zone.limits)
                    try:
                        await self._call("script", p["cancel_script"].split(".", 1)[1], {})
                        observed = self._observation(zone_id)
                        if observed.external_boost or observed.boost_unknown:
                            raise ValidationError(
                                "External cancellation did not confirm timer release"
                            )
                        data["external_operation"] = None
                        self._external_active[zone_id] = False
                        data["external_was_active"] = False
                    except (HomeAssistantError, TimeoutError, ValidationError):
                        data["external_operation"] = "uncertain"
                        await self.repository.smart_save(zone_id, data, zone.limits)
                        raise ValidationError(
                            "External stop is uncertain; check the receiver and reconcile ownership"
                        ) from None
                else:
                    raise ValidationError("No cancellable boost")
                self.controller._previous.pop(zone_id, None)
            elif action == "reconcile":
                if (
                    payload.get("confirmed") is not True
                    or observation.external_boost
                    or observation.boost_unknown
                ):
                    raise ValidationError(
                        "Confirm completed restoration and a known idle timer first"
                    )
                data["external_operation"] = None
                self._external_active[zone_id] = False
                data["external_was_active"] = False
                self.controller._previous.pop(zone_id, None)
                self._command_state.pop(zone_id, None)
            elif action == "retry":
                self._command_state.pop(zone_id, None)
            elif action == "resume":
                if not zone.enabled:
                    raise ValidationError("Enable zone scheduling before resuming it")
                if (
                    observation.external_boost
                    or observation.boost_unknown
                    or data["external_operation"]
                    or data["boost"]
                ):
                    raise ValidationError("Finish boost ownership before resuming the schedule")
                if not observation.available or not observation.supported:
                    raise ValidationError("An available, supported thermostat is required")
                await self.controller.resume(zone)
                if self.repository.control_enabled and self.receivers.owner.record(zone_id):
                    await self.receivers.owner.resume(zone_id)
                elif self.repository.control_enabled and observation.mode == "off":
                    await self._call(
                        "climate",
                        "set_hvac_mode",
                        {"entity_id": self.mappings[zone_id][0], "hvac_mode": "heat"},
                    )
            elif action == "mode":
                enabled = payload.get("enabled")
                if (
                    enabled is False
                    and self.repository.control_enabled
                    and any(self.smart_record(zid)["boost"] for zid in self.zones)
                ):
                    raise ValidationError("Stop built-in boosts before switching to observe only")
                if enabled is True and payload.get("acknowledgement") != "ENABLE HEATING CONTROL":
                    raise ValidationError("Review handover and type ENABLE HEATING CONTROL")
                if enabled is True:
                    for zid in self.zones:
                        record = self.smart_record(zid)
                        if record["external_operation"]:
                            raise ValidationError("Resolve every external boost handover first")
                await self.repository.set_control(enabled)
                self._command_state.clear()
            else:
                raise ValidationError("Unsupported control action")
            if action not in {
                "insights",
                "reset_learning",
                "clear_history",
                "restore_configuration",
            }:
                await self.repository.smart_save(zone_id, data, zone.limits)
            if action == "policy":
                self._refresh_completion_listeners()
        await self.async_evaluate()
