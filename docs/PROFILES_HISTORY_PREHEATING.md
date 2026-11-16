# Calendar profiles, history and measured preheating

Test release **0.4.0.dev1**, paired with card **0.4.0-dev.1**.
Not a stable release and not installed by preparing this candidate. Existing
weekly schedules, boost, occupancy and holiday settings remain authoritative.
All new profile and preheating controls start disabled/unmapped. History starts
collecting when this version is actually loaded, not when its files are downloaded.

## Where to find them

| Feature | Full scheduler | Mini thermostat |
|---|---|---|
| Day profiles | Schedules → Calendar & workdays | No extra form |
| Applied profile | Dashboard, beside today's schedule | Existing target remains the only temperature control |
| Temperature and decision history | History → selected zone, 24 hours / 7 days | Open full scheduler from its title |
| Learning and preheating | Settings → Smart preheating; dashboard status | No additional sliders or target inputs |

## Day profiles

Create a named profile from a selected day of the zone's **saved** weekly schedule.
Edit the copy using ordinary time, target and label fields. Each profile is a
complete day starting at midnight; up to 96 ordered periods and 12 profiles per zone.
Profiles are independent of the normal week and use the same reviewed limits/units.
Save zone features separately from saving a weekly schedule. Unsaved feature edits
are guarded when leaving the zone/view, and stale revisions are rejected.

Optional Workday mappings select a profile for workdays and non-workdays. Choose
an existing Home Assistant Workday binary sensor: on means workday, off means
non-workday. Its configured holidays are respected. The integration reads
`workday.check_date` for the next six dates rather than guessing tomorrow from
today's sensor state. Leave either profile empty to retain that day's normal week.

Optional calendar rules select a calendar, a case-insensitive title substring
(empty matches any event), and a profile. A matching event applies the profile
to the **entire local calendar day**, including events which only cover part of
that day. End times are exclusive; an all-day event ending Tuesday does not
override Tuesday. Up to 12 ordered rules, first matching calendar rule wins,
then Workday, then the ordinary weekly schedule. Specific rules belong first.

Calendar queries cover the next seven local dates, refresh at most every 15
minutes in the background without holding up heating deadlines, and invalidate on mapped state or HA timezone changes. A failed read is
retried after five minutes. During a bounded refresh the last good result is retained
to avoid transient thermostat changes; a failed refresh discards it. Missing/unavailable sources are shown as warnings
and fall back to the remaining rules/ordinary week. Preheating pauses while any
profile source is uncertain. Calendar availability can therefore affect which
profile applies: choose a reasonable weekly fallback.

Manual holds, Off, boost, holiday and occupancy keep their existing priority.
An occupied minimum may still raise a day-off profile while its configured window
is active. A next-period manual hold ends at the selected profile's next boundary.

## History

Local bounded history stores up to seven days of five-minute measurements
(2016 points per zone) and the latest 500 meaningful owner/target/reason changes.
The chart shows measured room temperature and the thermostat's reported target;
it is not a forecast or a fuel/energy meter. It never fills in HA downtime and
separates missing/long-gap measurements. Decision rows explain final resolved
owners, including manual changes, boost, occupancy, holiday, profile and preheat.
Calendar event titles/descriptions/locations are not retained in history or sent
to the card; only configured profile names and rule outcomes are exposed.

History may be disabled and cleared per zone. Clearing retains the learned model;
the current decision can reappear immediately. Disabling retention clears stored
chart/decision history. If preheating remains enabled, its learner keeps one latest
measurement for comparison. If both are disabled, no new samples are collected.
Data stays in HA's local storage and follows its normal backup/removal handling.

## Measured preheating

Enable separately per zone. The learner uses the mapped climate's
`current_temperature` and **reported `hvac_action: heating`**. It never assumes a
high target proves the boiler is heating. Unsupported/missing action reporting
means it stays in Learning. No extra temperature sensor is silently substituted.

At five-minute sample points, uninterrupted heating intervals of 5–15 minutes
with a positive, plausible measured rise contribute to a smoothed warm-up rate.
Cooling/stationary intervals, large jumps, missing readings, interruptions and
restart gaps do not train it. The first early start needs at least 12 accepted
intervals covering at least 60 minutes. This can take several heating cycles.
Old learning becomes unusable after 14 days without another accepted interval.

For the next warmer schedule/profile period, lead time is the measured temperature
shortfall divided by learned warm-up rate, plus the chosen margin. A configurable
5–180 minute maximum bounds every early start (default 90, margin 5). The target
is the forthcoming period's reviewed target, never an extra higher temperature.
Once started, it retains that target until the period boundary so reaching the
temperature early does not cause setback/heat cycling. Original absolute intent
survives reload/restart if still applicable; it is rechecked against current rules
and the current schedule/profile before being used.

Preheating only supplements the schedule owner. It pauses for manual/Off, disabled
or unavailable zones, boost/external handover, holiday, occupancy overrides/Away,
unknown occupancy on configured presence inputs, uncertain profile sources, and
missing room temperature. Observe-only mode computes proposals but sends no
climate commands. Storage failure pauses speculative early heat.

This first model estimates a warming rate. It does **not** model weather, solar
gain, cooling, boiler capacity, floor/radiator lag or guarantee arrival at an exact
minute. Reset learning after changing the heating hardware or room significantly.
Review several real heating cycles before relying on its timing.

## Compatibility and validation limits

The candidate adds a separate versioned `.insights` Store; the existing schedule
Store schema is unchanged. Removing the integration removes both Stores. Back up
before installation; rolling back to 0.3.x leaves the separate optional Store
unused and restores the original weekly-only profile behavior. Rollback is an
intentional behavior change if profiles or early starts have been enabled.

All feature mutations use the existing administrator-only, per-zone WebSocket
control endpoint. History read access follows the mapped climate permission.
The new card tolerates an older backend by explaining that an update is needed.
The older card cannot configure these features, so use the paired candidate.

Automated simulated HA and logic tests cover precedence, source failures, stale
edits, limits, retention, learning thresholds, owner guards and restart intent.
The candidate still needs live multi-cycle learning and third-party calendar/
Workday acceptance before a stable release. No production deployment is implied.


## Install this test release

Keep a current HA settings backup. In Settings → Marketplace, open each already
added repository, choose Install another version (or include pre-releases), and
select backend **0.4.0.dev1** and card **0.4.0-dev.1**. Follow the backend restart
prompt and refresh the browser/app after the card update. Stable-only users stay
on backend 0.3.1/card 0.3.0 until a stable successor is published.

Open Heating Control → Settings. **Add heating zone** opens HA's integration
page in a new tab: choose Add heating zone, select an existing supported heating
thermostat, name the zone, review its limits and complete setup. Repeat for each
thermostat. Existing zones do not need to be recreated when updating.

Settings now groups thermostat details under Zone settings, shows Occupancy and
Smart preheating as the two main optional features, and collapses Advanced
settings. Preheat measurements/margin/reset live under Advanced options. History
retention and clearing are under History → History settings. Open panels survive
incoming status updates. Administrator rights are required to add/edit zones.

Start testing with current schedules intact and preheating off. History starts
collecting automatically. Create day profiles under Schedules, then select only
calendar/Workday inputs whose meanings and fallback you have reviewed. Enable
preheating per zone when reported heating activity and learning are verified.
