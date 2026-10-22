"""Restart recovery under a shared cap; only disposable synthetic stores."""
from pathlib import Path
from contextlib import contextmanager
import random
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from machine_audit.budget import Budget, BudgetExceeded
from machine_audit.collector import Collector
from machine_audit.model import Clock, Field, Reading, Record
from machine_audit.query import Query
from machine_audit.store import Store, MINUTE, pack_summaries


class BudgetRecoveryTests(unittest.TestCase):
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
