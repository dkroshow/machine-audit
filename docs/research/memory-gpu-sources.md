# Memory and GPU sources

Status: research input for the compute evidence synthesis, 2026-10-21. It maps
where memory and GPU evidence comes from on macOS and Ubuntu, and how each kind
of source could be preserved. It does not choose a schema, cadence, or
retention period. Source recommendations require target validation; they are not
settled product choices. It develops the [Memory view](../memory-view.md) and
[GPU view](../gpu-view.md), and follows the record kinds and retention options
of the [CPU view](../cpu-view.md) and [CPU evidence](cpu-evidence.md).

## Evidence labels and targets

Each claim carries one label:

- **Doc:** primary documentation or source code, pinned to a version.
- **Code:** the source of an earlier private prototype.
- **Probe:** a check on Big Mac on 2026-10-21.
- **History:** an aggregate, read-only query of that prototype's store on Big Mac.
- **Est:** an inference or estimate, with its reasoning.
- **Unknown:** not established.

"Not found" below means not found in the sources surveyed for this note. It is
not a claim that no such interface exists.

**macOS target (probed):** Big Mac is a Mac Pro 2019 (`MacPro7,1`): Intel Xeon
W-3235, 384 GiB RAM, 4 KiB pages, macOS 15.6.1 (24G90, kernel
`xnu-11417.140.69`). It has one GPU: an AMD Radeon Pro W5500X (Navi 14,
8 GB VRAM, PCIe). No Apple silicon Mac was probed.

