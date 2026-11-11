"""Event replay must not turn ordinary missing paths into subtree walks."""
import errno
import json
import os
from pathlib import Path
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import patch

from machine_audit.budget import Budget, GB
from machine_audit.scanner.native import Volume
from machine_audit.scanner.policy import Policy
from machine_audit.scanner.runner import relist
from machine_audit.scanner.store import MapStore, MapQuery
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
        walker=Walker(self.volume,self.policy,self.store);walker.walk()
        self.store.finish_volume(self.volume, None,walker.gaps,full=True,lean=True)
        self.store.prepare(self.volume,full=False)

    def update(self,path,recursive=False):
        walker=Walker(self.volume,self.policy,self.store)
        relist(self.store,walker,self.policy,self.volume,0,[(str(path),recursive)])
        return walker

    def test_missing_path_is_shallow_but_new_directories_are_discovered(self):
        parent=self.tree/'parent';(parent/'kept').mkdir(parents=True)
        for i in range(30):(parent/'kept'/str(i)).write_bytes(b'kept')
        self.baseline()
        (parent/'new').mkdir();(parent/'new'/'added').write_bytes(b'new')
        walker=self.update(parent/'missing')
        self.assertEqual(walker.entries,3,'ordinary missing event walked an unchanged subtree')
        self.store.finish_volume(self.volume, None,walker.gaps,full=False,lean=True)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM files WHERE generation=?',(self.store.generation,)).fetchone()[0],31)

    def test_vanished_recursive_directory_leaves_the_map_without_its_subtree(self):
        # A must-scan root removed before its walk; the parent listing records the
        # removal and the update still completes.
        gone=self.tree/'parent'/'gone';gone.mkdir(parents=True)
        for i in range(5):(gone/str(i)).write_bytes(b'x')
        (self.tree/'parent'/'kept').write_bytes(b'kept')
        self.baseline()
        for i in range(5):(gone/str(i)).unlink()
        gone.rmdir()  # Disposable test directory only.
        walker=Walker(self.volume,self.policy,self.store)
        self.assertTrue(relist(self.store,walker,self.policy,self.volume,0,[(str(gone),True)]))
        self.store.finish_volume(self.volume, None,walker.gaps,full=False,lean=True)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM files WHERE generation=?',(self.store.generation,)).fetchone()[0],1)

    def test_vanished_directory_with_descendants_leaves_no_orphans(self):
        # A shallow event on a directory removed with its files; the orphaned
        # descendants broke publication with a missing-ancestor invariant.
        gone=self.tree/'parent'/'gone';gone.mkdir(parents=True)
        for i in range(5):(gone/str(i)).write_bytes(b'x')
        (self.tree/'parent'/'kept').write_bytes(b'kept')
        self.baseline()
        for i in range(5):(gone/str(i)).unlink()
        gone.rmdir()  # Disposable test directory only.
        walker=Walker(self.volume,self.policy,self.store)
        self.assertTrue(relist(self.store,walker,self.policy,self.volume,0,[(str(gone),False)]))
        self.store.finish_volume(self.volume, None,walker.gaps,full=False,lean=True)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM files WHERE generation=?',(self.store.generation,)).fetchone()[0],1)

    def test_unreadable_changed_directory_keeps_its_map_and_is_deferred(self):
        # An existing directory that cannot be opened (no read permission on it, or no
        # search permission on its parent) keeps its last map and is retried later;
        # clearing it first orphaned new nodes, and an unlistable root fails a walk.
        if os.geteuid()==0:self.skipTest('permission checks need an unprivileged user')
        locked=self.tree/'locked';inner=locked/'inner';inner.mkdir(parents=True)
        for i in range(3):(inner/str(i)).write_bytes(b'x')
        self.baseline()
        for target,event in ((inner,inner),(locked,inner)):
            target.chmod(0)
            try:
                walker=Walker(self.volume,self.policy,self.store)
                self.assertTrue(relist(self.store,walker,self.policy,self.volume,0,[(str(event),True)]))
            finally:target.chmod(0o700)
        self.assertIn('denied',walker.gaps)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM dirty').fetchone()[0],1)
        self.store.finish_volume(self.volume, None,walker.gaps,full=False,lean=True)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM files WHERE generation=?',(self.store.generation,)).fetchone()[0],3)

    def test_child_event_attaches_to_a_parent_replaced_during_the_run(self):
        # The parent was gone at its check (its subtree cleared, its own parent re-listed,
        # which staged the recreated parent unlisted); the child's own event then hung its
        # new row from the removed parent row, and publication failed on a missing ancestor.
        parent=self.tree/'parent';child=parent/'child';child.mkdir(parents=True)
        (child/'old').write_bytes(b'old')
        self.baseline()
        # New inodes for both, with the replacement built before the old one goes.
        fresh=self.tree/'fresh';(fresh/'child').mkdir(parents=True);(fresh/'child'/'new').write_bytes(b'new')
        (child/'old').unlink();child.rmdir();parent.rmdir()  # Disposable test directories only.
        fresh.rename(parent)
        identity=Walker.directory_identity;real_stat=os.stat;gone=[str(parent)]
        def gone_once(walker,path):
            if gone and path==gone[0]:return None
            return identity(walker,path)
        def stat_once(path,*args,**kwargs):
            if gone and path==gone[0]:gone.clear();raise FileNotFoundError(errno.ENOENT,'gone',path)
            return real_stat(path,*args,**kwargs)
        walker=Walker(self.volume,self.policy,self.store)
        with patch.object(Walker,'directory_identity',gone_once),patch('machine_audit.scanner.runner.os.stat',stat_once):
            self.assertTrue(relist(self.store,walker,self.policy,self.volume,0,[(str(parent),False),(str(child),False)]))
        self.store.finish_volume(self.volume, None,walker.gaps,full=False,lean=True)
        self.assertEqual([r[0] for r in self.store.db.execute('SELECT label FROM files WHERE generation=?',(self.store.generation,))],['new'])

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

    def test_inode_lookup_uses_the_walk_root_device_not_the_event_device_id(self):
        # Native volume.device comes from statfs/FSEvents, while a walk compares st_dev.
        volume=replace(self.volume,device=self.volume.device+1)
        walker=Walker(volume,self.policy,self.store)
        self.assertEqual(walker.directory_identity(str(self.tree)),str(self.tree.stat().st_ino))

    def test_private_event_updates_only_its_known_private_directory(self):
        private=self.tree/'browser';(private/'inner').mkdir(parents=True)
        (private/'Local State').write_bytes(b'privacy marker')
        for i in range(30):(self.tree/str(i)).write_bytes(b'unrelated')
        self.baseline()
        (private/'inner'/'sensitive').write_bytes(bytes(8192))
        walker=self.update(private/'inner')
        self.assertEqual(walker.entries,1,'private event rewalked the visible ancestor')
        self.store.finish_volume(self.volume, None,walker.gaps,full=False,lean=True)
        labels={r[0] for r in self.store.db.execute('SELECT label FROM nodes UNION SELECT label FROM files')}
        self.assertTrue({'inner','sensitive','Local State'}.isdisjoint(labels))

    def test_restaging_a_subtree_does_not_query_once_a_folder(self):
        # A recursive re-list cost about 6 times a fresh walk per entry: clearing the
        # old subtree and merging its removals each issued queries for every folder.
        for name,folders in (('small',40),('large',320)):
            for i in range(folders):
                (self.tree/name/str(i)).mkdir(parents=True);(self.tree/name/str(i)/'f').write_bytes(b'x')
        self.baseline()
        statements={}
        for name in ('small','large'):
            seen=[]
            def counted(method):
                def run(*args,**kwargs):
                    self.store.db.set_trace_callback(seen.append)
                    try:return method(*args,**kwargs)
                    finally:self.store.db.set_trace_callback(None)
                return run
            with patch.object(self.store,'clear_subtree',counted(self.store.clear_subtree)), \
                 patch.object(self.store,'merge_delta',counted(self.store.merge_delta)):
                walker=self.update(self.tree/name,recursive=True)
                self.store.finish_volume(self.volume,None,walker.gaps,full=False,lean=True)
            # Reads only: writing each changed row once is the work itself.
            statements[name]=sum(1 for statement in seen if statement.lstrip().startswith('SELECT'))
            self.store.prepare(self.volume,full=False)
        self.assertLessEqual(statements['large'],statements['small']+20,statements)

    def test_merging_a_small_update_does_not_read_the_whole_base(self):
        # Live, Nov 11: an hourly update's merge cost 76-100 s. Ordered by the base's
        # key, the removal merge read every base row to find a few re-listed folders.
        work={}
        for name,unrelated in (('small',200),('large',4000)):
            tree=self.root/name;(tree/'bulk').mkdir(parents=True);(tree/'changed').mkdir()
            for i in range(unrelated):(tree/'bulk'/str(i)).write_bytes(b'x')
            for i in range(5):(tree/'changed'/f'c{i}').write_bytes(b'x')
            data=self.root/(name+'-data');data.mkdir()
            store=MapStore(data/'scanner.sqlite',Budget(data,limit_bytes=256*1024**2,free_margin_bytes=0))
            volume=replace(self.volume,mount=str(tree))
            store.prepare(volume);walker=Walker(volume,self.policy,store);walker.walk()
            store.finish_volume(volume,None,walker.gaps,full=True,lean=True)
            store.prepare(volume,full=False)
            (tree/'changed'/'c0').unlink()  # Disposable synthetic file: a removal to merge.
            walker=Walker(volume,self.policy,store)
            relist(store,walker,self.policy,volume,0,[(str(tree/'changed'),False)])
            steps=[0]
            def step():
                steps[0]+=1
            original=store.merge_delta
            def merge(*args,**kwargs):
                store.db.set_progress_handler(step,100)
                try:return original(*args,**kwargs)
                finally:store.db.set_progress_handler(None,0)
            with patch.object(store,'merge_delta',merge):
                store.finish_volume(volume,None,walker.gaps,full=False,lean=True)
            self.assertEqual(store.db.execute("SELECT count(*) FROM files WHERE generation=? AND label='c0'",(store.generation,)).fetchone()[0],0)
            work[name]=steps[0];store.close()
        self.assertLess(work['large'],2*work['small'],work)

    def reconciled(self,used,changed):
        """Re-list `changed`, publish against `used` bytes of used space, and return
        the volume's verdict and gaps. The next update is prepared."""
        walker=self.update(changed)
        volume=replace(self.volume,used=used)
        self.store.finish_volume(volume,None,walker.gaps,full=False,lean=True,used_before=used)
        valid,gaps=self.store.db.execute("SELECT valid,gaps FROM volumes WHERE id='v'").fetchone()
        self.store.prepare(self.volume,full=False)
        return bool(valid),json.loads(gaps)

    def test_an_update_that_left_folders_deferred_does_not_judge_reconciliation(self):
        # Big Mac's 13.7 TB volume, Nov 11 12:27 PM PT: data moved from a deferred folder
        # into a re-listed one counted twice, the folders exceeded used space by 1.7 GB,
        # and the map went invalid until a full walk now paced over days.
        (self.tree/'moved-to').mkdir();(self.tree/'deferred').mkdir()
        self.baseline()
        self.store.defer(str((self.tree/'deferred').stat().st_ino),10**18,False)
        # Used space 2 GB below the folders: beyond the 1 GB tolerance.
        valid,gaps=self.reconciled(-2*GB,self.tree/'moved-to')
        self.assertTrue(valid,'an update with deferred folders marked the map invalid')
        self.assertIn('reconciliation_deferred',gaps)

    def test_an_update_with_nothing_deferred_judges_and_can_clear_an_invalid_map(self):
        (self.tree/'changed').mkdir()
        self.baseline()
        # As before: with nothing deferred, folders beyond the tolerance are invalid.
        self.assertFalse(self.reconciled(-2*GB,self.tree/'changed')[0])
        # A later update that reconciles clears it; it was kept until the next full walk.
        valid,gaps=self.reconciled(10*GB,self.tree/'changed')
        self.assertTrue(valid,'a reconciling update left the map invalid')
        self.assertNotIn('reconciliation_deferred',gaps)

    def test_repeated_directory_reset_does_not_restore_deleted_staged_files(self):
        (self.tree/'kept').write_bytes(b'kept')
        self.baseline()
        (self.tree/'temporary').write_bytes(b'temporary')
        self.update(self.tree)
        (self.tree/'temporary').unlink()
        walker=self.update(self.tree)
        self.store.finish_volume(self.volume, None,walker.gaps,full=False,lean=True)
        self.assertEqual([r[0] for r in self.store.db.execute('SELECT label FROM files WHERE generation=?',(self.store.generation,))],['kept'])

    def test_failed_publication_keeps_dirty_refresh_until_successful_retry(self):
        (self.tree/'kept').write_bytes(b'kept');(self.tree/'later').mkdir()
        self.baseline()
        identity=str(self.tree.stat().st_ino)
        self.store.defer(identity,0,False)  # Shallow: 'later' is not listed again.
        later=str((self.tree/'later').stat().st_ino)
        self.store.defer(later,10**18,True)  # Not yet due: no listing fulfils it.
        # A refresh of a folder gone from the map can never run: it is dropped, so a
        # backlog can drain to nothing.
        self.store.defer('not-observed',0,True)
        walker=self.update(self.tree)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM dirty WHERE id='not-observed'").fetchone()[0],0)
        def pending():
            return self.store.db.execute('SELECT id FROM dirty WHERE volume=? AND id=?',(self.volume.id,identity)).fetchall()
        self.assertEqual([r[0] for r in pending()],[identity], 'a relist is not a published refresh')
        with patch('machine_audit.scanner.store.save_hierarchy',side_effect=RuntimeError('synthetic publication failure')):
            with self.assertRaisesRegex(RuntimeError,'synthetic publication failure'):
                self.store.finish_volume(self.volume, None,walker.gaps,full=False,lean=True)
        self.assertEqual([r[0] for r in pending()],[identity], 'rollback lost the deferred refresh')
        self.store.finish_volume(self.volume, None,walker.gaps,full=False,lean=True)
        self.assertEqual(pending(),[])
        self.assertEqual(self.store.db.execute('SELECT id FROM dirty').fetchone()[0],later)

    def test_deferred_large_folder_keeps_its_event_scope(self):
        # Big Mac re-walked ~1M entries per attempt: a deferred large folder was
        # always re-listed recursively when due, though its events were shallow.
        big=self.tree/'big';(big/'sub').mkdir(parents=True)
        for i in range(30):(big/'sub'/str(i)).write_bytes(b'x')
        self.baseline()
        identity=str(big.stat().st_ino)
        with self.store.write(rows=1):
            self.store.db.execute('UPDATE listings SET count=30000 WHERE id=inode_key(?)',(identity,))
        self.assertEqual(self.update(big).entries,0)
        def pending():
            return tuple(self.store.db.execute('SELECT due_ns,recursive FROM dirty WHERE id=?',(identity,)).fetchone())
        due,recursive=pending()
        self.assertEqual(recursive,0)
        # Merged deferrals keep the earliest due time and never narrow the scope.
        self.store.defer(identity,due-5,False);self.assertEqual(pending(),(due-5,0))
        self.store.defer(identity,due+5,True);self.assertEqual(pending(),(due-5,1))
        self.store.defer(identity,due,False);self.assertEqual(pending(),(due-5,1))
        with self.store.write(rows=1):self.store.db.execute('UPDATE dirty SET recursive=NULL')
        self.store.defer(identity,due,False);self.assertEqual(pending(),(due-5,1))
        (big/'new').mkdir();(big/'new'/'a').write_bytes(b'new');(big/'new'/'b').write_bytes(b'new')
        for recursive,entries in ((0,4),(None,34)):
            with self.subTest(recursive=recursive):
                self.store.prepare(self.volume,full=False)
                with self.store.write(rows=1):self.store.db.execute('UPDATE dirty SET recursive=?',(recursive,))
                walker=Walker(self.volume,self.policy,self.store)
                self.assertTrue(relist(self.store,walker,self.policy,self.volume,due,[]))
                # Shallow: the folder's two entries and the new folder's two files.
                self.assertEqual(walker.entries,entries)
                self.store.finish_volume(self.volume, None,walker.gaps,full=False,lean=True)
                self.assertEqual(self.store.db.execute('SELECT count(*) FROM files WHERE generation=?',
                                                       (self.store.generation,)).fetchone()[0],32)
                self.assertEqual(self.store.db.execute('SELECT count(*) FROM dirty').fetchone()[0],0)
                with self.store.write(rows=1):
                    self.store.db.execute('INSERT INTO dirty VALUES(?,?,?,0)',(self.volume.id,identity,due))
        # A recursive event on the due shallow folder: the shallow listing used to
        # discharge both, losing the nested change.
        (big/'sub'/'nested').write_bytes(b'new')
        self.store.prepare(self.volume,full=False)
        with self.store.write(rows=1):
            self.store.db.execute('UPDATE listings SET count=30000 WHERE id=inode_key(?)',(identity,))
        walker=Walker(self.volume,self.policy,self.store)
        self.assertTrue(relist(self.store,walker,self.policy,self.volume,due,[(str(big),True)]))
        self.assertEqual(walker.entries,35)
        self.store.finish_volume(self.volume, None,walker.gaps,full=False,lean=True)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM files WHERE generation=?',
                                               (self.store.generation,)).fetchone()[0],33)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM dirty').fetchone()[0],0)

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
        self.store.finish_volume(self.volume, None,walker.gaps,full=False,lean=True)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM files WHERE generation=?',(self.store.generation,)).fetchone()[0],150)


