"""Small CoreFoundation/IOKit reader. Properties never escape unfiltered."""
from __future__ import annotations

import ctypes as C
import plistlib
from .native import I32, U32, U64, bind

P = C.c_void_p


class IOKit:
    def __init__(self):
        self.io = C.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
        self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.release = bind(self.cf, 'CFRelease', None, P)
        self.object_release = bind(self.io, 'IOObjectRelease', I32, U32)
        self.next = bind(self.io, 'IOIteratorNext', U32, U32)
        self.string = bind(self.cf, 'CFStringCreateWithCString', P, P, C.c_char_p, U32)
        self.typeid = bind(self.cf, 'CFGetTypeID', C.c_ulong, P)
        self.data = bind(self.cf, 'CFPropertyListCreateData', P, P, P, I32, U32, P)
        self.data_len = bind(self.cf, 'CFDataGetLength', C.c_long, P)
        self.data_ptr = bind(self.cf, 'CFDataGetBytePtr', P, P)

    def decode(self, obj):
        if not obj:
            return None
        raw = self.data(None, obj, 200, 0, None)
        if not raw:
            return None
        try:
            size = self.data_len(raw)
            if size < 0 or size > 4 * 1024 * 1024:
                raise OSError('property_limit')
            return plistlib.loads(C.string_at(self.data_ptr(raw), size))
        finally:
            self.release(raw)

    def property(self, entry, key):
        key_ref = self.string(None, key.encode(), 0x08000100)
        try:
            obj = bind(self.io, 'IORegistryEntryCreateCFProperty', P, U32, P, P, U32)(entry, key_ref, None, 0)
            if not obj:
                return None
            try:
                return self.decode(obj)
            finally:
                self.release(obj)
        finally:
            self.release(key_ref)

    def objects(self, classname):
        match = bind(self.io, 'IOServiceMatching', P, C.c_char_p)(classname.encode())
        it = U32()
        if bind(self.io, 'IOServiceGetMatchingServices', I32, U32, P, C.POINTER(U32))(0, match, C.byref(it)):
            raise OSError('iokit_matching_failed')
        try:
            while entry := self.next(it.value):
                try:
                    yield entry
                finally:
                    self.object_release(entry)
        finally:
            self.object_release(it.value)

    def children(self, entry, recursive=False):
        it = U32()
        if recursive:
            rc = bind(self.io, 'IORegistryEntryCreateIterator', I32, U32, C.c_char_p, U32, C.POINTER(U32))(
                entry, b'IOService', 1, C.byref(it))
        else:
            rc = bind(self.io, 'IORegistryEntryGetChildIterator', I32, U32, C.c_char_p, C.POINTER(U32))(
                entry, b'IOService', C.byref(it))
        if rc:
            raise OSError('iokit_children_failed')
        try:
            while child := self.next(it.value):
                try:
                    yield child
                finally:
                    self.object_release(child)
        finally:
            self.object_release(it.value)

    def identity(self, entry):
        value = U64()
        if bind(self.io, 'IORegistryEntryGetRegistryEntryID', I32, U32, C.POINTER(U64))(entry, C.byref(value)):
            raise OSError('iokit_identity_failed')
        return value.value

    def power_status(self):
        obj = P()
        rc = bind(self.io, 'IOPMCopyCPUPowerStatus', I32, C.POINTER(P))(C.byref(obj))
        if rc:
            raise OSError('power_status_failed')
        try:
            return self.decode(obj) or {}
        finally:
            if obj:
                self.release(obj)

    def power_supply(self):
        info=bind(self.io,'IOPSCopyPowerSourcesInfo',P)()
        if not info:raise OSError('power_sources_failed')
        try:
            value=bind(self.io,'IOPSGetProvidingPowerSourceType',P,P)(info)
            return self.decode(value)
        finally:self.release(info)

    def battery(self):
        info = bind(self.io, 'IOPSCopyPowerSourcesInfo', P)()
        if not info:
            raise OSError('power_sources_failed')
        try:
            sources = bind(self.io, 'IOPSCopyPowerSourcesList', P, P)(info)
            if not sources:
                raise OSError('power_sources_failed')
            try:
                count = bind(self.cf, 'CFArrayGetCount', C.c_long, P)(sources)
                at = bind(self.cf, 'CFArrayGetValueAtIndex', P, P, C.c_long)
                describe = bind(self.io, 'IOPSGetPowerSourceDescription', P, P, P)
                for i in range(count):
                    d = self.decode(describe(info, at(sources, i))) or {}
                    if d.get('Type') == 'InternalBattery':
                        return d
                return None
            finally:
                self.release(sources)
        finally:
            self.release(info)
