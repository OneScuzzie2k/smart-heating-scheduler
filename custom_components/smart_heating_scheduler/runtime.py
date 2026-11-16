"""HA event bridge for persistent rules and explicitly enabled heating controls."""

from __future__ import annotations

import asyncio
import logging

import probatio as vol
from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.const import (
    EVENT_CORE_CONFIG_UPDATE,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_STATE_CHANGED,
)
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_entity_registry_updated_event,
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import insights, smart
from .const import DOMAIN, STORAGE_VERSION
from .controller import PreviewController, Zone
from .controls import Controls
from .engine import Limits, Observation, ValidationError, Week, number, timestamp
from .insights import InsightsRepository
from .repository import Repository

_LOGGER = logging.getLogger(__name__)


class Runtime(Controls):
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.repository = Repository(
            Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}", atomic_writes=True)
        )
        self.controller = PreviewController(self.repository, hass.config.time_zone)
        self.insights = InsightsRepository(
            Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.insights", atomic_writes=True)
        )
        self.insight_status = {}
        self._calendar_cache = {}
        self._workday_cache = {}
        self._learning_started = set()
        self._insight_storage_failed = set()
        self._feature_reads = {}
        self._insight_generation = 0
        self.zones = {}
        self.mappings = {}
        self.status = {}
        self.listeners = set()
        self._unsubscribers = []
        self._cancel_deadline = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._evaluation_tasks = set()
        self._init_controls()

    async def async_setup(self):
        try:
            await self.repository.load()
            await self.insights.load()
            ids = set()
            for subentry in self.entry.subentries.values():
                if subentry.subentry_type != "zone":
                    continue
                data = subentry.data
                registry_id = data.get("climate_registry_id")
                entity_id = (
                    er.async_resolve_entity_id(er.async_get(self.hass), registry_id)
                    if registry_id
                    else data["climate_entity"]
                )
                # Deleted registry entities must not bind to a new device with a reused ID.
                entity_id = entity_id or f"climate.missing_{subentry.subentry_id}"
                if entity_id in ids:
                    raise ValidationError("Duplicate thermostat mapping")
                ids.add(entity_id)
                limits = Limits(data["minimum"], data["maximum"], data.get("step"))
                zone = Zone(
                    subentry.subentry_id,
                    limits,
                    data["enabled"],
                    data["manual_policy"],
                    data["manual_minutes"],
                )
                self.zones[zone.id] = zone
                self.mappings[zone.id] = (entity_id, data.get("boost_timer", ""), data["unit"])
                await self.repository.initialise(
                    zone.id,
                    Week.parse(data["initial_week"], limits)
                    if data.get("initial_week")
                    else Week.baseline(data["baseline_temperature"], limits),
                )
                self.repository.read(zone.id, limits)
                if zone.id not in self.repository.snapshot()["smart"] and data.get(
                    "initial_policy"
                ):
                    initial = smart.defaults()
                    initial["policy"] = data["initial_policy"]
                    await self.repository.smart_save(zone.id, initial, limits)
                if zone.id not in self.insights.data["zones"] and data.get("initial_features"):
                    initial = insights.empty_record()
                    initial["settings"] = insights.validate_settings(
                        data["initial_features"], limits
                    )
                    await self.insights.save(zone.id, initial)
                self.smart_record(zone.id)
                self.insights.read(zone.id, limits)
            await self.async_evaluate()
        except (ValidationError, ValueError, TypeError, KeyError) as err:
            raise ConfigEntryError(
                "Invalid scheduler configuration/storage; no heating control"
            ) from err
        tracked = [
            entity
            for entity, timer, _ in self.mappings.values()
            for entity in (entity, timer)
            if entity
        ]
        self._refresh_completion_listeners()
        if tracked:
            self._unsubscribers.append(
                async_track_state_change_event(self.hass, tracked, self._changed)
            )
        self._unsubscribers.append(
            self.hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, self._changed)
        )
        self._unsubscribers.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._smart_changed)
        )

        @callback
        def started(event):
            # HA removes a one-shot listener before calling it. Do not try to
            # unregister that consumed listener again during a later reload.
            if cancel_started in self._unsubscribers:
                self._unsubscribers.remove(cancel_started)
            self._changed(event)

        cancel_started = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, started)
        self._unsubscribers.append(cancel_started)
        if tracked:
            self._unsubscribers.append(
                async_track_entity_registry_updated_event(
                    self.hass, tracked, self._registry_changed
                )
            )

    @callback
    def _registry_changed(self, event):
        self.hass.async_create_task(self.hass.config_entries.async_reload(self.entry.entry_id))

    @callback
    def _changed(self, event):
        if self._closed:
            return
        if getattr(event, "event_type", None) == EVENT_CORE_CONFIG_UPDATE:
            self._invalidate_insights()
        task = self.hass.async_create_task(self.async_evaluate())
        self._evaluation_tasks.add(task)
        task.add_done_callback(self._evaluation_tasks.discard)

    @callback
    def _smart_changed(self, event):
        eid = event.data.get("entity_id")
        for zid in self.zones:
            policy = self.smart_record(zid)["policy"]
            ids = {i["entity_id"] for i in policy["entities"]} | {policy["timer"]}
            feature_ids = self.insights.source_ids(zid)
            ids |= feature_ids
            if eid in ids:
                if eid in feature_ids:
                    self._invalidate_insights()
                self._changed(event)
                break

    def _observation(self, zone_id):
        entity_id, timer_id, unit = self.mappings[zone_id]
        timer_id = self.smart_record(zone_id)["policy"]["timer"]
        state = self.hass.states.get(entity_id)
        timer = self.hass.states.get(timer_id) if timer_id else None
        boost_unknown = bool(timer_id) and (
            timer is None or timer.state not in {"active", "idle", "paused"}
        )
        # Paused external boosts retain ownership; never assume they have ended.
        boosted = timer is not None and timer.state in {"active", "paused"}
        until = None
        if boosted and timer.attributes.get("finishes_at"):
            try:
                until = timestamp(timer.attributes["finishes_at"])
            except ValidationError:
                pass
        if state is None or state.state in {"unknown", "unavailable"}:
            return Observation(False, False, None, None, boosted, until, boost_unknown)
        attrs = state.attributes
        target = attrs.get("temperature")
        try:
            target = number(target)
        except ValidationError:
            target = None
        limits = self.zones[zone_id].limits
        try:
            hardware = Limits(
                number(attrs.get("min_temp")),
                number(attrs.get("max_temp")),
                attrs.get("target_temp_step"),
            )
            hardware.validate(limits.minimum)
            hardware.validate(limits.maximum)
            supported = (
                "heat" in attrs.get("hvac_modes", [])
                and bool(
                    int(attrs.get("supported_features", 0))
                    & ClimateEntityFeature.TARGET_TEMPERATURE
                )
                and attrs.get("temperature_unit") == unit
            )
        except (ValidationError, ValueError, TypeError):
            supported = False
        return Observation(True, supported, state.state, target, boosted, until, boost_unknown)

    async def async_evaluate(self):
        async with self._lock:
            if self._closed:
                return
            from zoneinfo import ZoneInfo

            self.controller.timezone = ZoneInfo(self.hass.config.time_zone)
            now = dt_util.utcnow()
            deadlines = []
            for zone in self.zones.values():
                try:
                    self.status[zone.id] = await self._smart_evaluate(
                        zone, self._observation(zone.id), now
                    )
                    deadlines.append(self.status[zone.id]["next_evaluation"])
                except (ValidationError, OSError):
                    self.status[zone.id] = {
                        "target": None,
                        "source": "error",
                        "error": "storage_or_validation",
                        "reason": "Review storage and zone limits",
                        "observe_only": True,
                    }
                    _LOGGER.error("Preview calculation failed for zone %s", zone.id)
            if self._cancel_deadline:
                self._cancel_deadline()
                self._cancel_deadline = None
            if deadlines:
                self._cancel_deadline = async_track_point_in_utc_time(
                    self.hass, self._changed, min(deadlines)
                )
            for listener in tuple(self.listeners):
                listener()

    async def async_save_week(self, zone_id, week, revision):
        zone = self.zones[zone_id]
        result = await self.repository.save_week(zone_id, week, zone.limits, revision)
        await self.async_evaluate()
        return result

    @callback
    def subscribe(self, listener):
        self.listeners.add(listener)
        return lambda: self.listeners.discard(listener)

    @callback
    def close(self):
        if self._closed:
            return
        self._closed = True
        for task in tuple(self._evaluation_tasks):
            task.cancel()
        self._evaluation_tasks.clear()
        for task in tuple(self._feature_reads.values()):
            task.cancel()
        self._feature_reads.clear()
        for listener in tuple(self.listeners):
            listener()
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        for unsubscribe in getattr(self, "_completion_unsubscribers", []):
            unsubscribe()
        self._completion_unsubscribers = []
        self._unsubscribers.clear()
        if self._cancel_deadline:
            self._cancel_deadline()
            self._cancel_deadline = None
        self.listeners.clear()


