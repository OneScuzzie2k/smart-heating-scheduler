# Installation and setup

This guide covers stable integration **0.3.0** with companion card **0.3.0**. Start with observe-only comparison before a supervised heating-control test. See [release acceptance](RELEASE_ACCEPTANCE.md) for the tested setup and limits.

## Requirements

- Home Assistant Core **2026.11.1 or newer**; administrator access for installation and rule changes. Earlier HA versions are not currently supported.
- Each zone needs an existing, available `climate` entity with **heat** mode and one target temperature. Dual-setpoint heat/cool control and cooling-only entities are outside this version's zone setup.
- Keep the integration that connects your thermostat to HA installed and working. Device-specific receiver/hold handling may still be necessary.
- For occupancy, a person, device tracker or binary sensor whose occupied/unoccupied states you can verify.
- A GitHub connection in Marketplace for custom repositories. Complete the connection flow there if prompted.

The backend and card are two separate repositories. No additional scheduler integration, card-mod, Mushroom card, separate add-on, npm install or user-managed Python dependency is required. Built-in boost works without your own timer/helper/scripts. Existing external boost needs the adapter described below.

## 1. Install both repositories

On the supported HA version, use the built-in **Settings > Marketplace**. Older HACS menu instructions are not the installation route for this HA baseline.

Choose **Add from link**, enter each GitHub repository URL and confirm the repository details. Install both items:

| Package | GitHub repository | Type | Version |
| --- | --- | --- | --- |
| Backend | https://github.com/OneScuzzie2k/smart-heating-scheduler | Integration | `0.3.0` |
| Companion card | https://github.com/OneScuzzie2k/smart-heating-scheduler-card | Dashboard | `0.3.0` |

Select the stable versions above; **Include pre-releases** is not required. If necessary, use **Install another version** to choose the tag. Prefer tagged versions for reproducible installations. A short commit hash in the installed-version display means a default-branch build; record it when reporting an issue. Enable Include pre-releases only if deliberately testing a development release.

Follow any backend restart prompt. Marketplace can load a newly installed integration with a config flow immediately; an update may need a restart. Reload every browser/Companion App frontend after installing the card. Marketplace normally registers its JavaScript resource automatically; YAML-managed resources need the entry shown by the install dialog.

