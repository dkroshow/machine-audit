"""Measure WAL frames for the product's counted transaction contracts."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest

from machine_audit.budget import Budget
from machine_audit.scanner.accounting import Blocks
from machine_audit.scanner.native import Volume
from machine_audit.scanner.store import MapStore
from machine_audit.scanner.walker import Node
from machine_audit.store import Store
from test_scale import process_records


class MeasuredBudget(Budget):
    @contextmanager
    def sqlite_write(self,db,path,*args,**kwargs):
        with super().sqlite_write(db,path,*args,**kwargs) as receipt:
            yield receipt
            if kwargs.get('rows') is not None:
                page=receipt['page_size'];wal=Path(str(path)+'-wal').stat().st_size
                self.samples.append((kwargs['rows'],max(0,(wal-32)//(page+24)),receipt['dirty_pages'],
                                     db.execute('PRAGMA page_count').fetchone()[0]-receipt['starting_pages']))


class WriteContractTests(unittest.TestCase):
    def setUp(self):
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=scratch);self.root=Path(self.temp.name)
        self.budget=MeasuredBudget(self.root,limit_bytes=1024**3,free_margin_bytes=0);self.budget.samples=[]

    def tearDown(self):self.temp.cleanup()

    def assert_bounds(self):
        self.assertTrue(self.budget.samples)
        for rows,frames,bound,growth in self.budget.samples:
            self.assertLessEqual(frames,bound,(rows,frames,bound))
            self.assertLessEqual(growth,bound)

    def test_small_update_admits_above_whole_database_headroom(self):
        path=self.root/'readings.sqlite';db=sqlite3.connect(path,isolation_level=None)
        try:
            db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE t(value BLOB)')
            db.execute('BEGIN');db.executemany('INSERT INTO t VALUES(?)',[(bytes(4096),)]*5000);db.execute('COMMIT')
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self.budget.limit_bytes=40*1024**2
            (self.root/'scanner.sqlite').write_bytes(bytes(10*1024**2))
            with self.budget.sqlite_write(db,path,rows=1,payload_bytes=4096) as receipt:
                self.assertLess(receipt['peak_bytes']-self.budget.usage()['bytes'],1024**2)
                db.execute('BEGIN');db.execute('UPDATE t SET value=? WHERE rowid=1',(bytes(4096),));db.execute('COMMIT')
            self.assert_bounds()
        finally:db.close()

    def test_full_process_passes_bound_insert_and_summary_update_frames(self):
        store=Store(self.root/'readings.sqlite',self.budget)
        try:
            records=process_records(2000,2)
            store.append(records[:1]);store.append(records[1:])
            self.assert_bounds()
        finally:store.close()

    def test_largest_scanner_insert_charge_and_aggregate_batches(self):
        store=MapStore(self.root/'scanner.sqlite',self.budget)
        try:
            volume=Volume('v','Synthetic','/synthetic','apfs',1,10**12,1,1,10**9)
            store.prepare(volume)
            for n in range(8192):
                store.directory(Node(str(n+1),None if n==0 else '1',str(n),'directory',1,1,'observed',None,0))
            for n in range(4096):store.holder(n+10000,'1',n+10000,1,None,False)
            store.finish_volume(volume,Blocks(),[],full=True,lean=True)
            self.assertIn(8192,[r[0] for r in self.budget.samples])
            self.assertIn(4096,[r[0] for r in self.budget.samples])
            self.assert_bounds()
        finally:store.close()


if __name__=='__main__':unittest.main()
