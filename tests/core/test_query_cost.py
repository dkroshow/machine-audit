"""Repeated query-work regressions with small, source-shaped synthetic records."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from machine_audit.budget import Budget
from machine_audit.collector import Collector
from machine_audit.derive import points
from machine_audit.model import Clock, Field, Reading
from machine_audit.query import Query
from machine_audit.store import Store
from tests.core.test_query_tail import process_pass, host, S


class QueryCostTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).parent/'.tmp';root.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=root)
        self.store=Store(Path(self.tmp.name)/'readings.sqlite',
                         Budget(self.tmp.name,limit_bytes=20_000_000,free_margin_bytes=0))
        self.store.start_run('run','boot',0)
        self.collector=Collector(self.store,run_id='run',boot_id='boot',
                                 clock=lambda:Clock(30*S,25*S,30*S))
        for t in (0,10,20):
            reading=process_pass(t,8)
            fields=[]
            for field in reading.fields:
                def sleeping(clock):
                    return replace(clock,awake_ns=clock.awake_ns-(5*S if t else 0))
                field=replace(field,start=sleeping(field.start),end=sleeping(field.end))
                if t==20 and field.entity=='process:boot:0:1' and field.name in {'process.diskio_byteswritten','process.user_time'}:
                    field=replace(field,value=0)
                fields.append(field)
            self.collector.ingest(replace(reading,fields=tuple(fields)))
            self.collector.ingest(host(t))
            c=Clock(t*S,(t-5 if t else 0)*S,t*S)
            self.collector.ingest(Reading('synthetic.self','self',c,c,
                tuple(Field(name,t,'seconds','counter',entity='process:self:1',scope='process')
                      for name in ('self.user_time','self.system_time'))))
            if t==10:
                self.collector.ingest(Reading('synthetic.failed','cpu',c,c,
                    (Field('cpu.sensor',1,'count'),),status='failed',reason='source_failure'))
        self.query=Query(self.store,self.collector)

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def test_short_who_reuses_derivation_and_keeps_reset_sleep_and_status_gaps(self):
        with patch('machine_audit.query.points',wraps=points) as derive:
            answer=self.query.who(0,30*S,'process.diskio_byteswritten','storage',limit=75)
        self.assertEqual(derive.call_count,1)
        reasons={gap['reason'] for gap in answer['gaps']}
        self.assertTrue({'baseline','clock_or_counter_reset','window_contains_sleep'}<=reasons)
        self.assertEqual(answer['coverage']['sources']['synthetic.process']['denied'],3)
        self.assertFalse(answer['truncated'])

    def test_streamed_coverage_preserves_whole_pass_gaps_order_counts_and_pruning(self):
        def compare():
            with self.query._snapshot(0,30*S) as (db,captured):
                records,overflow=self.query._raw(db,captured,0,30*S)
                self.assertFalse(overflow)
                expected=self.query._coverage(db,captured,0,30*S,records,derived=points(records))
                with patch('machine_audit.query.points',wraps=points) as derive:
                    actual=self.query._coverage(db,captured,0,30*S,records)
                self.assertEqual(actual,expected)
                self.assertTrue(all(len(call.args[0])==1 for call in derive.call_args_list))
                with patch('machine_audit.query.MAX_ROWS',3):
                    bounded=self.query._coverage(db,captured,0,30*S,records)
                self.assertEqual(bounded['gaps'],expected['gaps'][:3])
                self.assertEqual(bounded['gap_count'],expected['gap_count'])
                self.assertEqual(bounded['sources'],expected['sources'])
            return actual
        coverage=compare()
        self.assertEqual(coverage['sources']['synthetic.failed']['failed'],1)
        self.assertEqual(coverage['sources']['synthetic.process']['denied'],3)
        self.assertTrue(self.collector.flush())
        self.assertTrue(self.store.prune_oldest())
        pruned=compare()
        self.assertTrue(pruned['retention'])

    def test_long_who_prefilter_matches_full_tail_including_cpu_constituent_reset(self):
        for saved in (False,True):
            if saved:self.assertTrue(self.collector.flush())
            for signal,component in [('process.cpu','cpu'),('self.cpu','self'),
                                     ('process.phys_footprint','memory'),
                                     ('process.diskio_byteswritten','storage')]:
                with patch.object(Query,'_who_records',side_effect=lambda records,signal:records):
                    expected=self.query.who(0,3600*S,signal,component,limit=75)
                with patch('machine_audit.query.points',wraps=points) as derive:
                    actual=self.query.who(0,3600*S,signal,component,limit=75)
                self.assertEqual(actual,expected,signal)
                if signal in {'process.cpu','self.cpu'}:
                    if not saved and signal=='process.cpu':
                        self.assertTrue(any(f.name=='process.coverage' for call in derive.call_args_list
                                            for r in call.args[0] for f in r.reading.fields))
                    continue  # Full CPU pass preserves entity-order ties and guards.
                allowed={signal}
                self.assertTrue(all(f.name in allowed for call in derive.call_args_list
                                    for r in call.args[0] for f in r.reading.fields))


if __name__=='__main__':unittest.main()
