"""Device sources, with AMD read/reset semantics retained in field kinds."""
from __future__ import annotations

import re
from .macos import field
from .native import I32, U64, bind
import ctypes as C
from machine_audit.model import Clock

GPU_FIELDS = {
    'GPU Activity(%)': ('activity', 'percent', 'rate'),
    'Device Utilization %': ('utilization', 'percent', 'rate'),
    'inUseVidMemoryBytes': ('vram_used', 'bytes', 'gauge'),
    'vramTotalBytes': ('vram_total', 'bytes', 'gauge'),
    'vramFreeBytes': ('vram_free', 'bytes', 'gauge'),
    'orphanedReusableVidMemoryBytes': ('vram_orphaned_reusable', 'bytes', 'gauge'),
    'Temperature(C)': ('temperature', 'degC', 'gauge'),
    'Total Power(W)': ('power', 'watts', 'gauge'),
    'Fan Speed(%)': ('fan', 'percent', 'gauge'),
    'gartUsedBytes': ('gart_used', 'bytes', 'gauge'),
    'gartSizeBytes': ('gart_size', 'bytes', 'gauge'),
    'inUseSysMemoryBytes': ('system_memory_used', 'bytes', 'gauge'),
    'In use system memory': ('system_memory_used', 'bytes', 'gauge'),
    'Core Clock(MHz)': ('core_clock', 'MHz', 'gauge'),
    'Memory Clock(MHz)': ('memory_clock', 'MHz', 'gauge'),
    'GPU Core Utilization': ('core_utilization', 'driver_units', 'rate'),
    'hardwareWaitTime': ('hardware_wait', 'unknown_units', 'accumulator'),
    'hardwareSubmitWaitTime': ('submit_wait', 'unknown_units', 'accumulator'),
    'clientGLWaitTime': ('client_gl_wait', 'unknown_units', 'accumulator'),
    'vramEvictionWaitTime': ('vram_eviction_wait', 'unknown_units', 'accumulator'),
    'texturePageOutBytes': ('texture_pageout', 'bytes', 'accumulator'),
    'stdTexturePageInBytes': ('texture_pagein', 'bytes', 'accumulator'),
    'ioSurfacePageInBytes': ('surface_pagein', 'bytes', 'accumulator'),
    'recoveryCount': ('recovery_count', 'unknown_units', 'gauge'),
}
DISK_FIELDS = {
    'Operations (Read)': ('read_operations', 'operations'),
    'Operations (Write)': ('write_operations', 'operations'),
    'Bytes (Read)': ('read', 'bytes'), 'Bytes (Write)': ('write', 'bytes'),
    'Total Time (Read)': ('read_time', 'ns'), 'Total Time (Write)': ('write_time', 'ns'),
    'Latency Time (Read)': ('read_latency', 'ns'), 'Latency Time (Write)': ('write_latency', 'ns'),
}


