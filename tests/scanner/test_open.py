"""Exercise Darwin's real directory-open flags only on disposable directories."""
import os
from dataclasses import replace
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from machine_audit.budget import Budget, BudgetExceeded
from machine_audit.scanner.events import Replay
from machine_audit.scanner.errors import ScanError
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
        return Walker(volume, Policy(home=str(self.base), homes=[]), Mock(), fs=object())

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
        walker = self.walker()
        self.assertFalse(walker.walk(str(alias)))
        self.assertIn('root_not_directory', walker.gaps)
        walker.sink.begin_directory.assert_not_called()

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
            store.finish_volume(self.volume, None, [], full=True, lean=True,
                                bookmark=10, event_uuid='events')
        store.close()

    def published(self):
        with sqlite3.connect(self.data / 'scanner.sqlite') as db:
            cursor = db.execute("SELECT * FROM volumes WHERE id='v'")
            volume = dict(zip((column[0] for column in cursor.description),cursor.fetchone()))
            # Attempt status can change without changing the published map.
            volume.pop('status')
            volume.pop('full_attempt_ns')
            volume.pop('budget_retry_bytes')
            generation = db.execute("SELECT generation FROM volumes WHERE id='v'").fetchone()[0]
            nodes = db.execute('SELECT * FROM nodes WHERE generation=? ORDER BY id', (generation,)).fetchall()
            points = db.execute("SELECT * FROM points WHERE volume='v' ORDER BY id,observed_ns").fetchall()
            epochs = db.execute("SELECT * FROM hierarchy_epochs WHERE volume='v' ORDER BY observed_ns").fetchall()
            hierarchy = db.execute("SELECT * FROM hierarchy_nodes WHERE volume='v' ORDER BY version,id").fetchall()
            state = db.execute('SELECT status FROM generations WHERE id=?', (generation,)).fetchone()
        children = MapQuery(self.data / 'scanner.sqlite').query('children', volume_id='v')
        children.pop('status')  # Last attempt status is checked separately.
        return volume, nodes, points, epochs, hierarchy, state, children

    def scan(self, volumes, policy=None, events=None, force=False, full=True, limit=10**10):
        native = SimpleNamespace(process=lambda pid: SimpleNamespace(proc_start_abstime=1, diskio_byteswritten=0))
        filesystem = SimpleNamespace(volumes=lambda: volumes, sharing=lambda *args: None)
        events = events or SimpleNamespace(uuid=lambda device: 'events', current_id=lambda: 20)
        with patch('machine_audit.scanner.runner.Darwin', return_value=native), \
             patch('machine_audit.scanner.runner.MacFilesystem', return_value=filesystem), \
             patch('machine_audit.scanner.runner.IOKit'), \
             patch('machine_audit.scanner.runner.FSEvents', return_value=events), \
             patch('machine_audit.scanner.runner.os.nice'), \
             patch('machine_audit.scanner.runner.Budget', lambda root: Budget(root, limit_bytes=limit)), \
             patch('machine_audit.scanner.runner.Policy', return_value=policy or Policy(home=str(self.base), homes=[])):
            result = run_scan(self.data, full=full, force=force)
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
                    walker = Walker(volume, Policy(home=str(self.base), homes=[]), sink, fs=object())
                    walker.walk()
                    self.assertEqual(walker.listed, set())
                    self.assertEqual(sink.directory.call_args.args[0].status, 'denied')
                result, status = self.scan([volume],force=True)
                self.assertEqual(self.published(), before)
                self.assertEqual((result['status'], status), ('root_unreadable', 'root_unreadable'))
                self.assertEqual(result['volumes'][0]['status'], 'root_unreadable')
                self.assertTrue(result['volumes'][0]['gaps'])
                with sqlite3.connect(self.data/'scanner.sqlite') as db:
                    self.assertEqual(db.execute('SELECT status FROM volumes').fetchone()[0],result['volumes'][0]['reason'])

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
        for error, status_code, reason in (
                (ValueError('synthetic private path'), 'scan_failed', 'invalid_scan_state_ValueError'),
                (OSError(5, 'synthetic private path'), 'scan_failed', 'os_error_OSError'),
                (TypeError('synthetic private path'), 'scan_failed', 'unexpected_error_TypeError'),
                (sqlite3.OperationalError('synthetic private path'), 'write_failed', 'write_failed_OperationalError')):
            with self.subTest(reason=reason), patch.object(MapStore, 'finish_volume', side_effect=error):
                result, status = self.scan([self.volume],force=True)
                self.assertEqual(result['status'], status_code)
                self.assertEqual(result['reason'], reason)
                self.assertEqual(result['exception_type'], type(error).__name__)
                self.assertEqual(status, reason)
                self.assertNotIn('synthetic private path', repr(result))
                # Failure status may change; published evidence cannot.
                after = self.published()
                self.assertEqual(after[1:6], before[1:6])
                self.assertEqual(after[6]['rows'], before[6]['rows'])
        # A budget refusal ends only that volume's attempt and is recorded with it.
        with patch.object(MapStore, 'finish_volume', side_effect=BudgetExceeded('synthetic_rule')):
            result, status = self.scan([self.volume],force=True)
        self.assertEqual((result['status'], status, result['volumes'][0]['status']), ('budget_blocked_synthetic_rule',)*3)
        self.assertEqual(self.published()[1:6], before[1:6])

    def test_capacity_refusal_recovers_once_by_full_walk_then_waits_for_freed_space(self):
        # Big Mac retried one refused update hourly for five days, re-walking a
        # million entries each time, and the refusal stopped every other volume.
        other = self.base/'other';other.mkdir();(other/'file').write_bytes(b'other')
        second = replace(self.volume, id='w', mount=str(other), device=2)
        volumes = [self.volume, second]
        self.scan(volumes)  # Publish real maps for both volumes.
        store = MapStore(self.data/'scanner.sqlite', Budget(self.data, free_margin_bytes=0))
        with store.write(rows=2, payload_bytes=8*1024**2):  # Retained history: whole-file WAL bounds exceed the cap.
            store.db.execute('CREATE TABLE padding(value BLOB)')
            store.db.execute('INSERT INTO padding VALUES(zeroblob(?))', (8*1024**2,))
        store.close()
        collector = self.data/'synthetic-collector.sqlite'
        collector.write_bytes(bytes(16*1024**2))
        limit = Budget(self.data).usage()['bytes']+4*1024**2
        for i in range(3000):(self.tree/'child'/str(i)).write_bytes(b'x')
        events = SimpleNamespace(uuid=lambda device:'events', current_id=lambda:30,
            replay=lambda device,since:Replay(paths={'/child':False} if device==1 else {}, up_to=30))
        before = self.published()
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            start, hour = db.execute('SELECT max(observed_ns) FROM volumes').fetchone()[0], 3600*10**9
        def run(after, chosen=None, **kwargs):
            with patch('machine_audit.scanner.runner.time.time_ns', return_value=start+after):
                result, status = self.scan(chosen or volumes, events=events, limit=limit, **{'full':False, **kwargs})
            return status, {r['volume_id']:r for r in result['volumes']}, result['status']
        def volume(column):
            with sqlite3.connect(self.data/'scanner.sqlite') as db:
                return db.execute(f"SELECT {column} FROM volumes WHERE id='v'").fetchone()[0]

        status, results, outcome = run(hour)
        self.assertEqual(status, 'partial')
        self.assertIn(results['v']['status'], ('budget_blocked_sqlite_checkpoint_headroom','budget_blocked_managed_storage_limit'))
        self.assertGreaterEqual(results['v']['entries'], 3000, 'failed work must still be counted')
        self.assertEqual(results['w']['status'], 'observed')
        self.assertEqual(self.published()[:6], before[:6])
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            generation = db.execute("SELECT generation FROM volumes WHERE id='w'").fetchone()[0]
            self.assertEqual(db.execute('SELECT count(*) FROM files WHERE generation=?', (generation,)).fetchone()[0], 1,
                             'the refused volume\'s pending rows were published as another volume\'s work')
        status, results, outcome = run(2*hour)
        self.assertEqual((results['v']['action'], results['v']['trigger']), ('full', 'budget_recovery'))
        self.assertTrue(results['v']['status'].startswith('budget_blocked_'))
        level = volume('budget_retry_bytes')
        self.assertIsNotNone(level)
        day = 86400*10**9
        for after, full in ((3*hour, False), (day+3*hour, True)):  # Hourly and persistent full settings.
            status, results, outcome = run(after, full=full)
            self.assertEqual((results['v']['action'], results['v']['trigger']), ('wait', 'budget_retry_gate'))
            self.assertEqual((results['w']['status'], status, outcome), ('observed', 'partial', 'partial'))
        status, results, outcome = run(day+4*hour, force=True)  # The operator's explicit retry.
        self.assertEqual(results['v']['action'], 'full')
        self.assertTrue(results['v']['status'].startswith('budget_blocked_'))
        level = volume('budget_retry_bytes')
        usage = Budget(self.data).usage()
        with open(collector, 'r+b') as file:
            file.truncate(16*1024**2-(usage['bytes']+usage['checkpoint_reservation_bytes']-level)+65536)
        status, results, outcome = run(day+5*hour, chosen=[self.volume])
        self.assertEqual(results['v']['trigger'], 'budget_retry_gate')
        # A run whose only volume is held must not report success.
        self.assertEqual((status, outcome), (volume('status'),)*2)
        collector.unlink()  # Synthetic file created by this test.
        status, results, outcome = run(day+6*hour)
        self.assertEqual((status, results['v']['action'], results['v']['status']), ('complete', 'full', 'observed'))
        self.assertIsNone(volume('budget_retry_bytes'))
        status, results, outcome = run(day+7*hour)
        self.assertEqual((results['v']['action'], results['v']['trigger'], results['v']['status']),
                         ('update', 'hourly_update', 'observed'))

    def test_refused_preparation_is_recorded_on_its_own_volume(self):
        # A prune refused before preparation named its volume recorded the failure
        # and retry level on the previous volume, or on none for the first one.
        other = self.base/'other';other.mkdir()
        second = replace(self.volume, id='w', mount=str(other), device=2)
        self.scan([self.volume, second])
        events = SimpleNamespace(uuid=lambda device:'events', current_id=lambda:30,
                                 replay=lambda device,since:Replay(up_to=30))
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            start = db.execute('SELECT max(observed_ns) FROM volumes').fetchone()[0]
        original = MapStore.prune_generations
        def refuse_on(call):
            calls = []
            def prune(store, full=True, pace=None):
                calls.append(full)
                if len(calls)==call:
                    refused = BudgetExceeded('sqlite_checkpoint_headroom', 1)
                    refused.retry_bytes = 1
                    raise refused
                return original(store, full, pace)
            return prune
        def stored():
            with sqlite3.connect(self.data/'scanner.sqlite') as db:
                return dict((r[0], r[1:]) for r in db.execute('SELECT id,status,budget_retry_bytes FROM volumes'))
        blocked = ('budget_blocked_sqlite_checkpoint_headroom', 1)
        # Updates run before paced walks: in the second run the recovery walk of 'w'
        # prunes after 'v' prepares its update.
        for hours, order, call, expected in ((1, [self.volume, second], 2, {'v':('observed', None), 'w':blocked}),
                                             (2, [second, self.volume], 2, {'v':('observed', None), 'w':blocked})):
            with patch.object(MapStore, 'prune_generations', refuse_on(call)), \
                 patch('machine_audit.scanner.runner.time.time_ns', return_value=start+hours*3600*10**9):
                result, status = self.scan(order, events=events, full=False)
            self.assertEqual(stored(), expected)
        # The recovery full walk was refused before its walk; it still counts as a
        # failed full attempt, so the next scheduled run waits instead of repeating it.
        self.assertEqual({r['volume_id']:r['trigger'] for r in result['volumes']}['w'], 'budget_recovery')
        with patch('machine_audit.scanner.runner.time.time_ns', return_value=start+3*3600*10**9):
            result, status = self.scan([second], events=events, full=False)
        self.assertEqual((result['volumes'][0]['trigger'], status), ('budget_retry_gate', blocked[0]))

    def test_refused_publication_keeps_a_numeric_receipt_of_what_it_declared(self):
        # Big Mac's main update was refused at publication and its staging was pruned
        # by the next volume, so nothing could say which counted part was too large.
        other = self.base/'other';other.mkdir()
        second = replace(self.volume, id='w', mount=str(other), device=2)
        self.scan([self.volume, second])
        events = SimpleNamespace(uuid=lambda device:'events', current_id=lambda:30,
                                 replay=lambda device,since:Replay(paths={'/child':False} if device==1 else {}, up_to=30))
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            start = db.execute('SELECT max(observed_ns) FROM volumes').fetchone()[0]
        phase, finish = MapStore.phase, MapStore.finish_volume
        labels = {'dirty'}  # Map rows reach the base through the bounded merge, after publication.
        for hours, refused in ((1, 'publication'), (2, 'progress')):
            def refusing(store, stage, **kwargs):
                full = lambda:setattr(store.budget,'limit_bytes',store.budget.usage()['bytes'])  # nothing more fits
                if stage=='publication' and store.volume=='v' and refused=='progress':full()
                phase(store, stage, **kwargs)
                if stage=='publication' and store.volume=='v' and refused=='publication':full()
            def roomy(store, *args, **kwargs):
                try:return finish(store, *args, **kwargs)
                finally:store.budget.limit_bytes=10**10  # Room again for the failure record and the next volume.
            with self.subTest(refused), patch.object(MapStore,'phase',refusing), patch.object(MapStore,'finish_volume',roomy), \
                 patch('machine_audit.scanner.runner.time.time_ns', return_value=start+hours*3600*10**9):
                result, status = self.scan([self.volume, second], events=events, full=False)
            outcome = {r['volume_id']:r for r in result['volumes']}
            self.assertEqual((status, outcome['w']['status']), ('partial', 'observed'))
            self.assertTrue(outcome['v']['status'].startswith('budget_blocked_'))
            with sqlite3.connect(self.data/'scanner.sqlite') as db:
                stored = {r['volume_id']:r for r in json.loads(db.execute('SELECT volumes FROM runs ORDER BY started_ns DESC LIMIT 1').fetchone()[0])}
                staging = db.execute("SELECT count(*) FROM generations g JOIN volumes v ON v.key=g.volume WHERE v.id='v' AND g.status='building'").fetchone()[0]
            self.assertEqual(staging, 0, "the next volume's preparation prunes the refused staging; the receipt must not depend on it")
            if refused=='progress':
                self.assertNotIn('publication', stored['v'], 'a refused progress write was reported as the publication')
                continue
            receipt = stored['v']['publication']
            self.assertEqual(set(receipt), {'plan','changed','point_candidates','hierarchy_candidates','tracked','rows',
                                            'payload_bytes','existing_pages','freelist_pages','shortfall_bytes'})
            self.assertEqual(set(receipt['plan']), labels)
            numbers = [v for k,v in receipt.items() if k!='plan']+[n for pair in receipt['plan'].values() for n in pair]
            self.assertTrue(all(type(n) is int and n>=0 for n in numbers), 'the receipt holds counts and bytes only')
            self.assertGreater(receipt['changed'], 0)
            self.assertEqual(receipt['rows'], sum(n for n,_ in receipt['plan'].values())+2*receipt['point_candidates']
                             +receipt['hierarchy_candidates']+2*receipt['tracked']+5+1)  # +1: the delta's own status

    def test_a_refused_merge_keeps_its_publication_and_merges_before_any_scan(self):
        # After publication, a merge refusal must not mark the volume failed, set a retry
        # level or start a recovery walk. Each run merges first, and a volume whose merge
        # is still refused is not scanned while the others go on.
        other = self.base/'other';other.mkdir()
        second = replace(self.volume, id='w', mount=str(other), device=2)
        self.scan([self.volume, second])
        events = SimpleNamespace(uuid=lambda device:'events', current_id=lambda:30,
                                 replay=lambda device,since:Replay(paths={'/child':False} if device==1 else {}, up_to=30))
        (self.tree/'child'/'added').write_bytes(b'x')
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            start = db.execute('SELECT max(observed_ns) FROM volumes').fetchone()[0]
        phase, finish, merges = MapStore.phase, MapStore.finish_volume, MapStore.finish_merges
        refuse = {'v'}
        def refusing(store, stage, **kwargs):
            phase(store, stage, **kwargs)
            if stage=='merge' and (store.volume in refuse or None in refuse):
                store.budget.limit_bytes=store.budget.usage()['bytes']  # nothing more fits
        def roomy(method):
            def call(store, *args, **kwargs):
                try:return method(store, *args, **kwargs)
                finally:store.budget.limit_bytes=10**10
            return call
        def run(hours, volumes=(self.volume, second)):
            with patch.object(MapStore,'phase',refusing), patch.object(MapStore,'finish_volume',roomy(finish)), \
                 patch.object(MapStore,'finish_merges',roomy(merges)), \
                 patch('machine_audit.scanner.runner.time.time_ns', return_value=start+hours*3600*10**9):
                result, status = self.scan(list(volumes), events=events, full=False)
            with sqlite3.connect(self.data/'scanner.sqlite') as db:
                row = db.execute("SELECT status,delta_generation,observed_ns,bookmark,budget_retry_bytes FROM volumes WHERE id='v'").fetchone()
                history = [db.execute(f"SELECT count(*) FROM {t} WHERE volume='v'").fetchone()[0] for t in ('points','hierarchy_epochs')]
            return status, {r['volume_id']:r for r in result['volumes']}, row, history
        status, results, row, history = run(1)
        self.assertEqual((status, results['v']['status'], results['w']['status']), ('partial', 'observed', 'observed'))
        self.assertTrue(results['v']['merge'].startswith('blocked_'))
        published = row
        self.assertEqual((row[0], row[4]), ('observed', None))
        self.assertIsNotNone(row[1])
        refuse = {None}  # still refused at the next run's first step
        status, results, row, again = run(2)
        self.assertEqual((results['v']['action'], results['v']['trigger'], results['v']['status']), ('wait', 'merge_pending', 'observed'))
        self.assertEqual((status, results['w']['status'], row, again), ('partial', 'observed', published, history))
        # With only that volume to scan, nothing was read, yet its map stands: partial.
        status, results, row, again = run(2, [self.volume])
        self.assertEqual((status, results['v']['trigger'], row, again), ('partial', 'merge_pending', published, history))
        refuse = set()
        status, results, row, again = run(3)
        # The merge finishes first; this run's own hourly update then publishes normally.
        self.assertEqual((status, results['v']['status'], row[1]), ('complete', 'observed', None))

    def test_full_walk_refused_by_an_unknown_page_shortfall_waits_for_an_operator(self):
        # Running out of an admitted page limit was write_failed, then a one-byte
        # retry level: every small collector prune repeated the same failing walk.
        filler = self.data/'synthetic-collector.sqlite'
        filler.write_bytes(bytes(1024**2))
        limit = Budget(self.data).usage()['bytes']+3*1024**2
        walks = []
        def overflow(store, *args):
            walks.append(1)
            with store.write(rows=100_000):
                store.db.execute('CREATE TABLE fill(value BLOB)')
                store.db.execute('INSERT INTO fill VALUES(zeroblob(?))', (8*1024**2,))
        before = self.published()
        day, hour = 86400*10**9, 3600*10**9
        def scan(now, **kwargs):
            with patch('machine_audit.scanner.runner.time.time_ns', return_value=now):
                result, status = self.scan([self.volume], limit=limit, **{'full':False, **kwargs})
            return result['volumes'][0]['trigger'], status, result['status']
        blocked = 'budget_blocked_sqlite_page_limit'
        events = SimpleNamespace(uuid=lambda device:'events', current_id=lambda:30,
                                 replay=lambda device,since:Replay(up_to=30))
        minute = 60*10**9
        self.assertEqual(scan(8*day)[1:], ('complete', 'observed'))
        self.assertEqual(scan(9*day+hour, events=events), ('hourly_update', 'complete', 'observed'))
        before = self.published()
        self.assertEqual(scan(9*day+hour+5*minute), ('update_not_due', 'complete', 'observed'))  # Healthy and current.
        failed = 9*day+hour+10*minute
        with patch.object(MapStore, '_aggregate_full', overflow):
            # The operator's full walk, inside the hour after an update.
            self.assertEqual(scan(failed, force=True), ('requested_full', blocked, blocked))
            self.assertEqual(self.published()[1:6], before[1:6])
            # Not yet due either way, but blocked: never reported as current.
            self.assertEqual(scan(failed+10*minute), ('update_not_due', blocked, blocked))
            with sqlite3.connect(self.data/'scanner.sqlite') as db:
                volumes = db.execute('SELECT volumes FROM runs ORDER BY started_ns DESC LIMIT 1').fetchone()[0]
            self.assertEqual(json.loads(volumes)[0]['status'], blocked)
            # Each run is a fresh process. Hourly, persistent full inside and
            # after its daily cooldown, and small freed space all keep waiting
            # and never report success.
            for now, full, trigger in ((failed+hour, False, 'budget_retry_gate'),
                                       (failed+2*hour, True, 'failed_full_retry_cooldown'),
                                       (failed+day+3*hour, True, 'budget_retry_gate'),
                                       (failed+day+4*hour, False, 'budget_retry_gate')):
                with open(filler, 'r+b') as file:file.truncate(filler.stat().st_size-64*1024)
                self.assertEqual(scan(now, full=full), (trigger, blocked, blocked))
            self.assertEqual(len(walks), 1)
            self.assertEqual(scan(failed+day+5*hour, force=True), ('budget_recovery', blocked, blocked))
            self.assertEqual(len(walks), 2)
        self.assertEqual(scan(failed+day+6*hour, force=True)[1:], ('complete', 'observed'))
        self.assertEqual(scan(failed+2*day+7*hour, events=events), ('hourly_update', 'complete', 'observed'))

    def test_specific_invariant_and_chosen_action_survive_a_failed_run(self):
        def fail_aggregate(store, *args):
            store.path_for('absent synthetic identity')
        with patch.object(MapStore, '_aggregate_full', fail_aggregate), \
             patch('machine_audit.scanner.runner.time.time_ns', return_value=86500*10**9):
            result, status = self.scan([self.volume])
        self.assertEqual(status, 'missing_directory_ancestor_ScanError')
        expected = ('v', 'full', 'requested_full', 'aggregate')
        self.assertEqual(tuple(result[key] for key in ('volume_id','action','trigger','stage')), expected)
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            row = db.execute('SELECT volume_id,action,trigger,stage FROM runs ORDER BY started_ns DESC LIMIT 1').fetchone()
        self.assertEqual(row, expected)
        self.assertNotIn('absent synthetic identity', repr(result))

    def test_a_failure_that_leaves_a_statement_open_is_still_recorded(self):
        # Big Mac's first physical walks died in sharing with the run left 'running':
        # the failure's open statement pinned the WAL, so recording it failed too.
        def pinned_failure(store, *args):
            pinned = store.db.execute('SELECT * FROM nodes')
            next(pinned)
            raise RuntimeError('synthetic failure')
        with patch.object(MapStore, '_sharing', pinned_failure), \
             patch('machine_audit.scanner.runner.time.time_ns', return_value=86500*10**9):
            result, status = self.scan([self.volume])
        self.assertEqual((status, result['failure_recorded'], result['stage']), ('unexpected_error_RuntimeError', True, 'sharing'))
        self.assertIn('test_open:pinned_failure', ' '.join(result['at']))
        self.assertNotIn('synthetic failure', repr(result))

    def test_failed_volume_retries_requested_full_before_successful_map_cooldown(self):
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            db.execute("UPDATE volumes SET status='invalid_scan_state'")
        before=self.published()
        with patch.object(MapStore,'_aggregate_full',side_effect=ScanError('missing_directory_ancestor')), \
             patch('machine_audit.scanner.runner.time.time_ns',return_value=110*10**9):
            result,status=self.scan([self.volume],force=True)
        self.assertEqual(result['action'],'full')
        self.assertEqual(result['trigger'],'failed_volume_retry')
        self.assertEqual(status,'missing_directory_ancestor_ScanError')
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            self.assertEqual(db.execute('SELECT status FROM volumes').fetchone()[0],status)
        self.assertEqual(self.published(),before)
        with patch('machine_audit.scanner.runner.time.time_ns',return_value=120*10**9):
            result,status=self.scan([self.volume],force=True)
        self.assertEqual((result['action'],result['trigger'],status),('full','failed_volume_retry','complete'))
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            self.assertEqual(db.execute('SELECT status,full_ns FROM volumes').fetchone(),('observed',120*10**9))

    def test_scanner_full_retries_failed_volume_only_daily_unless_explicit(self):
        day=86400*10**9
        first=100*10**9+day
        with patch.object(MapStore,'_aggregate_full',side_effect=ScanError('missing_directory_ancestor')) as aggregate:
            with patch('machine_audit.scanner.runner.time.time_ns',return_value=first):
                result,status=self.scan([self.volume])  # Persistent scanner_full, not CLI --full.
            self.assertEqual(status,'missing_directory_ancestor_ScanError')
            for hours in (1,2,23):
                with patch('machine_audit.scanner.runner.time.time_ns',return_value=first+hours*3600*10**9):
                    result,_=self.scan([self.volume])
                self.assertEqual((result['action'],result['trigger']),('wait','failed_full_retry_cooldown'))
                self.assertEqual(result['volumes'][0]['status'],'missing_directory_ancestor_ScanError')
            self.assertEqual(aggregate.call_count,1)
            with patch('machine_audit.scanner.runner.time.time_ns',return_value=first+day):
                result,_=self.scan([self.volume])
            self.assertEqual((result['action'],result['trigger']),('full','failed_volume_retry'))
            self.assertEqual(aggregate.call_count,2)
            with patch('machine_audit.scanner.runner.time.time_ns',return_value=first+day+10):
                result,_=self.scan([self.volume],force=True)
            self.assertEqual(result['action'],'full')
            self.assertEqual(aggregate.call_count,3)
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            self.assertEqual(db.execute('SELECT full_ns,full_attempt_ns FROM volumes').fetchone(),
                             (100*10**9,first+day+10))

    def test_root_must_scan_uses_full_writer_and_records_its_trigger(self):
        events = SimpleNamespace(uuid=lambda device:'events', current_id=lambda:30,
                                 replay=lambda device,since:Replay(paths={'/':True}, up_to=30))
        original = Walker.walk
        modes = []
        def walk(walker, *args, **kwargs):
            modes.append(walker.sink.base_gen)
            return original(walker,*args,**kwargs)
        with patch.object(Walker,'walk',walk), \
             patch('machine_audit.scanner.runner.time.time_ns', return_value=3700*10**9):
            result, status = self.scan([self.volume], events=events)
        self.assertEqual(modes, [None], 'root replay used incremental subtree/reset writes')
        self.assertEqual((result['action'],result['trigger']), ('full','event_root_must_scan'))
        self.assertEqual(status, 'complete')
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            self.assertEqual(db.execute('SELECT action,trigger FROM runs').fetchone(), ('full','event_root_must_scan'))

    def test_partial_stream_gap_does_not_request_another_full_map(self):
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            db.execute('UPDATE volumes SET gaps=?',('["partial_stream_needs_full_map"]',))
        events = SimpleNamespace(uuid=lambda device:'events', current_id=lambda:30,
                                 replay=lambda device,since:Replay(up_to=30))
        with patch('machine_audit.scanner.runner.time.time_ns', return_value=3700*10**9):
            result, status = self.scan([self.volume], events=events)
        self.assertEqual((result['action'],result['trigger'],result['entries']),('update','hourly_update',0))
        self.assertEqual(status,'complete')
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            self.assertEqual(db.execute('SELECT full_ns FROM volumes').fetchone()[0],100*10**9)

    def test_deferred_volume_root_uses_full_writer_unless_known_shallow(self):
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            db.execute('INSERT INTO dirty(volume,id,due_ns) VALUES(?,?,?)',('v','1',0))  # Recorded before the scope flag.
        events = SimpleNamespace(uuid=lambda device:'events', current_id=lambda:30,
                                 replay=lambda device,since:Replay(up_to=30))
        with patch('machine_audit.scanner.runner.time.time_ns', return_value=3700*10**9):
            result, status = self.scan([self.volume],events=events)
        self.assertEqual((result['action'],result['trigger']),('full','deferred_root_due'))
        self.assertEqual(status,'complete')
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            now = db.execute('SELECT observed_ns FROM volumes').fetchone()[0]+3600*10**9
            db.execute('INSERT INTO dirty VALUES(?,?,?,0)',('v',str(self.tree.stat().st_ino),0))
        with patch('machine_audit.scanner.runner.time.time_ns', return_value=now):
            result, status = self.scan([self.volume],events=events)
        self.assertEqual((result['action'],result['trigger'],result['entries']),('update','deferred_folder_due',1))
        self.assertEqual(status,'complete')

    def test_directory_replaced_before_the_scan_relists_its_parent(self):
        # A changed directory gone by scan time failed the whole update and left the
        # map stale for a day; its parent's listing records the replacement instead.
        self.scan([self.volume])  # Publish a real tiny map with real inode identities.
        before = self.published()
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            now = db.execute('SELECT observed_ns FROM volumes').fetchone()[0]+3600*10**9
        events = SimpleNamespace(uuid=lambda device:'events', current_id=lambda:30,
                                 replay=lambda device,since:Replay(paths={'/child':False}, up_to=30))
        child = self.tree/'child'
        child.rmdir()  # Disposable empty test directory only.
        child.write_bytes(b'replacement')
        with patch('machine_audit.scanner.runner.time.time_ns', return_value=now):
            result, status = self.scan([self.volume], events=events)
        self.assertEqual((status, result['volumes'][0]['status']), ('complete', 'observed'))
        after = self.published()
        self.assertNotEqual(after[1], before[1])
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            generation = db.execute("SELECT generation FROM volumes WHERE id='v'").fetchone()[0]
            self.assertEqual(db.execute('SELECT count(*) FROM files WHERE generation=?', (generation,)).fetchone()[0], 1)

    def test_directory_replaced_between_check_and_walk_is_a_volume_gap(self):
        self.scan([self.volume])  # Publish a real tiny map with real inode identities.
        before = self.published()
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            now = db.execute('SELECT observed_ns FROM volumes').fetchone()[0]+3600*10**9
        events = SimpleNamespace(uuid=lambda device:'events', current_id=lambda:30,
                                 replay=lambda device,since:Replay(paths={'/child':False}, up_to=30))
        original = Walker.walk
        child = self.tree/'child'
        def replace_before_walk(walker, root=None, *args, **kwargs):
            if root==str(child):
                child.rmdir()  # Disposable empty test directory only.
                child.write_bytes(b'replacement')
            return original(walker,root,*args,**kwargs)
        other = self.base/'other';other.mkdir()
        with patch.object(Walker,'walk',replace_before_walk), \
             patch('machine_audit.scanner.runner.time.time_ns', return_value=now):
            result, status = self.scan([self.volume,replace(self.volume,id='other',mount=str(other))], events=events)
        self.assertEqual(status, 'partial')
        self.assertEqual(result['volumes'][0]['status'], 'update_unreadable')
        self.assertIn('root_not_directory', result['volumes'][0]['gaps'])
        self.assertEqual(result['volumes'][1]['status'], 'observed')
        self.assertEqual(self.published(), before)
        # The scheduled scanner keeps no output: the run row holds each volume's codes.
        with sqlite3.connect(self.data/'scanner.sqlite') as db:
            recorded = json.loads(db.execute('SELECT volumes FROM runs ORDER BY started_ns DESC LIMIT 1').fetchone()[0])
        self.assertEqual([(v['status'], v['volume_id']) for v in recorded], [('update_unreadable', 'v'), ('observed', 'other')])
        self.assertIn('root_not_directory', recorded[0]['gaps'])

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
            store.finish_volume(self.volume, None, ['directory_unreadable'],
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
                    walker = Walker(self.volume, Policy(home=str(self.base), homes=[]), store, fs=object())
                    walker.walk()
                    self.assertEqual(walker.entries, 0)
                    self.assertTrue(walker.listed)
                    with patch('machine_audit.scanner.store.time.time_ns', return_value=200_000 * 10**9):
                        store.finish_volume(self.volume, None, [], full=True, lean=True,
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
