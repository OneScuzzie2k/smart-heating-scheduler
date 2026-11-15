# Approval-gated development

## Phase 1 preview

Build and test the independent schedule/storage/controller first. Expose configuration and proposed state through HA without a heating writer. Local tests do not authorise production installation. Real HA setup/config-flow/lifecycle tests are required before an installation candidate.

## Phase 2 — companion card

Separate card repository/build output, sharing a versioned backend interface. Dashboard, Schedules, Automation and Settings retain the approved orange/teal rounded-card visual language while respecting HA themes.

Use container-responsive layouts, a single-column phone editor, optional desktop timelines and explicit period forms. Include save/cancel, stale-revision recovery, copy-day/week, short-segment summaries, touch/keyboard access, labelled current/target values, empty/error/reconnect states and source explanations. Do not reproduce HA navigation or a phone status bar inside the card.

Validate narrow card containers as well as 360/390/768/1024/1440 CSS-pixel viewports, light/dark themes, long names and 200% zoom. Boiler demand/status and room imagery are optional, explicitly mapped capabilities.

## Phase 3 — complete control policies

All policies are configured in the UI and stored/validated by the backend. Use installation defaults with explicit per-zone inheritance/overrides. Avoid a free-form precedence editor; offer named policies with a preview of their effects.

| Setting | Proposed initial behavior | UI choices |
|---|---|---|
| Temperature source | Selected climate measurement | Explicit external display sensor; labelled fallback; does not change thermostat feedback |
| Manual target | Until next period | Duration, next boundary, until resume |
| Manual off | Until explicit resume | Duration or next boundary only after live-adapter support is tested |
| Holiday conflict | Supersede pre-existing temporary targets; respect off | Block temporary exceptions, acknowledge timed exceptions, allow an existing boost to finish |
| Presence | Disabled until entities are selected | Any/all with explicit sentences, per-zone selections, named-zone/sensor meanings |
| Away/return delay | 10 minutes / immediate | Configurable delays and current-period/next-boundary return |
| Unknown occupancy | Last trusted 10 minutes, then schedule | Grace duration and approved fallback |
| Forced Home/Away | Two hours | Custom duration, next boundary or explicit indefinite mode |
| Occupied comfort minimum | No rule on new installations | Entities, zone, days, time window and target |
| Built-in boost | Proposed 21°C / 30 minutes, reviewed and converted for the site's unit | Target/duration/presets and limits |
| Existing boost | Keep external ownership | Mapped start/cancel/extend, helpers/timer/deadline/completion signals, declared capabilities |
| Restart | Restore unexpired intent by original deadline | Adapter-appropriate recovery; never restart full boost duration |
| Device unavailable | Suspend writes and show fault | Bounded retries and reconciliation on recovery |

Review application minimum/maximum, comfort, away, holiday and fallback targets during setup. Do not infer safe building temperatures from a device minimum or impose Celsius defaults on Fahrenheit users. Lowering an accepted minimum requires explicit review. A temperature floor does not silently turn a manually off device back on.

The state machine must preserve exactly one active owner. A boost started explicitly during an off suspension returns to that suspension on expiry. Holiday expiry re-evaluates current rules, without resurrecting superseded temporary holds. Missing/unknown presence is never automatically away. Avoid double-counting persons and underlying trackers.

Only this later phase introduces a climate command writer, target comparison, serialized commands, throttling, bounded retries/backoff and complete ownership handover. Generic climate actions remain separate from optional device-specific hold adapters. Do not use the provisional five-second preview guard as the final ownership protocol.

## Phase 4 — release and staged validation

Run the full schedule, restart, DST, failure, presence, priority, concurrency, storage-migration and accessibility matrix. Run HA integration/config-flow tests and hassfest on the selected supported HA version. Finish documentation, translations, licence choice, actual GitHub ownership/URLs, HACS metadata, version compatibility, release ZIPs and backup/rollback procedures.

Public repositories contain reusable code and generic examples only. Home-specific audits, entity inventories, credentials and location data remain outside release repositories. Publishing, installation, disabling old writers, live acceptance tests and production takeover each require the applicable approval.

## Phase 3 candidate handoff - 15 November 2026

Independent per-zone boost/presence/holiday controls, guarded optional writer, durable ownership/deadlines, filtered external completion signals and card forms are implemented in 0.3.0.dev1. See CONTROL_POLICIES.md and VALIDATION.txt for the exact supported surface and tests. Global inheritance/bulk holiday scope, display sensors and richer return/off options remain follow-up scope; this candidate does not claim those are available. Live HA remains unchanged and takeover is still approval-gated.
