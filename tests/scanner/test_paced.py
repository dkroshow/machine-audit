"""Paced full walks: the hour's CPU allowance, the checkpoint, and the map meanwhile.

Synthetic directory trees in a disposable scratch folder only. The allowance clock
is a counter, so how much a run gets through is fixed, not timing-dependent."""
from dataclasses import replace
from itertools import count
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from machine_audit.budget import Budget
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

    def scan(self, data, *, force=False, full=False, events=None, ticks=None, floor=40 * SECOND, fs=None):
        """One scanner run. Each read of the allowance clock costs a second."""
        data.mkdir(exist_ok=True)
        native = SimpleNamespace(process=lambda pid: SimpleNamespace(proc_start_abstime=1, diskio_byteswritten=0))
        filesystem = fs or SimpleNamespace(volumes=lambda: [self.volume], sharing=lambda *args: None)
        events = events or SimpleNamespace(uuid=lambda device: 'events', current_id=lambda: 20,
                                           replay=lambda device, since: Replay(up_to=20))
        ticks = ticks or count()
        with patch('machine_audit.scanner.runner.Darwin', return_value=native), \
             patch('machine_audit.scanner.runner.MacFilesystem', return_value=filesystem), \
             patch('machine_audit.scanner.runner.IOKit'), \
             patch('machine_audit.scanner.runner.FSEvents', return_value=events), \
             patch('machine_audit.scanner.runner.os.nice'), \
             patch('machine_audit.scanner.runner.Budget', lambda root: Budget(root, free_margin_bytes=0)), \
             patch('machine_audit.scanner.runner.Policy', return_value=Policy(home=str(self.base), homes=[])), \
             patch('machine_audit.scanner.runner.time.time_ns', return_value=self.now), \
             patch.object(pace.Allowance, 'clock', staticmethod(lambda: next(ticks) * SECOND)), \
             patch.object(pace, 'FLOOR_NS', floor), patch.object(pace, 'WALK_FLOOR_NS', floor // 2):
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
        self.assertGreater(len(runs), 2, 'the walk was not paced')
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
        # An allowance the update spends at once: both changed folders wait for the next run.
        second = self.scan(data, events=events, floor=1)
        update = second['volumes'][0]
        self.assertEqual((update['action'], update['status']), ('update', 'observed'))
        self.assertIn('update_deferred', update['gaps'])
        with self.db(data) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM dirty WHERE volume='v'").fetchone()[0], 2)
        # The next update re-lists them first and publishes; the walk goes on beside it.
        third = self.scan(data, events=SimpleNamespace(uuid=lambda device: 'events', current_id=lambda: 22,
                                                       replay=lambda device, since: Replay(up_to=22)))
        self.assertEqual(third['volumes'][0]['action'], 'update')
        with self.db(data) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM dirty WHERE volume='v'").fetchone()[0], 0)
        nodes, files = self.published_map(data)
        self.assertEqual(sum(1 for f in files if f[2] == 'added'), 2)
        self.assertIsNotNone(self.walk_row(data), 'the walk should still be running')
        while self.walk_row(data):self.scan(data)
        with self.db(data) as db:
            # It publishes with its own start bookmark: the next update replays every change since.
            self.assertEqual(db.execute("SELECT bookmark FROM volumes WHERE id='v'").fetchone()[0], 20)

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
            for room, limit in ((300 * SECOND, 300 * SECOND), (-50 * SECOND, pace.FLOOR_NS)):
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
