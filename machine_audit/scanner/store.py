"""Private, compact scanner maps with staged atomic publication."""
from __future__ import annotations
from contextlib import contextmanager
from collections import defaultdict, deque
from array import array
import base64
import hashlib
import json
import os
from pathlib import Path
import resource
import sqlite3
import sys
import time
import uuid

from machine_audit.budget import Budget, BudgetExceeded
from .accounting import reconcile
from .encoding import inode_key, source_id
from .hierarchy import save_hierarchy

SCHEMA = '''
CREATE TABLE IF NOT EXISTS map_meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL) WITHOUT ROWID;
INSERT OR IGNORE INTO map_meta VALUES('next_generation',1);
CREATE TABLE IF NOT EXISTS generations(id INTEGER PRIMARY KEY,volume INTEGER,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS volumes(
 key INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,label TEXT NOT NULL,mount TEXT NOT NULL,generation INTEGER,
 observed_ns INTEGER,full_ns INTEGER,volume_used INTEGER,capacity INTEGER,free INTEGER,available INTEGER,
 valid INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,lean INTEGER NOT NULL,bookmark INTEGER,
 no_progress INTEGER NOT NULL DEFAULT 0,event_uuid TEXT,gaps TEXT NOT NULL DEFAULT '[]',mounted INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS nodes(
 generation INTEGER NOT NULL,volume INTEGER NOT NULL,id INTEGER NOT NULL,parent INTEGER,label TEXT,kind TEXT NOT NULL,
 own_listed INTEGER NOT NULL,own_uses INTEGER NOT NULL,listed INTEGER,uses INTEGER,status TEXT NOT NULL,mtime INTEGER,
 PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS node_parent ON nodes(generation,volume,parent,label);
CREATE INDEX IF NOT EXISTS node_size ON nodes(generation,volume,parent,uses DESC,id DESC);
CREATE INDEX IF NOT EXISTS node_listed ON nodes(generation,volume,parent,listed DESC,id DESC);
-- Coverage counts excluded roots without scanning the whole map.
CREATE INDEX IF NOT EXISTS node_excluded ON nodes(generation,volume) WHERE status='excluded';
CREATE TABLE IF NOT EXISTS files(
 generation INTEGER NOT NULL,volume INTEGER NOT NULL,parent INTEGER NOT NULL,id INTEGER NOT NULL,label TEXT,
 allocated INTEGER NOT NULL,mtime INTEGER,PRIMARY KEY(generation,volume,parent,id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS file_size ON files(generation,volume,parent,allocated DESC,id DESC);
CREATE TABLE IF NOT EXISTS holders(
 generation INTEGER NOT NULL,volume INTEGER NOT NULL,dir INTEGER NOT NULL,file_id INTEGER NOT NULL,
 stream INTEGER NOT NULL,allocated INTEGER NOT NULL,private INTEGER,partial INTEGER NOT NULL,
 PRIMARY KEY(generation,volume,dir,file_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS holder_stream ON holders(generation,volume,stream);
CREATE INDEX IF NOT EXISTS holder_file ON holders(generation,volume,file_id,dir);
CREATE TABLE IF NOT EXISTS charges(
 generation INTEGER NOT NULL,volume INTEGER NOT NULL,stream INTEGER NOT NULL,charge INTEGER NOT NULL,
 allocated INTEGER NOT NULL,private INTEGER,partial INTEGER NOT NULL,dir INTEGER NOT NULL,
 PRIMARY KEY(generation,volume,stream)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS listings(
 generation INTEGER NOT NULL,volume INTEGER NOT NULL,id INTEGER NOT NULL,listed_ns INTEGER,count INTEGER,
 PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS removed(generation INTEGER,volume INTEGER,id INTEGER,PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS reset_dirs(generation INTEGER,volume INTEGER,id INTEGER,PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS removed_charges(generation INTEGER,volume INTEGER,id INTEGER,PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS tracked(volume TEXT,id TEXT,parent TEXT,PRIMARY KEY(volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS points(
 volume TEXT NOT NULL,id TEXT NOT NULL,observed_ns INTEGER NOT NULL,label TEXT,metric TEXT NOT NULL,
 size INTEGER,status TEXT NOT NULL,PRIMARY KEY(volume,id,observed_ns)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hierarchy_epochs(
 volume TEXT NOT NULL,observed_ns INTEGER NOT NULL,version TEXT NOT NULL,PRIMARY KEY(volume,observed_ns)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hierarchy_nodes(
 volume TEXT NOT NULL,version TEXT NOT NULL,id TEXT NOT NULL,parent TEXT,label TEXT,present INTEGER NOT NULL,
 rank INTEGER NOT NULL,first_rank INTEGER NOT NULL,PRIMARY KEY(volume,version,rank),UNIQUE(volume,version,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS dirty(volume TEXT,id TEXT,due_ns INTEGER,PRIMARY KEY(volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY,started_ns INTEGER NOT NULL,updated_ns INTEGER NOT NULL,ended_ns INTEGER,
 pid INTEGER NOT NULL,pid_start TEXT,status TEXT NOT NULL,entries INTEGER NOT NULL DEFAULT 0,cpu_ns INTEGER NOT NULL,
 peak_rss_bytes INTEGER NOT NULL,written_bytes INTEGER,kind TEXT NOT NULL) WITHOUT ROWID;
'''
MAP_TABLES=('nodes','files','holders','charges','listings')
STAGE_TABLES=('removed','reset_dirs','removed_charges')
MAX_ANCESTRY=1024


