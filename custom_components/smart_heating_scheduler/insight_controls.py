"""Read-only HA calendar/Workday adapters and optional feature orchestration."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

from homeassistant.exceptions import HomeAssistantError

from . import insights
from .engine import ValidationError, Week


def event_time(value, timezone):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(UTC)


class InsightControls:
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
        # Occupancy, including an unresolved/unknown configured presence source,
        # must not be bypassed by speculative early heating.
        presence_configured = bool(self.smart_record(zone.id)["policy"]["entities"])
        allowed = not warnings and (not presence_configured or effective == "home")
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
            # A failed optional-history write cannot result in untracked early heat.
            decision = replace(
                decision,
                target=decision.schedule_target,
                source="schedule",
                reason="Following schedule; preheating storage unavailable",
            )
            preheat["state"] = "paused"
        self.insight_status[zone.id] = {
            "days": selected,
            "active_profile": selected[0]["profile"],
            "effective_week": week.to_dict(),
            "warnings": warnings,
            "preheat": preheat,
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
