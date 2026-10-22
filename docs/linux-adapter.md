# Linux adapter plan

Status: plan. Targets: Ubuntu 24.04 (kernel 6.8, Python 3.12,
systemd 255) and 26.04 (kernel 7.0, Python 3.14, systemd 259). Validation runs
on those machines; fixtures prove parsing only.

## Layout

- `sources/__init__.py` switches on `sys.platform`; Linux imports
  `sources/linux/`. The Mac path stays unchanged.
- `linux/host.py`: clock (`time_ns`, `CLOCK_MONOTONIC`, `CLOCK_BOOTTIME`),
  boot id (`/proc/sys/kernel/random/boot_id`), CPU, memory, process pass.
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

Unit entity: `cgroup:` + HMAC(salt, path)[:16]; label: the admitted leaf
name. Units are the first `.service`/`.scope` below slices, a non-overlapping
level; host minus units is the shown remainder. Per-PID fields only for each
unit's top consumers and our own processes.

## Gaps

`shipped_helper_required`: other users' `/proc/<pid>/io`, RAPL package power.
`icmp_not_permitted`: ICMP when `ping_group_range` excludes us; probes fall
back to TCP connect. `unavailable/no_devices`: thermal zones, fans, cpufreq,
GPU, battery, Wi-Fi when absent. `not_enabled`: GPU clients (DRM fdinfo),
capacity test.

## Fixtures

`tests/sources/linux/<case>/` holds `proc/`, `sys/` and cgroup trees with
synthetic text in the kernel's formats, including 7.0's extra keys. Captured,
scrubbed files replace them when the first user's bundle arrives.

## Slices

1. Clock, boot id, CPU, memory, units (CPU, memory), self, platform switch.
2. Storage, network, heat, power, GPU.
3. Scanner walk, nightly schedule, default exclusions, inotify hot set.
4. Linux setup, doctor, start, stop; system unit file; onboarding text.
