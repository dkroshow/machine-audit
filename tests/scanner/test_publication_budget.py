"""Publication must depend on changed rows, not unrelated database size."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest

from machine_audit.budget import Budget, BudgetExceeded
from machine_audit.scanner.accounting import Blocks
from machine_audit.scanner.events import next_action
from machine_audit.scanner.native import Volume
from machine_audit.scanner.store import MapStore, MapQuery, SCHEMA
from machine_audit.scanner.walker import Node


SCRATCH = Path(__file__).parent / '.tmp'
SCRATCH.mkdir(exist_ok=True)
GB = 1_000_000_000


class PublicationBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'scanner.sqlite'
        self.volume = Volume('v', 'Synthetic', '/synthetic', 'hfs', 1,
                             100*GB, 20*GB, 20*GB, 80*GB)

    def node(self, store, size):
        store.directory(Node('1', None, 'Root', 'directory', size, size, 'observed', None, 0))

    @staticmethod
    def published(store):
        """Everything a reader can see: pointer, published map rows and history."""
        generation = store.db.execute("SELECT generation FROM volumes WHERE id='v'").fetchone()[0]
        evidence = [tuple(store.db.execute("SELECT * FROM volumes WHERE id='v'").fetchone())]
        for table in ('nodes', 'files', 'holders', 'charges', 'listings'):
            evidence.append([tuple(r) for r in store.db.execute(
                f'SELECT * FROM {table} WHERE generation=? ORDER BY 2,3,4', (generation,))])
        for table in ('points', 'tracked', 'hierarchy_epochs', 'hierarchy_nodes', 'dirty'):
            evidence.append([tuple(r) for r in store.db.execute(f'SELECT * FROM {table} ORDER BY 1,2,3')])
        return evidence

    def test_run_diagnostics_migrate_without_changing_prior_run_evidence(self):
        legacy = (SCHEMA.replace(',\n volume_id TEXT,action TEXT,trigger TEXT,stage TEXT,volumes TEXT', '')
                  .replace(',full_attempt_ns INTEGER,budget_retry_bytes INTEGER', '').replace('due_ns INTEGER,recursive INTEGER', 'due_ns INTEGER'))
        original = ('old',1,2,2,123,'start','complete',10,20,30,40,'scheduled')
        with sqlite3.connect(self.path) as db:
            db.executescript(legacy)
            db.execute('PRAGMA user_version=2')
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',original)
            db.execute("INSERT INTO volumes(id,label,mount,status,lean,full_ns) VALUES('v','Volume','/synthetic','observed',1,17)")
            db.execute("INSERT INTO dirty VALUES('v','9',5)")  # The previous release's positional insert.
        store = MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        self.addCleanup(store.close)
        row = tuple(store.db.execute("SELECT * FROM runs WHERE id='old'").fetchone())
        self.assertEqual(row[:12],original)
        self.assertEqual(row[12:],(None,None,None,None,None))
        self.assertEqual(tuple(store.db.execute('SELECT full_ns,full_attempt_ns,budget_retry_bytes FROM volumes').fetchone()),(17,None,None))
        self.assertEqual(tuple(store.db.execute('SELECT * FROM dirty').fetchone()),('v','9',5,None))
        store.start('scheduled')
        store.phase('walk',volume_id='v',action='full',trigger='event_kernel_drop')
        self.assertEqual(tuple(store.db.execute('SELECT volume_id,action,trigger,stage FROM runs WHERE id=?',
                                              (store.run,)).fetchone()),('v','full','event_kernel_drop','walk'))

    def test_existing_large_store_publishes_a_restaged_folder_by_its_changes_with_counted_wal(self):
        # Big Mac refused every hourly update at publication: each unchanged file
        # of a re-listed folder counted as a copy, the WAL bound grew to the whole
        # database, and the cap refused it. Only changed rows may count.
        store = MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        store.prepare(self.volume)
        self.node(store, 0)
        store.directory(Node('2', '1', 'Child', 'directory', 2*GB, 2*GB, 'observed', None, 0))
        store.directory(Node('3', '1', 'Removed', 'directory', GB, GB, 'observed', None, 0))
        for identity in range(5):
            store.file(str(identity), '2', 'File '+str(identity), 100, 0)
        kept = [(str(i), '2', 'Kept '+str(i), 4096, i) for i in range(10, 3010)]
        for row in kept:store.file(*row)
        store.holder('0', '2', 10, 100, None, False)
        store.finish_volume(self.volume, Blocks(), [], full=True, lean=True, bookmark=10)
        # The inert payload models retained history. It must not reserve a
        # second copy when one folder changes or existing schema is reopened.
        with store.write(rows=2, payload_bytes=8*1024**2):
            store.db.execute('CREATE TABLE padding(value BLOB)')
            store.db.execute('INSERT INTO padding VALUES(zeroblob(?))', (8*1024**2,))
        store.close()
        size = sum(p.stat().st_size for p in self.root.iterdir())
        budget = Budget(self.root, limit_bytes=int(size/.64), free_margin_bytes=0)
        original = budget.sqlite_write
        admissions = []

        @contextmanager
        def checked(db, path, *args, **kwargs):
            self.assertIsNotNone(kwargs.get('rows'), 'uncounted scanner write')
            before = db.total_changes
            with original(db, path, *args, **kwargs) as admitted:
                yield admitted
                self.assertLessEqual(db.total_changes-before, kwargs['rows'])
                wal = Path(str(path)+'-wal')
                frames = max(0, (wal.stat().st_size-32)//(admitted['page_size']+24))
                self.assertLessEqual(frames, admitted['dirty_pages'])
                admissions.append((kwargs['rows'], frames))

        budget.sqlite_write = checked
        store = MapStore(self.path, budget)
        self.addCleanup(store.close)
        store.start('scheduled')
        store.prepare(self.volume, full=False)
        old = self.published(store)
        store.begin_directory('2')
        store.directory(Node('2', '1', 'Child', 'directory', 3*GB, 3*GB, 'observed', None, 0))
        # Staging batches stay far below the database, as Big Mac's 8,192-row
        # batches do against its 826,826 pages.
        for first in range(0, len(kept), 25):
            for row in kept[first:first+25]:store.file(*row)
            store.flush()
        store.file('6', '2', 'Replacement', 3*GB, 0)
        store.clear_subtree('3')
        limit, budget.limit_bytes = budget.limit_bytes, budget.usage()['bytes']+1_000_000
        with self.assertRaisesRegex(BudgetExceeded, 'sqlite_checkpoint_headroom'):
            store.finish_volume(self.volume, Blocks(), [], full=False, lean=True, bookmark=11)
        self.assertEqual(store.run_context['stage'], 'publication')
        self.assertEqual(self.published(store), old, 'a refused publication changed published evidence')
        budget.limit_bytes = limit
        result = store.finish_volume(self.volume, Blocks(), [], full=False, lean=True, bookmark=11)
        store.progress(status='complete')
        self.assertEqual(result['uses'], 3*GB)
        self.assertEqual(MapQuery(self.path).query()['rows'][0]['uses'], 3*GB)
        published = store.generation
        self.assertEqual(store.db.execute('SELECT count(*) FROM files WHERE generation=?', (published,)).fetchone()[0], 3001)
        self.assertEqual(store.db.execute("SELECT count(*) FROM files WHERE generation=? AND label LIKE 'Kept%'",
                                          (published,)).fetchone()[0], 3000)
        self.assertEqual(store.db.execute('SELECT count(*) FROM holders WHERE generation=?', (published,)).fetchone()[0], 0)
        self.assertEqual(store.db.execute('SELECT status FROM points WHERE id=? ORDER BY observed_ns DESC LIMIT 1',
                                         ('3',)).fetchone()[0], 'not_observed')
        self.assertEqual(store.db.execute('SELECT length(value) FROM padding').fetchone()[0], 8*1024**2)
        self.assertLessEqual(budget.usage()['bytes'], budget.limit_bytes)
        self.assertTrue(admissions)
        self.assertLess(max(rows for rows, _ in admissions), 1000)
        # Stopping before cleanup leaves only unpublished rows behind; the next
        # preparation removes them and never touches the published generation.
        new = self.published(store)
        staged = store.db.execute("SELECT id FROM generations WHERE status='building'").fetchall()
        self.assertEqual(len(staged), 1)
        store.close()
        # Prune batches declare 4,096 rows: small against Big Mac's file, not this one.
        store = MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        self.addCleanup(store.close)
        store.prepare(self.volume, full=False)
        self.assertEqual(self.published(store), new)
        for table in ('files', 'nodes', 'reset_dirs', 'removed'):
            self.assertEqual(store.db.execute(f'SELECT count(*) FROM {table} WHERE generation=?',
                                              (staged[0][0],)).fetchone()[0], 0)

    def test_page_limit_shortened_by_the_cap_is_a_capacity_refusal(self):
        # A transaction that ran out of the page limit the cap had shortened failed
        # as write_failed, outside recovery and its retry gate.
        store = MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        self.addCleanup(store.close)
        with store.write(rows=1):store.db.execute('CREATE TABLE fill(value BLOB)')
        filler = self.root/'synthetic-collector.sqlite';filler.write_bytes(bytes(4096))
        store.budget.limit_bytes = store.budget.usage()['bytes']+2*1024**2
        with self.assertRaisesRegex(BudgetExceeded, 'sqlite_page_limit') as refused:
            with store.write(rows=100_000):store.db.execute('INSERT INTO fill VALUES(zeroblob(?))', (4*1024**2,))
        self.assertEqual(store.db.execute('SELECT count(*) FROM fill').fetchone()[0], 0)
        self.assertIsNone(refused.exception.shortfall_bytes, 'the missing amount is unknown')
        self.assertFalse(store.budget.retry_level_reached(refused.exception.retry_bytes))
        filler.unlink()
        self.assertTrue(store.budget.retry_level_reached(refused.exception.retry_bytes))
        # Running past pages the writer declared itself is a contract error, not pressure.
        store.budget.limit_bytes = 10**10
        with self.assertRaisesRegex(sqlite3.OperationalError, 'full'):
            with store.write(rows=1):store.db.execute('INSERT INTO fill VALUES(zeroblob(?))', (4*1024**2,))

    def test_quiet_updates_leave_counter_unchanged_and_never_force_full(self):
        store = MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        self.addCleanup(store.close)
        store.prepare(self.volume)
        self.node(store, 2*GB)
        store.finish_volume(self.volume, Blocks(), [], full=True, lean=True, bookmark=10)
        for _ in range(3):
            store.prepare(self.volume, full=False)
            store.finish_volume(self.volume, Blocks(), [], full=False, lean=True, bookmark=10)
        row = store.db.execute('SELECT * FROM volumes').fetchone()
        self.assertEqual(row['no_progress'], 0)
        self.assertEqual(next_action(now_ns=row['observed_ns']+3600*10**9,
                         full_ns=row['full_ns'], update_ns=row['observed_ns'],
                         bookmark=row['bookmark'], no_progress=row['no_progress']), 'update')
        for _ in range(2):
            store.prepare(self.volume, full=False)
            store.finish_volume(self.volume, Blocks(), [], full=False, lean=True,
                                bookmark=10, event_progress=False)
        for _ in range(3):
            store.prepare(self.volume, full=False)
            store.finish_volume(self.volume, Blocks(), [], full=False, lean=True, bookmark=10)
        self.assertEqual(store.db.execute('SELECT no_progress FROM volumes').fetchone()[0], 2)
        store.prepare(self.volume, full=False)
        store.finish_volume(self.volume, Blocks(), [], full=False, lean=True,
                            bookmark=10, event_progress=True)
        self.assertEqual(store.db.execute('SELECT no_progress FROM volumes').fetchone()[0], 0)

    def test_removed_tracked_subtree_is_included_in_the_points_reservation(self):
        store = MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        self.addCleanup(store.close)
        store.prepare(self.volume)
        self.node(store, 0)
        for identity, parent, size in (('2', '1', 0), ('3', '2', GB), ('4', '2', GB)):
            store.directory(Node(identity, parent, 'Folder '+identity, 'directory', size, size, 'observed', None, 0))
        store.finish_volume(self.volume, Blocks(), [], full=True, lean=True)
        self.assertEqual(store.db.execute('SELECT count(*) FROM tracked').fetchone()[0], 4)
        store.prepare(self.volume, full=False)
        store.clear_subtree('2')
        writes = []
        original = store.write

        @contextmanager
        def record(*args, **kwargs):
            writes.append(kwargs['rows'])
            with original(*args, **kwargs):yield

        store.write = record
        store.finish_volume(self.volume, Blocks(), [], full=False, lean=True)
        # Four changed identities require eight point slots plus four possible
        # terminal points; only the root remains in the staged nodes table.
        # Six map and six hierarchy slots are also needed. Stage markers stay
        # with the unpublished generation for its bounded prune.
        self.assertGreaterEqual(writes[-1], 8+4+6+6)
        self.assertEqual(len(store.changed), 4)
        for identity in ('2', '3', '4'):
            point = store.db.execute('SELECT size,status FROM points WHERE id=? ORDER BY observed_ns DESC LIMIT 1',
                                     (identity,)).fetchone()
            self.assertEqual(tuple(point), (None, 'not_observed'))