class MapStore:
    def __init__(self,path,budget=None):
        self.path=Path(path)
        self.budget=budget or Budget(self.path.parent)
        if self.path.is_symlink():raise ValueError('scanner_store_cannot_be_symlink')
        if self.path.parent.resolve()!=self.budget.root.resolve():
            raise ValueError('scanner_store_must_be_in_budget_directory')
        self.db=None
        try:
            # Opening and schema reads can create WAL/SHM before write admission.
            # Release this startup reservation before taking sqlite_write's lock.
            with self.budget.reserve(256*1024):
                self.db=sqlite3.connect(self.path,isolation_level=None,timeout=5)
                self.db.row_factory=sqlite3.Row
                existing=self.db.execute("SELECT 1 FROM sqlite_master WHERE name='volumes'").fetchone()
                if existing and self.db.execute('PRAGMA user_version').fetchone()[0]!=2:
                    raise ValueError('scanner_format_requires_fresh_store')
                os.chmod(self.path,0o600)
                self.db.execute('PRAGMA journal_mode=WAL')
                self.db.execute('PRAGMA temp_store=MEMORY')
                self.db.execute('PRAGMA busy_timeout=5000')
                self.db.create_function('source_id',1,source_id,deterministic=True)
                self.db.create_function('inode_key',1,inode_key,deterministic=True)
            with self.write():
                for statement in SCHEMA.split(';'):
                    if statement.strip():self.db.execute(statement)
                self.db.execute('PRAGMA user_version=2')
        except BaseException:
            if self.db is not None:self.db.close()
            raise
        self.buffer=[];self.buffer_bytes=0
        self.generation=self.volume=self.vid=self.base_gen=self.run=None
        self.changed=set();self.last_progress=0.0;self.entries=0

    @contextmanager
    def write(self,growth=None):
        # Ask for the admissible range, not a payload multiplier. Budget still
        # clamps DB/WAL/SHM under its conservative cross-process hard bound.
        with self.budget.sqlite_write(self.db,self.path,max_growth_bytes=growth):
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                if self.db.in_transaction:self.db.execute('ROLLBACK')
                raise

    def start(self,kind):
        self.run=uuid.uuid4().hex;now=time.time_ns()
        with self.write():
            self.db.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
            self.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (self.run,now,now,None,os.getpid(),getattr(self,'process_start',None),'running',0,time.process_time_ns(),0,None,kind))
        return self.run

    def progress(self,entries=None,status='running'):
        if not self.run:return
        if entries is not None:self.entries=max(self.entries,entries)
        now=time.monotonic()
        if status=='running' and now-self.last_progress<5:return
        self.last_progress=now
        r=resource.getrusage(resource.RUSAGE_SELF)
        with self.write():
            self.db.execute('UPDATE runs SET updated_ns=?,entries=?,cpu_ns=?,peak_rss_bytes=?,written_bytes=?,status=?,ended_ns=? WHERE id=?',
                (time.time_ns(),self.entries,time.process_time_ns(),int(r.ru_maxrss*(1 if sys.platform=='darwin' else 1024)),
                 self.written_bytes() if hasattr(self,'written_bytes') else None,status,
                 None if status=='running' else time.time_ns(),self.run))

    def prepare(self,volume,full=True,lean=True):
        self.flush();self.prune_generations(full)
        with self.write():
            self.db.execute('INSERT OR IGNORE INTO volumes(id,label,mount,status,lean) VALUES(?,?,?,?,?)',
                            (volume.id,volume.label,volume.mount,'building',int(lean)))
            old=self.db.execute('SELECT * FROM volumes WHERE id=?',(volume.id,)).fetchone()
            self.generation=self.db.execute("SELECT value FROM map_meta WHERE key='next_generation'").fetchone()[0]
            self.db.execute("UPDATE map_meta SET value=value+1 WHERE key='next_generation'")
            self.db.execute("INSERT INTO generations VALUES(?,?,'building')",(self.generation,old['key']))
        self.volume,self.vid,self.mount=volume.id,old['key'],volume.mount
        self.base_gen=old['generation'] if not full else None
        self.changed=set()
        return old

    def prune_generations(self,full=True):
        obsolete=[r[0] for r in self.db.execute("SELECT id FROM generations WHERE status='building' OR (? AND status='retired')",(full,))]
        for generation in obsolete:
            for table in MAP_TABLES+STAGE_TABLES:
                columns=sorted(self.db.execute(f'PRAGMA table_info({table})'),key=lambda r:r['pk'])
                key=','.join(r['name'] for r in columns if r['pk'])
                # Cache spilling stays disabled. Bound deletions per commit,
                # retaining the generation record so interrupted cleanup resumes.
                while True:
                    with self.write():
                        removed=self.db.execute(f'''DELETE FROM {table} WHERE ({key}) IN
                            (SELECT {key} FROM {table} WHERE generation=? ORDER BY {key} LIMIT 4096)''',
                            (generation,)).rowcount
                    if removed<4096:break
            with self.write():
                self.db.execute('DELETE FROM generations WHERE id=?',(generation,))

    def observe_unscanned(self,volume,reason,lean=True):
        with self.write():
            self.db.execute('''INSERT INTO volumes(id,label,mount,status,lean,capacity,free,available,volume_used,mounted,gaps)
                VALUES(?,?,?,?,?,?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET mounted=1,capacity=excluded.capacity,
                free=excluded.free,available=excluded.available,volume_used=excluded.volume_used,status=excluded.status,gaps=excluded.gaps''',
                (volume.id,volume.label,volume.mount,reason,int(lean),volume.capacity,volume.free,volume.available,volume.used,json.dumps([reason])))

    def _queue(self,query,args):
        self.buffer.append((query,args))
        self.buffer_bytes+=sum(len(x.encode()) if isinstance(x,str) else 8 for x in args)
        if len(self.buffer)>=8192 or self.buffer_bytes>=2*1024**2:self.flush()

    def flush(self):
        if not self.buffer:return
        groups=defaultdict(list)
        for query,args in self.buffer:groups[query].append(args)
        with self.write():
            for query,rows in groups.items():self.db.executemany(query,rows)
        self.buffer.clear();self.buffer_bytes=0

    def listed(self,identity,count):
        self._queue('INSERT OR REPLACE INTO listings VALUES(?,?,?,?,?)',
                    (self.generation,self.vid,inode_key(identity),time.time_ns(),count))

    def defer(self,identity,due_ns):
        with self.write():self.db.execute('INSERT OR REPLACE INTO dirty VALUES(?,?,?)',(self.volume,identity,due_ns))

    def directory(self,node):
        identity=inode_key(node.id)
        if self.base_gen is not None:
            self.changed.add(identity)
            self._queue('DELETE FROM removed WHERE generation=? AND volume=? AND id=?',(self.generation,self.vid,identity))
        self._queue('INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (self.generation,self.vid,identity,inode_key(node.parent_id),node.label,node.kind,
                     node.listed,node.uses,None,None,node.status,node.mtime_ns))

    def file(self,identity,parent,label,allocated,mtime):
        self._queue('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)',
                    (self.generation,self.vid,inode_key(parent),inode_key(identity),label,allocated,mtime))

    def holder(self,identity,directory,stream,allocated,private,partial):
        self._queue('INSERT OR REPLACE INTO holders VALUES(?,?,?,?,?,?,?,?)',
                    (self.generation,self.vid,inode_key(directory),inode_key(identity),inode_key(stream),allocated,private,int(partial)))

    def _rows(self,table,where='1',args=()):
        """Only the scanner sees staged rows. Public queries read published rows."""
        identity={'nodes':'id','files':'parent','holders':'dir','charges':'stream','listings':'id'}[table]
        removed='removed_charges' if table=='charges' else 'removed'
        # SQLite otherwise prefers the volume-wide primary-key prefix for
        # these SELECT * lookups, scanning every holder per stream or file.
        index={'stream=?':'holder_stream','file_id=?':'holder_file'}.get(where) if table=='holders' else None
        hint=f' INDEXED BY {index}' if index else ''
        staged=f'SELECT * FROM {table}{hint} WHERE generation=? AND volume=? AND {where}'
        params=[self.generation,self.vid,*args]
        if table in ('nodes','listings'):
            staged+=f' AND {identity} NOT IN (SELECT id FROM removed WHERE generation=? AND volume=?)'
            params.extend((self.generation,self.vid))
        if self.base_gen is None:return self.db.execute(staged,params)
        if table in ('files','holders'):
            hidden=f'''NOT EXISTS(SELECT 1 FROM reset_dirs s WHERE s.generation=? AND s.volume=? AND s.id=b.{identity})
                       AND NOT EXISTS(SELECT 1 FROM removed s WHERE s.generation=? AND s.volume=? AND s.id=b.{identity})'''
            extra=[self.generation,self.vid,self.generation,self.vid]
        else:
            hidden=f'''NOT EXISTS(SELECT 1 FROM {table} s WHERE s.generation=? AND s.volume=? AND s.{identity}=b.{identity})
                       AND NOT EXISTS(SELECT 1 FROM {removed} s WHERE s.generation=? AND s.volume=? AND s.id=b.{identity})'''
            extra=[self.generation,self.vid,self.generation,self.vid]
        return self.db.execute(staged+f' UNION ALL SELECT * FROM {table} b{hint} WHERE generation=? AND volume=? AND {where} AND '+hidden,
                               params+[self.base_gen,self.vid,*args,*extra])

    def _node(self,identity):
        return self._rows('nodes','id=?',(identity,)).fetchone()

    def path_for(self,identity):
        parts=[];seen=set()
        for _ in range(MAX_ANCESTRY):
            if identity in seen:raise ValueError('cyclic_directory_ancestry')
            seen.add(identity);node=self._node(identity)
            if node is None:raise ValueError('missing_directory_ancestor')
            if node['parent'] is None:return os.path.join(self.mount,*reversed(parts))
            if node['label'] is None:return None
            parts.append(node['label']);identity=node['parent']
        raise ValueError('directory_ancestry_limit')

    def known_directory(self,identity):
        self.flush();node=self._node(inode_key(identity))
        if node is None:return None
        result=dict(node);result.update(id=source_id(node['id']),parent=source_id(node['parent']),path=self.path_for(node['id']))
        return result

    def find_path(self,path):
        self.flush()
        if os.path.commonpath((self.mount,path))!=self.mount:return None
        roots=list(self._rows('nodes','parent IS NULL'))
        if len(roots)!=1:return None
        identity=roots[0]['id'];relative=os.path.relpath(path,self.mount)
        parts=[] if relative=='.' else relative.split(os.sep)
        if len(parts)>MAX_ANCESTRY:raise ValueError('directory_ancestry_limit')
        for name in parts:
            rows=list(self._rows('nodes','parent=? AND label=?',(identity,name)))
            if len(rows)!=1:return None
            identity=rows[0]['id']
        return self.known_directory(source_id(identity))

    def listing(self,identity):
        self.flush();return self._rows('listings','id=?',(inode_key(identity),)).fetchone()

    def begin_directory(self,identity):
        if self.base_gen is None:return
        self.flush();identity=inode_key(identity)
        with self.write():
            self.db.execute('INSERT OR IGNORE INTO reset_dirs VALUES(?,?,?)',(self.generation,self.vid,identity))
            for table,key in (('files','parent'),('holders','dir')):
                self.db.execute(f'DELETE FROM {table} WHERE generation=? AND volume=? AND {key}=?',(self.generation,self.vid,identity))

    def _subtree(self,identity):
        pending=[identity];seen=set()
        while pending:
            current=pending.pop()
            if current in seen:raise ValueError('cyclic_directory_ancestry')
            seen.add(current)
            pending.extend(row['id'] for row in self._rows('nodes','parent=?',(current,)))
        return seen

    def relocate(self,identity,parent,label,path,private):
        key=inode_key(identity);old=self._node(key)
        if old is None:return False
        if old['parent']==inode_key(parent) and old['label']==(None if private else label) and (old['kind']=='private')==private:
            return True
        changed={key} if not private else self._subtree(key)
        for current in changed:
            node=dict(self._node(current))
            node.update(generation=self.generation)
            if current==key:node.update(parent=inode_key(parent),label=None if private else label)
            if private:
                node.update(label=None,kind='private')
                files=list(self._rows('files','parent=?',(current,)))
                holders=list(self._rows('holders','dir=?',(current,)))
                self.begin_directory(source_id(current))
                for f in files:self.file(source_id(f['id']),source_id(current),None,f['allocated'],f['mtime'])
                for h in holders:self.holder(source_id(h['file_id']),source_id(current),source_id(h['stream']),h['allocated'],h['private'],h['partial'])
            self._queue('INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',tuple(node.values()))
            self.changed.add(current)
        return True

    def forget_children(self,parent,present):
        self.flush();present={inode_key(x) for x in present}
        for row in list(self._rows('nodes','parent=?',(inode_key(parent),))):
            if row['id'] not in present:self.clear_subtree(source_id(row['id']))

    def clear_subtree(self,identity):
        self.flush();keys=self._subtree(inode_key(identity))
        with self.write():
            self.db.executemany('INSERT OR IGNORE INTO removed VALUES(?,?,?)',((self.generation,self.vid,k) for k in keys))
        self.changed.update(keys)

    def _old_node(self,key):
        if self.base_gen is None:return None
        return self.db.execute('SELECT * FROM nodes WHERE generation=? AND volume=? AND id=?',(self.base_gen,self.vid,key)).fetchone()

    def _charge(self,stream):
        return self._rows('charges','stream=?',(stream,)).fetchone()

    def _sharing(self,blocks,full):
        charges=blocks.charges();complete=not blocks.estimated
        affected=set(self.changed)
        pending=[]
        def save_charges():
            if not pending:return
            if full:
                # Only this unpublished generation changes. Bound each write;
                # no transaction or reservation spans the stream lookup loop.
                with self.write(growth=16*1024**2):
                    self.db.executemany('INSERT OR REPLACE INTO charges VALUES(?,?,?,?,?,?,?,?)',pending)
                self.progress()
            else:
                self.db.executemany('INSERT OR REPLACE INTO charges VALUES(?,?,?,?,?,?,?,?)',pending)
            pending.clear()
        if full:
            streams=[r[0] for r in self.db.execute('SELECT DISTINCT stream FROM holders WHERE generation=? AND volume=?',(self.generation,self.vid))]
        else:
            streams={r[0] for r in self.db.execute('''SELECT stream FROM holders WHERE generation=? AND volume=? AND dir IN
                 (SELECT id FROM reset_dirs WHERE generation=? AND volume=? UNION SELECT id FROM removed WHERE generation=? AND volume=?)''',
                 (self.base_gen,self.vid,self.generation,self.vid,self.generation,self.vid))}
            streams.update(r[0] for r in self.db.execute('SELECT DISTINCT stream FROM holders WHERE generation=? AND volume=?',(self.generation,self.vid)))
        for stream in streams:
            old=self._charge(stream)
            if old:affected.add(old['dir'])
            holders=list(self._rows('holders','stream=?',(stream,)))
            affected.update(h['dir'] for h in holders)
            if not holders:
                self.db.execute('INSERT OR IGNORE INTO removed_charges VALUES(?,?,?)',(self.generation,self.vid,stream));continue
            owner=min(holders,key=lambda h:(str(h['file_id']),str(h['dir'])))
            same=old and all(old[k]==owner[k] for k in ('allocated','private','partial'))
            if not full and same:amount=old['charge']
            elif not full and owner['partial']:
                amount=owner['private'] or 0;complete=False
            else:amount=charges.get(int(source_id(stream)),owner['private'] or 0 if owner['partial'] else owner['allocated'])
            pending.append((self.generation,self.vid,stream,amount,owner['allocated'],owner['private'],owner['partial'],owner['dir']))
            if len(pending)>=4096:save_charges()
        save_charges()
        return affected,complete

    def _own(self,node,homes):
        listed=uses=node['own_listed'];streams=set()
        for h in self._rows('holders','dir=?',(node['id'],)):
            if h['file_id'] not in homes:
                holders=list(self._rows('holders','file_id=?',(h['file_id'],)))
                homes[h['file_id']]=min((r['dir'] for r in holders),key=str)
            if homes[h['file_id']]!=node['id']:listed-=h['allocated']
            uses-=h['allocated']
            if h['stream'] not in streams:
                charge=self._charge(h['stream'])
                if charge and charge['dir']==node['id']:uses+=charge['charge']
                streams.add(h['stream'])
        return listed,uses

    def _aggregate_full(self,affected):
        # Files remain on disk. Only directory IDs and integer totals form the
        # temporary bottom-up working set; no recursive SQL or ancestor scans.
        keys=[];parents=[];listed=array('q');uses=array('q')
        for row in self.db.execute('SELECT id,parent,own_listed FROM nodes WHERE generation=? AND volume=?',
                                   (self.generation,self.vid)):
            keys.append(row['id']);parents.append(row['parent'])
            listed.append(row['own_listed']);uses.append(row['own_listed'])
        index={key:i for i,key in enumerate(keys)}
        homes={}
        for identity in affected:
            node=self._node(identity)
            if node:
                listed[index[identity]],uses[index[identity]]=self._own(node,homes)
        parent_index=array('q');remaining=array('I',[0])*len(keys)
        for parent in parents:
            if parent is not None and parent not in index:raise ValueError('missing_directory_ancestor')
            position=index[parent] if parent is not None else -1
            parent_index.append(position)
            if position>=0:remaining[position]+=1
        del parents,index
        ready=deque(i for i,count in enumerate(remaining) if not count);visited=0
        while ready:
            child=ready.popleft();visited+=1;parent=parent_index[child]
            if parent>=0:
                listed[parent]+=listed[child];uses[parent]+=uses[child]
                remaining[parent]-=1
                if not remaining[parent]:ready.append(parent)
        if visited!=len(keys):raise ValueError('cyclic_directory_ancestry')
        self.progress()
        for first in range(0,len(keys),8192):
            with self.write():
                self.db.executemany('UPDATE nodes SET listed=?,uses=? WHERE generation=? AND volume=? AND id=?',
                    ((listed[i],uses[i],self.generation,self.vid,keys[i]) for i in range(first,min(first+8192,len(keys)))))
            self.progress()

    def _aggregate(self,affected):
        homes={}
        work=set()
        for identity in affected:
            old=self._old_node(identity)
            seeds=[identity]+([old['parent']] if old and old['parent'] is not None else [])
            for seed in seeds:
                seen=set()
                for _ in range(MAX_ANCESTRY):
                    if seed is None:break
                    if seed in seen:raise ValueError('cyclic_directory_ancestry')
                    seen.add(seed);work.add(seed);node=self._node(seed)
                    if node is None:break
                    seed=node['parent']
                else:raise ValueError('directory_ancestry_limit')
        depths={}
        def depth(key):
            chain=[];seen=set();current=key
            while current is not None and current not in depths:
                if current in seen:raise ValueError('cyclic_directory_ancestry')
                if len(chain)>=MAX_ANCESTRY:raise ValueError('directory_ancestry_limit')
                seen.add(current);chain.append(current);node=self._node(current)
                if node is None and current!=key:raise ValueError('missing_directory_ancestor')
                current=node['parent'] if node else None
            value=depths.get(current,-1)
            for item in reversed(chain):value+=1;depths[item]=value
            return depths[key]
        for identity in sorted(work,key=depth,reverse=True):
            node=self._node(identity)
            if node is None:continue
            listed,uses=self._own(node,homes)
            for child in self._rows('nodes','parent=?',(identity,)):
                listed+=child['listed'];uses+=child['uses']
            updated=list(node);updated[0]=self.generation;updated[8]=listed;updated[9]=uses
            self.db.execute('INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',updated)
            self.changed.add(identity)

    def _publish_changes(self):
        if self.base_gen is None:return
        for table,key in (('files','parent'),('holders','dir')):
            self.db.execute(f'''DELETE FROM {table} WHERE generation=? AND volume=? AND {key} IN
                (SELECT id FROM reset_dirs WHERE generation=? AND volume=? UNION SELECT id FROM removed WHERE generation=? AND volume=?)''',
                (self.base_gen,self.vid,self.generation,self.vid,self.generation,self.vid))
        for table,key in (('nodes','id'),('listings','id'),('charges','stream')):
            removed='removed_charges' if table=='charges' else 'removed'
            self.db.execute(f'DELETE FROM {table} WHERE generation=? AND volume=? AND {key} IN (SELECT id FROM {removed} WHERE generation=? AND volume=?)',
                (self.base_gen,self.vid,self.generation,self.vid))
        for table in MAP_TABLES:
            key={'nodes':'id','files':'parent','holders':'dir','listings':'id','charges':'stream'}[table]
            removed='removed_charges' if table=='charges' else 'removed'
            self.db.execute(f'DELETE FROM {table} WHERE generation=? AND volume=? AND {key} IN (SELECT id FROM {removed} WHERE generation=? AND volume=?)',
                            (self.generation,self.vid,self.generation,self.vid))
            columns=[r[1] for r in self.db.execute(f'PRAGMA table_info({table})')]
            names=','.join(columns[1:])
            self.db.execute(f'INSERT OR REPLACE INTO {table} SELECT ?,{names} FROM {table} WHERE generation=? AND volume=?',
                            (self.base_gen,self.generation,self.vid))
            self.db.execute(f'DELETE FROM {table} WHERE generation=? AND volume=?',(self.generation,self.vid))

    def finish_volume(self,volume,blocks,gaps,*,full,lean,bookmark=None,event_uuid=None,complete=True):
        self.flush();old=self.db.execute('SELECT * FROM volumes WHERE id=?',(self.volume,)).fetchone()
        if full:
            affected,sharing_complete=self._sharing(blocks,True)
            self._aggregate_full(affected)
        with self.write():
            if not full:
                affected,sharing_complete=self._sharing(blocks,False)
                self._aggregate(affected)
            if not sharing_complete:gaps=set(gaps)|{'partial_stream_needs_full_map'}
            complete=complete and sharing_complete
            roots=list(self._rows('nodes','parent IS NULL'))
            total=sum(r['uses'] for r in roots) if roots else None
            result=reconcile(volume.used,total,supported=volume.fs in ('apfs','hfs'),complete=complete,
                             previously_invalid=bool(old['generation'] and not old['valid']),full=full)
            self._publish_changes()
            published=self.base_gen if self.base_gen is not None else self.generation
            now=time.time_ns()
            self._points(published,now,result['valid'],full)
            save_hierarchy(self.db,published,self.volume,now,volume_key=self.vid)
            self.db.execute('''UPDATE volumes SET label=?,mount=?,generation=?,observed_ns=?,full_ns=?,volume_used=?,capacity=?,free=?,available=?,
                valid=?,status=?,lean=?,bookmark=?,no_progress=?,event_uuid=?,gaps=?,mounted=1 WHERE id=?''',
                (volume.label,volume.mount,published,now,now if full else old['full_ns'],volume.used,volume.capacity,volume.free,volume.available,
                 int(result['valid']),result['status'],int(lean),bookmark,old['no_progress']+1 if bookmark==old['bookmark'] else 0,
                 event_uuid,json.dumps(sorted(set(gaps))),self.volume))
            if full and old['generation'] is not None:
                self.db.execute("UPDATE generations SET status='retired' WHERE id=?",(old['generation'],))
            self.db.execute("UPDATE generations SET status='published' WHERE id=?",(published,))
            if self.base_gen is not None:
                for table in STAGE_TABLES:self.db.execute(f'DELETE FROM {table} WHERE generation=?',(self.generation,))
                self.db.execute('DELETE FROM generations WHERE id=?',(self.generation,))
        self.generation=published;self.base_gen=None
        return result

    def _points(self,generation,now,valid,full):
        metric='uses' if valid else 'listed'
        if full:
            rows=self.db.execute(f'''SELECT * FROM nodes WHERE generation=? AND volume=? AND ({metric}>=1000000000 OR
                 EXISTS(SELECT 1 FROM tracked WHERE volume=? AND id=source_id(nodes.id)))''',(generation,self.vid,self.volume))
        else:
            rows=(self.db.execute('SELECT * FROM nodes WHERE generation=? AND volume=? AND id=?',(generation,self.vid,k)).fetchone() for k in self.changed)
        for row in rows:
            if row is None:continue
            identity=source_id(row['id'])
            last=self.db.execute('SELECT * FROM points WHERE volume=? AND id=? ORDER BY observed_ns DESC LIMIT 1',(self.volume,identity)).fetchone()
            if last is None and row[metric]<1_000_000_000:continue
            # Baseline readout context only; historical placement has its own
            # immutable observations and must not be inferred from this cache.
            self.db.execute('INSERT INTO tracked VALUES(?,?,?) ON CONFLICT(volume,id) DO UPDATE SET parent=excluded.parent',
                            (self.volume,identity,source_id(row['parent'])))
            status=row['status'] if row['status']!='observed' else 'below_threshold' if row[metric]<1_000_000_000 else 'observed'
            size=row[metric] if status in ('observed','below_threshold') else None
            if not last or (last['size'],last['metric'],last['status'])!=(size,metric,status):
                self.db.execute('INSERT INTO points VALUES(?,?,?,?,?,?,?)',(self.volume,identity,now,row['label'],metric,size,status))
        for tracked in self.db.execute('SELECT id FROM tracked WHERE volume=?',(self.volume,)):
            if not self.db.execute('SELECT 1 FROM nodes WHERE generation=? AND volume=? AND id=?',(generation,self.vid,inode_key(tracked['id']))).fetchone():
                last=self.db.execute('SELECT * FROM points WHERE volume=? AND id=? ORDER BY observed_ns DESC LIMIT 1',(self.volume,tracked['id'])).fetchone()
                if last and (last['size'] is not None or last['status']!='not_observed'):
                    self.db.execute('INSERT INTO points VALUES(?,?,?,?,?,?,?)',(self.volume,tracked['id'],now,last['label'],metric,None,'not_observed'))

    def mark_failed(self,status):
        self.buffer.clear();self.buffer_bytes=0
        with self.write():self.db.execute('UPDATE volumes SET status=? WHERE id=?',(status,self.volume))
        self.progress(status=status)

    def close(self):self.db.close()


