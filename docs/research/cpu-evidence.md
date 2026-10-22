# CPU evidence: sources, fields, and storage options

Research note behind the [CPU view](../cpu-view.md). It holds the field-level
mapping, the comparison of storage options, sizing arithmetic, and source
receipts. The [storage approach](../storage-approach.md) owns accepted collection
and preservation requirements; record layouts here remain working designs.
Sources read on 2026-10-21.

Hardware checks: the `hw.*` and `kern.clockrate` values below were read on an
Intel Mac Pro (macOS 15.6.1, Darwin 24.6.0: 24 logical, 12 physical CPUs, one
performance level named "Standard", `hz` 100, `kern.sleeptime` zero because the
machine has not slept). No Apple silicon Mac and no Ubuntu machine was checked
for this note; every Linux path and every two-class macOS key is from
documentation or source only.

## Capacity

| Fact | macOS | Ubuntu | Notes |
| --- | --- | --- | --- |
| Logical CPUs usable now | `hw.logicalcpu`, `hw.activecpu` | `/sys/devices/system/cpu/online` | Denominator for share of capacity. |
| Logical CPUs installed | `hw.logicalcpu_max` | `/sys/devices/system/cpu/present` | Shows reduced capacity. |
| Physical cores | `hw.physicalcpu`, `hw.physicalcpu_max` | `cpuN/topology/core_id`, `physical_package_id`, `thread_siblings_list` | SMT threads versus cores. |
| Core classes | `hw.nperflevels`, `hw.perflevelN.name`, `.logicalcpu`, `.physicalcpu` | `cpuN/cpu_capacity` where exposed; other markers to verify on the target | Absence means no class information, not one class. |
| Tick rate | `kern.clockrate` `hz` | `sysconf(_SC_CLK_TCK)` | Converts ticks to seconds. |
| Process time unit | `mach_timebase_info` numer/denom | nanoseconds (`schedstat`), ticks (`/proc/<pid>/stat`) | Mach units differ between Intel and Apple silicon. |
| Workload limits | none comparable (QoS and `darwinbg` are policy) | cgroup v2 `cpu.max` ("$MAX $PERIOD", default `max 100000`), `cpuset.cpus.effective`, `/proc/<pid>/status` `Cpus_allowed_list` | Stored with the workload. |

Capacity change: the collector observes a new value at a read; the change
happened somewhere between that read and the previous one. Per-CPU counters
cannot be split at the unknown instant, so the window is marked
`capacity_uncertain` and no share is derived for it. On Linux the aggregate
`cpu` line sums `for_each_possible_cpu` while per-CPU lines list only online
CPUs (`fs/proc/stat.c`), so an offlined CPU's past ticks stay in the aggregate
and vanish from the per-CPU lines; the two must not be mixed across such a
change.

## Usage states

Raw per-CPU counters: macOS `host_processor_info(PROCESSOR_CPU_LOAD_INFO)`
returns `natural_t cpu_ticks[CPU_STATE_MAX]` per logical CPU, each a 32-bit
count of `hz` ticks derived from Mach-time totals (`processor_cpu_load_info()`;
`CPU_STATE_NICE` is set to 0). Linux `/proc/stat` prints `u64` nanosecond
totals converted with `nsec_to_clock_t` to USER_HZ ticks.

| Normalized state | macOS fields | Linux fields | Inside it |
| --- | --- | --- | --- |
| user | `CPU_STATE_USER` | `user` + `nice` | Linux `guest` is inside `user`, `guest_nice` inside `nice` (`account_guest_time`). `nice` kept as a labeled part. |
| system | `CPU_STATE_SYSTEM` | `system` + `irq` + `softirq` | Interrupt handling kept as labeled parts. |
| idle | `CPU_STATE_IDLE` | `idle` + `iowait` | `iowait` is idle time while the CPU has I/O outstanding (`account_idle_time`); the kernel documents it as unreliable and sometimes decreasing. Labeled part, never contention. |
| steal | not applicable | `steal` | Hypervisor withheld time; reduces effective capacity in a VM. Not busy, not local idle. |

Derivations at read time, all from differences of the raw integers:

