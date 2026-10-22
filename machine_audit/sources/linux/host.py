"""Linux CPU, memory and workload sources. Keyed files are parsed by key, never by position."""
from __future__ import annotations

import errno
import hashlib
import os
import pwd
import re
from dataclasses import dataclass, replace
from pathlib import Path

from machine_audit.model import Clock, Reading, process_identity
from ..macos import error_status, field, private_token
from . import cgroups

CPU_STATES = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal', 'guest', 'guest_nice')
# Movement and limit counters named in memory-view.md. pgpgin/pgpgout are KiB.
VMSTAT = {'pgpgin': 'bytes', 'pgpgout': 'bytes', 'pswpin': 'pages', 'pswpout': 'pages'}
VMSTAT.update((k, 'count') for k in (
    'pgfault', 'pgmajfault', 'pgscan_kswapd', 'pgscan_direct', 'pgsteal_kswapd', 'pgsteal_direct',
    'workingset_refault_anon', 'workingset_refault_file', 'allocstall_normal', 'allocstall_movable',
    'oom_kill', 'zswpin', 'zswpout'))
# Per unit: the top CPU users since the last pass, the largest resident set,
# and our own processes. A process entering this set starts with a baseline.
TOP_CPU, TOP_RESIDENT = 3, 1


def gap_status(exc):
    """A file the kernel does not provide, or PSI switched off, is unavailable rather than failed."""
    return 'unavailable' if getattr(exc, 'errno', None) in (errno.ENOENT, errno.EOPNOTSUPP) else error_status(exc)


def keyed(text, separator=' '):
    """'key value' (cgroup, vmstat) or 'Key: value unit' (meminfo, status, io) lines."""
    return {key.strip(): value.split() for key, _, value in (line.partition(separator) for line in text.splitlines()) if value.strip()}


def kib(values, key):
    """The kernel's 'kB' is KiB; the byte value is exact."""
    value = values.get(key)
    return int(value[0]) * 1024 if value else None


def pressure(text):
    """PSI 'some' and 'full' lines; only the integer microsecond totals are kept."""
    return {kind: int(dict(pair.split('=', 1) for pair in pairs)['total'])
            for kind, *pairs in (line.split() for line in text.splitlines() if line.strip())}


