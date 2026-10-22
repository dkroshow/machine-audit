# GPU view

Implementation guidance for [GPU Capacity, Usage, Contention and Memory](machine-information.md#gpu).
Use the shared [collection and preservation requirements](storage-approach.md)
and [time/coverage design](cpu-view.md#time-windows-and-coverage-working).
[Memory and GPU sources](research/memory-gpu-sources.md) holds driver-specific
fields and receipts. The derivations below are working designs, not hardware
validation claims. Keep every GPU and client identity separate.

## Source to record to answer

**Capacity.** Inventory vendor, model, capabilities and dedicated/shared memory
at discovery. Sample operating limits, clocks, temperature and power separately;
these provide context, not an exact effective-capacity score. Link their meaning
to [Power](machine-information.md#power) and [Heat](machine-information.md#heat).
Record asleep, denied and unavailable states distinctly from zero activity.

**Usage.** Store driver percentages with their original key, units, scope and
known window. Big Mac's AMD percentages are undocumented; Linux amdgpu reports
firmware load; NVIDIA's headline utilization measures time with a kernel active.
None is execution-unit occupancy. Preserve engine distinctions where exposed.
A selected-interval percentage is a labeled summary of the driver's readings,
not an additive counter.

GPU clients provide another kind of evidence: macOS `accumulatedGPUTime` and
Linux DRM fdinfo engine time are cumulative nanoseconds. Keep source snapshots;
derive validated deltas per client/engine, with identity and coverage. Time after
the last read of an exited client is missing. DRM values can briefly decrease;
retain that observation and apply a documented derivation rule rather than
silently rewriting source data.

**Contention.** No documented external GPU queue-wait measure was found among
the surveyed sources. Retain separate signals: busy activity and submitting
clients show demand; power/thermal limits show constrained capacity; evicted
VRAM shows memory movement/competition. None alone establishes delayed ready work.
Undocumented wait counters, tracing and workload-internal timings have different
limits; do not merge them into one waiting number or substitute zero when absent.

**Memory.** Preserve dedicated VRAM gauges and host-RAM GPU use separately.
GART, GTT and unified-memory usage is part of host memory, never extra capacity.
Driver memory classes need not form a documented partition. Client memory can
overlap: DRM shared-byte counters cannot deduplicate allocations globally, and
macOS graphics-footprint overlap with process footprint is unverified. No external
allocation-failure signal was found among the surveyed interfaces; readable driver
fault/reset events are separate evidence.

**Attribution.** macOS driver clients expose PID and GPU time; Ubuntu DRM fdinfo
requires process/file-descriptor discovery, while NVIDIA can supply process data
through its driver. Deduplicate repeated descriptors for a client, not distinct
clients or shared allocations. Keep GPU, engine, client and process membership so
later queries can regroup them. Report read coverage and sharing limits; device
busy percentages cannot supply a process-time remainder.

## Read side effects

On the probed Mac, reading the AMD `PerformanceStatistics` dictionary reset
several command and wait counters, even if the caller discarded those fields.
The existing five-second device read uses that dictionary. Selecting fewer fields
after the read does not avoid the side effect. Whether a separate per-key access
avoids it, and whether busy percentages are affected, remain unverified.

The [Big Mac trial](installation.md#big-mac-first-user-trial) pauses the existing
recorder before Machine-audit starts. If another reader remains, reset-on-read values
can be incomplete with an unknown measurement interval. Validate the interaction
and coordinate collection or migration before running the new collector alongside
an existing reader. This document does not authorize stopping that reader.

Values returned by an authorized read may be preserved as named raw integers,
with read times and uncertain coverage; this does not solve interference. Do not
add separate polling for them, or convert unknown units into wait seconds. The
initial value may predate collection; even later lower-bound totals require
confirmed reset semantics. Mark busy-percentage reset behavior unknown until tested.

Linux GPU reads can also have side effects: the checked amdgpu paths in v6.8 wake
a suspended GPU; the pinned newer source refuses those reads instead. Verify the
actual driver/kernel behavior and preserve refusal status.
The Linux adapter reads amdgpu by default because its sysfs reads reset no
counters, and it skips a GPU whose runtime status is suspended.

## Interval answers and validation

For each retained interval, show each GPU's identity and limits, activity by
available engine/key, memory, workload attribution, named contention signals,
events and coverage. Apply the storage contract to rates, gauges and counters;
retain driver window/weighting when summarizing rates. Client collapse loses
context and regrouping, so it is a later pruning option, not an initial default.

Validate driver fields, process access and collection cost on the initial devices.
The Ubuntu cloud instance in the [initial machines](installation.md#initial-machines)
has no GPU. The physical Ubuntu workstation's GPU and driver
remain to be discovered by its implementing agent. Apple silicon awaits later validation. GPU profiling
and kernel tracing are outside routine collection.
Naming other users' GPU workloads requires an explicit privacy/access decision;
use admitted labels only and do not infer authorization from driver visibility.
