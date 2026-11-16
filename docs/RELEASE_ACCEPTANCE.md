# Release acceptance — 16 November 2026

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
