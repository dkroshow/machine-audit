"""Restart recovery under a shared cap; only disposable synthetic stores."""
import base64
from pathlib import Path
from contextlib import contextmanager
import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import machine_audit.store
from machine_audit.budget import Budget, BudgetExceeded
from machine_audit.collector import Collector
from machine_audit.model import Clock, Field, Reading, Record
from machine_audit.query import Query
from machine_audit.store import Store, MINUTE, pack_summaries, unpack_summaries
from test_conversion import saved_as_before


class BudgetRecoveryTests(unittest.TestCase):
    def test_budget_refusal_carries_a_code_never_free_text(self):
        # Scan and status records keep the refusal reason; a path or message
        # raised in its place would otherwise reach a run record.
        self.assertEqual(BudgetExceeded('managed_storage_limit').code, 'managed_storage_limit')
        with self.assertRaises(ValueError):
            BudgetExceeded('synthetic private path')

    def test_prune_recent_covered_raw_does_not_scan_old_coverage_per_reading(self):
        # Retained coverage outlives raw. A forward proof search repeated that
        # old prefix for each reading, stalling flushes before write admission.
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as root:
            store=Store(Path(root)/'readings.sqlite',Budget(root,free_margin_bytes=0))
            snapshot=sqlite3.connect(':memory:')
            try:
                db=store.db;history=33000;count=32768;per_minute=128
                first=history-count//per_minute
                db.execute('BEGIN')
                db.executemany('INSERT INTO coverage VALUES(?,?,?,?,?,?,?,?)',
                    (('run',(i+1)*per_minute,'minute','source','cpu',i*MINUTE,(i+1)*MINUTE,'{}')
                     for i in range(1,history+1)))
                # Keep the oldest uncovered row, then prune a later covered
                # window rather than prematurely discarding summary history.
                db.execute("INSERT INTO readings VALUES('run',1,0,'source','cpu',?)",(b'{}',))
                db.executemany('INSERT INTO readings VALUES(?,?,?,?,?,?)',
                    (('run',first*per_minute+i+1,(first+i//per_minute)*MINUTE+1,'source','cpu',b'{}')
                     for i in range(count)))
                db.execute('COMMIT');db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                db.backup(snapshot)
                proofs=[]
                db.set_trace_callback(lambda sql: proofs.append(sql) if sql.startswith('SELECT 1 FROM readings r') else None)
                try:self.assertTrue(store.prune_oldest())
                finally:db.set_trace_callback(None)
                self.assertEqual(db.execute('SELECT count(*) FROM readings').fetchone()[0],count-per_minute+1)
                self.assertEqual(db.execute('SELECT wall_ns FROM readings WHERE sequence=1').fetchone()[0],0)
                self.assertEqual(db.execute("SELECT kind,reason FROM retention ORDER BY rowid").fetchall(),
                                 [('raw_preserved','missing_summary_coverage'),('raw','storage_budget')])
                self.assertEqual(db.execute('SELECT count(*) FROM coverage').fetchone()[0],history)
                # Replay the exact precheck SQL against the pre-prune snapshot,
                # measuring SQLite work without a machine-speed assertion.
                steps=0
                def bounded():
                    nonlocal steps
                    steps+=1000
                    return steps>100_000
                snapshot.set_progress_handler(bounded,1000)
                for sql in proofs:snapshot.execute(sql).fetchone()
                self.assertEqual(len(proofs),2)
                self.assertLess(steps,100_000)

                collector=Collector(store,(),run_id='new',boot_id='boot')
                collector.start()
                clock=Clock((history+1)*MINUTE,(history+1)*MINUTE,(history+1)*MINUTE)
                collector.ingest(Reading('fresh','memory',clock,clock,(Field('memory.used',1,'bytes'),)))
                before=db.execute('SELECT count(*) FROM readings').fetchone()[0]
                with patch.object(store,'_write',side_effect=BudgetExceeded('managed_storage_limit')), \
                     patch('machine_audit.store.PRUNE_PRECHECK_STEPS',0):
                    self.assertFalse(collector.flush())
                self.assertEqual(collector.status()['save_error'],'prune_precheck_limit')
                self.assertGreater(collector.tail_bytes,0)
                self.assertEqual(collector.committed,0)
                self.assertEqual(db.execute('SELECT count(*) FROM readings').fetchone()[0],before)
                self.assertEqual(db.execute('SELECT count(*) FROM retention').fetchone()[0],2)
                self.assertTrue(collector.flush())
                self.assertTrue(collector.close())
            finally:
                snapshot.close();store.close()

    def test_uncovered_window_does_not_block_later_covered_raw_windows(self):
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as root:
            store=Store(Path(root)/'readings.sqlite',Budget(root,free_margin_bytes=0))
            try:
                records=[]
                for seq,t in enumerate((1,60_000_000_001,120_000_000_001),1):
                    clock=Clock(t,t,t)
                    records.append(Record(Reading('synthetic','memory',clock,clock,
                        (Field('memory.used',seq,'bytes'),)),'boot','run',seq,t,clock))
                store.append(records)
                store.db.execute('DELETE FROM coverage WHERE end_ns<60000000000')
                summaries=store.db.execute('SELECT count(*) FROM summaries').fetchone()[0]
                for sequence in (2,3):
                    self.assertTrue(store.prune_oldest())
                    self.assertIsNone(store.db.execute('SELECT 1 FROM readings WHERE sequence=?',(sequence,)).fetchone())
                    self.assertEqual(store.db.execute('SELECT payload FROM readings WHERE sequence=1').fetchone()[0],records[0].packed)
                    self.assertEqual(store.db.execute('SELECT count(*) FROM summaries').fetchone()[0],summaries)
                retained=store.db.execute("SELECT start_ns,end_ns,reason FROM retention WHERE kind='raw_preserved'").fetchall()
                self.assertEqual(retained,[(1,60_000_000_000,'missing_summary_coverage')])
                answer=Query(store).coverage(0,180_000_000_000)['coverage']
                self.assertTrue(any(r['kind']=='raw_preserved' for r in answer['retention']))
                self.assertFalse(any(r.get('kind')=='raw_preserved' for r in answer['gaps']))
                manifest=store.export_chunk(Path(root)/'chunk.sqlite',device_id='device',run_id='run')
                self.assertFalse(any(r['kind']=='raw_preserved' for r in manifest['pruned_ranges']))
            finally:store.close()

    def test_a_long_uncovered_prefix_resumes_and_never_blocks_pruning(self):
        # Big Mac after a crash: every flush restarted the bounded proof search at the
        # oldest raw window, ran out inside a long uncovered prefix and saved nothing.
        # Each search now resumes after the windows already recorded; one that still
        # runs out prunes the oldest summaries instead.
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as root:
            store=Store(Path(root)/'readings.sqlite',Budget(root,free_margin_bytes=0))
            try:
                records=[]
                for seq,t in enumerate(range(1,300*60,10),1):
                    clock=Clock(t*10**9,t*10**9,t*10**9)
                    records.append(Record(Reading('synthetic','memory',clock,clock,
                        (Field('memory.used',seq,'bytes'),)),'boot','run',seq,clock.wall_ns,clock))
                store.append(records)
                store.db.execute('DELETE FROM coverage WHERE end_ns<?',(200*MINUTE,))
                def raw(lo,hi):
                    return store.db.execute('SELECT count(*) FROM readings WHERE wall_ns>=? AND wall_ns<?',(lo,hi)).fetchone()[0]
                reasons=[]
                with patch('machine_audit.store.PRUNE_PRECHECK_STEPS',3_000):
                    while raw(200*MINUTE,300*MINUTE)==600:
                        self.assertTrue(store.prune_oldest())
                        reasons.append(store.db.execute('SELECT reason FROM retention ORDER BY rowid DESC LIMIT 1').fetchone()[0])
                        self.assertLess(len(reasons),20)
                self.assertEqual(raw(0,200*MINUTE),1200)  # uncovered readings stay
                self.assertIn('storage_budget_raw_uncovered',reasons[:-1])  # ran out, pruned summaries
                self.assertEqual(reasons[-1],'fine_floor_unmet')  # then reached covered raw, all under 48 hours old
            finally:store.close()

    def reads(self,collector,first,last):
        for t in range(first,last):
            clock=Clock(t*10**9,t*10**9,t*10**9)
            collector.ingest(Reading('synthetic.process','cpu',clock,clock,tuple(
                Field('process.cpu',float(t*pid),'seconds','counter',entity=f'process:{pid}',label=f'p{pid}',scope='process',additive=True)
                for pid in (1,2))))

    @staticmethod
    def saved(store):
        return (store.db.execute('SELECT run_id,sequence,payload FROM readings ORDER BY run_id,sequence').fetchall(),
                store.db.execute('SELECT run_id,resolution,bucket_ns,entity,payload FROM summaries ORDER BY 1,2,3,4').fetchall(),
                store.db.execute('SELECT run_id,last_sequence,stopped_ns FROM runs ORDER BY 1').fetchall())

    def held_minute(self,store,run):
        return {entity:[(row['sample_count'],row['total']) for row in unpack_summaries(payload)] for entity,payload in store.db.execute(
            "SELECT entity,payload FROM summaries WHERE run_id=? AND resolution='minute' AND bucket_ns=? AND entity LIKE 'process:%'",(run,60*10**9))}

    def test_a_stop_at_the_cap_checkpoints_without_merging_and_the_next_start_saves_it(self):
        # Big Mac at the cap: stopping pruned or merged until it outlasted the stop and
        # was killed. A stop now merges nothing: it checkpoints the unsaved readings and
        # the held minute, and the next start saves them under their own run.
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as root:
            store=Store(Path(root)/'readings.sqlite',Budget(root,free_margin_bytes=0))
            def at_cap():
                # Room for a checkpoint, not for saving the readings it holds.
                store.budget.limit_bytes=store.budget.usage()['bytes']+2*1024**2
            try:
                # Older raw-heavy history: pruning could make room, slowly and by deleting it.
                history=[]
                for n in range(300):
                    clock=Clock((n*10-20000)*10**9,(n*10-20000)*10**9,(n*10-20000)*10**9)
                    history.append(Record(Reading('synthetic.blob','storage',clock,clock,tuple(
                        Field(f'storage.f{i}',(n*7919+i*104729)%10**9,'bytes') for i in range(300))),'boot','history',n+1,clock.wall_ns,clock))
                for lo in range(0,300,60):store.append(history[lo:lo+60])
                store.stop_run('history',-17000*10**9)
                first=Collector(store,(),run_id='first',boot_id='boot');first.start()
                self.reads(first,0,70);self.assertTrue(first.flush())
                self.reads(first,70,100)  # unsaved tail; minute 1 is held
                at_cap();before=self.saved(store);began=time.monotonic();sql=[]
                store.db.set_trace_callback(sql.append)
                self.assertTrue(first.close(timeout_s=10))
                store.db.set_trace_callback(None)
                self.assertFalse([q for q in sql if not q.startswith('SELECT last_sequence FROM runs')],'the stop wrote or merged')
                self.assertLess(time.monotonic()-began,10)
                self.assertEqual((first.stop_code,self.saved(store)),('stop_checkpointed',before))  # nothing saved was lost
                self.assertFalse(first.flush(),'a save after the cutoff would save the checkpoint twice')
                checkpoint1,=Path(root).glob('unsaved-first-*.json')
                self.assertEqual(checkpoint1.stat().st_mode&0o777,0o600)
                kept=checkpoint1.read_bytes()
                # A start that cannot save it keeps it, says so even after its own saves
                # succeed, and its own stop never replaces that file.
                second=Collector(store,(),run_id='second',boot_id='boot')
                store.budget.limit_bytes=10**9
                with patch.object(store,'recover_unsaved',return_value=[BudgetExceeded('managed_storage_limit')]):second.start()
                self.reads(second,200,230);self.assertTrue(second.flush())
                self.assertEqual((second.status()['save_error'],second.status()['recovery_error']),(None,'managed_storage_limit'))
                self.reads(second,230,240);at_cap()
                self.assertTrue(second.close(timeout_s=10))
                self.assertEqual(checkpoint1.read_bytes(),kept)
                self.assertEqual(len(list(Path(root).glob('unsaved-second-*.json'))),1)
                store.budget.limit_bytes=10**9
                third=Collector(store,(),run_id='third',boot_id='boot');third.start()
                self.assertIsNone(third.recovery_error)
                self.assertEqual(list(Path(root).glob('unsaved-first-*'))+list(Path(root).glob('unsaved-second-*')),[])
                runs=dict(store.db.execute("SELECT run_id,last_sequence FROM runs WHERE stopped_ns IS NOT NULL").fetchall())
                self.assertEqual((runs['first'],runs['second']),(first.sequence,second.sequence))  # every reading, under its own run
                self.assertEqual({e:[n for n,_ in rows] for e,rows in self.held_minute(store,'first').items()},
                                 {'process:1':[39],'process:2':[39]})  # the whole held minute, 61-99 s
                self.assertTrue(third.close())
            finally:store.close()

    def coarse_process_rows(self,store,run):
        return {(resolution,entity):[(row['sample_count'],row['total']) for row in unpack_summaries(payload)] for resolution,entity,payload in store.db.execute(
            "SELECT resolution,entity,payload FROM summaries WHERE run_id=? AND resolution IN ('15-minute','hour') AND entity LIKE 'process:%'",(run,))}

    def test_the_held_quarter_and_hour_survive_a_stop_checkpoint(self):
        # A process row of an open quarter or hour exists only in memory: a stop must carry
        # it across the restart, or every stop would lose up to an hour of process history.
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as root:
            store=Store(Path(root)/'readings.sqlite',Budget(root,free_margin_bytes=0))
            try:
                run=Collector(store,(),run_id='run',boot_id='boot');run.start()
                self.reads(run,0,70);self.assertTrue(run.flush());self.reads(run,70,100)
                self.assertTrue(run.close())
                self.assertEqual(self.coarse_process_rows(store,'run'),{})
                checkpoint,=Path(root).glob('unsaved-run-*.json')
                self.assertEqual(set(json.loads(checkpoint.read_bytes())['held']),{'minute','15-minute','hour'})
                after=Collector(store,(),run_id='after',boot_id='boot');after.start()
                self.assertIsNone(after.recovery_error);self.assertTrue(after.close())
                # 100 readings, 99 s of (1 and 2 CPU s)/s, split by the save at 70.
                self.assertEqual(self.coarse_process_rows(store,'run'),{(resolution,entity):[(100,total)] for resolution in ('15-minute','hour')
                                                                        for entity,total in (('process:1',99.0),('process:2',198.0))})
            finally:store.close()

    def test_a_checkpoint_from_the_previous_release_recovers_and_extends_its_saved_rows(self):
        # An upgrade restarts from the previous release's stop. Its file holds a minute only,
        # and it had saved every process's quarter and hour rows at each save. Recovery must
        # read that file and add the unsaved tail to those rows, not replace them with it.
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as root:
            store=Store(Path(root)/'readings.sqlite',Budget(root,free_margin_bytes=0))
            try:
                with saved_as_before():
                    run=Collector(store,(),run_id='run',boot_id='boot');run.start()
                    self.reads(run,0,70);self.assertTrue(run.flush());self.reads(run,70,100)
                    self.assertTrue(run.close())
                checkpoint,=Path(root).glob('unsaved-run-*.json')
                data=json.loads(checkpoint.read_bytes())
                data.update(format='machine-audit-unsaved-v1',held=data['held']['minute'])
                checkpoint.write_text(json.dumps(data))
                self.assertEqual(self.coarse_process_rows(store,'run')[('hour','process:1')],[(70,69.0)])
                after=Collector(store,(),run_id='after',boot_id='boot');after.start()
                self.assertIsNone(after.recovery_error);self.assertTrue(after.close())
                self.assertEqual(list(Path(root).glob('unsaved-run-*')),[])
                self.assertEqual(self.coarse_process_rows(store,'run'),{(resolution,entity):[(100,total)] for resolution in ('15-minute','hour')
                                                                        for entity,total in (('process:1',99.0),('process:2',198.0))})
                self.assertEqual({e:[n for n,_ in rows] for e,rows in self.held_minute(store,'run').items()},{'process:1':[39],'process:2':[39]})
            finally:store.close()

    def test_a_checkpoint_is_saved_once_with_its_stop_whatever_point_recovery_reaches(self):
        # Recovery saves a checkpoint's readings, held minute and stop marker in one
        # transaction, so a stop marker proves it was saved: a crash before or after that
        # commit, an empty tail or a second recovery must neither lose nor double it.
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        def stopped(root,unsaved):
            store=Store(Path(root)/'readings.sqlite',Budget(root,free_margin_bytes=0))
            run=Collector(store,(),run_id='run',boot_id='boot');run.start()
            self.reads(run,0,100-unsaved);self.assertTrue(run.flush());self.reads(run,100-unsaved,100)
            self.assertTrue(run.close())
            return store,run
        def recover(store):
            after=Collector(store,(),run_id=f'after-{time.monotonic_ns()}',boot_id='boot');after.start()
            error=after.recovery_error;self.assertTrue(after.close());return error
        # Pinned from this fixture: 100 process readings and a minute-1 total of
        # (99-60)*1 and (99-60)*2 CPU seconds over 39 samples.
        expected={'process:1':[(39,39.0)],'process:2':[(39,78.0)]}
        for unsaved in (30,0):  # 0: an empty tail with only the held minute
            with self.subTest(unsaved=unsaved),tempfile.TemporaryDirectory(dir=scratch) as root:
                store,run=stopped(root,unsaved)
                try:
                    checkpoint,=Path(root).glob('unsaved-run-*.json');cutoff=int(checkpoint.stem.rsplit('-',1)[1])
                    # A crash inside the transaction, after its first writes, before commit.
                    with patch('machine_audit.store.UPSERT_SUMMARY','INSERT INTO missing_table VALUES(?)'):
                        self.assertEqual(recover(store),'sqlite_error')
                    self.assertTrue(checkpoint.exists())
                    self.assertEqual(store.db.execute("SELECT last_sequence,stopped_ns FROM runs WHERE run_id='run'").fetchone(),
                                     (run.committed,None))
                    unlink=Path.unlink
                    def kept(path,*args,**kwargs):
                        if path==checkpoint:raise OSError('synthetic crash after commit')
                        return unlink(path,*args,**kwargs)
                    with patch.object(Path,'unlink',kept):self.assertEqual(recover(store),'OSError')
                    saved=self.saved(store)
                    for attempt in range(2):  # the file left behind, then nothing left
                        self.assertIsNone(recover(store))
                        self.assertEqual([r for r in self.saved(store)[1] if r[0]=='run'],[r for r in saved[1] if r[0]=='run'])
                    self.assertFalse(checkpoint.exists())
                    count,=store.db.execute("SELECT count(*) FROM readings WHERE run_id='run' AND source='synthetic.process'").fetchone()
                    self.assertEqual((count,store.db.execute("SELECT stopped_ns FROM runs WHERE run_id='run'").fetchone()[0]),(100,cutoff))
                    self.assertEqual(self.held_minute(store,'run'),expected)
                finally:store.close()
        # An older release could save a checkpoint's readings and die before its stop:
        # its held minute is then stale. It is kept and named, never saved wrong.
        with tempfile.TemporaryDirectory(dir=scratch) as root:
            store,run=stopped(root,30)
            try:
                checkpoint,=Path(root).glob('unsaved-run-*.json')
                data=json.loads(checkpoint.read_bytes())
                store.append([Record.unpack(base64.b64decode(p)) for p in data['records']])
                after=Collector(store,(),run_id='after',boot_id='boot');after.start()
                self.reads(after,300,310);self.assertTrue(after.flush())
                self.assertEqual(after.status()['recovery_error'],'unsaved_checkpoint_mismatch')
                self.assertTrue(checkpoint.exists())
                self.assertEqual(store.db.execute("SELECT stopped_ns FROM runs WHERE run_id='run'").fetchone()[0],None)
                self.assertTrue(after.close())
            finally:store.close()
        # A checkpoint named for one run but holding another run's readings is kept and
        # named before anything is saved or removed, whether or not its run has stopped.
        for applied in (False,True):
            with self.subTest(applied=applied),tempfile.TemporaryDirectory(dir=scratch) as root:
                store=Store(Path(root)/'readings.sqlite',Budget(root,free_margin_bytes=0))
                try:
                    clock=Clock(1,1,1)
                    def record(run):return Record(Reading('synthetic.process','cpu',clock,clock),'boot',run,1,1,clock)
                    store.start_run('a','boot',0);store.start_run('b','boot',0)
                    if applied:store.append([record('a')]);store.stop_run('a',1000)
                    checkpoint=store.spill('a',1000,[record('b').packed])
                    before=self.saved(store)
                    self.assertEqual([str(e) for e in store.recover_unsaved()],['unsaved_checkpoint_mismatch'])
                    self.assertEqual((checkpoint.exists(),self.saved(store)),(True,before))
                finally:store.close()

    def test_a_stop_cut_short_by_process_exit_keeps_every_completed_reading(self):
        # Big Mac's collector stopped 26 s after SIGTERM with no saved stop, no spill and
        # its last half minute lost. A stop that outlives its deadline returns, the process
        # exits and kills the unfinished save. Each case crosses a real process exit through
        # the real startup wrapper; a new process then reopens and recovers.
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        child=r"""
import json,os,sys,threading,time
from pathlib import Path
from unittest.mock import patch
repo,root,case=sys.argv[1:4]
sys.path.insert(0,repo)
import machine_audit.store
from machine_audit import startup
from machine_audit.budget import Budget
from machine_audit.collector import Collector
from machine_audit.config import Config
from machine_audit.model import Clock,Field,Reading,Record
from machine_audit.store import Store
entered=threading.Event()
class Hung:
    name='synthetic.hung';component='cpu';cadence_s=1
    def read(self):entered.set();threading.Event().wait()  # A native read that never returns.
config=Config(data_dir=root,port=0)
def run(ready):
    store=Store(Path(root)/'readings.sqlite',Budget(root,free_margin_bytes=0))
    collector=Collector(store,[Hung()] if case=='hung_read' else [],run_id='stopped',boot_id='boot');collector.start()
    def reads(first,last):
        for t in range(first,last):
            clock=Clock(t*10**9,t*10**9,t*10**9)
            collector.ingest(Reading('synthetic.process','cpu',clock,clock,tuple(
                Field('process.cpu',float(t*pid),'seconds','counter',entity=f'process:{pid}',label=f'p{pid}',scope='process',additive=True)
                for pid in (1,2))))
    reads(0,70);assert collector.flush();reads(70,100)  # Minute 1 is held, 70-99 s unsaved.
    if case=='killed_while_checkpointing':
        # Another run's valid checkpoint, not yet saved, beside the one about to be cut short.
        store.start_run('earlier','boot',0)
        store.spill('earlier',5000,[Record(Reading('synthetic.process','cpu',Clock(t,t,t),Clock(t,t,t)),'boot','earlier',t,t,Clock(t,t,t)).packed
                                    for t in (1,2,3)])
    ready()
    if case=='hung_read':assert entered.wait(5)
    if case=='blocked_reservation':
        # Another writer's reservation leaves no room until it finishes, which it never does.
        store.budget.limit_bytes=store.budget.usage()['bytes']+4*1024**2
        other=Budget(root,limit_bytes=store.budget.limit_bytes,free_margin_bytes=0);held=threading.Event()
        def hold():
            with other.reserve(store.budget.limit_bytes-other.usage()['bytes']-2048):held.set();threading.Event().wait()
        threading.Thread(target=hold,daemon=True).start();assert held.wait(5)
    def slow(*args,real=machine_audit.store.points):time.sleep(60);return real(*args)
    def killed(fd):os._exit(3)  # Killed after writing the checkpoint's bytes, before they are synced.
    with patch('machine_audit.store.points',slow) if case=='slow_derivation' else \
         patch('machine_audit.store.os.fsync',killed) if case=='killed_while_checkpointing' else patch.object(Store,'close',Store.close):
        clean=collector.close(timeout_s=1)
    print(json.dumps({'clean':clean,'code':collector.stop_code}))
    # As the CLI does: storage stays open while the closer runs, then the process exits.
    if not collector._closer.is_alive():store.close()
    return (0 if clean else 2),collector.stop_code
startup.guarded_run(config,run)
print(json.dumps(startup.read(config)))
"""
        for case,outcome in (('hung_read','checkpointed'),('slow_derivation','checkpointed'),
                             ('blocked_reservation','failed'),('killed_while_checkpointing','killed')):
            with self.subTest(case),tempfile.TemporaryDirectory(dir=scratch) as root:
                stopped=subprocess.run([sys.executable,'-I','-c',child,str(Path(__file__).parents[2]),root,case],
                                       capture_output=True,text=True,timeout=60)
                self.assertEqual(stopped.returncode,3 if outcome=='killed' else 0,stopped.stderr)
                if outcome=='failed':
                    # Nothing durable: the stop must say so, never claim a stopped run.
                    result,status=map(json.loads,stopped.stdout.split('\n')[:2])
                    self.assertEqual((result,status['state'],status['code']),
                                     ({'clean':False,'code':None},'failed','shutdown_incomplete'))
                    self.assertEqual(list(Path(root).glob('*unsaved-*')),[])
                    continue
                if outcome=='checkpointed':
                    result,status=map(json.loads,stopped.stdout.split('\n')[:2])
                    self.assertEqual((result,status['state'],status['code']),
                                     ({'clean':True,'code':'stop_checkpointed'},'stopped','stop_checkpointed'))
                    checkpoint,=Path(root).glob('unsaved-stopped-*.json')
                    cutoff=int(checkpoint.stem.rsplit('-',1)[1])
                else:
                    # The partial file never became a checkpoint; recovery skips it and keeps it,
                    # and still saves the other run's checkpoint beside it.
                    partial,=Path(root).glob('.unsaved-stopped-*.partial')
                    self.assertEqual(list(Path(root).glob('unsaved-stopped-*')),[])
                    self.assertEqual([p.name for p in Path(root).glob('unsaved-earlier-*')],['unsaved-earlier-5000.json'])
                store=Store(Path(root)/'readings.sqlite',Budget(root,free_margin_bytes=0))
                try:
                    after=Collector(store,(),run_id='after',boot_id='boot');after.start()
                    self.assertIsNone(after.recovery_error)
                    self.assertTrue(after.close())
                    self.assertEqual(list(Path(root).glob('unsaved-stopped-*')),[])
                    runs=dict(store.db.execute('SELECT run_id,stopped_ns FROM runs'))
                    sequences=[s for s, in store.db.execute("SELECT sequence FROM readings WHERE run_id='stopped' AND source='synthetic.process'")]
                    held={entity:[row['sample_count'] for row in unpack_summaries(payload)] for entity,payload in store.db.execute(
                        "SELECT entity,payload FROM summaries WHERE run_id='stopped' AND resolution='minute' AND bucket_ns=? AND entity LIKE 'process:%'",(60*10**9,))}
                    if outcome=='killed':
                        earlier=store.db.execute("SELECT count(*) FROM readings WHERE run_id='earlier'").fetchone()[0]
                        self.assertEqual((runs['earlier'],earlier),(5000,3),'a valid checkpoint was blocked by a partial one')
                        self.assertEqual(list(Path(root).glob('unsaved-earlier-*')),[])
                        self.assertEqual((len(sequences),runs['stopped'],held),(70,None,{}))  # an explicit, unstopped loss
                        self.assertTrue(partial.exists())
                        continue
                    self.assertEqual(len(sequences),100,'completed readings were lost')
                    self.assertEqual(runs['stopped'],cutoff)
                    self.assertEqual(held,{'process:1':[39],'process:2':[39]},'the held minute was lost')
                    in_flight=store.db.execute("SELECT count(*) FROM readings WHERE run_id='stopped' AND source='synthetic.hung'").fetchone()[0]
                    self.assertEqual(in_flight,1 if case=='hung_read' else 0)
                finally:store.close()

    def test_largest_prune_batches_stay_inside_declared_wal_pages(self):
        class ObservedBudget(Budget):
            @contextmanager
            def sqlite_write(self,db,path,*args,**kwargs):
                with super().sqlite_write(db,path,*args,**kwargs) as result:
                    yield result
                    if kwargs.get('dirty_pages'):
                        page=db.execute('PRAGMA page_size').fetchone()[0]
                        wal=Path(str(path)+'-wal').stat().st_size
                        self.samples.append((max(0,(wal-32)//(page+24)),kwargs['dirty_pages']))
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as root:
            budget=ObservedBudget(root,limit_bytes=1024**3,free_margin_bytes=0);budget.samples=[]
            store=Store(Path(root)/'readings.sqlite',budget)
            try:
                db=store.db
                db.execute('BEGIN')
                for n in range(4096):
                    db.execute('INSERT INTO readings VALUES(?,?,?,?,?,?)',('run',n,1,'source','cpu',bytes(5000)))
                    db.execute('INSERT INTO reading_components VALUES(?,?,?,?)',('run',n,'cpu',1))
                    for resolution in ('minute','hour'):
                        db.execute('INSERT INTO summaries(run_id,sequence_min,sequence_max,resolution,bucket_ns,source,component,entity,payload) VALUES(?,?,?,?,?,?,?,?,?)',
                                   ('run',n,n,resolution,0,'source','cpu',str(n),bytes(5000)))
                    db.execute('INSERT INTO incidents VALUES(?,?,?,?)',('run',n,1,bytes(5000)))
                db.execute("INSERT INTO coverage VALUES('run',4096,'minute','source','cpu',0,2,'{}')")
                db.execute('COMMIT');db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                for _ in range(4):self.assertTrue(store.prune_oldest())
                self.assertEqual([r[0] for r in db.execute('SELECT kind FROM retention ORDER BY rowid')],['raw','minute','hour','incident'])
                self.assertEqual(len(budget.samples),4)
                for frames,bound in budget.samples:self.assertLessEqual(frames,bound)
                for _ in range(16):
                    if not store.prune_oldest():break
                else:self.fail('bounded vacuum did not finish reclaiming deleted history')
                self.assertFalse(store.prune_oldest())
            finally:store.close()

    def test_restart_prunes_raw_then_minutes_and_keeps_collecting(self):
        self.restart_fixture(uncovered=False)

    def test_restart_preserves_uncovered_raw_and_prunes_summaries(self):
        self.restart_fixture(uncovered=True)

    def restart_fixture(self,uncovered):
        # The live store was below its cap but could not reserve a whole-DB WAL.
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as root:
            root=Path(root);path=root/'readings.sqlite'
            budget=Budget(root,limit_bytes=200*1024**2,free_margin_bytes=0)
            store=Store(path,budget)
            clock=Clock(1,1,1)
            record=Record(Reading('synthetic','memory',clock,clock,
                                 (Field('memory.used',1,'bytes'),)),
                          'boot','old',1,1,clock)
            store.append([record])
            store.db.execute('PRAGMA max_page_count=50000')  # Synthetic fixture construction.
            # A crash-recovered window may contain raw evidence without its
            # coverage rows. Keep that evidence while making room elsewhere.
            orphan=Record(Reading('recovered','memory',clock,clock,
                                 (Field('memory.used',3,'bytes'),)),
                          'boot','recovered',1,1,clock)
            if uncovered:
                store.db.execute('INSERT INTO readings VALUES(?,?,?,?,?,?)',
                                 ('recovered',1,0,'recovered','memory',orphan.packed))
            rng=random.Random(7)
            payload=pack_summaries([{'signal':'synthetic','sample':rng.randbytes(4096).hex()}])
            store.db.execute('BEGIN')
            for i in range(5200):
                store.db.execute('INSERT INTO summaries(run_id,sequence_min,sequence_max,resolution,bucket_ns,source,component,entity,payload) VALUES(?,?,?,?,?,?,?,?,?)',
                                 ('old',1,1,'minute',(i//100+1)*60_000_000_000,'historical','memory',str(i),payload))
            store.db.execute('COMMIT');store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            hours=store.db.execute("SELECT count(*) FROM summaries WHERE resolution='hour'").fetchone()[0]
            store.close()
            budget.limit_bytes=40*1024**2
            (root/'scanner.sqlite').write_bytes(bytes(budget.limit_bytes-budget.usage()['bytes']-1024**2))
            self.assertLess(budget.usage()['bytes'],budget.limit_bytes)
            store=Store(path,budget)
            try:
                collector=Collector(store,(),run_id='new',boot_id='boot')
                collector.start()
                kinds=[r[0] for r in store.db.execute('SELECT kind FROM retention ORDER BY rowid')]
                self.assertIn('minute',kinds)
                if uncovered:
                    self.assertNotIn('raw',kinds)
                    self.assertEqual(store.db.execute("SELECT payload FROM readings WHERE run_id='recovered'").fetchone()[0],orphan.packed)
                    self.assertIn('storage_budget_raw_uncovered',[r[0] for r in store.db.execute('SELECT reason FROM retention')])
                else:
                    self.assertIn('raw',kinds)
                    self.assertLess(kinds.index('raw'),kinds.index('minute'))
                self.assertNotIn('hour',kinds)
                self.assertEqual(store.db.execute("SELECT count(*) FROM summaries WHERE resolution='hour'").fetchone()[0],hours)
                collector.ingest(Reading('fresh','memory',clock,clock,(Field('memory.used',2,'bytes'),)))
                self.assertTrue(collector.flush())
                self.assertEqual(store.db.execute("SELECT last_sequence FROM runs WHERE run_id='new'").fetchone()[0],collector.sequence)
                self.assertEqual(store.db.execute("SELECT count(*) FROM readings WHERE run_id='new' AND source='fresh'").fetchone()[0],1)
                self.assertLessEqual(budget.usage()['bytes'],budget.limit_bytes)
                self.assertTrue(collector.close())
            finally:store.close()


if __name__=='__main__':unittest.main()