def optional(path):
    """A file a controller does not provide is absent, not zero."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def read_boot_id(root):
    """Hashed, as the Mac hashes its boot time, so the kernel's value never enters an entity."""
    value = (Path(root) / 'proc/sys/kernel/random/boot_id').read_text().strip()
    if not re.fullmatch(r'[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}', value):
        raise ValueError('invalid_boot_id')
    return hashlib.sha256(value.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Stat:
    pid: int
    comm: str
    start: int
    user: int
    system: int
    child_user: int
    child_system: int
    threads: int
    resident_pages: int

    @classmethod
    def parse(cls, pid, text):
        # comm may hold spaces and parentheses; the fields after the last ')' are positional (proc(5)).
        head, _, tail = text.rpartition(')')
        f = tail.split()
        return cls(pid, head.partition('(')[2], int(f[19]), int(f[11]), int(f[12]), int(f[13]), int(f[14]),
                   int(f[17]), int(f[21]))


class LinuxHost:
    def __init__(self, root, salt):
        self.root = Path(root)
        self.salt = salt
        self.cgroup = self.root / 'sys/fs/cgroup'
        self.boot_id = read_boot_id(self.root)
        self.hz = os.sysconf('SC_CLK_TCK')
        # Core admission is the final gate; this also drops labels naming a person or this host.
        self.private_tokens = ((self.root / 'proc/sys/kernel/hostname').read_text().strip().split('.')[0],
                               *(p.pw_name for p in pwd.getpwall() if 1000 <= p.pw_uid < 65534))
        self.managed = None
        self.ticks = {}
        self.process_latest = None
        self.config_identity = self.install_revision = None

    def text(self, rel):
        return (self.root / rel).read_text()

    def start(self, pid):
        return Stat.parse(pid, self.text(f'proc/{pid}/stat')).start

    def label(self, name):
        return 'redacted' if private_token(name, self.private_tokens) else name

    def pressure(self, rel, name):
        try:
            totals, kw = pressure(self.text(rel)), {}
        except OSError as exc:
            totals, kw = {}, {'status': gap_status(exc)}
        for kind in ('some', 'full'):
            yield field(f'{name}.pressure.{kind}', totals.get(kind), 'us', 'counter', scale=1e-4,
                        rate_unit='percent', **kw)

    def cpu(self):
        count = 0
        for line in self.text('proc/stat').splitlines():
            name, *values = line.split()
            if name.startswith('cpu') and name[3:].isdigit():
                count += 1
                for state, value in zip(CPU_STATES, values):
                    yield field('cpu.' + state, int(value), 'ticks', 'counter', entity='cpu:' + name[3:],
                                scale=1 / self.hz, additive=True, rate_unit='CPUs')
            elif name == 'procs_running':
                # Instantaneous, and it counts the thread reading it.
                yield field('cpu.runnable', int(values[0]), 'threads')
            elif name == 'procs_blocked':
                yield field('cpu.blocked', int(values[0]), 'threads')
        yield field('cpu.logical', count, 'cpus')
        yield field('cpu.tick_hz', self.hz, 'Hz')
        for window, value in zip(('1m', '5m', '15m'), self.text('proc/loadavg').split()):
            yield field('cpu.load_' + window, float(value), 'threads')
        yield from self.pressure('proc/pressure/cpu', 'cpu')

    def memory(self):
        meminfo = keyed(self.text('proc/meminfo'), ':')
        for key, value in meminfo.items():
            name = 'memory.' + re.sub(r'[^A-Za-z0-9_]', '_', key).strip('_')
            # Unsuffixed meminfo values are huge-page counts.
            sized = value[1:] == ['kB']
            yield field(name, kib(meminfo, key) if sized else int(value[0]), 'bytes' if sized else 'count')
        vmstat = keyed(self.text('proc/vmstat'))
        for key, unit in VMSTAT.items():
            value = vmstat.get(key)
            yield field('memory.' + key, None if value is None else int(value[0]) * (1024 if unit == 'bytes' else 1),
                        unit, 'counter')
        yield field('memory.page_size', os.sysconf('SC_PAGE_SIZE'), 'bytes')
        yield from self.pressure('proc/pressure/memory', 'memory')

    def unit_fields(self, directory, kw):
        directory = Path(directory)
        stat = keyed(optional(directory / 'cpu.stat') or '')
        for key, unit in cgroups.CPU_STAT.items():
            if key in stat:
                rate = dict(scale=1e-6, rate_unit='CPUs') if key in ('usage_usec', 'user_usec', 'system_usec') else {}
                yield field('unit.cpu.stat.' + key, int(stat[key][0]), unit, 'counter', additive=True, **rate, **kw)
        for name in ('memory.current', 'memory.swap.current'):
            text = optional(directory / name)
            if text is not None:
                yield field('unit.' + name, int(text), 'bytes', component='memory', **kw)
        stat = keyed(optional(directory / 'memory.stat') or '')
        for key, unit in cgroups.MEMORY_STAT.items():
            if key in stat:
                yield field('unit.memory.stat.' + key, int(stat[key][0]), unit, 'gauge' if unit == 'bytes' else 'counter',
                            additive=unit == 'count', component='memory', **kw)
        events = keyed(optional(directory / 'memory.events') or '')
        for key in cgroups.MEMORY_EVENTS:
            if key in events:
                yield field('unit.memory.events.' + key, int(events[key][0]), 'count', 'counter', additive=True,
                            component='memory', **kw)
        for resource in ('cpu', 'memory'):
            totals = pressure(optional(directory / (resource + '.pressure')) or '')
            for kind in ('some', 'full'):
                if kind in totals:
                    yield field(f'unit.{resource}.pressure.{kind}', totals[kind], 'us', 'counter', scale=1e-4,
                                rate_unit='percent', component=resource, **kw)

    def process_fields(self, p):
        observed = Clock.now()
        kw = dict(entity=process_identity(p.pid, p.start, self.boot_id), label=self.label(p.comm), scope='process')
        fields = [field('process.' + name, value, 'ticks', 'counter', additive=True, scale=1 / self.hz,
                        rate_unit='CPUs', **kw)
                  for name, value in (('user_time', p.user), ('system_time', p.system),
                                      ('child_user_time', p.child_user), ('child_system_time', p.child_system))]
        fields.append(field('process.threads', p.threads, 'threads', **kw))
        try:
            status = keyed(self.text(f'proc/{p.pid}/status'), ':')
            fields.append(field('process.resident_size', kib(status, 'VmRSS'), 'bytes', **kw))
            fields.append(field('process.swap', kib(status, 'VmSwap'), 'bytes', component='memory', **kw))
            try:
                io = keyed(self.text(f'proc/{p.pid}/io'), ':')
                fields.append(field('process.diskio_bytesread', int(io['read_bytes'][0]), 'bytes', 'counter', additive=True, **kw))
                fields.append(field('process.diskio_byteswritten', int(io['write_bytes'][0]), 'bytes', 'counter', additive=True, **kw))
            except PermissionError:
                pass  # another user's process: the declared linux.other_user_io gap
        except (FileNotFoundError, ProcessLookupError):
            return []  # exited during the pass
        ended = Clock.now()
        return [replace(f, start=observed, end=ended) for f in fields]

    def workloads(self):
        start = Clock.now()
        roles = self.managed.snapshot() if self.managed else {}
        fields, ticks, seen = [], {}, set()
        for rel, directory in cgroups.units(self.cgroup):
            observed = Clock.now()
            try:
                kw = dict(entity=cgroups.identity(self.salt, rel, directory),
                          label=self.label(cgroups.leaf_name(rel)), scope='unit')
                unit = list(self.unit_fields(directory, kw))
            except FileNotFoundError:
                continue  # the unit stopped during the pass
            listed = cgroups.pids(directory)
            procs = []
            for pid in listed:
                if pid in seen:
                    continue  # moved between units during this pass
                seen.add(pid)
                try:
                    procs.append(Stat.parse(pid, self.text(f'proc/{pid}/stat')))
                except OSError:
                    pass  # exited, or hidden by the proc mount; unit.processes_sampled shows it
            for p in procs:
                ticks[(p.pid, p.start)] = p.user + p.system
            chosen = {p.pid: p for p in sorted(procs, reverse=True, key=lambda p: p.user + p.system - self.ticks.get((p.pid, p.start), 0))[:TOP_CPU]}
            chosen.update((p.pid, p) for p in sorted(procs, reverse=True, key=lambda p: p.resident_pages)[:TOP_RESIDENT])
            chosen.update((p.pid, p) for p in procs if (p.pid, str(p.start)) in roles)
            sampled = [self.process_fields(p) for p in chosen.values()]
            unit.append(field('unit.processes', len(listed), 'count', **kw))
            unit.append(field('unit.processes_sampled', sum(1 for s in sampled if s), 'count', **kw))
            ended = Clock.now()
            fields.extend(replace(f, start=observed, end=ended) for f in unit)
            fields.extend(f for s in sampled for f in s)
        self.ticks = ticks
        self.process_latest = Reading('linux.workloads', 'cpu', start, Clock.now(), tuple(fields))
        return fields
