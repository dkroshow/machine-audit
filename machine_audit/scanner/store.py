"""Private, compact scanner maps with staged atomic publication."""
from __future__ import annotations
from contextlib import contextmanager
from collections import defaultdict, deque
from itertools import groupby, islice
from array import array
import base64
import hashlib
import json
import os
from pathlib import Path
import resource
import re
import sqlite3
import sys
import time
import uuid

from machine_audit.budget import Budget, BudgetExceeded
from . import accounting
from .accounting import reconcile
from .encoding import inode_key, source_id
from .hierarchy import save_hierarchy
from .errors import ScanError

SCHEMA = '''
CREATE TABLE IF NOT EXISTS map_meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL) WITHOUT ROWID;
INSERT OR IGNORE INTO map_meta VALUES('next_generation',1);
CREATE TABLE IF NOT EXISTS generations(id INTEGER PRIMARY KEY,volume INTEGER,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS volumes(
 key INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,label TEXT NOT NULL,mount TEXT NOT NULL,generation INTEGER,
 observed_ns INTEGER,full_ns INTEGER,volume_used INTEGER,capacity INTEGER,free INTEGER,available INTEGER,
 valid INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,lean INTEGER NOT NULL,bookmark INTEGER,
 no_progress INTEGER NOT NULL DEFAULT 0,event_uuid TEXT,gaps TEXT NOT NULL DEFAULT '[]',mounted INTEGER NOT NULL DEFAULT 1,full_attempt_ns INTEGER,budget_retry_bytes INTEGER,delta_generation INTEGER,
 used_change INTEGER,unplaced_shared INTEGER);
CREATE TABLE IF NOT EXISTS nodes(
 generation INTEGER NOT NULL,volume INTEGER NOT NULL,id INTEGER NOT NULL,parent INTEGER,label TEXT,kind TEXT NOT NULL,
 own_listed INTEGER NOT NULL,own_physical INTEGER NOT NULL,listed INTEGER,physical INTEGER,status TEXT NOT NULL,mtime INTEGER,
 frees INTEGER,PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS node_parent ON nodes(generation,volume,parent,label);
CREATE INDEX IF NOT EXISTS node_size ON nodes(generation,volume,parent,physical DESC,id DESC);
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
-- A stream's sharing group: streams that share any block. `unplaced` is what an
-- update could not place: bytes APFS says are shared, with a partner it has not read.
CREATE TABLE IF NOT EXISTS streams(
 generation INTEGER NOT NULL,volume INTEGER NOT NULL,stream INTEGER NOT NULL,grp INTEGER NOT NULL,
 unplaced INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(generation,volume,stream)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS stream_group ON streams(generation,volume,grp);
CREATE INDEX IF NOT EXISTS stream_unplaced ON streams(generation,volume) WHERE unplaced>0;
-- Per-folder byte terms of one group. Summed over a subtree they give its physical
-- space and what deleting it frees. Byte counts only, never block addresses.
CREATE TABLE IF NOT EXISTS terms(
 generation INTEGER NOT NULL,volume INTEGER NOT NULL,grp INTEGER NOT NULL,dir INTEGER NOT NULL,
 physical INTEGER NOT NULL,frees INTEGER NOT NULL,PRIMARY KEY(generation,volume,grp,dir)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS term_dir ON terms(generation,volume,dir);
CREATE TABLE IF NOT EXISTS listings(
 generation INTEGER NOT NULL,volume INTEGER NOT NULL,id INTEGER NOT NULL,listed_ns INTEGER,count INTEGER,
 PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS removed(generation INTEGER,volume INTEGER,id INTEGER,PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS reset_dirs(generation INTEGER,volume INTEGER,id INTEGER,PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS removed_streams(generation INTEGER,volume INTEGER,id INTEGER,PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS reset_groups(generation INTEGER,volume INTEGER,id INTEGER,PRIMARY KEY(generation,volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS tracked(volume TEXT,id TEXT,parent TEXT,PRIMARY KEY(volume,id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS points(
 volume TEXT NOT NULL,id TEXT NOT NULL,observed_ns INTEGER NOT NULL,label TEXT,metric TEXT NOT NULL,
 size INTEGER,status TEXT NOT NULL,PRIMARY KEY(volume,id,observed_ns)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hierarchy_epochs(
 volume TEXT NOT NULL,observed_ns INTEGER NOT NULL,version TEXT NOT NULL,PRIMARY KEY(volume,observed_ns)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hierarchy_nodes(
 volume TEXT NOT NULL,version TEXT NOT NULL,id TEXT NOT NULL,parent TEXT,label TEXT,present INTEGER NOT NULL,
 rank INTEGER NOT NULL,first_rank INTEGER NOT NULL,PRIMARY KEY(volume,version,rank),UNIQUE(volume,version,id)) WITHOUT ROWID;
-- recursive NULL is a deferral recorded before the flag existed: treated as recursive.
CREATE TABLE IF NOT EXISTS dirty(volume TEXT,id TEXT,due_ns INTEGER,recursive INTEGER,PRIMARY KEY(volume,id)) WITHOUT ROWID;
-- The last time a volume's root was refused, kept apart from its published map's times.
CREATE TABLE IF NOT EXISTS denied(volume TEXT PRIMARY KEY,observed_ns INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY,started_ns INTEGER NOT NULL,updated_ns INTEGER NOT NULL,ended_ns INTEGER,
 pid INTEGER NOT NULL,pid_start TEXT,status TEXT NOT NULL,entries INTEGER NOT NULL DEFAULT 0,cpu_ns INTEGER NOT NULL,
 peak_rss_bytes INTEGER NOT NULL,written_bytes INTEGER,kind TEXT NOT NULL,
 volume_id TEXT,action TEXT,trigger TEXT,stage TEXT,volumes TEXT) WITHOUT ROWID;
'''
MAP_TABLES=('nodes','files','holders','streams','terms','listings')
STAGE_TABLES=('removed','reset_dirs','removed_streams','reset_groups')
MAX_ANCESTRY=1024
KEYS={'nodes':'id','files':'parent','holders':'dir','listings':'id','streams':'stream','terms':'grp'}
PRIMARY={'nodes':('id',),'files':('parent','id'),'holders':('dir','file_id'),'listings':('id',),'streams':('stream',),'terms':('grp','dir')}
# A staged marker that hides a key's rows in the base and the delta alike...
REMOVED={'nodes':'removed','files':'removed','holders':'removed','listings':'removed','streams':'removed_streams','terms':None}
# ...and one that hides only the base's rows, which the delta restates in full.
RESET={'files':'reset_dirs','holders':'reset_dirs','terms':'reset_groups'}
MERGE_ROWS=4096
NODE_INSERT='INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)'
# Re-read at most this many streams' extents in one hourly update.
SHARING_REREADS=20_000
# Format 3 adds a published delta. Format 4 measures physical space; its
# folder figures mean something else, so a format 3 reader must refuse it.
FORMAT=4
OPENS=(2,3,4)


def published(table,alias,base,volume,delta):
    """SQL condition: row `alias` of `table` is in its volume's published map, base
    generation `base` overlaid by published delta generation `delta` (NULL for none).
    These are exactly the rows a merge leaves in the base: delta rows not removed, and
    base rows that no delta row replaces, not removed and not reset (files and holders
    under a re-listed directory, terms of a recomputed group). The arguments are SQL
    expressions."""
    key=KEYS[table]
    def marked(marker):
        return f'EXISTS(SELECT 1 FROM {marker} m WHERE m.generation={delta} AND m.volume={volume} AND m.id={alias}.{key})'
    markers=[marked(m) for m in (REMOVED[table],RESET.get(table)) if m]
    removed=f'NOT {marked(REMOVED[table])}' if REMOVED[table] else '1'
    same=' AND '.join(f'o.{c}={alias}.{c}' for c in PRIMARY[table])
    return (f'(({alias}.generation={delta} AND {removed}) OR ({alias}.generation={base} AND NOT ({" OR ".join(markers)})'
            f' AND NOT EXISTS(SELECT 1 FROM {table} o WHERE o.generation={delta} AND o.volume={volume} AND {same})))')


