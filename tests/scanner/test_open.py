"""Exercise Darwin's real directory-open flags only on disposable directories."""
import os
from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from machine_audit.budget import Budget
from machine_audit.scanner.accounting import Blocks
from machine_audit.scanner.events import Replay
from machine_audit.scanner.native import Volume
from machine_audit.scanner.policy import Policy
from machine_audit.scanner.runner import run_scan
from machine_audit.scanner.store import MapQuery, MapStore
from machine_audit.scanner.walker import Node, Walker

SCRATCH = Path(__file__).parent / '.tmp'
SCRATCH.mkdir(exist_ok=True)


@unittest.skipUnless(sys.platform == 'darwin', 'Darwin O_NOFOLLOW_ANY boundary')
class DarwinDirectoryOpenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'root'
        self.root.mkdir()
        (self.root / 'child').mkdir()

    def walker(self):
        volume = Volume('synthetic', 'Synthetic', str(self.root), 'hfs', 0, 100, 50, 50, 50)
        # Empty directories need no sharing calls; the sentinel selects native
        # open flags while real os.open/fstat/scandir exercise the boundary.
        return Walker(volume, Policy(home=str(self.base), homes=[]), Mock(), Blocks(), fs=object())

    def test_native_flags_open_and_list_a_real_tiny_tree(self):
        walker = self.walker()
        walker.walk()
        self.assertEqual(walker.gaps, set())
        self.assertEqual(walker.entries, 1)
        self.assertEqual(walker.listed, {str(self.root.stat().st_ino), str((self.root / 'child').stat().st_ino)})
        self.assertEqual(walker.sink.directory.call_count, 2)

    def test_native_flags_refuse_intermediate_and_final_symlinks(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        walker = self.walker()
        walker.walk(str(alias / 'child'))
        self.assertEqual(walker.entries, 0)
        self.assertEqual(walker.listed, set())
        self.assertIn('directory_unreadable', walker.gaps)
        walker.sink.begin_directory.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'scan_root_must_be_directory'):
            self.walker().walk(str(alias))

    def test_opened_inode_must_match_the_preopen_stat(self):
        other = self.base / 'other'
        other.mkdir()
        original_open = os.open
        def raced_open(path, flags):
            return original_open(other if path == str(self.root) else path, flags)
        walker = self.walker()
        with patch('machine_audit.scanner.walker.os.open', side_effect=raced_open):
            walker.walk()
        self.assertEqual(walker.gaps, {'directory_moved'})
        self.assertEqual(walker.entries, 0)
        walker.sink.begin_directory.assert_not_called()


