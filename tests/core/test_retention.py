"""Retention by question, and the cap as an emergency stop. Fixtures are synthetic."""
from itertools import groupby
from pathlib import Path
import tempfile
import unittest

from machine_audit.budget import Budget
from machine_audit.collector import Collector
from machine_audit.model import Clock,Field,Reading,Record
from machine_audit.store import Store,unpack_summaries

S=10**9
HOUR=3600*S
STEP=600  # one pass of everything every 10 minutes


def step(t,run='run',first=1):
    """One 10-minute step: a process pass (process 0 the main CPU user), host CPU, the
    volume's used and free space, and on the first step a pressure event."""
    c=Clock(t*S,t*S,t*S);n=t//STEP
    processes=tuple(f for pid in range(12) for f in (
        Field('process.cpu',(5.0 if pid==0 else 0.01)*n,'seconds','counter',additive=True,rate_unit='CPUs',entity=f'process:{pid}',label=f'p{pid}',scope='process'),
        Field('process.phys_footprint',10**6,'bytes',component='memory',entity=f'process:{pid}',label=f'p{pid}',scope='process')))
    readings=[('synthetic.process','cpu',processes),
              ('synthetic.cpu','cpu',(Field('cpu.user',6.0*n,'seconds','counter'),Field('cpu.system',0.0,'seconds','counter'))),
              ('synthetic.capacity','storage',(Field('storage.used',10**9+n,'bytes',entity='volume:1'),Field('storage.free',10**10-n,'bytes',entity='volume:1')))]
    if n==0:readings.append(('synthetic.pressure','memory',(Field('memory.pressure_level',1,'level','event'),)))
    return [Record(Reading(source,component,c,c,fields),'boot',run,first+i,c.wall_ns,c) for i,(source,component,fields) in enumerate(readings)]


class RetentionTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).parent/'.tmp';root.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=root)
        self.store=Store(Path(self.tmp.name)/'readings.sqlite',Budget(self.tmp.name,limit_bytes=10**9,free_margin_bytes=0))

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def record(self,hours):
        sequence=1
        for t in range(0,hours*3600,STEP):
            batch=step(t,first=sequence);sequence+=len(batch);self.store.append(batch)
        return (hours*3600-STEP)*S  # the newest save

    def oldest(self,sql,*args):
        return self.store.db.execute(sql,args).fetchone()[0]

    def retention(self):
        return [(kind,reason) for kind,reason in self.store.db.execute('SELECT kind,reason FROM retention ORDER BY rowid')]

    def test_each_tier_keeps_what_its_question_needs_and_no_longer(self):
        # Hour rows filled Big Mac's budget until the cap kept 13 minutes of raw readings.
        # Now each tier keeps what its question needs: process passes a day, host readings
        # and minute and 15-minute rows three days, hour rows and incidents the whole span,
        # including the volume's used and free space. Nothing reaches the cap.
        newest=self.record(96)
        self.store.maintain()
        day,recent=newest-24*HOUR,newest-72*HOUR
        cut=lambda before:before//(60*S)*60*S
        self.assertGreaterEqual(self.oldest("SELECT MIN(wall_ns) FROM readings WHERE source='synthetic.process'"),cut(day))
        self.assertLess(self.oldest("SELECT MIN(wall_ns) FROM readings WHERE source='synthetic.process'"),day+STEP*S)
        self.assertGreaterEqual(self.oldest("SELECT MIN(wall_ns) FROM readings WHERE source='synthetic.cpu'"),cut(recent))
        self.assertLess(self.oldest("SELECT MIN(wall_ns) FROM readings WHERE source='synthetic.cpu'"),recent+STEP*S)
        for resolution,width in (('minute',60*S),('15-minute',900*S)):
            first=self.oldest('SELECT MIN(bucket_ns) FROM summaries WHERE resolution=?',resolution)
            self.assertTrue(cut(recent)-width<=first<recent+STEP*S,resolution)
        hours=lambda entity:{b for b, in self.store.db.execute("SELECT bucket_ns FROM summaries WHERE resolution='hour' AND entity=?",(entity,))}
        every=set(range(0,95*HOUR+1,HOUR))
        self.assertEqual(hours('volume:1')|{95*HOUR},every)  # the newest hour is still open
        self.assertTrue({'process:0','rest:process'}<={e for e, in self.store.db.execute("SELECT entity FROM summaries WHERE resolution='hour' AND bucket_ns=0")})
        free={row['signal'] for payload, in self.store.db.execute("SELECT payload FROM summaries WHERE resolution='hour' AND bucket_ns=0 AND entity='volume:1'") for row in unpack_summaries(payload)}
        self.assertEqual(free,{'storage.used','storage.free'})
        self.assertEqual(self.oldest('SELECT count(*) FROM incidents'),1)
        self.assertEqual({reason for _,reason in self.retention()}-{'missing_summary_coverage'},{'retention'})
        self.assertEqual({kind for kind,_ in self.retention()},{'process_raw','raw','minute','15-minute'})
        self.assertFalse(self.store.cap_pruning)

    def test_above_the_emergency_level_history_goes_in_the_stated_order_and_says_so(self):
        # The cap is a stop, not a policy: when it binds, the finest and cheapest history
        # goes first and hour rows, the long record, last. Each deletion says storage_cap.
        self.record(30)
        while self.store.prune_oldest():pass
        self.assertEqual([kind for kind,_ in groupby((k,r) for k,r in self.retention() if r=='storage_cap')],
                         [('process_raw','storage_cap'),('raw','storage_cap'),('minute','storage_cap'),('15-minute','storage_cap'),('hour','storage_cap'),('incident','storage_cap')])
        self.assertTrue(self.store.cap_pruning)

    def at_cap(self,over):
        """A 20 MB cap whose rest the scanner's map fills, `over` bytes past the emergency
        level (under it when negative)."""
        limit=20*1024**2;self.store.budget.limit_bytes=limit
        scanner=Path(self.tmp.name)/'scanner.sqlite'
        mine=self.store.budget.usage()['bytes']-(scanner.stat().st_size if scanner.exists() else 0)
        scanner.write_bytes(bytes(int(limit*.9)-mine+over))

    def test_a_save_at_the_cap_is_admitted_and_hour_rows_go_last(self):
        # A save that needs room makes it from the oldest fine history and still fits; the
        # cap never stops recording, the long record outlasts the detail, and the collector
        # says when the cap starts deciding.
        self.record(30)
        self.at_cap(-64*1024)
        collector=Collector(self.store,(),run_id='live',boot_id='boot',clock=lambda:Clock(31*HOUR,31*HOUR,31*HOUR))
        collector.start()
        self.at_cap(256*1024)
        for t in (31*3600,31*3600+STEP):
            for record in step(t,run='live'):collector.ingest(record.reading)
            self.assertTrue(collector.flush())
        self.assertEqual(self.oldest("SELECT last_sequence FROM runs WHERE run_id='live'"),collector.sequence)
        self.assertLessEqual(self.store.budget.usage()['bytes'],self.store.budget.limit_bytes)
        self.assertEqual(self.oldest("SELECT MIN(bucket_ns) FROM summaries WHERE resolution='hour'"),0)
        self.assertIn(('process_raw','storage_cap'),self.retention())
        self.assertNotIn(('hour','storage_cap'),self.retention())
        events=[f.value for payload, in self.store.db.execute('SELECT payload FROM incidents') for f in Record.unpack(payload).reading.fields if f.name=='self.storage_cap_pruning']
        self.assertEqual(events,[False,True])
        self.assertTrue(collector.close())