class MergePending(RuntimeError):
    """The update is published, but merging its delta into the base did not finish.
    The delta stays published; the next run merges it before any scan."""
    def __init__(self,cause):
        code=(f'blocked_{cause.code}' if isinstance(cause,BudgetExceeded) else
              getattr(cause,'sqlite_errorname','sqlite_error').lower() if isinstance(cause,sqlite3.Error) else type(cause).__name__)
        super().__init__(code);self.code=code


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
                if existing and self.db.execute('PRAGMA user_version').fetchone()[0] not in OPENS:
                    raise ValueError('scanner_format_requires_fresh_store')
                os.chmod(self.path,0o600)
                self.db.execute('PRAGMA journal_mode=WAL')
                self.db.execute('PRAGMA temp_store=MEMORY')
                self.db.execute('PRAGMA busy_timeout=5000')
                self.db.create_function('source_id',1,source_id,deterministic=True)
                self.db.create_function('inode_key',1,inode_key,deterministic=True)
            objects={r[0] for r in self.db.execute('SELECT name FROM sqlite_master')}
            missing=[(kind,name,table) for kind,name,table in re.findall(
                r'CREATE (TABLE|INDEX) IF NOT EXISTS (\w+)(?: ON (\w+))?',SCHEMA) if name not in objects]
            metadata=not existing or not self.db.execute(
                "SELECT 1 FROM map_meta WHERE key='next_generation'").fetchone()
            if missing or metadata:
                # Existing installs do no schema writes. A missing index counts
                # the rows it will index; empty-schema metadata has fixed size.
                rows=2*len(SCHEMA.split(';'));payload=len(SCHEMA.encode())
                for kind,_,table in missing:
                    if kind=='INDEX' and table in objects:
                        count,size=self._row_size(table)
                        rows+=count;payload+=size
                with self.write(rows=rows,payload_bytes=payload):
                    for statement in SCHEMA.split(';'):
                        if statement.strip():self.db.execute(statement)
                    if not existing:self.db.execute(f'PRAGMA user_version={FORMAT}')
            # Nullable diagnostic columns extend format 2 without rewriting map
            # rows or changing old observations. Each ALTER is metadata only.
            columns={r['name'] for r in self.db.execute('PRAGMA table_info(runs)')}
            missing_columns={'volume_id','action','trigger','stage','volumes'}-columns
            if missing_columns:
                with self.write(rows=len(missing_columns)):
                    for column in sorted(missing_columns):
                        self.db.execute(f'ALTER TABLE runs ADD COLUMN {column} TEXT')
            for table,column in (('volumes','full_attempt_ns'),('volumes','budget_retry_bytes'),('dirty','recursive')):
                if column not in {r['name'] for r in self.db.execute(f'PRAGMA table_info({table})')}:
                    with self.write(rows=1):
                        self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} INTEGER')
            # Format 3 is the delta column: both commit together, or neither does.
            if 'delta_generation' not in {r['name'] for r in self.db.execute('PRAGMA table_info(volumes)')}:
                with self.write(rows=2):
                    self.db.execute('ALTER TABLE volumes ADD COLUMN delta_generation INTEGER')
                    self.db.execute('PRAGMA user_version=3')
            if self.db.execute('PRAGMA user_version').fetchone()[0]<FORMAT:self._physical_format()
        except BaseException:
            if self.db is not None:self.db.close()
            raise
        self.buffer=[];self.buffer_bytes=0;self.pending_resets=set()
        self.generation=self.volume=self.vid=self.base_gen=self.run=None
        self.changed=set();self.last_progress=0.0;self.entries=0
        self.run_context=dict(volume_id=None,action=None,trigger=None,stage=None)

    def _physical_format(self):
        """Format 3 to 4, in one transaction. The old folder figures charged shared
        blocks to one folder; they cannot be converted, so every map is retired and
        rebuilt by the next full walk, pruned by it like any retired map. Points,
        tracked folders, hierarchy and runs stay. Statements are metadata, two table
        drops and two small updates."""
        volumes=self.db.execute('SELECT count(*) FROM volumes').fetchone()[0]
        generations=self.db.execute('SELECT count(*) FROM generations').fetchone()[0]
        def columns(table):return {r['name'] for r in self.db.execute(f'PRAGMA table_info({table})')}
        with self.write(rows=16+volumes+generations):
            if 'own_uses' in columns('nodes'):
                self.db.execute('ALTER TABLE nodes RENAME COLUMN own_uses TO own_physical')
                self.db.execute('ALTER TABLE nodes RENAME COLUMN uses TO physical')
            if 'frees' not in columns('nodes'):self.db.execute('ALTER TABLE nodes ADD COLUMN frees INTEGER')
            for column in ('used_change','unplaced_shared'):
                if column not in columns('volumes'):self.db.execute(f'ALTER TABLE volumes ADD COLUMN {column} INTEGER')
            self.db.execute('DROP TABLE IF EXISTS charges')
            self.db.execute('DROP TABLE IF EXISTS removed_charges')
            self.db.execute("UPDATE generations SET status='retired' WHERE status IN ('published','delta')")
            self.db.execute('''UPDATE volumes SET generation=NULL,delta_generation=NULL,valid=0,full_ns=NULL,
                status='awaiting_physical_walk' ''')
            self.db.execute(f'PRAGMA user_version={FORMAT}')

    @contextmanager
    def write(self,growth=None,*,rows=None,payload_bytes=0):
        with self.budget.sqlite_write(self.db,self.path,max_growth_bytes=growth,rows=rows,payload_bytes=payload_bytes) as admitted:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException as exc:
                if self.db.in_transaction:self.db.execute('ROLLBACK')
                # The transaction ran out of the page limit the cap had shortened:
                # storage pressure, not a failed write. How much it lacked is
                # unknown, so the refusal carries no retry level. Other errors
                # stay as raised.
                if (getattr(exc,'sqlite_errorcode',None)==sqlite3.SQLITE_FULL and admitted['budget_limited']
                        and self.db.execute('PRAGMA max_page_count').fetchone()[0]==admitted['max_pages']):
                    raise BudgetExceeded('sqlite_page_limit') from None
                raise

    def _row_size(self,table,where='1',args=()):
        """Count rows and variable payload before their bounded mutation."""
        columns=[r['name'] for r in self.db.execute(f'PRAGMA table_info({table})')]
        size='+'.join(f"CASE WHEN typeof({c}) IN ('text','blob') THEN length(CAST({c} AS BLOB)) ELSE 0 END" for c in columns)
        row=self.db.execute(f'SELECT count(*),coalesce(sum({size}),0) FROM {table} WHERE {where}',args).fetchone()
        return tuple(row)

    def start(self,kind):
        self.run=uuid.uuid4().hex;now=time.time_ns()
        with self.write(rows=self.db.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0]+1):
            self.db.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
            self.db.execute('''INSERT INTO runs(id,started_ns,updated_ns,ended_ns,pid,pid_start,status,
                entries,cpu_ns,peak_rss_bytes,written_bytes,kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (self.run,now,now,None,os.getpid(),getattr(self,'process_start',None),'running',0,time.process_time_ns(),0,None,kind))
        return self.run

    def phase(self,stage,*,volume_id=None,action=None,trigger=None):
        self.run_context.update(stage=stage)
        for key,value in (('volume_id',volume_id),('action',action),('trigger',trigger)):
            if value is not None:self.run_context[key]=value
        self.progress(force=True)

    def progress(self,entries=None,status='running',*,force=False):
        if not self.run:return
        if entries is not None:self.entries=max(self.entries,entries)
        now=time.monotonic()
        if status=='running' and not force and now-self.last_progress<5:return
        self.last_progress=now
        r=resource.getrusage(resource.RUSAGE_SELF)
        try:written=self.written_bytes() if hasattr(self,'written_bytes') else None
        except OSError:written=None
        context=tuple(self.run_context[key] for key in ('volume_id','action','trigger','stage'))
        with self.write(rows=1,payload_bytes=sum(len(v.encode()) for v in context if v is not None)):
            self.db.execute('''UPDATE runs SET updated_ns=?,entries=?,cpu_ns=?,peak_rss_bytes=?,written_bytes=?,status=?,ended_ns=?,
                volume_id=?,action=?,trigger=?,stage=? WHERE id=?''',
                (time.time_ns(),self.entries,time.process_time_ns(),int(r.ru_maxrss*(1 if sys.platform=='darwin' else 1024)),
                 written,status,
                 None if status=='running' else time.time_ns(),*context,self.run))

    def record_volumes(self,results):
        """Each volume's outcome codes for this run (status, action, trigger, gaps,
        reason, entries): the scheduled scanner keeps no output, so the run row is
        the only place a per-volume failure can be read back."""
        data=json.dumps(results,separators=(',',':'))
        with self.write(rows=1,payload_bytes=len(data)):
            self.db.execute('UPDATE runs SET volumes=? WHERE id=?',(data,self.run))

    def prepare(self,volume,full=True,lean=True):
        self.flush()
        pending=self.db.execute('SELECT delta_generation FROM volumes WHERE id=?',(volume.id,)).fetchone()
        if pending and pending[0] is not None:raise ValueError('merge_pending')
        # This attempt owns any failure from here on, including a refused prune,
        # and a full attempt is recorded before the cleanup that can refuse it.
        self.volume,self.generation,self.base_gen=volume.id,None,None
        with self.write(rows=2 if full else 1):
            self.db.execute('INSERT OR IGNORE INTO volumes(id,label,mount,status,lean) VALUES(?,?,?,?,?)',
                            (volume.id,volume.label,volume.mount,'building',int(lean)))
            if full:
                self.db.execute('UPDATE volumes SET full_attempt_ns=? WHERE id=?',(time.time_ns(),volume.id))
        self.prune_generations(full)
        with self.write(rows=2):
            old=self.db.execute('SELECT * FROM volumes WHERE id=?',(volume.id,)).fetchone()
            self.generation=self.db.execute("SELECT value FROM map_meta WHERE key='next_generation'").fetchone()[0]
            self.db.execute("UPDATE map_meta SET value=value+1 WHERE key='next_generation'")
            self.db.execute("INSERT INTO generations VALUES(?,?,'building')",(self.generation,old['key']))
        self.vid,self.mount=old['key'],volume.mount
        self.base_gen=old['generation'] if not full else None
        self.changed=set()
        self.pending_resets.clear()
        return old

    def prune_generations(self,full=True):
        obsolete=[r[0] for r in self.db.execute("SELECT id FROM generations WHERE status='building' OR (? AND status='retired')",(full,))]
        for generation in obsolete:
            for table in MAP_TABLES+STAGE_TABLES:
                columns=sorted(self.db.execute(f'PRAGMA table_info({table})'),key=lambda r:r['pk'])
                key=','.join(r['name'] for r in columns if r['pk'])
                # Cache spilling stays disabled. Bound deletions per commit,
                # retaining the generation record so interrupted cleanup resumes.
                # Each batch declares its actual rows: a small leftover must not
                # reserve a full batch.
                while True:
                    batch=self.db.execute(f'SELECT count(*) FROM (SELECT 1 FROM {table} WHERE generation=? LIMIT 4096)',
                                          (generation,)).fetchone()[0]
                    if not batch:break
                    with self.write(rows=batch):
                        self.db.execute(f'''DELETE FROM {table} WHERE ({key}) IN
                            (SELECT {key} FROM {table} WHERE generation=? ORDER BY {key} LIMIT 4096)''',(generation,))
                    if batch<4096:break
            with self.write(rows=1):
                self.db.execute('DELETE FROM generations WHERE id=?',(generation,))

    def observe_unscanned(self,volume,reason,lean=True):
        with self.write(rows=1):
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
        if self.base_gen is None:
            grouped=defaultdict(list)
            for query,args in self.buffer:grouped[query].append(args)
            groups=grouped.items()
        else:
            # Reset/delete operations must stay before that directory's inserts.
            groups=((query,[args for _,args in batch]) for query,batch in groupby(self.buffer,key=lambda item:item[0]))
        with self.write(rows=len(self.buffer),payload_bytes=self.buffer_bytes):
            for query,rows in groups:self.db.executemany(query,rows)
        self.buffer.clear();self.buffer_bytes=0;self.pending_resets.clear()

    def listed(self,identity,count):
        self._queue('INSERT OR REPLACE INTO listings VALUES(?,?,?,?,?)',
                    (self.generation,self.vid,inode_key(identity),time.time_ns(),count))

    def defer(self,identity,due_ns,recursive):
        # A pending refresh keeps its earliest due time and is never narrowed
        # by a later shallow event.
        with self.write(rows=1):self.db.execute('''INSERT INTO dirty VALUES(?,?,?,?) ON CONFLICT(volume,id) DO UPDATE SET
            due_ns=min(due_ns,excluded.due_ns),recursive=max(coalesce(recursive,1),excluded.recursive)''',
            (self.volume,identity,due_ns,int(recursive)))

    def directory(self,node):
        identity=inode_key(node.id)
        if self.base_gen is not None:
            self.changed.add(identity)
            self._queue('DELETE FROM removed WHERE generation=? AND volume=? AND id=?',(self.generation,self.vid,identity))
        self._queue(NODE_INSERT,(self.generation,self.vid,identity,inode_key(node.parent_id),node.label,node.kind,
                                 node.listed,node.physical,None,None,node.status,node.mtime_ns,None))

    def file(self,identity,parent,label,allocated,mtime):
        self._queue('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)',
                    (self.generation,self.vid,inode_key(parent),inode_key(identity),label,allocated,mtime))

    def holder(self,identity,directory,stream,allocated,private,partial):
        self._queue('INSERT OR REPLACE INTO holders VALUES(?,?,?,?,?,?,?,?)',
                    (self.generation,self.vid,inode_key(directory),inode_key(identity),inode_key(stream),allocated,private,int(partial)))

    def _rows(self,table,where='1',args=()):
        """Only the scanner sees staged rows. Public queries read published rows."""
        identity=KEYS[table]
        # SQLite otherwise prefers the volume-wide primary-key prefix for
        # these SELECT * lookups, scanning every row per parent/stream/file.
        index={('holders','stream=?'):'holder_stream',('holders','file_id=?'):'holder_file',
               ('streams','grp=?'):'stream_group',('terms','dir=?'):'term_dir'}.get((table,where))
        if table=='nodes' and where in ('parent IS NULL','parent=?','parent=? AND label=?'):
            index='node_parent'
        hint=f' INDEXED BY {index}' if index else ''
        staged=f'SELECT * FROM {table}{hint} WHERE generation=? AND volume=? AND {where}'
        params=[self.generation,self.vid,*args]
        if table in ('nodes','listings'):
            staged+=f' AND NOT EXISTS (SELECT 1 FROM removed r WHERE r.generation=? AND r.volume=? AND r.{"id"}={table}.{identity})'
            params.extend((self.generation,self.vid))
        if self.base_gen is None:return self.db.execute(staged,params)
        markers=[m for m in (REMOVED[table],RESET.get(table)) if m]
        if table not in RESET:
            # A staged row replaces its base row.
            hidden=[f'NOT EXISTS(SELECT 1 FROM {table} s WHERE s.generation=? AND s.volume=? AND s.{identity}=b.{identity})']
        else:hidden=[]
        hidden+=[f'NOT EXISTS(SELECT 1 FROM {m} s WHERE s.generation=? AND s.volume=? AND s.id=b.{identity})' for m in markers]
        extra=[self.generation,self.vid]*len(hidden)
        return self.db.execute(staged+f' UNION ALL SELECT * FROM {table} b{hint} WHERE generation=? AND volume=? AND {where} AND '+' AND '.join(hidden),
                               params+[self.base_gen,self.vid,*args,*extra])

    def _node(self,identity):
        return self._rows('nodes','id=?',(identity,)).fetchone()

    def path_for(self,identity):
        parts=[];seen=set()
        for _ in range(MAX_ANCESTRY):
            if identity in seen:raise ScanError('cyclic_directory_ancestry')
            seen.add(identity);node=self._node(identity)
            if node is None:raise ScanError('missing_directory_ancestor')
            if node['parent'] is None:return os.path.join(self.mount,*reversed(parts))
            if node['label'] is None:return None
            parts.append(node['label']);identity=node['parent']
        raise ScanError('directory_ancestry_limit')

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
        if len(parts)>MAX_ANCESTRY:raise ScanError('directory_ancestry_limit')
        for name in parts:
            rows=list(self._rows('nodes','parent=? AND label=?',(identity,name)))
            if len(rows)!=1:return None
            identity=rows[0]['id']
        return self.known_directory(source_id(identity))

    def listing(self,identity):
        self.flush();return self._rows('listings','id=?',(inode_key(identity),)).fetchone()

    def begin_directory(self,identity):
        if self.base_gen is None:return
        identity=inode_key(identity)
        repeated=identity in self.pending_resets or self.db.execute(
            'SELECT 1 FROM reset_dirs WHERE generation=? AND volume=? AND id=?',
            (self.generation,self.vid,identity)).fetchone()
        if repeated:
            self.flush()
            for table,parent,key in (('files','parent','id'),('holders','dir','file_id')):
                while True:
                    keys=[r[0] for r in self.db.execute(f'''SELECT {key} FROM {table}
                        WHERE generation=? AND volume=? AND {parent}=? LIMIT 4096''',(self.generation,self.vid,identity))]
                    if not keys:break
                    with self.write(rows=len(keys)):
                        self.db.executemany(f'DELETE FROM {table} WHERE generation=? AND volume=? AND {parent}=? AND {key}=?',
                            ((self.generation,self.vid,identity,key) for key in keys))
        self.pending_resets.add(identity)
        self._queue('INSERT OR IGNORE INTO reset_dirs VALUES(?,?,?)',(self.generation,self.vid,identity))

    def _subtree(self,identity):
        pending=[identity];seen=set()
        while pending:
            current=pending.pop()
            if current in seen:raise ScanError('cyclic_directory_ancestry')
            seen.add(current)
            pending.extend(row['id'] for row in self._rows('nodes','parent=?',(current,)))
        return seen

    def relocate(self,identity,parent,label,path,private):
        key=inode_key(identity);old=self._node(key)
        if old is None:return False
        # A shallow parent listing cannot observe whether a child's private
        # marker disappeared. Only a listing of that child can expose names.
        was_private=old['kind']=='private'
        if was_private and old['parent']==inode_key(parent) and old['label'] in (None,label):return True
        private=private or was_private
        if old['parent']==inode_key(parent) and old['label']==(None if private else label) and (old['kind']=='private')==private:
            return True
        changed=self._subtree(key) if private and not was_private else {key}
        for current in changed:
            node=dict(self._node(current))
            node.update(generation=self.generation)
            if current==key:node.update(parent=inode_key(parent),label=None if private else label)
            if private and not was_private:
                node.update(label=None,kind='private')
                files=list(self._rows('files','parent=?',(current,)))
                holders=list(self._rows('holders','dir=?',(current,)))
                self.begin_directory(source_id(current))
                for f in files:self.file(source_id(f['id']),source_id(current),None,f['allocated'],f['mtime'])
                for h in holders:self.holder(source_id(h['file_id']),source_id(current),source_id(h['stream']),h['allocated'],h['private'],h['partial'])
            self._queue(NODE_INSERT,tuple(node.values()))
            self.changed.add(current)
        return True

    def forget_children(self,parent,present):
        self.flush();present={inode_key(x) for x in present}
        for row in list(self._rows('nodes','parent=?',(inode_key(parent),))):
            if row['id'] not in present:self.clear_subtree(source_id(row['id']))

    def clear_subtree(self,identity):
        self.flush();keys=self._subtree(inode_key(identity))
        pending=iter(keys)
        while batch:=list(islice(pending,4096)):
            with self.write(rows=len(batch)):
                self.db.executemany('INSERT OR IGNORE INTO removed VALUES(?,?,?)',((self.generation,self.vid,k) for k in batch))
        self.changed.update(keys)

    def _old_node(self,key):
        if self.base_gen is None:return None
        return self.db.execute('SELECT * FROM nodes WHERE generation=? AND volume=? AND id=?',(self.base_gen,self.vid,key)).fetchone()

    def _sharing(self,reader,full):
        """Stage the sharing groups and per-folder terms of the streams this run touched.

        A full walk takes every stream. An update takes the streams of re-listed or
        removed folders, plus every stream
        in their groups, so a changed copy is measured against the unchanged copies
        it shares blocks with. `reader` gives a file's extents by inode, or is None
        where none can be read (each stream then counts once, by its allocation).
        Returns (folders whose totals an update changes, gap codes)."""
        gaps=set();known={};old_groups=set()
        if full:
            rows=self.db.execute('''SELECT stream,dir,file_id,allocated,private FROM holders INDEXED BY holder_stream
                WHERE generation=? AND volume=? ORDER BY stream''',(self.generation,self.vid))
            by_stream=((stream,list(found)) for stream,found in groupby(rows,key=lambda row:row[0]))
        else:
            touched={r[0] for r in self.db.execute('''SELECT stream FROM holders WHERE generation=? AND volume=? AND dir IN
                 (SELECT id FROM reset_dirs WHERE generation=? AND volume=? UNION SELECT id FROM removed WHERE generation=? AND volume=?)''',
                 (self.base_gen,self.vid,self.generation,self.vid,self.generation,self.vid))}
            touched.update(r[0] for r in self.db.execute('SELECT DISTINCT stream FROM holders WHERE generation=? AND volume=?',
                                                         (self.generation,self.vid)))
            for stream in touched:
                row=self._rows('streams','stream=?',(stream,)).fetchone()
                if row:known[stream]=row;old_groups.add(row['grp'])
            for group in old_groups:
                for row in self._rows('streams','grp=?',(group,)):
                    known[row['stream']]=row;touched.add(row['stream'])
            if reader and len(touched)>SHARING_REREADS:
                # Counted by stream until the next full walk: shared blocks between
                # different streams may count twice, and the run says so.
                reader=None;gaps.add('sharing_reread_capped')
            by_stream=((stream,[(stream,h['dir'],h['file_id'],h['allocated'],h['private'])
                                for h in self._rows('holders','stream=?',(stream,))]) for stream in touched)
        # stream -> (holding folders, APFS private bytes, held by one file)
        streams={}
        def read():
            for count,(stream,found) in enumerate(by_stream):
                if not found:continue
                files=tuple(dict.fromkeys(row[2] for row in found))
                streams[stream]=(tuple(dict.fromkeys(row[1] for row in found)),found[0][4],len(files)==1)
                ranges=None
                if reader:
                    for file_id in files:
                        try:ranges=reader(source_id(file_id));break
                        except OSError:continue
                    else:gaps.add('extents_unreadable')
                if count%1000==0:self.progress()
                yield stream,max(row[3] for row in found),ranges
        order,private,shared,group,overflowed=accounting.pieces(read())
        if overflowed:gaps.add('block_map_budget')
        unplaced={}
        if reader and not full:
            # A copy made since the last full walk can share blocks with a file the
            # update did not read. APFS says how much of a single file is private;
            # the rest is held, and counted, where its partner is.
            for i,stream in enumerate(order):
                _,apfs_private,single=streams[stream];previous=known.get(stream)
                if apfs_private is None or not single or (previous is not None and not previous['unplaced']):continue
                unknown=min(private[i]-apfs_private,private[i])
                if unknown>0:private[i]-=unknown;unplaced[stream]=unknown
        holding={stream:entry[0] for stream,entry in streams.items()}
        parents=self._parents({folder for folders in holding.values() for folder in folders})
        def parent_of(folder):
            if folder not in parents:raise ScanError('missing_directory_ancestor')
            return parents[folder]
        shapes={}
        grouped=defaultdict(list)
        for members,size in shared.items():
            grouped[group[next(iter(members))]].append((members,size))
        def groups():
            for i,stream in enumerate(order):
                key=group.get(stream)
                if key is None:yield stream,((stream,),private[i])
                elif private[i]:grouped[key].append(((stream,),private[i]))
            for key,found in grouped.items():yield key,found
        def staged(key,terms):
            for folder,(physical,frees) in terms.items():
                self._queue('INSERT OR REPLACE INTO terms VALUES(?,?,?,?,?,?)',(self.generation,self.vid,key,folder,physical,frees))
        affected=set()
        if full:
            # The folder totals read these terms back, summed by folder.
            for count,(key,found) in enumerate(groups()):
                staged(key,accounting.terms(found if isinstance(found,list) else (found,),holding,parent_of,shapes))
                if count%1000==0:self.progress()
        else:
            # Restage only groups whose terms changed, and total again only the
            # folders whose sum over the touched groups changed: a new copy in a
            # large family moves terms at its own folder and the branches above it.
            computed={key:accounting.terms(found if isinstance(found,list) else (found,),holding,parent_of,shapes)
                      for key,found in groups()}
            before=defaultdict(dict)
            for key in old_groups:
                for row in self.db.execute('SELECT dir,physical,frees FROM terms WHERE generation=? AND volume=? AND grp=?',
                                           (self.base_gen,self.vid,key)):
                    before[key][row[0]]=(row[1],row[2])
            totals=defaultdict(lambda:[0,0])
            for sign,side in ((-1,before),(1,computed)):
                for terms in side.values():
                    for folder,(physical,frees) in terms.items():
                        total=totals[folder];total[0]+=sign*physical;total[1]+=sign*frees
            affected={folder for folder,total in totals.items() if total!=[0,0]}
            changed={key for key,terms in computed.items() if before.get(key)!=terms}
            for key in old_groups-computed.keys()|changed:
                self._queue('INSERT OR IGNORE INTO reset_groups VALUES(?,?,?)',(self.generation,self.vid,key))
            for key in changed:staged(key,computed[key])
            for stream in touched-streams.keys():
                self._queue('INSERT OR IGNORE INTO removed_streams VALUES(?,?,?)',(self.generation,self.vid,stream))
        for stream in order:
            previous=known.get(stream);key=group.get(stream,stream)
            if previous is None or (previous['grp'],previous['unplaced'])!=(key,unplaced.get(stream,0)):
                self._queue('INSERT OR REPLACE INTO streams VALUES(?,?,?,?,?)',
                            (self.generation,self.vid,stream,key,unplaced.get(stream,0)))
        self.flush()
        return affected,gaps

    def _parents(self,folders):
        """Each folder's parent, and its ancestors' parents, in the map being built.
        A family of copies can span many thousands of folders, so they are read a
        level at a time in batches, not one lookup each."""
        found={};pending=set(folders)
        for _ in range(MAX_ANCESTRY+1):
            if not pending:return found
            batch=list(pending);pending=set()
            for first in range(0,len(batch),500):
                part=batch[first:first+500];marks=','.join('?'*len(part))
                rows={}
                for generation in (self.base_gen,self.generation):  # A staged row replaces its base row.
                    if generation is None:continue
                    rows.update(self.db.execute(f'SELECT id,parent FROM nodes WHERE generation=? AND volume=? AND id IN ({marks})',
                                                (generation,self.vid,*part)).fetchall())
                removed={r[0] for r in self.db.execute(f'SELECT id FROM removed WHERE generation=? AND volume=? AND id IN ({marks})',
                                                       (self.generation,self.vid,*part))}
                for folder in part:
                    if folder not in rows or folder in removed:raise ScanError('missing_directory_ancestor')
                    found[folder]=parent=rows[folder]
                    if parent is not None and parent not in found:pending.add(parent)
            pending-=found.keys()
        raise ScanError('directory_ancestry_limit')

    def _own(self,node):
        listed=node['own_listed'];physical=frees=node['own_physical']
        for term in self._rows('terms','dir=?',(node['id'],)):
            physical+=term['physical'];frees+=term['frees']
        return listed,physical,frees

    def _aggregate_full(self):
        # Files remain on disk. Only directory IDs and integer totals form the
        # temporary bottom-up working set; no recursive SQL or ancestor scans.
        keys=[];parents=[];listed=array('q');physical=array('q')
        for row in self.db.execute('SELECT id,parent,own_listed,own_physical FROM nodes WHERE generation=? AND volume=?',
                                   (self.generation,self.vid)):
            keys.append(row['id']);parents.append(row['parent'])
            listed.append(row['own_listed']);physical.append(row['own_physical'])
        index={key:i for i,key in enumerate(keys)}
        frees=array('q',physical)
        for folder,extra,freed in self.db.execute('''SELECT dir,sum(physical),sum(frees) FROM terms INDEXED BY term_dir
                WHERE generation=? AND volume=? GROUP BY dir''',(self.generation,self.vid)):
            if folder not in index:raise ScanError('missing_directory_ancestor')
            i=index[folder];physical[i]+=extra;frees[i]+=freed
        parent_index=array('q');remaining=array('I',[0])*len(keys)
        for parent in parents:
            if parent is not None and parent not in index:raise ScanError('missing_directory_ancestor')
            position=index[parent] if parent is not None else -1
            parent_index.append(position)
            if position>=0:remaining[position]+=1
        del parents,index
        ready=deque(i for i,count in enumerate(remaining) if not count);visited=0
        while ready:
            child=ready.popleft();visited+=1;parent=parent_index[child]
            if parent>=0:
                listed[parent]+=listed[child];physical[parent]+=physical[child];frees[parent]+=frees[child]
                remaining[parent]-=1
                if not remaining[parent]:ready.append(parent)
        if visited!=len(keys):raise ScanError('cyclic_directory_ancestry')
        self.progress()
        for first in range(0,len(keys),8192):
            with self.write(rows=min(8192,len(keys)-first)):
                self.db.executemany('UPDATE nodes SET listed=?,physical=?,frees=? WHERE generation=? AND volume=? AND id=?',
                    ((listed[i],physical[i],frees[i],self.generation,self.vid,keys[i]) for i in range(first,min(first+8192,len(keys)))))
            self.progress()

    def _aggregate(self,affected):
        work=set()
        for identity in affected:
            old=self._old_node(identity)
            seeds=[identity]+([old['parent']] if old and old['parent'] is not None else [])
            for seed in seeds:
                seen=set()
                for _ in range(MAX_ANCESTRY):
                    if seed is None:break
                    if seed in seen:raise ScanError('cyclic_directory_ancestry')
                    seen.add(seed);work.add(seed);node=self._node(seed)
                    if node is None:break
                    seed=node['parent']
                else:raise ScanError('directory_ancestry_limit')
        depths={}
        def depth(key):
            chain=[];seen=set();current=key
            while current is not None and current not in depths:
                if current in seen:raise ScanError('cyclic_directory_ancestry')
                if len(chain)>=MAX_ANCESTRY:raise ScanError('directory_ancestry_limit')
                seen.add(current);chain.append(current);node=self._node(current)
                if node is None and current!=key:raise ScanError('missing_directory_ancestor')
                current=node['parent'] if node else None
            value=depths.get(current,-1)
            for item in reversed(chain):value+=1;depths[item]=value
            return depths[key]
        for _,level in groupby(sorted(work,key=depth,reverse=True),key=depth):
            for identity in level:
                node=self._node(identity)
                if node is None:continue
                listed,physical,frees=self._own(node)
                for child in self._rows('nodes','parent=?',(identity,)):
                    listed+=child['listed'];physical+=child['physical'];frees+=child['frees']
                updated=dict(node);updated.update(generation=self.generation,listed=listed,physical=physical,frees=frees)
                self._queue(NODE_INSERT,tuple(updated.values()))
                self.changed.add(identity)
            # Parents must see the committed staged totals of their children.
            self.flush()

    def _publication_changes(self):
        """The publication's own map statement: the deferred refreshes its listings
        fulfil, counted. Map rows reach the base through the bounded merge."""
        # Only a committed listing fulfills a deferred refresh. Probe its
        # primary key from the small dirty set, never scan the complete map.
        where='''volume=? AND EXISTS(SELECT 1 FROM listings l
            WHERE l.generation=? AND l.volume=? AND l.id=inode_key(dirty.id))'''
        args=(self.volume,self.generation,self.vid)
        count,payload=self._row_size('dirty',where,args)
        return [(f'DELETE FROM dirty WHERE {where}',args,count,payload,'dirty')]

    def finish_volume(self,volume,reader,gaps,*,full,lean,bookmark=None,event_uuid=None,complete=True,physical=True,
                      event_progress=None,used_before=None):
        """Place shared blocks, total every folder and publish. `volume` is read after the
        walk; `used_before` is its used space before it, for the reconciliation's tolerance."""
        self.flush();old=self.db.execute('SELECT * FROM volumes WHERE id=?',(self.volume,)).fetchone()
        self.phase('sharing')
        affected,sharing_gaps=self._sharing(reader,full)
        self.phase('aggregate')
        if full:self._aggregate_full()
        else:self._aggregate(affected|self.changed)
        gaps=set(gaps)|sharing_gaps
        complete=complete and not sharing_gaps
        roots=list(self._rows('nodes','parent IS NULL'))
        total=sum(r['physical'] for r in roots) if roots else None
        used_change=volume.used-used_before if volume.used is not None and used_before is not None else 0
        unplaced=sum(row['unplaced'] for row in self._rows('streams','unplaced>0'))
        if unplaced:gaps.add('shared_copy_placed_at_full_walk')
        if physical:
            result=reconcile(volume.used,total,supported=volume.fs in ('apfs','hfs'),complete=complete,
                             previously_invalid=bool(old['generation'] and not old['valid']),full=full,used_change=used_change)
        else:
            # No extents provider (Linux): each inode counts once per folder; no residual is claimed.
            result={'valid':True,'physical':total,'not_placed':None,'tolerance':None,'status':'observed' if complete else 'partial'}
            gaps=set(gaps)|{'physical_residual_not_computed'}
        plan=self._publication_changes()
        metric='physical' if result['valid'] else 'listed'
        where='generation=? AND volume=?'
        params=(self.generation,self.vid)
        if full:
            where+=f' AND ({metric}>=1000000000 OR EXISTS(SELECT 1 FROM tracked WHERE volume=? AND id=source_id(nodes.id)))'
            params+=self.volume,
        candidates,payload=self._row_size('nodes',where,params)
        # An update writes points only for the changed rows _points writes for, and
        # only tracked rows join the hierarchy, so the new ones are among them.
        point_candidates=candidates if full else sum(1 for key in self.changed if (
            node:=self._published_node(key,self.base_gen,self.generation)) is not None and self._last_point(node,metric)[1])
        hierarchy_candidates=candidates if full else point_candidates
        tracked,tracked_payload=self._row_size('tracked','volume=?',(self.volume,))
        # Points also visit removed identities absent from the staged nodes.
        # Reserve two writes per candidate and a terminal point per old identity.
        # Hierarchy: at most old+new identities, plus one epoch. An update also marks
        # its staged generation as the published delta.
        rows=sum(p[2] for p in plan)+2*point_candidates+hierarchy_candidates+2*tracked+5+(self.base_gen is not None)
        payload=3*payload+3*tracked_payload+sum(p[3] for p in plan)
        payload+=(candidates+tracked)*(len(self.volume.encode())+64)  # volume and hierarchy version
        payload+=self.db.execute('''SELECT coalesce(sum(length(CAST(p.label AS BLOB))),0)
            FROM tracked t JOIN points p ON p.volume=t.volume AND p.id=t.id
            AND p.observed_ns=(SELECT max(q.observed_ns) FROM points q WHERE q.volume=t.volume AND q.id=t.id)
            WHERE t.volume=?''',(self.volume,)).fetchone()[0]*2
        gap_text=json.dumps(sorted(set(gaps)))
        payload+=len((volume.label+volume.mount+gap_text+(event_uuid or '')).encode())
        no_progress=0 if full or event_progress is True else old['no_progress']+1 if event_progress is False else old['no_progress']
        # Counts only, with fixed labels: what a refused publication declared, so a
        # run record can say which part was too large.
        receipt=dict(plan={label:[count,size] for _,_,count,size,label in plan},changed=len(self.changed),
                     point_candidates=point_candidates,hierarchy_candidates=hierarchy_candidates,
                     tracked=tracked,rows=rows,payload_bytes=payload)
        self.phase('publication')
        receipt.update(existing_pages=self.db.execute('PRAGMA page_count').fetchone()[0],
                       freelist_pages=self.db.execute('PRAGMA freelist_count').fetchone()[0])
        try:
            with self.write(rows=rows,payload_bytes=payload):
                for sql,args,_,_,_ in plan:self.db.execute(sql,args)
                # An update publishes its staged generation as a delta over the base.
                published,delta=(self.generation,None) if self.base_gen is None else (self.base_gen,self.generation)
                now=time.time_ns()
                self._points(published,now,result['valid'],full,delta)
                save_hierarchy(self.db,published,self.volume,now,volume_key=self.vid,delta=delta)
                self.db.execute('''UPDATE volumes SET label=?,mount=?,generation=?,observed_ns=?,full_ns=?,volume_used=?,capacity=?,free=?,available=?,
                    valid=?,status=?,lean=?,bookmark=?,no_progress=?,event_uuid=?,gaps=?,mounted=1,budget_retry_bytes=NULL,delta_generation=?,
                    used_change=?,unplaced_shared=? WHERE id=?''',
                    (volume.label,volume.mount,published,now,now if full else old['full_ns'],volume.used,volume.capacity,volume.free,volume.available,
                     int(result['valid']),result['status'],int(lean),bookmark,no_progress,
                     event_uuid,gap_text,delta,used_change,unplaced,self.volume))
                if full and old['generation'] is not None:
                    self.db.execute("UPDATE generations SET status='retired' WHERE id=?",(old['generation'],))
                self.db.execute("UPDATE generations SET status='published' WHERE id=?",(published,))
                if delta is not None:self.db.execute("UPDATE generations SET status='delta' WHERE id=?",(delta,))
        except BudgetExceeded as exc:
            exc.publication=dict(receipt,shortfall_bytes=exc.shortfall_bytes);raise
        self.generation=published;self.base_gen=None
        if delta is not None:
            try:self.merge_delta(self.volume)
            except Exception as exc:raise MergePending(exc) from exc
        return result

    def _published_node(self,key,base,delta):
        """A directory's row in this volume's published map: the base overlaid by `delta`."""
        return self.db.execute(f'''SELECT * FROM nodes n WHERE n.generation IN (:g,:d) AND n.volume=:v AND n.id=:id
            AND {published('nodes','n',':g',':v',':d')}''',dict(g=base,d=delta,v=self.vid,id=key)).fetchone()

    def finish_merges(self):
        """Merge every published delta, before any scan this run. Returns, by volume id,
        a MergePending for each delta still pending."""
        pending={}
        for volume_id, in self.db.execute('SELECT id FROM volumes WHERE delta_generation IS NOT NULL').fetchall():
            try:self.merge_delta(volume_id)
            except Exception as exc:pending[volume_id]=MergePending(exc)
        return pending

    def merge_delta(self,volume_id):
        """Apply a volume's published delta to its base in bounded batches, then clear it.

        The delta still overlays every row a batch writes, so each committed batch leaves
        the published map as it was. The work is the base rows the delta replaces or hides,
        plus the delta itself. A refusal or crash keeps the delta; merging again resumes."""
        row=self.db.execute('SELECT key,generation,delta_generation FROM volumes WHERE id=?',(volume_id,)).fetchone()
        if row is None or row['delta_generation'] is None:return
        self.phase('merge')
        names=dict(g=row['generation'],v=row['key'],d=row['delta_generation'])
        for table in MAP_TABLES:
            self._merge_removals(table,names)
            self._merge_rows(table,names)
        with self.write(rows=2):
            self.db.execute('UPDATE volumes SET delta_generation=NULL WHERE key=?',(names['v'],))
            # The merged delta is only staging now, pruned like any other.
            self.db.execute("UPDATE generations SET status='building' WHERE id=?",(names['d'],))

    def _payload(self,table,alias):
        columns=[r['name'] for r in self.db.execute(f'PRAGMA table_info({table})')]
        return '+'.join(f"CASE WHEN typeof({alias}.{c}) IN ('text','blob') THEN length(CAST({alias}.{c} AS BLOB)) ELSE 0 END" for c in columns)

    @staticmethod
    def _chunks(rows):
        batch=[]
        for row in rows:
            batch.append(row)
            if len(batch)==MERGE_ROWS:yield batch;batch=[]
        if batch:yield batch

    def _merge_removals(self,table,names):
        """Delete base rows the delta removes, or re-lists without a replacement. It walks
        the delta's markers and, within each, the base rows by key, so a marker that hides
        more than one batch keeps its place."""
        key,primary=KEYS[table],PRIMARY[table]
        markers=[m for m in (REMOVED[table],RESET.get(table)) if m]
        # Each marker table's next id by its key; the smaller is the next marker.
        first,following=(f"SELECT min(id) FROM ({' UNION ALL '.join(f'SELECT * FROM (SELECT id FROM {m} WHERE generation=:d AND volume=:v{bound} ORDER BY id LIMIT 1)' for m in markers)})"
                         for bound in ('',' AND id>:after'))
        within=primary[1:]
        keys=','.join('b.'+c for c in primary)
        replaced=' AND '.join(f'r.{c}=b.{c}' for c in primary)
        def doomed():
            marker=None
            while (marker:=self.db.execute(following if marker is not None else first,dict(names,after=marker)).fetchone()[0]) is not None:
                last=None
                while True:
                    seek=f" AND ({','.join('b.'+c for c in within)})>({','.join(':s'+str(i) for i in range(len(within)))})" if last else ''
                    # A delta row with this key replaces the base row instead.
                    rows=self.db.execute(f'''SELECT {keys},{self._payload(table,'b')} FROM {table} b
                        WHERE b.generation=:g AND b.volume=:v AND b.{key}=:marker{seek}
                        AND NOT EXISTS(SELECT 1 FROM {table} r WHERE r.generation=:d AND r.volume=:v AND {replaced}
                            AND {published(table,'r',':g',':v',':d')})
                        ORDER BY {keys} LIMIT :n''',dict(names,marker=marker,n=MERGE_ROWS,
                        **{f's{i}':v for i,v in enumerate(last or ())})).fetchall()
                    yield from rows
                    if len(rows)<MERGE_ROWS or not within:break
                    last=tuple(rows[-1])[1:len(primary)]
        for batch in self._chunks(doomed()):
            with self.write(rows=len(batch),payload_bytes=sum(r[-1] for r in batch)):
                self.db.executemany(f'DELETE FROM {table} WHERE generation=? AND volume=? AND {" AND ".join(c+"=?" for c in primary)}',
                                    ((names['g'],names['v'],*tuple(r)[:-1]) for r in batch))

    def _merge_rows(self,table,names):
        """Write the delta rows that differ into the base, one insert-or-update each,
        walking the delta by primary key."""
        columns=[r['name'] for r in self.db.execute(f'PRAGMA table_info({table})')]
        primary=PRIMARY[table]
        pk=('generation','volume',*primary)
        keys=','.join('d.'+c for c in primary)
        values=','.join(f'{c}=excluded.{c}' for c in columns if c not in pk)
        same=' AND '.join(f'o.{c}{"=" if c in pk else " IS "}d.{c}' for c in columns[1:])
        last=None
        while True:
            seek=f" AND ({keys})>({','.join(':s'+str(i) for i in range(len(primary)))})" if last else ''
            found=self.db.execute(f'''SELECT {keys},{self._payload(table,'d')} FROM {table} d
                WHERE d.generation=:d AND d.volume=:v AND {published(table,'d',':g',':v',':d')}{seek}
                AND NOT EXISTS(SELECT 1 FROM {table} o WHERE o.generation=:g AND {same})
                ORDER BY {keys} LIMIT :n''',dict(names,n=MERGE_ROWS,**{f's{i}':v for i,v in enumerate(last or ())})).fetchall()
            if not found:return
            with self.write(rows=len(found),payload_bytes=sum(r[-1] for r in found)):
                self.db.executemany(f'''INSERT INTO {table} SELECT ?,{",".join(columns[1:])} FROM {table}
                    WHERE generation=? AND volume=? AND {" AND ".join(c+"=?" for c in primary)}
                    ON CONFLICT({",".join(pk)}) DO UPDATE SET {values}''',
                    ((names['g'],names['d'],names['v'],*tuple(r)[:-1]) for r in found))
            if len(found)<MERGE_ROWS:return
            last=tuple(found[-1])[:-1]

    def _points(self,generation,now,valid,full,delta=None):
        metric='physical' if valid else 'listed'
        if full:
            rows=self.db.execute(f'''SELECT * FROM nodes WHERE generation=? AND volume=? AND ({metric}>=1000000000 OR
                 EXISTS(SELECT 1 FROM tracked WHERE volume=? AND id=source_id(nodes.id)))''',(generation,self.vid,self.volume))
        else:
            rows=(self._published_node(k,generation,delta) for k in self.changed)
        for row in rows:
            if row is None:continue
            identity=source_id(row['id'])
            last,writes=self._last_point(row,metric)
            if not writes:continue
            # Baseline readout context only; historical placement has its own
            # immutable observations and must not be inferred from this cache.
            self.db.execute('INSERT INTO tracked VALUES(?,?,?) ON CONFLICT(volume,id) DO UPDATE SET parent=excluded.parent',
                            (self.volume,identity,source_id(row['parent'])))
            status=row['status'] if row['status']!='observed' else 'below_threshold' if row[metric]<1_000_000_000 else 'observed'
            size=row[metric] if status in ('observed','below_threshold') else None
            if not last or (last['size'],last['metric'],last['status'])!=(size,metric,status):
                self.db.execute('INSERT INTO points VALUES(?,?,?,?,?,?,?)',(self.volume,identity,now,row['label'],metric,size,status))
        for tracked in self.db.execute('SELECT id FROM tracked WHERE volume=?',(self.volume,)):
            if not self._published_node(inode_key(tracked['id']),generation,delta):
                last=self.db.execute('SELECT * FROM points WHERE volume=? AND id=? ORDER BY observed_ns DESC LIMIT 1',(self.volume,tracked['id'])).fetchone()
                if last and (last['size'] is not None or last['status']!='not_observed'):
                    self.db.execute('INSERT INTO points VALUES(?,?,?,?,?,?,?)',(self.volume,tracked['id'],now,last['label'],metric,None,'not_observed'))

    def _last_point(self,row,metric):
        """A map row's latest point, and whether _points writes for it: a row with
        a point keeps its history, any other only from 1 GB."""
        last=self.db.execute('SELECT * FROM points WHERE volume=? AND id=? ORDER BY observed_ns DESC LIMIT 1',
                             (self.volume,source_id(row['id']))).fetchone()
        return last,last is not None or row[metric]>=1_000_000_000

    def mark_failed(self,status,*,end_run=True,retry_bytes=None):
        self.buffer.clear();self.buffer_bytes=0;self.pending_resets.clear()
        # End the run before attempting a separate volume-status write. A
        # failure of that second admission must not leave the run "running".
        if end_run:self.progress(status=status)
        # A published attempt stands, even if its write failed after commit: a later
        # failure is the run's, not the map's.
        if self.db.execute('SELECT 1 FROM volumes WHERE id=? AND ? IN (generation,delta_generation)',
                           (self.volume,self.generation)).fetchone():return
        with self.write(rows=1,payload_bytes=len(status.encode())):
            self.db.execute('UPDATE volumes SET status=?,budget_retry_bytes=? WHERE id=?',(status,retry_bytes,self.volume))

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


def hot_set_query(delta):
    """The hot set over each volume's published map; `delta` is the volumes row's delta
    column, or NULL for a format-2 store that has none."""
    def visible(table,alias,source):
        return published(table,alias,f'{source}.generation',f'{source}.vkey' if source!='v' else 'v.key',f'{source}.delta')
    return f'''
WITH volume AS (SELECT id,key,generation,{delta} AS delta,mount,mounted FROM volumes),
hot(volume_id,vkey,generation,delta,mount,id,size) AS (
  SELECT v.id,v.key,v.generation,v.delta,v.mount,n.id,n.listed FROM volume v
  JOIN nodes n ON n.generation IN (v.generation,v.delta) AND n.volume=v.key
  WHERE v.mounted=1 AND n.kind='directory' AND n.listed>=1000000000 AND {visible('nodes','n','v')}
  UNION ALL
  SELECT v.id,v.key,v.generation,v.delta,v.mount,f.parent,f.allocated FROM volume v
  JOIN files f ON f.generation IN (v.generation,v.delta) AND f.volume=v.key
  WHERE v.mounted=1 AND f.allocated>=100000000 AND {visible('files','f','v')}),
top AS (SELECT volume_id,vkey,generation,delta,mount,id,max(size) AS size FROM hot
  GROUP BY vkey,id ORDER BY size DESC LIMIT ?),
chain(volume_id,generation,delta,vkey,mount,size,start,start_parent,node,rel,depth) AS (
  SELECT t.volume_id,t.generation,t.delta,t.vkey,t.mount,t.size,t.id,n.parent,n.parent,
         CASE WHEN n.parent IS NULL THEN '' ELSE '/'||n.label END,0
  FROM top t JOIN nodes n ON n.generation IN (t.generation,t.delta) AND n.volume=t.vkey AND n.id=t.id AND {visible('nodes','n','t')}
  UNION ALL
  SELECT c.volume_id,c.generation,c.delta,c.vkey,c.mount,c.size,c.start,c.start_parent,n.parent,
         CASE WHEN n.parent IS NULL THEN c.rel ELSE '/'||n.label||c.rel END,c.depth+1
  FROM chain c JOIN nodes n ON n.generation IN (c.generation,c.delta) AND n.volume=c.vkey AND n.id=c.node AND {visible('nodes','n','c')}
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
        db.execute('BEGIN')  # The format and the map it selects are one snapshot.
        version=db.execute('PRAGMA user_version').fetchone()[0]
        if version not in OPENS:
            raise ValueError('scanner_format_requires_fresh_store')
        rows=db.execute(hot_set_query('delta_generation' if version>=3 else 'NULL'),(limit,)).fetchall()
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
            db.execute('BEGIN')
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version!=FORMAT:
                if version not in OPENS:raise ValueError('scanner_format_requires_fresh_store')
                # The scanner converts the store when it next runs; until then nothing is shown.
                return dict(schema_version=1,shape='storage',action=action,status='awaiting_physical_walk',coverage={},rows=[],truncated=False)
            volumes = list(db.execute('SELECT * FROM volumes ORDER BY label LIMIT 2001'))
            def map_rows(table,v,where,tail='',**params):
                # Each generation of the published map keeps its own index, seek and
                # limit; callers merge the two bounded results.
                delta=v['delta_generation'] if 'delta_generation' in v.keys() else None
                names=dict(params,g=v['generation'],d=delta,v=v['key'])
                return [row for generation in dict.fromkeys((v['generation'],delta)) if generation is not None
                        for row in db.execute(f'''SELECT * FROM {table} x WHERE x.generation=:gen AND x.volume=:v
                            AND {published(table,'x',':g',':v',':d')} AND {where} {tail}''',dict(names,gen=generation))]
            def map_sum(table,v,column,where,**params):
                delta=v['delta_generation'] if 'delta_generation' in v.keys() else None
                names=dict(params,g=v['generation'],d=delta,v=v['key'])
                return sum(db.execute(f'''SELECT coalesce(sum(x.{column}),0) FROM {table} x WHERE x.generation=:gen AND x.volume=:v
                    AND {published(table,'x',':g',':v',':d')} AND {where}''',dict(names,gen=generation)).fetchone()[0]
                    for generation in dict.fromkeys((v['generation'],delta)) if generation is not None)
            chosen = params.get('volume_id') or (volumes[0]['id'] if volumes else None)
            volume = next((v for v in volumes if v['id']==chosen),None)
            rows=[];truncated=False;next_cursor=None;hierarchy=None;folder=None
            if action == 'volumes':
                if cursor:raise ValueError('incompatible_storage_cursor')
                for v in volumes[:limit+1]:
                    root = next(iter(map_rows('nodes',v,'x.parent IS NULL','LIMIT 1')),None)
                    listed,physical=(root['listed'],root['physical']) if root else (None,None)
                    valid = bool(v['valid']) and physical is not None
                    placed=valid and v['volume_used'] is not None and 'physical_residual_not_computed' not in json.loads(v['gaps'])
                    rows.append(dict(id=v['id'],label=v['label'],observed_ns=v['observed_ns'],full_scan_ns=v['full_ns'],
                                     listed=listed,physical=physical if valid else None,volume_used=v['volume_used'],
                                     not_placed=v['volume_used']-physical if placed else None,
                                     tolerance=max(accounting.GB,abs(v['used_change'] or 0)) if placed else None,
                                     unplaced_shared=v['unplaced_shared'] or 0,
                                     capacity=v['capacity'],free=v['free'],available=v['available'],valid=valid,status=v['status'],
                                     mounted=bool(v['mounted']),gaps=json.loads(v['gaps'])))
                truncated=len(rows)>limit
            elif volume and action == 'children':
                parent = params.get('parent_id')
                if parent is None:
                    root = next(iter(map_rows('nodes',volume,'x.parent IS NULL','LIMIT 1')),None)
                    parent = source_id(root['id']) if root else None
                metric='physical' if volume['valid'] else 'listed'
                scope=_scope([action,volume['generation'],volume['observed_ns'],chosen,parent,metric,'size-desc-id-desc-v2'])
                if cursor and cursor['scope']!=scope:raise ValueError('stale_or_incompatible_storage_cursor')
                seek='';values={}
                if cursor:
                    if set(cursor)!={'size','id','scope'}:raise ValueError('invalid_children_cursor')
                    values=dict(size=cursor['size'],id=inode_key(cursor['id']))
                seek=f' AND (x.{metric},x.id)<(:size,:id)' if cursor else ''
                for n in map_rows('nodes',volume,f'x.parent IS :parent{seek}',f'ORDER BY x.{metric} DESC,x.id DESC LIMIT :n',
                                  parent=inode_key(parent),n=limit+1,**values):
                    valid=bool(volume['valid'])
                    rows.append(dict(id=source_id(n['id']),parent_id=source_id(n['parent']),label=n['label'],kind=n['kind'],listed=n['listed'],
                                     physical=n['physical'] if valid else None,frees=n['frees'] if valid else None,
                                     asterisk=accounting.asterisk(n['physical'],n['listed'],n['frees']) if valid else None,
                                     status=n['status'],observed_ns=volume['observed_ns'],_size=n[metric] or 0))
                seek=' AND (x.allocated,x.id)<(:size,:id)' if cursor else ''
                for f in map_rows('files',volume,f'x.parent IS :parent{seek}','ORDER BY x.allocated DESC,x.id DESC LIMIT :n',
                                  parent=inode_key(parent),n=limit+1,**values):
                    # A file's allocation is its own physical space, shared blocks included.
                    rows.append(dict(id=source_id(f['id']),parent_id=source_id(f['parent']),label=f['label'],kind='file',listed=f['allocated'],
                                     physical=f['allocated'] if volume['valid'] else None,frees=None,asterisk=None,
                                     status='observed',observed_ns=volume['observed_ns'],_size=f['allocated']))
                def order(row):
                    key=inode_key(row['id'])
                    return row['_size'],isinstance(key,str),key
                rows.sort(key=order,reverse=True)
                truncated=len(rows)>limit
                rows=rows[:limit]
                if truncated and rows:next_cursor=_encode_cursor({'size':rows[-1]['_size'],'id':rows[-1]['id'],'scope':scope})
                for row in rows:row.pop('_size')
                own=next(iter(map_rows('nodes',volume,'x.id=:id','LIMIT 1',id=inode_key(parent))),None) if parent is not None else None
                if own is not None and volume['valid']:
                    # Rows add to more than their folder when they share blocks with each
                    # other: each counts every block it holds. Its own entry's blocks are
                    # in the folder but in no row, a few kilobytes.
                    shared=(map_sum('nodes',volume,'physical','x.parent IS :parent',parent=inode_key(parent))+
                            map_sum('files',volume,'allocated','x.parent IS :parent',parent=inode_key(parent))-own['physical'])
                    folder=dict(id=parent,physical=own['physical'],shared_between_rows=shared if shared>=accounting.GB else None)
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
            # Excluded roots are few: configured exclusions, each a single row.
            excluded=len(map_rows('nodes',volume,"x.status='excluded'")) if volume else 0
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
                        rows=rows[:limit],truncated=truncated,next_cursor=next_cursor,
                        **({'folder':folder} if action=='children' else {}))
        finally:db.close()
