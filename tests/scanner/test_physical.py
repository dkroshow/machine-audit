"""Physical space: each shared block counts once in every folder holding a copy.

The APFS cases make real clones, hard links and files in a scratch folder and
read their extents with F_LOG2PHYS_EXT. The gigabyte case uses invented ranges
through the same store boundary. No path or name leaves the scratch folder."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from machine_audit.budget import Budget
from machine_audit.scanner.native import Volume
from machine_audit.scanner.policy import Policy
from machine_audit.scanner.runner import relist
from machine_audit.scanner.store import MapStore, MapQuery
from machine_audit.scanner.encoding import source_id
from machine_audit.scanner.walker import Node, Walker

SCRATCH = Path(__file__).parent / '.tmp'
SCRATCH.mkdir(exist_ok=True)
GB = 1_000_000_000
MB = 1 << 20
BLOCK = 4096


def apfs_clones_available():
    if sys.platform != 'darwin':
        return False
    with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
        source = Path(temp) / 'source'
        source.write_bytes(b'x' * BLOCK)
        return subprocess.run(['cp', '-c', str(source), str(Path(temp) / 'clone')], capture_output=True).returncode == 0


def rewrite(path, offset):
    with open(path, 'r+b') as file:
        file.seek(offset)
        file.write(os.urandom(BLOCK))


@unittest.skipUnless(apfs_clones_available(), 'needs APFS clones on macOS')
class ApfsPhysicalTests(unittest.TestCase):
    """An 8 MB original with three clones in a sibling folder, two of them changed
    by one block; a hard link across two folders; an unshared 4 MB file."""

    def setUp(self):
        from machine_audit.sources.native import Darwin
        from machine_audit.scanner.native import MacFilesystem
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.tree = tree = root / 'tree'
        data = root / 'data'
        data.mkdir()
        for folder in ('app', 'clones/c1', 'clones/c2', 'clones/c3', 'links1', 'links2', 'large'):
            (tree / folder).mkdir(parents=True)
        (tree / 'app/big').write_bytes(os.urandom(8 * MB))
        for clone in ('c1', 'c2', 'c3'):
            subprocess.run(['cp', '-c', str(tree / 'app/big'), str(tree / 'clones' / clone / 'big')], check=True)
        rewrite(tree / 'clones/c1/big', 2 * BLOCK)
        rewrite(tree / 'clones/c2/big', 4 * MB)
        (tree / 'links1/a').write_bytes(os.urandom(MB))
        os.link(tree / 'links1/a', tree / 'links2/b')
        (tree / 'large/f').write_bytes(os.urandom(4 * MB))
        self.fs = MacFilesystem(Darwin())
        self.device = tree.stat().st_dev
        self.offsets = set()
        self.volume = Volume('v', 'Synthetic', str(tree), 'apfs', self.device, 10**12, 1, 1, 10**11)
        self.policy = Policy(home=str(root), homes=[], full_disk_access=True)
        self.store = MapStore(data / 'scanner.sqlite', Budget(data, free_margin_bytes=0))
        self.addCleanup(self.store.close)
        self.store.prepare(self.volume, lean=False)
        walker = Walker(self.volume, self.policy, self.store, self.fs)
        walker.walk()
        self.result = self.store.finish_volume(self.volume, self.reader, walker.gaps, full=True, lean=False)

    def reader(self, inode):
        found = self.fs.extents(self.device, int(inode))
        self.offsets.update(start for start, _ in found)
        return found

    def update(self, *changes):
        self.store.prepare(self.volume, full=False, lean=False)
        walker = Walker(self.volume, self.policy, self.store, self.fs)
        self.assertTrue(relist(self.store, walker, self.policy, self.volume, 0,
                               [(str(self.tree / path), recursive) for path, recursive in changes]))
        return self.store.finish_volume(self.volume, self.reader, walker.gaps, full=False, lean=False)

    def folders(self, parent=None):
        """Every folder's row by name, read through the public query."""
        found = {}
        for row in MapQuery(self.store.path).query('children', volume_id='v', parent_id=parent)['rows']:
            if row['kind'] != 'file':
                found[row['label']] = row
                found.update({f"{row['label']}/{k}": v for k, v in self.folders(row['id']).items()})
        return found

    def own(self, *folders):
        """The folders' own entries: their blocks count in their physical space."""
        return sum(os.stat(self.tree / f, follow_symlinks=False).st_blocks * 512 for f in folders)

    def test_full_walk_counts_each_shared_block_once_in_every_folder_holding_a_copy(self):
        # The oldest-copy rule charged every clone's shared blocks to the original's
        # folder, so a clone folder showed only its changed blocks.
        rows = self.folders()
        self.assertEqual(rows['app']['physical'], 8 * MB + self.own('app'))
        for clone in ('c1', 'c2', 'c3'):
            self.assertEqual(rows['clones/' + clone]['physical'], 8 * MB + self.own('clones/' + clone))
        clones = self.own('clones', 'clones/c1', 'clones/c2', 'clones/c3')
        self.assertEqual(rows['clones']['physical'], 8 * MB + 2 * BLOCK + clones)
        self.assertEqual(rows['clones']['listed'], 24 * MB + clones)
        self.assertEqual(rows['clones']['frees'], 2 * BLOCK + clones)
        self.assertEqual((rows['links1']['physical'], rows['links2']['physical']),
                         (MB + self.own('links1'), MB + self.own('links2')))
        everything = self.own('.', 'app', 'clones', 'clones/c1', 'clones/c2', 'clones/c3', 'links1', 'links2', 'large')
        self.assertEqual(self.result['physical'], 8 * MB + 2 * BLOCK + MB + 4 * MB + everything)

    def test_update_measures_a_changed_copy_against_its_unchanged_partners(self):
        before = self.result['physical']
        subprocess.run(['cp', '-c', str(self.tree / 'clones/c3/big'), str(self.tree / 'clones/c4')], check=True)
        rewrite(self.tree / 'clones/c4', BLOCK)
        (self.tree / 'app/big').unlink()
        after = self.update(('clones', False), ('app', False))
        self.assertEqual(after['physical'], before + BLOCK)
        rows = self.folders()
        # Every copy is now inside the clone folder: deleting it frees all of it.
        self.assertEqual(rows['clones']['frees'], rows['clones']['physical'])
        self.assertEqual(rows['app']['physical'], self.own('app'))

    def test_update_without_the_partner_places_only_private_bytes_and_records_the_rest(self):
        before = self.result['physical']
        (self.tree / 'copy').mkdir()
        subprocess.run(['cp', '-c', str(self.tree / 'large/f'), str(self.tree / 'copy/g')], check=True)
        rewrite(self.tree / 'copy/g', 0)
        after = self.update(('', False))
        self.assertEqual(after['physical'], before + BLOCK + self.own('copy'))
        volume = MapQuery(self.store.path).query('volumes')['rows'][0]
        self.assertEqual(volume['unplaced_shared'], 4 * MB - BLOCK)
        self.assertIn('shared_copy_placed_at_full_walk', volume['gaps'])

    def test_no_extent_address_is_saved(self):
        # Criterion: physical addresses are never saved. Every integer in every
        # table is compared with every device offset the run read.
        self.assertTrue(self.offsets)
        db = sqlite3.connect(self.store.path)
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        saved = {value for table in tables for row in db.execute(f'SELECT * FROM {table}')
                 for value in row if isinstance(value, int)}
        db.close()
        self.assertFalse(saved & self.offsets)