```text
busy      = user + system                  (guest not added: already inside user)
accounted = user + system + idle + steal   (should approach usable_cpus × awake time)
cpus_used = busy / awake_s
share     = busy / (usable_cpus × awake_s)
```

A difference between `accounted` and `usable_cpus × awake_s` is diagnostic
context with an unknown tolerance: tick quantization, the kernel's update
timing, non-atomic reads across CPUs, sleep the awake clock did not capture,
and accounting changes can all contribute, and none is verified to a bound. It
is not by itself evidence of a late read. A decreasing `iowait` invalidates the idle sub-part for that window only; user,
system and steal deltas remain valid, and the combined idle state is marked
uncertain.

## Contention signals

| Signal | macOS | Ubuntu | Kind | Definition and limits |
| --- | --- | --- | --- | --- |
| Runnable demand | `processor_set_statistics(PROCESSOR_SET_LOAD_INFO)` → `load_average`, scaled by `LOAD_SCALE` (1000) | `/proc/stat` `procs_running` (`nr_running()`) | Gauge | macOS returns the global `sched_load_average`, updated as `(4 × old + now) / 5` per scheduler load interval; the source comment puts settling at about 30 to 45 ms. Linux is an instantaneous count of runnable tasks. |
| Excess runnable | derived per sample: `max(0, runnable − usable_cpus)` | same | Estimate | Computed per sample, then summarized. Zero does not prove no waiting. |
| Pressure stall time | not available | `/proc/pressure/cpu` line `some`: `avg10 avg60 avg300 total`, `total` cumulative microseconds | Counter | "share of time in which at least some tasks are stalled". System-level `full` is undefined and reported as 0 since 5.13; cgroup `cpu.pressure` `full` is defined. The `avg*` values are kernel windows, usable live only. Needs PSI enabled (`CONFIG_PSI`, possibly `psi=1`); verify on target. |
| Scheduler wait per CPU | not available | `/proc/schedstat` `cpuN` fields 7 (ran) and 8 (waited to run), nanoseconds; version 17 current | Counter | Fields only increment. Reported only when `kernel.sched_schedstats` is on; the sysctl documentation states an overhead exists but gives no figure. Check the version line at startup. |
| Quota throttling | not applicable | cgroup `cpu.stat` `nr_throttled`, `throttled_usec` (fair-class tasks only, non-hierarchical) | Counter | Waiting caused by a limit, not a full machine. |
| Per-process waiting | `proc_pid_rusage` `ri_runnable_time` − (`ri_user_time` + `ri_system_time`) | sum over `/proc/<pid>/task/<tid>/schedstat` field 2 (`sched_info.run_delay`) | macOS: estimate; Linux: counter | xnu starts `runnable_timer` in `thread_unblock` and stops it in `thread_dispatch` when the thread waits, so runnable time is running plus queued; the subtraction mixes two clocks, may go negative, and is clamped. Linux `/proc/<pid>/schedstat` reports **one task** (the thread whose pid equals the tgid), not the thread group (`proc_pid_schedstat` reads the single `task_struct`; `fs/proc/base.c`). Process wait therefore needs one read per thread; threads that exit between reads are lost; cost unmeasured. |

Gauge aggregation (working): at the detailed level keep each sample as read
(integer, with read times). At rollup compute, for runnable and for each
derived signal, `n`, `sum`, `min`, `max`, `last` from the original samples, with
`method_version` and `weighting` recorded. Default weighting is one per
sample; the sample read times allow time weighting later only while the
samples exist; weighted summaries retain their weights or time integral. Observed
peak is `max` over samples; the peak of per-window
averages is a different quantity and is named as such. Example on 24 CPUs:
samples 0 and 48 average 24 runnable, derived excess 0, but per-sample excess
averages 12.

## Attribution

