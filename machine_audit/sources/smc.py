"""AppleSMC read methods only. No control or write selector is present."""
from __future__ import annotations
import ctypes as C
import math
import statistics
import struct
import threading
import time
from .native import U32, I32, bind
from .macos import field


class Version(C.Structure):
    _fields_ = [('major', C.c_char), ('minor', C.c_char), ('build', C.c_char), ('reserved', C.c_char), ('release', C.c_uint16)]


class Limit(C.Structure):
    _fields_ = [('version', C.c_uint16), ('length', C.c_uint16), ('cpu', U32), ('gpu', U32), ('mem', U32)]


class Info(C.Structure):
    _fields_ = [('size', U32), ('type', U32), ('attributes', C.c_char)]


class Param(C.Structure):
    _fields_ = [('key', U32), ('version', Version), ('limit', Limit), ('info', Info),
                ('result', C.c_ubyte), ('status', C.c_ubyte), ('command', C.c_ubyte),
                ('data32', U32), ('bytes', C.c_ubyte * 32)]


def keycode(s):
    return int.from_bytes(s.encode('ascii'), 'big')


def decode(kind, raw):
    if kind == 'sp78' and len(raw) == 2:
        return int.from_bytes(raw, 'big', signed=True) / 256
    if kind == 'fpe2' and len(raw) == 2:
        return int.from_bytes(raw, 'big') / 4
    if kind == 'flt ' and len(raw) == 4:
        return struct.unpack('<f', raw)[0]
    if kind in ('ui8 ', 'ui16', 'ui32'):
        return int.from_bytes(raw, 'big')
    raise ValueError('unsupported_smc_type')


def admitted_sensors(candidates):
    candidates = {k: (t, v) for k, (t, v) in candidates.items()
                  if len(k) == 4 and k.startswith('T') and t in ('sp78', 'flt ', 'ui8 ')
                  and math.isfinite(v) and 0 < v < 120
                  and k not in ('TCXR', 'TCXr')
                  and not (k.startswith('TC') and k[-1] in 'EFJT')
                  and not (k.startswith('TG') and k[-1] in 'EF')
                  and not (k.startswith('TH') and k[-1] in 'FR')}
    raw = {'TA': 'p', 'Tb': 'p', 'TC': 'p', 'TG': 'pd', 'TH': 'abcdpx', 'TM': 'p', 'Tm': 'p', 'Tp': 'p'}
    return {k: v for k, v in candidates.items()
            if not (k[-1] in raw.get(k[:2], '') and k[:-1] + k[-1].upper() in candidates)}