@callback
def async_register_services(hass):
    """Only integration-local preview mutations; never generic service execution."""
    if hass.services.has_service(DOMAIN, "update_schedule"):
        return

    async def update(call):
        entry = hass.config_entries.async_get_entry(call.data["entry_id"])
        if entry is None or entry.domain != DOMAIN or not getattr(entry, "runtime_data", None):
            raise HomeAssistantError("Scheduler entry is not loaded")
        runtime = entry.runtime_data
        if call.data["zone_id"] not in runtime.zones:
            raise HomeAssistantError("Unknown zone")
        try:
            await runtime.async_save_week(
                call.data["zone_id"], call.data["week"], call.data["revision"]
            )
        except (ValidationError, OSError) as err:
            raise HomeAssistantError(str(err)) from err

    async_register_admin_service(
        hass,
        DOMAIN,
        "update_schedule",
        update,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): str,
                vol.Required("zone_id"): str,
                vol.Required("revision"): vol.All(int, vol.Range(min=1)),
                vol.Required("week"): dict,
            }
        ),
    )

    async def resume(call):
        entry = hass.config_entries.async_get_entry(call.data["entry_id"])
        if entry is None or entry.domain != DOMAIN or not getattr(entry, "runtime_data", None):
            raise HomeAssistantError("Scheduler entry is not loaded")
        runtime = entry.runtime_data
        zone = runtime.zones.get(call.data["zone_id"])
        if zone is None:
            raise HomeAssistantError("Unknown zone")
        try:
            await runtime.controller.resume(zone)
            await runtime.async_evaluate()
        except (ValidationError, OSError) as err:
            raise HomeAssistantError(str(err)) from err

    async_register_admin_service(
        hass,
        DOMAIN,
        "resume_preview",
        resume,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): str,
                vol.Required("zone_id"): str,
            }
        ),
    )
