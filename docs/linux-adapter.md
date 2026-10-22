# Linux adapter

Status: slices 1–4 built (all seven components, the scanner, installation);
awaiting validation on the targets.
Targets: Ubuntu 24.04 (kernel 6.8, Python 3.12, systemd 255) and 26.04 (kernel
7.0, Python 3.14, systemd 259). Validation runs on those machines; fixtures
prove parsing only.

## Layout

- `sources/__init__.py` switches on `sys.platform`; Linux imports
  `sources/linux/`. The Mac path stays unchanged.
- `linux/host.py`: boot id (`/proc/sys/kernel/random/boot_id`), CPU, memory,
  and the workload pass. `Clock.now()` already reads `CLOCK_MONOTONIC` and
  `CLOCK_BOOTTIME` on Linux, so it is the native clock.
- `linux/cgroups.py`: unit discovery and identity. `linux/devices.py`: disks,
  volumes, hwmon heat and power, cpufreq, power supplies, amdgpu.
  `linux/network.py`: counters and probes on the shared probe rules.
  `linux/files.py`: the kernel's text formats and salted identities.
  `linux/watcher.py`: the collector's inotify watch for the scanner.
- `self_cost.py` stays shared, with a Linux field map.
- Every source reads through a `root` path (default `/`), so tests point it
  at a directory tree instead of mocking.

## What each component reads

| Component | Sources (cadence) |
| --- | --- |
| CPU | `/proc/stat` host totals from the aggregate line, `procs_running`, `/proc/loadavg`, `/proc/pressure/cpu` (1 s); per-CPU states as `cpu.per_cpu.*` (10 s); unit `cpu.stat` (10 s); `cpu.max`, `cpu.weight` (60 s) |
| Memory | `/proc/meminfo`, `/proc/vmstat`, `/proc/pressure/memory` by key (1 s); unit `memory.current`, `memory.stat`, `memory.events`, pressure (10 s); limits (60 s) |
| Storage | `/proc/diskstats` per whole physical disk, `/proc/pressure/io` (5 s); unit `io.stat`, `io.pressure` (10 s); `statvfs` per local filesystem (60 s) |
| Network | `/proc/net/dev` by interface index with link state, speed and physical flag, `/proc/net/snmp` TCP and UDP (10 s); probes (5 s); resolver lookup and connect (60 s); `/proc/net/wireless` (10 s) |
| Heat | hwmon temperatures by chip and label, with `max`/`crit`; hwmon fans; thermal zones; `scaling_cur_freq` and `scaling_max_freq` (5 s) |
| Power | hwmon `power*_average`, `_input`, `_cap` in microwatts; `/sys/class/power_supply` (5 s) |
| GPU | amdgpu `gpu_busy_percent`, `mem_busy_percent`, `mem_info_vram_*`, `mem_info_gtt_*`, sclk (5 s) |
| Self | the workload pass's own processes; storage and scanner run records |

## Workloads

One 10 s pass, `linux.workloads`, reads units and their processes together.

- A unit is the first non-slice cgroup below the slices: one non-overlapping
  level. Host minus units is the remainder (root cgroup, kernel work).
- Entity: `cgroup:` + HMAC(salt, path)[:16] + `:` + directory inode, so a
  restarted unit starts a new counter baseline. Units are never host series.
  Only consumption (CPU time, `memory.current`, `io.stat`) has scope `unit`
  and ranks in `who`; limits, breakdowns, pressure and process counts have
  scope `unit_detail`.
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
keeps 22 keys (capacity, availability, page classes, swap, compression, dirty
data, commitment), stored as bytes (exact ×1024). PSI totals are microsecond
counters whose rate reads as percent of time stalled. A unit limit of `max`
is `not_enabled` (no limit), never a number.

Storage cost: the first Ubuntu run grew 1.1 MB a minute on 2 vCPUs. Host CPU
totals now come from the aggregate line every second and per-CPU states
every 10 s, so a 32-thread host writes about 42 CPU fields a second instead
of 320; `cpu.capacity` is `cpu.logical`, the online CPU count (on the Mac it
equals the per-CPU count). `/proc/meminfo` keeps 22 keys.

## Devices and network

- Disks: rows of `/proc/diskstats` whose `/sys/block/<name>/device` exists,
  entity `disk:<major>:<minor>`. Partitions, dm, loop and zram rows are
  skipped. Sectors are 512 bytes; busy time reads as percent busy. Unit
  `io.stat` sums physical disks only, because a request through LVM is also
  counted on its dm device.
- Volumes: local disk filesystems from mountinfo, entity `volume:` +
  HMAC(salt, statvfs `f_fsid`)[:16]; `f_fsid` derives from the filesystem
  UUID on ext4. A mount showing part of a tree already listed is a bind
  mount and is skipped; btrfs subvolumes (`@`, `@home`) share a device but
  show disjoint trees, so each is a volume reporting the shared pool.
- Interfaces: entity `interface:<ifindex>`; loopback is skipped. A down link
  is `network.link_state` = `down`, not a failure. `network.physical` marks
  interfaces with a device; bridges, veth and tunnels repeat that traffic.
