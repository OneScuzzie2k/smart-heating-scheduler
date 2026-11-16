# Release acceptance — 0.4.0, 16 November 2026

The stable pair promotes integration 0.4.0.dev1 and card 0.4.0-dev.3, with
release metadata, clearer setup naming and documentation updates. No heating
policy, storage schema or learned-model behavior changes in this promotion.
Supported baseline: Home Assistant Core 2026.11.1+ and a supported single-target
heating climate entity. Live coverage is one two-zone Hive installation.

## Automated acceptance

- 76 standalone logic/storage tests; HA-specific tests run separately.
- 33 real Home Assistant runtime/API tests using simulated devices.
- 38 frontend model/DOM tests and a self-contained production card build.
- Backend Ruff lint/format checks and GitHub backend, card, HACS and hassfest validation.

Coverage includes profile precedence and future Workday checks, all-day/DST
calendar boundaries, unavailable inputs, slow nonblocking calendar reads while
boost remains usable, storage failures, revision conflicts, learning thresholds,
Celsius/Fahrenheit observations, stale/missing readings, preheat restart intent,
existing override priority, unloading and mini-card regressions.

## Live acceptance of the promoted feature set

| Check | Result |
| --- | --- |
| Installation and Core restart | Both existing zones and control intent recovered; commands withheld while ZHA receivers were unavailable. |
| Built-in boost after cleanup | Natural expiry, cancel and original-deadline restart recovery passed during migration. |
| Mini boost on the 0.4 build | One-minute boost reported heating at its target, expired naturally and returned to the current occupancy rule. |
| Mini manual target / Resume schedule | Actual target changed, manual owner appeared, and resume restored the current scheduled target. |
| Scheduled transition | Both zones followed their evening schedule at the configured boundary; occupied minimum handed back correctly. |
| Profile editor | Named weekday/weekend copies persisted on both zones without activating calendar rules. Live input-name loss was fixed and regression-tested. |
| History | Real temperature/target samples and final boost/manual/occupancy/schedule decisions appeared; compact 1-hour/24-hour/7-day views checked. |
| Zone setup | Card shortcut opened HA's Add heating zone form; no duplicate/test zone was submitted. |
| Logs | No scheduler errors after restart/reload and supervised acceptance. |

## Explicit limits

Calendar and Workday selection logic is tested with simulated inputs; live
third-party calendar/Workday mappings were not activated on this installation.
The preheating learner was enabled on one live zone but had not accumulated
sufficient accepted warm-up observations at acceptance. **Real multi-cycle
preheating timing accuracy is not yet established.** Preheating remains optional,
disabled on new zones by default, and bounded by its configured maximum lead.
It uses a measured warming rate, not weather, cooling or radiator/floor lag models.

Full server bootstrap, wider heating hardware, translations and independent
accessibility review remain follow-up coverage. Existing thermostat integration
and appropriate per-zone configuration remain necessary. Stable release status
is a package/distribution designation, not a guarantee of universal compatibility.
Guided configurator and frost protection are planned and are not included.

Private device IDs, household mappings, logs and live screenshots are excluded
from the public repositories. The older acceptance record below is historical.

---

# Historical 0.3 acceptance — 16 November 2026

Candidate versions: integration `0.3.0.dev2`, card `0.3.0-dev.5`; Home Assistant Core `2026.11.1`. These results cover one live installation with two Hive receivers and an external script/timer adapter. They do not establish compatibility with every heating system.

## Automated checks

- 49 standalone tests passed; the HA-specific module is skipped in that host environment and run separately.
- 26 tests passed using the real Home Assistant API/runtime with simulated devices. New regressions recreate the runtime during an active built-in boost without extending its deadline, and after a holiday ends while its thermostat is unavailable.
- 32 frontend model/DOM tests passed; the production card bundle built successfully.
- Ruff checks and formatting passed for the backend regression changes.

The HA runtime suite does not replace a full server bootstrap or physical device testing.

## Live results

| Check | Result |
| --- | --- |
| Five-minute external boost on each zone | Passed: both timers expired naturally, restoration scripts emitted completion events, and current rules resumed without manual confirmation. |
| Return to occupancy minimum after boost | Passed: occupied zone returned to 20°C; the other zone returned to its 16°C schedule. |
| Scheduled holiday activation | Passed on both zones with distinct 16°C and 18°C targets. |
| Holiday blocks external boost | Passed: the card rejected the request and no timer started. |
| Holiday survives Core restart | Passed: original return deadline and active target retained. |
| Holiday ends during startup/device unavailability | Passed: writes waited for the receiver, then the current 16°C schedule applied. |
| Automatic holiday return after recovery | Passed: occupied zone returned to its 20°C minimum at its original return time. |
| Manual holiday cancellation | Passed: the other zone immediately returned from 18°C to its current 16°C schedule. |
| Control ownership after restart | Passed: heating control remained enabled and the previous schedule writer remained disabled. |
| Active external boost across Core restart | Passed on both zones with timer restoration enabled: original expiry instants retained, both receivers stayed under external boost ownership, then current rules resumed automatically at expiry. |

A settings backup was completed before the live restart. Both zones were returned to normal rules, with no active boosts or test holidays. Timer restoration was enabled temporarily with the owner's approval and returned to its original disabled setting afterward, as requested. The successful countdown recovery result therefore requires enabling timer restoration on installations that need it; it is not a claim about the final disabled configuration.

## Scope and remaining limitations

The requested live acceptance checks passed on this installation. A stable release does not remove each installer's responsibility to validate their device and adapter contract. Downtime that spans an external timer's expiry requires explicit verification of restoration; an idle timer does not prove the stop script completed. That guarded path is covered by simulated runtime tests, not this live restart, which completed before expiry. Built-in boost restart/expiry behavior is also covered by simulated runtime tests, not a live change of this installation's boost owner. Full server bootstrap/config-flow lifecycle coverage, additional hardware and accessibility field review remain follow-up work. Private entity mappings, logs and screenshots are excluded from these public repositories.
