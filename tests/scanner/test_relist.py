"""Event replay must not turn ordinary missing paths into subtree walks."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from machine_audit.budget import Budget
from machine_audit.scanner.accounting import Blocks
from machine_audit.scanner.native import Volume
from machine_audit.scanner.policy import Policy
from machine_audit.scanner.runner import relist
from machine_audit.scanner.store import MapStore
from machine_audit.scanner.walker import Walker


class RelistTests(unittest.TestCase):
    def setUp(self):
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=scratch);self.root=Path(self.tmp.name)
        self.tree=self.root/'tree';self.tree.mkdir();data=self.root/'data';data.mkdir()
        self.store=MapStore(data/'scanner.sqlite',Budget(data,limit_bytes=256*1024**2,free_margin_bytes=0))
        self.volume=Volume('v','Synthetic',str(self.tree),'hfs',self.tree.stat().st_dev,10**12,1,1,10**9)
        self.policy=Policy(home=str(self.root),homes=[])

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def baseline(self):
        self.store.prepare(self.volume)
        walker=Walker(self.volume,self.policy,self.store,Blocks());walker.walk()
        self.store.finish_volume(self.volume,walker.blocks,walker.gaps,full=True,lean=True)
        self.store.prepare(self.volume,full=False)

    def update(self,path,recursive=False):
        walker=Walker(self.volume,self.policy,self.store,Blocks())
        relist(self.store,walker,self.policy,self.volume,0,[(str(path),recursive)])
        return walker

    def test_missing_path_is_shallow_but_new_directories_are_discovered(self):
        parent=self.tree/'parent';(parent/'kept').mkdir(parents=True)
        for i in range(30):(parent/'kept'/str(i)).write_bytes(b'kept')
        self.baseline()
        (parent/'new').mkdir();(parent/'new'/'added').write_bytes(b'new')
        walker=self.update(parent/'missing')
        self.assertEqual(walker.entries,3,'ordinary missing event walked an unchanged subtree')
        self.store.finish_volume(self.volume,walker.blocks,walker.gaps,full=False,lean=True)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM files WHERE generation=?',(self.store.generation,)).fetchone()[0],31)

    def test_must_scan_still_recurses(self):
        (self.tree/'parent'/'kept').mkdir(parents=True)
        for i in range(30):(self.tree/'parent'/'kept'/str(i)).write_bytes(b'kept')
        self.baseline()
        self.assertEqual(self.update(self.tree/'parent',True).entries,31)

    def test_denied_ancestor_is_checked_before_resolving_an_event_inode(self):
        (self.tree/'Desktop').mkdir()
        self.policy=Policy(home=str(self.tree),homes=[str(self.tree)])
        self.baseline()
        with patch.object(Walker,'directory_identity',side_effect=AssertionError('protected open')):
            self.assertEqual(self.update(self.tree/'Desktop'/'event').entries,0)

    def test_private_event_updates_only_its_known_private_directory(self):
        private=self.tree/'browser';(private/'inner').mkdir(parents=True)
        (private/'Local State').write_bytes(b'privacy marker')
        for i in range(30):(self.tree/str(i)).write_bytes(b'unrelated')
        self.baseline()
        (private/'inner'/'sensitive').write_bytes(bytes(8192))
        walker=self.update(private/'inner')
        self.assertEqual(walker.entries,1,'private event rewalked the visible ancestor')
        self.store.finish_volume(self.volume,walker.blocks,walker.gaps,full=False,lean=True)
        labels={r[0] for r in self.store.db.execute('SELECT label FROM nodes UNION SELECT label FROM files')}
        self.assertTrue({'inner','sensitive','Local State'}.isdisjoint(labels))

    def test_repeated_directory_reset_does_not_restore_deleted_staged_files(self):
        (self.tree/'kept').write_bytes(b'kept')
        self.baseline()
        (self.tree/'temporary').write_bytes(b'temporary')
        self.update(self.tree)
        (self.tree/'temporary').unlink()
        walker=self.update(self.tree)
        self.store.finish_volume(self.volume,walker.blocks,walker.gaps,full=False,lean=True)
        self.assertEqual([r[0] for r in self.store.db.execute('SELECT label FROM files WHERE generation=?',(self.store.generation,))],['kept'])

    def test_shallow_parent_keeps_private_child_without_copying_its_subtree(self):
        private=self.tree/'browser';(private/'inner').mkdir(parents=True)
        (private/'Local State').write_bytes(b'private marker')
        for i in range(30):(private/'inner'/str(i)).write_bytes(b'private')
        self.baseline()
        walker=self.update(self.tree)
        self.store.flush()
        self.assertEqual(walker.entries,1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM nodes WHERE generation=?',(self.store.generation,)).fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM files WHERE generation=?',(self.store.generation,)).fetchone()[0],0)

    def test_recursive_update_batches_directory_resets_and_retains_publication(self):
        for i in range(150):
            folder=self.tree/str(i);folder.mkdir();(folder/'file').write_bytes(b'payload')
        self.baseline()
        published=self.store.db.execute('SELECT generation FROM volumes').fetchone()[0]
        statements=[];self.store.db.set_trace_callback(statements.append)
        walker=self.update(self.tree,True);self.store.flush()
        self.store.db.set_trace_callback(None)
        self.assertEqual(walker.entries,300)
        self.assertLessEqual(sum(q=='BEGIN IMMEDIATE' for q in statements),4)
        self.assertEqual(self.store.db.execute('SELECT generation FROM volumes').fetchone()[0],published)
        self.store.finish_volume(self.volume,walker.blocks,walker.gaps,full=False,lean=True)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM files WHERE generation=?',(self.store.generation,)).fetchone()[0],150)


if __name__=='__main__':unittest.main()
