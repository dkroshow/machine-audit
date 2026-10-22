"""Managed-process cost from the existing pass, plus separately labelled run records."""
from __future__ import annotations
import os
from pathlib import Path
import re
import sqlite3
import threading
from dataclasses import replace
from .macos import field


class ManagedProcesses:
    def __init__(self,start):
        self.lock = threading.Lock()
        self.roles = {(os.getpid(),str(start)): 'collector'}

    def register(self, pid, start, role):
        if role not in ('collector', 'scanner', 'helper', 'transfer', 'query') or pid <= 0:
            raise ValueError('invalid_managed_process')
        with self.lock:
            self.roles[(pid,str(start))] = role

    def snapshot(self):
        with self.lock:
            return self.roles.copy()


class SelfCost:
    def __init__(self, mac, budget, operations=None, scanner=None, registry=None, memory='process.phys_footprint'):
        # The host's own memory gauge: macOS footprint, Linux resident set (not equivalent).
        self.mac, self.budget, self.memory = mac, budget, memory
        self.operations = operations if operations is not None else {}
        self.scanner = scanner
        self.registry = registry or ManagedProcesses(mac.native.process(os.getpid()).proc_start_abstime)

    def processes(self):
        reading = self.mac.process_latest
        if reading is None:
            return [field('self.cpu', None, 'mach_ticks', 'counter', status='unobserved')]
        roles = self.registry.snapshot()
        fields = []
        for f in reading.fields:
            parts = f.entity.split(':')
            if len(parts)<4 or not parts[2].isdigit() or (int(parts[2]),parts[3]) not in roles:
                continue
            pid = int(parts[2])
            role = roles[(pid,parts[3])]
            names = {'process.user_time':'self.user_time','process.system_time':'self.system_time',
                     self.memory:'self.memory','process.diskio_bytesread':'self.read',
                     'process.diskio_byteswritten':'self.write','process.pkg_idle_wkups':'self.idle_wakeups',
                     'process.interrupt_wkups':'self.interrupt_wakeups'}
            if f.name in names:
                fields.append(replace(f,name=names[f.name],label=role,scope='managed_process'))
        # Child CPU is retained by the process source, but never added here: it overlaps
        # children already sampled. Short-lived unregistered child cost is unknown.
        fields.append(field('self.unobserved_children', None, 'seconds','counter',status='unobserved'))
        fields.append(field('self.browser_cost', None,'seconds','counter',status='unobserved'))
        return fields

    def storage(self):
        usage = self.budget.usage()
        yield field('self.storage',usage['bytes'],'bytes',scope='managed_storage')
        parts={}
        for name,value in usage['parts'].items():
            # Known files have stable roles; private/unknown names are one bucket.
            key=('other','files')
            for kind in ('scanner','readings'):
                for suffix,role in (('','db'),('-wal','wal'),('-shm','shm')):
                    if name==kind+'.sqlite'+suffix:key=(kind,role)
            parts[key]=parts.get(key,0)+value
        for (kind,role),value in sorted(parts.items()):
            yield field('self.storage_part',value,'bytes',entity='storage:'+kind+':'+role,
                        label=kind,scope='managed_storage')
        for name in ('flush_count','flush_elapsed_s','query_count','query_elapsed_s','http_sent_bytes'):
            value = self.operations.get(name)
            yield field('self.'+name,value,'bytes' if name.endswith('bytes') else 'seconds' if name.endswith('_s') else 'count',
                        'counter',scope='operation_breakdown',status='observed' if value is not None else 'unobserved')
        yield field('self.config_identity',self.mac.config_identity,'sha256','state')
        revision=getattr(self.mac,'install_revision',None)
        valid=isinstance(revision,str) and re.fullmatch(r'[0-9a-f]{7,40}',revision)
        yield field('self.install_revision',revision if valid else None,'git_commit','state',
                    status='observed' if valid else 'unobserved' if revision is None else 'invalid')

    def scanner_runs(self):
        path = Path(self.scanner) if self.scanner else self.budget.root/'scanner.sqlite'
        if not path.exists():
            yield field('self.scanner_cpu_total',None,'ns',status='unobserved',scope='scanner_run')
            return
        db = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)
        try:
            rows = db.execute('SELECT id,pid,pid_start,cpu_ns,peak_rss_bytes,written_bytes,updated_ns,status FROM runs ORDER BY started_ns DESC LIMIT 32')
            for run,pid,start,cpu,rss,writes,updated,status in rows:
                if status=='running':
                    self.registry.register(pid,start,'scanner')
                entity='scan-run:'+run
                yield field('self.scanner_status',status,'state','state',entity=entity,scope='scanner_run')
                # Run totals are evidence for completed/interrupted work, not additive to
                # live process counter windows. This avoids counting a scanner twice.
                yield field('self.scanner_cpu_total',cpu,'ns',entity=entity,scope='scanner_run')
                yield field('self.scanner_peak_memory',rss,'bytes',entity=entity,scope='scanner_run')
                yield field('self.scanner_written_total',writes,'bytes',entity=entity,scope='scanner_run')
                yield field('self.scanner_checkpoint_time',updated,'unix_ns',entity=entity,scope='scanner_run')
        finally:
            db.close()
