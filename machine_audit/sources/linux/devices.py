"""Linux storage, heat, power and GPU sources from /proc and sysfs. Device names,
paths and PCI addresses stay here; readings carry numbers and salted hashes."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
import re

from ..macos import error_status, field
from .files import gap_status, keyed, optional, psi

# Columns of /proc/diskstats after major, minor and name (Documentation/admin-guide/iostats.rst).
# Discard columns arrived in 4.18 and flush columns in 5.5; absent columns are not emitted.
DISKSTATS = (('read_operations', 'operations'), ('read_merges', 'operations'), ('read', 'sectors'),
             ('read_time', 'ms'), ('write_operations', 'operations'), ('write_merges', 'operations'),
             ('write', 'sectors'), ('write_time', 'ms'), ('in_progress', 'operations'), ('busy_time', 'ms'),
             ('weighted_time', 'ms'), ('discard_operations', 'operations'), ('discard_merges', 'operations'),
             ('discard', 'sectors'), ('discard_time', 'ms'), ('flush_operations', 'operations'), ('flush_time', 'ms'))
FILESYSTEMS = frozenset({'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'f2fs', 'vfat', 'exfat', 'zfs', 'bcachefs'})
RUNTIME_STATES = frozenset({'active', 'suspended', 'suspending', 'resuming', 'unsupported'})
BATTERY_STATES = frozenset({'charging', 'discharging', 'full', 'not charging', 'unknown'})
GPU_FILES = (('utilization', 'gpu_busy_percent', 'percent', 'rate'),
             ('memory_activity', 'mem_busy_percent', 'percent', 'rate'),
             ('vram_used', 'mem_info_vram_used', 'bytes', 'gauge'), ('vram_total', 'mem_info_vram_total', 'bytes', 'gauge'),
             ('gtt_used', 'mem_info_gtt_used', 'bytes', 'gauge'), ('gtt_total', 'mem_info_gtt_total', 'bytes', 'gauge'))


def physical_disks(root):
    """major:minor of whole disks with a device behind them; partitions, dm, loop and zram have none."""
    block = root / 'sys/block'
    return {(d / 'dev').read_text().strip() for d in block.iterdir() if (d / 'device').exists()} if block.exists() else set()


def channels(directory, prefix):
    """Channel numbers of hwmon inputs such as temp1_input, in order."""
    return sorted(int(m[1]) for m in (re.fullmatch(prefix + r'(\d+)_input', p.name) for p in directory.iterdir()) if m)


@dataclass(frozen=True)
class Volume:
    device: str
    mount: str
    filesystem: str


def volumes(root):
    """Local disk filesystems from mountinfo, once per device; bind mounts repeat a device."""
    seen = set()
    for line in (root / 'proc/self/mountinfo').read_text().splitlines():
        left, _, right = line.partition(' - ')
        f, filesystem = left.split(), right.split()[0]
        if filesystem in FILESYSTEMS and f[2] not in seen:
            seen.add(f[2])
            yield Volume(f[2], re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), f[4]), filesystem)


class Devices:
    def __init__(self, root, salt):
        self.root, self.salt = root, salt

    def hashed(self, kind, text):
        return kind + ':' + hmac.new(self.salt, text.encode(), hashlib.sha256).hexdigest()[:16]

    def disks(self):
        disks = physical_disks(self.root)
        for line in (self.root / 'proc/diskstats').read_text().splitlines():
            major, minor, _, *values = line.split()
            if f'{major}:{minor}' not in disks:
                continue
            kw = dict(entity=f'disk:{major}:{minor}', scope='physical_disk')
            for (name, unit), value in zip(DISKSTATS, map(int, values)):
                if unit == 'sectors':
                    value, unit = value * 512, 'bytes'  # diskstats sectors are always 512 bytes
                if name == 'in_progress':
                    yield field('disk.in_progress', value, unit, **kw)
                else:
                    # Busy milliseconds per second, scaled, read as percent of time busy.
                    rate = dict(scale=0.1, rate_unit='percent') if name == 'busy_time' else {}
                    yield field('disk.' + name, value, unit, 'counter', additive=True, **rate, **kw)
        yield from psi(self.root / 'proc/pressure/io', 'io')

    def capacity(self):
        entities = set()
        for volume in volumes(self.root):
            try:
                s = os.statvfs(self.root / volume.mount.lstrip('/'))
            except OSError as exc:
                yield field('storage.capacity', None, 'bytes', status=error_status(exc),
                            entity='device:' + volume.device, scope='volume')
                continue
            entity = f'volume:{s.f_fsid:x}'  # derived from the filesystem UUID on ext4 and btrfs
            if entity in entities:
                continue
            entities.add(entity)
            kw = dict(entity=entity, scope='volume')
            yield field('storage.capacity', s.f_blocks * s.f_frsize, 'bytes', **kw)
            yield field('storage.free', s.f_bfree * s.f_frsize, 'bytes', **kw)
            yield field('storage.available', s.f_bavail * s.f_frsize, 'bytes', **kw)
            yield field('storage.filesystem', volume.filesystem, 'state', 'state', **kw)

    def hwmon(self):
        """(chip, identity, directory, suspended) per hwmon device. An amdgpu device that is
        runtime-suspended is not read: on kernel 6.8 the read wakes it."""
        base = self.root / 'sys/class/hwmon'
        for directory in sorted(base.iterdir()) if base.exists() else ():
            chip = (directory / 'name').read_text().strip()
            device = directory / 'device'
            # The hwmonN index can change between boots; the device path does not.
            identity = os.path.realpath(device if device.exists() else directory)
            status = (optional(device / 'power/runtime_status') or '').strip()
            yield chip, identity, directory, chip == 'amdgpu' and status == 'suspended'

    def sensor_fields(self, prefix, kind, name, unit, divisor=1, limits=()):
        for chip, identity, directory, suspended in self.hwmon():
            for n in channels(directory, prefix):
                label = (optional(directory / f'{prefix}{n}_label') or f'{prefix}{n}').strip()
                kw = dict(entity=self.hashed('sensor', f'{identity}:{prefix}{n}'), label=f'{chip} {label}', scope='sensor')
                if suspended:
                    yield field(name, None, unit, kind, status='unobserved', **kw)
                    continue
                try:
                    value = int((directory / f'{prefix}{n}_input').read_text())
                except OSError as exc:
                    yield field(name, None, unit, kind, status=gap_status(exc), **kw)
                    continue
                yield field(name, value / divisor if divisor != 1 else value, unit, kind, **kw)
                for limit in limits:
                    text = optional(directory / f'{prefix}{n}_{limit}')
                    if text is not None:
                        yield field(f'{name}_{limit}', int(text) / divisor if divisor != 1 else int(text), unit, **kw)

    def temperatures(self):
        # hwmon reports millidegrees Celsius.
        yield from self.sensor_fields('temp', 'gauge', 'heat.temperature', 'degC', 1000, ('max', 'crit'))

    def fans(self):
        yield from self.sensor_fields('fan', 'gauge', 'heat.fan_rpm', 'rpm', limits=('min', 'max'))

    def power(self):
        # power*_average is the driver's own average; power*_input is instantaneous; both microwatts.
        for chip, identity, directory, suspended in self.hwmon():
            for n in sorted({int(m[1]) for m in (re.fullmatch(r'power(\d+)_(?:average|input|cap)', p.name)
                                                 for p in directory.iterdir()) if m}):
                label = (optional(directory / f'power{n}_label') or f'power{n}').strip()
                kw = dict(entity=self.hashed('sensor', f'{identity}:power{n}'), label=f'{chip} {label}', scope='sensor')
                for kind in ('average', 'input', 'cap'):
                    path = directory / f'power{n}_{kind}'
                    if not path.exists():
                        continue
                    if suspended:
                        yield field('power.' + kind, None, 'uW', status='unobserved', **kw)
                        continue
                    try:
                        yield field('power.' + kind, int(path.read_text()), 'uW', **kw)
                    except OSError as exc:
                        yield field('power.' + kind, None, 'uW', status=gap_status(exc), **kw)

    def thermal_zones(self):
        base = self.root / 'sys/class/thermal'
        for zone in sorted(base.glob('thermal_zone*')) if base.exists() else ():
            kw = dict(entity='thermal-zone:' + zone.name[len('thermal_zone'):], label=(zone / 'type').read_text().strip(),
                      scope='thermal_zone')
            try:
                yield field('heat.zone_temperature', int((zone / 'temp').read_text()) / 1000, 'degC', **kw)
            except OSError as exc:
                yield field('heat.zone_temperature', None, 'degC', status=gap_status(exc), **kw)

    def frequency(self):
        # scaling_cur_freq is the frequency the driver requested or estimated, not a measurement (heat-view.md).
        base = self.root / 'sys/devices/system/cpu'
        for cpu in sorted(base.glob('cpu[0-9]*')) if base.exists() else ():
            for name in ('scaling_cur_freq', 'scaling_max_freq'):
                text = optional(cpu / 'cpufreq' / name)
                if text is not None:
                    yield field('heat.' + name, int(text) * 1000, 'Hz', entity='cpu:' + cpu.name[3:], scope='cpu')

    def supply(self):
        base = self.root / 'sys/class/power_supply'
        supplies = [keyed(optional(d / 'uevent') or '', '=') for d in sorted(base.iterdir())] if base.exists() else []
        mains = [s for s in supplies if s.get('POWER_SUPPLY_TYPE') == ['Mains']]
        batteries = [s for s in supplies if s.get('POWER_SUPPLY_TYPE') == ['Battery']]
        yield field('power.mains_online', any(s.get('POWER_SUPPLY_ONLINE') == ['1'] for s in mains) if mains else None,
                    'boolean', 'state')
        yield field('power.battery_present', bool(batteries), 'boolean', 'state')
        for n, battery in enumerate(batteries):
            kw = dict(entity=f'battery:{n}', scope='battery')
            capacity = battery.get('POWER_SUPPLY_CAPACITY')
            yield field('power.battery_capacity', int(capacity[0]) if capacity else None, 'percent', **kw)
            status = ' '.join(battery.get('POWER_SUPPLY_STATUS', ['unknown'])).lower()
            yield field('power.battery_status', status if status in BATTERY_STATES else 'unknown', 'state', 'state', **kw)

    def gpu(self):
        base = self.root / 'sys/class/drm'
        for card in sorted(base.iterdir()) if base.exists() else ():
            device = card / 'device'
            uevent = keyed(optional(device / 'uevent') or '', '=')
            if not re.fullmatch(r'card\d+', card.name) or uevent.get('DRIVER') != ['amdgpu']:
                continue  # connectors, and drivers without these files
            kw = dict(entity=self.hashed('gpu', uevent.get('PCI_SLOT_NAME', [card.name])[0]), scope='gpu')
            status = (optional(device / 'power/runtime_status') or 'unknown').strip()
            yield field('gpu.runtime_status', status if status in RUNTIME_STATES else 'unknown', 'state', 'state', **kw)
            if status == 'suspended':
                continue  # reading would wake it on kernel 6.8
            for name, file, unit, kind in GPU_FILES:
                try:
                    yield field('gpu.' + name, int((device / file).read_text()), unit, kind, **kw)
                except FileNotFoundError:
                    pass  # not exposed by this GPU; an APU has no mem_busy_percent
                except OSError as exc:
                    yield field('gpu.' + name, None, unit, kind, status=gap_status(exc), **kw)
            sclk = next((optional(h / 'freq1_input') for h in sorted((device / 'hwmon').glob('hwmon*'))), None)
            if sclk is not None:
                yield field('gpu.sclk', int(sclk), 'Hz', **kw)
            yield field('gpu.window', 'unknown driver window', 'state', 'state', **kw)
