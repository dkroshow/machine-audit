"""SQLite readings and cumulative summaries share every atomic watermark."""
from __future__ import annotations
from collections import defaultdict
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import sqlite3
import threading
import zlib
from .budget import Budget, BudgetExceeded
from .derive import points, rollups, merge_summaries
from .model import Record, field_component, is_series

MINUTE = 60_000_000_000
HOUR = 60 * MINUTE
# Per-scope summary resolution (storage-approach.md): these scopes keep hour summaries only.
# Raw readings still answer recent intervals.
HOUR_ONLY_SCOPES = frozenset({"unit_detail"})
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, boot_id TEXT NOT NULL, started_ns INTEGER NOT NULL, stopped_ns INTEGER, last_sequence INTEGER NOT NULL DEFAULT 0, last_saved_ns INTEGER);
CREATE TABLE IF NOT EXISTS readings(run_id TEXT NOT NULL, sequence INTEGER NOT NULL, wall_ns INTEGER NOT NULL, source TEXT NOT NULL, component TEXT NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(run_id,sequence));
CREATE TABLE IF NOT EXISTS reading_components(run_id TEXT NOT NULL, sequence INTEGER NOT NULL, component TEXT NOT NULL, is_series INTEGER NOT NULL, PRIMARY KEY(component,run_id,sequence));
-- Series and coverage check each window reading's components by record.
CREATE INDEX IF NOT EXISTS reading_components_record ON reading_components(run_id,sequence);
CREATE INDEX IF NOT EXISTS readings_time ON readings(wall_ns,source);
CREATE INDEX IF NOT EXISTS readings_source_time ON readings(source,wall_ns);
CREATE TABLE IF NOT EXISTS signal_catalog(source TEXT NOT NULL, signal TEXT NOT NULL, component TEXT NOT NULL, kind TEXT NOT NULL, unit TEXT NOT NULL, scope TEXT NOT NULL, scale REAL NOT NULL, PRIMARY KEY(source,signal,scope));
CREATE TABLE IF NOT EXISTS summaries(id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, sequence_min INTEGER NOT NULL, sequence_max INTEGER NOT NULL, resolution TEXT NOT NULL, bucket_ns INTEGER NOT NULL, source TEXT NOT NULL, component TEXT NOT NULL, entity TEXT NOT NULL, payload BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS summaries_time ON summaries(resolution,component,bucket_ns);
CREATE UNIQUE INDEX IF NOT EXISTS summaries_identity ON summaries(run_id,resolution,bucket_ns,source,component,entity);
CREATE TABLE IF NOT EXISTS baselines(source TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS incidents(run_id TEXT NOT NULL, sequence INTEGER NOT NULL, wall_ns INTEGER NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(run_id,sequence));
CREATE TABLE IF NOT EXISTS coverage(run_id TEXT NOT NULL, sequence_max INTEGER NOT NULL, resolution TEXT NOT NULL, source TEXT NOT NULL, component TEXT NOT NULL, start_ns INTEGER NOT NULL, end_ns INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS coverage_time ON coverage(resolution,start_ns,end_ns);
CREATE TABLE IF NOT EXISTS retention(kind TEXT NOT NULL, start_ns INTEGER NOT NULL, end_ns INTEGER NOT NULL, reason TEXT NOT NULL);
"""


def encode(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def pack_summaries(rows):
    groups = {}
    for row in rows:
        keys=tuple(sorted(row))
        groups.setdefault(keys,[]).append([row[k] for k in keys])
    return zlib.compress(encode([2, [[keys,values] for keys,values in groups.items()]]).encode(), 1)


def unpack_summaries(payload):
    version, groups = json.loads(zlib.decompress(payload))
    if version != 2:
        raise ValueError("unsupported_summary_encoding")
    return [dict(zip(keys,values)) for keys,rows in groups for values in rows]


class Store:
    def __init__(self, path: str | Path, budget: Budget | None = None, *, read_only=False):
        self.path = Path(path)
        if read_only and not self.path.is_file():
            raise FileNotFoundError("no saved Machine-audit history")
        self.budget = budget or Budget(self.path.parent)
        if self.path.parent.resolve() != self.budget.root.resolve() or self.path.is_symlink():
            raise ValueError("store must be a regular file in the budget directory")
        self._mutex = threading.RLock()
        if read_only:
            self.db=sqlite3.connect(f"file:{self.path}?mode=ro",uri=True,check_same_thread=False,isolation_level=None)
            if self.db.execute("PRAGMA user_version").fetchone()[0]!=2:
                self.db.close();raise ValueError("unsupported_private_store_schema")
            return
        with self.budget.reserve(256 * 1024):
            self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=5)
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone() and self.db.execute("PRAGMA user_version").fetchone()[0]!=2:
                self.db.close();raise ValueError("unsupported_private_store_schema")
            os.chmod(self.path, 0o600)
            self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript(SCHEMA)
            self.db.execute("PRAGMA user_version=2")
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self):
        with self._mutex:
            self.db.close()

    @contextmanager
    def read(self):
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, isolation_level=None, timeout=5)
        db.row_factory = sqlite3.Row
        db.create_function("is_series", 3, is_series, deterministic=True)
        try:
            db.execute("BEGIN")
            # Establish the SQLite snapshot immediately, before any racing commit.
            db.execute("SELECT count(*) FROM runs").fetchone()
            yield db
        finally:
            db.close()

    def start_run(self, run_id, boot_id, started_ns):
        with self._mutex, self.budget.sqlite_write(self.db,self.path):
            self.db.execute("INSERT INTO runs(run_id,boot_id,started_ns) VALUES(?,?,?)", (run_id, boot_id, started_ns))

    def stop_run(self, run_id, stopped_ns):
        with self._mutex, self.budget.sqlite_write(self.db,self.path):
            self.db.execute("UPDATE runs SET stopped_ns=? WHERE run_id=?", (stopped_ns,run_id))

    def _previous(self, records):
        result = {}
        for r in records:
            source = r.reading.source
            if source in result:
                continue
            row = self.db.execute("SELECT payload FROM readings WHERE run_id=? AND source=? ORDER BY sequence DESC LIMIT 1", (r.run_id,source)).fetchone()
            if not row:
                row = self.db.execute("SELECT payload FROM baselines WHERE run_id=? AND source=?",(r.run_id,source)).fetchone()
            if row:
                result[source] = Record.unpack(row[0])
        return result

    def append(self, records: list[Record], *, commit_lock=None, on_commit=None):
        if not records:
            return
        with self._mutex:
            for i, r in enumerate(records):
                if r.sequence < 1 or (i and (r.run_id != records[0].run_id or r.boot_id != records[0].boot_id or r.sequence <= records[i-1].sequence)):
                    raise ValueError("one append requires one run and increasing positive sequences")
            raw = [(r, r.packed) for r in records]
            derived = points(records, self._previous(records))
            summaries = defaultdict(list)
            hours=defaultdict(list)
            for bucket,row in rollups(derived,MINUTE):
                if row["scope"] not in HOUR_ONLY_SCOPES:
                    summaries[("minute",bucket,row["source"],row["component"],row["entity"])].append(row)
                hours[(bucket//HOUR*HOUR,row["source"],row["component"],row["entity"],row["signal"],row["unit"],row["scope"],row["kind"],row["scale"])].append(row)
            for key,rows in hours.items():
                bucket,source,component,entity,*_=key
                summaries[("hour",bucket,source,component,entity)].append(merge_summaries(rows) if len(rows)>1 else rows[0])
            self.maintain()
            for attempt in range(8):
                try:
                    self._write(records,raw,summaries,commit_lock,on_commit)
                    return
                except (sqlite3.OperationalError,BudgetExceeded) as exc:
                    full=getattr(exc,"sqlite_errorcode",None)==sqlite3.SQLITE_FULL or str(exc) in {"managed_storage_limit","sqlite_checkpoint_headroom"}
                    if not full or attempt==7 or not self.prune_oldest():raise

    def _write(self,records,raw,summaries,commit_lock,on_commit):
        with self.budget.sqlite_write(self.db,self.path):
            self.db.execute("BEGIN IMMEDIATE")
            try:
                first, last = records[0], records[-1]
                self.db.execute("INSERT OR IGNORE INTO runs(run_id,boot_id,started_ns) VALUES(?,?,?)", (first.run_id,first.boot_id,first.reading.start.wall_ns))
                saved, boot = self.db.execute("SELECT last_sequence,boot_id FROM runs WHERE run_id=?",(first.run_id,)).fetchone()
                if boot!=first.boot_id:raise ValueError("run boot identity changed")
                if first.sequence <= saved:
                    raise ValueError("sequence already committed")
                self.db.executemany("INSERT INTO readings VALUES(?,?,?,?,?,?)",[(r.run_id,r.sequence,r.reading.end.wall_ns,r.reading.source,r.reading.component,p) for r,p in raw])
                self.db.executemany("INSERT INTO reading_components VALUES(?,?,?,?)",[(r.run_id,r.sequence,c,int(not r.reading.fields or any(field_component(f,r.reading.component)==c and is_series(c,f.entity,f.scope) for f in r.reading.fields))) for r,_ in raw for c in {r.reading.component,*(field_component(f,r.reading.component) for f in r.reading.fields)}])
                for (resolution,bucket,source,component,entity),rows in summaries.items():
                    lower=first.sequence
                    old=self.db.execute("SELECT sequence_min,payload FROM summaries WHERE run_id=? AND resolution=? AND bucket_ns=? AND source=? AND component=? AND entity=?",(first.run_id,resolution,bucket,source,component,entity)).fetchone()
                    def key(row):return tuple(row[k] for k in ("signal","unit","kind","scope","scale"))
                    merged={key(row):row for row in unpack_summaries(old[1])} if old else {}
                    for row in rows:
                        k=key(row)
                        merged[k]=merge_summaries([merged[k],row]) if k in merged else row
                    if old:lower=min(lower,old[0])
                    self.db.execute("INSERT INTO summaries(run_id,sequence_min,sequence_max,resolution,bucket_ns,source,component,entity,payload) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,resolution,bucket_ns,source,component,entity) DO UPDATE SET sequence_min=excluded.sequence_min,sequence_max=excluded.sequence_max,payload=excluded.payload",(first.run_id,lower,last.sequence,resolution,bucket,source,component,entity,pack_summaries(list(merged.values()))))
                catalog={(row["source"],row["signal"],row["component"],row["kind"],row["unit"],row["scope"],row["scale"]) for rows in summaries.values() for row in rows}
                self.db.executemany("INSERT OR REPLACE INTO signal_catalog VALUES(?,?,?,?,?,?,?)",sorted(catalog))
                latest = {}
                coverage = defaultdict(list)
                for r,p in raw:
                    latest[r.reading.source]=(r,p)
                    for resolution,width in (("minute",MINUTE),("hour",HOUR)):
                        for component in {r.reading.component,*(field_component(f,r.reading.component) for f in r.reading.fields)}:
                            coverage[(resolution,r.reading.source,component,r.reading.end.wall_ns//width)].append(r)
                    if any(f.kind=="event" for f in r.reading.fields):
                        self.db.execute("INSERT INTO incidents VALUES(?,?,?,?)",(r.run_id,r.sequence,r.reading.end.wall_ns,p))
                for source,(r,p) in latest.items():
                    self.db.execute("INSERT OR REPLACE INTO baselines VALUES(?,?,?)",(source,r.run_id,p))
                for (resolution,source,component,_), group in coverage.items():
                    statuses=defaultdict(int)
                    for r in group:
                        statuses[r.reading.status]+=1
                        for f in r.reading.fields:
                            if f.status not in {"observed","measured_zero"}:statuses[f.status]+=1
                    first_ns=min(r.reading.start.wall_ns for r in group)
                    last_ns=max(r.reading.end.wall_ns for r in group)
                    data={"statuses":dict(statuses),"reads":len(group)}
                    self.db.execute("INSERT INTO coverage VALUES(?,?,?,?,?,?,?,?)",(first.run_id,last.sequence,resolution,source,component,first_ns,last_ns,encode(data)))
                self.db.execute("UPDATE runs SET last_sequence=?,last_saved_ns=? WHERE run_id=?",(last.sequence,max(r.reading.end.wall_ns for r in records),first.run_id))
                with commit_lock if commit_lock is not None else nullcontext():
                    self.db.execute("COMMIT")
                    if on_commit is not None:on_commit()
            except BaseException:
                if self.db.in_transaction:self.db.execute("ROLLBACK")
                raise

    def maintain(self, required_bytes=0):
        """Prune raw, then minute, hour and finally rare incident history.

        Work starts well before the cap; if reserve cannot fit, fail without
        removing a file or discarding the collector's pending batch.
        """
        own_bytes = self.path.stat().st_size
        # Retain the conservative full-WAL headroom, without subtracting the
        # database a second time from an already halved usable target.
        other = self.budget.usage()["bytes"] - own_bytes
        target = other + max(0, (self.budget.limit_bytes-other)//2)*.85
        attempts = 0
        while self.budget.usage()["bytes"] + required_bytes > target and attempts < 128:
            attempts += 1
            if not self.prune_oldest():
                break

    def prune_oldest(self):
        with self._mutex:
            row = self.db.execute("SELECT MIN(wall_ns) FROM readings").fetchone()
            if row[0] is not None:
                kind, start = "raw", row[0]
                end = (start // MINUTE + 1) * MINUTE
                table, condition, args = "readings", "wall_ns < ?", (end,)
                # Every raw row must have a committed summary fragment before pruning.
                missing = self.db.execute("SELECT 1 FROM readings r WHERE wall_ns<? AND NOT EXISTS (SELECT 1 FROM coverage s WHERE s.run_id=r.run_id AND s.resolution='minute' AND s.sequence_max>=r.sequence AND s.source=r.source) LIMIT 1",(end,)).fetchone()
                if missing:
                    return False
            else:
                row = self.db.execute("SELECT MIN(bucket_ns) FROM summaries WHERE resolution='minute'").fetchone()
                if row[0] is not None:
                    kind, start, end = "minute", row[0], row[0]+MINUTE
                    table, condition, args = "summaries", "resolution='minute' AND bucket_ns < ?", (end,)
                else:
                    row=self.db.execute("SELECT MIN(bucket_ns) FROM summaries WHERE resolution='hour'").fetchone()
                    if row[0] is not None:
                        kind,start,end="hour",row[0],row[0]+HOUR
                        table,condition,args="summaries","resolution='hour' AND bucket_ns < ?",(end,)
                    else:
                        row=self.db.execute("SELECT MIN(wall_ns) FROM incidents").fetchone()
                        if row[0] is None:return False
                        kind,start,end="incident",row[0],(row[0]//MINUTE+1)*MINUTE
                        table,condition,args="incidents","wall_ns < ?",(end,)
            with self.budget.sqlite_write(self.db,self.path,0):
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    if table=="readings":
                        self.db.execute("DELETE FROM reading_components WHERE (run_id,sequence) IN (SELECT run_id,sequence FROM readings WHERE wall_ns<?)",args)
                    self.db.execute(f"DELETE FROM {table} WHERE {condition}",args)
                    if kind=="minute":
                        self.db.execute("DELETE FROM coverage WHERE resolution='minute' AND end_ns<?",(end,))
                    elif kind=="hour":
                        self.db.execute("DELETE FROM coverage WHERE end_ns<?",(end,))
                    previous=self.db.execute("SELECT rowid FROM retention WHERE kind=? AND end_ns=? AND reason='storage_budget'",(kind,start)).fetchone()
                    if previous:self.db.execute("UPDATE retention SET end_ns=? WHERE rowid=?",(end,previous[0]))
                    else:self.db.execute("INSERT INTO retention VALUES(?,?,?,?)",(kind,start,end,"storage_budget"))
                    # One transaction keeps each dirty page to one WAL frame.
                    for _ in range(min(4096,self.db.execute("PRAGMA freelist_count").fetchone()[0])):
                        self.db.execute("PRAGMA incremental_vacuum(1)")
                    self.db.execute("COMMIT")
                except BaseException:
                    if self.db.in_transaction:self.db.execute("ROLLBACK")
                    raise
            return True

    def export_chunk(self, path: str | Path, *, device_id: str, run_id: str,
                     after_sequence=0, limit=1000, max_bytes=8*1024**2,
                     manifest_path: str | Path | None = None):
        """Create a bounded ordinary-record SQLite chunk, then its hashed manifest.

        Output paths must be new files in the shared managed root. A manifest
        appears only after the chunk is complete. No scanner table is opened.
        """
        import hashlib
        import time
        import re
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}",device_id) or not 1<=limit<=2000 or after_sequence<0 or max_bytes<65536:
            raise ValueError("invalid export request")
        path=Path(path)
        manifest_path=Path(manifest_path) if manifest_path else path.with_suffix(".manifest.json")
        for target in (path,manifest_path):
            if target.parent.resolve()!=self.budget.root.resolve() or target.exists() or target.is_symlink():
                raise ValueError("export needs new files in the managed root")
        if path==manifest_path:
            raise ValueError("chunk and manifest paths must differ")
        created=[]
        with self.budget.reserve(max_bytes+256*1024), self.read() as source:
            run=source.execute("SELECT * FROM runs WHERE run_id=?",(run_id,)).fetchone()
            if run is None:
                raise ValueError("unknown run")
            run=dict(run)
            try:
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                os.close(fd);created.append(path)
                chunk=sqlite3.connect(path,isolation_level=None)
                try:
                    # A new chunk is disposable until its manifest is written.
                    # No rollback/WAL sidecar can escape this hard file-size cap.
                    chunk.execute("PRAGMA journal_mode=OFF")
                    chunk.execute("PRAGMA synchronous=FULL")
                    chunk.execute("PRAGMA page_size=4096")
                    chunk.execute(f"PRAGMA max_page_count={max_bytes//4096}")
                    chunk.executescript("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE runs(run_id TEXT PRIMARY KEY,payload TEXT NOT NULL); CREATE TABLE records(run_id TEXT NOT NULL,sequence INTEGER NOT NULL,wall_ns INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(run_id,sequence)); PRAGMA user_version=1;")
                    chunk.executemany("INSERT INTO metadata VALUES(?,?)",[("schema_version","1"),("device_id",device_id)])
                    chunk.execute("INSERT INTO runs VALUES(?,?)",(run_id,encode(run)))
                    included=[]
                    payload_bytes=0
                    rows=source.execute("SELECT sequence,wall_ns,payload FROM readings WHERE run_id=? AND sequence>? AND sequence<=? ORDER BY sequence LIMIT ?",(run_id,after_sequence,run["last_sequence"],limit+1))
                    more=False
                    required_bytes=0
                    for sequence,wall_ns,packed in rows:
                        payload=encode(Record.unpack(packed).to_dict())
                        if len(included)>=limit or payload_bytes+len(payload)>max_bytes//2:
                            if not included:required_bytes=2*len(payload)+65536
                            more=True;break
                        chunk.execute("INSERT INTO records VALUES(?,?,?,?)",(run_id,sequence,wall_ns,payload))
                        included.append((sequence,wall_ns));payload_bytes+=len(payload)
                finally:
                    chunk.close()
                if more and not included:
                    raise ValueError(f"record_exceeds_export_chunk_limit: increase max_bytes to at least {required_bytes}")
                with path.open("rb") as stream:
                    digest=hashlib.file_digest(stream,"sha256").hexdigest()
                    os.fsync(stream.fileno())
                pruned=[dict(r) for r in source.execute("SELECT * FROM retention ORDER BY start_ns LIMIT 1001")]
                manifest={"schema_version":1,"format":"machine-audit-records-sqlite-v1","device_id":device_id,"run_id":run_id,
                          "after_sequence":after_sequence,"first_sequence":included[0][0] if included else None,
                          "last_sequence":included[-1][0] if included else None,"count":len(included),
                          "wall_start_ns":min(t for _,t in included) if included else None,
                          "wall_end_ns":max(t for _,t in included) if included else None,
                          "snapshot_watermark":run["last_sequence"],"pruned_ranges":pruned[:1000],"pruned_truncated":len(pruned)>1000,
                          "truncated":more,"sha256":digest,"creation_ns":time.time_ns(),"bytes":path.stat().st_size}
                data=encode(manifest).encode()
                if len(data)>256*1024:
                    raise ValueError("manifest bound exceeded")
                fd=os.open(manifest_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                created.append(manifest_path)
                with os.fdopen(fd,"wb") as stream:
                    stream.write(data);stream.flush();os.fsync(stream.fileno())
                return manifest
            except BaseException:
                # Only files exclusively created by this call are disposable.
                for target in created:
                    target.unlink(missing_ok=True)
                raise