class SharedCopiesQueryTests(unittest.TestCase):
    """A 4.5 GB application file and a folder of three changed clones of it, each
    sharing 2.3 GB with the original and holding 2.2 GB of its own: the case of a
    browser's code-signing clones, at the same scale."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.store = MapStore(Path(self.temp.name) / 'scanner.sqlite', Budget(self.temp.name, free_margin_bytes=0))
        self.addCleanup(self.store.close)
        volume = Volume('v', 'Synthetic', '/synthetic', 'apfs', 1, 100 * GB, 80 * GB, 80 * GB, 12 * GB)
        self.store.prepare(volume)
        folder = lambda identity, parent, listed: self.store.directory(
            Node(identity, parent, identity, 'directory', listed, 0, 'observed', None, 0))
        folder('root', None, 0)
        folder('app', 'root', 4_500_000_000)
        folder('clones', 'root', 0)
        self.store.holder('a', 'app', 'original', 4_500_000_000, 0, False)
        extents = {'a': [(0, 4_500_000_000)]}
        for k in (1, 2, 3):
            folder(f'c{k}', 'clones', 4_500_000_000)
            self.store.holder(f'f{k}', f'c{k}', f'changed{k}', 4_500_000_000, 2_200_000_000, True)
            extents[f'f{k}'] = [(0, 2_300_000_000), (10 * k * GB, 2_200_000_000)]
        self.store.finish_volume(volume, extents.__getitem__, [], full=True, lean=True)
        self.query = MapQuery(self.store.path)

    def test_clone_folder_reads_its_physical_space_with_an_asterisk(self):
        answer = self.query.query('children', volume_id='v')
        rows = {r['label']: r for r in answer['rows']}
        clones = rows['clones']
        self.assertEqual((clones['physical'], clones['listed'], clones['frees']),
                         (8_900_000_000, 13_500_000_000, 6_600_000_000))
        self.assertEqual(clones['asterisk'], {'listed': 13_500_000_000, 'frees': 6_600_000_000})
        # The original shares 2.3 GB with the clones: deleting it frees 2.2 GB.
        self.assertEqual(rows['app']['asterisk'], {'frees': 2_200_000_000})
        # One clone: Finder agrees with physical, yet deleting it frees under half.
        one = self.query.query('children', volume_id='v', parent_id='clones')['rows'][0]
        self.assertEqual((one['physical'], one['listed'], one['asterisk']),
                         (4_500_000_000, 4_500_000_000, {'frees': 2_200_000_000}))

    def test_rows_their_shared_line_and_not_placed_add_up_to_used_space(self):
        volume = self.query.query('volumes')['rows'][0]
        self.assertEqual((volume['physical'], volume['not_placed'], volume['volume_used']),
                         (11_100_000_000, 900_000_000, 12 * GB))
        answer = self.query.query('children', volume_id='v')
        shared = answer['folder']['shared_between_rows']
        self.assertEqual(shared, 2_300_000_000)
        self.assertEqual(sum(r['physical'] for r in answer['rows']) - shared + volume['not_placed'], volume['volume_used'])


class SharingUpdateCostTests(unittest.TestCase):
    def test_relisting_one_copy_of_a_large_family_totals_only_it_and_its_ancestors(self):
        # Re-walking one of a browser's 104 code-signing clones re-totalled all
        # 170 k folders of the family (203 s on Big Mac's map): every folder of a
        # touched group was treated as changed, though its terms were not.
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            store = MapStore(Path(temp) / 'scanner.sqlite', Budget(temp, free_margin_bytes=0))
            self.addCleanup(store.close)
            volume = Volume('v', 'Synthetic', '/synthetic', 'apfs', 1, 100 * GB, 50 * GB, 50 * GB, 50 * GB)
            extents = {f'f{k}': [(0, GB), (10 * GB + k * GB, MB)] for k in range(50)}
            def copy(k):
                store.directory(Node(f'c{k}', 'family', f'c{k}', 'directory', GB + MB, 0, 'observed', None, 0))
                store.holder(f'f{k}', f'c{k}', f'f{k}', GB + MB, MB, True)
            store.prepare(volume)
            store.directory(Node('root', None, 'root', 'directory', 0, 0, 'observed', None, 0))
            store.directory(Node('family', 'root', 'family', 'directory', 0, 0, 'observed', None, 0))
            for k in range(50):
                copy(k)
            store.finish_volume(volume, extents.__getitem__, [], full=True, lean=True)
            before = MapQuery(store.path).query('volumes')['rows'][0]['physical']
            store.prepare(volume, full=False)
            staged = store.generation
            store.begin_directory('c7')
            copy(7)
            store.finish_volume(volume, extents.__getitem__, [], full=False, lean=True)
            written = {r[0] for r in store.db.execute('SELECT id FROM nodes WHERE generation=?', (staged,))}
            self.assertEqual({str(source) for source in map(source_id, written)}, {'c7', 'family', 'root'})
            self.assertEqual(MapQuery(store.path).query('volumes')['rows'][0]['physical'], before)


class FormatConversionTests(unittest.TestCase):
    def test_previous_release_store_converts_keeps_history_and_rebuilds_its_map(self):
        # The previous release's schema, captured from it. Its folder figures charged
        # shared blocks to one folder, so they are retired, never shown as physical.
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            path = Path(temp) / 'scanner.sqlite'
            db = sqlite3.connect(path)
            db.executescript((Path(__file__).parent / 'format3_schema.sql').read_text())
            db.execute('PRAGMA user_version=3')
            db.execute("INSERT INTO volumes(key,id,label,mount,generation,valid,status,lean,full_ns) VALUES(1,'v','Synthetic','/synthetic',7,1,'observed',1,5)")
            db.execute("INSERT INTO generations VALUES(7,1,'published')")
            db.execute("INSERT INTO nodes VALUES(7,1,1,NULL,'Synthetic','directory',2000000000,0,2000000000,0,'observed',0)")
            db.execute("INSERT INTO charges VALUES(7,1,1,0,1,NULL,0,1)")
            db.execute("INSERT INTO tracked VALUES('v','1',NULL)")
            db.execute("INSERT INTO points VALUES('v','1',3,'Synthetic','uses',2000000000,'observed')")
            db.commit()
            db.close()
            self.assertEqual(MapQuery(path).query('volumes')['status'], 'awaiting_physical_walk')
            store = MapStore(path, Budget(temp, free_margin_bytes=0))
            self.addCleanup(store.close)
            self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertEqual(tuple(store.db.execute('SELECT generation,valid,status,full_ns FROM volumes').fetchone()),
                             (None, 0, 'awaiting_physical_walk', None))
            self.assertIsNone(MapQuery(path).query('volumes')['rows'][0]['physical'])
            self.assertEqual(store.db.execute('SELECT count(*) FROM points').fetchone()[0], 1)
            volume = Volume('v', 'Synthetic', '/synthetic', 'apfs', 1, 10 * GB, 5 * GB, 5 * GB, 5 * GB)
            store.prepare(volume)
            store.directory(Node('1', None, 'Synthetic', 'directory', 3 * GB, 3 * GB, 'observed', None, 0))
            store.finish_volume(volume, None, [], full=True, lean=True)
            self.assertEqual(MapQuery(path).query('volumes')['rows'][0]['physical'], 3 * GB)
            self.assertEqual(store.db.execute('SELECT count(*) FROM nodes WHERE generation=7').fetchone()[0], 0)
            # Growth across the switch compares different measures, so it is not claimed.
            growth = MapQuery(path).query('growth', volume_id='v', start_ns=3, end_ns=8 * 10**18)['rows'][0]
            self.assertIsNone(growth['change'])


if __name__ == '__main__':
    unittest.main()
