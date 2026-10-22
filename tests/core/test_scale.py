"""Review-scale regressions. Fixtures contain no machine observations or private labels."""
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

from machine_audit.budget import Budget
from machine_audit.model import Clock,Field,Reading,Record
from machine_audit.query import Query
from machine_audit.store import Store

S=10**9

def process_records(count,passes):
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
        native.tick+=1;r=source.read();records.append(Record(r,'boot','run',seq,r.end.wall_ns,r.end));t[0]+=10*S
    return records



class ScaleTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).parent/'.tmp';root.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=root)
        self.store=Store(Path(self.tmp.name)/'readings.sqlite',Budget(self.tmp.name,free_margin_bytes=0))

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
