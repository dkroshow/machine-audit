"""Converting a store saved before share ranking. Fixtures are synthetic."""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from machine_audit.budget import Budget
from machine_audit.model import Clock,Field,Reading,Record
from machine_audit.query import Query
from machine_audit.store import CONVERTED,Store,unpack_summaries


@contextmanager
def saved_as_before():
    """The previous release: every process and program saved its 15-minute and hour rows
    at each save; only minutes were ranked."""
    original=Store._top_buckets
    def previous(self,records,buckets,summaries,held):
        for resolution in ('15-minute','hour'):
            for bucket,rows in buckets[resolution].items():
                for row in rows:summaries[(resolution,bucket,row['source'],row['component'],row['entity'])].append(row)
            buckets[resolution].clear()
        return original(self,records,buckets,summaries,held)
    with patch.object(Store,'_top_buckets',previous):yield


S=10**9
PROCESSES=120
# Each component's main contributors, the same in every bucket: CPU 0-4, footprint 110-119,
# bytes written 20-24. Everyone else is under 1% of the bucket's use.
MAIN={'cpu':range(5),'memory':range(110,120),'storage':range(20,25)}


def saves(run,start,end,first=1,processes=PROCESSES,cpu=lambda pid:10.0 if pid<5 else 0.05):
    """One save per 300 s pass of processes, programs (label pid%40) and the host."""
    batches=[];sequence=first-1
    for t in range(start,end,300):
        n=t//300;c=Clock(t*S,t*S,t*S);fields=[]
        for pid in range(processes):
            kw=dict(entity=f'process:{pid}',label=f'p{pid%40}',scope='process')
            fields+=[Field('process.cpu',cpu(pid)*n,'seconds','counter',additive=True,rate_unit='CPUs',**kw),
                     Field('process.phys_footprint',10**9 if pid>=110 else 10**6,'bytes',component='memory',**kw),
                     Field('process.diskio_byteswritten',(10**6 if 20<=pid<25 else 100)*n,'bytes','counter',component='storage',additive=True,**kw)]
        host=(Field('cpu.user',75.0*n,'seconds','counter'),Field('cpu.system',0.0,'seconds','counter'))
        batch=[]
        for source,values in (('synthetic.process',tuple(fields)),('synthetic.cpu',host)):
            sequence+=1
            batch.append(Record(Reading(source,'cpu',c,c,values),'boot',run,sequence,c.wall_ns,c))
        batches.append(batch)
    return batches


class ConversionTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).parent/'.tmp';root.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=root);self.stores=[]

    def tearDown(self):
        for store in self.stores:store.close()
        self.tmp.cleanup()

    def store(self,name='a',budget=None):
        root=Path(self.tmp.name)/name;root.mkdir(exist_ok=True)
        store=Store(root/'readings.sqlite',budget(root) if budget else Budget(root,limit_bytes=10**9,free_margin_bytes=0))
        self.stores.append(store)
        return store

    def saved_before(self,name='a',budget=None,runs=(('old-a',0,5400),('old-b',5400,7800))):
        """A version 2 store: runs saved as the previous release saved them, reopened."""
        store=self.store(name,budget)
        with saved_as_before():
            for run,start,end in runs:
                for batch in saves(run,start,end):store.append(batch)
                store.stop_run(run,end*S)
        store.db.execute('PRAGMA user_version=2');store.close();self.stores.remove(store)
        return self.store(name,budget)

    @staticmethod
    def rows(store):
        return store.db.execute('SELECT id,run_id,resolution,bucket_ns,source,component,entity,sequence_min,sequence_max,who_rank,payload FROM summaries ORDER BY id').fetchall()

    @staticmethod
    def converts(row):
        return row[2] in ('15-minute','hour') and row[5]!='self' and (row[6].startswith('process:') or row[6]=='rest:process')

    @staticmethod
    def version(store):
        return store.db.execute('PRAGMA user_version').fetchone()[0]

    def convert(self,store):
        for _ in range(500):
            if self.version(store)==3:return
            store.maintain()
        self.fail('conversion did not finish')

    def test_it_keeps_each_buckets_main_processes_and_a_rest_and_nothing_else_changes(self):
        # Kenny approved deleting these rows and nothing else: 15-minute and hour process
        # rows outside each bucket's main contributors. Their use moves into one rest row per
        # component, so totals still add up; every other row stays byte for byte, who's
        # answers do not move, and each span is recorded but not called pruned.
        store=self.saved_before()
        reader=Store(store.path,read_only=True);reader.close()  # a version 2 store still reads
        before=self.rows(store)
        query=Query(store)
        def answers():
            return [query.who(0,7200*S,signal=signal,component=component,limit=2000) for signal,component in
                    (('process.cpu','cpu'),('process.diskio_byteswritten','storage'))]
        was=answers()
        self.convert(store)
        after=self.rows(store)
        self.assertEqual([r for r in after if not self.converts(r)],[r for r in before if not self.converts(r)])
        buckets={(r[1],r[2],r[3]) for r in before if self.converts(r)}
        for run,resolution,bucket in buckets:
            def rows(rows,component):return {r[6]:r for r in rows if self.converts(r) and (r[1],r[2],r[3],r[5])==(run,resolution,bucket,component)}
            for component,main in MAIN.items():
                old,new=rows(before,component),rows(after,component)
                kept={f'process:{pid}' for pid in main}
                counter={'cpu':'process.cpu','storage':'process.diskio_byteswritten'}.get(component)
                if counter and all(row['total'] is None for row in unpack_summaries(old[f'process:{main[0]}'][-1]) if row['signal']==counter):
                    kept=set()  # a bucket with only a run's first pass has no counter use yet
                self.assertEqual(set(new),kept|{'rest:process'},(run,resolution,bucket,component))
                self.assertEqual([new[e] for e in sorted(kept)],[old[e] for e in sorted(kept)])
                rest={row['signal']:row for row in unpack_summaries(new['rest:process'][-1])}
                dropped=[row for e,r in old.items() if e not in kept for row in unpack_summaries(r[-1])]
                for signal,row in rest.items():
                    values=[d['sum'] for d in dropped if d['signal']==signal]
                    self.assertEqual((row['entities'],row['sum']),(len(values),sum(values)),(component,signal))
        for was_answer,now in zip(was,answers()):
            self.assertEqual((was_answer['rows'],was_answer['total_rows'],was_answer['accounting']),(now['rows'],now['total_rows'],now['accounting']))
        records=store.db.execute('SELECT kind,start_ns,end_ns,reason FROM retention ORDER BY kind').fetchall()
        self.assertEqual(records,[('15-minute',0,8100*S,CONVERTED),('hour',0,10800*S,CONVERTED)])
        coverage=query.coverage(0,7200*S)['coverage']
        self.assertEqual((len(coverage['retention']),[g for g in coverage['gaps'] if g['status']=='pruned']),(2,[]))
        path=store.path.parent/'chunk.sqlite'
        store.export_chunk(path,device_id='device',run_id='old-a')
        self.assertEqual(json.loads(path.with_suffix('.manifest.json').read_text())['pruned_ranges'],[])
        store.maintain()
        self.assertEqual(self.rows(store),after)

    def test_a_conversion_stopped_after_any_step_resumes_to_the_same_result(self):
        # Each restart begins the walk again with nothing recorded but the rows: a bucket's
        # rest holds its cut, so one stopped part way, with some rows deleted and others not,
        # finishes the same and a finished one gives nothing more.
        whole=self.saved_before('whole')
        whole.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        (Path(self.tmp.name)/'parts').mkdir()
        shutil.copy(whole.path,Path(self.tmp.name)/'parts'/'readings.sqlite')
        self.convert(whole)
        steps=0
        with patch('machine_audit.store.PRUNE_BATCH',50):
            while True:
                parts=self.store('parts')
                if self.version(parts)==3:break
                parts.maintain();parts.close();self.stores.remove(parts);steps+=1
        self.assertGreater(steps,10)
        self.assertEqual(self.rows(parts),self.rows(whole))
        self.assertEqual(parts.db.execute('SELECT * FROM retention ORDER BY kind').fetchall(),whole.db.execute('SELECT * FROM retention ORDER BY kind').fetchall())

    def test_the_largest_steps_stay_inside_their_declared_wal_pages(self):
        # A step shares the prune transaction and adds rest rows: 4,096 deletes and the
        # inserts must stay inside the frames it reserved, or admission promises too little.
        class ObservedBudget(Budget):
            samples=[]
            @contextmanager
            def sqlite_write(self,db,path,*args,**kwargs):
                with super().sqlite_write(db,path,*args,**kwargs) as result:
                    yield result
                    if kwargs.get('dirty_pages'):
                        page=db.execute('PRAGMA page_size').fetchone()[0]
                        wal=Path(str(path)+'-wal').stat().st_size
                        self.samples.append((max(0,(wal-32)//(page+24)),kwargs['dirty_pages']))
        store=self.store('wide',lambda root:ObservedBudget(root,limit_bytes=2*1024**3,free_margin_bytes=0))
        with saved_as_before():
            for batch in saves('wide',0,600,processes=1500,cpu=lambda pid:1.0):store.append(batch)
        store.db.execute('PRAGMA user_version=2');store.close();self.stores.remove(store)
        store=self.store('wide',lambda root:ObservedBudget(root,limit_bytes=2*1024**3,free_margin_bytes=0))
        ObservedBudget.samples.clear()
        self.convert(store)
        deletes=[bound for frames,bound in ObservedBudget.samples if bound>1000]
        self.assertGreaterEqual(len(deletes),2)  # more than one transaction's rows in the hour
        for frames,bound in ObservedBudget.samples:
            self.assertGreater(frames,0);self.assertLessEqual(frames,bound)

    def test_saves_continue_while_it_converts_and_a_failure_costs_no_save(self):
        # It runs inside the collector's own saves, one step each: a live run's rows are
        # saved as usual meanwhile. A step that fails stops converting until the next start
        # and is reported; the save it rode on is still made.
        store=self.saved_before()
        live=iter(saves('live',9000,90000,first=1))
        with patch.object(Store,'_convert_bucket',side_effect=RuntimeError('synthetic')):
            store.append(next(live))
        self.assertEqual((store.conversion_error,self.version(store)),('RuntimeError',2))
        self.assertEqual(store.db.execute("SELECT last_sequence FROM runs WHERE run_id='live'").fetchone()[0],2)
        store.close();self.stores.remove(store)
        store=self.store()
        for _ in range(500):
            if self.version(store)==3:break
            store.append(next(live))
        self.assertEqual((store.conversion_error,self.version(store)),(None,3))
        self.assertGreater(store.db.execute("SELECT count(*) FROM summaries WHERE run_id='live' AND resolution='hour' AND entity LIKE 'process:%'").fetchone()[0],0)
