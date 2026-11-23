# Receiver-backed heating: architecture and rollout

This work is on `feature/receiver-schedule-foundation`. It is not part of the
stable release and does not change the active controller.

## Intended behaviour

Each zone chooses a schedule owner. Ordinary HA scheduling remains available for
other thermostats. A supported Hive/ZHA receiver can eventually own its weekly
schedule: HA reads and edits that same timetable, while the receiver executes it.
No hot-water entities, controls or endpoint writes are included.

Keep the existing configurable zone rules:

| Feature | Intended receiver-owned operation |
| --- | --- |
| Weekly schedule | Receiver executes its stored timetable, including when HA is unavailable. This requires a verified handover. |
| Occupancy override | HA applies a configurable temperature floor for selected people/sensors, days and hours. Higher scheduled targets remain higher. Leaving or reaching the end time releases the override to the receiver's current scheduled target. |
| Smart preheating | HA learns warming rates and temporarily starts heating before an upcoming receiver schedule transition. Presence alone does not gate it. |
| Boost | A verified finite receiver override, with expiry/readback checks and explicit cancel. Keep the dial locked during an active boost. |
| Manual | An explicit receiver hold; distinguish a hardware hold from an HA schedule target. |
| Holiday and frost | HA-managed rules with the existing configurable targets and Off behaviour. |
| Card | Responsive heating-only dial, actual mode feedback, boost action and details. Existing compact cards remain supported. |

HA-dependent rules need HA running. Hardware timer expiry must be demonstrated on
each supported receiver/firmware before claiming an override reliably returns to
the local schedule during an HA outage. A radio send acknowledgement is not proof
that a timetable or mode was stored.

## Stage A: implemented foundation

- Optional adapter for reviewed single-channel Hive receiver models through ZHA,
  using the mapped heating endpoint only. Imports are lazy; non-ZHA zones continue
  to work through the existing HA controller.
- Read responses are correlated to one request and must cover all seven days.
  Timeout, malformed, cooling-only and conflicting replies are rejected. Temporary
  listeners are removed on success, error, timeout and cancellation.
- Celsius conversion, maximum six transitions per day and half-degree setpoints.
  Native Off markers and overnight carry-over are retained honestly in reads;
  they are not silently converted into ordinary HA schedule temperatures.
- Admin-only read, preview, copy and backup-restore API. Preview freezes the saved
  HA week or a selected receiver backup for ten minutes. Unsaved card drafts do
  not participate. No automatic synchronisation or overwrite on reconnect.
- A write requires a disabled HA zone, no active/uncertain boost, current schedule
  revision, unchanged mapping, a matching fresh read and the explicit phrase
  `WRITE RECEIVER TIMETABLE`.
- Before the first radio command, persist the complete original and proposed
  timetable in a separate atomic HA Store. Retain the last five operations.
  Write only changed days, then read all seven days again. Only an exact match
  becomes `verified`. Interrupted operations remain `writing` or `uncertain`
  after restart. Restoring a backup uses the same preview and readback process.
- Existing HA scheduling remains the owner. This stage never writes mode, hold,
  boost, reporting configuration or a clear-schedule command. No automatic
  rollback is attempted after an uncertain radio write.

The backup Store is included in normal HA configuration backups. Download an
additional copy before removing the integration: explicit integration removal
also removes its local receiver backup Store. Backups contain device identifiers
and should be reviewed before sharing publicly.

The reference transport is attributed in `THIRD_PARTY_NOTICES.md`.

## Stage B: hardware qualification and owner handover

The development checkpoint `0.6.0-dev.2` implements a supervised native timer
test, not permanent receiver ownership. In the card's receiver disclosure, open
**Hardware timer qualification**:

1. Inspect the actual receiver mode, then prepare the test. Preparation is
   read-only and records the original indefinite Manual hold and native week.
   It requires a fresh room reading at least 1°C above the unchanged Manual
   target, and no higher native period within the next five minutes.
2. Pause competing receiver controllers for the test window and disable only
   this zone's HA scheduling. Review the two-minute test and type
   `TEST RECEIVER TIMER`. Normal household boost permission does not grant
   access to these administrator tools.
3. The test saves the original hold before changing any modes, selects native
   Schedule, verifies a low target, and sends all four boost attributes in one
   frame. It requires the receiver to report Boost, the unchanged target and a
   bounded one/two-minute countdown. Unknown/65535 countdowns are failures.
4. HA polls for native return to Schedule before sending a restoration command.
   Hardware expiry, agreement with the current native target, and restoration
   of the original Manual hold are three separate results. Timer values have
   minute resolution; observations are timestamped rather than pretending an
   exact second was measured. An HA watchdog restore is never a hardware-expiry
   pass. A physical Off or unrelated new manual target is not overwritten.
5. Original state, phase, deadline and observations survive reloads. After a
   reload/Core restart the first fresh native read is recorded *before*
   restoration. `after_reload` includes integration reloads; this field alone
   does not prove a Core outage. A supervised outage test needs its actual stop
   and recovery times recorded too.
6. Confirm original-hold restoration, restore paused automations and re-enable
   the zone. Re-enabling is blocked while a test is active. A lost radio or
   unverified restoration stays visible for review; do not treat it as passed.

The receiver may apply its own clock/timetable when Schedule is selected. A
fresh scheduled-target read is checked immediately and a warmer-than-reviewed
target aborts the test. Exact clock/DST and a real scheduled boundary still
need qualification before handover.

Supervised acceptance of the foundation has confirmed timetable write and
original restoration on one OTR1 receiver with firmware `0x01000108`: both
operations matched a complete seven-day readback. That does not establish mode,
timer, outage or other-model compatibility.

Remaining handover acceptance:

1. Take a current HA backup and export both HA weeks/settings. Inventory all
   automations that command the receivers, especially any forced-hold controller.
2. Read both receivers without changing their mode or timetable. Verify the
   physical receiver identity, response coverage, native Off semantics, time zone,
   clock/DST behaviour and transition capacity.
3. With an explicitly reviewed test window and one disabled HA zone, copy a
   timetable, compare fresh readback, restore the original and compare again.
   Verify that a physical timetable edit is detected rather than overwritten.
4. Test finite override, cancel, scheduled-boundary crossing, HA restart, radio
   loss and an HA outage through expiry. A missing or invalid countdown must
   become unresolved, never silently successful. Do not enable native ownership
   until return-to-schedule behaviour is proven.
5. Implement a persisted per-zone owner state machine: HA / preparing / receiver /
   unresolved. Switch ownership only after timetable and actual mode verification.
   Disable conflicting forced-hold automations as part of the reviewed handover.
   Keep a verified route back to the existing HA controller.

## Stage C: overlays and production card

Implement one per-zone arbiter for boost/manual/holiday/frost/preheat/occupancy,
with explicit ownership, priority and expiry. Reuse the existing rule evaluation
and settings; a native transport executes bounded holds instead of continuously
overwriting schedule targets. Read actual receiver mode and countdown before
renewing or releasing a lease. Never cancel an unrelated physical override.

On reconnect/restart, reconcile the persisted operation with current hardware;
do not assume an old HA target is still authoritative. Receiver mode, timetable
and effective target need separate fields. Release overlays to the *current*
receiver schedule, not a cached target captured when the override began.

Bring the approved heating-only dial design into the production card after these
states exist. Validate desktop/tablet/mobile, light/dark themes, keyboard/touch,
equal-height cards, ordinary household boost permissions and locked boost dials.
Run the existing HA-owned controller regression suite as well as native-mode
tests before releasing.
