# Power view

Working design for [Power](machine-information.md#power), not hardware validation.
The [source notes](research/power-heat-sources.md) hold fields, units, access and
prior measurements. Shared [collection and preservation](storage-approach.md)
and [time/coverage rules](cpu-view.md#time-windows-and-coverage-working) apply.
Power caps and evidence of them acting belong here; thermal causes belong in
the [Heat view](heat-view.md).

Each value keeps its kind and source-named **domain**: what it measures.
Gauges, counters, driver averages, states and events retain their distinct
meanings. Domains from different sources overlap and are never added.

## Source to record to answer

**Consumption.** Big Mac's routine source is four undocumented SMC power
keys at 5 s: reported system power, CPU input rail and two GPU rails. Keep
the source readings and labels; the rails are unmapped, not wall or
CPU-package watts. The predecessor's combined GPU-rail reading is not a
per-GPU measurement. The optional root helper adds `powermetrics` package
watts at 30 s. Its averages are estimates, potentially inaccurate and not
comparable between devices; parsing is verified only on an Intel capture.

Ubuntu offers Intel RAPL energy counters per package and subzone, root-only
and recorded as denied without a disclosed access decision. It also offers
hwmon power readings; an AMD APU's reported power includes its CPU. The
[GPU read-side-effect rule](gpu-view.md#read-side-effects) applies to amdgpu
hwmon and the macOS GPU driver's power key.

No surveyed source provides a validated whole-machine or wall figure.
System-named sources, including SMC's system key and RAPL `psys`, keep their
labels and documentation status without that inference. Battery discharge
is net battery flow, not machine consumption.

**Supply.** macOS reports AC, battery or UPS and notifies on changes; Low
Power Mode is a readable flag from macOS 12. Ubuntu exposes mains/USB online
state. The surveyed notifications and files have no event timestamp:
record receipt time and observation bounds, not an invented change time.

**Battery.** Big Mac has none: `present: false`, never zero charge. Laptop
fields await validation. Preserve the published percent, charge or energy
family without mixing units, and keep documented sentinels as unavailable
or calculating. Watts derived from voltage/current and health derived from
compatible full-charge/design capacities must be labelled as derivations.

**Power limits.** Ubuntu exposes RAPL constraints with named domains,
microwatts and time windows, plus hwmon device caps; see the
[GPU receipts](research/memory-gpu-sources.md#capacity-1) for driver fields.
The surveyed macOS routine sources publish no package power cap;
`CPU_Speed_Limit` belongs in Heat. Preserve cap, domain, unit, window, access
status and observed changes. Discovery then 60 s is a working proposal,
not an accepted baseline cadence.

A configured cap does not prove enforcement or supply adequacy. Keep
reported limit events, such as enabled Intel power-limit counts or driver
clock-event reasons, separately. Show caps beside same-domain readings;
do not compute headroom from them.

**Attribution.** No surveyed routine source gives per-process energy.
`powermetrics` offers a platform-weighted process proxy outside the helper's
fixed stream; if later captured, retain its own name, never watts or joules.
Show CPU, GPU and disk attribution for the same interval as correlation.
Do not rank processes by power.

## Energy and summaries

Preserve original counter snapshots with zone identity, read clocks and
status. Derive differences under source-specific continuity and wrap rules
validated on the target. A decrease may be wrap or reinitialisation; a gap
can hide complete wraps even when the later value is higher. A configurable
average-power cap does not establish an instantaneous bound. Ambiguous
intervals stay unknown; safe intervals require validation.

A counter may span sleep: retain a valid difference with its elapsed span
and unknown distribution through sleep. Gauge integration is a separate
estimate over awake time: the working method holds each reading until the
next, breaking at gaps over two cadences, and retains sample count and
method. Driver averages remain readings with their own windows; convert
them to interval energy only with known contiguous, non-overlapping windows.
Every derived value names its method.

## Interval answers and validation

Both interfaces use shared summaries, events and coverage. Add supply
changes, same-domain caps and readings, energy with covered duration and
validity, and battery trajectory with estimated charging/discharging time.
Zone/device identity and label admission follow the
[source boundary](architecture.md#source-boundary); an index alone is weak.

Validate sources, permissions, read cost and counter continuity on the actual
targets; the source notes name the remaining gaps. All laptop and Apple
silicon power paths remain unvalidated, including GPU watts and the
IOReport candidate. The product never writes a cap, charge threshold or
power setting.
