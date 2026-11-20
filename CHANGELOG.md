# 0.5.3 — household boost permissions

Non-administrators with mapped thermostat read/control permission can start/cancel bounded boost. External adapters additionally check script/helper control permissions. All other scheduler mutations remain administrator-only.

99 standalone tests, 44 real-HA simulated runtime tests and 46 frontend/model/DOM tests pass locally. Coverage includes denied access, forged settings commands, adapter permissions, full/mini boost and disconnected cards. Live acceptance as a household non-admin remains to be confirmed after installation.

# 0.5.2 — scheduled preheating independent of presence

- Away/unknown presence alone no longer blocks preheating of saved schedule periods.
- Occupancy remains an additional minimum; preheating can raise it for a higher upcoming scheduled target and never lower it.
- Explicit Away targets, manual/Off, boost, holiday and device guards retain priority.
- Preserve the ordinary occupied floor if optional preheating persistence fails.
- Report the actual pause reason in the companion card. No saved heating settings or learned models reset.

# 0.5.1 — separate frost minimum

- Allow frost below the normal schedule/manual minimum without changing normal target limits.
- Validate frost and recovery targets against the available thermostat’s actual range and current increment, including on save/import and before control.
- Expose verified frost limits to the card and restore owned Off cycles if capabilities become incompatible.
- Preserve existing schedules, user settings and Off permission. No automatic frost activation or target changes.

# 0.5.0 — guided setup, frost protection and configuration transfer

- Four-step HA zone wizard with capability-derived suggestions, review and disabled-by-default new zones.
- Optional per-zone frost minimum, explicit Off permission, bounded recovery target and persisted restart/Off restoration intent.
- Weekly schedule and zone-settings JSON export, validated preview/import, disabled-zone settings restoration and schedule copying.
- Learning progress, estimated warm-up/start and last measured preheat result.
- Preserve existing schedules, control intent, mini-card layout and learned models; new frost options default off.
- See docs/SETUP_FROST_BACKUPS.md for scope, report-freshness and configuration-restore interruption limits.

# 0.3.1 - boost migration and reload cleanup

## 0.4.0 — stable

- Promote the tested 0.4 backend and compact card feature set to stable releases.
- Calendar/Workday profiles, local temperature/decision history and optional measured preheating.
- Compact history charts, preserved profile drafts, simpler Settings and Add heating zone shortcut.
- Remove development wording from new integration setup and user-facing names.
- Preserve existing schedules, storage schemas, control intent and legacy service IDs.
- Preheat timing accuracy and third-party calendar/hardware coverage remain field-validation limits.
- Guided configurator and frost protection are planned, not included.


## 0.4.0.dev1 — local candidate

- Optional per-zone named day profiles with ordered calendar and Workday rules.
- Bounded seven-day room/target measurements and final decision explanations.
- Measured warm-up learning and opt-in, capped preheating that respects existing owners.
- Separate versioned feature storage, stale edit protection, source-failure warnings,
  and cancellation of queued evaluation jobs during unload.
- Companion UI adds History, profile editing and preheat settings; minis stay compact.
- Pending live multi-cycle learning and calendar acceptance; no stable release yet.


- Zone settings show the active card policy's boost timer, allowing the initial timer mapping to be cleared after migration to built-in boost, even after the old helper has been removed. Settings cannot select a timer that disagrees with the active boost policy.
- Do not unregister a consumed Home Assistant startup listener during a later integration reload, avoiding an unnecessary core error log entry.
- Add regression tests for both cases: 49 standalone and 28 real-HA simulated runtime tests pass. Compatible with card 0.3.0; schedules and active control intent are preserved.

# 0.3.0 - first stable release

- Promote the tested multi-zone scheduler with schedules, boost, occupancy override, holidays and explicit observe-only/heating-control handover.
- Add real-HA runtime regression coverage for an active built-in boost through restart and holiday return while a receiver is unavailable.
- Complete live two-zone external boost expiry, holiday activation/cancel/return and Core restart acceptance. External countdown continuity requires timer helper restoration enabled; expiry during downtime can require explicit restoration confirmation.
- Update installation guidance and publish the scope and limits in `docs/RELEASE_ACCEPTANCE.md`. Compatible with companion card 0.3.0; requires Home Assistant Core 2026.11.1+.
- Preserve existing storage, API, schedule and control-mode behavior. Historical Preview setup names remain for this release.

# 0.3.0.dev2 - zone setup lifecycle fix

Reload the integration when zone subentries change, so newly added zones become available without a manual reload. Classify the multi-zone scheduler as a service so HA displays its zone subentries and management controls. Adds a real-HA config-entry update-listener test. Existing schedules and control-mode intent are preserved.

# 0.3.0.dev1 - Phase 3 development candidate

Added working per-zone boost, presence and holiday controls; persistent deadlines/ownership, guarded optional climate commands, adapter completion signals, policy revisions and admin-only mutations. Default remains observe-only. Updated responsive card and disconnected demo. Full installation/hardware validation and release work remain.

# Changelog

## 0.2.0.dev1 — companion card API

- Added versioned dashboard reads/subscriptions and revision-protected schedule saves.
- Filtered card data by mapped climate read permissions; restricted saves to administrators.
- Notify subscribers when the runtime closes so cards can reconnect.
- Expanded real-HA smoke coverage to eight tests. Heating actuation remains unavailable.

## 0.1.0.dev1 — Phase 1 observe-only backend

- Added validated weekly scheduling with DST and midnight handling.
- Added revision-protected storage, manual holds and existing boost observation.
- Added HA configuration/subentry flows, proposed-target sensors, diagnostics and local preview actions.
- Added standalone scheduling, persistence, concurrency and restart tests.
- Added four smoke tests against real HA 2026.11.1 APIs with simulated entities; enabled atomic storage writes and made shutdown cleanup idempotent.
- No live command writer, Lovelace card, publication or production deployment.

This is the first development schema. Unsupported schema versions fail validation; migrations will be added alongside future schema changes.
