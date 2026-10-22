import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch,Mock
from machine_audit.budget import Budget, BudgetExceeded
from machine_audit.scanner.accounting import Blocks, Stream, reconcile
from machine_audit.scanner.events import Replay, next_action, USER_DROP, KERNEL_DROP, HISTORY_DONE
from machine_audit.scanner.native import Volume
from machine_audit.scanner.policy import Decision, Policy
from machine_audit.scanner.store import MapStore, MapQuery
from machine_audit.scanner.walker import Walker, Node


SCRATCH=Path(__file__).parent/'.tmp'
SCRATCH.mkdir(exist_ok=True)


class ScannerTests(unittest.TestCase):
    def test_shared_partial_extents_go_to_oldest_stream_once(self):
        b=Blocks()
        b.add(Stream(10,'a',8192,None,False),[(0,8192)])
        b.add(Stream(20,'b',8192,4096,True),[(4096,8192)])
        self.assertEqual(b.charges(),{10:8192,20:4096})
        self.assertEqual(reconcile(20000,12288)['not_attributed'],7712)
        self.assertFalse(reconcile(1,12288)['valid'])
        self.assertIsNone(reconcile(1,12288)['uses'])

    def test_failed_block_budget_keeps_private_lower_bound_and_explicit_estimate(self):
        b=Blocks(max_bytes=24)
        b.add(Stream(20,'b',8192,100,True),[(0,4096),(8192,4096)])
        self.assertEqual(b.charges(),{20:100})
        self.assertEqual(b.estimated,{20})

    def test_privacy_rules_apply_before_open_and_keep_browser_contents_unnamed(self):
        policy=Policy(home='/Users/tester',homes=['/Users/tester'])
        self.assertFalse(policy.decide('/Users/tester/Documents').descend)
        self.assertFalse(policy.decide('/Users/another').descend)
        self.assertFalse(policy.decide('/Users/tester/Library/CloudStorage').descend)
        self.assertFalse(policy.decide('/Users/tester/Library/Mail').names)
        self.assertTrue(policy.contents_private(['Local State','Default']))
        self.assertFalse(Policy(full_disk_access=True,home='/Users/tester',homes=[]).decide('/Users/tester/a.PHOTOSLIBRARY').names)

    def test_exclusion_covers_root_and_children_but_not_a_same_prefix_sibling(self):
        policy=Policy(home='/home/tester',homes=[],exclusions=['/srv/private'])
        for path in ('/srv/private','/srv/private/chain/db'):
            self.assertEqual(policy.decide(path),Decision(False,False,'excluded'))
        self.assertTrue(policy.decide('/srv/private2').descend)

    def test_excluded_root_is_reported_by_count_without_names_or_paths(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            data=Path(temp)/'data';tree=Path(temp)/'tree';data.mkdir()
            (tree/'kept').mkdir(parents=True);(tree/'private'/'inner').mkdir(parents=True)
            (tree/'private'/'inner'/'secret.bin').write_bytes(b'x'*8192)
            store=MapStore(data/'scanner.sqlite',Budget(data,limit_bytes=16*1024**2,warning_bytes=8*1024**2,free_margin_bytes=0))
            volume=Volume('synthetic','Synthetic',str(tree),'hfs',1,1_000_000,800000,800000,200000)
            store.start('full');store.prepare(volume)
            blocks=Blocks();walker=Walker(volume,Policy(home=temp,homes=[],exclusions=[str(tree/'private')]),store,blocks)
            walker.walk();store.finish_volume(volume,blocks,walker.gaps,full=True,lean=True);store.close()
            answers=[MapQuery(store.path).query(action) for action in ('volumes','children')]
            for answer in answers:
                self.assertEqual(answer['coverage']['excluded_roots'],1)
                self.assertIn('excluded',answer['coverage']['gaps'])
            db=sqlite3.connect(store.path)
            dumped=repr(answers)+repr(db.execute('SELECT label FROM nodes UNION ALL SELECT label FROM files').fetchall())
            db.close()
            for secret in ('private','inner','secret.bin'):
                self.assertNotIn(secret,dumped)
            self.assertIn('kept',dumped)

    def test_dropped_events_restart_after_last_good_and_no_progress_forces_full(self):
        r=Replay();self.assertTrue(r.add('safe',0,10))
        self.assertFalse(r.add('root',USER_DROP,11))
        self.assertEqual(r.last_good,10);self.assertFalse(r.lost)
        lost=Replay();lost.add('',KERNEL_DROP,1);self.assertTrue(lost.lost)
        self.assertEqual(next_action(now_ns=100,full_ns=1,update_ns=1,bookmark=10,no_progress=3),'full')

    def test_synthetic_walk_hardlinks_reconcile_and_budget_failure_preserves_map(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            data=Path(temp)/'data';tree=Path(temp)/'tree';data.mkdir();tree.mkdir()
            (tree/'a').mkdir();(tree/'b').mkdir()
            (tree/'a'/'file').write_bytes(b'x'*8192)
            os.link(tree/'a'/'file',tree/'b'/'copy')
            budget=Budget(data,limit_bytes=16*1024**2,warning_bytes=8*1024**2,free_margin_bytes=0)
            store=MapStore(data/'scanner.sqlite',budget)
            volume=Volume('synthetic','Synthetic',str(tree),'hfs',1,1_000_000,800000,800000,200000)
            store.start('full');store.prepare(volume)
            blocks=Blocks();walker=Walker(volume,Policy(home=temp,homes=[],data_root=str(data)),store,blocks)
            walker.walk();result=store.finish_volume(volume,blocks,walker.gaps,full=True,lean=True)
            self.assertTrue(result['valid'])
            total=result['uses'];self.assertLess(total,200000)
            before=MapQuery(store.path).query()['rows'][0]
            self.assertEqual(before['uses']+before['not_attributed'],before['volume_used'])
            generation=store.generation
            budget.limit_bytes=budget.usage()['bytes']
            with self.assertRaises(BudgetExceeded):store.prepare(volume)
            after=MapQuery(store.path).query()['rows'][0]
            self.assertEqual(after,before)
            self.assertEqual(store.db.execute('SELECT generation FROM volumes').fetchone()[0],generation)
            store.close()

    def test_incremental_metadata_copy_preserves_known_shared_charges(self):
        # Regression: reusing an unchanged map must not turn 6 GB shared use
        # back into 8 GB listed allocation when no new block map is present.
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            store=MapStore(Path(temp)/'scanner.sqlite',Budget(temp,free_margin_bytes=0))
            volume=Volume('v','Volume',temp,'apfs',1,10**12,1,1,10**11)
            store.prepare(volume)
            store.directory(Node('1',None,'root','directory',8*10**9,0,'observed',temp,0))
            store.holder(1,'1',1,4*10**9,None,False)
            store.holder(2,'1',2,4*10**9,2*10**9,True)
            blocks=Blocks()
            blocks.add(Stream(1,'1',4*10**9,None,False),[(0,4*10**9)])
            blocks.add(Stream(2,'1',4*10**9,2*10**9,True),[(2*10**9,4*10**9)])
            first=store.finish_volume(volume,blocks,[],full=True,lean=True)
            store.prepare(volume,full=False)
            second=store.finish_volume(volume,Blocks(),[],full=False,lean=True)
            self.assertEqual(first['uses'],6*10**9)
            self.assertEqual(second['uses'],first['uses'])
            store.close()

    def test_hourly_direct_relist_keeps_unchanged_descendants(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            tree=Path(temp)/'tree';tree.mkdir();data=Path(temp)/'data';data.mkdir()
            (tree/'child').mkdir();(tree/'child'/'existing').write_bytes(b'x'*4096)
            store=MapStore(data/'scanner.sqlite',Budget(data,free_margin_bytes=0))
            v=Volume('v','Volume',str(tree),'hfs',1,10**9,1,1,10**8)
            policy=Policy(home=temp,homes=[])
            store.prepare(v);w=Walker(v,policy,store,Blocks());w.walk()
            store.finish_volume(v,Blocks(),[],full=True,lean=True)
            (tree/'new').write_bytes(b'y'*4096)
            store.prepare(v,full=False)
            w=Walker(v,policy,store,Blocks());w.walk(recursive=False)
            self.assertEqual(w.entries,2)  # child directory + new file, not its unchanged content
            store.finish_volume(v,Blocks(),[],full=False,lean=True)
            self.assertEqual(store.db.execute('SELECT count(*) FROM files WHERE generation=?',(store.generation,)).fetchone()[0],2)
            store.close()

    def test_manual_scan_cli_passes_configured_exclusions_like_the_installed_entry(self):
        # Review of the exclusions merge: the launchd entry honoured scanner_exclusions but the
        # `machine-audit scan` path did not, so a manual scan would walk and name excluded folders.
        from machine_audit import scanner
        from machine_audit.config import Config
        config=Config(data_dir='/tmp/synthetic-data',enabled={'scanner':True},scanner_exclusions=['/synthetic/excluded'])
        with patch('machine_audit.scanner.runner.run_scan',return_value={'status':'observed'}) as run:
            self.assertEqual(scanner.main(config),0)
        self.assertEqual(run.call_args.kwargs['exclusions'],['/synthetic/excluded'])

    def test_missing_permission_proof_preserves_existing_rich_map(self):
        from machine_audit.scanner.runner import run_scan
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            store=MapStore(Path(temp)/'scanner.sqlite',Budget(temp,free_margin_bytes=0))
            v=Volume('v','Volume',temp,'apfs',1,10**9,1,1,10**8)
            store.prepare(v,lean=False)
            store.directory(Node('1',None,'Prior map','directory',100,100,'observed',temp,0))
            store.finish_volume(v,Blocks(),[],full=True,lean=False)
            generation=store.generation;store.close()
            native=Mock();native.process.return_value.proc_start_abstime=1
            fs=Mock();fs.volumes.return_value=[v]
            with patch('machine_audit.scanner.runner.Darwin',return_value=native),patch('machine_audit.scanner.runner.MacFilesystem',return_value=fs),patch('machine_audit.scanner.runner.IOKit'),patch('machine_audit.scanner.runner.FSEvents'),patch('machine_audit.scanner.runner.os.nice'):
                run_scan(temp,full_disk_access=True)  # requested True without observation grants nothing
            db=sqlite3.connect(Path(temp)/'scanner.sqlite')
            row=db.execute('SELECT generation,status FROM volumes').fetchone()
            self.assertEqual(row,(generation,'access_unverified'))
            self.assertEqual(db.execute('SELECT label FROM nodes WHERE generation=?',(generation,)).fetchone()[0],'Prior map')
            db.close()

    def test_child_and_growth_pages_use_bounded_opaque_cursors(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            store=MapStore(Path(temp)/'scanner.sqlite',Budget(temp,free_margin_bytes=0))
            v=Volume('v','Volume',temp,'hfs',1,10**12,1,1,10**11)
            store.prepare(v)
            store.directory(Node('root',None,'Root','directory',0,0,'observed',temp,0))
            for i in range(5):
                store.directory(Node(str(i),'root',f'Folder {i}','directory',(i+1)*10**9,0,'observed',None,0))
            store.finish_volume(v,Blocks(),[],full=True,lean=True)
            query=MapQuery(store.path)
            one=query.query('children',volume_id='v',limit=2)
            two=query.query('children',volume_id='v',limit=2,cursor=one['next_cursor'])
            self.assertTrue(one['truncated']);self.assertEqual(len(one['rows']),2)
            self.assertTrue(set(r['id'] for r in one['rows']).isdisjoint(r['id'] for r in two['rows']))
            page=query.query('growth',volume_id='v',start_ns=0,end_ns=8*10**18,limit=2)
            self.assertEqual(page['coverage']['candidate_limit'],2)
            self.assertTrue(page['truncated'])
            self.assertIsNotNone(page['next_cursor'])
            with self.assertRaisesRegex(ValueError,'stale_or_incompatible'):
                query.query('growth',volume_id='v',start_ns=1,end_ns=8*10**18,limit=2,cursor=page['next_cursor'])
            store.db.execute("UPDATE volumes SET generation='new-generation' WHERE id='v'")
            with self.assertRaisesRegex(ValueError,'stale_or_incompatible'):
                query.query('children',volume_id='v',parent_id='root',limit=2,cursor=one['next_cursor'])
            with self.assertRaisesRegex(ValueError,'use_storage_cursor'):query.query('children',offset=1000000)
            store.close()

    def test_billion_byte_points_use_actual_boundaries_and_never_cross_metrics(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            budget=Budget(temp,free_margin_bytes=0)
            store=MapStore(Path(temp)/'scanner.sqlite',budget)
            volume=Volume('v','Volume',temp,'hfs',1,10**12,1,1,10**11)
            store.prepare(volume);store.directory(Node('1',None,'Root','directory',2*10**9,2*10**9,'observed',temp,0))
            store.finish_volume(volume,Blocks(),[],full=True,lean=True)
            point=store.db.execute('SELECT * FROM points').fetchone()
            self.assertEqual(point['metric'],'uses')
            store.db.execute('UPDATE points SET observed_ns=100')
            store.db.execute('INSERT INTO points VALUES(?,?,?,?,?,?,?)',('v','1',200,'Root','listed',3*10**9,'observed'))
            answer=MapQuery(store.path).query('growth',volume_id='v',start_ns=100,end_ns=200)
            self.assertIsNone(answer['rows'][0]['change'])
            self.assertEqual(answer['rows'][0]['status'],'unobserved')
            store.close()

if __name__=='__main__':unittest.main()