class SMC:
    headline = {'TCXc', 'TC0P', 'TG0D', 'TG1D', 'TH0a', 'TPCD', 'TA0P'}
    powers = {'PSTR': 'system_reported', 'PC0R': 'cpu_input_rail', 'PG0R': 'gpu_rail_0', 'PG2R': 'gpu_rail_2'}

    def __init__(self, io, native):
        self.io, self.native = io, native
        self.conn = U32()
        self.metadata, self.sensors = {}, None
        self.lock = threading.Lock()
        self.fan_costs = []
        self.fan_cadence_s = 5

    def _open(self):
        if self.conn.value:
            return
        for obj in self.io.objects('AppleSMC'):
            rc = bind(self.io.io, 'IOServiceOpen', I32, U32, U32, U32, C.POINTER(U32))(
                obj, self.native.task, 0, C.byref(self.conn))
            if rc:
                raise PermissionError('smc_denied')
            return
        raise OSError('smc_unavailable')

    def _call(self, arg):
        self._open()
        out, size = Param(), C.c_size_t(C.sizeof(Param))
        rc = bind(self.io.io, 'IOConnectCallStructMethod', I32, U32, U32, C.c_void_p, C.c_size_t,
                  C.c_void_p, C.POINTER(C.c_size_t))(
            self.conn.value, 2, C.byref(arg), C.sizeof(arg), C.byref(out), C.byref(size))
        if rc or out.result or size.value != C.sizeof(Param):
            raise OSError('smc_read_failed')
        return out

    def _value(self, key):
        if key not in self.metadata:
            p = Param(key=keycode(key), command=9)
            self.metadata[key] = self._call(p).info
        info = self.metadata[key]
        if not 0 < info.size <= 4:
            raise OSError('smc_type_unavailable')
        p = Param(key=keycode(key), command=5)
        p.info.size = info.size
        out = self._call(p)
        kind = info.type.to_bytes(4, 'big').decode('ascii')
        try:
            return kind, decode(kind, bytes(out.bytes[:info.size]))
        except (ValueError, UnicodeError) as exc:
            raise OSError('smc_type_unavailable') from exc

    def _discover(self):
        # Enumeration reads metadata; candidate temperatures are admitted once.
        p = Param(key=keycode('#KEY'), command=5)
        p.info.size = 4
        count = int.from_bytes(bytes(self._call(p).bytes[:4]), 'big')
        if count > 16384:
            raise OSError('smc_key_limit')
        values = {}
        for i in range(count):
            k = self._call(Param(command=8, data32=i)).key.to_bytes(4, 'big').decode('ascii', 'replace')
            if len(k) == 4 and k.startswith('T'):
                try:
                    values[k] = self._value(k)
                except OSError:
                    continue
        self.sensors = admitted_sensors(values)

    def temperatures(self, full=False):
        with self.lock:
            if self.sensors is None:
                self._discover()
            out = []
            known={'TCXc':('CPU die','cpu'),'TC0P':('CPU proximity','cpu'),'TG0D':('GPU 1 die','gpu'),
                   'TG1D':('GPU 2 die','gpu'),'TH0a':('Storage','storage'),'TA0P':('Ambient','ambient'),
                   'TPCD':('PCH','other'),'TB0T':('Battery','battery'),'TC0H':('CPU heatsink','heatsink'),'TG0H':('GPU heatsink','heatsink')}
            for index,key in enumerate(sorted(self.sensors),1):
                if not full and key not in self.headline:
                    continue
                try:
                    value = self._value(key)[1]
                    valid = math.isfinite(value) and 0 < value < 120
                except OSError:
                    value, valid = None, False
                out.append(field('heat.temperature', value if valid else None, 'degC',
                                 entity='smc:' + key, scope=known.get(key,('', 'other'))[1], label=known.get(key,(f'Sensor {index}',))[0], status='observed' if valid else 'failed'))
            return out

    def power(self):
        with self.lock:
            out = []
            for key, label in self.powers.items():
                try:
                    value = self._value(key)[1]
                    valid = math.isfinite(value) and 0 <= value < 10000
                except OSError:
                    value, valid = None, False
                out.append(field('power.watts', value if valid else None, 'watts', entity='smc:' + key,
                                 scope=label, label=label, status='observed' if valid else 'failed'))
            return out

    def fans(self):
        start = time.thread_time_ns()
        with self.lock:
            count = int(self._value('FNum')[1])
            if not 0 <= count <= 10:
                raise OSError('fan_count_invalid')
            out = []
            for i in range(count):
                for suffix, name in [('Ac', 'rpm'), ('Mn', 'minimum'), ('Mx', 'maximum')]:
                    try:
                        value = self._value(f'F{i}{suffix}')[1]
                        valid = math.isfinite(value) and 0 <= value <= 20000
                    except OSError:
                        value, valid = None, False
                    out.append(field('heat.fan_' + name, value if valid else None, 'rpm', entity=f'fan:{i}',
                                     scope='fan', status='observed' if valid else 'failed'))
            if len(self.fan_costs) < 12:
                self.fan_costs.append(time.thread_time_ns() - start)
                if len(self.fan_costs) == 12 and statistics.median(self.fan_costs) > 5_000_000:
                    self.fan_cadence_s = 60
            out.append(field('heat.fan_cadence', self.fan_cadence_s, 'seconds'))
            return out

    def close(self):
        if self.conn.value:
            bind(self.io.io, 'IOServiceClose', I32, U32)(self.conn.value)
            self.conn.value = 0
