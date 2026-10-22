# Linux adapter plan

Status: slice 1 built (CPU, memory, units, self); slices 2–4 planned.
Targets: Ubuntu 24.04 (kernel 6.8, Python 3.12, systemd 255) and 26.04 (kernel
7.0, Python 3.14, systemd 259). Validation runs on those machines; fixtures
prove parsing only.

## Layout

- `sources/__init__.py` switches on `sys.platform`; Linux imports
  `sources/linux/`. The Mac path stays unchanged.
- `linux/host.py`: boot id (`/proc/sys/kernel/random/boot_id`), CPU, memory,
  and the workload pass. `Clock.now()` already reads `CLOCK_MONOTONIC` and
  `CLOCK_BOOTTIME` on Linux, so it is the native clock.
- `linux/cgroups.py`: unit attribution. `linux/devices.py`: disks, volumes,
  hwmon heat and power, amdgpu. `linux/network.py`: counters and probes.
- `self_cost.py` stays shared, with a Linux field map.
- Every source reads through a `root` path (default `/`), so tests point it
  at a directory tree instead of mocking.

## What each component reads

| Component | Sources (cadence) |
| --- | --- |
| CPU | `/proc/stat` per-CPU states, `procs_running`, `/proc/loadavg`, `/proc/pressure/cpu` (1 s); unit `cpu.stat` (10 s); `scaling_cur_freq` when present (5 s) |
| Memory | `/proc/meminfo`, `/proc/vmstat`, `/proc/pressure/memory` by key (1 s); unit `memory.current`, `memory.stat`, `memory.events`, pressure (10 s); limits (60 s) |
| Storage | `/proc/diskstats`, `/proc/pressure/io`, unit `io.stat` (5 s/10 s); `statvfs` per local mount (60 s) |
| Network | `/proc/net/dev` by interface index, `operstate` as state (10 s); `/proc/net/snmp` (10 s); probes (5 s); resolver lookup and connect (60 s) |
| Heat, power | hwmon by chip and label: `k10temp` Tctl, `amdgpu` edge and `power1_average`, `nvme` Composite, Wi-Fi phy (5 s) |
| GPU | amdgpu `gpu_busy_percent`, `mem_info_vram_*`, `mem_info_gtt_*` (5 s), skipped while runtime-suspended |
| Self | own `/proc/<pid>/stat`, `statm`, `io`; scanner run records |

## Workloads

One 10 s pass, `linux.workloads`, reads units and their processes together.

- A unit is the first non-slice cgroup below the slices: one non-overlapping
  level. Host minus units is the remainder (root cgroup, kernel work).
- Entity: `cgroup:` + HMAC(salt, path)[:16] + `:` + directory inode, so a
  restarted unit starts a new counter baseline. Scope `unit` ranks units in
  `who` like programs, never as host series.
- Label: the leaf name; a templated unit uses its template name
  (`getty@tty1.service` → `getty.service`), because instances can carry
  private names. Mount, automount and swap units are labelled `mount`,
  `automount` or `swap`: their names are escaped paths. Labels containing a
  login name or the host's short name become `redacted`; a common host name
  such as `ubuntu` also redacts legitimate names like
  `ubuntu-advantage.service`.
- Kept keys: `cpu.stat` usage and throttling, `memory.current`,
  `memory.swap.current`, the `memory.stat` usage and movement keys listed in
  `cgroups.py`, `memory.events`, CPU and memory PSI totals. Keys a newer
  kernel adds are ignored until reviewed; a file a controller lacks is absent.
- Per unit, per-PID fields cover the 3 largest CPU users since the last pass,
  the largest resident set, and our own processes. A process entering that
  set starts with a baseline window; its CPU in that window stays in the unit
  total. `unit.processes` and `unit.processes_sampled` give coverage.
- Process labels come from `comm` (the field in `/proc/<pid>/stat`) for
  every process. Residual under the upload hold: a script's `comm` is its
  file name. Linux `self.memory` is the resident set, not the Mac footprint.
- The boot id is a hash of the kernel's `boot_id`, as the Mac hashes its boot
  time, so the kernel value never enters an entity.

Host CPU busy time is user+nice+system+irq+softirq per `cpu-view.md`; iowait,
steal and guest (already inside user) are kept but not added. `/proc/meminfo`
`kB` values are stored as bytes (exact ×1024). PSI totals are microsecond
counters whose rate reads as percent of time stalled.

## Gaps

`shipped_helper_required`: other users' `/proc/<pid>/io`, RAPL package power.
`icmp_not_permitted`: ICMP when `ping_group_range` excludes us; probes fall
back to TCP connect. `unavailable/no_devices`: thermal zones, fans, cpufreq,
GPU, battery, Wi-Fi when absent. `not_enabled`: GPU clients (DRM fdinfo),
capacity test.

## Fixtures

A bundle is one text file of named sections, each `==> /proc/stat <==`
followed by that file's text. Tests write the sections under a temporary root
and point the sources at it. `tests/sources/linux/synthetic.txt` is synthetic
until the first user's captured, scrubbed bundles replace it.

## Slices

1. Clock, boot id, CPU, memory, units (CPU, memory), self, platform switch.
2. Storage, network, heat, power, GPU.
3. Scanner walk, nightly schedule, default exclusions, inotify hot set.
4. Linux setup, doctor, start, stop; system unit file; onboarding text.