| Concern | macOS | Ubuntu |
| --- | --- | --- |
| Enumerate | `proc_listpids` (`PROC_UID_ONLY` in an earlier private prototype; `PROC_ALL_PIDS` exists) | `/proc` directory; `hidepid` mount option can hide other users' processes |
| Read CPU counters | `proc_pid_rusage(RUSAGE_INFO_V4)`: `ri_user_time`, `ri_system_time`, `ri_runnable_time`, `ri_child_user_time`, `ri_child_system_time`, Mach units. The prototype reports that a non-root caller can read only its own user's processes; the policy source was not verified here. | `/proc/<pid>/stat` fields 14 `utime`, 15 `stime` (thread-group totals via `thread_group_cputime_adjusted` for the tgid entry; `utime` includes guest time), 16 `cutime`, 17 `cstime` (reaped children, from `sig->cutime/cstime`), 22 `starttime` (ticks since boot), 20 `num_threads`; `/proc/<pid>/task/<tid>/stat` for one thread |
| Identity | pid + `ri_proc_start_abstime` + executable UUID | pid + `starttime` + boot id (`/proc/sys/kernel/random/boot_id`, `btime`) |
| Group | app bundle or executable; parent chain to the first real program | cgroup v2 path from `/proc/<pid>/cgroup` (`0::$PATH`); same parent-chain grouping available |
| Group counters | sum of member processes | cgroup `cpu.stat` `usage_usec`, `user_usec`, `system_usec`: all processes in the cgroup **including descendants** and exited members |
| Exited work | child time reaches the parent that reaped it | same; cgroup totals also keep it |

Double counting to avoid: cgroup totals are hierarchical, so sum only cgroups
at one non-overlapping level (or leaves) and never add a cgroup total to the
per-process totals of its own members. Child time (`ri_child_*`, `cutime`,
`cstime`) includes CPU of children that were themselves observed while alive;
charging it to the parent without subtracting what was already observed counts
it twice. The prototype resolves this with a take-back rule; the rule for
Machine-audit is unresolved. A cgroup deleted and recreated under the same path
is a new group with reset counters; `/proc/<pid>/cgroup` membership can change
between reads and a path alone is not a stable identity (the cgroup inode
number is a candidate; unverified).

## Raw snapshots versus validated deltas

Three ways to store the detailed level:

| Option | Stored per read or window | Reconstructable later | Lost |
| --- | --- | --- | --- |
| A. Raw integer snapshots | Each read's counter values in source units, gauge samples, read times, identity | Any delta between any two retained reads; re-derivation after a derivation-rule correction (for example a changed guest or iowait rule) from the fields that were stored; reset and identity errors re-judged from the stored start times and boot ids | Activity, order and peaks between reads were never observed, only totals between reads; a field parsed wrongly at capture, or never stored, cannot be repaired from these records; whatever the source never exposed |
| B. Validated integer deltas | Per window: differences the collector accepted, plus flags | Sums over covered windows; the collector's view at the time | A delta judged invalid at collection time; any re-pairing of reads; the rule that produced the delta if it was wrong |
| C. Checkpoints plus deltas | A raw snapshot every N windows, deltas between | Re-derivation at checkpoint granularity | Detail between checkpoints behind a wrong rule |

The accepted preservation requirement selects option A at the detailed level,
in source integer units. Conversion factors (tick rate, timebase) can be stored
once in the capacity row. Float seconds are a derived presentation, not the
stored truth.
Option A costs about the same per row as B: the same number of fields, and
SQLite stores integers in 1 to 8 bytes by magnitude, so large snapshot values
may take a few more bytes than small deltas. Rollups (minute, hour) are
derived sums and summaries and can be rebuilt from A while A exists.

## Sizing (arithmetic assumptions, not measurements)

Payload-only lower bounds; SQLite page, row header and index costs are
excluded and must be measured. Integers assumed at 8 bytes except macOS
per-CPU ticks at 4 bytes (32-bit source). Linux per-CPU values are printed
from 64-bit totals and are assumed at 8 bytes.

- Per host read, without per-CPU detail: identity and times (boot ref, wall,
  stopping clock, continuous clock: 4 × 8 B) + 3 summed states (3 × 8 B) +
  runnable sample (8 B) + status flags (4 B) = 68 B.
- Per-CPU detail adds the per-CPU state payload times the CPU count: macOS
  4 states × 4 B = 16 B per CPU (384 B on 24 CPUs); Linux 10 fields × 8 B =
  80 B per CPU (160 B on a 2-CPU VM, 1,920 B on 24 CPUs). Identity, times and
  contention fields do not multiply.
