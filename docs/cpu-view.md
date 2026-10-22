# CPU view

Implementation guidance for [CPU Capacity, Usage and Contention](machine-information.md#cpu).
The [storage approach](storage-approach.md) owns accepted cadences and preservation
requirements; the [product brief](product-brief.md) owns the human/agent questions.
Source definitions and sizing assumptions are in [CPU evidence](research/cpu-evidence.md).

These views distinguish accepted requirements from **working** designs that need
validation. Implementers resolve routine schema, query and presentation details,
record their choices and submit them for review. They do not reopen accepted
requirements or enable optional access without authorization.

CPU time is a cumulative counter; runnable demand is a sampled gauge (smoothed
on macOS, instantaneous on Linux); capacity is a slow-changing fact. Keep those
kinds distinct. Counter differences describe the observed window, limited by
the source's units and update timing.

## Source to record to answer

**Capacity.** macOS `sysctl hw.*` keys and Ubuntu `/sys/devices/system/cpu`
give logical CPUs usable now and installed, physical cores, and core classes
where the OS reports them. We keep those counts separately and do not compute
an "effective cores" number; SMT threads and performance or efficiency cores
are not equal. A change in usable CPUs is recorded with the time it was
observed; that does not tell us when it happened, so the window in which it
was first seen is marked uncertain rather than split. Ubuntu can limit a
workload below the machine (cgroup quota, affinity); those limits are recorded
with the workload, never mixed into machine capacity.

**Usage.** macOS `host_processor_info` and Ubuntu `/proc/stat` give cumulative
CPU time per logical CPU by state. The normalized states are user (Linux
`user` plus `nice`, guest time inside), system (Linux `system` plus `irq` and
`softirq`), idle (Linux `idle` plus `iowait`), and Linux `steal`. Linux
`guest`, `nice` and `iowait` are kept as labeled parts of those states, never
added again. Busy time is user plus system. Average CPUs used is busy time
divided by the window's awake duration; the share of capacity divides again by
the usable CPU count. These are derived at read time; stored usage is CPU time
in the source's integer units.

**Contention.** The signals differ by OS and are stored by name, never merged
into one "waiting" number: runnable demand (both), excess runnable as an
estimate derived per sample (both), pressure stall time (Ubuntu), scheduler
wait time per CPU (Ubuntu, when enabled), quota throttling (Ubuntu cgroups),
and per-process waiting (macOS as an estimate from runnable minus CPU time;
Ubuntu as a counter summed over threads). Each signal carries a status:
observed, unavailable on this OS, denied, not enabled, or read failed. Zero
excess runnable does not prove no waiting: affinity, quotas, QoS and core
classes can queue threads while other CPUs idle.

**Attribution.** macOS `proc_pid_rusage` and Ubuntu `/proc/<pid>/stat` give
per-process CPU time; Ubuntu cgroup `cpu.stat` gives per-group totals that
include members that have exited. Identity is pid plus start time plus boot;
a reused pid is a new process and its first read is a baseline. Each window
also records coverage: whole-machine busy time, time attributed to visible
processes, time charged from exited children, and the remainder, which is
shown rather than hidden. Keep per-process readings and group membership; cgroup
totals use a non-overlapping level and are not added to their member processes.
Contributors and affected workloads are ranked separately, because the largest user is not necessarily the one that waited.

**What any retained interval can answer.** The capacity in force and whether
it changed; CPU time by state, average CPUs used and share of capacity; each
contention signal with its kind, range, sample count and status; workloads by
CPU time and by waiting with the grouping method and permission level that
produced them; coverage; and the gaps, sleep, resets and uncertain spans
inside the interval.

## Time, windows and coverage (working)

Every read records three times: wall clock, a clock that stops during sleep
(`mach_absolute_time`, `CLOCK_MONOTONIC`), and one that does not
(`mach_continuous_time`, `CLOCK_BOOTTIME`). The difference between the last
two is sleep inside the window; awake duration is the stopping clock's
advance. Usage shares use awake duration as the denominator and the window is
marked when it contains sleep. This is a design choice to validate, not a
measured property of either OS.

A stored window spans the two reads that bound it. A requested interval can
only be answered from the windows that cover it; how the interface handles a
request that cuts a window (snap, pro-rate with a flag, or show the covered
span) is an implementation choice that must be visible to the reader. Adjacent
windows of counter differences add to the covered span's total, subject to the unit quantization of each
read; summaries of gauges and of peaks do not add and carry their method and
sample count. The peak of per-window averages is not the peak of samples and
is named as such.

A counter that goes backwards (process identity change, 32-bit tick wrap on
macOS, Linux `iowait`) invalidates only that field for that window; unrelated
fields keep their deltas. Each window carries boot identity, gap and
clock-jump flags, and the collector version that produced it.

## Storage and derivation

Follow the [preservation contract](storage-approach.md#preserve-readings-derive-summaries):
source integer snapshots and gauge samples first, summaries alongside them.
Retain per-CPU and per-process detail at the accepted cadences. Lossless no-change
encoding is an implementation option only if it reconstructs the readings and
coverage; sparse CPU-effect rows are a derived view, not a replacement for baselines.

Compute excess runnable for each sample before averaging; it cannot be recovered
from average runnable demand. Keep observed sample maxima distinct from maxima
of window averages. The [evidence note](research/cpu-evidence.md#raw-snapshots-versus-validated-deltas)
explains what can be reconstructed and what was never observed.

Later pruning would have specific losses: dropping per-CPU readings hides a pinned
core; group-only rows lose members and regrouping; top-N loses small consumers and
victims; expiring detailed samples loses timing and burst shape. These are not
initial storage defaults.

## Access and validation

Use Linux scheduler statistics where already available; offering to enable them
is a separate, disclosed choice. Never enable them silently. Without the Mac root
helper, own processes are measured and other CPU use remains a labeled remainder.
Thread-level Linux waiting needs extra reads; its coverage and cost require validation.

The implementation must prevent overlap between live-child CPU and parent-reported
exited-child CPU. Raw counters allow later correction of that derivation; they do
not justify presenting an unverified attribution total as exact.
