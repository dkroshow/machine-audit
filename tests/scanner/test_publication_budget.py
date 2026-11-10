"""Publication must depend on changed rows, not unrelated database size."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from machine_audit.budget import Budget, BudgetExceeded
from machine_audit.scanner.events import next_action
from machine_audit.scanner.native import Volume
from machine_audit.scanner.policy import Policy
from machine_audit.scanner.runner import relist
from machine_audit.scanner.store import MapStore, MapQuery, MergePending, SCHEMA, hot_set
from machine_audit.scanner.walker import Node, Walker


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
        for table in ('nodes', 'files', 'holders', 'streams', 'terms', 'listings'):
            evidence.append([tuple(r) for r in store.db.execute(
                f'SELECT * FROM {table} WHERE generation=? ORDER BY 2,3,4', (generation,))])
        for table in ('points', 'tracked', 'hierarchy_epochs', 'hierarchy_nodes', 'dirty'):
            evidence.append([tuple(r) for r in store.db.execute(f'SELECT * FROM {table} ORDER BY 1,2,3')])
        return evidence

    def test_run_diagnostics_migrate_without_changing_prior_run_evidence(self):
        legacy = (SCHEMA.replace(',\n volume_id TEXT,action TEXT,trigger TEXT,stage TEXT,volumes TEXT', '')
                  .replace(',full_attempt_ns INTEGER,budget_retry_bytes INTEGER,delta_generation INTEGER', '').replace('due_ns INTEGER,recursive INTEGER', 'due_ns INTEGER'))
        original = ('old',1,2,2,123,'start','complete',10,20,30,40,'scheduled')
        with sqlite3.connect(self.path) as db:
            db.executescript(legacy)
            db.execute('DROP INDEX node_excluded')  # The schema repair path, too.
            db.execute('PRAGMA user_version=2')
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',original)
            db.execute("INSERT INTO volumes(id,label,mount,status,lean,full_ns) VALUES('v','Volume','/synthetic','observed',1,17)")
            db.execute("INSERT INTO dirty VALUES('v','9',5)")  # The previous release's positional insert.
        # Schema repair once set format 3 without the delta column, and every merge and
        # hot-set read then failed. The column and the format must commit together.
        write = MapStore.write
        @contextmanager
        def failing(store, *args, **kwargs):
            with write(store, *args, **kwargs):
                yield
                if 'delta_generation' in {r[1] for r in store.db.execute('PRAGMA table_info(volumes)')}:
                    raise RuntimeError('synthetic failure before the migration commits')
        with patch.object(MapStore, 'write', failing), self.assertRaises(RuntimeError):
            MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertNotIn('delta_generation', {r[1] for r in db.execute('PRAGMA table_info(volumes)')})
        store = MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        self.addCleanup(store.close)
        row = tuple(store.db.execute("SELECT * FROM runs WHERE id='old'").fetchone())
        self.assertEqual(row[:12],original)
        self.assertEqual(row[12:],(None,None,None,None,None))
        # Format 4 retires every map for one full walk: its old folder figures meant
        # something else. Run evidence and deferred folders stay.
        self.assertEqual(tuple(store.db.execute('SELECT full_ns,full_attempt_ns,budget_retry_bytes,delta_generation,status FROM volumes').fetchone()),
                         (None,None,None,None,'awaiting_physical_walk'))
        self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 4)
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
        store.finish_volume(self.volume, None, [], full=True, lean=True, bookmark=10)
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
            store.finish_volume(self.volume, None, [], full=False, lean=True, bookmark=11)
        self.assertEqual(store.run_context['stage'], 'publication')
        self.assertEqual(self.published(store), old, 'a refused publication changed published evidence')
        budget.limit_bytes = limit
        result = store.finish_volume(self.volume, None, [], full=False, lean=True, bookmark=11)
        store.progress(status='complete')
        self.assertEqual(result['physical'], 3*GB)
        self.assertEqual(MapQuery(self.path).query()['rows'][0]['physical'], 3*GB)
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

    def walked(self, tree, data, *, padding=0):
        """A full map of a real tree, then a legacy-scope deferral due on `big` so the
        next update re-lists it recursively, as Big Mac's deferred folders were."""
        volume = Volume('v', 'Synthetic', str(tree), 'hfs', tree.stat().st_dev, 10**12, 1, 1, 10**9)
        policy = Policy(home=str(tree.parent), homes=[])
        store = MapStore(data/'scanner.sqlite', Budget(data, free_margin_bytes=0))
        store.prepare(volume);walker = Walker(volume, policy, store);walker.walk()
        store.finish_volume(volume, None, walker.gaps, full=True, lean=True, bookmark=1)
        with store.write(rows=2, payload_bytes=padding):  # Retained history the bound must copy.
            store.db.execute('CREATE TABLE padding(value BLOB)')
            store.db.execute('INSERT INTO padding VALUES(zeroblob(?))', (padding,))
        with store.write(rows=1):
            store.db.execute("INSERT INTO dirty(volume,id,due_ns) VALUES('v',?,0)", (str((tree/'big').stat().st_ino),))
        return store, volume, policy

    def counted(self, store):
        """Publication and merge admissions: stage, declared rows, SQLite changes,
        declared pages, WAL frames."""
        seen = []
        original = store.budget.sqlite_write
        @contextmanager
        def checked(db, path, *args, **kwargs):
            with original(db, path, *args, **kwargs) as admitted:
                before = db.total_changes
                yield admitted
                if store.run_context['stage'] in ('publication', 'merge'):
                    wal = Path(str(path)+'-wal')
                    seen.append((store.run_context['stage'], kwargs['rows'], db.total_changes-before, admitted['dirty_pages'],
                                 max(0, (wal.stat().st_size-32)//(admitted['page_size']+24))))
        store.budget.sqlite_write = checked
        return seen

    def update(self, store, volume, policy, batch=None):
        store.prepare(volume, full=False)
        if batch:
            # Model Big Mac's staging batches (8,192 operations against 846,874 pages)
            # at this file size, so staging stays far below the database.
            queue = store._queue
            def batched(query, args):
                queue(query, args)
                if len(store.buffer) >= batch:store.flush()
            store._queue = batched
        walker = Walker(volume, policy, store)
        self.assertTrue(relist(store, walker, policy, volume, 1, []))
        return walker

    def test_an_unchanged_relisted_folder_publishes_within_its_counted_changes(self):
        # Big Mac's main update was refused at publication; what it declared is unknown.
        # This mechanism can cause that refusal: each re-listed folder declared a two-row
        # REPLACE for its listing and three point and hierarchy slots, so unchanged
        # folders alone reserved a second copy of the whole file.
        tree = self.root/'tree';data = self.root/'data';data.mkdir()
        for d in range(600):(tree/'big'/f'd{d}').mkdir(parents=True);(tree/'big'/f'd{d}'/'f').write_bytes(b'x')
        store, volume, policy = self.walked(tree, data, padding=60*1024**2)
        store.close()
        (data/'synthetic-readings.sqlite').write_bytes(bytes(30*1024**2))  # Another store's share of the cap.
        budget = Budget(data, free_margin_bytes=0)
        budget.limit_bytes = budget.usage()['bytes']+50*1024**2  # Room for the real work, not a second copy.
        store = MapStore(data/'scanner.sqlite', budget);self.addCleanup(store.close)
        seen = self.counted(store)
        walker = self.update(store, volume, policy, batch=64)
        store.finish_volume(volume, None, walker.gaps, full=False, lean=True, bookmark=2)
        for stage, declared, changes, pages, frames in seen:
            self.assertGreaterEqual(declared, changes);self.assertLessEqual(frames, pages)
        (_, declared, changes, pages, frames), = [entry for entry in seen if entry[0]=='publication']
        self.assertLess(pages, store.db.execute('PRAGMA page_count').fetchone()[0], 'the bound was the whole file')
        self.assertGreaterEqual(declared, changes);self.assertLessEqual(frames, pages)
        listed = store.db.execute('SELECT count(*) FROM listings l JOIN volumes v ON l.generation=v.generation AND l.volume=v.key').fetchone()[0]
        self.assertEqual((listed, store.db.execute('SELECT count(*) FROM dirty').fetchone()[0]), (602, 0))

    def test_a_mixed_update_publishes_what_a_full_walk_of_the_same_tree_maps(self):
        # Published rows are copied by one insert-or-update each. Changed, added,
        # deleted, moved, removed and hard-linked entries, and tracked folders, must
        # publish exactly what an independent full walk maps, after a failure that
        # rolls back.
        tree = self.root/'tree';data = self.root/'data';data.mkdir()
        big = tree/'big'
        for d in range(30):(big/f'd{d}').mkdir(parents=True);(big/f'd{d}'/'f').write_bytes(b'x'*(d+1))
        os.link(big/'d1'/'f', big/'d2'/'linked')
        store, volume, policy = self.walked(tree, data)
        self.addCleanup(store.close)
        with store.write(rows=10):  # Folders tracked earlier at 2 GB; d20 is later removed.
            for d in (3,4,5,20,21):
                ino = str((big/f'd{d}').stat().st_ino)
                store.db.execute("INSERT INTO tracked VALUES('v',?,?)", (ino, str(big.stat().st_ino)))
                store.db.execute("INSERT INTO points VALUES('v',?,1,?,'physical',2000000000,'observed')", (ino, f'd{d}'))
        tracked = {d:str((big/f'd{d}').stat().st_ino) for d in (3,4,5,20,21)}
        for d in (10,11):(big/f'd{d}'/'f').write_bytes(b'y'*20000)            # resized
        (big/'d12'/'f').unlink()                                               # deleted
        (big/'d13'/'g').write_bytes(b'z')                                      # added
        (big/'d20'/'f').unlink();(big/'d20').rmdir()                           # removed, tracked
        os.rename(big/'d22', big/'d23'/'moved')                                # moved
        os.link(big/'d1'/'f', big/'d24'/'linked')                              # another shared holder
        def published():
            return [store.db.execute(f'SELECT * FROM {t} WHERE generation=(SELECT generation FROM volumes) ORDER BY 3,4').fetchall()
                    for t in ('nodes','files','holders','streams','terms','listings')]
        before = published()
        walker = self.update(store, volume, policy)
        with patch('machine_audit.scanner.store.save_hierarchy', side_effect=RuntimeError('synthetic publication failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic publication failure'):
                store.finish_volume(volume, None, walker.gaps, full=False, lean=True, bookmark=2)
        self.assertEqual(published(), before, 'a failed publication changed the published map')
        seen = self.counted(store)
        store.finish_volume(volume, None, walker.gaps, full=False, lean=True, bookmark=2)
        for stage, declared, changes, pages, frames in seen:
            self.assertGreaterEqual(declared, changes);self.assertLessEqual(frames, pages)
        (_, declared, changes, pages, frames), = [entry for entry in seen if entry[0]=='publication']
        self.assertGreaterEqual(declared, changes);self.assertLessEqual(frames, pages)
        reference = MapStore(self.root/'reference'/'scanner.sqlite', Budget(self.root/'reference', free_margin_bytes=0))
        self.addCleanup(reference.close)
        reference.prepare(volume);walker = Walker(volume, policy, reference);walker.walk()
        reference.finish_volume(volume, None, walker.gaps, full=True, lean=True, bookmark=2)
        def mapped(map_store):
            # Rows by identity; the generation and listing times differ by design.
            g, = map_store.db.execute('SELECT generation FROM volumes').fetchone()
            rows = [sorted(tuple(r)[1:] for r in map_store.db.execute(f'SELECT * FROM {t} WHERE generation=?', (g,)))
                    for t in ('nodes','files','holders','streams','terms')]
            return rows+[sorted((r[2],r[4]) for r in map_store.db.execute('SELECT * FROM listings WHERE generation=?', (g,)))]
        self.assertEqual(mapped(store), mapped(reference))
        self.assertGreater(len(mapped(store)[2]), 2, 'the fixture lost its shared holders')
        latest = {}
        for identity, size, status in store.db.execute('SELECT id,size,status FROM points ORDER BY observed_ns'):
            latest[identity] = (size, status)
        self.assertEqual({d:latest[i][1] for d,i in tracked.items()},
                         {3:'below_threshold',4:'below_threshold',5:'below_threshold',20:'not_observed',21:'below_threshold'})

    def readers(self, path, volume_id='v'):
        """What the public readers return for one volume: totals, every page of the
        children of each folder of interest, and the hot set."""
        query = MapQuery(path)
        totals = [{k:row[k] for k in ('id','listed','physical','valid','mounted')} for row in query.query()['rows'] if row['id']==volume_id]
        pages = {}
        frontier = [None]
        while frontier:
            parent = frontier.pop()
            listed, cursor = [], None
            while True:
                page = query.query('children', volume_id=volume_id, parent_id=parent, limit=7, **({'cursor':cursor} if cursor else {}))
                listed += [(r['id'],r['label'],r['kind'],r['listed'],r['physical'],r['status']) for r in page['rows']]
                cursor = page['next_cursor']
                if not cursor:break
            pages[parent] = sorted(listed)
            frontier += [r[0] for r in listed if r[2]!='file']
        return totals, pages, sorted(r for r in hot_set(path, 100) if r[0]==volume_id), page['coverage']['excluded_roots']

    def history(self, store):
        return [store.db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in ('points','tracked','hierarchy_epochs','hierarchy_nodes')]

    def test_a_published_delta_reads_the_same_through_every_merge_batch_and_resumes(self):
        # An update now publishes its staged rows as a delta and merges them into the base
        # in bounded batches. Readers must see the old map before publication and exactly
        # the new one at every committed batch, across a crash, another volume's
        # preparation and the final clear, with history written once.
        tree = self.root/'tree';data = self.root/'data';data.mkdir()
        big = tree/'big'
        for d in range(40):(big/f'd{d}').mkdir(parents=True);(big/f'd{d}'/'f').write_bytes(b'x'*(d+1))
        (big/'reset').mkdir()
        for i in range(150):(big/'reset'/f'r{i}').write_bytes(b'r')
        os.link(big/'d1'/'f', big/'d2'/'linked')
        (big/'d3'/'s').write_bytes(b's'*5000);os.link(big/'d3'/'s', big/'d4'/'s2')  # a stream removed later
        store, volume, policy = self.walked(tree, data)
        self.addCleanup(store.close)
        with store.write(rows=6):  # Folders tracked earlier at 2 GB; d7 is later removed.
            for d in (5,6,7):
                ino = str((big/f'd{d}').stat().st_ino)
                store.db.execute("INSERT INTO tracked VALUES('v',?,?)", (ino, str(big.stat().st_ino)))
                store.db.execute("INSERT INTO points VALUES('v',?,1,?,'physical',2000000000,'observed')", (ino, f'd{d}'))
        other = self.root/'other';(other/'o').mkdir(parents=True)
        second = Volume('w', 'Other', str(other), 'hfs', other.stat().st_dev, 10**12, 1, 1, 10**9)
        store.prepare(second);walker = Walker(second, policy, store);walker.walk()
        store.finish_volume(second, None, walker.gaps, full=True, lean=True, bookmark=1)
        old = self.readers(store.path)
        for i in range(140):(big/'reset'/f'r{i}').unlink()                     # one marker, several batches
        (big/'reset'/'new').write_bytes(b'n')
        for d in (10,11):(big/f'd{d}'/'f').write_bytes(b'y'*20000)            # resized
        (big/'d12'/'f').unlink();(big/'d13'/'g').write_bytes(b'z')             # deleted, added
        (big/'d7'/'f').unlink();(big/'d7').rmdir()                             # removed, tracked
        os.rename(big/'d20', big/'d21'/'moved')                                # moved
        (big/'d3'/'s').unlink();(big/'d4'/'s2').unlink()                       # removed charge
        os.link(big/'d1'/'f', big/'d24'/'linked')                              # another shared holder
        reference = MapStore(self.root/'reference'/'scanner.sqlite', Budget(self.root/'reference', free_margin_bytes=0))
        self.addCleanup(reference.close)
        reference.prepare(volume);walker = Walker(volume, policy, reference);walker.walk()
        reference.finish_volume(volume, None, walker.gaps, full=True, lean=True, bookmark=2)
        new = self.readers(reference.path)
        self.assertNotEqual(new, old)
        walker = self.update(store, volume, policy)
        self.assertEqual(self.readers(store.path), old, 'staging changed the published map')
        seen = self.counted(store)
        views, cursors, left = [], [], []
        original = MapStore.write
        reset = """SELECT count(*) FROM files f JOIN volumes v ON f.generation=v.generation AND f.volume=v.key
            WHERE f.parent=(SELECT id FROM nodes n WHERE n.generation=v.generation AND n.volume=v.key AND n.label='reset')"""
        @contextmanager
        def watched(merging, *args, **kwargs):
            with original(merging, *args, **kwargs):yield
            if merging.run_context['stage'] == 'merge':
                views.append(self.readers(merging.path))
                remaining = merging.db.execute(reset).fetchone()[0]
                if not left and 11 < remaining < 150:  # some of the one marker's rows deleted
                    left.append(remaining)
                    page = MapQuery(merging.path).query('children', volume_id='v', parent_id=str((big/'reset').stat().st_ino), limit=3)
                    cursors.append((page['rows'], page['next_cursor']))
                    raise RuntimeError('synthetic crash inside one marker')
        with patch('machine_audit.scanner.store.MERGE_ROWS', 64), patch.object(MapStore, 'write', watched):
            with self.assertRaises(MergePending):
                store.finish_volume(volume, None, walker.gaps, full=False, lean=True, bookmark=2)
            delta = store.db.execute("SELECT delta_generation FROM volumes WHERE id='v'").fetchone()[0]
            self.assertIsNotNone(delta)
            self.assertEqual(left, [store.db.execute(reset).fetchone()[0]])
            written = self.history(store)
            store.close()
            store = MapStore(store.path, Budget(data, free_margin_bytes=0))  # a later run resumes
            self.addCleanup(store.close)
            resumed = self.counted(store)
            # Another volume's preparation prunes staging, never a published delta.
            with self.assertRaisesRegex(ValueError, 'merge_pending'):store.prepare(volume, full=False)
            store.prepare(second, full=False)
            self.assertEqual(store.db.execute('SELECT status FROM generations WHERE id=?', (delta,)).fetchone()[0], 'delta')
            self.assertEqual(store.finish_merges(), {})
        seen += resumed
        self.assertEqual(store.db.execute(reset).fetchone()[0], 11)  # ten kept and one added
        self.assertEqual(views[0], new)
        self.assertGreater(len(views), 3, 'the fixture did not span several batches')
        self.assertTrue(all(view == new for view in views), 'a committed merge batch changed the published map')
        self.assertEqual(self.readers(store.path), new)
        self.assertEqual(store.db.execute("SELECT delta_generation FROM volumes WHERE id='v'").fetchone()[0], None)
        self.assertEqual(store.db.execute('SELECT status FROM generations WHERE id=?', (delta,)).fetchone()[0], 'building')
        self.assertEqual(self.history(store), written, 'resuming the merge wrote history again')
        rows, cursor = cursors[0]
        while cursor:  # A cursor taken mid-merge pages on after the merge.
            page = MapQuery(store.path).query('children', volume_id='v', parent_id=str((big/'reset').stat().st_ino), limit=3, cursor=cursor)
            rows += page['rows'];cursor = page['next_cursor']
        self.assertEqual(sorted((r['id'],r['label'],r['kind'],r['listed'],r['physical'],r['status']) for r in rows),
                         new[1][str((big/'reset').stat().st_ino)])
        latest = {}
        for identity, status in store.db.execute('SELECT id,status FROM points ORDER BY observed_ns'):latest[identity] = status
        self.assertEqual(sorted(latest.values()), ['below_threshold','below_threshold','not_observed'])
        for stage, declared, changes, pages, frames in seen:
            self.assertGreaterEqual(declared, changes);self.assertLessEqual(frames, pages)
        self.assertEqual({stage for stage, *_ in seen}, {'publication','merge'})

    def test_a_large_genuine_update_publishes_through_bounded_merge_batches(self):
        # Big Mac's second update was refused at publication: its genuine changes made
        # one counted transaction larger than the room left beside the other stores.
        # Publication is now small, and the merge is bounded by its batches.
        tree = self.root/'tree';data = self.root/'data';data.mkdir()
        for d in range(600):(tree/'big'/f'd{d}').mkdir(parents=True);(tree/'big'/f'd{d}'/'f').write_bytes(b'x')
        store, volume, policy = self.walked(tree, data, padding=60*1024**2)
        store.close()
        for d in range(600):(tree/'big'/f'd{d}'/'f').write_bytes(b'y'*(4096*(1+d%3)))  # every file changed
        for d in range(550,600):
            (tree/'big'/f'd{d}'/'f').unlink();(tree/'big'/f'd{d}').rmdir()          # some folders removed
        (data/'synthetic-readings.sqlite').write_bytes(bytes(30*1024**2))
        budget = Budget(data, free_margin_bytes=0)
        budget.limit_bytes = budget.usage()['bytes']+50*1024**2
        store = MapStore(data/'scanner.sqlite', budget);self.addCleanup(store.close)
        seen = self.counted(store)
        walker = self.update(store, volume, policy, batch=64)
        with patch('machine_audit.scanner.store.MERGE_ROWS', 64):
            store.finish_volume(volume, None, walker.gaps, full=False, lean=True, bookmark=2)
        self.assertIsNone(store.db.execute("SELECT delta_generation FROM volumes WHERE id='v'").fetchone()[0])
        changed = store.db.execute("""SELECT count(*) FROM files f JOIN volumes v ON f.generation=v.generation AND f.volume=v.key
                                      WHERE f.allocated>=4096""").fetchone()[0]
        self.assertEqual(changed, 550)
        for stage, declared, changes, pages, frames in seen:
            self.assertGreaterEqual(declared, changes);self.assertLessEqual(frames, pages)
        self.assertGreater(sum(1 for entry in seen if entry[0]=='merge'), 10)

    def test_the_hot_set_reads_its_format_and_map_from_one_snapshot(self):
        # The hot set read the format, then the map in a later snapshot. A store migrated
        # and partly merged in between gave one old and one new path.
        store = MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        store.prepare(self.volume)
        self.node(store, 0)
        for identity, label in (('2','A'), ('3','B')):
            store.directory(Node(identity, '1', label, 'directory', 2*GB, 2*GB, 'observed', None, 0))
        store.finish_volume(self.volume, None, [], full=True, lean=True, bookmark=1)
        store.close()
        with sqlite3.connect(self.path) as db:  # Format 2, as the previous release left it.
            db.execute('ALTER TABLE volumes DROP COLUMN delta_generation');db.execute('PRAGMA user_version=2')
        old = hot_set(self.path, 10)
        def race():
            # Another connection migrates, publishes a delta renaming both folders and
            # merges one. A product writer's admission would be refused while the snapshot
            # pins its checkpoint.
            with connect(self.path) as db:
                base = db.execute("SELECT generation FROM volumes WHERE id='v'").fetchone()[0]
                db.execute('ALTER TABLE volumes ADD COLUMN delta_generation INTEGER');db.execute('PRAGMA user_version=3')
                db.execute('CREATE TEMP TABLE staged AS SELECT * FROM nodes WHERE generation=?', (base,))
                db.execute("UPDATE staged SET generation=?, label='New '||label WHERE id IN (2,3)", (base+1,))
                db.execute('DELETE FROM staged WHERE generation=?', (base,))
                db.execute('INSERT INTO nodes SELECT * FROM staged')
                db.execute("UPDATE volumes SET delta_generation=? WHERE id='v'", (base+1,))
                db.execute("UPDATE nodes SET label='New A' WHERE generation=? AND id=2", (base,))
        connect, opened = sqlite3.connect, []
        class Gate:  # The hot set's own connection: the race runs right after its format read.
            def __init__(self, db):self.db = db
            def close(self):self.db.close()
            def execute(self, sql, *args):
                cursor = self.db.execute(sql, *args)
                if sql != 'PRAGMA user_version':return cursor
                version = cursor.fetchone();race()
                return SimpleNamespace(fetchone=lambda:version)
        def opening(*args, **kwargs):
            opened.append(1)
            return Gate(connect(*args, **kwargs)) if len(opened) == 1 else connect(*args, **kwargs)
        with patch('machine_audit.scanner.store.sqlite3.connect', opening):
            raced = hot_set(self.path, 10)
        new = hot_set(self.path, 10)
        self.assertNotEqual(old, new)
        self.assertIn(raced, (old, new), 'the hot set mixed two published maps')

    def test_a_publication_whose_write_fails_after_commit_stands_through_a_refused_clear(self):
        # The write that publishes an update failed after its commit, while releasing its
        # reservation. The run's failure handler then marked the published volume failed.
        store = MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        self.addCleanup(store.close)
        store.prepare(self.volume)
        self.node(store, 0)
        for identity, label in (('2','A'), ('3','B')):
            store.directory(Node(identity, '1', label, 'directory', 2*GB, 2*GB, 'observed', None, 0))
        store.finish_volume(self.volume, None, [], full=True, lean=True, bookmark=1)
        store.start('scheduled')
        store.prepare(self.volume, full=False)
        for identity, label in (('2','New A'), ('3','New B')):
            store.directory(Node(identity, '1', label, 'directory', 2*GB, 2*GB, 'observed', None, 0))
        unlink, failed = Path.unlink, []
        def releasing(path, *args, **kwargs):
            unlink(path, *args, **kwargs)
            if path.suffix == '.claim' and not failed and \
               store.db.execute("SELECT delta_generation FROM volumes WHERE id='v'").fetchone()[0] is not None:
                failed.append(path);raise OSError('synthetic failure releasing the reservation')
        with patch.object(Path, 'unlink', releasing), self.assertRaises(OSError):
            store.finish_volume(self.volume, None, [], full=False, lean=True, bookmark=2)
        store.mark_failed('os_error_OSError')  # what the run's failure handler does
        state = "SELECT status,bookmark,budget_retry_bytes,delta_generation FROM volumes WHERE id='v'"
        published = tuple(store.db.execute(state).fetchone())
        self.assertEqual(published[:3], ('observed', 2, None))
        written, view = self.history(store), self.readers(self.path)
        self.assertEqual([r[3] for r in view[2]], ['/synthetic', '/synthetic/New A', '/synthetic/New B'])
        store.close()
        # Every row merged, then the write that clears the delta is refused.
        store = MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        write = store.write
        def refusing(*args, **kwargs):
            if store.run_context['stage'] == 'merge' and store.db.execute("""SELECT count(*) FROM nodes n JOIN volumes v
                    ON n.generation=v.generation AND n.volume=v.key WHERE n.label LIKE 'New %'""").fetchone()[0] == 2:
                store.budget.limit_bytes = store.budget.usage()['bytes']
            return write(*args, **kwargs)
        with patch.object(store, 'write', refusing):
            self.assertEqual(store.finish_merges()['v'].code, 'blocked_managed_storage_limit')
        self.assertEqual((tuple(store.db.execute(state).fetchone()), self.readers(self.path)), (published, view))
        store.close()
        store = MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        self.addCleanup(store.close)
        self.assertEqual(store.finish_merges(), {})
        self.assertEqual(tuple(store.db.execute(state).fetchone()), published[:3]+(None,))
        self.assertEqual((self.history(store), self.readers(self.path)), (written, view))

    def test_merge_work_grows_linearly_with_its_markers(self):
        # Finding each next marker scanned every remaining marker: 2,000 re-listed
        # folders cost four times the work of 1,000, with nothing in the base to remove.
        steps = []
        for count in (1000, 2000):
            folder = self.root/str(count);folder.mkdir()
            store = MapStore(folder/'scanner.sqlite', Budget(folder, free_margin_bytes=0))
            self.addCleanup(store.close)
            store.prepare(self.volume);self.node(store, 0)
            store.finish_volume(self.volume, None, [], full=True, lean=True)
            with store.write(rows=count+2):  # a published delta that re-lists `count` folders
                store.db.executemany('INSERT INTO reset_dirs VALUES(99,1,?)', ((i,) for i in range(count)))
                store.db.execute("INSERT INTO generations VALUES(99,1,'delta')")
                store.db.execute("UPDATE volumes SET delta_generation=99 WHERE id='v'")
            counted = [0]
            store.db.set_progress_handler(lambda:counted.__setitem__(0, counted[0]+1), 100)
            store.merge_delta('v')
            steps.append(counted[0])
        self.assertLess(steps[1], 2.5*steps[0])

    def test_page_limit_shortened_by_the_cap_is_a_capacity_refusal(self):
        # A transaction that ran out of the page limit the cap had shortened failed
        # as write_failed, outside recovery and its retry gate.
        store = MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        self.addCleanup(store.close)
        with store.write(rows=1):store.db.execute('CREATE TABLE fill(value BLOB)')
        store.budget.limit_bytes = store.budget.usage()['bytes']+2*1024**2
        with self.assertRaisesRegex(BudgetExceeded, 'sqlite_page_limit') as refused:
            with store.write(rows=100_000):store.db.execute('INSERT INTO fill VALUES(zeroblob(?))', (4*1024**2,))
        self.assertEqual(store.db.execute('SELECT count(*) FROM fill').fetchone()[0], 0)
        # How much it lacked is unknown: no invented shortfall or retry level.
        self.assertEqual((refused.exception.shortfall_bytes, refused.exception.retry_bytes), (None, None))
        # Running past pages the writer declared itself is a contract error, not pressure.
        store.budget.limit_bytes = 10**10
        with self.assertRaisesRegex(sqlite3.OperationalError, 'full'):
            with store.write(rows=1):store.db.execute('INSERT INTO fill VALUES(zeroblob(?))', (4*1024**2,))

    def test_quiet_updates_leave_counter_unchanged_and_never_force_full(self):
        store = MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        self.addCleanup(store.close)
        store.prepare(self.volume)
        self.node(store, 2*GB)
        store.finish_volume(self.volume, None, [], full=True, lean=True, bookmark=10)
        for _ in range(3):
            store.prepare(self.volume, full=False)
            store.finish_volume(self.volume, None, [], full=False, lean=True, bookmark=10)
        row = store.db.execute('SELECT * FROM volumes').fetchone()
        self.assertEqual(row['no_progress'], 0)
        self.assertEqual(next_action(now_ns=row['observed_ns']+3600*10**9,
                         full_ns=row['full_ns'], update_ns=row['observed_ns'],
                         bookmark=row['bookmark'], no_progress=row['no_progress']), 'update')
        for _ in range(2):
            store.prepare(self.volume, full=False)
            store.finish_volume(self.volume, None, [], full=False, lean=True,
                                bookmark=10, event_progress=False)
        for _ in range(3):
            store.prepare(self.volume, full=False)
            store.finish_volume(self.volume, None, [], full=False, lean=True, bookmark=10)
        self.assertEqual(store.db.execute('SELECT no_progress FROM volumes').fetchone()[0], 2)
        store.prepare(self.volume, full=False)
        store.finish_volume(self.volume, None, [], full=False, lean=True,
                            bookmark=10, event_progress=True)
        self.assertEqual(store.db.execute('SELECT no_progress FROM volumes').fetchone()[0], 0)

    def test_removed_tracked_subtree_is_included_in_the_points_reservation(self):
        store = MapStore(self.path, Budget(self.root, free_margin_bytes=0))
        self.addCleanup(store.close)
        store.prepare(self.volume)
        self.node(store, 0)
        for identity, parent, size in (('2', '1', 0), ('3', '2', GB), ('4', '2', GB)):
            store.directory(Node(identity, parent, 'Folder '+identity, 'directory', size, size, 'observed', None, 0))
        store.finish_volume(self.volume, None, [], full=True, lean=True)
        self.assertEqual(store.db.execute('SELECT count(*) FROM tracked').fetchone()[0], 4)
        store.prepare(self.volume, full=False)
        store.clear_subtree('2')
        writes = []
        original = store.write

        @contextmanager
        def record(*args, **kwargs):
            before = store.db.total_changes
            with original(*args, **kwargs):yield
            writes.append((kwargs['rows'], store.db.total_changes-before))

        store.write = record
        store.finish_volume(self.volume, None, [], full=False, lean=True)
        # The removed tracked identities' terminal points, the removed rows and the
        # hierarchy must all fit the publication's declared rows.
        declared, changes = writes[-1]
        self.assertGreaterEqual(declared, changes)
        self.assertEqual(len(store.changed), 4)
        for identity in ('2', '3', '4'):
            point = store.db.execute('SELECT size,status FROM points WHERE id=? ORDER BY observed_ns DESC LIMIT 1',
                                     (identity,)).fetchone()
            self.assertEqual(tuple(point), (None, 'not_observed'))
