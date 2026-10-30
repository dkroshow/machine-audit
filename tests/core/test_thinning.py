"""Thinning rows saved before the top-consumer rule. Fixtures are synthetic."""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

from machine_audit.budget import Budget
from machine_audit.model import Clock,Field,Reading,Record
from machine_audit.query import Query
from machine_audit.store import Store,THINNED,encode

S=10**9
OLD_RULE={'minute':('process:','program:'),'15-minute':(),'hour':()}
PROCESSES=120
OWN=70
# Disjoint top lists, the same in every bucket: CPU 0-24, bytes written 35-59, footprint 95-119.
TOP={f'process:{pid}' for r in (range(25),range(35,60),range(95,120)) for pid in r}|{f'process:{OWN}'}


def saves(run,start,end,first=1):
    """One save per 300 s pass, with the collector's own reading."""
    batches=[];sequence=first-1
    for t in range(start,end,300):
        n=t//60;c=Clock(t*S,t*S,t*S);fields=[]
        for pid in range(PROCESSES):
            kw=dict(entity=f'process:{pid}',label=f'p{pid%40}',scope='process')
            rate=2.0 if pid<25 else 0.01
            fields+=[Field('process.cpu',rate*n,'seconds','counter',additive=True,rate_unit='CPUs',**kw),
                     Field('process.phys_footprint',pid,'bytes',component='memory',**kw),
                     Field('process.diskio_byteswritten',(pid+60)%120*n,'bytes','counter',component='storage',additive=True,**kw)]
        own=(Field('self.user_time',float(n),'seconds','counter',entity=f'process:{OWN}',label='collector',scope='managed_process'),)
        host=(Field('cpu.user',61.0*n,'seconds','counter'),Field('cpu.system',0.0,'seconds','counter'))
        batch=[]
        for source,component,values in (('synthetic.process','cpu',tuple(fields)),('self.process','self',own),('synthetic.cpu','cpu',host)):
            sequence+=1
            batch.append(Record(Reading(source,component,c,c,values),'boot',run,sequence,c.wall_ns,c))
        batches.append(batch)
    return batches