def _cursor(value):
    if not value:return {}
    if not isinstance(value,str) or len(value)>512:raise ValueError('invalid_storage_cursor')
    try:
        decoded=json.loads(base64.urlsafe_b64decode(value.encode()).decode())
        if not isinstance(decoded,dict) or set(decoded)-{'id','size','scope'}:raise ValueError()
        if 'id' in decoded and (not isinstance(decoded['id'],str) or len(decoded['id'])>160):raise ValueError()
        if 'size' in decoded and (not isinstance(decoded['size'],int) or decoded['size']<0):raise ValueError()
        if not isinstance(decoded.get('scope'),str) or len(decoded['scope'])!=64:raise ValueError()
        return decoded
    except (ValueError,UnicodeError) as exc:raise ValueError('invalid_storage_cursor') from exc


def _scope(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _encode_cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value,separators=(',',':')).encode()).decode()


HOT_SET=f'''
WITH hot(volume_id,vkey,generation,mount,id,size) AS (
  SELECT v.id,v.key,v.generation,v.mount,n.id,n.listed FROM volumes v
  JOIN nodes n ON n.generation=v.generation AND n.volume=v.key
  WHERE v.mounted=1 AND n.kind='directory' AND n.listed>=1000000000
  UNION ALL
  SELECT v.id,v.key,v.generation,v.mount,f.parent,f.allocated FROM volumes v
  JOIN files f ON f.generation=v.generation AND f.volume=v.key
  WHERE v.mounted=1 AND f.allocated>=100000000),
top AS (SELECT volume_id,vkey,generation,mount,id,max(size) AS size FROM hot
  GROUP BY vkey,id ORDER BY size DESC LIMIT ?),
chain(volume_id,generation,vkey,mount,size,start,start_parent,node,rel,depth) AS (
  SELECT t.volume_id,t.generation,t.vkey,t.mount,t.size,t.id,n.parent,n.parent,
         CASE WHEN n.parent IS NULL THEN '' ELSE '/'||n.label END,0
  FROM top t JOIN nodes n ON n.generation=t.generation AND n.volume=t.vkey AND n.id=t.id
  UNION ALL
  SELECT c.volume_id,c.generation,c.vkey,c.mount,c.size,c.start,c.start_parent,n.parent,
         CASE WHEN n.parent IS NULL THEN c.rel ELSE '/'||n.label||c.rel END,c.depth+1
  FROM chain c JOIN nodes n ON n.generation=c.generation AND n.volume=c.vkey AND n.id=c.node
  WHERE c.rel IS NOT NULL AND c.depth<{MAX_ANCESTRY})
SELECT volume_id,start,start_parent,mount,rel FROM chain WHERE node IS NULL AND rel IS NOT NULL ORDER BY size DESC
'''


