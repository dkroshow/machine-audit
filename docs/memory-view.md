# Memory view

Implementation guidance for [Memory Capacity, Usage, Movement and Contention](machine-information.md#memory).
Use the shared [collection and preservation requirements](storage-approach.md)
and [time/coverage design](cpu-view.md#time-windows-and-coverage-working).
[Memory and GPU sources](research/memory-gpu-sources.md) holds field definitions,
units, permissions and receipts. Record shapes and derivations below are working
designs for implementation review.

## Source to record to answer

**Capacity.** Keep physical memory, swap capacity and workload limits separately.
macOS `hw.memsize` reports installed RAM; Ubuntu `MemTotal` reports usable RAM.
Linux cgroups expose hard limits, reclaim thresholds, protections and swap limits.
The surveyed macOS sources offer only remaining process-limit headroom and some
termination reasons. Record changes when observed; do not invent their exact time.
GPU use of host RAM (unified memory, GART or GTT) belongs to this same capacity.

**Usage.** Preserve the OS's page classes: macOS VM statistics, Ubuntu `meminfo`,
and cgroup `memory.current`/`memory.stat`. Derive readable views with a named method,
without equating the two OSes' categories. On macOS, API free pages already include
speculative pages; `vm_stat` subtracts them for its displayed free count. Wired
pages include throttled pages. Linux `MemAvailable` is a kernel estimate. Full
memory, or a reclaimable-to-unreclaimable ratio, does not establish contention.

**Movement.** Keep compression, decompression, swap-in/out, page-ins, reclaim,
faults and refaults as separate counters in source units. Some count pages, some
KiB, some events; do not multiply every field by page size. Retain macOS
swap-housekeeping counters alongside total swap-ins, then derive a fault-driven
estimate. Neither that estimate nor refault volume measures waiting or proves harm.

**Contention.** Ubuntu PSI totals measure memory stalls at machine and cgroup
scope; store integer microsecond snapshots and derive differences. Its fixed-window
averages cannot be re-cut to an arbitrary interval. macOS pressure level is an OS
classification, not stall time; keep polled states and notifications with observation
times. Time in each state is estimated, with gaps and possible missed transients.
No macOS stall-time source was found in the surveyed interfaces.

Keep limit-event counters distinct from individual reports. Linux `memory.events`
counts reclaim/throttling, limit encounters, OOM conditions and kills; an increment
is not necessarily a refused allocation and supplies no victim or exact event time.
Jetsam reports and readable Linux OOM logs can identify victims and reasons,
including workload limits versus machine-wide shortages.
Capture bounded, privacy-filtered records promptly; if the reason cannot be read
within the byte budget, record that explicitly. Raw reports are not retained.

**Attribution.** The process pass combines usage gauges with cumulative effects:

| Platform | Usage gauges | Effects and context |
| --- | --- | --- |
| macOS | `proc_pid_rusage` footprint; `TASK_VM_INFO` compressed and graphics footprint, limit headroom | Page-ins, swap-ins, decompressions, lifetime high-water mark |
| Ubuntu | Process `status` RSS/swap; cgroup memory charges | cgroup movement, PSI and limit-event counters |

Retain both kinds at their scheduled cadence. Nonzero effect rows can accelerate
queries, but are derived from retained observations. A lossless encoding must
reconstruct gauges, source counter baselines, identity/status changes and read
coverage; missing rows alone never establish zero activity.

Footprint, RSS, PSS and cgroup charges have different accounting scopes. Linux RSS
counts shared pages in each mapping process; macOS footprint sharing is not yet
verified. Do not sum overlapping or unlike scopes or subtract them from host use to invent an
unattributed remainder. Record coverage instead. Rank memory users separately from
workloads that needed data back, stalled, or were terminated.

## Interval answers and validation

For any retained interval, show capacity and observed limits, usage by class,
movement by kind, each contention signal, affected workloads and coverage.
Preserve events separately from counts. Summary means must retain their weighting;
maxima preserve observed magnitude, not the full sequence leading to a failure.
The storage contract governs retention, including the default to keep process detail.

Measure expanded process reads and stored size on each initial device. The observed
24 ms `TASK_VM_INFO` pass on Big Mac is not a total overhead measurement. Ubuntu
availability, permissions and kernel fields still require target validation.

Detailed shared-memory accounting (`smaps_rollup`), delay accounting, PSI triggers
and privileged log access are optional paths with a disclosed purpose. Whether to
offer them is a product/access decision; field layouts, bounded parsing and query
methods are implementation work to review, not product decisions that must be
settled first.