- Probes reuse the Mac rules. With ping sockets allowed, ICMP replies carry
  `SO_TIMESTAMPNS` receive times. Without them, `linux.icmp` reports
  `icmp_not_permitted`, the router ping has that outcome, and the internet
  probe is a TCP connect to port 443 timed in user space
  (`network.probe_method` = `tcp_connect`); probe byte counts are then
  unobserved. Router identity is the salted hash of its ARP hardware address.
- The resolver is `systemd-resolved stub` when `resolv.conf` names
  127.0.0.53. Probe, resolver and portal targets are the Mac's; they become
  configurable if the first user asks.
- Sensors: entity is a salted hash of the hwmon device path and channel;
  label is chip plus channel label (`k10temp Tctl`, `nvme Composite`).
  Temperatures are millidegrees, stored as °C; driver sentinels for "no
  reading" (at or below -273 °C, or 1000 °C and above, such as -273150 and
  65261850) are unavailable. Power is microwatts, stored as watts, as on
  the Mac.
- GPU: amdgpu cards only, read by default because sysfs reads reset no
  counters (the Mac gates its GPU for that reason). Entity: a salted hash of
  the PCI slot. A card whose
  `power/runtime_status` is `suspended` is not read, nor are its hwmon
  sensors (`unobserved`), because the read wakes it on kernel 6.8.

## Scanner

- `scanner/linux.py` walks each local disk filesystem from mountinfo (volume
  id: the capacity source's salted `f_fsid`) with the shared walker:
  `os.scandir`, `st_blocks` bytes, hard links charged once. ext4 shares no
  blocks: a complete map is `observed` with uses (hard links once) and the
  gap `physical_residual_not_computed`; no residual is claimed. A volume
  root the service user cannot read (an EFI partition) is a `denied` gap on
  that volume; the run still completes.
- Never entered: `/proc`, `/sys`, `/dev`, `/run`, `/tmp` (counted as
  `excluded`, unnamed like configured exclusions), other users' homes
  (`other_account`), the data directory (`own_store`), and a bind mount of
  the walked filesystem (`bind_mount`: same device, and its mountinfo root
  inside the walked root), which would count its contents twice.
- Schedule: a full walk when there is no map, when a run requests one 20 h
  or more after the last (the nightly timer), or after 40 h. Otherwise a run
  re-lists the folders in the dirty file, with the Mac's re-list step
  (`runner.relist`), or finds the map current.
- Watcher (`linux.watcher`, 10 s, in the collector): inotify on the hot set,
  rebuilt after each published map. A change dirties its folder; a folder
  that went away dirties its parent; a queue overflow dirties every hot
  folder. Each read drains the queue without waiting and merges the folders
  into `scanner-dirty.json` (0600, volume and inode numbers only) in one
  short budgeted write. The scanner claims the file by renaming it.
  `self.watched_folders`, `self.watch_dirtied` and `self.watch_overflows`
  show its work. The hot set is one SQL statement, so its read holds the
  WAL snapshot only while it runs. To measure: that statement's time on Big
  Mac's full map; above a second, the scanner's writes after publication
  can wait on it.
- Grain: Linux setup chooses `folders` (storage-view.md); the first Ubuntu
  walk stored 56 MB of names for 613,000 entries at every-file grain.
- Schedule under systemd: the hourly timer's runs between 01:00 and 05:00
  local time walk in full when the last walk is 20 h old or more.

## Installation

`installation_linux.py` writes, as the service user, a release snapshot, a
CLI and three unit files (collector, scanner, hourly timer) with the run
model agreed with the first user; root places them. See
[installation](installation.md#linux-ubuntu-install).

## Gaps

- `shipped_helper_required`: other users' `/proc/<pid>/io`
  (`linux.other_user_io`); RAPL package power when powercap exists
  (`linux.package_power`; `no_devices` without it).
- `icmp_not_permitted` (`linux.icmp`): ping sockets not allowed.
- `unavailable/no_devices`: any of temperatures, fans, thermal zones,
  cpufreq, hwmon power and amdgpu that the host lacks.
- `cgroup_v2_required`: workloads and unit limits without cgroup v2.
- `not_enabled`: GPU clients (`drm_fdinfo_not_implemented`), capacity test.
- Not read: Wi-Fi link rate (nl80211), thermal trip points and cooling
  devices.

## Fixtures

A bundle is one text file of named sections, each `==> /proc/stat <==`
followed by that file's text. Tests write the sections under a temporary root
and point the sources at it. `tests/sources/linux/synthetic.txt` is synthetic
until the first user's captured, scrubbed bundles replace it.

## Slices

1. Clock, boot id, CPU, memory, units (CPU, memory), self, platform switch. Done.
2. Storage, network, heat, power, GPU. Done.
3. Scanner walk, nightly schedule, default exclusions, inotify hot set. Done.
4. Linux setup, doctor, start, stop; system unit file; onboarding text. Done.
