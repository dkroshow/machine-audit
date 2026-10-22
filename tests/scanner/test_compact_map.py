"""Compact map publication preserves evidence without copying unchanged files."""
from pathlib import Path
from contextlib import contextmanager
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from machine_audit.budget import Budget, BudgetExceeded
from machine_audit.scanner.accounting import Blocks, Stream
from machine_audit.scanner.encoding import inode_key, source_id
from machine_audit.scanner.native import Volume
from machine_audit.scanner.store import MapStore, MapQuery
from machine_audit.scanner.walker import Node

SCRATCH=Path(__file__).parent/'.tmp'
SCRATCH.mkdir(exist_ok=True)
GB=1_000_000_000


class StartupBudgetTests(unittest.TestCase):
    def test_startup_refusal_does_not_allocate_or_modify_an_existing_store(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            path=Path(temp)/'scanner.sqlite'
            budget=Budget(temp,limit_bytes=1024,free_margin_bytes=0)
            with self.assertRaises(BudgetExceeded):MapStore(path,budget)
            self.assertLessEqual(budget.usage()['bytes'],1024)
            self.assertFalse(path.exists())
            store=MapStore(path,Budget(temp,free_margin_bytes=0));store.close()
            before={p.name:p.read_bytes() for p in Path(temp).iterdir()}
            budget=Budget(temp,limit_bytes=sum(map(len,before.values()))+1024,free_margin_bytes=0)
            with self.assertRaises(BudgetExceeded):MapStore(path,budget)
            self.assertEqual({p.name:p.read_bytes() for p in Path(temp).iterdir()},before)

    def test_schema_budget_failure_closes_the_opened_connection(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            # Include the live reservation record so startup reaches schema
            # allocation; the schema still exceeds this hard limit.
            budget=Budget(temp,limit_bytes=256*1024+512,free_margin_bytes=0)
            connections=[];connect=sqlite3.connect
            def capture(*args,**kwargs):
                db=connect(*args,**kwargs);connections.append(db);return db
            with patch('machine_audit.scanner.store.sqlite3.connect',side_effect=capture):
                with self.assertRaises(sqlite3.OperationalError) as failure:
                    MapStore(Path(temp)/'scanner.sqlite',budget)
            self.assertEqual(failure.exception.sqlite_errorcode,sqlite3.SQLITE_FULL)
            self.assertLessEqual(budget.usage()['bytes'],budget.limit_bytes)
            self.assertEqual(len(connections),1)
            with self.assertRaises(sqlite3.ProgrammingError):connections[0].execute('SELECT 1')


class CompactMapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=SCRATCH)
        self.store=MapStore(Path(self.temp.name)/'scanner.sqlite',Budget(self.temp.name,free_margin_bytes=0))
        self.volume=Volume('v','Volume','/synthetic','hfs',1,100*GB,20*GB,20*GB,80*GB)

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def node(self,identity,parent,size=0,label=None):
        self.store.directory(Node(identity,parent,label or identity,'directory',size,size,'observed',None,0))

    def seed(self):
        self.store.prepare(self.volume)
        self.node('1',None)
        for branch in range(2,22):
            self.node(str(branch),'1',2*GB)
            for file in range(50):self.store.file(str(branch*100+file),str(branch),f'f{file}',40_000_000,0)
        self.store.finish_volume(self.volume,Blocks(),[],full=True,lean=True)

    def test_obsolete_generation_pruning_is_batched_resumable_and_generation_last(self):
        # A restarted walk used one transaction to delete millions of abandoned
        # rows; cache_spill=OFF retained its dirty pages for the whole deletion.
        self.store.prepare(self.volume);self.node('published',None,2*GB)
        self.store.finish_volume(self.volume,Blocks(),[],full=True,lean=True)
        before=MapQuery(self.store.path).query()
        vid=self.store.vid
        with self.store.write():
            for generation,status in ((100,'retired'),(101,'building')):
                self.store.db.execute('INSERT INTO generations VALUES(?,?,?)',(generation,vid,status))
                self.store.db.execute('INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (generation,vid,0,None,'obsolete','directory',0,0,0,0,'observed',0))
                self.store.db.executemany('INSERT INTO files VALUES(?,?,?,?,?,?,?)',
                    ((generation,vid,i//100,i%100,'obsolete',1,0) for i in range(4097)))
                self.store.db.executemany('INSERT INTO holders VALUES(?,?,?,?,?,?,?,?)',
                    ((generation,vid,i//100,i%100,i%100,1,None,0) for i in range(4097)))
                self.store.db.execute('INSERT INTO charges VALUES(?,?,?,?,?,?,?,?)',(generation,vid,0,1,1,None,0,0))
                self.store.db.execute('INSERT INTO listings VALUES(?,?,?,?,?)',(generation,vid,0,0,4097))
                for table in ('removed','reset_dirs','removed_charges'):
                    self.store.db.execute(f'INSERT INTO {table} VALUES(?,?,?)',(generation,vid,0))
        tables=('nodes','files','holders','charges','listings','removed','reset_dirs','removed_charges')
        original_write=self.store.write;committed=[];interrupt=True
        @contextmanager
        def checked_write(*args,**kwargs):
            changes=self.store.db.total_changes
            with original_write(*args,**kwargs):yield
            committed.append(self.store.db.total_changes-changes)
            self.assertLessEqual(committed[-1],4096)
            for generation in (100,101):
                if not self.store.db.execute('SELECT 1 FROM generations WHERE id=?',(generation,)).fetchone():
                    for table in tables:
                        self.assertIsNone(self.store.db.execute(
                            f'SELECT 1 FROM {table} WHERE generation=? LIMIT 1',(generation,)).fetchone())
            if interrupt:raise RuntimeError('synthetic interruption after committed batch')
        with patch.object(self.store,'write',checked_write):
            with self.assertRaisesRegex(RuntimeError,'synthetic interruption'):
                self.store.prune_generations(full=False)
            self.assertIsNotNone(self.store.db.execute('SELECT 1 FROM generations WHERE id=101').fetchone())
            interrupt=False
            self.store.prune_generations(full=False)
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM files WHERE generation=100').fetchone()[0],4097)
            self.assertIsNone(self.store.db.execute('SELECT 1 FROM generations WHERE id=101').fetchone())
            self.store.prune_generations(full=True)
        self.assertIn(4096,committed)
        self.assertEqual(MapQuery(self.store.path).query(),before)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM generations').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('PRAGMA cache_spill').fetchone()[0],0)

    def test_inode_uint64_and_fixture_namespaces_round_trip_without_collisions(self):
        values=['0',str(2**63-1),str(2**63),str(2**64-1),'-1','fixture:1','01','a']
        keys=[inode_key(v) for v in values]
        self.assertEqual(len(set(keys)),len(keys))
        self.assertEqual([source_id(v) for v in keys],values)
        self.store.prepare(self.volume);self.node('root',None)
        for identity in values:self.store.file(identity,'root',identity,10,0)
        self.store.finish_volume(self.volume,Blocks(),[],full=True,lean=True)
        query=MapQuery(self.store.path);cursor=None;found=[]
        while True:
            page=query.query('children',volume_id='v',limit=2,cursor=cursor)
            found.extend(row['id'] for row in page['rows'])
            if not page['truncated']:break
            cursor=page['next_cursor']
        self.assertCountEqual(found,values)

    def test_unsigned_clone_and_negative_partial_stream_do_not_alias(self):
        # The native uint64 clone and signed partial-stream key must stay
        # distinct through persisted holders, charges and public totals.
        blocks=Blocks()
        blocks.add(Stream(2**64-7,'a',8,None,False),[(0,8)])
        blocks.add(Stream(-7,'b',8,0,True),[(8,8)])
        self.store.prepare(self.volume)
        self.node('root',None)
        self.node('a','root',8);self.node('b','root',8)
        self.store.holder(1,'a',2**64-7,8,None,False)
        self.store.holder(2,'b',-7,8,0,True)
        self.store.finish_volume(self.volume,blocks,[],full=True,lean=True)
        charges={int(source_id(row['stream'])):row['charge']
                 for row in self.store.db.execute('SELECT stream,charge FROM charges')}
        self.assertEqual(charges,{2**64-7:8,-7:8})
        children=MapQuery(self.store.path).query('children',volume_id='v')['rows']
        self.assertEqual({row['id']:row['uses'] for row in children},{'a':8,'b':8})

    def test_holder_lookups_use_specific_indexes_and_keep_shared_totals(self):
        # The volume-wide primary-key scan made each stream/file lookup
        # revisit every holder after the first real full walk.
        self.store.prepare(self.volume)
        self.node('root',None);self.node('a','root',4*GB);self.node('b','root',8*GB)
        self.store.holder(1,'a',1,4*GB,None,False)
        self.store.holder(1,'b',1,4*GB,None,False)  # Same file in two directories.
        self.store.holder(2,'b',2,4*GB,2*GB,True)
        self.store.flush()

        def check_lookups(branches):
            for column,index in (('stream','holder_stream'),('file_id','holder_file')):
                statements=[]
                self.store.db.set_trace_callback(statements.append)
                try:rows=list(self.store._rows('holders',column+'=?',(inode_key(1),)))
                finally:self.store.db.set_trace_callback(None)
                self.assertEqual({(source_id(r['dir']),source_id(r['file_id'])) for r in rows},
                                 {('a','1'),('b','1')})
                plan=[r['detail'] for r in self.store.db.execute('EXPLAIN QUERY PLAN '+statements[0])]
                with self.subTest(branches=branches,column=column):
                    self.assertEqual(sum('USING INDEX '+index+' ' in detail for detail in plan),branches,plan)

        check_lookups(1)
        blocks=Blocks()
        blocks.add(Stream(1,'a',4*GB,None,False),[(0,4*GB)])
        blocks.add(Stream(2,'b',4*GB,2*GB,True),[(2*GB,4*GB)])
        self.store.finish_volume(self.volume,blocks,[],full=True,lean=True)
        first=MapQuery(self.store.path).query()['rows'][0]
        self.assertEqual((first['listed'],first['uses']),(8*GB,6*GB))
        self.store.prepare(self.volume,full=False)
        self.store.begin_directory('a');self.node('a','root',4*GB)
        self.store.holder(1,'a',1,4*GB,None,False);self.store.flush()
        check_lookups(2)
        self.store.finish_volume(self.volume,Blocks(),[],full=False,lean=True)
        second=MapQuery(self.store.path).query()['rows'][0]
        self.assertEqual((second['listed'],second['uses']),(8*GB,6*GB))

    def test_baseline_growth_preserves_small_change_without_global_claim(self):
        for at,extra in ((1_000_000_000,0),(2_000_000_000,50_000_000)):
            self.store.prepare(self.volume)
            self.node('parent',None,10*GB+extra);self.node('child','parent',10*GB)
            with patch('machine_audit.scanner.store.time.time_ns',return_value=at):
                self.store.finish_volume(self.volume,Blocks(),[],full=True,lean=True)
        result=MapQuery(self.store.path).query('growth',start_ns=1_000_000_000,end_ns=2_000_000_000)
        changes={row['id']:row['change'] for row in result['rows']}
        self.assertEqual(changes,{'parent':50_000_000,'child':0})
        self.assertFalse(result['coverage']['global_growth_complete'])
        self.assertNotEqual(result['coverage']['growth_grouping'],'globally_deepest')

    def test_historical_hierarchy_foundation_records_observation_without_backdating(self):
        self.store.prepare(self.volume);self.node('root',None)
        self.node('a','root');self.node('b','root');self.node('leaf','a',2*GB)
        with patch('machine_audit.scanner.store.time.time_ns',return_value=1_000_000_000):
            self.store.finish_volume(self.volume,Blocks(),[],full=True,lean=True)
        query=MapQuery(self.store.path)
        first=query.query('growth',start_ns=0,end_ns=1_000_000_000,limit=1)
        self.store.prepare(self.volume,full=False)
        self.store.relocate('leaf','b','leaf','/synthetic/b/leaf',False)
        with patch('machine_audit.scanner.store.time.time_ns',return_value=2_000_000_000):
            self.store.finish_volume(self.volume,Blocks(),[],full=False,lean=True)
        observed=list(self.store.db.execute('''SELECT e.observed_ns,n.parent FROM hierarchy_epochs e JOIN hierarchy_nodes n
            ON n.volume=e.volume AND n.version=e.version WHERE n.id='leaf' ORDER BY e.observed_ns'''))
        self.assertEqual([tuple(row) for row in observed],[(1_000_000_000,'a'),(2_000_000_000,'b')])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM points WHERE id='leaf'").fetchone()[0],1)
        with self.assertRaisesRegex(ValueError,'stale_or_incompatible'):
            query.query('growth',start_ns=0,end_ns=1_000_000_000,limit=1,cursor=first['next_cursor'])

    def test_hourly_update_stages_only_changed_rows_and_preserves_unchanged_files(self):
        self.seed()
        query=MapQuery(self.store.path)
        cursor=query.query('children',limit=2)['next_cursor']
        published=self.store.generation
        self.store.prepare(self.volume,full=False)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM files WHERE generation=?',(self.store.generation,)).fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM nodes WHERE generation=?',(self.store.generation,)).fetchone()[0],0)
        self.store.begin_directory('2');self.node('2','1',3*GB)
        self.store.file('9999','2','new',3*GB,0)
        self.store.flush();before=self.store.db.total_changes
        self.store.finish_volume(self.volume,Blocks(),[],full=False,lean=True)
        self.assertLess(self.store.db.total_changes-before,150)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM files').fetchone()[0],951)
        self.assertEqual(self.store.generation,published)
        with self.assertRaisesRegex(ValueError,'stale_or_incompatible'):
            query.query('children',limit=2,cursor=cursor)
        result=MapQuery(self.store.path).query('children',volume_id='v',parent_id='3')
        self.assertEqual(len(result['rows']),50)
        self.assertEqual(MapQuery(self.store.path).query()['rows'][0]['uses'],41*GB)
        self.store.prepare(self.volume,full=False);self.store.clear_subtree('2')
        self.store.finish_volume(self.volume,Blocks(),[],full=False,lean=True)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM files').fetchone()[0],950)
        self.assertEqual(MapQuery(self.store.path).query()['rows'][0]['uses'],38*GB)
        last=self.store.db.execute("SELECT status FROM points WHERE id='2' ORDER BY observed_ns DESC LIMIT 1").fetchone()
        self.assertEqual(last['status'],'not_observed')

    def test_full_sharing_commits_bounded_unpublished_batches(self):
        self.store.prepare(self.volume);self.node('old',None,2*GB)
        self.store.finish_volume(self.volume,Blocks(),[],full=True,lean=True)
        before=MapQuery(self.store.path).query()
        self.store.prepare(self.volume)
        self.node('replacement',None,4097*4096)
        for identity in range(4097):self.store.holder(identity,'replacement',identity,4096,None,False)
        self.store.flush()
        batches=[];current=0
        def trace(sql):
            nonlocal current
            if sql.startswith('INSERT OR REPLACE INTO charges'):current+=1
            if sql=='COMMIT' and current:batches.append(current);current=0
        self.store.db.set_trace_callback(trace)
        try:
            # A later construction failure must retain the published map even
            # though completed charge batches exist in the building generation.
            with patch.object(self.store,'_aggregate_full',side_effect=RuntimeError('synthetic failure')):
                with self.assertRaises(RuntimeError):
                    self.store.finish_volume(self.volume,Blocks(),[],full=True,lean=True)
        finally:self.store.db.set_trace_callback(None)
        self.assertEqual(batches,[4096,1])
        self.assertEqual(MapQuery(self.store.path).query(),before)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM charges WHERE generation=?',
                                              (self.store.generation,)).fetchone()[0],4097)

    def test_failure_after_applying_staged_rows_rolls_back_map_and_points(self):
        self.seed();query=MapQuery(self.store.path)
        before=query.query();points=list(self.store.db.execute('SELECT * FROM points'))
        self.store.prepare(self.volume,full=False)
        self.store.begin_directory('2');self.node('2','1',3*GB)
        with patch('machine_audit.scanner.store.save_hierarchy',side_effect=sqlite3.OperationalError('synthetic failure')):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.finish_volume(self.volume,Blocks(),[],full=False,lean=True)
        self.assertEqual(query.query(),before)
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM points')],[tuple(r) for r in points])
        self.assertEqual(len(query.query('children',parent_id='2')['rows']),50)

    def test_publication_budget_refusal_keeps_the_last_complete_map(self):
        self.seed();query=MapQuery(self.store.path);before=query.query()
        self.store.prepare(self.volume,full=False);self.store.begin_directory('2')
        self.node('2','1',3*GB);self.store.flush()
        self.store.budget.limit_bytes=self.store.budget.usage()['bytes']
        with self.assertRaises(BudgetExceeded):self.store.finish_volume(self.volume,Blocks(),[],full=False,lean=True)
        self.assertEqual(query.query(),before)
        self.assertEqual(len(query.query('children',parent_id='2')['rows']),50)

    def test_sqlite_full_autorollback_preserves_the_original_failure(self):
        with self.assertRaises(sqlite3.OperationalError) as failure:
            with self.store.write():
                pages=self.store.db.execute('PRAGMA page_count').fetchone()[0]
                self.store.db.execute(f'PRAGMA max_page_count={pages}')
                self.store.db.execute("INSERT INTO map_meta VALUES('too_large',zeroblob(1000000))")
        self.assertEqual(failure.exception.sqlite_errorcode,sqlite3.SQLITE_FULL)
        self.assertFalse(self.store.db.in_transaction)
        self.assertIsNone(self.store.db.execute("SELECT value FROM map_meta WHERE key='too_large'").fetchone())

    def test_failed_full_publication_preserves_old_generation_after_staged_aggregation(self):
        self.seed();query=MapQuery(self.store.path);before=query.query()
        self.store.prepare(self.volume,full=True);staged=self.store.generation
        self.node('replacement',None,3*GB)
        with patch('machine_audit.scanner.store.save_hierarchy',side_effect=sqlite3.OperationalError('synthetic failure')):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.finish_volume(self.volume,Blocks(),[],full=True,lean=True)
        self.assertEqual(query.query(),before)
        self.assertEqual(self.store.db.execute('SELECT uses FROM nodes WHERE generation=?',(staged,)).fetchone()[0],3*GB)

    def test_parent_move_reconstructs_paths_and_recomputes_both_ancestor_totals(self):
        self.store.prepare(self.volume);self.node('root',None)
        self.node('left','root');self.node('right','root');self.node('leaf','left',2*GB)
        self.store.finish_volume(self.volume,Blocks(),[],full=True,lean=True)
        self.store.prepare(self.volume,full=False)
        self.assertTrue(self.store.relocate('leaf','right','leaf','/synthetic/right/leaf',False))
        self.store.finish_volume(self.volume,Blocks(),[],full=False,lean=True)
        self.store.prepare(self.volume,full=False)
        self.assertEqual(self.store.known_directory('leaf')['path'],'/synthetic/right/leaf')
        rows={r['id']:r for r in MapQuery(self.store.path).query('children')['rows']}
        self.assertEqual(rows['left']['uses'],0)
        self.assertEqual(rows['right']['uses'],2*GB)

    def test_reconstructed_path_detects_missing_and_cyclic_ancestry(self):
        self.store.prepare(self.volume)
        self.node('a','missing');self.store.flush()
        with self.assertRaisesRegex(ValueError,'missing_directory_ancestor'):self.store.known_directory('a')
        self.node('a','b');self.node('b','a');self.store.flush()
        with self.assertRaisesRegex(ValueError,'cyclic_directory_ancestry'):self.store.known_directory('a')
        for i in range(1025):self.node(f'deep{i}',None if i==0 else f'deep{i-1}')
        self.store.flush()
        with self.assertRaisesRegex(ValueError,'ancestry_limit'):self.store.known_directory('deep1024')

    def test_fresh_format_refuses_legacy_without_modifying_its_tables(self):
        legacy=Path(self.temp.name)/'legacy.sqlite'
        db=sqlite3.connect(legacy);db.execute('CREATE TABLE volumes(id TEXT)');db.execute("INSERT INTO volumes VALUES('kept')");db.commit();db.close()
        before=legacy.read_bytes()
        with self.assertRaisesRegex(ValueError,'fresh_store'):MapStore(legacy,self.store.budget)
        self.assertEqual(legacy.read_bytes(),before)
        with self.assertRaisesRegex(ValueError,'fresh_store'):MapQuery(legacy).query()
        db=sqlite3.connect(legacy)
        self.assertEqual(db.execute('SELECT id FROM volumes').fetchone()[0],'kept');db.close()


if __name__=='__main__':unittest.main()
