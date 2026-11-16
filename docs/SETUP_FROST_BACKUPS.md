# Guided setup, frost protection and backups — 0.5.1

Install integration **0.5.1** and card **0.5.1** together. Existing zones keep their schedules, control settings and learned warm-up model. Frost starts disabled on existing zones.

## Guided zone setup

In the card's Settings choose **Add heating zone**, then in Home Assistant choose **Add heating zone > Guided setup**. The four steps are thermostat/name, reviewed limits and initial target, optional features, and a review summary. No zone is created before the final submit. Back controls let you revise choices.

The starting target fills all seven days. Configure normal periods in the visual planner afterwards. Optional person occupancy creates a Mon–Fri 08:30–16:30 occupied minimum; change its inputs, times and days in the card. External boost adapters and other presence sensors remain available in card Settings. Advanced zone setup remains available for experienced users.

**Guided zones start disabled**, including when the scheduler already controls other zones. Review the schedule and features, then use **Settings > Zone settings > Manage thermostat and limits > Zone settings** to enable zone scheduling. Global observe-only mode remains a separate guard. Existing zones are never disabled by opening the wizard.

## Optional frost protection

Open **Settings > Frost protection** for a zone, enable it, review the target and save settings. In 0.5.1, frost has an independent lower bound taken from the available thermostat’s supported range. Normal schedule/manual minimums stay unchanged. For example, normal targets can remain 16–30°C while frost uses 12°C, provided the thermostat supports 12°C. Frost still respects the reviewed upper bound, actual hardware range and increment. Version 0.5.0 shares the normal minimum; update both packages to 0.5.1 to use the independent lower range.

Off is respected by default. **Allow temporary frost heating while thermostat is Off** is a separate opt-in. When room temperature drops below the frost minimum, the integration persists its original Off intent and a recovery threshold before issuing heating commands. It uses a temporary heat target above the recovery threshold (minimum + recovery margin, rounded up with one additional thermostat increment). This gives the thermostat room to warm past its own hysteresis. That temporary target must also fit the hardware range and reviewed maximum. Once the room reaches minimum + margin, the thermostat returns to Off. Changing the settings to disable this feature during its own cycle also restores Off when the device is available.

Higher targets are retained. Device/zone availability, capability checks, external boost ownership and unresolved handovers prevent frost writes. Built-in boost keeps its ownership. Global observe-only mode proposes the frost action without commanding or claiming an Off cycle. Persistence failure prevents new untracked frost heat. Restarts recover the saved cycle; no command is sent while the thermostat is unavailable. An explicit different manual thermostat target releases the frost cycle's ownership.

Unknown/nonfinite/missing room readings do not count as cold. The configurable report-age limit checks the climate entity's last update; it cannot establish independent freshness of the device's internal room sensor if an adapter republishes cached values. A lost/stale reading during an owned Off cycle causes restoration to Off when possible. Hardware protection remains necessary: HA cannot protect a home during power, network, boiler or device failure. No artificial readings are written to live sensors for testing.

## Export, import and copying

Open **Settings > Schedule backup & copying**.

- **Export saved week** downloads a versioned JSON schedule file. It excludes entity IDs and unrelated zone settings.
- **Export zone settings** also includes occupancy/boost policy entity references, named profiles, Calendar/Workday mappings, history preferences, preheating and frost settings. Keep this file private if it identifies household people or devices.
- **Import week** accepts either file type. Choose a file or paste its JSON, then **Validate & preview**. The seven-day preview must fit the destination's unit, limits and increments. Automatic Celsius/Fahrenheit conversion is deliberately unsupported. **Load into draft** changes no stored data; review and **Save week** to apply.
- A zone-settings file additionally offers **Restore zone settings**. Disable the destination zone first, inspect every entity mapping in the JSON, and tick the review acknowledgement. Backend validation checks all revisions, referenced entities and exclusive boost mappings. The zone stays disabled. Review the result before enabling it.
- **Copy from another zone** uses a compatible unit and the destination's reviewed limits. It loads an independent weekly draft, which must also be reviewed and saved.

Thermostat identity/mapping, reviewed limits, zone-enabled state and global control mode are never imported. Existing active holds/holiday state are not imported from the file, nor are history or learned models. Finish boost/frost handovers before restoring settings. History/learning stay local to the destination. This is configuration transfer, not a substitute for a full Home Assistant backup.

Settings restoration writes the optional-feature store and main schedule/policy store with compensating rollback on a reported save error. The destination must remain disabled throughout. A process/power interruption between files can leave a partial configuration; keep the zone disabled and retry the reviewed backup before enabling it. No automatic enabling follows an import.

## Preheating feedback

Settings shows learning progress toward at least **12 accepted warming observations and 60 minutes of observed heating**, the measured rate, estimated upcoming warm-up duration and planned start when available. Progress is a minimum data threshold, not an accuracy score. Stale learning still expires after 14 days without useful readings.

For a completed preheat, a usable reading within ten minutes of its scheduled boundary records the target and measured room temperature. The last result indicates whether the room was within 0.3 displayed temperature units of target. This is a near-boundary measurement, not continuous proof of exact on-time arrival. Interrupted or unobserved cycles may have no result. Keep checking actual performance across real heating cycles and changing weather.

## If settings will not save

A rejected save keeps your draft and previous saved settings. Version 0.5.1 keeps the reason beside **Save settings**, including through live status updates. Correct the error and retry, or choose **Reload saved** to discard the draft. A successful save clears the unsaved indicator. On 0.5.0, a frost target below the normal minimum is rejected and status updates can hide the error; update both packages to 0.5.1.
