"""Review-scale regressions. Fixtures contain no machine observations or private labels."""
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
import ctypes
import json
import resource
import struct
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from machine_audit.budget import Budget
from machine_audit.collector import Collector
from machine_audit.model import Clock,Field,Reading,Record
from machine_audit.query import Query
from machine_audit.store import Store, unpack_summaries

S=10**9

def process_records(count,passes,step=10*S):
    from machine_audit.sources.native import RUsage4,TaskVM
    from machine_audit.sources.macos import MacOS,ReadSource
    class Native:
        numer=denom=1
        tick=0
        def bytes(self,name):return struct.pack('=4i',100,10000,0,100) if name=='kern.clockrate' else b'synthetic-boot'
        def processes(self):return range(100,100+count)
        def process_path(self,pid):return '/usr/bin/synthetic'
        def process(self,pid):
            r=RUsage4();r.proc_start_abstime=pid*7;r.user_time=pid*1000+self.tick;r.system_time=pid*500
            r.phys_footprint=pid*100000;r.resident_size=pid*90000;r.diskio_bytesread=pid+self.tick;r.diskio_byteswritten=pid+self.tick
            return r
        def task_memory(self,pid):
            vm=TaskVM();vm.ledger_swapins=pid+self.tick;return vm,ctypes.sizeof(vm)
    native=Native();mac=MacOS(native);t=[0]
    def clock():
        t[0]+=50000;return Clock(t[0]+10**18,t[0]+S,t[0]+S)
    mac.clock=clock;source=ReadSource('macos.process','cpu',10,mac.processes,clock)
    records=[]
    for seq in range(1,passes+1):
        native.tick+=1;r=source.read();records.append(Record(r,'boot','run',seq,r.end.wall_ns,r.end));t[0]+=step
    return records


def passes(count,seconds,cpu,label=lambda pid:f'program{pid%600}',written=lambda pid:pid,own=None,start=0,first=1):
    """Synthetic 10 s passes from `start` s: cumulative CPU from cpu(pid,t), footprint rising
    with pid and written(pid) bytes a pass. `own` adds a collector self reading for that
    process. Sequences begin at `first`."""
    records=[];used=defaultdict(float)
    for t in range(0,start+seconds+1,10):
        for pid in range(count):used[pid]+=cpu(pid,t)
        if t<start:continue
        n=t//10;c=Clock(t*S,t*S,t*S);fields=[]
        for pid in range(count):
            kw=dict(entity=f'process:{pid}',label=label(pid),scope='process')
            fields+=[Field('process.cpu',used[pid],'seconds','counter',additive=True,rate_unit='CPUs',**kw),
                     Field('process.phys_footprint',pid,'bytes',component='memory',**kw),
                     Field('process.diskio_byteswritten',written(pid)*n,'bytes','counter',component='storage',additive=True,**kw)]
        records.append(Record(Reading('synthetic.process','cpu',c,c,tuple(fields)),'boot','run',len(records)+first,c.wall_ns,c))
        if own is not None:
            fields=(Field('self.user_time',used[own],'seconds','counter',entity=f'process:{own}',label='collector',scope='managed_process'),)
            records.append(Record(Reading('self.process','self',c,c,fields),'boot','run',len(records)+first,c.wall_ns,c))
    return records


class ScaleTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).parent/'.tmp';root.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=root)
        # 1 GB covers the 200 MB full-day fixture without needing the default cap free.
        self.store=Store(Path(self.tmp.name)/'readings.sqlite',Budget(self.tmp.name,limit_bytes=1_000_000_000,free_margin_bytes=0))

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def test_real_process_adapter_500_entities_saves_all_raw_fields(self):
        records=process_records(500,2)
        self.store.append(records)
        with self.store.read() as db:
            saved=[Record.unpack(row[0]) for row in db.execute('SELECT payload FROM readings ORDER BY sequence')]
        self.assertEqual([r.to_dict() for r in saved],[r.to_dict() for r in records])
        self.assertEqual(len(saved[-1].reading.fields),10500)
        self.assertLess(self.store.budget.usage()['bytes'],30_000_000)
        self.assertTrue(self.store.prune_oldest())
        lo=records[0].reading.start.wall_ns;hi=lo+3600*S
        for signal,component in [('process.cpu','cpu'),('process.ledger_swapins','memory'),('process.diskio_byteswritten','storage')]:
            answer=Query(self.store).who(lo,hi,signal=signal,component=component,limit=2000)
            self.assertTrue(answer['rows'],signal);self.assertTrue(answer['totals_complete'])

    def test_hour_answers_with_2000_process_passes_come_from_summaries_within_bounds(self):
        # Big Mac and a Linux box took 4-34 s per hour view and a 5 MB series: answers
        # decoded process passes (stored and unsaved) and carried every gauge sample's
        # instant as covered time. Hour windows answer from summaries.
        procs=process_records(2000,7,step=12*60*S);base=procs[0].reading.end.wall_ns
        host=['synthetic.cpu','synthetic.memory','synthetic.disk','synthetic.net','synthetic.power','synthetic.heat','synthetic.gpu','synthetic.probe']
        sequence=0
        for minute in range(73):
            t=base+minute*60*S;a=t-10**18+S  # the adapter's clock: wall 10**18 ahead of awake
            c0=Clock(t-60*S,a-60*S,a-60*S);c=Clock(t,a,a);batch=[]
            for source in host:
                sequence+=1;fields=(Field(source[10:]+'.work',float(minute),'seconds','counter'),Field(source[10:]+'.level',float(minute),'bytes'))
                batch.append(Record(Reading(source,'cpu',c0,c,fields),'boot','run',sequence,t,c))
            for p in procs[:-1]:
                if t-60*S<p.reading.end.wall_ns<=t:sequence+=1;batch.append(Record(p.reading,'boot','run',sequence,t,c))
            self.store.append(batch)
        end=procs[-1].reading.end.wall_ns
        collector=Collector(self.store,run_id='tail',boot_id='boot',clock=lambda:procs[-1].reading.end)
        collector.ingest(procs[-1].reading)  # an unsaved pass ending the window
        query=Query(self.store,collector)
        unpack=Record.unpack
        def counting(payload):
            record=unpack(payload);decoded.append(len(record.reading.fields));return record
        for name,call,seconds in (('series',lambda:query.series(end-3600*S,end,limit=600),0.5),
                                  ('who',lambda:query.who(end-3600*S,end,'process.cpu','cpu',limit=75),1.0),
                                  ('coverage',lambda:query.coverage(end-3600*S,end),0.5)):
            began=time.thread_time();decoded=[]
            with patch.object(Record,'unpack',side_effect=counting):answer=call()
            self.assertLess(time.thread_time()-began,seconds,name)
            self.assertFalse(answer['truncated'],name)
            self.assertTrue(answer['rows'] if name!='coverage' else answer['coverage']['sources'],name)
            self.assertEqual(decoded,[],name)  # no pass, stored or unsaved
        self.assertEqual(answer['coverage']['truncated'],False)
        series=query.series(end-3600*S,end,limit=600)
        self.assertEqual(series['resolution'],'minute')
        self.assertLess(len(json.dumps(series,separators=(',',':'))),500_000)

    def test_2000_process_minutes_keep_top_consumers_and_15_minute_rows_keep_all(self):
        # Big Mac grew 11.3 MB/min, half of it per-instance minute rows. Minute rows keep each
        # minute's top 25 by CPU, memory and bytes written (75) plus the collector; 15-minute
        # rows keep every process. The three top lists are disjoint. Processes 1000-1024 burst
        # only after a flush splits minute 1, so a minute must be ranked whole, not on its
        # first saved fragment.
        def cpu(pid,t):
            if 1000<=pid<1025:return 9.0 if 90<t<=120 else 0.0
            return 2.0 if pid<25 else 0.01
        records=passes(2000,190,cpu,written=lambda pid:(pid+1000)%2000,own=500)
        split=[r.sequence for r in records if r.reading.end.wall_ns in (90*S,160*S)]
        for lo,hi in zip([0]+split,split+[len(records)]):self.store.append(records[lo:hi])
        whole=Store(Path(self.tmp.name)/'whole.sqlite',self.store.budget)
        whole.append(records)
        def minute_rows(store):
            result={}
            for bucket,component,entity,payload in store.db.execute("SELECT bucket_ns,component,entity,payload FROM summaries WHERE resolution='minute' AND bucket_ns<180*? AND component!='self'",(S,)):
                for row in unpack_summaries(payload):result[(bucket//(60*S),entity,row['signal'])]=(row['total'],row['sample_count'])
            return result
        self.assertEqual(minute_rows(self.store),minute_rows(whole))
        whole.close()
        def named(*ranges):return {f'process:{pid}' for r in ranges for pid in r}
        others=named(range(975,1000),range(1975,2000),[500])  # top bytes written, top footprint, own
        for bucket,top_cpu in enumerate([range(25),range(1000,1025),range(25)]):
            for component in ('cpu','memory','storage'):
                entities={e for e, in self.store.db.execute("SELECT entity FROM summaries WHERE resolution='minute' AND bucket_ns=? AND component=?",(bucket*60*S,component))}
                self.assertEqual({e for e in entities if e.startswith('process:')},named(top_cpu)|others,(bucket,component))
                self.assertLessEqual(len({e for e in entities if e.startswith('program:')}),75,(bucket,component))
        for component in ('cpu','memory','storage'):
            count=self.store.db.execute("SELECT count(*) FROM summaries WHERE resolution='15-minute' AND bucket_ns=0 AND component=? AND entity LIKE 'process:%'",(component,)).fetchone()[0]
            self.assertEqual(count,2000,component)

    def test_who_answers_a_process_outside_the_top_from_15_minute_rows(self):
        # Program 0 is below the top 25 on every measure except in minutes 32 and 33, so its
        # end edge has minute rows for only some minutes; over an unaligned 30 minutes it
        # answers from 15-minute rows and says so, counting the burst once. Program 26, top
        # by bytes written, keeps minute edges and exact minute-aligned totals.
        def cpu(pid,t):
            if pid==0:return 9.0 if 1920<t<=2040 else 1.0
            return 2.0
        records=passes(27,2400,cpu,label=lambda pid:f'p{pid}')
        for lo in range(0,len(records),7):self.store.append(records[lo:lo+7])
        rows={r['entity']:r for r in Query(self.store).who(450*S,2250*S,signal='process.cpu',component='cpu')['rows']}
        light,heavy=rows['program:p0'],rows['program:p26']
        self.assertEqual((light['summary_resolution'],light['partial_boundary']),('15-minute',True))
        self.assertEqual(light['total'],240.0+12*8)  # all 2,400 s: its three 15-minute rows
        self.assertEqual(heavy['summary_resolution'],'minute')
        self.assertEqual(heavy['total'],2.0*6*31)  # minute buckets 7 to 37 whole

    def test_who_counts_minute_rows_saved_before_15_minute_rows_once(self):
        # An upgraded store has minute-only history. A run saved that way (to 420 s) and a
        # new run outside the top (from 430 s) share a quarter hour: the old minute rows end
        # before the new 15-minute row begins, so both count, once.
        records=passes(1,840,lambda pid,t:1.0)
        old=[r for r in records if r.reading.end.wall_ns<=420*S]
        self.store.append(old)
        self.store.stop_run('run',420*S)
        self.store.db.execute("DELETE FROM summaries WHERE resolution='15-minute'")
        new=[replace(r,run_id='run2',sequence=n+1) for n,r in enumerate(records[len(old):])]
        with patch('machine_audit.store.TOP_N',0):self.store.append(new)
        row=Query(self.store).who(30*S,810*S,signal='process.cpu',component='cpu')['rows'][0]
        self.assertEqual((row['summary_resolution'],row['total']),('15-minute',42.0+41.0))  # 10-420 s, 440-840 s

    def test_the_newest_minute_is_held_until_complete_and_saved_on_stop(self):
        # Saving the newest minute's rows for every process and deleting most a save later
        # doubled the rows each save reserved. It is held in memory instead; stopping the
        # run saves its top consumers: 25 by CPU (0-24), 25 by footprint and bytes (75-99).
        records=passes(100,90,lambda pid,t:2.0 if pid<25 else 0.5)
        self.store.append(records)
        def processes(bucket):
            return {e for e, in self.store.db.execute("SELECT entity FROM summaries WHERE resolution='minute' AND bucket_ns=? AND component='cpu' AND entity LIKE 'process:%'",(bucket*60*S,))}
        top={f'process:{pid}' for pid in [*range(25),*range(75,100)]}
        self.assertEqual((processes(0),processes(1)),(top,set()))
        self.store.stop_run('run',90*S)
        self.assertEqual(processes(1),top)

    def test_a_save_that_fails_before_commit_changes_nothing_saved_or_held(self):
        # A save that fails inside its transaction must leave saved rows as they were and
        # must not merge its data into the held minute, or a retried save counts it twice.
        records=passes(30,170,lambda pid,t:1.0+pid%7)
        parts=[records[:9],records[9:14],records[14:]]  # each minute spans two saves
        def summaries(store):
            return sorted((res,bucket,entity,row['signal'],row['total'],row['sample_count']) for res,bucket,entity,payload in
                          store.db.execute('SELECT resolution,bucket_ns,entity,payload FROM summaries') for row in unpack_summaries(payload))
        self.store.append(parts[0]);self.store.append(parts[1])
        saved=summaries(self.store)
        with self.assertRaisesRegex(ValueError,'sequence already committed'):self.store.append(parts[1])
        self.assertEqual(summaries(self.store),saved)
        self.store.append(parts[2]);self.store.stop_run('run',170*S)
        reference=Store(Path(self.tmp.name)/'reference.sqlite',self.store.budget)
        for part in parts:reference.append(part)
        reference.stop_run('run',170*S)
        self.assertEqual(summaries(self.store),summaries(reference))
        reference.close()

    def test_a_save_at_the_cap_prepares_once_and_prunes_only_until_it_fits(self):
        # Big Mac at the cap during a full scanner walk: each failed admission pruned one
        # raw minute and then re-merged every summary row, about 20 s a round for 165
        # rounds. A 2,000-process save must merge once and prune only until it fits.
        save=passes(2000,50,lambda pid,t:0.5,start=1810,first=1801)
        with tempfile.TemporaryDirectory(dir=Path(self.tmp.name).parent) as fresh:
            sized=Store(Path(fresh)/'readings.sqlite',Budget(fresh,limit_bytes=10**9,free_margin_bytes=0))
            empty=sized.db.execute('PRAGMA page_count').fetchone()[0]
            sized.append(save)
            growth=sized.db.execute('PRAGMA page_count').fetchone()[0]-empty
            sized.close()
        # Thirty minutes of raw-heavy host readings, all covered by summaries.
        history=[]
        for t in range(1800):
            c=Clock(t*S,t*S,t*S)
            fields=tuple(Field(f'cpu.f{i}',t*(i+1)*7919%10**9,'count') for i in range(20))
            history.append(Record(Reading('synthetic.host','cpu',c,c,fields),'boot','run',t+1,c.wall_ns,c))
        for lo in range(0,1800,60):self.store.append(history[lo:lo+60])
        page,pages=(self.store.db.execute(f'PRAGMA {name}').fetchone()[0] for name in ('page_size','page_count'))
        # Admission reserves the database plus one WAL frame per page: this fits only
        # after about half of the history is pruned, and stays under proactive pruning.
        self.store.budget.limit_bytes=(growth+pages//2)*(2*page+24)+65536
        merges=[];rounds=[]
        # Preparing looks up each summary row's saved row once, before merging it.
        self.store.db.set_trace_callback(lambda sql:sql.startswith('SELECT sequence_min,payload FROM summaries') and merges.append(1))
        prune=Store.prune_oldest
        def counting_prune(store):rounds.append(1);return prune(store)
        with patch.object(Store,'prune_oldest',counting_prune):
            self.store.append(save)
        self.store.db.set_trace_callback(None)
        written=self.store.db.execute('SELECT count(*) FROM summaries WHERE sequence_max=?',(save[-1].sequence,)).fetchone()[0]
        self.assertEqual(len(merges),written)  # one merge per saved row, however many rounds
        self.assertGreater(len(rounds),1,rounds)
        oldest=self.store.db.execute('SELECT min(wall_ns) FROM readings').fetchone()[0]
        self.assertTrue(0<oldest<1800*S,oldest)  # pruned, then stopped once the save fit

    def test_large_ordinary_record_exports_at_explicit_64_mib_bound(self):
        record=process_records(2000,1)[0]
        self.assertEqual(len(record.reading.fields),42000)
        self.store.append([record])
        path=Path(self.tmp.name)/'transfer.sqlite'
        with self.assertRaisesRegex(ValueError,'increase max_bytes'):
            self.store.export_chunk(path,device_id='device',run_id='run')
        self.assertFalse(path.exists())
        manifest=self.store.export_chunk(path,device_id='device',run_id='run',max_bytes=64*1024**2)
        with sqlite3.connect(path) as db:
            restored=json.loads(db.execute('SELECT payload FROM records').fetchone()[0])
        self.assertEqual(restored,record.to_dict())
        self.assertEqual(manifest['count'],1)
        self.assertFalse(manifest['truncated'])

    def test_hour_and_day_who_cover_600_programs_and_2000_instances(self):
        records=[]
        # Dense first hour catches the original minute-input truncation. Later
        # hourly counter windows also prove the complete 24-hour answer.
        for minute in list(range(61))+[hour*60 for hour in range(2,25)]:
            t=minute*60;c=Clock(t*S,t*S,t*S)
            fields=[Field('process.cpu',t,'seconds','counter',entity=f'process:{i}',label=f'program{i}',scope='process',additive=True,rate_unit='CPUs') for i in range(600)]
            fields.extend(Field('process.phys_footprint',100_000_000+i,'bytes',entity=f'process:{i}',label=f'program{i%600}',scope='footprint',component='memory') for i in range(2000))
            records.append(Record(Reading('macos.process','cpu',c,c,tuple(fields)),'boot','run',len(records)+1,c.wall_ns,c))
        self.store.append(records)
        code="import json,sys;sys.path.insert(0,sys.argv[1]);from test_scale import measure_queries;print(json.dumps(measure_queries(sys.argv[2])))"
        result=subprocess.run([sys.executable,'-c',code,str(Path(__file__).parent.resolve()),str(self.store.path.resolve())],capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr)
        print(result.stdout.strip())


def measure_queries(path):
    """A fresh process excludes fixture construction from query peak memory."""
    check=unittest.TestCase()
    store=Store(path,read_only=True);query=Query(store);began=time.thread_time()
    for end in (3600*S,86400*S):
        cpu=query.who(0,end,signal='process.cpu',component='cpu',limit=2000)
        memory=query.who(0,end,signal='process.phys_footprint',component='memory',limit=2000)
        check.assertEqual(cpu['total_rows'],600);check.assertEqual(memory['total_rows'],2000)
        check.assertTrue(cpu['totals_complete']);check.assertTrue(memory['totals_complete'])
        check.assertEqual(cpu['covered'],{'start_ns':0,'end_ns':end})
        check.assertTrue(all(row['total']==end//S for row in cpu['rows']))
        check.assertEqual(cpu['accounting']['attributed'],600*(end//S))
        check.assertFalse(memory['accounting']['additive'])
        check.assertTrue(all(row['mean']==100_000_000+int(row['entity'].split(':')[-1]) for row in memory['rows']))
    small=query.who(0,3600*S,signal='process.cpu',component='cpu',limit=7)
    check.assertEqual(len(small['rows']),7);check.assertTrue(small['truncated']);check.assertTrue(small['totals_complete'])
    check.assertEqual(small['accounting']['attributed'],600*3600)
    report=({'query_cpu_s':time.thread_time()-began,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024),'stored_bytes':store.budget.usage()['bytes']})
    store.close()
    return report


if __name__=='__main__':unittest.main()
