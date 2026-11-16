# Smart Heating Scheduler

A Home Assistant custom integration for weekly heating schedules, timed boost, occupancy overrides and holidays, with a separate companion Lovelace card. Configure your own thermostats and occupancy inputs through the UI.

**Stable release 0.3.0:** [integration](https://github.com/OneScuzzie2k/smart-heating-scheduler/releases/tag/0.3.0) and [companion card](https://github.com/OneScuzzie2k/smart-heating-scheduler-card/releases/tag/0.3.0). This promotes the tested development builds with additional restart regression coverage and installation guidance. See the [acceptance results and limits](docs/RELEASE_ACCEPTANCE.md).

**[Installation and setup guide](docs/INSTALLATION.md)** - install both packages, add zones, configure the card and move from observe-only to heating control.

Requires **Home Assistant Core 2026.11.1 or newer** and an existing available, single-target `climate` entity supporting heating. On this supported HA version, use **Settings > Marketplace**. The repositories use the HACS-compatible format; older HA/HACS installations are outside the current supported baseline. The integration is shown in HA as **Smart Heating Scheduler (Preview)**.

No separate add-on, third-party scheduler card, Node.js or user-installed Python package is needed to run it. Your thermostat/device integration remains required. Existing boost scripts/helpers are optional: choose built-in boost if you do not use them.

New installations start in **observe only**. Heating control is a separate administrator action after reviewing every enabled zone.

## Screenshots

The companion card on desktop and phone. These screenshots use sample data from the working 0.3.0 card, disconnected from Home Assistant.

**Desktop weekly planner**

<img src="docs/screenshots/weekly-planner-desktop.jpg" alt="Dark desktop weekly heating planner with seven-day temperature timelines and a period editor" width="960">

| Mobile schedule editor | Mini thermostat and boost cards |
| --- | --- |
| <img src="docs/screenshots/schedule-mobile.jpg" alt="Light mobile schedule editor with zone and weekday selection" width="300"> | <img src="docs/screenshots/mini-cards-mobile.jpg" alt="Two compact thermostat cards with interactive target dials, boost duration sliders and boost buttons" width="300"> |

[View the full dashboard screenshot](docs/screenshots/dashboard-desktop.jpg). Both full and mini layouts are included in the [companion card](https://github.com/OneScuzzie2k/smart-heating-scheduler-card).

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

For backend development, use an isolated HA instance with simulated entities. API tests use real HA 2026.11.1 objects and simulated states. A supervised two-zone installation has also exercised schedule transitions, manual targets, occupancy activation, natural external boost expiry, holidays and Core restart recovery. Active external boosts recovered and expired at their original deadlines with timer restoration enabled. This does not establish compatibility with every thermostat; see [acceptance scope](docs/RELEASE_ACCEPTANCE.md).

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest
python -m unittest tests.ha.test_runtime -v
```

The HA test command requires Python >=3.14.2 with homeassistant==2026.11.1; standalone logic tests also run on Python 3.13. Restricted Windows environments may need permission for asyncio's internal loopback socket. See VALIDATION.txt for the actual checks and outstanding work.

## Install through Marketplace

1. Open **Settings > Marketplace > Add from link** and add this repository as an **Integration**:
   `https://github.com/OneScuzzie2k/smart-heating-scheduler`.
2. Select stable version **0.3.0** and install. Follow any restart prompt. Including pre-releases is not required.
3. Add the [companion card repository](https://github.com/OneScuzzie2k/smart-heating-scheduler-card) the same way as a **Dashboard** item; select **0.3.0** and reload your browser after installation.
4. Open **Settings > Devices & services > Add integration**, search for **Smart Heating Scheduler (Preview)** and complete setup. Open its integration entry and choose **Add heating zone** for each thermostat.
5. Add **Smart Heating Scheduler** from your dashboard's card picker, select the scheduler and build your weekly schedules while still in observe-only mode.

**[Continue with the full setup and handover instructions](docs/INSTALLATION.md)**, including manual installation, the mini card, boost adapters, occupancy, updates and rollback. Installing the files alone does not enable heating control.

![Scheduler icon](custom_components/smart_heating_scheduler/brand/icon.png)

## Distribution status

Published public GitHub releases, available as custom Marketplace repositories. Catalog inclusion is pending. Full bootstrap/config-flow coverage, more heating hardware, inheritance/bulk configuration, import/export, accessibility and translations remain follow-up work. The historical Preview integration/entry names are retained in this release; the card reports actual control mode. MIT licensed. Public packages exclude home-specific audit and handover files.
