"""macOS CPU, memory and process sources; original counters and units survive."""
from __future__ import annotations

import ctypes as C
import errno
import hashlib
import os
import struct
import pwd
import socket
import re
import time
from dataclasses import replace
from collections.abc import Callable

from machine_audit.model import Clock, Field, Reading, process_identity
from .native import Darwin, I32, U32, U64, VMStats, SwapUsage, bind


class MacClock:
    def __init__(self, native):
        self.native = native

    def __call__(self):
        n = self.native
        return Clock(time.time_ns(), n.absolute() * n.numer // n.denom,
                     n.continuous() * n.numer // n.denom)


def error_status(exc):
    return 'denied' if isinstance(exc, PermissionError) or getattr(exc, 'errno', None) in (errno.EACCES, errno.EPERM) else 'failed'


def private_token(text, tokens):
    """A user or host name inside a candidate label; short tokens match whole words."""
    return any(t and (t.lower() in text.lower() if len(t)>=4 else re.search(r'\b'+re.escape(t)+r'\b',text,re.I)) for t in tokens)


def field(name, value, unit, kind='gauge', **kw):
    if value is None:
        kw.setdefault('status', 'unavailable')
    elif value == 0 and kw.get('status', 'observed') == 'observed':
        kw['status'] = 'measured_zero'
    return Field(name, value, unit, kind, **kw)


class ReadSource:
    def __init__(self, name, component, cadence_s, read: Callable, clock=Clock.now):
        self.name, self.component, self.cadence_s = name, component, cadence_s
        self._read, self.clock = read, clock

    def read(self):
        start = self.clock()
        try:
            fields = tuple(self._read())
            status, reason = ('observed', None) if fields else ('unavailable', 'no_devices')
        except (OSError, NotImplementedError) as exc:
            status, reason = error_status(exc), 'native_read_failed'
            fields = ()
        return Reading(self.name, self.component, start, self.clock(), fields, status, reason)


class MacOS:
    def __init__(self, native=None, labeler=None):
        self.native = native or Darwin()
        self.clock = MacClock(self.native)
        # Core admission is the final gate; conservatively disclose only public runtime labels here.
        self.private_tokens=(pwd.getpwuid(os.getuid()).pw_name,socket.gethostname().split('.')[0])
        self.labeler = labeler or self._label
        self.labels={}
        self.process_latest = None
        self.tick_hz = struct.unpack_from('=i', self.native.bytes('kern.clockrate'))[0]
        self.boot_id = hashlib.sha256(self.native.bytes('kern.boottime')).hexdigest()[:32]

    def _label(self,path):
        if not path:
            return 'unidentified program'
        base = os.path.basename(path)
        allowed = {'python', 'python3', 'python3.12', 'node', 'ruby', 'java', 'deno', 'bun', 'go', 'cargo', 'uv', 'claude', 'codex', 'grok'}
        if base in allowed:return base
        # Paths come from proc_pidpath and die inside this source. Only public
        # executable basenames pass, with identifying user/host tokens removed.
        public=path.startswith(('/System/','/usr/','/bin/','/sbin/','/Library/','/Applications/','/opt/homebrew/'))
        valid=re.fullmatch(r'[\w .()+-]{1,64}',base) and '..' not in base
        private=private_token(base,self.private_tokens)
        return base if public and valid and not private else 'user program'

    def cpu(self):
        n = self.native
        count, words, data = U32(), U32(), C.POINTER(I32)()
        f = bind(n.lib, 'host_processor_info', I32, U32, I32, C.POINTER(U32), C.POINTER(C.POINTER(I32)), C.POINTER(U32))
        n.check(f(n.host(), 2, C.byref(count), C.byref(data), C.byref(words)))
        try:
            if words.value != count.value * 4:
                raise OSError('cpu_shape_changed')
            for i in range(count.value):
                for j, state in enumerate(('user', 'system', 'idle', 'nice')):
                    yield field('cpu.' + state, U32(data[i * 4 + j]).value, 'ticks', 'counter',
                                entity=f'cpu:{i}', scale=1 / self.tick_hz, additive=True, rate_unit='CPUs')
            yield field('cpu.logical', count.value, 'cpus')
            yield field('cpu.tick_hz', self.tick_hz, 'Hz')
            yield field('cpu.timebase_numer',n.numer,'integer')
            yield field('cpu.timebase_denom',n.denom,'integer')
        finally:
            n.deallocate(n.task, C.cast(data, C.c_void_p).value, words.value * 4)
        pset = U32()
        n.check(bind(n.lib, 'processor_set_default', I32, U32, C.POINTER(U32))(n.host(), C.byref(pset)))
        try:
            load, size = (I32 * 4)(), U32(4)
            n.check(bind(n.lib, 'processor_set_statistics', I32, U32, I32, C.c_void_p, C.POINTER(U32))(
                pset.value, 4, load, C.byref(size)))
            yield field('cpu.runnable', load[2], 'LOAD_SCALE_1000', scale=0.001)
        finally:
            n.port_deallocate(n.task, pset.value)

    def memory(self):
        n, vm = self.native, VMStats()
        size = U32(C.sizeof(vm) // 4)
        n.check(bind(n.lib, 'host_statistics64', I32, U32, I32, C.c_void_p, C.POINTER(U32))(
            n.host(), 4, C.byref(vm), C.byref(size)))
        for name, _ in vm._fields_:
            kind = 'gauge' if name.endswith('_count') and name not in ('zero_fill_count',) else 'counter'
            if name in ('total_uncompressed_pages_in_compressor',):
                kind = 'gauge'
            yield field('memory.' + name, getattr(vm, name), 'pages', kind)
        yield field('memory.page_size', os.sysconf('SC_PAGE_SIZE'), 'bytes')
        for key, name, kind in [('hw.memsize', 'installed', U64),
                               ('kern.memorystatus_vm_pressure_level', 'pressure_level', I32),
                               ('vm.compressor_swapper_reclaim_swapins', 'reclaim_swapins', U64),
                               ('vm.compressor_swapper_defrag_swapins', 'defrag_swapins', U64)]:
            try:
                value = n.value(key, kind)
                yield field('memory.' + name, value, 'bytes' if key == 'hw.memsize' else 'pages' if 'swapins' in name else 'level',
                            'counter' if 'swapins' in name else 'state' if 'pressure' in name else 'gauge')
            except OSError as exc:
                yield field('memory.' + name, None, 'bytes' if key == 'hw.memsize' else 'pages' if 'swapins' in name else 'level', status=error_status(exc))
        try:
            swap = n.value('vm.swapusage', SwapUsage)
            for k in ('total', 'used', 'avail'):
                yield field('memory.swap_' + k, getattr(swap, k), 'bytes')
        except OSError as exc:
            yield field('memory.swap_used', None, 'bytes', status=error_status(exc))

    def processes(self):
        n, start = self.native, self.clock()
        fields = []
        seen=set()
        for pid in n.processes():
            first = len(fields)
            observed = self.clock()
            try:
                r = n.process(pid)
                entity = process_identity(pid, r.proc_start_abstime, self.boot_id)
                cache_key=(pid,r.proc_start_abstime,bytes(r.uuid))
                seen.add(cache_key)
                if cache_key not in self.labels:self.labels[cache_key]=self.labeler(n.process_path(pid))
                label=self.labels[cache_key]
                kw = dict(entity=entity, label=label, scope='process')
                fields.append(field('process.exec_uuid', bytes(r.uuid).hex(), 'uuid', 'state', **kw))
                for k, unit in [('user_time', 'mach_ticks'), ('system_time', 'mach_ticks'), ('runnable_time', 'mach_ticks'),
                    ('child_user_time', 'mach_ticks'), ('child_system_time', 'mach_ticks'), ('phys_footprint', 'bytes'),
                    ('resident_size', 'bytes'), ('lifetime_max_phys_footprint', 'bytes'), ('diskio_bytesread', 'bytes'),
                    ('diskio_byteswritten', 'bytes'), ('logical_writes', 'bytes'), ('pkg_idle_wkups', 'count'),
                    ('interrupt_wkups', 'count'), ('pageins', 'pages')]:
                    gauge = k in ('phys_footprint', 'resident_size', 'lifetime_max_phys_footprint')
                    fields.append(field('process.' + k, getattr(r, k), unit, 'gauge' if gauge else 'counter',
                                        additive=not gauge, scale=n.numer / n.denom / 1e9 if unit == 'mach_ticks' else 1,
                                        rate_unit='CPUs' if unit == 'mach_ticks' else None, **kw))
                try:
                    vm, length = n.task_memory(pid)
                    for k in ('compressed', 'ledger_swapins', 'decompressions', 'limit_bytes_remaining',
                              'ledger_tag_graphics_footprint', 'ledger_tag_graphics_footprint_compressed'):
                        typ = dict(vm._fields_)[k]
                        available = getattr(type(vm), k).offset + C.sizeof(typ) <= length
                        fields.append(field('process.' + k, getattr(vm, k) if available else None,
                                            'count' if k == 'decompressions' else 'bytes',
                                            'counter' if k in ('ledger_swapins', 'decompressions') else 'gauge', **kw))
                except OSError as exc:
                    fields.append(field('process.ledger_swapins', None, 'bytes', 'counter', status=error_status(exc), **kw))
                # Identity is rechecked after the separate VM call, avoiding PID-reuse joins.
                ended = self.clock()
                fields[first:] = [replace(f,start=observed,end=ended) for f in fields[first:]]
                if n.process(pid).proc_start_abstime != r.proc_start_abstime:
                    fields = [f for f in fields if f.entity != entity]
                    fields.append(field('process.coverage', None, 'state', 'state', status='invalid', entity=f'pid:{pid}', scope='process'))
            except OSError as exc:
                fields.append(field('process.coverage', None, 'state', 'state', status=error_status(exc), entity=f'pid:{pid}', scope='process'))
        self.labels={k:v for k,v in self.labels.items() if k in seen}
        self.process_latest = Reading('macos.process', 'cpu', start, self.clock(), tuple(fields))
        return fields

    def sources(self):
        return [ReadSource('macos.cpu', 'cpu', 1, self.cpu, self.clock),
                ReadSource('macos.memory', 'memory', 1, self.memory, self.clock),
                ReadSource('macos.process', 'cpu', 10, self.processes, self.clock)]
