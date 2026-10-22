"""Read-only Darwin ABI bindings. Importing this module performs no OS reads."""
from __future__ import annotations

import ctypes as C
import errno
import os
import sys

U32, U64, I32, I64 = C.c_uint32, C.c_uint64, C.c_int32, C.c_int64
PROC_UID_ONLY = 4  # sys/proc_info.h; 2 selects a process group.


class Timebase(C.Structure):
    _fields_ = [('numer', U32), ('denom', U32)]


class RUsage4(C.Structure):
    _fields_ = [('uuid', C.c_ubyte * 16)] + [(n, U64) for n in (
        'user_time system_time pkg_idle_wkups interrupt_wkups pageins wired_size resident_size '
        'phys_footprint proc_start_abstime proc_exit_abstime child_user_time child_system_time '
        'child_pkg_idle_wkups child_interrupt_wkups child_pageins child_elapsed_abstime '
        'diskio_bytesread diskio_byteswritten cpu_time_qos_default cpu_time_qos_maintenance '
        'cpu_time_qos_background cpu_time_qos_utility cpu_time_qos_legacy cpu_time_qos_user_initiated '
        'cpu_time_qos_user_interactive billed_system_time serviced_system_time logical_writes '
        'lifetime_max_phys_footprint instructions cycles billed_energy serviced_energy '
        'interval_max_phys_footprint runnable_time').split()]


class VMStats(C.Structure):
    _fields_ = ([(n, U32) for n in 'free_count active_count inactive_count wire_count'.split()]
                + [(n, U64) for n in 'zero_fill_count reactivations pageins pageouts faults cow_faults lookups hits purges'.split()]
                + [(n, U32) for n in 'purgeable_count speculative_count'.split()]
                + [(n, U64) for n in 'decompressions compressions swapins swapouts'.split()]
                + [(n, U32) for n in 'compressor_page_count throttled_count external_page_count internal_page_count'.split()]
                + [('total_uncompressed_pages_in_compressor', U64)])


class TaskVM(C.Structure):
    # Mach structs use 4-byte packing, including the 64-bit ledger after decompressions.
    _pack_ = 4
    _fields_ = [('virtual_size', U64), ('region_count', I32), ('page_size', I32)] + [
        (n, I64 if n.startswith('ledger_') else U64) for n in ('resident_size resident_size_peak device device_peak internal internal_peak '
        'external external_peak reusable reusable_peak purgeable_volatile_pmap purgeable_volatile_resident '
        'purgeable_volatile_virtual compressed compressed_peak compressed_lifetime phys_footprint '
        'min_address max_address ledger_phys_footprint_peak ledger_purgeable_nonvolatile '
        'ledger_purgeable_novolatile_compressed ledger_purgeable_volatile ledger_purgeable_volatile_compressed '
        'ledger_tag_network_nonvolatile ledger_tag_network_nonvolatile_compressed ledger_tag_network_volatile '
        'ledger_tag_network_volatile_compressed ledger_tag_media_footprint ledger_tag_media_footprint_compressed '
        'ledger_tag_media_nofootprint ledger_tag_media_nofootprint_compressed ledger_tag_graphics_footprint '
        'ledger_tag_graphics_footprint_compressed ledger_tag_graphics_nofootprint ledger_tag_graphics_nofootprint_compressed '
        'ledger_tag_neural_footprint ledger_tag_neural_footprint_compressed ledger_tag_neural_nofootprint '
        'ledger_tag_neural_nofootprint_compressed limit_bytes_remaining').split()
    ] + [('decompressions', I32), ('ledger_swapins', I64),
         ('ledger_tag_neural_nofootprint_total', I64), ('ledger_tag_neural_nofootprint_peak', I64)]


class SwapUsage(C.Structure):
    _fields_ = [('total', U64), ('avail', U64), ('used', U64), ('pagesize', U32), ('encrypted', I32)]


def bind(lib, name, restype, *argtypes):
    f = getattr(lib, name)
    f.restype, f.argtypes = restype, argtypes
    return f