- At one read per second: 68 B → about 5.9 MB per day; 452 B (macOS, 24 CPUs
  with per-CPU detail) → about 39 MB per day; 1,988 B (Linux, 24 CPUs) → about
  172 MB per day. Linux pressure
  and schedstat counters add 8 B each (schedstat per CPU).
- Per process per read: identity (pid, start, boot ref: 3 × 8 B) + user,
  system, runnable or wait, child user, child system (5 × 8 B) = 64 B. Two
  hundred processes every ten seconds → about 110 MB per day; 2,000 → about
  1.1 GB per day. These illustrate uncompressed payload cost, not measured
  database growth. Preserve the accepted cadence and detail; measure actual
  growth and prove any lossless encoding before relying on it.

## Record sketch (working, illustrative names)

```text
cpu_capacity      boot_id, observed_wall, observed_continuous, logical_usable, logical_installed,
                  physical, classes[{name, logical, physical}], tick_hz, timebase, os, collector_version

cpu_read          boot_id, wall, mono_awake, continuous, status{usage, runnable, pressure, schedstat},
                  macos:  per_cpu[{cpu, user, system, nice, idle}]                     (hz ticks, as returned)
                  linux:  per_cpu[{cpu, user, nice, system, idle, iowait, irq, softirq,
                                   steal, guest, guest_nice}]                            (USER_HZ ticks, as printed)
                  runnable_scaled (macos LOAD_SCALE units) | procs_running (linux)      (gauge sample)
                  linux: pressure_some_total_us, schedstat_wait_ns[], schedstat_run_ns[]

cpu_process_read  read_ref, pid, start, boot_id, exec_id, group_key, group_method,
                  user, system, runnable_or_wait, wait_kind, child_user, child_system,
                  observation{observed, baseline_only}
                  (per-thread schedstat reads, cgroup cpu.stat / cpu.pressure / cpu.max totals:
                   record shape not yet worked; they are not represented by this row)

cpu_rollup        covers[reads], boot_id, wall_start, wall_end, awake_s, sleep_s,
                  flags{gap, clock_jump, reset_fields[], capacity_uncertain},
                  sums{user, system, idle, steal, linux parts, pressure_some_us, sched_wait_ns},
                  gauges{runnable: n, sum, min, max, last; excess: n, sum, min, max, last;
                         method_version, weighting},
                  coverage{busy, attributed, exited_charged, remainder, permission_level},
                  workloads[{group_key, nproc, cpu, child_cpu, wait, wait_kind, observation}]
```

Normalization into user, system, idle and steal happens at derivation time from
the original fields. Shares, CPUs-used, and health words are not stored.

## Source receipts

Revisions read on 2026-10-21. A source revision is evidence for that revision;
behavior on the target OS versions (the checked Mac runs Darwin 24.6.0, older
than current xnu main) must be confirmed on those machines.

- xnu main `f6217f891ac0` (tag `xnu-12377.1.9`, 2025-10-16):
  - `osfmk/kern/processor.c`: `PROCESSOR_CPU_LOAD_INFO` zeroes user, system,
    idle, calls `processor_cpu_load_info()` which accumulates
    `(uint32_t)(mach_time / hz_tick_interval)` per state, then sets
    `CPU_STATE_NICE = 0`; `PROCESSOR_SET_LOAD_INFO` sets `load_average =
    sched_load_average`, `mach_factor = sched_mach_factor`, `task_count`,
    `thread_count`.
    <https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/processor.c>
  - `osfmk/kern/sched_average.c`: `sched_load_average = ((sched_load_average
    << 2) + average_now) / 5` with `average_now = nthreads * LOAD_SCALE`;
    comment: settles in 2 to 3 load-compute intervals, about 30 to 45 ms.
    <https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/sched_average.c>
  - `osfmk/kern/sched_prim.c`: `timer_start(&thread->runnable_timer, ctime)`
    in `thread_unblock` when the thread was not `TH_RUN`; `timer_stop` in the
    waiting branch of `thread_dispatch`. `osfmk/kern/thread.c`:
    `task->total_runnable_time += timer_grab(&thread->runnable_timer)` on
    thread termination.
    <https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/sched_prim.c>
  - `bsd/sys/resource.h`: `rusage_info_v4` fields.
    <https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/resource.h>
