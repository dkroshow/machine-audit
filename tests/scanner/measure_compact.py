"""Synthetic metadata cost, including indexes, publication and WAL peaks.

Default is 10k entries. Coordinate larger runs; this never walks a filesystem.
"""
from array import array
from contextlib import contextmanager
from pathlib import Path
import argparse
import json
import resource
import sys
import tempfile
import time

from machine_audit.budget import Budget
from machine_audit.scanner.accounting import Blocks
from machine_audit.scanner.native import Volume
from machine_audit.scanner.store import MapStore, MapQuery
from machine_audit.scanner.walker import Node


def measure(directories=2000,files=8000,replace=False):
    scratch=Path(__file__).parent/'.tmp';scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as root:
        budget=Budget(root,free_margin_bytes=0)
        peak={'total_bytes':0,'wal_bytes':0,'db_bytes':0}
        transactions=[]
        original=budget.sqlite_write
        @contextmanager
        def measured_write(*args,**kw):
            wall,cpu=time.perf_counter(),time.thread_time()
            with original(*args,**kw) as admission:
                yield admission
                parts=budget.usage()['parts']
                peak['total_bytes']=max(peak['total_bytes'],sum(parts.values()))
                peak['wal_bytes']=max(peak['wal_bytes'],parts.get('scanner.sqlite-wal',0))
                peak['db_bytes']=max(peak['db_bytes'],parts.get('scanner.sqlite',0))
            transactions.append({'elapsed_s':time.perf_counter()-wall,'cpu_s':time.thread_time()-cpu})
        budget.sqlite_write=measured_write
        store=MapStore(Path(root)/'scanner.sqlite',budget)
        volume=Volume('synthetic','Synthetic','/synthetic','hfs',1,2*10**12,10**12,10**12,10**12)
        own=array('Q',[4096])*(directories+1)
        counts=array('I',[0])*(directories+1)
        for i in range(files):
            own[1+i%directories]+=4096*(1+i%8);counts[1+i%directories]+=1
        for i in range(1,directories+1):counts[0 if i<1000 else 1+i%min(998,directories)]+=1
        phases=[]
        def phase(name,operation):
            wall,cpu=time.perf_counter(),time.thread_time();before=store.db.total_changes
            first=len(transactions)
            result=operation()
            calls=transactions[first:]
            phases.append({'phase':name,'elapsed_s':time.perf_counter()-wall,'cpu_s':time.thread_time()-cpu,
                           'row_changes':store.db.total_changes-before,'managed_bytes':budget.usage()['bytes'],
                           'transactions':len(calls),'max_transaction_cpu_s':max((r['cpu_s'] for r in calls),default=0),
                           'last_transaction_cpu_s':calls[-1]['cpu_s'] if calls else 0})
            return result
        def stage():
            store.directory(Node('0',None,'Synthetic','directory',4096,4096,'observed',None,0))
            store.listed('0',counts[0])
            for i in range(1,directories+1):
                parent=0 if i<1000 else 1+i%min(998,directories)
                store.begin_directory(str(i))
                store.directory(Node(str(i),str(parent),f'dir{i}','directory',own[i],own[i],'observed',None,0))
                store.listed(str(i),counts[i])
            for i in range(files):
                store.file(str(10_000_000+i),str(1+i%directories),f'file{i}.dat',4096*(1+i%8),0)
                if i%1000==0:store.progress(i+directories)
            store.flush()
        store.start('synthetic_scale')
        phase('initial_prepare',lambda:store.prepare(volume))
        phase('initial_stage',stage)
        phase('initial_publish',lambda:store.finish_volume(volume,Blocks(),[],full=True,lean=True))
        initial_bytes=budget.usage()['bytes']
        expected=sum(own)
        assert MapQuery(store.path).query()['rows'][0]['uses']==expected
        if replace:
            phase('replacement_prepare',lambda:store.prepare(volume))
            phase('replacement_stage',stage)
            phase('replacement_publish',lambda:store.finish_volume(volume,Blocks(),[],full=True,lean=True))
            assert MapQuery(store.path).query()['rows'][0]['uses']==expected
        phase('hourly_prepare',lambda:store.prepare(volume,full=False))
        def update():
            store.begin_directory('1')
            store.directory(Node('1','0','dir1','directory',own[1]+8192,own[1]+8192,'observed',None,0))
            for i in range(0,files,directories):
                store.file(str(10_000_000+i),'1',f'file{i}.dat',4096*(1+i%8),0)
            store.file('999999999','1','new.dat',8192,0);store.flush()
        phase('hourly_stage',update)
        phase('hourly_publish',lambda:store.finish_volume(volume,Blocks(),[],full=False,lean=True))
        assert MapQuery(store.path).query()['rows'][0]['uses']==expected+8192
        categories={r[0]:r[1] for r in store.db.execute('SELECT name,sum(pgsize) FROM dbstat GROUP BY name')}
        result={'directories':directories+1,'files':files,'entries':directories+files+1,
                'initial_managed_bytes':initial_bytes,'initial_bytes_per_entry':initial_bytes/(directories+files+1),
                'final_managed_bytes':budget.usage()['bytes'],'peak':peak,'phases':phases,'tables_and_indexes':categories,
                'format_version':store.db.execute('PRAGMA user_version').fetchone()[0],
                'points':store.db.execute('SELECT count(*) FROM points').fetchone()[0],
                'peak_rss_bytes':int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)),
                'known_total_bytes':expected+8192}
        store.close();return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--directories',type=int,default=2000)
    parser.add_argument('--files',type=int,default=8000);parser.add_argument('--replace',action='store_true')
    args=parser.parse_args();print(json.dumps(measure(args.directories,args.files,args.replace),indent=2))
