"""The Linux map on a synthetic root: mountinfo and /proc/self are written as the kernel
formats them (proc(5)); inotify events are packed as inotify(7) lays them out."""
import os
from pathlib import Path
import re
import struct
import tempfile
import unittest
from unittest.mock import patch

from machine_audit.budget import Budget
from machine_audit.scanner.accounting import Blocks
from machine_audit.scanner.linux import run_scan
from machine_audit.scanner.native import Volume
from machine_audit.scanner.store import MapQuery, MapStore, hot_set
from machine_audit.scanner.walker import Node
from machine_audit.sources.linux.watcher import (DIRTY, IN_CREATE, IN_DELETE_SELF, IN_IGNORED, IN_Q_OVERFLOW,
                                                 changes, record_dirty)

SCRATCH = Path(__file__).parent / '.tmp'
SCRATCH.mkdir(exist_ok=True)


def event(wd, mask, name=b''):
    padded = name.ljust((len(name) + 4) // 4 * 4, b'\0') if name else b''
    return struct.pack('=iIII', wd, mask, 0, len(padded)) + padded


class LinuxScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.tmp.name) / 'root'
        self.data = self.root / 'var/lib/monitor/data'
        self.data.mkdir(parents=True)
        device = os.stat(self.root).st_dev
        same = f'{os.major(device)}:{os.minor(device)}'
        files = {
            # /bound mounts part of the root filesystem a second time.
            'proc/self/mountinfo': f'22 1 {same} / / rw,relatime shared:1 - ext4 /dev/sda2 rw\n'
                                   f'27 22 {same} /srv /bound rw,relatime shared:1 - ext4 /dev/sda2 rw\n'
                                   '25 22 0:21 / /proc rw,nosuid shared:4 - proc proc rw\n',
            'proc/self/stat': '4242 (python3) S 1 4242 4242 0 -1 4194560 1200 0 3 0 50 10 0 0 20 0 1 0 5000 '
                              + ' '.join(['0'] * 30) + '\n',
            'proc/self/io': 'rchar: 1\nwchar: 2\nsyscr: 3\nsyscw: 4\nread_bytes: 0\nwrite_bytes: 8192\ncancelled_write_bytes: 0\n',
            'a/file': 'x' * 8192, 'srv/kept': 'k' * 4096, 'bound/kept': 'k' * 4096, 'tmp/scratch': 's' * 4096,
        }
        for name, text in files.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(text)

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self):
        with patch('machine_audit.scanner.linux.os.nice'):
            return run_scan(self.data, root=str(self.root))

    def test_nightly_walk_skips_system_areas_and_a_second_mount_of_the_same_filesystem(self):
        result = self.scan()
        self.assertEqual(result['status'], 'observed')
        volume = result['volumes'][0]['volume_id']
        self.assertRegex(volume, r'\A[0-9a-f]{16}\Z')  # the salted id the capacity source uses
        rows = MapQuery(self.data / 'scanner.sqlite').query('children', volume_id=volume)['rows']
        named = {r['label']: r for r in rows}
        self.assertEqual((named['bound']['status'], named['bound']['listed']), ('bind_mount', 0))
        self.assertEqual(named['srv']['status'], 'observed')
        # /proc and /tmp are excluded by default and, like configured exclusions, stay unnamed.
        self.assertEqual([(r['label'], r['listed']) for r in rows if r['status'] == 'excluded'], [(None, 0), (None, 0)])
        self.assertEqual(self.scan()['volumes'][0]['status'], 'current')  # the same night, nothing changed

    def test_watched_change_is_relisted_by_the_next_run_without_a_walk(self):
        volume = self.scan()['volumes'][0]['volume_id']
        (self.root / 'a/new').write_text('n' * 4096)
        record_dirty(self.data, Budget(self.data), {volume: {str(os.stat(self.root / 'a').st_ino)}})
        dirty = (self.data / DIRTY)
        self.assertEqual(dirty.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(re.search(r'[A-Za-z/]', dirty.read_text().replace('version', '').replace('volumes', '')
                                    .replace(volume, '')))  # numbers only: no names reach the file
        result = self.scan()['volumes'][0]
        self.assertEqual((result['action'], result['entries']), ('update', 2))  # a/file and a/new only
        query = MapQuery(self.data / 'scanner.sqlite')
        a = next(r for r in query.query('children', volume_id=volume)['rows'] if r['label'] == 'a')
        self.assertIn('new', {r['label'] for r in query.query('children', volume_id=volume, parent_id=a['id'])['rows']})
        self.assertEqual(list(self.data.glob(DIRTY + '*')), [])  # claimed and consumed

    def test_hot_set_is_largest_named_folders_and_events_dirty_folder_parent_or_all(self):
        tree = Path(self.tmp.name) / 'tree'
        store = MapStore(self.data / 'scanner.sqlite', Budget(self.data, free_margin_bytes=0))
        volume = Volume('v', 'Volume', str(tree), 'ext4', 1, 10**13, 1, 1, None)
        store.prepare(volume, lean=False)
        store.directory(Node('1', None, 'Volume', 'directory', 0, 0, 'observed', str(tree), 0))
        store.directory(Node('2', '1', 'big', 'directory', 2 * 10**9, 0, 'observed', str(tree / 'big'), 0))
        store.directory(Node('3', '1', 'media', 'directory', 0, 0, 'observed', str(tree / 'media'), 0))
        store.file('4', '3', 'film', 2 * 10**8, 0)
        store.directory(Node('5', '1', None, 'private', 3 * 10**9, 0, 'observed', None, 0))
        store.finish_volume(volume, Blocks(), [], full=True, lean=False)
        store.close()
        rows = hot_set(self.data / 'scanner.sqlite', 10)
        self.assertEqual(rows, [('v', '1', None, str(tree)), ('v', '2', '1', str(tree / 'big')),
                                ('v', '3', '1', str(tree / 'media'))])  # the private folder has no path
        self.assertEqual(len(hot_set(self.data / 'scanner.sqlite', 2)), 2)
        watches = {1: ('v', '2', '1'), 2: ('v', '3', '1')}
        found, overflowed, ended = changes(event(1, IN_CREATE, b'name') + event(2, IN_DELETE_SELF) + event(2, IN_IGNORED), watches)
        self.assertEqual((found, overflowed, ended), ({'v': {'2', '1'}}, False, {2}))
        found, overflowed, _ = changes(event(-1, IN_Q_OVERFLOW), watches)
        self.assertEqual((found, overflowed), ({'v': {'2', '3'}}, True))


if __name__ == '__main__':
    unittest.main()
