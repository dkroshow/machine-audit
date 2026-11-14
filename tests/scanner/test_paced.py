"""Paced full walks: the hour's CPU allowance, the checkpoint, and the map meanwhile.

Synthetic directory trees in a disposable scratch folder only. The allowance clock
is a counter, so how much a run gets through is fixed, not timing-dependent."""
from dataclasses import replace
from itertools import count
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from machine_audit.budget import Budget, BudgetExceeded
from machine_audit.scanner import pace
from machine_audit.scanner.events import Replay
from machine_audit.scanner.native import Volume
from machine_audit.scanner.policy import Policy
from machine_audit.scanner.runner import run_scan
from machine_audit.scanner.store import MapQuery, MapStore

SCRATCH = Path(__file__).parent / '.tmp'
SCRATCH.mkdir(exist_ok=True)
SECOND = 10**9
HOUR = 3600 * SECOND


def who(rows, complete=True):
    """A `who` answer in the shape the collector's query returns (captured on Big Mac,
    entities replaced by synthetic ones)."""
    return {'totals_complete': complete, 'rows': [dict(source='macos.process', component='cpu', scope='process',
            unit='mach_ticks', scale=1e-09, **row) for row in rows]}


@unittest.skipUnless(sys.platform == 'darwin', 'paced walks resume through fsgetpath')
class PacedWalkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.tree = self.base / 'tree'
        for a in range(5):
            for b in range(6):
                folder = self.tree / f'folder{a}' / f'sub{b}'
                folder.mkdir(parents=True)
                for c in range(3):(folder / f'file{c}').write_bytes(b'x' * (a + b + c + 1) * 4096)
        self.volume = Volume('v', 'Volume', str(self.tree), 'hfs', self.tree.stat().st_dev,
                             100 * 10**9, 50 * 10**9, 50 * 10**9, 40 * 10**9)
        self.now = 1_000 * HOUR

    def scan(self, data, *, force=False, full=False, events=None, ticks=None, floor=40 * SECOND, fs=None, clock=None, limit=None,
             walk_floor=None):
        """One scanner run. Each read of the allowance clock costs a second, unless
        `clock` gives the CPU spent instead."""
        data.mkdir(exist_ok=True)
        native = SimpleNamespace(process=lambda pid: SimpleNamespace(proc_start_abstime=1, diskio_byteswritten=0))
        filesystem = fs or SimpleNamespace(volumes=lambda: [self.volume], sharing=lambda *args: None)
        events = events or SimpleNamespace(uuid=lambda device: 'events', current_id=lambda: 20,
                                           replay=lambda device, since: Replay(up_to=20))
        ticks = ticks or count()
        clock = clock or (lambda: next(ticks) * SECOND)
        with patch('machine_audit.scanner.runner.Darwin', return_value=native), \
             patch('machine_audit.scanner.runner.MacFilesystem', return_value=filesystem), \
             patch('machine_audit.scanner.runner.IOKit'), \
             patch('machine_audit.scanner.runner.FSEvents', return_value=events), \
             patch('machine_audit.scanner.runner.os.nice'), \
             patch('machine_audit.scanner.runner.Budget', lambda root: Budget(root, free_margin_bytes=0, **({'limit_bytes': limit} if limit else {}))), \
             patch('machine_audit.scanner.runner.Policy', return_value=Policy(home=str(self.base), homes=[])), \
             patch('machine_audit.scanner.runner.time.time_ns', return_value=self.now), \
             patch.object(pace.Allowance, 'clock', staticmethod(clock)), \
             patch.object(pace, 'FLOOR_NS', floor), patch.object(pace, 'WALK_FLOOR_NS', walk_floor or floor // 2):
            result = run_scan(data, full=full, force=force)
        self.now += HOUR
        return result

    def db(self, data):
        return sqlite3.connect(data / 'scanner.sqlite')

    def published_map(self, data):
        """The published folders and files, without generation numbers or times."""
        with self.db(data) as db:
            generation, delta = db.execute("SELECT generation,delta_generation FROM volumes WHERE id='v'").fetchone()
            self.assertIsNone(delta)
            nodes = sorted(db.execute('SELECT id,parent,label,kind,own_listed,own_physical,listed,physical,status FROM nodes WHERE generation=?',
                                      (generation,)).fetchall())
            files = sorted(db.execute('SELECT parent,id,label,allocated FROM files WHERE generation=?', (generation,)).fetchall())
        return nodes, files

    def reference(self):
        data = self.base / 'reference'
        self.assertEqual(self.scan(data, force=True)['status'], 'observed')
        return self.published_map(data)

    def walk_row(self, data):
        with self.db(data) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM walks WHERE volume='v'").fetchone()
        return dict(row) if row else None

    def test_paced_walk_pauses_at_the_allowance_and_resumes_to_the_unpaced_map(self):
        # The full walk cost 2,948 s of CPU in one hour on Big Mac (Nov 10). Paced, each
        # run walks what its allowance covers, checkpoints, and a later run goes on.
        expected = self.reference()
        data = self.base / 'paced'
        runs = []
        while (walk := self.walk_row(data) if runs else None) is not None or not runs:
            result = self.scan(data)
            runs.append(result)
            walk = self.walk_row(data)
            if walk is None:break
            # Until it publishes there is no map, and the tab says how far along it is.
            row = MapQuery(data / 'scanner.sqlite').query('volumes')['rows'][0]
            self.assertEqual((row['walk']['phase'], row['walk']['entries']), (walk['phase'], walk['entries']))
            with self.db(data) as db:
                self.assertEqual(db.execute("SELECT status FROM generations WHERE id=?", (walk['generation'],)).fetchone(), ('building',))
            self.assertLess(len(runs), 30, 'the walk never finished')
        # The first run paused at its allowance; a later one published.
        self.assertEqual(runs[0]['volumes'][0]['walk']['state'], 'paused', 'the walk was not paced')
        self.assertEqual(runs[-1]['volumes'][0]['walk']['state'], 'published')
        self.assertEqual(self.published_map(data), expected)
        with self.db(data) as db:
            # The run row says what the hour allowed and where the CPU went.
            allowance, stages = db.execute('SELECT allowance_ns,stage_cpu FROM runs ORDER BY started_ns DESC LIMIT 1').fetchone()
        self.assertEqual(allowance, 40 * SECOND)
        self.assertIn('aggregate', json.loads(stages))

    def test_a_stop_mid_walk_resumes_from_its_checkpoint_not_the_root(self):
        expected = self.reference()
        data = self.base / 'stopped'
        original = MapStore.checkpoint
        calls = count(1)
        def checkpoint(store, *args, **kwargs):
            original(store, *args, **kwargs)
            if next(calls) == 2:raise KeyboardInterrupt  # The process stops after its second checkpoint.
        with patch.object(MapStore, 'checkpoint', checkpoint), self.assertRaises(KeyboardInterrupt):
            self.scan(data, floor=10**6 * SECOND)
        before = self.walk_row(data)
        self.assertEqual(before['phase'], 'listing')
        self.assertGreater(before['entries'], 0)
        result = self.scan(data, floor=10**6 * SECOND)
        self.assertEqual(result['volumes'][0]['walk']['state'], 'published')
        # Only what the checkpoint left was listed again.
        total = sum(1 for _ in self.tree.rglob('*'))
        self.assertEqual(result['entries'], total - before['entries'])
        self.assertEqual(self.published_map(data), expected)
        with self.db(data) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs WHERE status='interrupted'").fetchone()[0], 1)

    def test_hourly_updates_publish_during_a_paced_walk_and_stop_at_the_allowance(self):
        data = self.base / 'updating'
        self.assertEqual(self.scan(data, force=True)['status'], 'observed')
        with self.db(data) as db:
            db.execute("UPDATE volumes SET full_ns=?", (self.now - 8 * 24 * HOUR,))  # A weekly walk is due.
        changed = [self.tree / 'folder1' / 'sub2', self.tree / 'folder3' / 'sub4']
        events = SimpleNamespace(uuid=lambda device: 'events', current_id=lambda: 20,
                                 replay=lambda device, since: Replay(paths={'/' + str(p.relative_to(self.tree)): False for p in changed}, up_to=21))
        first = self.scan(data)  # Starts the walk; the published map stands.
        self.assertEqual(first['volumes'][0]['walk']['state'], 'paused')
        for folder in changed:(folder / 'added').write_bytes(b'y' * 8192)
        # An allowance the update spends at once (no minimum share). The volume walks, so
        # nothing is queued: its walk and the replay after it re-list both folders.
        with patch.object(pace, 'MIN_SHARE_NS', 0):
            second = self.scan(data, events=events, floor=1)
        update = second['volumes'][0]
        self.assertEqual((update['action'], update['status']), ('update', 'observed'))
        self.assertIn('walk_supersedes', update['gaps'])
        self.assertEqual(update['superseded'], 2)
        with self.db(data) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM dirty WHERE volume='v'").fetchone()[0], 0, 'the backlog grew')
        self.assertIsNotNone(self.walk_row(data), 'the walk should still be running')
        while self.walk_row(data):self.scan(data)
        with self.db(data) as db:
            # It publishes with its own start bookmark: the next update replays every change since.
            self.assertEqual(db.execute("SELECT bookmark FROM volumes WHERE id='v'").fetchone()[0], 20)
        self.now += 60 * SECOND  # The walk published 1 ns after that run's update: an hour on is 1 ns short.
        catch_up = self.scan(data, events=events)
        self.assertEqual(catch_up['volumes'][0]['action'], 'update')
        nodes, files = self.published_map(data)
        self.assertEqual(sum(1 for f in files if f[2] == 'added'), 2, 'the catch-up did not re-list them')

    def updating_store(self):
        """A published map of the tree, with an hourly update due."""
        data = self.base / 'merging'
        self.assertEqual(self.scan(data, force=True)['status'], 'observed')
        return data

    def events(self, folders, up_to):
        return SimpleNamespace(uuid=lambda device: 'events', current_id=lambda: up_to,
                               replay=lambda device, since: Replay(paths={'/' + str(p.relative_to(self.tree)): False for p in folders}, up_to=up_to))

    def test_an_update_merge_stops_at_the_allowance_and_resumes_with_the_map_valid(self):
        # Live, Nov 11: every update's merge ran unpaced after listing filled the
        # allowance, 76-100 s over a 120 s floor.
        data = self.updating_store()
        changed = self.tree / 'folder2' / 'sub3'
        (changed / 'file0').unlink()  # Disposable synthetic file: a removal for the merge.
        (changed / 'added').write_bytes(b'y' * 8192)
        merging = []
        original = MapStore.merge_delta
        def merge(store, *args, **kwargs):
            merging.append(True)
            try:return original(store, *args, **kwargs)
            finally:merging.pop()
        # The CPU clock jumps once the merge starts: listing fitted, the merge does not.
        with patch.object(MapStore, 'merge_delta', merge):
            result = self.scan(data, events=self.events([changed], 21), clock=lambda: (10**6 if merging else 0) * SECOND)
        self.assertEqual(result['status'], 'observed', 'a paused merge is not a failure')
        self.assertEqual(result['volumes'][0]['merge'], 'allowance_spent')
        with self.db(data) as db:
            self.assertIsNotNone(db.execute("SELECT delta_generation FROM volumes WHERE id='v'").fetchone()[0])
        def labels():
            query = MapQuery(data / 'scanner.sqlite')
            folder = next(r for r in query.query('children', volume_id='v')['rows'] if r['label'] == 'folder2')
            sub = next(r for r in query.query('children', volume_id='v', parent_id=folder['id'])['rows'] if r['label'] == 'sub3')
            return sorted(r['label'] for r in query.query('children', volume_id='v', parent_id=sub['id'])['rows'])
        self.assertEqual(labels(), ['added', 'file1', 'file2'], 'the published map shows the update while it merges')
        result = self.scan(data, events=self.events([], 22), clock=lambda: 0)
        with self.db(data) as db:
            self.assertIsNone(db.execute("SELECT delta_generation FROM volumes WHERE id='v'").fetchone()[0])
        self.assertEqual(labels(), ['added', 'file1', 'file2'])

    def test_a_run_at_the_floor_drains_deferred_folders_before_new_changes(self):
        # Live, Nov 11: the deferred backlog did not drain. New shallow changes, re-listed
        # first, took each run's allowance; deferred deeper folders waited.
        data = self.updating_store()
        deferred = [self.tree / f'folder{a}' / f'sub{b}' for a in range(5) for b in range(2)]
        store = MapStore(data / 'scanner.sqlite', Budget(data, free_margin_bytes=0))
        store.volume = 'v'
        for folder in deferred:store.defer(str(folder.stat().st_ino), 0, False)
        store.close()
        listed = [0]  # Folders listed: the run's CPU clock, 10 s each.
        original = MapStore.listed
        def counted(store, *args):
            listed[0] += 1
            return original(store, *args)
        backlog = []
        with patch.object(MapStore, 'listed', counted):
            for run in range(6):
                # Every run brings three new changes: more than its allowance re-lists.
                shallow = [self.tree / f'folder{(run + i) % 5}' for i in range(3)]
                self.scan(data, events=self.events(shallow, 30 + run), floor=25 * SECOND, clock=lambda: listed[0] * 10 * SECOND)
                with self.db(data) as db:
                    waiting = {r[0] for r in db.execute("SELECT id FROM dirty WHERE volume='v'")}
                backlog.append(sum(1 for folder in deferred if str(folder.stat().st_ino) in waiting))
        self.assertEqual(backlog[-1], 0, backlog)
        self.assertTrue(all(b < a for a, b in zip(backlog, backlog[1:]) if a), backlog)

    def test_new_hard_links_re_list_their_other_folders_within_the_allowance(self):
        # Live, Nov 11 4:19-6:30 PM PT: updates overran by up to 77 s in listing after
        # many files gained hard links; finding and re-listing the folders that held
        # them as plain files was not paced.
        data = self.updating_store()
        linked = self.tree / 'linked';linked.mkdir()
        for a in range(5):
            for b in range(2):os.link(self.tree / f'folder{a}' / f'sub{b}' / 'file0', linked / f'{a}-{b}')
        expected = self.reference()
        listed = [0]  # Folders listed: the run's CPU clock, 10 s each.
        original = MapStore.listed
        def counted(store, *args):
            listed[0] += 1
            return original(store, *args)
        runs = []
        with patch.object(MapStore, 'listed', counted):
            for run in range(8):
                before = listed[0]
                result = self.scan(data, events=self.events([linked] if run == 0 else [], 40 + run), floor=25 * SECOND,
                                   clock=lambda: listed[0] * 10 * SECOND)
                runs.append((listed[0] - before, result['volumes'][0]['gaps']))
                self.assertEqual(MapQuery(data / 'scanner.sqlite').query('volumes')['rows'][0]['valid'], True)
                with self.db(data) as db:
                    if not db.execute('''SELECT (SELECT count(*) FROM dirty WHERE volume='v')+
                            (SELECT count(delta_generation) FROM volumes WHERE id='v')''').fetchone()[0]:break
        # Each run lists at most what its 25 s allowance covers, plus the one folder
        # under way when it ran out; the rest carry over until every link counts once.
        self.assertTrue(all(count <= 3 for count, _ in runs), runs)
        self.assertIn('update_deferred', runs[0][1])
        self.assertGreater(len(runs), 2, runs)
        self.assertEqual(self.published_map(data), expected)

    def listing_clock(self, per_entry):
        """A CPU clock driven by what the walker lists: `per_entry` a listed entry."""
        spent = [0]
        original = MapStore.listed
        def counted(store, identity, entries):
            spent[0] += max(entries, 1) * per_entry
            return original(store, identity, entries)
        return spent, patch.object(MapStore, 'listed', counted)

    def test_a_deferral_backlog_drains_over_runs_within_the_bound_and_a_walk_keeps_its_floor(self):
        # Big Mac, Nov 14 12:24-3:36 AM PT: after the startup volume's walk published,
        # its 39 k due deferrals were each resolved to a path before any listing (31 s a
        # run), and every run overran (135-173 s of 120) while another volume walked.
        other = self.base / 'other'
        for d in range(8):
            (other / f'd{d}').mkdir(parents=True)
            for f in range(4):(other / f'd{d}' / f'f{f}').write_bytes(b'o')
        second = replace(self.volume, id='w', label='Second', mount=str(other))
        filesystem = SimpleNamespace(volumes=lambda: [self.volume, second], sharing=lambda *args: None)
        data = self.base / 'backlog'
        self.assertEqual(self.scan(data, force=True, fs=filesystem)['status'], 'observed')
        expected = self.published_map(data)
        folders = [self.tree] + [p for p in self.tree.rglob('*') if p.is_dir()]
        with self.db(data) as db:
            db.execute("UPDATE volumes SET full_ns=? WHERE id='w'", (self.now - 8 * 24 * HOUR,))  # Its walk is due.
            db.executemany("INSERT INTO dirty VALUES('v',?,?,0)", [(str(p.stat().st_ino), self.now - HOUR) for p in folders])
        spent, counting = self.listing_clock(SECOND)
        original = MapStore.known_directory
        def looked_up(store, identity):  # Resolving a folder of the map costs 2 s.
            spent[0] += 2 * SECOND
            return original(store, identity)
        def walked_entries():  # The second volume's walk: entries listed, or None once published.
            with self.db(data) as db:
                row = db.execute("SELECT entries FROM walks WHERE volume='w'").fetchone()
            return row and row[0]
        costs = [];walked = []
        with counting, patch.object(MapStore, 'known_directory', looked_up):
            for run in range(30):
                before = spent[0];walk = walked_entries()
                self.scan(data, events=self.events([], 50 + run), fs=filesystem, floor=25 * SECOND, clock=lambda: spent[0])
                costs.append((spent[0] - before) // SECOND)
                if walk is not None:walked.append((walked_entries() or 10**9) - walk)
                with self.db(data) as db:
                    if not db.execute("SELECT count(*) FROM dirty WHERE volume='v'").fetchone()[0]:break
        # Each run: the 25 s allowance, the walk's 12 s floor and the one folder under
        # way (a 2 s lookup and at most 6 entries); the backlog drains over many runs.
        self.assertTrue(all(cost <= 25 + 12 + 8 for cost in costs), costs)
        self.assertGreater(len(costs), 3, costs)
        self.assertTrue(walked and all(step > 0 for step in walked), walked)
        self.assertEqual(self.published_map(data), expected)

    def test_pruning_a_replaced_map_before_a_walk_is_recorded_as_pruning(self):
        # Big Mac, Nov 14: a due walk spent its 20 s floor each run pruning the startup
        # volume's replaced map, recorded as 'prepare', which read as unexplained work.
        data = self.updating_store()
        with self.db(data) as db:
            db.execute("INSERT INTO generations VALUES(99,1,'retired')")
            for table in ('nodes', 'files', 'listings'):
                db.execute(f'CREATE TEMP TABLE copy_{table} AS SELECT * FROM {table} WHERE generation=1')
                db.execute(f'UPDATE copy_{table} SET generation=99')
                db.execute(f'INSERT INTO {table} SELECT * FROM copy_{table}')
            db.execute('UPDATE volumes SET full_ns=?', (self.now - 8 * 24 * HOUR,))
        self.scan(data)
        with self.db(data) as db:
            stages = json.loads(db.execute('SELECT stage_cpu FROM runs ORDER BY started_ns DESC LIMIT 1').fetchone()[0])
            self.assertIsNone(db.execute('SELECT 1 FROM generations WHERE id=99').fetchone())
        self.assertIn('prune', stages)
        self.assertIsNotNone(self.walk_row(data), 'the walk did not start after the pruning')

    def test_a_volume_queued_for_a_walk_keeps_its_hourly_update(self):
        # Big Mac, Nov 11 8:43 and 9:46 PM PT: while the startup volume walked, the
        # other eight volumes waited their turn with no hourly update.
        other = self.base / 'other';(other / 'inner').mkdir(parents=True);(other / 'inner' / 'f').write_bytes(b'o')
        second = replace(self.volume, id='w', label='Second', mount=str(other))
        filesystem = SimpleNamespace(volumes=lambda: [self.volume, second], sharing=lambda *args: None)
        data = self.base / 'queued'
        self.assertEqual(self.scan(data, force=True, fs=filesystem)['status'], 'observed')
        with self.db(data) as db:
            db.execute('UPDATE volumes SET full_ns=?', (self.now - 8 * 24 * HOUR,))  # Both walks are due.
        spent, counting = self.listing_clock(10 * SECOND)
        with counting:
            for run in range(3):
                with self.db(data) as db:
                    before = db.execute("SELECT observed_ns FROM volumes WHERE id='w'").fetchone()[0]
                self.scan(data, full=False, events=self.events([], 50 + run), fs=filesystem, floor=25 * SECOND,
                          clock=lambda: spent[0])
                with self.db(data) as db:
                    after = db.execute("SELECT observed_ns FROM volumes WHERE id='w'").fetchone()[0]
                    walking = {r[0] for r in db.execute('SELECT volume FROM walks')}
                self.assertEqual(walking, {'v'}, 'the first volume walks; the second waits its turn')
                self.assertGreater(after, before, f'run {run + 1}: the queued volume got no update')

    def test_updates_that_spend_the_allowance_leave_the_walk_only_its_floor(self):
        data = self.updating_store()
        with self.db(data) as db:
            db.execute('UPDATE volumes SET full_ns=?', (self.now - 8 * 24 * HOUR,))
        spent, counting = self.listing_clock(10 * SECOND)
        with counting:
            self.scan(data, full=False, floor=25 * SECOND, clock=lambda: spent[0])  # The walk starts.
            changed = [self.tree / f'folder{a}' / f'sub{b}' for a in range(5) for b in range(2)]
            before = self.walk_row(data)['entries']
            result = self.scan(data, full=False, events=self.events(changed, 60), floor=25 * SECOND, clock=lambda: spent[0])
        self.assertIn('walk_supersedes', result['volumes'][0]['gaps'])
        # The update reached the 25 s allowance; the walk got its 20 s floor: two folders
        # of at most 6 entries each.
        self.assertTrue(0 < self.walk_row(data)['entries'] - before <= 2 * 6, (before, self.walk_row(data)['entries']))

    def test_a_large_folder_waits_for_a_share_that_covers_it(self):
        # Big Mac, Nov 11 9:46 PM PT: the walk began a folder of 416 k entries with its
        # share nearly spent, and that one listing ran 80 s past the run's allowance.
        big = self.tree / 'big';big.mkdir()
        for i in range(40):(big / str(i)).write_bytes(b'b')
        expected = self.reference()
        data = self.base / 'large'
        self.assertEqual(self.scan(data, force=True)['status'], 'observed')
        with self.db(data) as db:
            db.execute('UPDATE volumes SET full_ns=?', (self.now - 8 * 24 * HOUR,))
        spent, counting = self.listing_clock(SECOND)
        big_listed = [];waited = False
        with counting, patch('machine_audit.scanner.runner.LARGE_FOLDER_ENTRIES', 20, create=True), \
             patch('machine_audit.scanner.runner.LIST_NS_PER_ENTRY', SECOND, create=True):
            for run in range(12):
                before = spent[0]
                result = self.scan(data, full=False, floor=25 * SECOND, clock=lambda: spent[0])
                if not waited:
                    # Within the 25 s share, plus the one small folder under way (6 entries).
                    self.assertLessEqual(spent[0] - before, 31 * SECOND, f'run {run + 1} overran its share')
                walk = self.walk_row(data)
                if walk is None:break
                if walk['phase'] == 'large':
                    self.assertFalse(big_listed, 'listed before its share covered it')
                    with self.db(data) as db:  # A day passes with no hour that covers it.
                        db.execute('UPDATE walks SET phase_ns=phase_ns-?', (25 * HOUR,))
                    waited = True
                with self.db(data) as db:
                    big_listed = [1] if db.execute('SELECT 1 FROM listings WHERE generation=? AND id=?',
                                                   (walk['generation'], big.stat().st_ino)).fetchone() else []
                self.assertTrue(big_listed or walk['phase'] in ('listing', 'large'))
        self.assertIsNone(self.walk_row(data), 'the walk never finished')
        self.assertEqual(self.published_map(data), expected)

    def test_an_update_defers_a_folder_its_share_cannot_cover(self):
        # Big Mac, Nov 12 12:15 and 1:22 AM PT: the startup volume's update overran its
        # allowance every run (231.3 s of 126.9); a due unit was listed whatever its size.
        big = self.tree / 'big';big.mkdir()
        for i in range(40):(big / str(i)).write_bytes(b'b')
        data = self.updating_store()
        small = [self.tree / 'folder0' / 'sub0', self.tree / 'folder1' / 'sub1']
        spent, counting = self.listing_clock(SECOND)
        def listed_ns(folder):
            with self.db(data) as db:
                # The published map: its base, overlaid by a delta still merging.
                return db.execute('''SELECT max(l.listed_ns) FROM listings l JOIN volumes v ON l.volume=v.key
                    AND l.generation IN (v.generation,v.delta_generation) WHERE l.id=?''', (folder.stat().st_ino,)).fetchone()[0]
        with counting, patch('machine_audit.scanner.runner.RELIST_NS_PER_ENTRY', SECOND):
            before = spent[0];big_before = listed_ns(big)
            result = self.scan(data, events=self.events([big] + small, 70), floor=25 * SECOND, clock=lambda: spent[0])
            self.assertIn('update_deferred', result['volumes'][0]['gaps'])
            self.assertEqual(listed_ns(big), big_before, 'a 40 s folder was listed in a 25 s share')
            self.assertLessEqual(spent[0] - before, 25 * SECOND)
            self.assertTrue(all(listed_ns(folder) > big_before for folder in small))
            # It waits, however long, for a share that covers it: never forced at a day...
            with self.db(data) as db:
                db.execute('UPDATE dirty SET due_ns=due_ns-?', (25 * HOUR,))
            self.scan(data, events=self.events([], 71), floor=25 * SECOND, clock=lambda: spent[0])
            self.assertEqual(listed_ns(big), big_before, 'forced past its share at the day cap')
        # ...and the weekly walk lists it.
        self.assertEqual(self.scan(data, force=True)['status'], 'observed')
        self.assertGreater(listed_ns(big), big_before)

    def test_an_update_pauses_a_new_subtree_between_folders_at_its_share(self):
        # Big Mac, Nov 13 8:16 AM PT: an update overran its 120 s allowance by 32 s. A
        # folder new since the last map has no size to admit it by, and its whole
        # subtree was listed with it.
        data = self.updating_store()
        new = self.tree / 'new'
        for d in range(8):
            (new / f'd{d}' / 'inner').mkdir(parents=True)
            for f in range(5):(new / f'd{d}' / f'f{f}').write_bytes(b'n')
            for f in range(3):(new / f'd{d}' / 'inner' / f'f{f}').write_bytes(b'n')
        expected = self.reference()
        spent, counting = self.listing_clock(SECOND)  # 80 s to list the new subtree.
        runs = []
        with counting:
            for run in range(10):
                before = spent[0]
                result = self.scan(data, events=self.events([self.tree] if run == 0 else [], 90 + run), floor=25 * SECOND,
                                   clock=lambda: spent[0])
                runs.append((spent[0] - before) // SECOND)
                self.assertEqual(MapQuery(data / 'scanner.sqlite').query('volumes')['rows'][0]['valid'], True)
                with self.db(data) as db:
                    stages, volumes = db.execute('SELECT stage_cpu,volumes FROM runs ORDER BY started_ns DESC LIMIT 1').fetchone()
                    stages = json.loads(stages);volume, = json.loads(volumes)
                    left = db.execute("""SELECT (SELECT count(*) FROM dirty WHERE volume='v')+
                        (SELECT count(delta_generation) FROM volumes WHERE id='v')""").fetchone()[0]
                if run == 0:
                    self.assertIn('update_deferred', result['volumes'][0]['gaps'])
                    # An update's listing is its own stage, apart from a walk's, and the run
                    # row splits its listing CPU from its change and deferral handling, beside
                    # the entries it listed: a per-entry cost to compare with the walk's.
                    self.assertIn('relist', stages);self.assertNotIn('walk', stages)
                    self.assertTrue(volume['entries'] > 0 and volume['list_ns'] > 0, volume)
                    self.assertTrue({'events_ns', 'deferrals_ns'} <= volume.keys(), volume)
                if not left:break
        # Each run stops within its 25 s share plus the one folder under way (at most
        # 8 entries); the folders left are listed by later runs, to the unpaced map.
        self.assertTrue(all(cost <= 25 + 8 for cost in runs), runs)
        self.assertGreater(len(runs), 2, runs)
        self.assertEqual(self.published_map(data), expected)

    def test_a_churning_volume_does_not_starve_another_volumes_update(self):
        # Big Mac, Nov 12: the startup volume's churn took every run's allowance and the
        # other volumes' updates re-listed nothing.
        other = self.base / 'other';(other / 'inner').mkdir(parents=True);(other / 'inner' / 'f').write_bytes(b'o')
        second = replace(self.volume, id='w', label='Second', mount=str(other), device=-2)
        filesystem = SimpleNamespace(volumes=lambda: [self.volume, second], sharing=lambda *args: None)
        data = self.base / 'churn'
        self.assertEqual(self.scan(data, force=True, fs=filesystem)['status'], 'observed')
        churning = ['/' + str(p.relative_to(self.tree)) for p in [self.tree / f'folder{a}' / f'sub{b}' for a in range(5) for b in range(2)]]
        def events(up_to):
            return SimpleNamespace(uuid=lambda device: 'events', current_id=lambda: up_to,
                replay=lambda device, since: Replay(paths=dict.fromkeys(['/inner'] if device == -2 else churning, False), up_to=up_to))
        spent, counting = self.listing_clock(2 * SECOND)
        inner = (other / 'inner').stat().st_ino
        with counting, patch('machine_audit.scanner.runner.RELIST_NS_PER_ENTRY', 2 * SECOND):
            for run in range(3):
                (other / 'inner' / f'new{run}').write_bytes(b'n')
                self.scan(data, events=events(80 + run), fs=filesystem, floor=25 * SECOND, clock=lambda: spent[0])
                with self.db(data) as db:
                    waiting = db.execute("SELECT count(*) FROM dirty WHERE volume='w'").fetchone()[0]
                    labels = {r[0] for r in db.execute('''SELECT f.label FROM files f JOIN volumes v ON f.volume=v.key
                        AND f.generation IN (v.generation,v.delta_generation) WHERE v.id='w' AND f.parent=?''', (inner,))}
                self.assertEqual(waiting, 0, f'run {run + 1}: the quiet volume deferred its only change')
                self.assertIn(f'new{run}', labels, f'run {run + 1}: the quiet volume re-listed nothing')

    def test_an_update_beside_a_staged_walk_is_admitted_near_the_stores_real_size(self):
        # Big Mac, Nov 12 5:56 AM PT: re-queuing 28 k deferred folders in one write
        # reserved 16 pages a row, 1.8 GB beside 7.7 GB managed, and the cap refused the
        # walking volume's update; it then had no update until its walk would publish.
        for i in range(1200):(self.tree / 'many' / str(i)).mkdir(parents=True)
        data = self.updating_store()
        with self.db(data) as db:
            db.execute('UPDATE volumes SET full_ns=?', (self.now - 8 * 24 * HOUR,))
        self.scan(data)  # The walk starts and pauses: its staged generation stands.
        self.assertIsNotNone(self.walk_row(data))
        store = MapStore(data / 'scanner.sqlite', Budget(data, free_margin_bytes=0))
        store.volume = 'v'
        store.defer_all([(str((self.tree / 'many' / str(i)).stat().st_ino), self.now, False) for i in range(1200)])
        # A store larger than the write declares, as Big Mac's 4 GB one is: admission
        # then reserves the declared pages, not the whole file.
        with store.write(rows=2, payload_bytes=96 * 1024**2):
            store.db.execute('CREATE TABLE padding(value BLOB)')
            store.db.execute('INSERT INTO padding VALUES(zeroblob(?))', (96 * 1024**2,))
        store.close()
        limit = Budget(data).usage()['bytes'] + 40 * 1024**2  # Room for counted writes, not a 79 MB one.
        # A share that covers none of them: every deferred folder waits again.
        result = self.scan(data, events=self.events([], 90), floor=10**6, limit=limit)
        update = result['volumes'][0]
        self.assertEqual((update['action'], update['status']), ('update', 'observed'))
        # The walk supersedes the whole queue: it is dropped in bounded writes, and counted
        # in the run row even when the update's merge pauses at the allowance.
        self.assertEqual(update['superseded'], 1200)
        with self.db(data) as db:
            recorded = json.loads(db.execute('SELECT volumes FROM runs ORDER BY started_ns DESC LIMIT 1').fetchone()[0])
        self.assertEqual(recorded[0]['superseded'], 1200)
        self.assertIn('walk_supersedes', update['gaps'])
        with self.db(data) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM dirty WHERE volume='v'").fetchone()[0], 0)

    def test_a_third_due_walk_waits_while_two_are_open(self):
        # Big Mac, Nov 13: a walk waiting for a piece left its allowance to the next
        # volume's walk. With every volume's walk open beside the startup volume's
        # publication, the peak was projected at 9.7 GB of the 10 GB cap.
        mounts = {}
        for name in ('w', 'x'):
            mounts[name] = self.base / f'mount-{name}';(mounts[name] / 'inner').mkdir(parents=True)
            (mounts[name] / 'inner' / 'f').write_bytes(b'o')
        volumes = [self.volume] + [replace(self.volume, id=name, label=name, mount=str(mount)) for name, mount in mounts.items()]
        filesystem = SimpleNamespace(volumes=lambda: volumes, sharing=lambda *args: None)
        data = self.base / 'three'
        self.assertEqual(self.scan(data, force=True, fs=filesystem)['status'], 'observed')
        with self.db(data) as db:
            db.execute('UPDATE volumes SET full_ns=?', (self.now - 8 * 24 * HOUR,))  # Every walk is due.
        spent, counting = self.listing_clock(SECOND // 10)
        # Totals never fit the allowance, so each walk waits a day for its last piece.
        with counting, patch('machine_audit.scanner.runner.TOTALS_NS_PER_FOLDER', 10**15):
            for run in range(2):
                result = self.scan(data, events=self.events([], 50 + run), fs=filesystem, clock=lambda: spent[0])
                with self.db(data) as db:
                    open_walks = {r[0] for r in db.execute('SELECT volume FROM walks')}
                self.assertEqual(len(open_walks), 2, f'run {run + 1}: {open_walks}')
                third, = (v for v in result['volumes'] if v['volume_id'] not in open_walks)
                self.assertEqual(third['walk'], {'state': 'waiting_turn'})
            with self.db(data) as db:
                db.execute('UPDATE walks SET phase_ns=phase_ns-?', (25 * HOUR,))  # A day passes.
            self.scan(data, events=self.events([], 60), fs=filesystem, clock=lambda: spent[0])
        with self.db(data) as db:
            # The two published, and the third took its turn.
            self.assertEqual({r[0] for r in db.execute('SELECT volume FROM walks')}, {third['volume_id']})

    def test_a_walk_piece_runs_when_it_fits_the_allowance_plus_the_walk_floor(self):
        # Big Mac, Nov 13: sharing (127 s) never fit a 120 s allowance and waited a day,
        # though a run may spend the allowance plus the 20 s walk floor.
        files = [p for p in self.tree.rglob('*') if p.is_file()]
        filesystem = SimpleNamespace(volumes=lambda: [replace(self.volume, fs='apfs')],
                                     sharing=lambda fd, name, st: (st.st_ino, None, False),
                                     extents=lambda device, inode: [(inode * 4096, 4096)])
        data = self.base / 'piece'
        self.assertEqual(self.scan(data, force=True, fs=filesystem)['status'], 'observed')
        with self.db(data) as db:
            db.execute('UPDATE volumes SET full_ns=?', (self.now - 8 * 24 * HOUR,))
        spent, counting = self.listing_clock(SECOND // 100)  # Listing the tree: about 1.3 s.
        with counting, patch('machine_audit.scanner.runner.SHARING_NS_PER_HOLDER', 127 * SECOND // len(files)):
            result = self.scan(data, fs=filesystem, floor=120 * SECOND, walk_floor=20 * SECOND, clock=lambda: spent[0])
        self.assertEqual(result['volumes'][0]['walk']['state'], 'published', result['volumes'][0]['walk'])

    def test_a_refused_walk_write_keeps_the_checkpoint_and_resumes(self):
        # A walk dropped on a refusal restarts days of walking (criteria 1 and 2).
        expected = self.reference()
        data = self.base / 'refused'
        original = MapStore.checkpoint
        calls = []
        def checkpoint(store, *args, **kwargs):
            calls.append(1)
            if len(calls) == 2:  # The second checkpoint is refused.
                refusal = BudgetExceeded('sqlite_checkpoint_headroom', 1)
                refusal.retry_bytes = 10**12  # Storage is back under the level by the next run.
                raise refusal
            return original(store, *args, **kwargs)
        with patch.object(MapStore, 'checkpoint', checkpoint):
            while len(calls) < 2:result = self.scan(data, floor=12 * SECOND)
        self.assertEqual(result['volumes'][0]['walk']['state'], 'failed')
        walk = self.walk_row(data)
        self.assertIsNotNone(walk, 'the refused walk was dropped')
        for run in range(30):
            self.scan(data, floor=12 * SECOND)
            after = self.walk_row(data)
            if after is None:break
            self.assertEqual(after['generation'], walk['generation'], 'the walk restarted')
        self.assertEqual(self.published_map(data), expected)

    def test_a_walks_writes_declare_at_most_4096_rows(self):
        # At publication Big Mac's store leaves about 1 GB under the 10 GB cap; a write
        # of 8,192 rows reserved 0.54 GB of it, at 16 pages a row.
        for i in range(9000):(self.tree / 'folder0' / 'sub0' / f'many{i}').write_bytes(b'')
        for i in range(4500):(self.tree / 'deep' / str(i)).mkdir(parents=True)
        declared = []
        original = Budget.sqlite_write
        def counted(budget, *args, rows=None, **kwargs):
            declared.append(rows or 0)
            return original(budget, *args, rows=rows, **kwargs)
        with patch.object(Budget, 'sqlite_write', counted):
            self.assertEqual(self.scan(self.base / 'rows', force=True)['status'], 'observed')
        self.assertLessEqual(max(declared), 4096 + 1, 'a staging flush or totals batch declared more')  # +1: the checkpoint's own row

    def test_a_run_with_room_spends_at_most_85_percent_of_it_plus_the_walk_floor(self):
        # Big Mac, Nov 12 6-8 PM PT: the walk took the whole trailing hour's room and a
        # falling hour went 1-3% over the bar.
        data = self.base / 'room'
        spent, counting = self.listing_clock(SECOND)
        room = 100 * SECOND
        with counting, patch.object(pace, 'room_ns', return_value=room):
            for run in range(3):
                before = spent[0]
                self.scan(data, floor=SECOND, clock=lambda: spent[0])
                # The walk ends its share between folders: at most one folder (6 entries) over.
                self.assertLessEqual(spent[0] - before, 0.85 * room + pace.WALK_FLOOR_NS + 6 * SECOND)
                with self.db(data) as db:
                    self.assertEqual(db.execute('SELECT allowance_ns FROM runs ORDER BY started_ns DESC LIMIT 1').fetchone()[0],
                                     int(0.85 * room))

    def test_a_name_withheld_folder_resumes_by_inode_with_no_name_or_path_stored(self):
        private = self.tree / 'Album.musiclibrary'
        for b in range(8):
            (private / f'withheld{b}').mkdir(parents=True)
            (private / f'withheld{b}' / 'file').write_bytes(b'z' * 4096)
        expected = self.reference()
        data = self.base / 'private'
        seen_private = False
        while True:
            result = self.scan(data, floor=12 * SECOND)
            walk = self.walk_row(data)
            if walk is None:break
            with self.db(data) as db:
                text = ' '.join(str(v) for row in db.execute('SELECT * FROM walks') for v in row if isinstance(v, str))
                pending = db.execute('SELECT pending FROM walks').fetchone()[0] or b''
            for name in ('Album', 'withheld', str(self.base)):
                self.assertNotIn(name, text)
                self.assertNotIn(name.encode(), pending)
            store = MapStore(data / 'scanner.sqlite', Budget(data, free_margin_bytes=0))
            store.walk = walk
            seen_private |= any(private for _, _, private in store.pending())
            store.close()
        self.assertTrue(seen_private, 'no checkpoint held a name-withheld folder')
        self.assertEqual(self.published_map(data), expected)

    def test_run_rows_count_extent_reads(self):
        # The extent cost bound from the physical-space work is checked live by this count.
        files = [p for p in self.tree.rglob('*') if p.is_file()]
        filesystem = SimpleNamespace(volumes=lambda: [replace(self.volume, fs='apfs')],
                                     sharing=lambda fd, name, st: (st.st_ino, None, False),
                                     extents=lambda device, inode: [(inode * 4096, 4096)])
        data = self.base / 'extents'
        self.assertEqual(self.scan(data, force=True, fs=filesystem)['status'], 'observed')
        with self.db(data) as db:
            reads, stages = db.execute('SELECT extent_reads,stage_cpu FROM runs').fetchone()
        self.assertEqual(reads, len(files))
        self.assertTrue({'walk', 'sharing', 'aggregate', 'publication'} <= json.loads(stages).keys())


class AllowanceTests(unittest.TestCase):
    def test_room_is_the_share_of_process_cpu_left_by_the_collector(self):
        cpu = who([dict(signal='process.cpu', entity='program:Example', label='Example', total=20_000 * SECOND),
                   dict(signal='process.cpu', entity='rest:program', label=None, total=10_000 * SECOND)])
        own = {'rows': [dict(signal='self.user_time', label='collector', total=100 * SECOND, scale=1e-09),
                        dict(signal='self.system_time', label='collector', total=50 * SECOND, scale=1e-09),
                        dict(signal='self.user_time', label='scanner', total=900 * SECOND, scale=1e-09)]}
        self.assertEqual(pace.room_from(cpu, own, 0.01), 150 * SECOND)
        self.assertIsNone(pace.room_from(dict(cpu, totals_complete=False), own, 0.01))

    def test_floors_keep_updates_current_and_the_walk_moving(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as root:
            self.assertIsNone(pace.room_ns(root, 10 * HOUR))  # No collector store.
            (Path(root) / 'readings.sqlite').write_bytes(b'not a database')
            self.assertIsNone(pace.room_ns(root, 10 * HOUR))
            self.assertEqual(pace.allowance(root, force=False).limit, pace.FLOOR_NS)
            # A run spends at most 85% of the room: the margin for an hour whose load falls.
            for room, limit in ((300 * SECOND, 255 * SECOND), (100 * SECOND, pace.FLOOR_NS), (-50 * SECOND, pace.FLOOR_NS)):
                with patch.object(pace, 'room_ns', return_value=room):
                    self.assertEqual(pace.allowance(root, force=False).limit, limit)
        self.assertIsNone(pace.allowance('.', force=True).limit)
        ticks = iter(range(0, 10**4))
        with patch.object(pace.Allowance, 'clock', staticmethod(lambda: next(ticks) * SECOND)):
            allowance = pace.Allowance(5 * SECOND)
            while not allowance.exhausted():pass
            # Updates spent the hour; the walk still gets its floor.
            self.assertTrue(allowance.walk_open())
            self.assertEqual(allowance.walk_until - allowance.start, 6 * SECOND + pace.WALK_FLOOR_NS)


if __name__ == '__main__':
    unittest.main()
