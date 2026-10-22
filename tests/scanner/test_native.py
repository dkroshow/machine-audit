"""Scrubbed volume replies preserve the observed Darwin ABI and packing order."""
import ctypes as C
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import uuid

from machine_audit.scanner.native import MacFilesystem, StatFS

UUID = uuid.UUID('00112233-4455-6677-8899-aabbccddeeff')
RETURNED, VOL_UUID, VOL_USED = 0x80000000, 0x40000, 0x800000


def reply(mask, values):
    return struct.pack('=6I', 24 + len(values), RETURNED, mask, 0, 0, 0) + values


class VolumeNativeTests(unittest.TestCase):
    def filesystem(self, payload, *, reject_legacy=False):
        entries = (StatFS * 1)()
        entry = entries[0]
        entry.mount, entry.fstype, entry.flags = b'/System/Volumes/Data', b'apfs', 0x1000
        entry.bsize, entry.blocks, entry.bfree, entry.bavail = 4096, 1000, 10, 9
        entry.fsid[0] = 42
        def mounts(output, flags):
            self.assertEqual(flags, 2)  # MNT_NOWAIT
            C.cast(output, C.POINTER(C.POINTER(StatFS)))[0] = C.cast(entries, C.POINTER(StatFS))
            return 1
        def legacy(*args):
            self.fail('unsuffixed getmntinfo returns the legacy statfs ABI on Intel')
        def attrs(path, request, buffer, size, options):
            self.assertEqual(path, b'/System/Volumes/Data')
            C.memmove(buffer, payload, len(payload))
            return 0
        lib = SimpleNamespace(getattrlist=attrs, getattrlistat=Mock(), fcntl=Mock(),
                              getmntinfo=legacy if reject_legacy else mounts)
        setattr(lib, 'getmntinfo$INODE64', mounts)
        return MacFilesystem(SimpleNamespace(lib=lib))

    def test_mount_symbol_matches_sdk_statfs_layout(self):
        # Compiled sys/mount.h on the Intel target: modern layout + $INODE64.
        self.assertEqual((C.sizeof(StatFS), StatFS.flags.offset, StatFS.mount.offset), (2168, 64, 88))
        fs = self.filesystem(reply(VOL_UUID, UUID.bytes), reject_legacy=True)
        volume, = fs.volumes()
        self.assertEqual((volume.capacity, volume.free, volume.available), (4096000, 40960, 36864))

    def test_used_precedes_uuid_and_missing_or_short_values_stay_unknown(self):
        # Real separate/combined getattrlist probes established this packing;
        # UUID and size are scrubbed, not copied from the machine.
        cases = [(reply(VOL_USED | VOL_UUID, struct.pack('=q', 1234567) + UUID.bytes), 1234567),
                 (reply(VOL_UUID, UUID.bytes), None)]
        for packet, used in cases:
            with self.subTest(used=used):
                volume, = self.filesystem(packet).volumes()
                self.assertEqual(volume.id, str(UUID))
                self.assertEqual(volume.used, used)
        for packet in (reply(VOL_USED, struct.pack('=q', 1234567)),
                       reply(VOL_USED | VOL_UUID, struct.pack('=q', 1234567))):
            with self.subTest(packet_length=len(packet)):
                self.assertEqual(list(self.filesystem(packet).volumes()), [])


if __name__ == '__main__':
    unittest.main()
