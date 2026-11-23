"""Read-only HA calendar/Workday adapters and optional feature orchestration."""

import asyncio
import math
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

from homeassistant.exceptions import HomeAssistantError

from . import frost, insights
from .engine import Hold, Limits, ValidationError, Week


def event_time(value, timezone):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(UTC)


class InsightControls:
    def _frost_limits(self, zone):
        observation = self._observation(zone.id)
        entity = self.hass.states.get(self.mappings[zone.id][0])
        if not entity or not observation.available or not observation.supported:
            return None
        attrs = entity.attributes
        try:
            low, high = attrs["min_temp"], min(attrs["max_temp"], zone.limits.maximum)
            step = attrs.get("target_temp_step") or zone.limits.step
            if step:
                low = math.ceil(low / step - 1e-8) * step
                high = math.floor(high / step + 1e-8) * step
            return Limits(low, high, step)
        except (ValidationError, KeyError, TypeError):
            return None

    async def _read_feature_service(self, domain, service, data):
        async with asyncio.timeout(10):
            return await self.hass.services.async_call(
                domain, service, data, blocking=True, return_response=True
            )

    def _invalidate_insights(self):
        self._insight_generation += 1
        self._calendar_cache.clear()
        self._workday_cache.clear()

    def _queue_feature_read(self, key, cache, cache_key, domain, service, payload, parser, now):
        if key in self._feature_reads or self._closed:
            return
        generation = self._insight_generation

        async def read():
            try:
                try:
                    response = await self._read_feature_service(domain, service, payload)
                    value = parser(response)
                    deadline = now + timedelta(minutes=15)
                except (HomeAssistantError, TimeoutError, ValueError, TypeError, KeyError):
                    value, deadline = None, now + timedelta(minutes=5)
                if not self._closed and generation == self._insight_generation:
                    cache[cache_key] = (deadline, value)
            finally:
                self._feature_reads.pop(key, None)
                if not self._closed:
                    self._changed(None)

        self._feature_reads[key] = self.hass.async_create_task(read())

    async def _profile_week(self, zone, now):
        record = self.insights.read(zone.id, zone.limits)
        settings = record["settings"]
        timezone = self.controller.timezone
        today = now.astimezone(timezone).date()
        for key in list(self._workday_cache):
            if key[1] < today.isoformat():
                del self._workday_cache[key]
        events, workdays, warnings = {}, {}, []
        for eid in sorted({r["entity_id"] for r in settings["calendar_rules"]}):
            state = self.hass.states.get(eid)
            if state is None or state.state in {"unknown", "unavailable"}:
                warnings.append("Calendar unavailable; weekly/Workday fallback applies")
                continue
            cached = self._calendar_cache.get(eid)
            if not cached or cached[0] <= now:

                def parse_calendar(response, entity=eid):
                    raw = response[entity]["events"]
                    if not isinstance(raw, list) or len(raw) > 1000:
                        raise ValueError("Calendar response exceeds supported bound")
                    return [
                        {
                            "start": event_time(e["start"], timezone),
                            "end": event_time(e["end"], timezone),
                            "summary": str(e.get("summary", "")),
                        }
                        for e in raw
                    ]

                self._queue_feature_read(
                    ("calendar", eid),
                    self._calendar_cache,
                    eid,
                    "calendar",
                    "get_events",
                    {
                        "entity_id": eid,
                        "start_date_time": datetime.combine(today, time(), timezone).isoformat(),
                        "end_date_time": datetime.combine(
                            today + timedelta(days=7), time(), timezone
                        ).isoformat(),
                    },
                    parse_calendar,
                    now,
                )
                # Retain the last good result only during this bounded refresh,
                # avoiding transient target changes on every successful refresh.
                # A failed refresh discards it and applies the advertised fallback.
                if cached and cached[1] is not None:
                    events[eid] = cached[1]
                    warnings.append("Updating calendar; keeping last result, preheating paused")
                else:
                    warnings.append("Updating calendar; weekly/Workday fallback applies")
            elif cached[1] is None:
                warnings.append("Calendar could not be read; weekly/Workday fallback applies")
            else:
                events[eid] = cached[1]
        eid = settings["workday_entity"]
        if eid:
            state = self.hass.states.get(eid)
            if state is None or state.state not in {"on", "off"}:
                warnings.append("Workday unavailable; weekly fallback applies")
            else:
                workdays[today.isoformat()] = state.state == "on"
                for offset in range(1, 7):
                    date = (today + timedelta(days=offset)).isoformat()
                    key = (eid, date)
                    cached = self._workday_cache.get(key)
                    if not cached or cached[0] <= now:

                        def parse_workday(response, entity=eid):
                            value = response[entity]["workday"]
                            if type(value) is not bool:
                                raise ValueError("Invalid Workday response")
                            return value

                        self._queue_feature_read(
                            ("workday", eid, date),
                            self._workday_cache,
                            key,
                            "workday",
                            "check_date",
                            {"entity_id": eid, "check_date": date},
                            parse_workday,
                            now,
                        )
                        if cached and cached[1] is not None:
                            workdays[date] = cached[1]
                        warnings.append("Updating future Workday dates; preheating paused")
                    elif cached[1] is None:
                        warnings.append("Future Workday dates could not be read; preheating paused")
                    else:
                        workdays[date] = cached[1]
        week, selected = insights.effective_week(
            self.repository.read(zone.id, zone.limits)[0], settings, now, timezone, workdays, events
        )
        return Week.parse(week, zone.limits), record, selected, sorted(set(warnings))

    async def _finish_insights(
        self, zone, record, selected, warnings, week, decision, effective, now
    ):
        entity = self.hass.states.get(self.mappings[zone.id][0])
        attrs = entity.attributes if entity else {}
        # Presence alone does not own the thermostat. Preheat the saved schedule
        # regardless of occupancy unless an actual configured override wins.
        allowed = not warnings
        ordinary_decision = decision
        original = deepcopy(record)
        if zone.id not in self._learning_started:
            if record["samples"]:
                record["samples"][-1]["interrupted"] = True
            self._learning_started.add(zone.id)
        decision, preheat = insights.preheat(
            decision,
            record,
            week,
            now,
            self.controller.timezone,
            attrs.get("current_temperature"),
            allowed=allowed,
        )
        previous_cycle = deepcopy(record.get("frost_cycle"))
        decision, frost_status = frost.resolve(
            decision,
            record,
            self._observation(zone.id),
            attrs.get("current_temperature"),
            entity.last_updated if entity else None,
            now,
            control_enabled=self.repository.control_enabled,
            step=zone.limits.step,
            hardware_limits=self._frost_limits(zone),
            reviewed_limits=zone.limits,
            settling=bool(
                self._command_state.get(zone.id) and self._command_state[zone.id][2] > now
            ),
        )
        if record.get("frost_cycle") and not previous_cycle:
            # Preserve the original physical Off state through a restart.
            await self.repository.save_hold(zone.id, Hold("off", None, None), zone.limits)
        # History records every final owner, including manual/boost/holiday.
        if record["settings"]["history_enabled"] or record["settings"]["preheat_enabled"]:
            insights.observe(
                record,
                now,
                attrs.get("current_temperature")
                if entity and entity.state not in {"unknown", "unavailable"}
                else None,
                attrs.get("temperature"),
                attrs.get("hvac_action") if entity and entity.state == "heat" else None,
                decision.source,
                decision.reason,
                unit=self.mappings[zone.id][2],
                decision_target=decision.target,
            )
        storage_error = False
        if record != original or zone.id in self._insight_storage_failed:
            try:
                await self.insights.save(zone.id, record)
                self._insight_storage_failed.discard(zone.id)
            except OSError:
                storage_error = True
                self._insight_storage_failed.add(zone.id)
                record = original
                warnings.append("History could not be saved; preheating paused")
        if storage_error and decision.source == "preheat":
            # Keep the actual ordinary owner, including an occupied minimum.
            decision = replace(
                ordinary_decision, reason="Preheating storage unavailable; using current rule"
            )
            preheat.update(state="paused", pause_reason="Preheating storage unavailable")
        elif storage_error and decision.source in {"frost", "frost_return_off"}:
            decision = replace(
                decision,
                target=None,
                source="error",
                inhibited=True,
                reason="Optional control state could not be saved; commands paused",
            )
            preheat.update(state="paused", pause_reason="Optional control storage unavailable")
            frost_status["state"] = "unavailable"
        self.insight_status[zone.id] = {
            "days": selected,
            "active_profile": selected[0]["profile"],
            "effective_week": week.to_dict(),
            "warnings": warnings,
            "preheat": preheat,
            "frost": frost_status,
            "samples": record["samples"] if record["settings"]["history_enabled"] else [],
            "decisions": record["decisions"] if record["settings"]["history_enabled"] else [],
        }
        if (
            record["settings"]["history_enabled"]
            or record["settings"]["preheat_enabled"]
            or record["settings"]["calendar_rules"]
            or record["settings"]["workday_entity"]
        ):
            decision = replace(
                decision, next_evaluation=min(decision.next_evaluation, now + timedelta(minutes=5))
            )
        return decision

    async def _insights_action(self, zone, action, payload):
        record = self.insights.read(zone.id, zone.limits)
        if type(payload.get("revision")) is not int or payload["revision"] != record["revision"]:
            raise ValidationError("Zone features changed elsewhere; reload before saving")
        if action == "insights":
            settings = insights.validate_settings(payload["settings"], zone.limits)
            frost.validate(
                settings,
                zone.limits,
                hardware_limits=self._frost_limits(zone),
                require_hardware=True,
            )
            ids = {settings["workday_entity"]} | {
                r["entity_id"] for r in settings["calendar_rules"]
            }
            for eid in ids - {""}:
                if self.hass.states.get(eid) is None:
                    raise ValidationError(f"Entity does not exist: {eid}")
            record["settings"] = settings
            record["preheat"] = None
            if not settings["history_enabled"]:
                record["samples"], record["decisions"] = [], []
        elif action == "reset_learning":
            record["model"] = insights.empty_record()["model"]
            record["samples"] = []
            record["preheat"] = None
        elif action == "clear_history":
            record["samples"], record["decisions"] = [], []
        record["revision"] += 1
        await self.insights.save(zone.id, record)
        self._invalidate_insights()

    async def _restore_configuration(self, zone, payload):
        """Import reviewed configuration only while this destination is disabled."""
        if self.receivers.owner.record(zone.id):
            raise ValidationError("Return to HA ownership before restoring a whole zone backup")
        from . import smart

        if zone.enabled or payload.get("acknowledgement") != "RESTORE ZONE SETTINGS":
            raise ValidationError(
                "Disable this zone and review the import before restoring settings"
            )
        old = self.insights.read(zone.id, zone.limits)
        data = payload["backup"]
        if not isinstance(data, dict):
            raise ValidationError("Configuration backup must be an object")
        if (
            data.get("format") != "smart-heating-scheduler/zone"
            or data.get("version") != 1
            or data.get("unit") != self.mappings[zone.id][2]
        ):
            raise ValidationError(
                "Unsupported configuration backup or incompatible temperature units"
            )
        current = self.smart_record(zone.id)
        observed = self._observation(zone.id)
        if (
            current["boost"]
            or current["external_operation"]
            or observed.external_boost
            or observed.boost_unknown
            or old.get("frost_cycle")
        ):
            raise ValidationError("Finish boost/frost ownership before restoring settings")
        _, _, revision = self.repository.read(zone.id, zone.limits)
        if any(
            type(payload.get(k)) is not int
            for k in ("revision", "features_revision", "policy_revision")
        ) or (
            revision != payload["revision"]
            or old["revision"] != payload["features_revision"]
            or current["policy_revision"] != payload["policy_revision"]
        ):
            raise ValidationError("Zone changed elsewhere; reopen the import preview")
        Week.parse(data["week"], zone.limits)
        candidate = deepcopy(old)
        candidate["settings"] = insights.validate_settings(data["features"], zone.limits)
        frost.validate(
            candidate["settings"],
            zone.limits,
            hardware_limits=self._frost_limits(zone),
            require_hardware=True,
        )
        candidate["preheat"] = None
        candidate["revision"] += 1
        policy_record = deepcopy(current)
        policy_record["policy"] = data["policy"]
        smart.validate(policy_record, zone.limits)
        self._validate_entities(policy_record)
        settings = candidate["settings"]
        for eid in (
            {settings["workday_entity"]} | {r["entity_id"] for r in settings["calendar_rules"]}
        ) - {""}:
            if self.hass.states.get(eid) is None:
                raise ValidationError(f"Feature entity does not exist: {eid}")
        keys = ("timer", "start_script", "cancel_script", "duration_helper")
        for zid in self.zones:
            if zid != zone.id and (
                {data["policy"][k] for k in keys}
                & {self.smart_record(zid)["policy"][k] for k in keys}
            ) - {""}:
                raise ValidationError("External boost mappings already belong to another zone")
        await self.insights.save(zone.id, candidate)
        try:
            await self.repository.restore_configuration(
                zone.id, data["week"], data["policy"], zone.limits, revision
            )
        except (OSError, ValidationError):
            try:
                await self.insights.save(zone.id, old)
            except OSError as err:
                raise ValidationError(
                    "Partial import: settings rollback failed. "
                    "Keep the zone disabled and retry the reviewed backup"
                ) from err
            raise
        self._invalidate_insights()
        self._refresh_completion_listeners()
