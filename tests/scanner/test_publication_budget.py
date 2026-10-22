"""Publication must depend on changed rows, not unrelated database size."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest

from machine_audit.budget import Budget
from machine_audit.scanner.accounting import Blocks
from machine_audit.scanner.events import next_action
from machine_audit.scanner.native import Volume
from machine_audit.scanner.store import MapStore, MapQuery
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

    def test_existing_large_store_opens_and_publishes_small_update_with_counted_wal(self):
        store = MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        store.prepare(self.volume)
        self.node(store, 0)
        store.directory(Node('2', '1', 'Child', 'directory', 2*GB, 2*GB, 'observed', None, 0))
        store.directory(Node('3', '1', 'Removed', 'directory', GB, GB, 'observed', None, 0))
        for identity in range(5):
            store.file(str(identity), '2', 'File '+str(identity), 100, 0)
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
        store.begin_directory('2')
        store.directory(Node('2', '1', 'Child', 'directory', 3*GB, 3*GB, 'observed', None, 0))
        store.file('6', '2', 'Replacement', 3*GB, 0)
        store.clear_subtree('3')
        result = store.finish_volume(self.volume, Blocks(), [], full=False, lean=True, bookmark=11)
        store.progress(status='complete')
        self.assertEqual(result['uses'], 3*GB)
        self.assertEqual(MapQuery(self.path).query()['rows'][0]['uses'], 3*GB)
        self.assertEqual(store.db.execute('SELECT count(*) FROM files').fetchone()[0], 1)
        self.assertEqual(store.db.execute('SELECT count(*) FROM holders').fetchone()[0], 0)
        self.assertEqual(store.db.execute('SELECT status FROM points WHERE id=? ORDER BY observed_ns DESC LIMIT 1',
                                         ('3',)).fetchone()[0], 'not_observed')
        self.assertEqual(store.db.execute('SELECT length(value) FROM padding').fetchone()[0], 8*1024**2)
        self.assertLessEqual(budget.usage()['bytes'], budget.limit_bytes)
        self.assertTrue(admissions)
        self.assertLess(max(rows for rows, _ in admissions), 1000)

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
