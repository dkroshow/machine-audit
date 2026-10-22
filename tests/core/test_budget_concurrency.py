"""Cross-process admission must not serialize unrelated SQLite stores."""
from pathlib import Path
import multiprocessing
import os
import sqlite3
import tempfile
import unittest

from machine_audit.budget import Budget
from machine_audit.scanner.store import MapStore


def hold_scanner(root, entered, release, outcome):
    try:
        store=MapStore(Path(root)/'scanner.sqlite',Budget(root,limit_bytes=64*1024**2,free_margin_bytes=0))
        with store.write(growth=64*1024):
            store.db.execute("INSERT INTO map_meta VALUES('synthetic',1)")
            entered.set()
            if not release.wait(10):raise RuntimeError('release_timeout')
        store.close();outcome.put(None)
    except BaseException as exc:
        outcome.put(type(exc).__name__+':'+str(exc));entered.set()


def save_other_store(root, saved, outcome):
    try:
        budget=Budget(root,limit_bytes=64*1024**2,free_margin_bytes=0)
        path=Path(root)/'readings.sqlite'
        db=sqlite3.connect(path,isolation_level=None,timeout=0)
        with budget.sqlite_write(db,path):
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO readings VALUES(1)')
            db.execute('COMMIT')
        db.close();outcome.put(None);saved.set()
    except BaseException as exc:
        outcome.put(type(exc).__name__+':'+str(exc))


def abandon_reservation(root):
    with Budget(root,limit_bytes=64*1024**2,free_margin_bytes=0).reserve(32*1024**2):
        os._exit(0)  # Own disposable process: simulate a crash, bypass finally.


def reserve_peak(root, peak, entered, release, acquired, outcome):
    try:
        entered.set()
        with Budget(root,limit_bytes=64*1024**2,free_margin_bytes=0).reserve(peak):
            acquired.set()
            if not release.wait(10):raise RuntimeError('release_timeout')
        outcome.put(None)
    except BaseException as exc:outcome.put(type(exc).__name__+':'+str(exc))


def wait_for_checkpoint(root, entered, outcome):
    db=sqlite3.connect(Path(root)/'pending.sqlite',isolation_level=None,timeout=3)
    class ObservedConnection:
        @property
        def in_transaction(self):return db.in_transaction
        def execute(self,sql,*args):
            if sql=='PRAGMA wal_checkpoint(TRUNCATE)':entered.set()
            return db.execute(sql,*args)
    try:
        budget=Budget(root,limit_bytes=64*1024**2,free_margin_bytes=0)
        with budget.sqlite_write(ObservedConnection(),Path(root)/'pending.sqlite'):pass
        outcome.put(None)
    except BaseException as exc:outcome.put(type(exc).__name__+':'+str(exc))
    finally:db.close()


class BudgetConcurrencyTests(unittest.TestCase):
    def test_collector_saves_before_a_waiting_scanner_transaction_finishes(self):
        # Reproduces the live failure without a scan: one SQLite writer waits
        # while another process must commit to a different managed database.
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as root:
            path=Path(root)/'readings.sqlite'
            db=sqlite3.connect(path,isolation_level=None)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE readings(value INTEGER)');db.close()
            context=multiprocessing.get_context('spawn')
            entered,release,saved=(context.Event() for _ in range(3))
            scanner_result,writer_result=context.Queue(),context.Queue()
            scanner=context.Process(target=hold_scanner,args=(root,entered,release,scanner_result))
            writer=context.Process(target=save_other_store,args=(root,saved,writer_result))
            scanner.start()
            try:
                self.assertTrue(entered.wait(5),'scanner did not reach its transaction')
                writer.start()
                committed_while_held=saved.wait(2)
            finally:
                release.set();scanner.join(5)
                if writer.pid is not None:writer.join(5)
            self.assertFalse(scanner.is_alive());self.assertFalse(writer.is_alive())
            self.assertIsNone(scanner_result.get(timeout=1))
            self.assertIsNone(writer_result.get(timeout=1))
            self.assertTrue(committed_while_held,'collector waited for unrelated scanner transaction')
            db=sqlite3.connect(path)
            self.assertEqual(db.execute('SELECT value FROM readings').fetchall(),[(1,)])
            db.close()

    def test_checkpoint_wait_does_not_block_another_store(self):
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as root:
            for name,table in (('pending.sqlite','t'),('readings.sqlite','readings')):
                db=sqlite3.connect(Path(root)/name,isolation_level=None)
                db.execute('PRAGMA journal_mode=WAL');db.execute(f'CREATE TABLE {table}(value)');db.close()
            writer=sqlite3.connect(Path(root)/'pending.sqlite',isolation_level=None)
            reader=sqlite3.connect(Path(root)/'pending.sqlite',isolation_level=None)
            reader.execute('BEGIN');reader.execute('SELECT count(*) FROM t').fetchone()
            writer.execute('INSERT INTO t VALUES(zeroblob(10000))')
            context=multiprocessing.get_context('spawn')
            entered,saved=context.Event(),context.Event()
            checkpoint_result,other_result=context.Queue(),context.Queue()
            checkpoint=context.Process(target=wait_for_checkpoint,args=(root,entered,checkpoint_result))
            other=context.Process(target=save_other_store,args=(root,saved,other_result))
            checkpoint.start()
            try:
                self.assertTrue(entered.wait(5))
                other.start();committed_while_pinned=saved.wait(2)
            finally:
                reader.close();writer.close();checkpoint.join(5)
                if other.pid is not None:other.join(5)
            self.assertFalse(checkpoint.is_alive());self.assertFalse(other.is_alive())
            self.assertIsNone(checkpoint_result.get(timeout=1));self.assertIsNone(other_result.get(timeout=1))
            self.assertTrue(committed_while_pinned,'collector waited for another database checkpoint')

    def test_crashed_writer_releases_reservation_without_pid_checks(self):
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as root:
            context=multiprocessing.get_context('spawn')
            child=context.Process(target=abandon_reservation,args=(root,))
            child.start();child.join(5)
            self.assertEqual(child.exitcode,0)
            self.assertEqual(len(list(Path(root).glob('.budget-*.claim'))),1)
            budget=Budget(root,limit_bytes=64*1024**2,free_margin_bytes=0)
            with budget.reserve(budget.limit_bytes-512):pass
            self.assertEqual(list(Path(root).glob('.budget-*.claim')),[])

    def test_concurrent_reservations_cannot_spend_the_same_headroom(self):
        scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as root:
            context=multiprocessing.get_context('spawn')
            entered,release,acquired=(context.Event() for _ in range(3))
            outcome=context.Queue()
            other=context.Process(target=reserve_peak,args=(root,40*1024**2,entered,release,acquired,outcome))
            budget=Budget(root,limit_bytes=64*1024**2,free_margin_bytes=0)
            with budget.reserve(32*1024**2):
                other.start()
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertFalse(acquired.wait(0.2),'overlapping peaks were both admitted')
                finally:release.set()
            other.join(5)
            self.assertFalse(other.is_alive());self.assertIsNone(outcome.get(timeout=1))
            self.assertTrue(acquired.is_set())
            self.assertLessEqual(budget.usage()['bytes'],budget.limit_bytes)


if __name__=='__main__':unittest.main()
