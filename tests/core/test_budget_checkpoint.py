"""Real SQLite checkpoint/admission regressions; every file is disposable."""
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from machine_audit.budget import Budget, BudgetExceeded


class CheckpointBudgetTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).parent/'.tmp';root.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=root)
        self.root=Path(self.tmp.name)
        self.budget=Budget(self.root,limit_bytes=4_000_000,free_margin_bytes=0)
        self.connections=[]
        self.path,self.writer=self.database('a.sqlite')

    def tearDown(self):
        for db in reversed(self.connections):db.close()
        self.tmp.cleanup()

    def database(self,name):
        path=self.root/name
        with self.budget.reserve(256*1024):
            db=sqlite3.connect(path,isolation_level=None,timeout=0)
            self.connections.append(db)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE t(value BLOB)')
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return path,db

    def reader(self,path):
        db=sqlite3.connect(f'file:{path}?mode=ro',uri=True,isolation_level=None,timeout=0)
        self.connections.append(db)
        db.execute('BEGIN');db.execute('SELECT count(*) FROM t').fetchone()
        return db

    def pinned_commit(self):
        with self.budget.sqlite_write(self.writer,self.path):
            self.writer.execute('BEGIN IMMEDIATE')
            self.writer.execute('INSERT INTO t VALUES(zeroblob(1000000))')
            old=self.reader(self.path)
            self.writer.execute('COMMIT')
        self.assertEqual(self.budget.last_checkpoint_error,'reader_pinned')
        return old

    def export(self,size):
        with self.budget.reserve(size):(self.root/'outbox.bin').write_bytes(bytes(size))

    def test_two_readers_and_generic_export_never_overrun_and_retry_clears_wal(self):
        old=self.pinned_commit()
        before=self.budget.usage()
        wal=Path(str(self.path)+'-wal').stat().st_size
        self.assertEqual(before['bytes'],sum(before['parts'].values()))
        rejected=False
        try:
            self.export(self.budget.limit_bytes-before['bytes'])
        except BudgetExceeded:
            rejected=True
            # Leave ordinary write headroom too, so the final retry can write.
            self.export(self.budget.limit_bytes-before['bytes']-wal-128*1024)
        new=self.reader(self.path);old.close()
        with self.assertRaisesRegex(BudgetExceeded,'checkpoint_pinned_by_reader'):
            with self.budget.sqlite_write(self.writer,self.path):pass
        self.assertLessEqual(self.budget.usage()['bytes'],self.budget.limit_bytes)
        self.assertTrue(rejected,'generic admission consumed pending checkpoint space')
        self.assertEqual(self.budget.usage()['checkpoint_reservation_bytes'],wal)
        self.assertGreater(self.budget.usage()['bytes']+wal,self.budget.limit_bytes,
                           'retry must clear an already-backfilled conservative reservation')
        new.close()
        with self.budget.sqlite_write(self.writer,self.path):
            self.writer.execute('INSERT INTO t VALUES(zeroblob(10))')
        self.assertEqual(self.budget.usage()['checkpoint_reservation_bytes'],0)
        with self.budget.reserve(0):pass
        self.assertEqual(self.writer.execute('SELECT count(*) FROM t').fetchone()[0],2)

    def test_pre_admission_checkpoint_refuses_legacy_full_root_before_copy(self):
        old=self.pinned_commit();new=self.reader(self.path);old.close()
        # Reproduce a root filled by the old admission rule, below the cap.
        (self.root/'old-export.bin').write_bytes(bytes(self.budget.limit_bytes-self.budget.usage()['bytes']-50_000))
        size=self.path.stat().st_size
        with self.assertRaisesRegex(BudgetExceeded,'managed_storage_limit'):
            with self.budget.sqlite_write(self.writer,self.path):pass
        self.assertEqual(self.path.stat().st_size,size)
        self.assertLessEqual(self.budget.usage()['bytes'],self.budget.limit_bytes)
        new.close()

    def test_other_sqlite_store_reserves_the_pinned_wal_copy(self):
        old=self.pinned_commit();path,db=self.database('b.sqlite')
        with self.assertRaises(sqlite3.DatabaseError):
            with self.budget.sqlite_write(db,path):
                try:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute('INSERT INTO t VALUES(zeroblob(1200000))')
                    db.execute('COMMIT')
                except sqlite3.DatabaseError:
                    if db.in_transaction:db.execute('ROLLBACK')
                    raise
        self.assertLessEqual(self.budget.usage()['bytes'],self.budget.limit_bytes)
        old.close()
        with self.budget.sqlite_write(self.writer,self.path):pass
        with self.budget.sqlite_write(db,path):db.execute('INSERT INTO t VALUES(zeroblob(1200000))')
        self.assertEqual(db.execute('SELECT count(*) FROM t').fetchone()[0],1)

    def test_generic_free_space_includes_checkpoint_reservation(self):
        old=self.pinned_commit();self.budget.free_margin_bytes=4096
        with patch('machine_audit.budget.shutil.disk_usage',return_value=SimpleNamespace(free=8192)):
            with self.assertRaisesRegex(BudgetExceeded,'free_space_margin'):self.export(4096)
        self.assertFalse((self.root/'outbox.bin').exists())
        old.close()

    def test_checkpoint_free_space_refusal_happens_before_db_growth(self):
        old=self.pinned_commit();new=self.reader(self.path);old.close()
        logical=self.writer.execute('PRAGMA page_count').fetchone()[0]*self.writer.execute('PRAGMA page_size').fetchone()[0]
        physical=self.path.stat().st_size;self.budget.free_margin_bytes=4096
        with patch('machine_audit.budget.shutil.disk_usage',return_value=SimpleNamespace(free=logical-physical+4095)):
            with self.assertRaisesRegex(BudgetExceeded,'free_space_margin'):
                with self.budget.sqlite_write(self.writer,self.path):pass
        self.assertEqual(self.path.stat().st_size,physical)
        new.close()

    def test_no_growth_checkpoint_can_clear_reservation_before_space_refusal(self):
        old=self.pinned_commit();new=self.reader(self.path);old.close()
        with self.assertRaisesRegex(BudgetExceeded,'checkpoint_pinned_by_reader'):
            with self.budget.sqlite_write(self.writer,self.path):pass
        new.close()
        with patch('machine_audit.budget.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(BudgetExceeded,'free_space_margin'):
                with self.budget.sqlite_write(self.writer,self.path):pass
        self.assertEqual(self.budget.usage()['checkpoint_reservation_bytes'],0)


if __name__=='__main__':unittest.main()
