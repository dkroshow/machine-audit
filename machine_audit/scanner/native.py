"""macOS volume metadata and event-only shared-extent reads."""
from __future__ import annotations
import ctypes as C
import os
import stat
import struct
import uuid
from dataclasses import dataclass
from machine_audit.sources.native import I32, U32, U64, I64, bind


class Attr(C.Structure):
    _fields_ = [('count', C.c_ushort), ('reserved', C.c_ushort), ('common', U32),
                ('volume', U32), ('directory', U32), ('file', U32), ('fork', U32)]


class StatFS(C.Structure):
    _fields_ = [('bsize', U32), ('iosize', I32)] + [(n, U64) for n in 'blocks bfree bavail files ffree'.split()] + [
        ('fsid', I32 * 2), ('owner', U32), ('type', U32), ('flags', U32), ('subtype', U32),
        ('fstype', C.c_char * 16), ('mount', C.c_char * 1024), ('device_name', C.c_char * 1024),
        ('flags_ext', U32), ('reserved', U32 * 7)]


class Log2Phys(C.Structure):
    _pack_ = 4
    _fields_ = [('flags', U32), ('length', I64), ('offset', I64)]


@dataclass(frozen=True)
class Volume:
    id: str
    label: str
    mount: str
    fs: str
    device: int
    capacity: int
    free: int
    available: int
    used: int | None
    external: bool = False


class MacFilesystem:
    def __init__(self, native):
        self.native = native
        self.attrs = bind(native.lib, 'getattrlist', I32, C.c_char_p, C.POINTER(Attr), C.c_void_p, C.c_size_t, C.c_ulong)
        self.at = bind(native.lib, 'getattrlistat', I32, I32, C.c_char_p, C.POINTER(Attr), C.c_void_p, C.c_size_t, C.c_ulong)
        self.fcntl = bind(native.lib, 'fcntl', I32, I32, I32, C.c_void_p)

    def volumes(self):
        pointer = C.POINTER(StatFS)()
        # Intel's unsuffixed symbol returns the legacy, smaller statfs layout.
        count = bind(self.native.lib, 'getmntinfo$INODE64', I32, C.POINTER(C.POINTER(StatFS)), I32)(C.byref(pointer), 2)
        for i in range(count):
            item = pointer[i]
            mount = os.fsdecode(item.mount)
            fs = item.fstype.decode('ascii', 'replace')
            if not item.flags & 0x1000:  # MNT_LOCAL; never network mounts
                continue
            external = mount.startswith('/Volumes/') and '/' not in mount[9:]
            if mount != '/System/Volumes/Data' and not external:
                continue
            request = Attr(count=5, common=0x80000000, volume=0x80000000 | 0x00040000 | 0x00800000)
            buf = C.create_string_buffer(64)
            rc = self.attrs(os.fsencode(mount), C.byref(request), buf, len(buf), 0)
            if rc:
                continue
            length = struct.unpack_from('=I', buf.raw)[0]
            if not 24 <= length <= len(buf):
                continue
            returned = struct.unpack_from('=5I', buf.raw, 4)
            if not returned[1] & 0x40000:
                continue
            # Darwin packs SPACEUSED before UUID, despite their bitmap order.
            at, used = 24, None
            if returned[1] & 0x800000:
                if length < at + 8:
                    continue
                used = struct.unpack_from('=q', buf.raw, at)[0]
                at += 8
            if length < at + 16:
                continue
            identity = str(uuid.UUID(bytes=buf.raw[at:at+16]))
            yield Volume(identity, 'Startup data' if not external else os.path.basename(mount), mount, fs,
                         item.fsid[0], item.blocks * item.bsize, item.bfree * item.bsize,
                         item.bavail * item.bsize, used, external)

    def sharing(self, parent_fd, name, st):
        request = Attr(count=5, common=0x80000000, fork=0x200)
        buf = C.create_string_buffer(64)
        self.native.check(self.at(parent_fd, os.fsencode(name), C.byref(request), buf, len(buf), 0x21))
        attrs = struct.unpack_from('=5I', buf.raw, 4)
        if not attrs[4] & 0x200:
            raise OSError('extended_flags_unavailable')
        flags = struct.unpack_from('=Q', buf.raw, 24)[0]
        if not flags & 1:
            return None
        request.fork = 0x8 | 0x100 | 0x1000
        self.native.check(self.at(parent_fd, os.fsencode(name), C.byref(request), buf, len(buf), 0x21))
        attrs = struct.unpack_from('=5I', buf.raw, 4)
        at = 24
        private = None
        if attrs[4] & 8:
            private = struct.unpack_from('=q', buf.raw, at)[0]
            at += 8
        if not attrs[4] & 0x100:
            raise OSError('clone_identity_unavailable')
        clone = struct.unpack_from('=Q', buf.raw, at)[0]
        at += 8
        refs = struct.unpack_from('=I', buf.raw, at)[0] if attrs[4] & 0x1000 else None
        partial = refs is not None and refs < 2 and private is not None and private < st.st_blocks * 512
        return clone if not partial else -st.st_ino, private, partial

    def extents(self, parent_fd, name, st):
        flags = os.O_RDONLY | 0x8000 | 0x20000000 | os.O_CLOEXEC
        # Compressed files keep their physical data in the resource fork.
        path = name + '/..namedfork/rsrc' if getattr(st, 'st_flags', 0) & 0x20 else name
        fd = os.open(path, flags, dir_fd=parent_fd)
        try:
            current = os.fstat(fd)
            if (current.st_dev, current.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISREG(current.st_mode):
                raise OSError('file_changed_during_read')
            position = 0
            while position < current.st_size:
                info = Log2Phys(length=current.st_size-position, offset=position)
                self.native.check(self.fcntl(fd, 65, C.byref(info)))
                if not 0 < info.length <= current.st_size-position:
                    raise OSError('invalid_extent')
                if info.offset >= 0:
                    # Convert to allocated block boundaries, as the reference scanner does.
                    yield info.offset // 4096 * 4096, (info.length + 4095) // 4096 * 4096
                position += info.length
        finally:
            os.close(fd)
