# Smart control policies — development candidate

New installations start in **observe only**. Boost, holiday and presence actions update durable proposals; no climate/script/helper commands are sent in that mode. The card always identifies sample data, observe-only or heating-control mode.

Heating-control mode is global to the scheduler, includes newly enabled zones, persists across restarts, and requires the administrator to type `ENABLE HEATING CONTROL`. This is an explicit takeover action, not part of initial setup. Start with simulated climates or observe-only comparison; use the [installation handover](INSTALLATION.md#6-hand-over-heating-control) before a supervised live test. Disable competing schedule writers before takeover. Returning to observe-only is blocked until built-in boosts have been stopped. Unloading HA/integration stops command processing; hardware keeps its last command until independently changed or the controller resumes.

## Per-zone rules

The companion card exposes independent settings for every zone. No people, home-specific entity IDs, fixed Celsius targets or building safety assumptions are bundled. Configure the reviewed application bounds in the integration setup; configure boost/presence targets in the zone's unit in the card.

Presence selections accept persons, device trackers and binary sensors with explicit Home/Away state meanings. Choose a person or their trackers, not both. `not_home` for a person/tracker includes named away zones. Any-selected-Home means Away only when all selected entities are known away; All-selected-Home means one known away entity suffices for Away. Unknown inputs follow three-valued logic, not an automatic Away assumption.

Away delay defaults to ten minutes; return is immediate. Unknown occupancy holds the last trusted result for ten minutes, then follows the schedule by default. Delays, unknown fallback and targets are editable. Candidate transitions, their start time and unknown-grace deadlines persist. Home/Away overrides can expire by duration or the next actual weekly boundary, or remain until cleared. Auto clears only the override. Home returns to the weekly target unless a separately configured occupied comfort floor applies.

An optional occupied minimum applies on selected days in a same-day start/end window while Home. It honours higher scheduled targets. Window edges and DST offset transitions trigger evaluation. It is subordinate to manual target holds, holiday and explicit Off. New users have no floor configured.

Holiday has per-zone start/return instants, target and either blocked or acknowledged timed boost exceptions. Repeat configuration on the zones to include; other zones are excluded. UI times use the HA timezone, not browser timezone. Nonexistent and ambiguous DST wall times are rejected with a clear message. Backend timestamps require an offset and are stored as UTC instants. Activation clears pre-existing manual target holds and unacknowledged built-in boosts; it never resurrects those later. Explicit Off stays Off. Return evaluates current rules. Finish external boosts before configuring holiday; external holiday exceptions are not supported by the generic script adapter.

## Boost ownership and commands

Choose exactly one owner: built-in, external scripts/timer, or none. A timer cannot remain mapped to a built-in owner. External timer/scripts/helper mappings cannot be shared between zones.

Built-in boost validates target and duration, saves its original absolute deadline before commanding, and re-evaluates current rules on expiry/cancel. Boost started from Off returns to an Off hold. A new physical Off during boost cancels it. An Off device found at startup also cancels an old boost, since the user may have switched it off while HA was offline. Reboot never starts a fresh full-duration boost. The UI requires a separate acknowledgement for a permitted holiday exception.

External adapters declare a fixed target, duration/default/maximum, start/cancel scripts, timer and optional duration helper. This adapter does not inject a temperature into arbitrary scripts. Direct script calls are awaited rather than launching `script.turn_on`. Helper min/max/step constrain requested durations. The timer must confirm start and release after cancel. Scripts are never called in observe-only mode and are not blindly retried after a partial failure.

For automatic expiry handover, optionally map a custom completion event, one event-data field, and its exact value. The event must be emitted **after all receiver restoration actions complete**. Timer release alone cannot prove this. A matching completion signal releases the guard only with a known idle timer. Without it, timer release or uncertain script execution inhibits writes until the administrator checks restoration and confirms it in the card. Active ownership is persisted, including when HA is offline at expiry. An unknown/paused timer never silently gives control back. Verify the actual script/event contract before hardware testing.

Generic climate commands are serialized, compared against observed mode/target and throttled. Unconfirmed commands get at most three attempts with bounded delays, then a visible fault and explicit Retry controls. Unavailable/unsupported entities inhibit commands. Own-context and exact pending-command confirmations do not become manual holds. Other native changes remain manual changes; HA cannot always identify which other automation caused one. Device-specific hold handling stays separate and must be retained until tested replacement support exists.

## Storage and API

Logical storage schema 2 migrates schema 1 schedules, holds and revisions without enabling control. HA Store's enclosing version remains 1 because migration occurs inside the payload. Save-before-publish transactions retain old memory on failure. Policy edits use independent revisions to reject stale editors; weekly edits retain their schedule revision checks. Invalid storage/limits suspend calculation and commands.

Authenticated WebSocket API schema 1 adds optional `smart`, `presence` and boost-capability fields to zone snapshots and a new administrator-only `smart_heating_scheduler/control` endpoint. Its action allowlist is policy, presence, holiday, boost_start, boost_cancel, resume, retry, reconcile and mode. Policy payloads contain `{policy, revision}`. No endpoint executes arbitrary action names or arbitrary service domains. Read snapshots follow climate entity permissions; they contain no tracker GPS coordinates. Minimal diagnostics omit entity IDs, presence selections, schedules and locations.

The existing `update_schedule` and `resume_preview` services remain administrator-only. Saving a schedule can affect heating when control mode is enabled. `resume_preview` clears the stored hold but does not switch on a physically Off thermostat; use the card's explicit Resume schedule action to do that after takeover.

## Remaining release work

This is a test candidate, not a HACS release or completed hardware integration. Remaining work includes full HA bootstrap/config-flow/platform tests, supported Linux testing, physical receiver and wall-control acceptance, accessibility/zoom/theme review, installation defaults with explicit per-zone inheritance, bulk holiday scope, external display-sensor configuration, richer return/off policies, import/export, translations and release metadata/licensing. Basic per-zone settings already work independently. Do not label unimplemented bulk/inheritance options as available.
