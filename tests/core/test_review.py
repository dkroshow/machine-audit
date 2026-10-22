"""Regressions from independent review, using only disposable recording stores."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zlib

from machine_audit.budget import Budget
from machine_audit.collector import Collector
from machine_audit.model import Clock, Field, Reading, Record
from machine_audit.query import Query
from machine_audit.store import Store

S = 10**9


class ReviewTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).parent/'.tmp';root.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=root)
        self.store=Store(Path(self.tmp.name)/'readings.sqlite',Budget(self.tmp.name,limit_bytes=200_000_000,free_margin_bytes=0))

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def _collector_above_one_batch(self):
        collector=Collector(self.store,run_id='run',clock=lambda:Clock(10*S,10*S,10*S))
        compress=zlib.compress
        def padded(data,level):
            stream=compress(data,level)
            # Valid empty DEFLATE blocks make actual encoded bytes cross the
            # batch bound without a large process fixture or costly sampling.
            return stream[:2]+b'\x00\x00\x00\xff\xff'*450000+stream[2:]
        with patch('machine_audit.model.zlib.compress',side_effect=padded):
            for n in (1,2):
                c=Clock(n*S,n*S,n*S)
                collector.ingest(Reading('fixture','memory',c,c,(Field('memory.used',n,'bytes'),)))
        self.assertGreater(collector.tail_bytes,4*1024**2)
        return collector

    def test_flush_drains_entry_tail_in_bounded_batches_without_chasing_arrivals(self):
        collector=self._collector_above_one_batch()
        append=self.store.append;batch_sizes=[];late=[]
        def saving(records,**kwargs):
            batch_sizes.append(sum(len(r.packed) for r in records))
            if len(batch_sizes)==1:
                c=Clock(3*S,3*S,3*S)
                late.append(collector.ingest(Reading('fixture','memory',c,c,(Field('memory.used',3,'bytes'),))))
            return append(records,**kwargs)
        self.store.append=saving
        self.assertTrue(collector.flush())
        self.assertGreaterEqual(len(batch_sizes),2)
        self.assertTrue(all(size<=4*1024**2 for size in batch_sizes))
        self.assertEqual(collector.committed,late[0].sequence-1)
        self.assertEqual([r.sequence for r in collector.snapshot()['tail']],[late[0].sequence])
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM readings WHERE source='fixture'").fetchone()[0],2)

    def test_later_flush_batch_failure_retains_the_unsaved_prefix_for_retry(self):
        collector=self._collector_above_one_batch()
        append=self.store.append;calls=[]
        def saving(records,**kwargs):
            calls.append([r.sequence for r in records])
            if len(calls)==2:raise OSError('synthetic write failure')
            return append(records,**kwargs)
        self.store.append=saving
        self.assertFalse(collector.flush())
        self.assertEqual(collector.committed,1)
        self.assertEqual(collector.snapshot()['tail'][0].sequence,2)
        self.assertEqual(collector.status()['save_error'],'OSError')
        self.store.append=append
        self.assertTrue(collector.flush())
        self.assertEqual(collector.tail,[])
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM readings WHERE source='fixture'").fetchone()[0],2)

    def test_private_record_encoding_preserves_types_clocks_metadata_and_gap(self):
        a,b,c=Clock(11,12,13),Clock(21,22,23),Clock(31,32,33)
        fields=(Field('sample.integer',2**63+9,'ticks','counter',entity='process:1',label='Editor',scope='own',additive=True,scale=1,start=a,end=b,rate_unit='CPUs',component='cpu'),
                Field('sample.float',1.0,'percent',scale=1.0,start=b,end=c),
                Field('sample.flag',True,'boolean','state'),Field('sample.state','idle','state','event'),
                Field('sample.denied',None,'bytes',status='denied',start=a,end=c))
        r=Record(Reading('fixture','cpu',a,c,fields,reason='partial',version='2'),'boot','run',17,41,b)
        restored=Record.unpack(r.packed)
        self.assertEqual(restored.to_dict(),r.to_dict())
        for left,right in zip(r.reading.fields,restored.reading.fields):
            self.assertIs(type(left.value),type(right.value));self.assertIs(type(left.scale),type(right.scale))
        self.assertIs(r.packed,r.packed)
        from machine_audit.store import pack_summaries,unpack_summaries
        rows=[{"signal":"cpu.busy","total":2**63+9,"mean":1.0,"covered_windows":{"run":[[1,9]]}},
              {"signal":"memory.used","total":None,"mean":0,"status":"denied"}]
        self.assertEqual(unpack_summaries(pack_summaries(rows)),rows)

    def test_aligned_summary_keeps_crossing_window_and_marks_partial_boundary(self):
        records=[]
        for seq,t in enumerate(range(-5,3606,10),1):
            c=Clock(t*S,(t+10)*S,(t+10)*S)
            fields=(Field('cpu.busy',t+10,'seconds','counter'),Field('cpu.unrequested',t+20,'seconds','counter'))
            records.append(Record(Reading('host','cpu',c,c,fields),'boot','run',seq,c.wall_ns,c))
        self.store.append(records)
        answer=Query(self.store).series(0,3600*S,signals=['cpu.busy'],limit=120)
        observed=[r for r in answer['rows'] if r['count']]
        self.assertEqual(sum(r['total'] or 0 for r in observed),3600)
        self.assertEqual(min(r['start_ns'] for r in observed),-5*S)
        self.assertTrue(any(r.get('partial_boundary') for r in observed))
        self.assertTrue(all(r['signal']=='cpu.busy' for r in answer['rows']))

    def test_close_has_one_deadline_and_holds_lock_until_native_read_finishes(self):
        entered=threading.Event();release=threading.Event()
        class Source:
            name='slow';component='cpu';cadence_s=1
            def read(self):
                entered.set();release.wait(2)
                c=Clock(1,1,1)
                return Reading(self.name,self.component,c,c)
        collector=Collector(self.store,[Source()]);collector.start();self.assertTrue(entered.wait(1))
        began=time.monotonic()
        self.assertFalse(collector.close(timeout_s=.03))
        self.assertLess(time.monotonic()-began,.2)
        replacement=Collector(self.store)
        with self.assertRaisesRegex(RuntimeError,'collector_already_running'):replacement.acquire()
        release.set();self.assertTrue(collector.close(timeout_s=2))
        replacement.acquire();self.assertTrue(replacement.close())

    def test_field_contract_rejects_host_names_and_addresses_in_units_and_reasons(self):
        # Security review P2: syntax-only checks let a dotted host name or ip:port ride out in
        # unit, rate_unit or reason. The slots are closed sets now; labels stay the only free text.
        a = Clock(S, S, S)
        Field('sample', 1, 'bytes', 'counter', rate_unit='CPUs', start=a, end=a)
        for bad in ({'unit': 'host.example.com'}, {'unit': 'bytes', 'rate_unit': 'eth0'}, {'unit': 'x'}):
            with self.assertRaises(ValueError):
                Field('sample', 1, bad['unit'], 'counter', rate_unit=bad.get('rate_unit'), start=a, end=a)
        Reading('src', 'cpu', a, a, status='failed', reason='PermissionError')
        for reason in ('host.example.com', '10.0.0.1:8080', 'unit.service', '1failed'):
            with self.assertRaises(ValueError):
                Reading('src', 'cpu', a, a, status='failed', reason=reason)

    def test_cpu_histograms_do_not_apply_cpu_thresholds_to_byte_gauges(self):
        from machine_audit.derive import points,summarize
        c=Clock(1,1,1)
        r=Record(Reading('memory','memory',c,c,(Field('memory.used',1000,'bytes'),)),'boot','run',1,1,c)
        result=summarize(points([r]))
        self.assertEqual(result['above_s'],{});self.assertEqual(result['histogram_s'],[])

    def test_pruning_makes_progress_under_cap_without_losing_new_batch(self):
        self.store.budget.limit_bytes=2_000_000
        for sequence in range(1,17):
            c=Clock(sequence*60*S,sequence*60*S,sequence*60*S)
            fields=tuple(Field('memory.used',i,'bytes',entity=f'process:{i}',component='memory') for i in range(200))
            self.store.append([Record(Reading('memory','memory',c,c,fields),'boot','run',sequence,c.wall_ns,c)])
            self.assertLessEqual(self.store.budget.usage()['bytes'],2_000_000)
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT last_sequence FROM runs').fetchone()[0],16)
            self.assertGreater(db.execute('SELECT count(*) FROM retention').fetchone()[0],0)


if __name__=='__main__':unittest.main()