**Ubuntu sources (documentation only):** no Ubuntu source was probed for this
note. The Ubuntu cloud instance and physical workstation targets are listed
in [installation](../installation.md#initial-machines); inventory is not source
validation. The source claims below come from documentation and source code.
Linux sources are pinned to v6.8 (Ubuntu 24.04's release kernel; 24.04.4 offers
a 6.17 kernel), v7.0 (Ubuntu 26.04 LTS), and `master` at `a804c57`. Facts cited
from `master` match v7.0 unless noted. Ubuntu settings that matter here, such as
PSI, cgroup v2, kernel-log access, `/proc` visibility, and systemd-oomd, are
unverified on any target.

**Probe interference.** Most probes were reads with no side effects. The GPU
statistics probes were not: on Big Mac's AMD driver, reading
`PerformanceStatistics` resets several counters (see
[GPU sources](#big-macs-gpu-as-observed)). The probes ran while the prototype's
recorder was reading the same statistics, so for a few seconds they took part
of the values that recorder would otherwise have seen. Read-only API access did
not make those reads non-mutating. They were not repeated.

## Key findings

1. **macOS pressure level does not measure delay.** XNU sets it from how much
   uncompressed memory remains relative to compressor thresholds (Doc). On Big
   Mac, all 35 minutes with at least 100 MiB/min of swap-in read "normal", and
   no minute reached "warning" in 14 days (History). Keep it as an observed OS
   state, not as a contention measure.
2. **Linux exposes memory stall time; no macOS source for it was found.** PSI
   reports time tasks were stalled on memory, for the machine and each cgroup;
   optional delay accounting reports per-task delays (Doc). On macOS,
   `task_info(TASK_VM_INFO)` reports each process's swap-ins (bytes) and
   decompressions, without root for the user's own processes (Doc). One pass
   over 1,974 own processes on Big Mac took 24 ms, and 820 had swap-ins
   (Probe). These counts show which processes needed data back, not how long
   they waited. The prototype does not use this source.
3. **macOS system swap-ins include housekeeping.** The `swapins` counter also
   counts the compressor re-reading swap files to reclaim or defragment them
   (Doc). Since boot on Big Mac, these made up 26% of swap-in pages (Probe).
   Subtracting them estimates fault-driven swap-ins; even that volume does not
   show that programs waited.
4. **Termination evidence disappears.** Big Mac recorded 35 jetsam reports since
   2026-09-19, but none remain on disk now (History, Probe). The prototype skipped
   10 of the 35 because they exceeded its 16 MiB file-size cap, so their reasons
   are lost (History, Code). Capture needs to happen soon after each report
   appears, with bounded parsing.
5. **Memory accounting scopes overlap.** Linux RSS, PSS, and cgroup charges
   follow different documented rules for shared memory; cgroups charge shared
   page cache to whichever cgroup touched it first (Doc). How macOS footprint
   treats pages shared between processes was not verified (Unknown). Sums of process memory do not form a machine total, and a machine
   total minus those sums is not meaningful "unattributed" memory.
6. **Big Mac's GPU has two memory pools.** It has its own 8 GB VRAM and a
   128 GiB GART window into system RAM (the GART is the GPU's mapping of system
   memory); 253 MB of system RAM was in GPU use (Probe). VRAM is separate
   capacity. GART use is system RAM and must not be added on top of host memory.
7. **The macOS AMD driver resets many statistics when they are read.** Command
   counts and wait-time counters dropped to zero on a read milliseconds after
   another read; memory gauges did not (Probe). Any other reader takes part of
   those values, so the interval a value covers is unknown. Idle readings could
   not show whether the busy percentages also reset.
8. **"GPU busy" means different things.** Big Mac's driver reports two
   undocumented percentages. amdgpu's `gpu_busy_percent` is a firmware load
   estimate. NVIDIA's `utilization.gpu` is the share of a short sample period in
   which any kernel ran. DRM fdinfo reports busy nanoseconds per engine per
   client (Doc). None of these measures how fully the execution units are
   occupied.
9. **No documented GPU queue-wait measure was found.** External evidence is
   indirect: busy time with several active clients (demand, not proof that
   demand exceeded capacity), undocumented driver wait counters with unknown
   units, NVIDIA clock-event reasons, and amdgpu evictions. Measuring queue wait
   directly needs instrumentation inside the workload, or tracing.
10. **Linux per-process GPU data needs a scan.** DRM fdinfo needs a walk of every
    process's open files and ptrace-level read access (Doc); its cost was not
    measured. On macOS, driver clients list GPU time for every PID; the prototype
    gives about 12 ms per read as an earlier, unversioned cost figure (Code).
11. **Some amdgpu reads can wake the GPU on older kernels.** In v6.8,
    `gpu_busy_percent`, `mem_busy_percent`, and hwmon sensors read through a
    helper that calls `pm_runtime_get_sync`, which resumes a runtime-suspended
    device. In `master`, the equivalent helper uses `pm_runtime_get_if_active`
    and refuses instead (Doc). Other read paths were not checked. A refused read
    must be recorded as such, not as zero.

## Memory sources

### Capacity

| Evidence | macOS | Ubuntu | Type and units | Notes |
| --- | --- | --- | --- | --- |
| Physical memory | `sysctl hw.memsize`: installed bytes (Probe) | `MemTotal` in `/proc/meminfo`: usable RAM, "physical RAM minus a few reserved bits and the kernel binary code", kB (Doc) | Slow fact | Not the same quantity; label each. |
| Swap capacity | `vm.swapusage` → `xsw_usage` total, avail, used (bytes) (Doc: SDK `sys/sysctl.h`) | `SwapTotal`, `SwapFree`; `/proc/swaps` per device (Doc) | Gauge | Big Mac showed 15360 MB total (Probe). macOS appears to add swap files as needed (Est), so the total can change. |
| Workload limits | The limits themselves were not found. `task_info(TASK_VM_INFO)` `limit_bytes_remaining` gives bytes left before a process's limit (Doc: XNU `task.c:6125`); non-zero for 52 of 1,974 own processes on Big Mac (Probe). Otherwise a limit shows only in a `per-process-limit` kill. `os_proc_available_memory` is unavailable on macOS (Doc: SDK `os/proc.h`). | cgroup v2 `memory.max`, `memory.high`, `memory.low`, `memory.min`, `memory.swap.max`, `memory.zswap.max` (bytes or `max`) (Doc) | Slow fact or gauge | Linux limits can be read in advance; macOS exposes only headroom for some processes. |
| Compressed-memory capacity | `vm.compressor_pool_size` sysctl, undocumented (Probe) | zswap `max_pool_percent`; zram device size (Doc) | Slow fact | Optional context. |

### Usage and reclaimability

macOS and Linux classify pages differently. Do not equate macOS "inactive"
with Linux `Inactive(file)`: macOS active and inactive mix anonymous and file
pages, while Linux keeps separate lists for each.

| Source | Fields | Scope | Meaning and caveats |
| --- | --- | --- | --- |
| macOS `host_statistics64(HOST_VM_INFO64)` (Doc: SDK `mach/vm_statistics.h`) | Gauges, in pages: `free_count`, `active_count`, `inactive_count`, `speculative_count`, `wire_count`, `throttled_count`, `purgeable_count`, `internal_page_count` (anonymous), `external_page_count` (file-backed), `compressor_page_count` (pages holding compressed data), `total_uncompressed_pages_in_compressor` | Machine | One call; multiply page counts by `vm_page_size`. `free_count` already includes `speculative_count` (Doc: XNU `host.c:799`), and on macOS `wire_count` already includes throttled pages (Doc: `host.c`). Do not add either part again. |
| `vm_stat` (command-line presentation) | "Pages free" = `free_count − speculative_count`; "Pages wired down" = `wire_count`; "Pages throttled" printed separately (Doc: `vm_stat.c` at `system_cmds-1026.140.2`, the installed version) | Machine | A transformation of the API fields, not the same fields. From `vm_stat` on Big Mac: free + speculative + active + inactive + wired + compressor-occupied came within 5,488 pages (21 MiB) of installed memory, and anonymous + file-backed equalled active + inactive + speculative (Probe). In API terms: `free_count + active + inactive + wire_count + compressor_page_count`. Throttled was 0, so the probe did not test double counting of throttled pages. |
| macOS `kern.memorystatus_level` | Percent | Machine | Pages the kernel counts as available, as a share of total pages (Doc: XNU `vm_pageout.c:4703`). The kernel's own definition, not Linux `MemAvailable`. Big Mac read 91 (Probe). |
| Linux `/proc/meminfo` (Doc) | `MemFree`, `MemAvailable`, `Cached`, `Buffers`, `Active/Inactive(anon/file)`, `Unevictable`, `Mlocked`, `Shmem`, `AnonPages`, `Mapped`, `SReclaimable`, `SUnreclaim`, `KReclaimable`, `SwapCached`, `Zswap`, `Zswapped`, `Committed_AS`, `CommitLimit` (kB) | Machine | `MemAvailable` is a kernel estimate of memory for new programs without swapping. `Shmem` (tmpfs and shared memory) is counted in `Cached` but cannot be reclaimed without swap. |
| Linux cgroup v2 `memory.current`, `memory.stat` (Doc) | Bytes: `anon`, `file`, `kernel`, `shmem`, `file_mapped`, `file_dirty`, `slab_reclaimable`, `slab_unreclaimable`, `zswap`, `zswapped`, and more | Workload (cgroup) | Look up keys by name; the kernel may add entries anywhere. Totals are hierarchical. |

On macOS, speculative and purgeable pages are the easiest to reclaim, then
clean file-backed pages (Est). Inactive pages are reclaim candidates, not free
memory. Activity Monitor's "App Memory" and "Cached Files" are derived figures
that Apple does not document. The prototype reports memory in use as internal +
wired + compressor pages (Code).

### Movement

All are cumulative counters since boot unless noted. Multiply by page size only
where the unit is pages.

| Source | Field | Unit | Notes |
| --- | --- | --- | --- |
| macOS `host_statistics64` (Doc: SDK `mach/vm_statistics.h`) | `compressions`, `decompressions`, `swapins`, `swapouts`, `purges`, `reactivations`, `zero_fill_count` | Pages (header comments) | `swapins` and `swapouts` also count swap-file reclaim (a read, then a rewrite) and defragmentation reads (Doc: XNU `vm_compressor_backing_store.c:757`, `:2296`, `:2326`). The prototype stores swap-in, swap-out, decompression, and page-in per minute, but not compressions (Code). |
| | `pageins` | Pages | Counted per page in the paging path, excluding ordinary file I/O (`UPL_FILE_IO`) (Doc: XNU `vm_pageout.c:6405`); other increment sites not checked. |
| | `pageouts` | Unknown | The header says "# of pageouts"; the unit was not verified. |
| | `faults`, `cow_faults` | Events | Not pages; do not multiply. |
| macOS compressor sysctls (undocumented) | `vm.compressor_swapper_reclaim_swapins`, `vm.compressor_swapper_defrag_swapins` | Pages (Doc: XNU sysctl targets) | Since boot on Big Mac: 2,094,609 reclaim and 34,080 defragmentation pages out of 8,134,796 swap-in pages (Probe). Names may change between releases. |
| | `vm.compressor_input_bytes`, `vm.compressor_compressed_bytes`, `vm.compressor_bytes_used` | Bytes (by name; Est) | A compression ratio is possible; meanings not verified. |
| macOS `vm.swapusage` used | — | Bytes; gauge | Past swap-out not yet returned, not current activity. Big Mac ranged from about 3.7 to 21.5 GiB while memory in use peaked near 327 GiB of 384 GiB (History). |
| Linux `/proc/vmstat` (Doc: `mm/vmstat.c`, v6.8; meanings from cgroup `memory.stat` docs, v7.0) | `pswpin`, `pswpout` | Pages | Swap traffic. |
| | `pgpgin`, `pgpgout` | KiB | Block I/O converted from 512-byte sectors (Doc: `vmstat.c:1830`). Not swap-specific. |
| | `pgscan_kswapd`, `pgscan_direct`, `pgsteal_kswapd`, `pgsteal_direct` | Pages | Reclaim scanning and reclaim; `_direct` means allocating tasks reclaimed themselves. |
| | `workingset_refault_anon`, `workingset_refault_file`, `workingset_activate_*`, `workingset_restore_*` | Pages | Refaults are previously evicted pages faulted back. Many refaults suggest the working set does not fit; alone they do not show harm (Est). |
| | `zswpin`, `zswpout`, `zswpwb` | Pages | zswap traffic. |
| | `pgmajfault`, `allocstall_*`, `compact_stall`, `oom_kill` | Events | Not pages. |
| Linux cgroup `memory.stat` (Doc) | `pgscan`, `pgsteal`, `pgmajfault`, `workingset_refault_*`, `zswpin`, `zswpout`, `pswpin`, `pswpout` (v7.0) | As above | Per workload, hierarchical. |

macOS `pageins` mixes first reads of mapped files with re-reads of evicted
pages; no public macOS refault counter was found (Est from the field
definitions).

### Contention: delays, pressure, refusals, and terminations

**Delays**

| Source | Fields and units | Scope | Access | Caveats |
| --- | --- | --- | --- | --- |
| Linux PSI `/proc/pressure/memory` (Doc: `psi.rst`) | `some` and `full` lines: `avg10`, `avg60`, `avg300` (%); `total` (µs, cumulative) | Machine | Unprivileged | Counts time in reclaim, compaction, waits for recently evicted pages, swap-in of those pages, and `memory.high` throttling (Doc: kernel callers of `psi_memstall_enter`). Triggers can report bursts inside a sample interval; unprivileged trigger windows must be multiples of 2 s. Requires PSI enabled (Unknown on the target). |
| Linux cgroup `memory.pressure` (Doc) | Same format | Workload | Per cgroup | Shows which workloads were delayed, including small ones. |
| Linux delay accounting (taskstats) (Doc: `delay-accounting.rst`) | Per task or process: swap-in, reclaim, thrashing, and compaction delay totals | Process | Off by default; enabled with the `delayacct` boot option or `kernel.task_delayacct`, counting only tasks started after enabling. Netlink queries need `CAP_NET_ADMIN` (Doc: `kernel/taskstats.c`, `GENL_ADMIN_PERM`). | Enabling it is a system change. |
| macOS | No memory stall-time source found | Machine and process | — | Volume proxies only: fault-driven swap-ins (estimated), decompressions, and per-process swap-ins and decompressions. They show that data had to come back, not how long anything waited. The prototype grades raw swap-in volume with thresholds it has not validated (Code). |

**Pressure state**

| Source | Values | Meaning |
| --- | --- | --- |
| macOS `kern.memorystatus_vm_pressure_level` (polled) and `DISPATCH_SOURCE_TYPE_MEMORYPRESSURE` (notifications) (Doc: SDK `dispatch/source.h`) | 1 normal, 2 warning, 4 critical | With the compressor active, warning starts when active + inactive + free + speculative pages fall below a fraction of that total plus compressor pages; critical adds compressor space shortage (Doc: XNU `vm_pageout.c:10011–10070`, `vm_page.h:1519`). The sysctl is hidden from `sysctl -a` but readable by name (Doc: `kern_memorystatus_notify.c:1817`). Polling gives the level at each read; notification timing and coalescing were not verified. Durations are estimates bounded by observations, and short transients can be missed. |
| Linux | No OS-wide level | systemd-oomd, where configured, uses cgroup v2 and PSI and can act on swap use for units with `ManagedOOMSwap=` or `ManagedOOMMemoryPressure=`; `oomctl` lists monitored cgroups (Doc: systemd v255). Whether it runs on the target, and where its kills are recorded, is unverified. |

**Limits and refusals**

| Source | Fields | Meaning |
| --- | --- | --- |
| Linux cgroup `memory.events` and `memory.events.local` (Doc) | `low`, `high`, `max`, `oom`, `oom_kill`, `oom_group_kill`; `sock_throttled` in v7.0 | Cumulative counters, not event records. A delta counts events between two reads, with no victim names and no exact times. `high`: throttled into direct reclaim. `max`: about to exceed the limit. `oom`: an allocation was about to fail. The plain file is hierarchical; `.local` covers one cgroup. A change raises a file-modified event (Doc), which can give an approximate time. |
| Linux `memory.swap.events` (Doc) | `high`, `max`, `fail` | Cumulative counters of swap limit and swap allocation failures. |
| User-space allocation failure | Not found on either OS | With Linux overcommit, `malloc` can succeed and failure can appear later as an OOM kill (Est from `sysctl/vm.rst` overcommit). |

**Terminations**

| Source | Fields | Notes |
| --- | --- | --- |
| macOS JetsamEvent `.ips` reports in `/Library/Logs/DiagnosticReports` and `~/Library/Logs/DiagnosticReports` (Doc: Apple, "Identifying high-memory use with jetsam event reports"; Code) | Header `largestProcess`, `pageSize`; a `processes` array with `rpages`, `lifetimeMax`, `states`, `coalition`, `name`; `reason` only on the killed process | An event record with a snapshot of processes at the kill. XNU cause names: `per-process-limit`, `vm-pageshortage`, `vnode-limit`, `highwater`, `idle-exit`, `long-idle-exit`, `proc-thrashing`, `fc-thrashing`, `vm-compressor-thrashing`, `vm-compressor-space-shortage`, `low-swap`, `sustained-memory-pressure`, `vm-pageout-starvation`, `zone-map-exhaustion`, `disk-space-shortage`, `jettisoned` (Doc: `kern_memorystatus.c:105–124`). The prototype names three and maps the rest to "other" (Code). Big Mac: 25 `per-process-limit`, 10 unread because of the size cap (History). |
| Linux `/proc/vmstat` `oom_kill`; cgroup `memory.events` `oom_kill` (Doc) | Cumulative counters | Machine-wide and per-cgroup counts of kernel OOM kills, without victims. Kills by a user-space killer such as systemd-oomd are probably not counted here (Est). |
| Linux kernel log or journal (Est) | "Out of memory: Killed process …" lines | Can name the victim. Read access on the target is unverified. |

A per-process or per-workload limit differs from a whole-machine shortage. The
jetsam reason on macOS and cgroup events versus a global OOM on Linux expose
that difference.

### Attribution

| Source | Fields | Accounting scope | Access and cost |
| --- | --- | --- | --- |
| macOS `proc_pid_rusage(RUSAGE_INFO_V4)` (Doc: SDK `sys/resource.h`; Code) | `ri_phys_footprint`, `ri_resident_size`, `ri_lifetime_max_phys_footprint`, `ri_interval_max_phys_footprint`, `ri_pageins` | Footprint = anonymous + compressed anonymous + IOKit mappings + non-volatile purgeable + page tables (Doc: XNU `task.c:1185`). How it treats pages mapped by several processes was not verified; the prototype's docs say shared memory appears in each footprint (Code docs, unverified). | Own user without root; others need root. `ri_pageins` counts the process's page-ins; the prototype does not keep it. |
| macOS `task_info(TASK_VM_INFO)` via `task_name_for_pid` (Doc: SDK `mach/task_info.h`; XNU `task.c:6125–6143`) | `phys_footprint`, `compressed`, `decompressions` (count), `ledger_swapins` (bytes), `ledger_tag_graphics_footprint` (bytes), `limit_bytes_remaining` (bytes) | Per-process ledgers. `decompressions` counts faults on compressed pages taken by the process's own threads (Doc: `vm_fault.c:209`) and saturates at 2^31 − 1. `ledger_swapins` counts pages the process decompressed from compressed data that had been read back from swap (Doc: `vm_compressor.c:5411`). | Needs the same user and real user, or root (Doc: XNU `kern_proc.c:5660`); root-owned processes were refused without root (Probe). One observed pass: 1,974 processes in 24 ms on Big Mac (Probe); total collector overhead not measured. |
| Linux `/proc/<pid>/status` (Doc) | `VmRSS` = `RssAnon` + `RssFile` + `RssShmem`; `VmSwap`; `VmHWM` | RSS counts a shared page in every process that maps it (Doc: PSS definition). | Visible to others unless `/proc` uses `hidepid`. Cost not measured. |
| Linux `/proc/<pid>/smaps_rollup` (Doc) | `Pss`, `Pss_Anon`, `Pss_File`, `Pss_Shmem`, `Pss_Dirty`, `Swap`, `SwapPss` | PSS divides each mapped page among the processes mapping it. It does not cover unmapped page cache or kernel memory (Est from the definition). | Needs ptrace read access (Doc: `task_mmu.c`, `PTRACE_MODE_READ`). It walks every mapping's page tables under the target's memory-map lock, so cost grows with mapped memory and it can briefly delay the target's mapping changes (Doc, Est). |
| Linux cgroups (Doc) | `memory.current`, `memory.stat`, `memory.events`, `memory.pressure` per systemd unit or slice | Charges, not usage: shared page cache goes to the first cgroup that touched it, and moving a process does not move its charges. Hierarchical, so sum only one non-overlapping level. | One file per cgroup. |

Who uses memory and who is affected can differ. On Linux, per-cgroup PSI and
optional delay accounting show who was delayed. On macOS, per-process swap-ins
and decompressions, and jetsam victims, show who needed data back or was
killed. A top-N list by memory size alone drops small affected processes.

Instead of a machine-minus-processes residual, record coverage: which processes
or cgroups were read, at what permission level, which were baseline-only, and
which reads failed. Scopes overlap and exclude different things (kernel memory,
page cache, shared mappings), so no additive machine total is implied.

## GPU sources

### Big Mac's GPU, as observed

Values from the `IOAccelerator` → `PerformanceStatistics` dictionary. These
reads reset several counters for every reader (see
[Probe interference](#evidence-labels-and-targets)).

| Statistic | Probe value or behavior | Interpretation |
| --- | --- | --- |
| `Device Utilization %`, `GPU Activity(%)` | 0–2 at idle | Undocumented percentages; window unknown. The prototype prefers `GPU Activity(%)`, citing saturation (Code). |
| `HWChannel <GFX, SDMA0/1, SDMA*_PAGE, HIQ, KIQ, VCN0Dec, VCN0Enc, VCN0EncLLQ> \| Commands Submitted/Completed` | Reset to 0 on each read | Command counts per hardware queue: graphics, copy (SDMA), video decode and encode. Counts, not time. |
| `hardwareWaitTime`, `hardwareSubmitWaitTime`, `clientGLWaitTime`, `finish*WaitTime`, `vramEvictionWaitTime`, `freeToAllocGPUAddressWaitTime` | Reset on read | Undocumented; units Unknown. Keep as named raw integers; do not convert to seconds. |
| `inUseVidMemoryBytes`, `vramFreeBytes`, `orphanedReusableVidMemoryBytes` | 1.68 GB, 5.64 GB, 1.25 GB; stable across reads | In one snapshot these sum to 8 GiB minus 16 MiB (Probe). This suggests freed-but-cached allocations are neither in use nor free; not a documented identity. |
| `inUseSysMemoryBytes` = `gartUsedBytes`; `gartSizeBytes` | 253 MB of 128 GiB | System RAM mapped for the GPU (Est from names). Part of host memory. |
| `texturePageOutBytes`, `stdTexturePageInBytes`, `ioSurfacePageInBytes` | Reset on read | Possibly movement between VRAM and system memory (Est). |
| `Temperature(C)`, `Total Power(W)`, `Core Clock(MHz)`, `Memory Clock(MHz)`, `Fan Speed(%)` | 46 °C, 17 W, 5 MHz, 1750 MHz | Operating state for Capacity, Power, and Heat; units from key names. |
| `recoveryCount` | 0 | Possibly a count of GPU resets (Est). |
| Client entries with `accumulatedGPUTime` (ns), `lastSubmittedTime` | 78 clients across 39 PIDs | Per-context GPU time. Context class hints at the work type: Metal command queues, 2D, video, OpenGL (Probe). |

### Capacity

| Source | Fields | Notes |
| --- | --- | --- |
| macOS IORegistry and `system_profiler SPDisplaysDataType` | Model, vendor and device IDs, VRAM total, GART size, Metal support; power, temperature, and clocks from the statistics above | On Apple silicon the GPU uses unified system memory, so GPU capacity is not a separate pool. |
| Linux amdgpu sysfs (Doc: `amdgpu_vram_mgr.c`, `amdgpu_gtt_mgr.c`, `amdgpu_pm.c`) | `mem_info_vram_total`, `mem_info_vram_used`, `mem_info_vis_vram_*`, `mem_info_gtt_total`, `mem_info_gtt_used` (bytes); hwmon `power1_average`, `power1_input`, `power1_cap` (µW), `temp*_input`, `temp*_crit`, `temp*_emergency` (m°C); `gpu_metrics` (binary, versioned) | On APUs, `power1_*` includes the CPU (Doc). |
| NVIDIA NVML (Doc: NVML API R550) | Memory total and used; power limit; temperature; clock-event reasons such as `SwPowerCap`, `SwThermalSlowdown`, `HwSlowdown`, `HwPowerBrakeSlowdown` | Requires the proprietary driver and its library. Clock-event reasons show reduced capacity, not queueing. |
| Intel i915 and xe | Engines and hwmon power on discrete parts | Unknown detail; documentation only. |

### Usage

| Source | Scope | Meaning |
| --- | --- | --- |
| macOS AMD busy percentages (Probe, Code) | Device | Undocumented; possibly computed since the previous read by any reader (Unknown). Keep the driver's key name with each value. |
| macOS Apple silicon `Device Utilization %`, `Renderer Utilization %`, `Tiler Utilization %` | Device | Key names from the prototype's code and third-party tools; unverified here (Unknown). |
| macOS client `accumulatedGPUTime` (Code) | Context → PID | Cumulative ns. Time accrued after the last read before a client closes is lost. The prototype charges only the user's own programs, although the driver lists every PID. |
| Linux DRM fdinfo `drm-engine-<name>` (ns), `drm-engine-capacity-<name>`, `drm-cycles-<name>`, `drm-total-cycles-<name>`, `drm-maxfreq-<name>` (Doc: `drm-usage-stats.rst`) | Client → process | Busy time per engine. amdgpu engines: `gfx`, `compute`, `dma`, `dec`, `enc`, `jpeg`, `vpe`; i915: `render`, `copy`, `video`, `video-enhance` (Doc). Busy time alone ignores clock speed; cycles with maximum frequency or total cycles give a frequency-aware figure. Values may briefly go backwards; the spec says to keep the larger value. `drm-client-id` (with `drm-pdev`) removes duplicate descriptors of one client. |
| Linux amdgpu `gpu_busy_percent`, `mem_busy_percent` (Doc: `amdgpu_pm.c`) | Device | A firmware load estimate from "aggregate activity level in the IP cores", not a time share. |
| NVML `utilization.gpu`, `utilization.memory` (Doc) | Device | Share of a sample period (1/6 s to 1 s, by product) in which at least one kernel ran, or device memory was read or written. 100% can coexist with few active compute units. |
| NVML per-process utilization samples (Doc: `nvmlDeviceGetProcessUtilization`) | Process | Timestamped samples since a caller-supplied timestamp. |
| Profiling counters: DCGM SM activity, SM occupancy, tensor activity; AMD raw counters (Big Mac's registry names `AMDRawCounterPlugin`) | Device | Measure execution-unit activity and occupancy, but need profiling tools. DCGM profiling supports data-center GPUs from Volta onward on Linux (Doc). |

### Contention

No documented GPU queue-wait measure for an external monitor was found.
Available evidence:

- Busy time while several clients submit work: evidence of demand, not proof
  that demand exceeded capacity (Est).
- macOS AMD wait counters above: undocumented, unknown units, reset on read.
- NVIDIA clock-event reasons and amdgpu power caps: capacity limits, not
  queueing.
- amdgpu per-client `amd-evicted-vram`: memory competition (Doc:
  `amdgpu_fdinfo.c`).
- Linux GPU scheduler tracepoints and debugfs fence information: root and
  tracing.
- Timing inside the workload, such as Metal command-buffer GPU times or CUDA
  events: requires instrumenting the application.

### Memory

| Source | Fields | Notes |
| --- | --- | --- |
| macOS AMD statistics (Probe) | VRAM in use and free; system memory in GPU use; page-in and page-out bytes | See the Big Mac table. |
| macOS per process | `ledger_tag_graphics_footprint` from `TASK_VM_INFO` (Doc) | 45 own processes on Big Mac had non-zero values, 239 MiB in total (Probe). Which allocations are tagged, and whether the value is inside `phys_footprint`, is Unknown. |
| macOS Apple silicon | `In use system memory`, `Alloc system memory` (Code) | Part of unified memory; unverified here. |
| Linux DRM fdinfo (Doc) | `drm-total-<region>`, `drm-shared-<region>`, `drm-resident-<region>`, `drm-purgeable-<region>`, `drm-active-<region>`; amdgpu regions `vram`, `gtt`, `cpu`; amdgpu `amd-evicted-vram`, `amd-requested-vram`, `amd-requested-gtt` | Per client. A buffer shared between clients appears in each one's totals. `drm-shared-*` says how much of a client's total is shared, not with whom, so exact global deduplication is not possible. Keep per-client values with their shared parts as overlap. |
| NVML (Doc) | Device memory; per-process `usedGpuMemory` (`nvmlDeviceGetComputeRunningProcesses_v3`) | PID namespaces in containers may hide the owner (Unknown). |

GTT, GART, and unified memory are system RAM. Show them inside host memory,
tagged as GPU use, not as extra capacity. Discrete VRAM is separate capacity.
No documented, externally observable GPU allocation-failure signal was found.

### Attribution

- **macOS:** per-PID GPU time from driver clients, for all users; about 12 ms
  per read by the prototype's earlier, unversioned cost figure (Code docs).
  WindowServer and other system processes are visible by PID.
- **Linux:** for each process, find open DRM device files and read their
  fdinfo. Work grows with processes × open files; cost not measured. Needs the
  same user or `CAP_SYS_PTRACE` (Doc: `fs/proc/fd.c`,
  `PTRACE_MODE_READ_FSCREDS`).
- **NVIDIA:** per-process data comes from the driver, without a file scan.

## Applying these findings

Accepted collection cadences and preservation requirements live in the
[storage approach](../storage-approach.md). The [memory](../memory-view.md) and
[GPU](../gpu-view.md) views explain how these sources become interval answers.
Keep this note as source evidence, not a second set of collection defaults.

## Checks for the Ubuntu target

During implementation on each Ubuntu target, verify:

- Kernel version, PSI availability, cgroup v2, and how slices map to workloads.
- Read access to the kernel log, the journal, and other users' `/proc` entries
  for an unprivileged collector.
- GPU vendor and driver; which sysfs, hwmon, and fdinfo keys exist; whether
  reads are refused or wake a suspended GPU on that kernel.
- Swap, zswap, or zram configuration, and whether systemd-oomd runs and where
  its kills are recorded.
- Collection cost of the process and fdinfo scans on real process counts.

## Sources

Linux kernel, `github.com/torvalds/linux`, at
[v6.8](https://github.com/torvalds/linux/tree/v6.8),
[v7.0](https://github.com/torvalds/linux/tree/v7.0), and
[`a804c57`](https://github.com/torvalds/linux/tree/a804c5742bff224d4cb4d25dd383554d598be32b):

- [`Documentation/filesystems/proc.rst`](https://github.com/torvalds/linux/blob/v6.8/Documentation/filesystems/proc.rst): meminfo (including `MemTotal`), status, smaps_rollup, PSS
- [`Documentation/accounting/psi.rst`](https://github.com/torvalds/linux/blob/v6.8/Documentation/accounting/psi.rst)
- [`Documentation/admin-guide/cgroup-v2.rst`](https://github.com/torvalds/linux/blob/v7.0/Documentation/admin-guide/cgroup-v2.rst): memory interface files, `memory.stat` units, Memory Ownership
- [`Documentation/accounting/delay-accounting.rst`](https://github.com/torvalds/linux/blob/a804c5742bff224d4cb4d25dd383554d598be32b/Documentation/accounting/delay-accounting.rst); [`kernel/taskstats.c`](https://github.com/torvalds/linux/blob/a804c5742bff224d4cb4d25dd383554d598be32b/kernel/taskstats.c)
- [`mm/vmstat.c`](https://github.com/torvalds/linux/blob/v6.8/mm/vmstat.c#L1830) (counter names; `pgpgin`/`pgpgout` sector-to-KiB conversion); memory-stall callers found by searching `psi_memstall_enter` (`mm/vmscan.c`, `mm/compaction.c`, `mm/page_alloc.c`, `mm/filemap.c`, `mm/readahead.c`, `mm/page_io.c`, `mm/memcontrol.c`)
- [`Documentation/admin-guide/sysctl/vm.rst`](https://github.com/torvalds/linux/blob/v6.8/Documentation/admin-guide/sysctl/vm.rst): overcommit
- [`Documentation/power/runtime_pm.rst`](https://github.com/torvalds/linux/blob/v6.8/Documentation/power/runtime_pm.rst): `pm_runtime_get_sync`, `pm_runtime_get_if_active`
- [`Documentation/gpu/drm-usage-stats.rst`](https://github.com/torvalds/linux/blob/v7.0/Documentation/gpu/drm-usage-stats.rst); [`Documentation/gpu/i915.rst`](https://github.com/torvalds/linux/blob/a804c5742bff224d4cb4d25dd383554d598be32b/Documentation/gpu/i915.rst)
- amdgpu: [`amdgpu_pm.c` at v6.8](https://github.com/torvalds/linux/blob/v6.8/drivers/gpu/drm/amd/pm/amdgpu_pm.c#L1510) (`amdgpu_hwmon_get_sensor_generic` with `pm_runtime_get_sync`, used by the busy-percent and hwmon sensor reads) and [at `a804c57`](https://github.com/torvalds/linux/blob/a804c5742bff224d4cb4d25dd383554d598be32b/drivers/gpu/drm/amd/pm/amdgpu_pm.c#L1448) (`amdgpu_pm_get_sensor_generic` with `amdgpu_pm_get_access_if_active`); `pm_runtime_get_if_active` is absent from that file at v6.12 and present at v6.13. [`amdgpu_fdinfo.c`](https://github.com/torvalds/linux/blob/a804c5742bff224d4cb4d25dd383554d598be32b/drivers/gpu/drm/amd/amdgpu/amdgpu_fdinfo.c), [`amdgpu_vram_mgr.c`](https://github.com/torvalds/linux/blob/a804c5742bff224d4cb4d25dd383554d598be32b/drivers/gpu/drm/amd/amdgpu/amdgpu_vram_mgr.c), [`amdgpu_gtt_mgr.c`](https://github.com/torvalds/linux/blob/a804c5742bff224d4cb4d25dd383554d598be32b/drivers/gpu/drm/amd/amdgpu/amdgpu_gtt_mgr.c)
- [`fs/proc/fd.c`](https://github.com/torvalds/linux/blob/a804c5742bff224d4cb4d25dd383554d598be32b/fs/proc/fd.c), [`fs/proc/task_mmu.c`](https://github.com/torvalds/linux/blob/a804c5742bff224d4cb4d25dd383554d598be32b/fs/proc/task_mmu.c): access checks and the smaps_rollup page walk

Ubuntu and systemd:
[Ubuntu 24.04 release notes](https://discourse.ubuntu.com/t/noble-numbat-release-notes/39890)
("Ubuntu 24.04 LTS includes the new 6.8 Linux kernel"; 24.04.4 notes a 6.17
HWE kernel; systemd v255.4);
[26.04 LTS ships Linux 7.0](https://discourse.ubuntu.com/t/26-04-lts-resolute-raccoon-shipping-with-the-final-7-0-linux-kernel/80838);
[`systemd-oomd.service(8)`, systemd v255](https://www.freedesktop.org/software/systemd/man/255/systemd-oomd.service.html).

macOS:

- SDK headers, MacOSX15.5 SDK (Command Line Tools): `mach/vm_statistics.h`,
  `mach/task_info.h`, `sys/resource.h`, `sys/sysctl.h`, `dispatch/source.h`,
  `os/proc.h`.
- XNU at [`xnu-11417.140.69`](https://github.com/apple-oss-distributions/xnu/tree/xnu-11417.140.69),
  the exact kernel on Big Mac:
  [`osfmk/kern/host.c`](https://github.com/apple-oss-distributions/xnu/blob/xnu-11417.140.69/osfmk/kern/host.c#L799),
  [`osfmk/vm/vm_pageout.c`](https://github.com/apple-oss-distributions/xnu/blob/xnu-11417.140.69/osfmk/vm/vm_pageout.c#L10011)
  (pressure at L10011, `memorystatus_level` at L4703, `pageins` at L6405),
  [`osfmk/vm/vm_page.h`](https://github.com/apple-oss-distributions/xnu/blob/xnu-11417.140.69/osfmk/vm/vm_page.h#L1519),
  [`bsd/kern/kern_memorystatus.c`](https://github.com/apple-oss-distributions/xnu/blob/xnu-11417.140.69/bsd/kern/kern_memorystatus.c#L105),
  [`bsd/kern/kern_memorystatus_notify.c`](https://github.com/apple-oss-distributions/xnu/blob/xnu-11417.140.69/bsd/kern/kern_memorystatus_notify.c#L1817),
  [`osfmk/kern/task.c`](https://github.com/apple-oss-distributions/xnu/blob/xnu-11417.140.69/osfmk/kern/task.c#L1185)
  (footprint at L1185, `TASK_VM_INFO` at L6125),
  [`osfmk/vm/vm_compressor_backing_store.c`](https://github.com/apple-oss-distributions/xnu/blob/xnu-11417.140.69/osfmk/vm/vm_compressor_backing_store.c#L2296),
  [`osfmk/vm/vm_compressor.c`](https://github.com/apple-oss-distributions/xnu/blob/xnu-11417.140.69/osfmk/vm/vm_compressor.c#L5411),
  [`osfmk/vm/vm_fault.c`](https://github.com/apple-oss-distributions/xnu/blob/xnu-11417.140.69/osfmk/vm/vm_fault.c#L209),
  [`bsd/kern/kern_proc.c`](https://github.com/apple-oss-distributions/xnu/blob/xnu-11417.140.69/bsd/kern/kern_proc.c#L5660),
  [`bsd/kern/kern_sysctl.c`](https://github.com/apple-oss-distributions/xnu/blob/xnu-11417.140.69/bsd/kern/kern_sysctl.c#L4628)
  (reclaim and defragmentation swap-in sysctls).
- `vm_stat`: [`system_cmds-1026.140.2`](https://github.com/apple-oss-distributions/system_cmds/blob/system_cmds-1026.140.2/vm_stat/vm_stat.c#L131),
  the version named in Big Mac's `/usr/bin/vm_stat`.
- Apple: [Identifying high-memory use with jetsam event reports](https://developer.apple.com/documentation/xcode/identifying-high-memory-use-with-jetsam-event-reports).
  The page is written for iOS apps; Big Mac's DiagnosticReports folders held
  JetsamEvent reports that the prototype parsed (History).
- The `PerformanceStatistics` keys are undocumented by Apple. Their meanings
  here come from the Big Mac probes and the prototype's code.

NVIDIA, NVML API reference R550:
[`nvmlUtilization_t`](https://docs.nvidia.com/deploy/archive/R550/nvml-api/structnvmlUtilization__t.html),
[device queries](https://docs.nvidia.com/deploy/archive/R550/nvml-api/group__nvmlDeviceQueries.html),
[clock-event reasons](https://docs.nvidia.com/deploy/archive/R550/nvml-api/group__nvmlClocksEventReasons.html);
[DCGM profiling metrics](https://docs.nvidia.com/datacenter/dcgm/latest/learn/modules/profiling.html).
That page's headings and field IDs disagree; this document follows the metric
descriptions.

Code: an earlier private prototype's source and its own notes on memory,
GPU and measurement cost. Not linked.

Probes on Big Mac, 2026-10-21: `sysctl`, `vm_stat`,
`system_profiler SPDisplaysDataType`, `ioreg -a -r -c IOAccelerator` (which
reset driver counters, as stated above), a Python `ctypes` call of `task_info`
on the user's own processes, and aggregate `sqlite3` queries opened read-only
on the prototype's store. No process names, command lines, or private data were
recorded.
