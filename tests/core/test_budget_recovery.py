"""Restart recovery under a shared cap; only disposable synthetic stores."""
from pathlib import Path
from contextlib import contextmanager
import random
import tempfile
import unittest

from machine_audit.budget import Budget
from machine_audit.collector import Collector
from machine_audit.model import Clock, Field, Reading, Record
from machine_audit.store import Store, pack_summaries


class BudgetRecoveryTests(unittest.TestCase):
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
                self.assertIn('raw',kinds)
                self.assertIn('minute',kinds)
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
