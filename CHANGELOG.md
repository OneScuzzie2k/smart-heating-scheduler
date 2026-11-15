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