- macOS SDK `mach/mach_time.h` (Command Line Tools on the checked Mac):
  `mach_continuous_time` is "like mach_absolute_time, but advances during
  sleep".
- Apple, "Determining system capabilities" (`hw.*` keys):
  <https://developer.apple.com/documentation/kernel/1387446-sysctlbyname/determining_system_capabilities>
  (page did not render for the fetch tool; keys confirmed by reading them on
  the checked Mac).
- Linux master `a804c5742bff` (2026-10-21):
  - `kernel/sched/cputime.c` `account_guest_time`: `p->utime += cputime` and
    either `CPUTIME_NICE` + `CPUTIME_GUEST_NICE` or `CPUTIME_USER` +
    `CPUTIME_GUEST`; `account_idle_time`: `CPUTIME_IOWAIT` when
    `cpu_has_iowait()` else `CPUTIME_IDLE`; `account_steal_time`:
    `CPUTIME_STEAL`.
    <https://github.com/torvalds/linux/blob/master/kernel/sched/cputime.c>
  - `fs/proc/stat.c`: `u64` totals, `nsec_to_clock_t`, aggregate line over
    `for_each_possible_cpu`, per-CPU lines over `for_each_online_cpu`,
    `procs_running` from `nr_running()`, `btime` from `getboottime64()`.
    <https://github.com/torvalds/linux/blob/master/fs/proc/stat.c>
  - `fs/proc/base.c` `proc_pid_schedstat`: prints `task->se.sum_exec_runtime`,
    `task->sched_info.run_delay`, `task->sched_info.pcount` for the one
    `task_struct`; same function for the tgid and tid entries; under
    `CONFIG_SCHED_INFO`; prints `0 0 0` when `sched_info_on()` is false.
    <https://github.com/torvalds/linux/blob/master/fs/proc/base.c>
  - `fs/proc/array.c` `do_task_stat`: `thread_group_cputime_adjusted` for the
    tgid entry, `task_cputime_adjusted` for a tid; `cutime`/`cstime` from
    `sig->cutime`/`sig->cstime`.
    <https://github.com/torvalds/linux/blob/master/fs/proc/array.c>
- Linux documentation (docs.kernel.org "latest", read 2026-10-21):
  - `/proc/stat` fields and the three iowait caveats; `procs_running` "total
    number of threads that are running or ready to run".
    <https://www.kernel.org/doc/html/latest/filesystems/proc.html>
  - PSI file format, `total` in microseconds, CPU `full` undefined at system
    level, cgroup `cpu.pressure`.
    <https://docs.kernel.org/accounting/psi.html>
  - schedstat version 17, per-CPU fields 7 and 8 in nanoseconds,
    `/proc/<pid>/schedstat` three fields.
    <https://docs.kernel.org/scheduler/sched-stats.html>
  - `kernel.sched_schedstats`: "Enables/disables scheduler statistics";
    overhead stated without a figure.
    <https://docs.kernel.org/admin-guide/sysctl/kernel.html>
  - cgroup v2 `cpu.stat`, `cpu.max`, `cpu.pressure`, `cpuset.cpus.effective`,
    `/proc/<pid>/cgroup` format.
    <https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html>
  - `clock_gettime(2)`: `CLOCK_MONOTONIC` "does not count time that the
    system is suspended"; `CLOCK_BOOTTIME` "also includes any time that the
    system is suspended".
    <https://man7.org/linux/man-pages/man2/clock_gettime.2.html>
  - Ubuntu noble `proc_pid_stat(5)`: field numbers, clock-tick units, `utime`
    includes guest time.
    <https://manpages.ubuntu.com/manpages/noble/man5/proc_pid_stat.5.html>
- An earlier private prototype, read 2026-10-21 (not linked): per-CPU ticks
  summed at read; busy share returns nil on CPU-count change or reset; queue
  estimate; process identity = pid + start + executable UUID; wait deltas
  clamped at 0 and called an approximation; baseline-only observation for
  processes seen first; minute columns `cpu_avg_pm`, `cpu_peak_pm`,
  `cpu_runnable_avg_c`, `cpu_queue_avg_c`, `cpu_queue_peak_c`, hourly
  `seg_metric` avg/med/p95/max).
