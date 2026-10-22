# Heat view

Working design for [Heat](machine-information.md#heat), not hardware validation.
The [source notes](research/power-heat-sources.md) hold fields, units, access and
prior measurements. Shared [collection and preservation](storage-approach.md)
and [time/coverage rules](cpu-view.md#time-windows-and-coverage-working) apply.
[Power](power-view.md) owns power caps; a power cap acting is not thermal
throttling.

Temperatures and fan speeds are gauges; cooling/throttle states are observed
states; throttle counts and kernel-kept durations are counters. Temperature
alone establishes neither a cause nor a constraint.

## Source to record to answer

**Temperature.** Big Mac uses undocumented AppleSMC keys. Keep the earlier
prototype's admission rules: plausible absolute temperatures, relative and duplicate
channels excluded, known labels only. Unknown locations remain "Sensor N"
in `other`. Seven headline keys read at 5 s; the full sweep reads at 60 s.
Identity is the SMC key with admission-rule version. SMC publishes no
operating limits, so these readings have no distance-to-limit claim.
Apple silicon keys and battery temperature remain unvalidated.

Ubuntu exposes thermal zones, trip points and hwmon sensors. A label is
evidence of location; an index is not. The
[GPU read-side-effect rule](gpu-view.md#read-side-effects) covers the macOS
driver's temperature key and amdgpu reads that may wake a suspended device
or refuse access. Preserve refusals and the source's side-effect limitations.

Group summaries weight admitted sensors equally and keep membership/count
per sample: missing sensors change coverage visibly. `other` is context,
not a headline. Cross-run identity and external labels follow the
[source boundary and admission rules](architecture.md#source-boundary).

**Cooling.** macOS SMC supplies fan count, rpm and each fan's range, where
available. Keep the 5 s cadence and the prototype's measured-cost fallback to
60 s after its first twelve reads; retain the cadence in force. Quiet,
audible and loud are derived positions within that fan's range, not
universal thresholds. Store rpm and range.

Ubuntu supplies hwmon fan readings and thermal cooling devices: type,
current/maximum state, associated trip point and available kernel-kept
time/transition counts. Keep source durations as counters; durations
estimated from our observations retain gaps. Assess temperature against
published limits where available. For SMC, show temperature trend and fan
state; fan speed alone does not establish adequate cooling.

**Throttling.** Keep three groups of evidence separate:

- **Limits in force:** trip points, hwmon thresholds, frequency limits,
  boost state and zone policy. A configured limit does not prove it acted.
- **Corroborating observations:** temperature crossings with sensor and
  threshold named; frequency readings; macOS thermal pressure with raw level,
  mapping version and receipt time. Linux `scaling_cur_freq` is a requested
  or estimated frequency, depending on the driver; `cpuinfo_cur_freq` reports
  hardware frequency where supported and requires root. The Mac helper
  supplies a 30 s average. Low frequency can reflect idleness or policy;
  these observations alone do not establish reduced capacity.
- **Reported throttling or capacity reduction:** Intel thermal-throttle
  counts and durations; enabled power-limit counts; processor or other
  documented capacity-reducing cooling actuators; driver clock-event reasons;
  and macOS `CPU_Speed_Limit` below 100 with its companions, read only on
  Intel by the prototype. Keep each source's semantics: requested limit,
  applied limit or reported throttling are different evidence.

For the third group, retain whether the source reports hardware or OS
action and a thermal, power or unnamed cause. Intel thermal-throttle
counters already name a thermal cause; no extra corroboration is needed.
Cooling actuators keep their type and trip-point association; a fan state
alone shows cooling, not reduced capacity. GPU causes follow the
[driver receipts](research/memory-gpu-sources.md#capacity-1). An unnamed
reduction beside high temperature is coincident, not attributed to heat.

No surveyed source measures lost useful work. A throttled core still
accrues CPU time; show usage beside the evidence without claiming lost
throughput. Each signal keeps its observed, unavailable, denied,
not-enabled or failed status. For macOS thermal pressure, retain registration
status; the alternative `thermalState` API can report nominal when unsupported.

## Interval answers and validation

Both interfaces use shared summaries, events and coverage. Add hottest
sensor identity and peak; thresholds, observed changes and crossings; fan
rpm against its range with derived level/cadence; cooling states and
durations; and each throttling/limit signal with its source-named cause.

Validate the admitted sensors, access, read cost and driver behaviour on
the actual targets. Apple silicon and all Ubuntu paths remain unvalidated;
the source notes list the checks. The product never writes trip points,
fan settings, cooling states or frequency limits.