@unittest.skipUnless(sys.platform == 'darwin', 'Darwin O_NOFOLLOW_ANY boundary')
class RootPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.data = self.base / 'data'
        self.data.mkdir()
        self.tree = self.base / 'tree'
        self.tree.mkdir()
        (self.tree / 'child').mkdir()
        self.volume = Volume('v', 'Volume', str(self.tree), 'hfs', 1,
                             100 * 10**9, 50 * 10**9, 50 * 10**9, 40 * 10**9)
        store = MapStore(self.data / 'scanner.sqlite', Budget(self.data, free_margin_bytes=0))
        store.prepare(self.volume)
        store.directory(Node('1', None, 'Root', 'directory', 0, 0, 'observed', None, 0))
        store.directory(Node('2', '1', 'Big folder', 'directory', 2 * 10**9, 0, 'observed', None, 0))
        with patch('machine_audit.scanner.store.time.time_ns', return_value=100 * 10**9):
            store.finish_volume(self.volume, Blocks(), [], full=True, lean=True,
                                bookmark=10, event_uuid='events')
        store.close()

    def published(self):
        with sqlite3.connect(self.data / 'scanner.sqlite') as db:
            volume = db.execute("SELECT * FROM volumes WHERE id='v'").fetchone()
            generation = db.execute("SELECT generation FROM volumes WHERE id='v'").fetchone()[0]
            nodes = db.execute('SELECT * FROM nodes WHERE generation=? ORDER BY id', (generation,)).fetchall()
            points = db.execute("SELECT * FROM points WHERE volume='v' ORDER BY id,observed_ns").fetchall()
            epochs = db.execute("SELECT * FROM hierarchy_epochs WHERE volume='v' ORDER BY observed_ns").fetchall()
            hierarchy = db.execute("SELECT * FROM hierarchy_nodes WHERE volume='v' ORDER BY version,id").fetchall()
            state = db.execute('SELECT status FROM generations WHERE id=?', (generation,)).fetchone()
        children = MapQuery(self.data / 'scanner.sqlite').query('children', volume_id='v')
        return volume, nodes, points, epochs, hierarchy, state, children

    def scan(self, volumes, policy=None, events=None):
        native = SimpleNamespace(process=lambda pid: SimpleNamespace(proc_start_abstime=1, diskio_byteswritten=0))
        filesystem = SimpleNamespace(volumes=lambda: volumes, sharing=lambda *args: None)
        events = events or SimpleNamespace(uuid=lambda device: 'events', current_id=lambda: 20)
        with patch('machine_audit.scanner.runner.Darwin', return_value=native), \
             patch('machine_audit.scanner.runner.MacFilesystem', return_value=filesystem), \
             patch('machine_audit.scanner.runner.IOKit'), \
             patch('machine_audit.scanner.runner.FSEvents', return_value=events), \
             patch('machine_audit.scanner.runner.os.nice'), \
             patch('machine_audit.scanner.runner.Policy', return_value=policy or Policy(home=str(self.base), homes=[])):
            result = run_scan(self.data, full=True)
        with sqlite3.connect(self.data / 'scanner.sqlite') as db:
            status, ended = db.execute('SELECT status,ended_ns FROM runs ORDER BY started_ns DESC LIMIT 1').fetchone()
        self.assertIsNotNone(ended)
        return result, status

    def test_unreadable_roots_keep_generation_children_points_hierarchy_and_bookmark(self):
        before = self.published()
        alias = self.base / 'alias'
        alias.symlink_to(self.tree, target_is_directory=True)
        original_open = os.open
        def deny_root(path, flags, *args, **kwargs):
            if str(path) == str(self.tree):
                raise PermissionError(13, 'synthetic root denial')
            return original_open(path, flags, *args, **kwargs)
        for mode, volume in [('missing', replace(self.volume, mount=str(self.base / 'missing'))),
                             ('symlink', replace(self.volume, mount=str(alias / 'child'))),
                             ('denied', self.volume)]:
            with self.subTest(mode=mode), patch('machine_audit.scanner.walker.os.open',
                                               side_effect=deny_root if mode == 'denied' else original_open):
                if mode == 'denied':
                    sink = Mock()
                    walker = Walker(volume, Policy(home=str(self.base), homes=[]), sink, Blocks(), fs=object())
                    walker.walk()
                    self.assertEqual(walker.listed, set())
                    self.assertEqual(sink.directory.call_args.args[0].status, 'denied')
                result, status = self.scan([volume])
                self.assertEqual(self.published(), before)
                self.assertEqual((result['status'], status), ('root_unreadable', 'root_unreadable'))
                self.assertEqual(result['volumes'][0]['status'], 'root_unreadable')
                self.assertTrue(result['volumes'][0]['gaps'])

    def test_readable_empty_root_still_publishes_with_zero_entries(self):
        before = self.published()
        empty = self.base / 'empty'
        empty.mkdir()
        result, status = self.scan([replace(self.volume, mount=str(empty))])
        self.assertEqual((result['status'], status, result['entries']), ('observed', 'complete', 0))
        self.assertNotEqual(self.published()[0], before[0])
        self.assertEqual(self.published()[6]['rows'], [])
        with sqlite3.connect(self.data / 'scanner.sqlite') as db:
            self.assertEqual(db.execute('SELECT bookmark FROM volumes').fetchone()[0], 20)

    def test_unexpected_publication_error_ends_run_without_exposing_exception_text(self):
        before = self.published()
        for error, reason in ((ValueError('synthetic private path'), 'invalid_scan_state'),
                              (OSError(5, 'synthetic private path'), 'os_error'),
                              (TypeError('synthetic private path'), 'unexpected_error')):
            with self.subTest(reason=reason), patch.object(MapStore, 'finish_volume', side_effect=error):
                result, status = self.scan([self.volume])
                self.assertEqual(result['status'], 'scan_failed')
                self.assertEqual(result['reason'], reason)
                self.assertEqual(status, reason)
                self.assertNotIn('synthetic private path', repr(result))
                # Failure status may change; published evidence cannot.
                after = self.published()
                self.assertEqual(after[1:6], before[1:6])
                self.assertEqual(after[6]['rows'], before[6]['rows'])

    def test_failed_volume_does_not_prevent_another_empty_root_publication(self):
        before = self.published()
        empty = self.base / 'empty'
        empty.mkdir()
        other = replace(self.volume, id='other', mount=str(empty))
        original_open = os.open
        def deny_root(path, flags, *args, **kwargs):
            if str(path) == str(self.tree):
                raise PermissionError(13, 'synthetic root denial')
            return original_open(path, flags, *args, **kwargs)
        with patch('machine_audit.scanner.walker.os.open', side_effect=deny_root):
            result, status = self.scan([self.volume, other])
        self.assertEqual(self.published(), before)
        self.assertEqual((result['status'], status), ('partial', 'partial'))
        self.assertEqual([v['status'] for v in result['volumes']], ['root_unreadable', 'observed'])
        with sqlite3.connect(self.data / 'scanner.sqlite') as db:
            self.assertIsNotNone(db.execute("SELECT generation FROM volumes WHERE id='other'").fetchone()[0])

    def legacy_empty_map(self, at):
        # Reproduce the old publication bug, including its retained history.
        store = MapStore(self.data / 'scanner.sqlite', Budget(self.data, free_margin_bytes=0))
        store.prepare(self.volume)
        with patch('machine_audit.scanner.store.time.time_ns', return_value=at):
            store.finish_volume(self.volume, Blocks(), ['directory_unreadable'],
                                full=True, lean=True, bookmark=10, event_uuid='events')
        store.close()
        self.assertEqual(self.published()[1], [])

    def test_recent_legacy_empty_generation_does_not_throttle_full_retry(self):
        at = 200 * 10**9
        self.legacy_empty_map(at)
        before = self.published()
        with patch('machine_audit.scanner.runner.time.time_ns', return_value=at + 10 * 10**9):
            result, status = self.scan([self.volume])
        self.assertEqual((result['status'], status), ('observed', 'complete'))
        self.assertEqual(result['volumes'][0].get('action'), 'full')
        self.assertEqual(result['entries'], 1)
        after = self.published()
        self.assertEqual(len(after[1]), 2)
        self.assertTrue(set(before[2]).issubset(after[2]))  # original size points
        self.assertTrue(set(before[3]).issubset(after[3]))  # original observation times
        self.assertTrue(set(before[4]).issubset(after[4]))  # original hierarchy
        with sqlite3.connect(self.data / 'scanner.sqlite') as db:
            self.assertEqual(db.execute("SELECT full_ns FROM volumes WHERE id='v'").fetchone()[0], at + 10 * 10**9)

    def test_failed_retry_preserves_legacy_timestamps_and_history(self):
        at = 200 * 10**9
        self.legacy_empty_map(at)
        before = self.published()
        original_open = os.open
        def deny_root(path, flags, *args, **kwargs):
            if str(path) == str(self.tree):
                raise PermissionError(13, 'synthetic root denial')
            return original_open(path, flags, *args, **kwargs)
        with patch('machine_audit.scanner.walker.os.open', side_effect=deny_root), \
             patch('machine_audit.scanner.runner.time.time_ns', return_value=at + 10 * 10**9):
            result, status = self.scan([self.volume])
        self.assertEqual((result['status'], status), ('root_unreadable', 'root_unreadable'))
        self.assertEqual(self.published(), before)

    def test_changes_inside_exclusions_or_own_store_do_not_rewalk_their_parent(self):
        # Without the filter, either change re-walks the whole volume every hour.
        (self.tree / 'private' / 'inner').mkdir(parents=True)
        volume = replace(self.volume, id='w', mount=str(self.base))
        policy = Policy(home=str(self.base), homes=[], exclusions=[str(self.tree / 'private')], data_root=str(self.data))
        events = SimpleNamespace(uuid=lambda device: 'events', current_id=lambda: 20,
                                 replay=lambda device, since: Replay(paths={'/tree/private/inner/log': False,
                                                                            '/data/scanner.sqlite-wal': False}, up_to=30))
        self.scan([volume], policy, events)
        with patch('machine_audit.scanner.runner.time.time_ns', return_value=time.time_ns() + 2 * 3600 * 10**9):
            result, _ = self.scan([volume], policy, events)
        self.assertEqual((result['volumes'][0]['action'], result['volumes'][0]['entries']), ('update', 0))

    def test_populated_and_readable_empty_roots_keep_daily_full_limit(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                if empty:
                    directory = self.base / 'empty'
                    directory.mkdir()
                    self.volume = replace(self.volume, mount=str(directory))
                    store = MapStore(self.data / 'scanner.sqlite', Budget(self.data, free_margin_bytes=0))
                    store.prepare(self.volume)
                    walker = Walker(self.volume, Policy(home=str(self.base), homes=[]), store, Blocks(), fs=object())
                    walker.walk()
                    self.assertEqual(walker.entries, 0)
                    self.assertTrue(walker.listed)
                    with patch('machine_audit.scanner.store.time.time_ns', return_value=200_000 * 10**9):
                        store.finish_volume(self.volume, Blocks(), [], full=True, lean=True,
                                            bookmark=10, event_uuid='events')
                    store.close()
                with sqlite3.connect(self.data / 'scanner.sqlite') as db:
                    full_ns = db.execute("SELECT full_ns FROM volumes WHERE id='v'").fetchone()[0]
                before = self.published()
                with patch('machine_audit.scanner.runner.time.time_ns', return_value=full_ns + 10 * 10**9):
                    result, _ = self.scan([self.volume])
                self.assertEqual(result['volumes'][0]['status'], 'current')
                self.assertEqual(self.published(), before)
                with patch('machine_audit.scanner.runner.time.time_ns', return_value=full_ns + 86400 * 10**9):
                    result, _ = self.scan([self.volume])
                self.assertEqual(result['volumes'][0].get('action'), 'full')


if __name__ == '__main__':
    unittest.main()