class Darwin:
    def __init__(self):
        if sys.platform != 'darwin':
            raise NotImplementedError('macos_only')
        self.lib = C.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        self.sysctlbyname = bind(self.lib, 'sysctlbyname', I32, C.c_char_p, C.c_void_p,
                                C.POINTER(C.c_size_t), C.c_void_p, C.c_size_t)
        self.sysctl = bind(self.lib, 'sysctl', I32, C.POINTER(I32), U32, C.c_void_p,
                           C.POINTER(C.c_size_t), C.c_void_p, C.c_size_t)
        self.host = bind(self.lib, 'mach_host_self', U32)
        self.task = U32.in_dll(self.lib, 'mach_task_self_').value
        self.deallocate = bind(self.lib, 'mach_vm_deallocate', I32, U32, U64, U64)
        self.port_deallocate = bind(self.lib, 'mach_port_deallocate', I32, U32, U32)
        self.absolute = bind(self.lib, 'mach_absolute_time', U64)
        self.continuous = bind(self.lib, 'mach_continuous_time', U64)
        info = Timebase()
        self.check(bind(self.lib, 'mach_timebase_info', I32, C.POINTER(Timebase))(C.byref(info)))
        self.numer, self.denom = info.numer, info.denom
        self.rusage = bind(self.lib, 'proc_pid_rusage', I32, I32, I32, C.c_void_p)
        self.listpids = bind(self.lib, 'proc_listpids', I32, U32, U32, C.c_void_p, I32)
        self.pidpath = bind(self.lib, 'proc_pidpath', I32, I32, C.c_void_p, U32)

    @staticmethod
    def check(result):
        if result != 0:
            code = C.get_errno() or errno.EIO
            raise OSError(code, 'native_read_failed')

    def value(self, name, kind=I32):
        value, size = kind(), C.c_size_t(C.sizeof(kind))
        self.check(self.sysctlbyname(name.encode(), C.byref(value), C.byref(size), None, 0))
        if size.value != C.sizeof(kind):
            raise OSError(errno.EIO, 'native_size_mismatch')
        return value.value if hasattr(value, 'value') else value

    def bytes(self, name):
        size = C.c_size_t()
        self.check(self.sysctlbyname(name.encode(), None, C.byref(size), None, 0))
        if size.value > 65536:
            raise OSError(errno.EOVERFLOW, 'native_size_limit')
        buf = C.create_string_buffer(size.value)
        self.check(self.sysctlbyname(name.encode(), buf, C.byref(size), None, 0))
        return buf.raw[:size.value]

    def process(self, pid):
        value = RUsage4()
        self.check(self.rusage(pid, 4, C.byref(value)))
        return value

    def processes(self):
        n=self.listpids(PROC_UID_ONLY,os.getuid(),None,0)
        if n<=0 or n>16_000_000:raise OSError(errno.EIO,'process_list_failed')
        for _ in range(3):
            buf=(I32*(n//4+256))()
            got=self.listpids(PROC_UID_ONLY,os.getuid(),buf,C.sizeof(buf))
            if got<=0:raise OSError(errno.EIO,'process_list_failed')
            if got<C.sizeof(buf):return tuple(int(p) for p in buf[:got//4] if p>0)
            n=got*2
            if n>16_000_000:break
        raise OSError(errno.EOVERFLOW,'process_list_changed_too_quickly')

    def process_path(self, pid):
        buf = C.create_string_buffer(4096)
        if self.pidpath(pid, buf, len(buf)) <= 0:
            return None
        return os.fsdecode(buf.value)

    def task_memory(self, pid):
        port = U32()
        rc = bind(self.lib, 'task_name_for_pid', I32, U32, I32, C.POINTER(U32))(self.task, pid, C.byref(port))
        if rc:
            raise PermissionError('task_memory_denied')
        try:
            vm, count = TaskVM(), U32(C.sizeof(TaskVM) // 4)
            rc = bind(self.lib, 'task_info', I32, U32, I32, C.c_void_p, C.POINTER(U32))(
                port.value, 22, C.byref(vm), C.byref(count))
            if rc:
                raise PermissionError('task_memory_denied')
            return vm, count.value * 4
        finally:
            self.port_deallocate(self.task, port.value)