class ThinningTests(unittest.TestCase):
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

    def saved_before_the_rule(self,store):
        """Two runs under the previous release share an hour; a third saved under the rule."""
        with patch('machine_audit.store.RANKED',OLD_RULE):
            for batch in saves('old-a',0,5400):store.append(batch)
            store.stop_run('old-a',5400*S)
            for batch in saves('old-b',5400,7800):store.append(batch)
            store.stop_run('old-b',7800*S)
        for batch in saves('new',7800,9000):store.append(batch)
        store.stop_run('new',9000*S)

    @staticmethod
    def rows(store):
        return store.db.execute('SELECT id,run_id,resolution,bucket_ns,source,component,entity,sequence_min,sequence_max,who_rank,payload FROM summaries ORDER BY id').fetchall()

    @staticmethod
    def thinnable(row):
        return row[2] in ('15-minute','hour') and row[5]!='self' and row[6].startswith('process:')

    def test_it_deletes_only_process_rows_outside_each_buckets_top(self):
        # Kenny's approval covers these rows and nothing else: 15-minute and hour rows of
        # processes outside their bucket's top consumers. Everything else stays byte for
        # byte, who's answers do not move, and each span is recorded but not called pruned.
        store=self.store();self.saved_before_the_rule(store)
        before=self.rows(store)
        query=Query(store)
        def answers():
            return (query.who(0,7200*S,signal='process.cpu',component='cpu',limit=2000),
                    query.who(0,7200*S,signal='process.diskio_byteswritten',component='storage',limit=2000))
        cpu,disk=answers()
        self.assertEqual({r[6] for r in before if self.thinnable(r) and r[1].startswith('old')},{f'process:{pid}' for pid in range(PROCESSES)})
        result=store.thin_process_rows()
        after=self.rows(store)
        self.assertEqual([r for r in after if not self.thinnable(r)],[r for r in before if not self.thinnable(r)])
        self.assertEqual(sorted(r[1:] for r in after if self.thinnable(r)),sorted(r[1:] for r in before if self.thinnable(r) and r[6] in TOP))
        self.assertTrue(result['complete'])
        self.assertEqual((result['rows_deleted'],result['rows_unread']),(len(before)-len(after),0))
        self.assertEqual(result['payload_bytes_deleted'],sum(len(r[-1]) for r in before)-sum(len(r[-1]) for r in after))
        again=answers()
        for was,now in zip((cpu,disk),again):
            self.assertEqual((was['rows'],was['total_rows'],was['accounting']),(now['rows'],now['total_rows'],now['accounting']))
        records=store.db.execute('SELECT kind,start_ns,end_ns,reason FROM retention ORDER BY kind').fetchall()
        self.assertEqual(records,[('15-minute',0,8100*S,THINNED),('hour',0,10800*S,THINNED)])
        coverage=query.coverage(0,7200*S)['coverage']
        self.assertEqual(len(coverage['retention']),2)
        self.assertEqual([g for g in coverage['gaps'] if g['status']=='pruned'],[])
        path=store.path.parent/'chunk.sqlite'
        store.export_chunk(path,device_id='device',run_id='old-a')
        self.assertEqual(json.loads(path.with_suffix('.manifest.json').read_text())['pruned_ranges'],[])
        self.assertEqual(store.thin_process_rows(),dict(result,batches=0,rows_deleted=0,payload_bytes_deleted=0))

    def test_an_interrupted_run_resumes_to_the_same_result(self):
        # Stopped after any transaction, even with a process's CPU row deleted and its
        # memory row not, the next run must rank the same top and finish the same.
        whole=self.store('whole');self.saved_before_the_rule(whole)
        whole.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        (Path(self.tmp.name)/'parts').mkdir()
        shutil.copy(whole.path,Path(self.tmp.name)/'parts'/'readings.sqlite')
        parts=self.store('parts')
        whole.thin_process_rows()
        steps=0
        with patch('machine_audit.store.PRUNE_BATCH',50):
            while not parts.thin_process_rows(1)['complete']:steps+=1
        self.assertGreater(steps,10)
        self.assertEqual(self.rows(parts),self.rows(whole))
        self.assertEqual(parts.db.execute('SELECT * FROM retention ORDER BY kind').fetchall(),whole.db.execute('SELECT * FROM retention ORDER BY kind').fetchall())

    def test_the_largest_batches_stay_inside_their_declared_wal_pages(self):
        # Thinning shares the prune transaction: 4,096 rows with overflow payloads and page
        # moves must stay inside the frames it reserved, or admission promises too little.
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
        store=self.store(budget=lambda root:ObservedBudget(root,limit_bytes=2*1024**3,free_margin_bytes=0))
        db=store.db
        db.execute('BEGIN')
        for n in range(1500):
            for component,signal,size in (('cpu','process.cpu',n),('memory','process.phys_footprint',n),('storage','process.diskio_byteswritten',n)):
                row=dict(signal=signal,unit='bytes',kind='counter',scope='process',scale=1.0,component=component,entity=f'process:{n}',total=float(size),max=float(size),noise=bytes(range(256)).hex()*12+str(n))
                payload=zlib.compress(encode([2,[[sorted(row),[[row[k] for k in sorted(row)]]]]]).encode(),0)
                db.execute('INSERT INTO summaries(run_id,sequence_min,sequence_max,resolution,bucket_ns,source,component,entity,payload) VALUES(?,?,?,?,?,?,?,?,?)',
                           ('run',n,n,'hour',0,'synthetic.process',component,f'process:{n}',payload))
        db.execute("INSERT INTO runs(run_id,boot_id,started_ns) VALUES('run','boot',0)")
        db.execute('COMMIT');db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        result=store.thin_process_rows()
        self.assertEqual((result['batches'],result['rows_deleted']),(2,4425))  # all but 25 processes
        self.assertEqual(len(ObservedBudget.samples),2)
        for frames,bound in ObservedBudget.samples:
            self.assertGreater(frames,0);self.assertLessEqual(frames,bound)

    def test_it_runs_beside_a_collector_that_keeps_saving(self):
        # The owner thins Big Mac's store with the collector running: a separate process and
        # connection, taking turns through the budget. Neither may fail the other, and rows
        # saved meanwhile stay.
        store=self.store();self.saved_before_the_rule(store)
        before=store.db.execute("SELECT count(*) FROM summaries WHERE run_id='new'").fetchone()[0]
        config=store.path.parent/'config.json'
        config.write_text(json.dumps({'data_dir':str(store.path.parent)}))
        child=subprocess.Popen([sys.executable,'-m','machine_audit','--config',str(config),'thin-process-rows'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=Path(__file__).parents[2])
        for batch in saves('live',9000,12600):
            store.append(batch)
            if child.poll() is not None:break
        out,err=child.communicate(timeout=120)
        self.assertEqual((child.returncode,err),(0,''))
        self.assertTrue(json.loads(out)['complete'])
        self.assertGreater(store.db.execute("SELECT count(*) FROM summaries WHERE run_id='live'").fetchone()[0],0)
        self.assertEqual(store.db.execute("SELECT count(*) FROM summaries WHERE run_id='new'").fetchone()[0],before)
        left=store.db.execute("SELECT count(DISTINCT entity) FROM summaries WHERE run_id='old-a' AND resolution='hour' AND component='cpu' AND entity LIKE 'process:%'").fetchone()[0]
        self.assertEqual(left,len(TOP))