class Devices:
    def __init__(self, io, clock=Clock.now):
        self.io = io
        self.clock = clock
        self.thermal_token = None

    def gpu(self):
        for obj in self.io.objects('IOAccelerator'):
            entity = 'gpu:' + str(self.io.identity(obj))
            # The whole property read resets AMD statistics even for selected keys.
            stats = self.io.property(obj, 'PerformanceStatistics') or {}
            emitted = set()
            for key, (name, unit, kind) in GPU_FIELDS.items():
                if name in emitted:
                    continue
                value = stats.get(key)
                if isinstance(value, (int, float)):
                    emitted.add(name)
                    stamp=self.clock()
                    yield field('gpu.' + name, value, unit, kind, entity=entity, scope='gpu',
                                status='unobserved' if kind=='accumulator' else 'observed',
                                start=stamp,end=stamp)
            for engine in ('GFX','SDMA0','SDMA1','VCN0Dec','VCN0Enc','VCN0EncLLQ'):
                for operation in ('Submitted','Completed'):
                    value=stats.get(f'HWChannel {engine} | Commands {operation}')
                    if isinstance(value,int):
                        yield field(f'gpu.commands_{engine.lower()}_{operation.lower()}',value,'count','accumulator',
                                    entity=entity,scope='gpu',status='unobserved')
            yield field('gpu.window', 'unknown driver window', 'state', 'state', entity=entity, scope='gpu')
            yield field('gpu.reset_semantics', 'AMD dictionary read resets some counters', 'state', 'state', entity=entity, scope='gpu')
            yield field('gpu.queue_wait', None, 'ns', 'counter', entity=entity, scope='gpu')

    def clients(self):
        for obj in self.io.objects('IOAccelerator'):
            gpu = self.io.identity(obj)
            for client in self.io.children(obj, recursive=True):
                value = self.io.property(client, 'accumulatedGPUTime')
                if value is None:
                    usage = self.io.property(client, 'AppUsage')
                    if isinstance(usage, list):
                        vals = [u.get('accumulatedGPUTime') for u in usage if isinstance(u, dict)]
                        if vals and all(isinstance(v, int) for v in vals):
                            value = sum(vals)
                if not isinstance(value, int):
                    continue
                raw = self.io.property(client, 'IOUserClientCreator')
                match = re.match(r'^pid ([0-9]+)(?:,|$)', raw or '')
                entity = f'gpu-client:{gpu}:{self.io.identity(client)}'
                yield field('gpu.client_time', value, 'ns', 'counter', entity=entity,
                            scope='gpu_client', additive=True, scale=1e-9)
                if match:
                    yield field('gpu.client_pid', int(match[1]), 'pid', 'state', entity=entity, scope='gpu_client')
                # Driver visibility is not permission to name another user's process.

    def disk(self):
        for driver in self.io.objects('IOBlockStorageDriver'):
            stats = self.io.property(driver, 'Statistics') or {}
            whole = [self.io.identity(c) for c in self.io.children(driver)
                     if self.io.property(c, 'Whole') is True]
            if len(whole) != 1:
                continue
            entity = 'disk:' + str(whole[0])
            for key, (name, unit) in DISK_FIELDS.items():
                value = stats.get(key)
                yield field('disk.' + name, value if isinstance(value, int) else None, unit, 'counter',
                            entity=entity, scope='physical_disk', additive=True,
                            scale=1e-9 if unit == 'ns' else 1)
        yield field('disk.pressure', None, 'ns', 'counter', scope='host')

    def supply(self):
        source={'AC Power':'ac','Battery Power':'battery','UPS Power':'ups'}.get(self.io.power_supply())
        yield field('power.supply',source,'state','state')
        d = self.io.battery()
        yield field('power.battery_present', d is not None, 'boolean', 'state')
        if d is None:
            return
        for key, name, unit, kind in [('Current Capacity', 'battery_current_capacity', 'source_units', 'gauge'),
            ('Max Capacity', 'battery_max_capacity', 'source_units', 'gauge'),
            ('Is Charging', 'charging', 'boolean', 'state'), ('Time to Empty', 'time_to_empty', 'minutes', 'gauge'),
            ('Time to Full Charge', 'time_to_full', 'minutes', 'gauge')]:
            value = d.get(key)
            yield field('power.' + name, value if isinstance(value, (int, float)) and value >= 0 else None, unit, kind)

    def heat(self):
        status=self.io.power_status()
        for key in ('CPU_Speed_Limit', 'CPU_Scheduler_Limit', 'CPU_Available_CPUs'):
            value = status.get(key)
            yield field('heat.' + key.lower(), value if isinstance(value, int) else None,
                        'cpus' if key == 'CPU_Available_CPUs' else 'percent', 'state')

    def thermal(self):
        lib=C.CDLL('/usr/lib/libSystem.B.dylib')
        if self.thermal_token is None:
            token=I32()
            try:key=C.c_char_p.in_dll(self.io.io,'kOSThermalNotificationPressureLevelName').value
            except ValueError:raise OSError('thermal_notification_unavailable')
            if bind(lib,'notify_register_check',I32,C.c_char_p,C.POINTER(I32))(key,C.byref(token))!=0:
                yield field('heat.thermal_pressure',None,'level','state',status='failed')
                return
            self.thermal_token=token.value
        value=U64()
        if bind(lib,'notify_get_state',I32,I32,C.POINTER(U64))(self.thermal_token,C.byref(value)):
            yield field('heat.thermal_pressure',None,'level','state',status='failed')
        else:
            yield field('heat.thermal_pressure',value.value,'level','state')