These projects are custom repositories and are not yet catalog-listed. See the official [Marketplace overview](https://contributors.home-assistant.io/marketplace/how-it-works/), [pre-release/version guide](https://contributors.home-assistant.io/marketplace/guides/releases/) and [dashboard resource guide](https://contributors.home-assistant.io/marketplace/guides/dashboard/) for the platform's installation behaviour.

## 2. Create the scheduler and zones

1. Open **Settings > Devices & services > Add integration**.
2. Search for **Smart Heating Scheduler (Preview)**. Complete the initial observe-only setup. The entry is currently named **Heating Scheduler Preview**; this name does not determine whether heating control is enabled.
3. Open that integration entry and choose **Add heating zone**. Only one scheduler instance is supported; add each thermostat as a zone inside it.
4. Set a zone name, select its thermostat, and review its minimum/maximum target and initial all-day target. Values use the thermostat's Celsius or Fahrenheit unit and must fit its range/increment.
5. Choose how manual target changes should resume: next period, a duration, or explicit resume. If using duration, set the hold minutes.
6. Leave **Existing boost timer entity ID** empty for built-in boost. If retaining external boost scripts, select their existing zone timer and configure the adapter later.
7. Repeat for the other zones. Each thermostat can belong to only one zone.

The initial target fills **all seven days**. An existing schedule is not imported automatically. Add the desired periods in the card before enabling heating control. New zones appear after the integration reload triggered by zone creation.

## 3. Add the full scheduler card

1. Open a dashboard you can edit: **Edit dashboard > Add card**.
2. Search for **Smart Heating Scheduler** and select it. In the visual editor choose **Full scheduler**, then your scheduler in the **Scheduler** field.
3. Save. Confirm the card is connected and shows **Observe only**.
4. Under **Schedules**, choose each zone and edit its seven-day week. Every day starts at `00:00`; each period runs to the next start, with the last ending at midnight. Save each zone's changes.
5. Compare its proposals with your intended targets while your existing controller remains in charge.

The visual editor fills in `entry_id`; you do not need to find it manually. For a YAML dashboard, configure a card visually first and use **Show code editor** to copy its generated configuration. `entry_id` is a scheduler config-entry ID, not a climate entity ID. Omit `demo` for a real connection.

## 4. Add a mini thermostat/boost card

Add another **Smart Heating Scheduler** card, choose **Mini thermostat & boost**, select the scheduler and the exact **Heating zone**, then save. Add one card per zone. Set **Full scheduler dashboard path** to your own full-dashboard path if you want the title to navigate there.

The round dial is the only target-temperature control. The lower slider selects boost duration. A small **Resume schedule** control appears during a manual hold. The three-dot button opens the native thermostat. Dial changes are disabled in observe-only mode and when device/ownership guards prevent them.

Example structure only; replace both IDs using the visual editor:

```yaml
type: custom:smart-heating-scheduler-card
layout: mini
entry_id: YOUR_SCHEDULER_CONFIG_ENTRY_ID
zone_id: YOUR_ZONE_SUBENTRY_ID
title: Heating
dashboard_path: /your-heating-dashboard/0
theme: auto
```

Both layouts use the same JavaScript resource. The mini card does not create another controller.

## 5. Configure optional zone features

### Boost

In the full card's **Settings**, select a zone and review **Boost adapter**:

- **Built-in timed boost**: configure target, default duration and maximum duration; remove any external timer mapping. No external boost helpers or scripts are needed.
- **Existing scripts and timer**: supply that zone's start/cancel script entity IDs, ownership timer and fixed declared target. Map a duration helper if the scripts use one. The adapter does not inject a target into arbitrary scripts; helper bounds/increments constrain duration.
- **No boost**: disables boost for that zone.

External scripts/timers/helpers must be separate for each zone. Preserve their supporting expiry/restoration automations. For automatic external expiry handover, configure a completion event emitted **after restoration finishes**, with an exact data-field/value filter. Without that signal, timer expiry can pause scheduler writes pending an administrator's restoration confirmation. See the [external adapter contract](CONTROL_POLICIES.md#boost-ownership-and-commands) before using an existing boost system.

For a countdown that survives an HA restart, enable **Restore state and time when Home Assistant starts** on each external timer helper. Verify this in the helper's settings; the scheduler does not change it for you. Test an active boost through a restart before relying on this adapter unattended. Restoration preserves the original deadline, not a new full duration. If a timer expires while HA is offline, do not assume its normal expiry automation and completion event ran: inspect receiver restoration and use the card's explicit confirmation when needed. A restored idle timer alone is not proof of completed restoration.

### Occupancy override

In **Settings > Occupancy override**, enable the feature for the selected zone, choose the minimum target, people/sensors, occupied/unoccupied state meanings, weekdays and start/end hours, then save. The minimum applies only while occupied inside that window; a higher scheduled target is retained. Leave the optional unoccupied target empty to follow the weekly schedule while away. Configure delays and unknown-state fallback to suit the setup. Downstairs or other zones are unaffected unless configured independently.

Verify that selected people actually report Home/Away correctly, with recent phone/tracker updates. Select a person or their trackers, rather than counting both. Unknown-state fallback does not detect a tracker stuck on an old but valid `home`/`not_home` reading. Manual holds, Off, boost and holiday have higher priority than the occupied minimum.

### Holiday

Use **Automation** to set start/return and target for each participating zone. Times use HA's configured timezone. Configure each zone separately. Finish existing external boosts before holiday setup; generic external boost does not support holiday exceptions. Test cancellation/return before relying on it unattended.

## 6. Hand over heating control

Do this during a supervised test with a current HA backup and an understood rollback path.

1. Review every enabled zone's mapping, limits, full week and optional rules. Finish active boosts.
2. Disable the old automations/schedulers that write targets or HVAC modes to those same thermostats. Keep required device integration, receiver/hold and external boost restoration dependencies. Disabling competing writers does not mean deleting every heating automation.
3. In full-card **Settings > Heating control mode**, enter `ENABLE HEATING CONTROL`, then choose **Enable heating control**.
4. Confirm **Heating control enabled**, matching proposed/device targets and the expected control source. Check a manual target/resume and boost start/cancel on each zone, then observe a real schedule boundary and any occupancy rule.

Control mode is **global** to this scheduler and persists across restarts. It applies to every enabled zone, including enabled zones added later. In observe-only mode, schedule/smart-rule changes affect proposals without sending heating commands. Explicitly Off thermostats are respected; use the native thermostat or the full card's explicit resume action when you intend to turn heating on.

Keep the old definitions available for rollback during early testing. Automatic boost expiry, holiday return, restart recovery and actual device behaviour need acceptance on your heating setup; the development tests do not replace those checks.

## Updates and rollback

Back up before updating. Read both repositories' release notes and use a compatible version pair. Update the backend through Marketplace and follow its restart prompt; update the card and reload all frontends. Installing a card update alone does not require an HA restart.

To return control to the old system: stop active boosts, select **Switch to observe only** in the full card, confirm new writes have stopped, then re-enable the previous schedule controller. Observe-only does not restore old targets automatically; check the actual thermostat settings. Built-in boosts must be stopped before switching mode. If external ownership/handover is uncertain, check receiver restoration first. Avoid having both schedule writers active together.

Uninstalling files or stopping HA does not itself reset a thermostat's last command. Keep required device/receiver and external boost dependencies until they have a tested replacement.

## Manual installation

For users who prefer copying files:

1. Download the backend release's **Source code (zip)**. Copy its `custom_components/smart_heating_scheduler` directory into your HA configuration directory, giving `<config>/custom_components/smart_heating_scheduler/manifest.json`. Avoid an extra nested repository folder. Restart HA, then follow zone setup above.
2. Download **smart-heating-scheduler-card.js** from the card release's Assets. Copy it to `<config>/www/smart-heating-scheduler-card.js`; create `www` if needed.
3. Add `/local/smart-heating-scheduler-card.js` as a dashboard resource of type **JavaScript module**. Use the dashboard's resource editor, or your existing YAML resource list with `type: module`.
4. Reload the browser, then add the card. Register the resource once. If moving to Marketplace later, remove the old manual resource so two versions are not loaded.

The built release bundle is ready to use. End users do not need to build the card or install this repository's development dependencies.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No release offered | Core is at least 2026.11.1; both repositories added; GitHub connected; refresh repository information; choose stable 0.3.0. Include pre-releases is only needed for development versions. |
| Integration not in Add integration | Backend files installed in the correct folder; follow restart prompt; search for Smart Heating Scheduler (Preview); inspect HA logs. |
| Card type not found | Card repository installed; resource registered as a module once; reload the browser/app frontend. |
| No scheduler or zones | Finish backend setup, choose Scheduler in the visual editor, add a zone, and confirm the user's entity permissions. |
| Zone setup rejected | Thermostat available; supports heat and a single target; reviewed limits/increment fit the device; thermostat not already mapped. |
| Card shows Observe only | Expected initial state. Complete the reviewed handover to permit real heating commands. |
| Temperature does not change | Check Off/manual/holiday/boost priority, zone enabled state, connectivity and visible faults. Confirm external boost restoration before Retry controls. |
| Occupancy override does not activate | Check saved enable switch, day/time in HA timezone, selected input's actual state, state mappings and delays; confirm phone location is updating. |
| Wrong mini-card zone | Select the exact Heating zone in the editor; a zone subentry ID is different from a climate entity ID. |

For an issue, include HA version, integration/card versions (or installed commit hashes), thermostat integration/model, expected/actual behaviour and a redacted relevant log excerpt. Remove tokens, location coordinates and private home details.
