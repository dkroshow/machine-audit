"""Live-tail admission regressions; synthetic records, no native source reads."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from machine_audit.budget import Budget
from machine_audit.collector import Collector
from machine_audit.model import Clock, Field, Reading, Record
from machine_audit.query import Query
from machine_audit.store import Store

S = 10**9


def process_pass(t, count=40):
    fields = []
    specs = [('user_time', 'mach_ticks'), ('system_time', 'mach_ticks'),
             ('runnable_time', 'mach_ticks'), ('child_user_time', 'mach_ticks'),
             ('child_system_time', 'mach_ticks'), ('phys_footprint', 'bytes'),
             ('resident_size', 'bytes'), ('lifetime_max_phys_footprint', 'bytes'),
             ('diskio_bytesread', 'bytes'), ('diskio_byteswritten', 'bytes'),
             ('logical_writes', 'bytes'), ('pkg_idle_wkups', 'count'),
             ('interrupt_wkups', 'count'), ('pageins', 'pages'),
             ('compressed', 'bytes'), ('ledger_swapins', 'bytes'),
             ('decompressions', 'count'), ('limit_bytes_remaining', 'bytes'),
             ('ledger_tag_graphics_footprint', 'bytes'),
             ('ledger_tag_graphics_footprint_compressed', 'bytes')]
    for pid in range(count):
        start = Clock(t*S+pid*1000, t*S+pid*1000, t*S+pid*1000)
        end = Clock(start.wall_ns+100, start.awake_ns+100, start.continuous_ns+100)
        kw = dict(entity=f'process:boot:{pid}:1', label=f'Program{pid%4}',
                  scope='process', start=start, end=end)
        fields.append(Field('process.exec_uuid', 'a'*32, 'uuid', 'state', **kw))
        for name, unit in specs:
            gauge = name in {'phys_footprint', 'resident_size', 'lifetime_max_phys_footprint',
                             'compressed', 'limit_bytes_remaining',
                             'ledger_tag_graphics_footprint', 'ledger_tag_graphics_footprint_compressed'}
            unavailable = name == 'ledger_tag_graphics_footprint_compressed'
            fields.append(Field('process.'+name, None if unavailable else 1000+pid+t,
                                unit, 'gauge' if gauge else 'counter',
                                status='unavailable' if unavailable else 'observed',
                                additive=not gauge, scale=1e-9 if unit=='mach_ticks' else 1,
                                rate_unit='CPUs' if unit=='mach_ticks' else None, **kw))
    first, last = fields[0].start, fields[-1].end
    # Failed native reads have no stable process identity; scope carries that fact.
    fields.append(Field('process.coverage', None, 'state', 'state', status='denied',
                        entity='pid:9999', scope='process', start=first, end=last))
    return Reading('synthetic.process', 'cpu', first, last, tuple(fields))


def host(t, value=None):
    c = Clock(t*S, t*S, t*S)
    return Reading('synthetic.cpu', 'cpu', c, c,
                   (Field('cpu.busy', t if value is None else value, 'seconds', 'counter'),))


class QueryTailTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent/'.tmp'
        root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.store = Store(Path(self.tmp.name)/'readings.sqlite',
                           Budget(self.tmp.name, limit_bytes=40_000_000, free_margin_bytes=0))
        self.collector = Collector(self.store, run_id='run', boot_id='boot',
                                   clock=lambda: Clock(20*S, 20*S, 20*S))
        self.store.start_run('run', 'boot', 0)
        self.query = Query(self.store, self.collector)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def populate(self, count=40):
        for t in (0, 10):
            self.collector.ingest(process_pass(t, count))
            self.collector.ingest(host(t))

    def test_snapshot_copies_immutable_references_without_decoding(self):
        self.populate()
        with patch.object(Record, 'unpack', side_effect=AssertionError('decoded under snapshot lock')):
            snapshot = self.collector.snapshot(0, 20*S)
        self.assertEqual([r.sequence for r in snapshot['tail']], [1, 2, 3, 4])

    def test_series_admits_before_decoding_and_counts_unselected_source_status(self):
        self.populate()
        decoded = []
        unpack = Record.unpack
        def observe(payload):
            self.assertFalse(self.collector.lock._is_owned())
            record = unpack(payload)
            decoded.append(record.reading.source)
            return record
        # A process pass exceeds this small budget; the requested host series does not.
        with patch('machine_audit.query.MAX_QUERY_BYTES', 10_000), patch.object(Record, 'unpack', side_effect=observe):
            for end in (20*S, 1200*S):  # Raw and summary-with-tail paths share admission.
                answer = self.query.series(0, end, ['cpu.busy'], 'cpu', limit=75)
                self.assertEqual(sum(r['total'] or 0 for r in answer['rows']), 10)
                self.assertFalse(answer['truncated'])
        self.assertNotIn('synthetic.process', decoded)
        self.assertEqual(answer['coverage']['sources']['synthetic.process'],
                         {'observed': 2, 'unavailable': 80, 'denied': 2})
        self.assertFalse(any(g.get('signal', '').startswith('process.') for g in answer['gaps']))

    def test_series_admission_matches_component_in_mixed_reading_before_and_after_save(self):
        # A CPU host field must not admit process-only memory fields into a memory series.
        for t in (0, 10):
            c = Clock(t*S, t*S, t*S)
            self.collector.ingest(Reading('mixed', 'cpu', c, c,
                (Field('cpu.busy', t, 'seconds', 'counter'),
                 Field('process.phys_footprint', None, 'bytes', status='denied',
                       entity='process:1', label='Editor', scope='process'))))
        for saved in (False, True):
            if saved:
                self.assertTrue(self.collector.flush())
            with patch.object(Record, 'unpack', side_effect=AssertionError('unselected mixed reading')):
                answer = self.query.series(0, 20*S, ['memory.available'], 'memory', limit=75)
            self.assertFalse(answer['truncated'])
            self.assertFalse(any(g.get('signal')=='process.phys_footprint' for g in answer['gaps']))

    def test_bundled_memory_and_storage_who_match_saved_rows(self):
        self.populate(count=8)
        live = {}
        for component, signal in [('memory', 'process.phys_footprint'),
                                  ('storage', 'process.diskio_byteswritten')]:
            live[component] = self.query.who(0, 20*S, signal, component, limit=75)
            self.assertTrue(live[component]['rows'])
            self.assertFalse(live[component]['truncated'])
        self.assertEqual(live['memory']['total_rows'], 8)
        self.assertEqual(sum(r['total'] for r in live['storage']['rows']), 80)
        self.assertTrue(self.collector.flush())
        for component, signal in [('memory', 'process.phys_footprint'),
                                  ('storage', 'process.diskio_byteswritten')]:
            saved = Query(self.store).who(0, 20*S, signal, component, limit=75)
            for key in ('rows', 'covered', 'total_rows', 'truncated', 'accounting'):
                self.assertEqual(live[component][key], saved[key], key)

    def test_denied_pid_scope_is_workload_evidence_in_tail_and_saved_answers(self):
        self.populate(count=2)
        for saved in (False, True):
            if saved:
                self.assertTrue(self.collector.flush())
            for end in (20*S, 1200*S):
                series = self.query.series(0, end, component='cpu', limit=75)
                self.assertFalse(any(r['signal']=='process.coverage' for r in series['rows']))
                self.assertEqual(series['coverage']['sources']['synthetic.process']['denied'], 2)
                who = self.query.who(0, end, signal='process.coverage', component='cpu', limit=75)
                self.assertEqual(len(who['rows']), 1)
                self.assertEqual(who['rows'][0]['entity'], 'pid:9999')
                self.assertEqual(who['rows'][0]['scope'], 'process')
                self.assertEqual(who['rows'][0]['status'], 'denied')
                self.assertIsNone(who['rows'][0]['total'])
            with self.query._snapshot(0, 20*S) as (db, captured):
                raw, overflow = self.query._raw(db, captured, 0, 20*S, 'cpu')
            self.assertFalse(overflow)
            denied = [f for r in raw for f in r.reading.fields if f.name=='process.coverage']
            self.assertEqual(len(denied), 2)
            self.assertTrue(all(f.status=='denied' and f.scope=='process' for f in denied))

    def test_legacy_permissive_index_is_screened_without_rewriting_saved_rows(self):
        self.populate(count=2)
        self.assertTrue(self.collector.flush())
        with self.store.budget.sqlite_write(self.store.db, self.store.path):
            self.store.db.execute("UPDATE reading_components SET is_series=1 WHERE (run_id,sequence) IN (SELECT run_id,sequence FROM readings WHERE source='synthetic.process')")
        unpack = Record.unpack
        def observe(payload):
            record = unpack(payload)
            self.assertNotEqual(record.reading.source, 'synthetic.process')
            return record
        with patch.object(Record, 'unpack', side_effect=observe):
            for end in (20*S, 1200*S):
                answer = self.query.series(0, end, ['cpu.busy'], 'cpu', limit=75)
                self.assertEqual(sum(r['total'] or 0 for r in answer['rows']), 10)
                self.assertFalse(answer['truncated'])
                self.assertEqual(answer['coverage']['sources']['synthetic.process']['denied'], 2)
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT min(is_series) FROM reading_components WHERE (run_id,sequence) IN (SELECT run_id,sequence FROM readings WHERE source='synthetic.process')").fetchone()[0], 1)

    def test_mixed_legacy_candidates_keep_byte_bound_and_report_incomplete(self):
        # A source's old host field prevents a scope-only catalog rejection.
        self.collector.ingest(Reading('synthetic.process', 'cpu', host(0).start, host(0).end,
                                     host(0).fields))
        self.collector.ingest(process_pass(10))
        self.assertTrue(self.collector.flush())
        with self.store.budget.sqlite_write(self.store.db, self.store.path):
            self.store.db.execute("UPDATE reading_components SET is_series=1 WHERE component='cpu'")
        with patch('machine_audit.query.MAX_QUERY_BYTES', 10_000):
            answer = Query(self.store).series(0, 20*S, ['cpu.busy'], 'cpu', limit=75)
        self.assertTrue(answer['truncated'])
        self.assertEqual(answer['coverage']['sources']['synthetic.process']['denied'], 1)
        self.assertFalse(any(r['signal']=='process.coverage' for r in answer['rows']))

    def test_self_process_scope_remains_a_series_before_and_after_save(self):
        for t in (0, 10):
            c = Clock(t*S, t*S, t*S)
            self.collector.ingest(Reading('synthetic.self', 'self', c, c,
                tuple(Field(name, t, 'seconds', 'counter', entity='process:self:1',
                            label='Collector', scope='process')
                      for name in ('self.user_time', 'self.system_time'))))
        for saved in (False, True):
            if saved:
                self.assertTrue(self.collector.flush())
            for end in (20*S, 1200*S):
                answer = self.query.series(0, end, ['self.cpu'], 'self', limit=75)
                self.assertEqual(sum(r['total'] or 0 for r in answer['rows']), 20)
                self.assertFalse(answer['truncated'])

    def test_around_events_keep_byte_bound_after_snapshot_becomes_references(self):
        reading = process_pass(10)
        self.collector.ingest(Reading(reading.source, reading.component, reading.start, reading.end,
            reading.fields+(Field('memory.event', 'changed', 'state', 'event', component='memory'),)))
        with patch('machine_audit.query.MAX_QUERY_BYTES', 10_000), \
             patch.object(Record, 'unpack', side_effect=AssertionError('oversized event decoded')):
            answer = self.query.around(10*S, 10*S, component='cpu', signals=['cpu.busy'], limit=75)
        self.assertEqual(answer['events'], [])
        self.assertTrue(answer['coverage']['truncated'])

    def test_around_uses_one_watermark_when_tail_commits_and_new_events_arrive(self):
        self.populate(count=2)
        event_clock = Clock(5*S, 5*S, 5*S)
        self.collector.ingest(Reading('synthetic.event', 'memory', event_clock, event_clock,
                                     (Field('memory.event', 'before', 'state', 'event'),)))
        original = self.store.read
        @contextmanager
        def race():
            with original() as db:
                self.collector.ingest(host(15, 40))
                c = Clock(15*S, 15*S, 15*S)
                self.collector.ingest(Reading('synthetic.event', 'memory', c, c,
                                             (Field('memory.event', 'after', 'state', 'event'),)))
                self.assertTrue(self.collector.flush())
                yield db
        self.store.read = race
        with patch.object(self.collector, 'snapshot', wraps=self.collector.snapshot) as snapshot:
            answer = self.query.around(10*S, 10*S, component='cpu', signals=['cpu.busy'], limit=75)
        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(sum(r['total'] or 0 for r in answer['series']['rows']), 10)
        self.assertEqual([r['reading']['fields'][0]['value'] for r in answer['events']], ['before'])
        self.assertEqual(answer['coverage']['sources']['synthetic.cpu']['observed'], 2)
        self.assertEqual(answer['coverage']['sources']['synthetic.process']['denied'], 2)
        self.assertFalse(any(r['signal']=='process.coverage' for r in answer['series']['rows']))
        self.assertTrue(any(r['signal']=='process.coverage' for r in answer['who']['rows']))


if __name__ == '__main__':
    unittest.main()