def hot_set(path,limit):
    """Published folders to watch between full walks, largest first: folders of 1 GB or more and
    folders directly holding a file of 100 MB or more. Returns (volume id, folder id, parent id,
    path) rows; a folder under a private name has no path and is left out. One statement, so
    the read holds its WAL snapshot only while it runs."""
    db=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,timeout=2)
    try:
        if db.execute('PRAGMA user_version').fetchone()[0]!=2:
            raise ValueError('scanner_format_requires_fresh_store')
        rows=db.execute(HOT_SET,(limit,)).fetchall()
    finally:
        db.close()
    return [(volume,source_id(folder),source_id(parent),os.path.normpath(mount+rel)) for volume,folder,parent,mount,rel in rows]


class MapQuery:
    def __init__(self, path):
        self.path = Path(path)

    def query(self, action='volumes', **params):
        if action not in ('volumes','children','growth'):
            raise ValueError('unknown_storage_action')
        if not self.path.exists():
            return dict(schema_version=1,shape='storage',action=action,status='unobserved',coverage={},rows=[],truncated=False)
        limit = min(2000,max(1,int(params.get('limit',200))))
        if int(params.get('offset',0))!=0:raise ValueError('use_storage_cursor')
        cursor=_cursor(params.get('cursor'))
        db = sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=2)
        db.row_factory = sqlite3.Row
        try:
            if db.execute('PRAGMA user_version').fetchone()[0]!=2:
                raise ValueError('scanner_format_requires_fresh_store')
            db.execute('BEGIN')
            volumes = list(db.execute('SELECT * FROM volumes ORDER BY label LIMIT 2001'))
            chosen = params.get('volume_id') or (volumes[0]['id'] if volumes else None)
            volume = next((v for v in volumes if v['id']==chosen),None)
            rows=[];truncated=False;next_cursor=None;hierarchy=None
            if action == 'volumes':
                if cursor:raise ValueError('incompatible_storage_cursor')
                for v in volumes[:limit+1]:
                    root = db.execute('SELECT listed,uses FROM nodes WHERE generation=? AND volume=? AND parent IS NULL LIMIT 1',
                                      (v['generation'],v['key'])).fetchone()
                    listed,uses=(root[0],root[1]) if root else (None,None)
                    valid = bool(v['valid'])
                    rows.append(dict(id=v['id'],label=v['label'],observed_ns=v['observed_ns'],full_scan_ns=v['full_ns'],
                                     listed=listed,uses=uses if valid else None,volume_used=v['volume_used'],
                                     not_attributed=v['volume_used']-uses if valid and uses is not None else None,
                                     capacity=v['capacity'],free=v['free'],available=v['available'],valid=valid,status=v['status'],
                                     mounted=bool(v['mounted']),gaps=json.loads(v['gaps'])))
                truncated=len(rows)>limit
            elif volume and action == 'children':
                parent = params.get('parent_id')
                if parent is None:
                    root = db.execute('SELECT id FROM nodes WHERE generation=? AND volume=? AND parent IS NULL LIMIT 1',
                                      (volume['generation'],volume['key'])).fetchone()
                    parent = source_id(root[0]) if root else None
                metric='uses' if volume['valid'] else 'listed'
                scope=_scope([action,volume['generation'],volume['observed_ns'],chosen,parent,metric,'size-desc-id-desc-v2'])
                if cursor and cursor['scope']!=scope:raise ValueError('stale_or_incompatible_storage_cursor')
                seek='';values=[]
                if cursor:
                    if set(cursor)!={'size','id','scope'}:raise ValueError('invalid_children_cursor')
                    seek=f' AND ({metric},id)<(?,?)';values=[cursor['size'],inode_key(cursor['id'])]
                for n in db.execute(f'SELECT * FROM nodes WHERE generation=? AND volume=? AND parent IS ?{seek} ORDER BY {metric} DESC,id DESC LIMIT ?',
                                    (volume['generation'],volume['key'],inode_key(parent),*values,limit+1)):
                    rows.append(dict(id=source_id(n['id']),parent_id=source_id(n['parent']),label=n['label'],kind=n['kind'],listed=n['listed'],
                                     uses=n['uses'] if volume['valid'] else None,status=n['status'],observed_ns=volume['observed_ns'],_size=n[metric] or 0))
                seek=' AND (allocated,id)<(?,?)' if cursor else ''
                for f in db.execute(f'SELECT * FROM files WHERE generation=? AND volume=? AND parent IS ?{seek} ORDER BY allocated DESC,id DESC LIMIT ?',
                                    (volume['generation'],volume['key'],inode_key(parent),*values,limit+1)):
                    rows.append(dict(id=source_id(f['id']),parent_id=source_id(f['parent']),label=f['label'],kind='file',listed=f['allocated'],
                                     uses=None,status='observed',observed_ns=volume['observed_ns'],_size=f['allocated']))
                def order(row):
                    key=inode_key(row['id'])
                    return row['_size'],isinstance(key,str),key
                rows.sort(key=order,reverse=True)
                truncated=len(rows)>limit
                rows=rows[:limit]
                if truncated and rows:next_cursor=_encode_cursor({'size':rows[-1]['_size'],'id':rows[-1]['id'],'scope':scope})
                for row in rows:row.pop('_size')
            elif volume:
                start = int(params.get('start_ns',float(params.get('start',0))*1e9))
                end = int(params.get('end_ns',float(params.get('end',time.time()))*1e9))
                if end < start:raise ValueError('invalid_interval')
                hierarchy=db.execute('''SELECT observed_ns,version FROM hierarchy_epochs
                    WHERE volume=? AND observed_ns<=? ORDER BY observed_ns DESC LIMIT 1''',(chosen,end)).fetchone()
                scope=_scope([action,volume['generation'],volume['observed_ns'],chosen,start,end,
                              tuple(hierarchy) if hierarchy else None,'original-totals-with-overlap','identity-v1'])
                if cursor and cursor['scope']!=scope:raise ValueError('stale_or_incompatible_storage_cursor')
                candidates=list(db.execute('SELECT id FROM tracked WHERE volume=? AND id>? ORDER BY id LIMIT ?',
                                           (chosen,cursor.get('id',''),limit+1)))
                truncated=len(candidates)>limit;candidates=candidates[:limit]
                if truncated and candidates:next_cursor=_encode_cursor({'id':candidates[-1]['id'],'scope':scope})
                for candidate in candidates:
                    identity=candidate['id']
                    a = db.execute('SELECT * FROM points WHERE volume=? AND id=? AND observed_ns<=? ORDER BY observed_ns DESC LIMIT 1',(chosen,identity,start)).fetchone()
                    b = db.execute('SELECT * FROM points WHERE volume=? AND id=? AND observed_ns<=? ORDER BY observed_ns DESC LIMIT 1',(chosen,identity,end)).fetchone()
                    comparable = a and b and a['metric']==b['metric'] and a['size'] is not None and b['size'] is not None
                    ancestry=db.execute('''SELECT n.parent,n.present,p.label AS parent_label,p.present AS parent_present
                        FROM hierarchy_nodes n LEFT JOIN hierarchy_nodes p
                          ON p.volume=n.volume AND p.version=n.version AND p.id=n.parent
                        WHERE n.volume=? AND n.version=? AND n.id=?''',
                        (chosen,hierarchy['version'],identity)).fetchone() if hierarchy else None
                    parent_id=parent_label=None;hierarchy_status='unobserved'
                    if ancestry:
                        hierarchy_status='not_observed'
                        if ancestry['present']:
                            parent_id=ancestry['parent']
                            parent_label=ancestry['parent_label'] if ancestry['parent_present'] else None
                            hierarchy_status='parent_unobserved' if parent_id is not None and not ancestry['parent_present'] else 'observed'
                    rows.append(dict(id=identity,parent_id=parent_id,parent_label=parent_label,
                        hierarchy_observed_ns=hierarchy['observed_ns'] if hierarchy else None,hierarchy_status=hierarchy_status,
                        label=b['label'] if b else None,metric=b['metric'] if b else None,
                        start_metric=a['metric'] if a else None,end_metric=b['metric'] if b else None,
                        start_ns=a['observed_ns'] if a else None,end_ns=b['observed_ns'] if b else None,
                        start_bytes=a['size'] if a else None,end_bytes=b['size'] if b else None,
                        change=b['size']-a['size'] if comparable else None,status='observed' if comparable else 'unobserved'))
                rows.sort(key=lambda r:abs(r['change'] or 0),reverse=True)
            # Configured exclusions are reported by count only; their paths stay in config.
            excluded=db.execute("SELECT COUNT(*) FROM nodes WHERE generation=? AND volume=? AND status='excluded'",
                                (volume['generation'],volume['key'])).fetchone()[0] if volume else 0
            gaps=json.loads(volume['gaps']) if volume else []
            if excluded:gaps=sorted(set(gaps)|{'excluded'})
            coverage={'observed_ns':volume['observed_ns'] if volume else None,
                      'gaps':gaps,'excluded_roots':excluded,
                      'candidate_limit':limit,'growth_grouping':'original_totals_with_overlap' if action=='growth' else None,
                      'global_growth_complete':False}
            if action=='growth':
                coverage.update(nested_totals_overlap=True,hierarchy_observed_ns=hierarchy['observed_ns'] if hierarchy else None,
                                hierarchy_status='observed' if hierarchy else 'unobserved')
            return dict(schema_version=1,shape='storage',action=action,status=volume['status'] if volume else 'unobserved',coverage=coverage,
                        rows=rows[:limit],truncated=truncated,next_cursor=next_cursor)
        finally:db.close()