@unittest.skipUnless(os.path.isdir('/System/Volumes/Data/Users'), 'needs the macOS Data volume and its firmlinked paths')
class DataVolumeRelistTests(unittest.TestCase):
    def test_relisted_folder_keeps_an_excluded_child_excluded_and_unnamed(self):
        # The re-list judged children by their /System/Volumes/Data path while the
        # policy holds shown paths, so a known excluded child was taken as ordinary
        # and its name stored.
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            tree=Path(temp).resolve()/'tree';(tree/'withheld-name'/'inner').mkdir(parents=True);(tree/'kept').mkdir()
            data=Path(temp)/'data';data.mkdir()
            store=MapStore(data/'scanner.sqlite',Budget(data,free_margin_bytes=0));self.addCleanup(store.close)
            mount='/System/Volumes/Data'
            volume=Volume('v','Startup data',mount,'apfs',os.stat(mount).st_dev,10**12,1,1,10**9)
            policy=Policy(home=temp,homes=[],exclusions=[str(tree/'withheld-name')])
            root=mount+str(tree)
            store.prepare(volume);walker=Walker(volume,policy,store);walker.walk(root)
            store.finish_volume(volume,None,walker.gaps,full=True,lean=True)
            store.prepare(volume,full=False)
            walker=Walker(volume,policy,store);walker.walk(root,recursive=False)
            store.finish_volume(volume,None,walker.gaps,full=False,lean=True)
            labels=[r[0] for r in store.db.execute('SELECT label FROM nodes')]
            self.assertIn('kept',labels)
            self.assertNotIn('withheld-name',labels)
            children=MapQuery(store.path).query('children',volume_id='v')['rows']
            self.assertEqual({(r['label'],r['status']) for r in children},{(None,'excluded'),('kept','observed')})

if __name__=='__main__':unittest.main()
