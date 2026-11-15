# Smart Heating Scheduler - Phase 3 development candidate

A reusable Home Assistant integration with a separate companion Lovelace card. New installations start in **observe only**. This build now includes an optional climate writer, so it must not be treated as incapable of changing heating: enabling control is a separate, explicit administrator action.

## Working features

- Reviewed per-zone thermostat mappings, limits, units and seven-day schedules.
- Persistent manual holds, weekly revisions, DST-aware boundaries and event/deadline-driven evaluation.
- Built-in timed boost with absolute restart deadlines, cancel, and return-to-Off behaviour.
- Generic external boost adapter with awaited start/cancel scripts, fixed target declaration, timer ownership, optional duration helper and filtered completion signal.
- Explicit presence entities/state meanings, any/all logic, configurable delays, unknown grace/fallback, expiring Home/Away overrides and optional occupied comfort minimum.
- Per-zone holiday dates/targets and acknowledged built-in boost exceptions; current-rule resumption on return.
- Administrator-only mutations, permission-filtered card subscriptions, stale policy/schedule rejection, minimal diagnostics and three status sensors per zone.
- Optional serialized/throttled climate commands with three-attempt limits, availability guards and explicit handover faults.

Read [control policies](docs/CONTROL_POLICIES.md) for priority, configuration, adapter contracts, restart handling and remaining limitations. No home-specific IDs or policies are bundled. Targets are entered in the chosen climate unit; no Celsius default is imposed on Fahrenheit installations.

## Testing

Use the disconnected browser preview first. For backend testing, use an isolated HA instance with simulated entities. No production installation or control takeover has been performed. The initial target is HA Core 2026.11.x; local API tests use real HA 2026.11.1 objects and simulated states, without starting an HA server or connecting to live heating.

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest
python -m unittest tests.ha.test_runtime -v
```

The HA test command requires Python >=3.14.2 with homeassistant==2026.11.1; standalone logic tests also run on Python 3.13. Restricted Windows environments may need permission for asyncio's internal loopback socket. See VALIDATION.txt for the actual checks and outstanding work.

## Install through HACS / Marketplace

Development preview: start in observe-only mode and keep your existing heating schedule in charge. Hardware acceptance is still pending.

1. In HA 2026.11, open Settings > Marketplace. In older supported HACS interfaces, open HACS.
2. Add `https://github.com/OneScuzzie2k/smart-heating-scheduler` as a custom **Integration** repository, then download it. Select the default branch for this initial development preview if no release is listed.
3. Add `https://github.com/OneScuzzie2k/smart-heating-scheduler-card` as a separate **Dashboard** repository and download it.
4. Follow any restart or browser-reload prompt. Add Smart Heating Scheduler under Devices & services, then add zones and review units, limits and baseline.
5. Add the companion card through the dashboard editor and select the scheduler entry. Confirm **Observe only**, then compare its proposals with the existing schedule. Do not enable Heating control during this initial test.

The card is a separate repository because HACS/Marketplace installs one item of one type per repository. See [the companion card](https://github.com/OneScuzzie2k/smart-heating-scheduler-card) and the [repository structure reference](https://contributors.home-assistant.io/marketplace/reference/repository-structure/).

![Scheduler icon](custom_components/smart_heating_scheduler/brand/icon.png)

## Distribution status

Development version 0.3.0.dev2. Public GitHub development source prepared for HACS/Marketplace custom-repository installation. This is not a catalog-listed release or a live deployment. Full HA bootstrap/platform/config-flow testing, Linux and hardware acceptance, inheritance/bulk configuration, import/export, accessibility, translations and broader release validation remain before a stable release. MIT licensed. Public packages exclude home-specific audit and handover files.
