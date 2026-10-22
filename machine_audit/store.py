"""SQLite readings and cumulative summaries share every atomic watermark."""
from __future__ import annotations
import base64
from collections import defaultdict
from contextlib import contextmanager, nullcontext
import heapq
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import zlib
from .budget import Budget, BudgetExceeded
from .derive import points, rollups, merge_summaries
from .model import Record, field_component, is_series

MINUTE = 60_000_000_000
QUARTER = 15 * MINUTE
HOUR = 60 * MINUTE
# Stored summary resolutions, finest first; pruning removes them in this order.
WIDTHS = {"minute": MINUTE, "15-minute": QUARTER, "hour": HOUR}
# Minute rows for processes and programs keep only each minute's top consumers: per-instance
# minute rows were half of a 2,000-process Mac's store growth (development.md). 25 each by CPU
# time, memory and disk bytes names a minute's heavy users; 15-minute and hour rows keep everyone.
TOP_N = 25
MEMORY_RANK = ("process.phys_footprint", "process.resident_size")  # Mac footprint, else Linux resident set
IO_RANK = ("process.diskio_bytesread", "process.diskio_byteswritten")
# Per-scope summary resolution (storage-approach.md): these scopes keep hour summaries only.
# Raw readings still answer recent intervals.
HOUR_ONLY_SCOPES = frozenset({"unit_detail"})
PRUNE_BATCH = 4096
PRUNE_ROW_PAGES = 16
PRUNE_PRECHECK_STEPS = 1_000_000
PRUNE_PRECHECK_SECONDS = 1.0
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


UPSERT_SUMMARY = ("INSERT INTO summaries(run_id,sequence_min,sequence_max,resolution,bucket_ns,source,component,entity,payload) VALUES(?,?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(run_id,resolution,bucket_ns,source,component,entity) DO UPDATE SET "
                  "sequence_min=excluded.sequence_min,sequence_max=excluded.sequence_max,payload=excluded.payload")


def signal_key(row):
    return tuple(row[k] for k in ("signal", "unit", "kind", "scope", "scale"))


def ranked(component, entity):
    """Process and program rows whose minute grain is limited to top consumers."""
    return component != "self" and entity.startswith(("process:", "program:"))


def top_consumers(rows):
    """The union of the top N processes and the top N programs by each measure."""
    cpu, io, memory = defaultdict(float), defaultdict(float), {}
    for row in rows:
        signal, entity = row["signal"], row["entity"]
        if signal == "process.cpu" and row["total"] is not None:
            cpu[entity] += row["total"]*row["scale"]
        elif signal in IO_RANK and row["total"] is not None:
            io[entity] += row["total"]
        elif signal in MEMORY_RANK and row["max"] is not None:
            key = (-MEMORY_RANK.index(signal), row["max"])
            memory[entity] = max(memory.get(entity, key), key)
    measures = (cpu, {entity: value for entity, (_, value) in memory.items()}, io)
    keep = set()
    for measure in measures:
        for kind in ("process:", "program:"):
            keep.update(heapq.nlargest(TOP_N, (e for e, v in measure.items() if v > 0 and e.startswith(kind)),
                                       key=lambda e: (measure[e], e)))
    return keep


def on_time(deadline):
    """A stopping save gives up once its deadline passes; the stop then spills."""
    if deadline is not None and time.monotonic()>=deadline:raise TimeoutError("stop_deadline")


def held_rows(held,deadline=None):
    rows=[]
    for payload in held["rows"].values():
        on_time(deadline)
        rows.extend(unpack_summaries(payload))
    return rows


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
        self._held = None
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
        with self._mutex:
            self.maintain()
            def write():
                with self.budget.sqlite_write(self.db,self.path,rows=1):
                    self.db.execute("INSERT INTO runs(run_id,boot_id,started_ns) VALUES(?,?,?)", (run_id, boot_id, started_ns))
            self._pruning_until_admitted(write)

    def stop_run(self, run_id, stopped_ns, prunes=None, deadline=None):
        """Mark the run stopped and save its held minute, which is now complete."""
        with self._mutex:
            on_time(deadline)
            prepared=[]
            held=self._held if self._held is not None and self._held["run_id"]==run_id else None
            if held:
                rows=held_rows(held,deadline)
                keep=top_consumers(rows)|held["own"]
                groups=defaultdict(list)
                for row in rows:
                    if row["entity"] in keep:groups[(row["source"],row["component"],row["entity"])].append(row)
                for key,group in groups.items():
                    on_time(deadline)
                    prepared.append((run_id,held["lower"],held["upper"],"minute",held["bucket"],*key,pack_summaries(group)))
            def write():
                with self.budget.sqlite_write(self.db,self.path,rows=1+len(prepared),payload_bytes=sum(len(row[-1]) for row in prepared)):
                    self.db.execute("BEGIN IMMEDIATE")
                    try:
                        self._interrupt_at(deadline)
                        self.db.executemany(UPSERT_SUMMARY,prepared)
                        self.db.execute("UPDATE runs SET stopped_ns=? WHERE run_id=?", (stopped_ns,run_id))
                        self.db.execute("COMMIT")
                    except BaseException as exc:
                        self._rollback(exc,deadline)
                        raise
                    finally:
                        self.db.set_progress_handler(None,0)
            self._pruning_until_admitted(write,prunes)
            if held:self._held=None

    def _interrupt_at(self,deadline):
        """Interrupt SQLite work that would outlast a stopping save's deadline."""
        if deadline is not None:self.db.set_progress_handler(lambda:time.monotonic()>=deadline,1000)

    def _rollback(self,exc,deadline):
        self.db.set_progress_handler(None,0)
        if self.db.in_transaction:self.db.execute("ROLLBACK")
        if isinstance(exc,sqlite3.OperationalError):on_time(deadline)

    def _pruning_until_admitted(self,write,prunes=None):
        """Run a write, pruning the oldest history between attempts until it fits,
        or until `prunes` batches have been pruned."""
        while True:
            try:
                return write()
            except (sqlite3.OperationalError,BudgetExceeded) as exc:
                if not self._capacity_error(exc) or prunes==0 or not self.prune_oldest():raise
                if prunes is not None:prunes-=1

    def spill(self, run_id, stopped_ns, payloads):
        """Keep a stopping run's unsaved readings and held minute for the next start.

        Each stop writes its own private file, created exclusively inside the
        budget, so it never replaces an earlier stop's file."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}",run_id):raise ValueError("invalid run id")
        with self._mutex:
            held=self._held if self._held is not None and self._held["run_id"]==run_id else None
            committed=self.db.execute("SELECT last_sequence FROM runs WHERE run_id=?",(run_id,)).fetchone()[0]
            data=encode({"format":"machine-audit-unsaved-v1","run_id":run_id,"stopped_ns":stopped_ns,"committed":committed,
                         "records":[base64.b64encode(p).decode() for p in payloads],
                         "held":None if held is None else {"bucket":held["bucket"],"lower":held["lower"],"upper":held["upper"],
                             "own":sorted(held["own"]),"rows":[[*key,base64.b64encode(payload).decode()] for key,payload in held["rows"].items()]}}).encode()
            path=self.path.parent/f"unsaved-{run_id}-{stopped_ns}.json"
            with self.budget.reserve(len(data)):
                fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                try:
                    with os.fdopen(fd,"wb") as stream:
                        stream.write(data);stream.flush();os.fsync(stream.fileno())
                except BaseException:
                    path.unlink(missing_ok=True);raise
            return path

    def recover_unsaved(self):
        """Save each spilled stop under its own run, as its next save would have, then
        remove the file. Readings saved since the spill are not saved twice."""
        with self._mutex:
            for path in sorted(self.path.parent.glob("unsaved-*.json")):
                with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),"rb") as stream:
                    data=json.loads(stream.read())
                if data.get("format")!="machine-audit-unsaved-v1":raise ValueError("unsupported_unsaved_file")
                run_id,held=data["run_id"],data["held"]
                last=self.db.execute("SELECT last_sequence FROM runs WHERE run_id=?",(run_id,)).fetchone()[0]
                records=[r for r in (Record.unpack(base64.b64decode(p)) for p in data["records"]) if r.sequence>last]
                # A save after the spill already merged newer data; that held minute is stale.
                self._held=None if held is None or last!=data["committed"] else dict(
                    run_id=run_id,bucket=held["bucket"],lower=held["lower"],upper=held["upper"],own=set(held["own"]),
                    rows={tuple(row[:3]):base64.b64decode(row[3]) for row in held["rows"]})
                self.append(records)
                self.stop_run(run_id,data["stopped_ns"])
                path.unlink()

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

    def append(self, records: list[Record], *, commit_lock=None, on_commit=None, prunes=None, deadline=None):
        """Save records. `prunes` bounds the pruning a stopping save may do; it then
        skips proactive pruning as well. A save still unfinished at `deadline`
        raises TimeoutError("stop_deadline") and saves nothing."""
        if not records:
            return
        with self._mutex:
            for i, r in enumerate(records):
                if r.sequence < 1 or (i and (r.run_id != records[0].run_id or r.boot_id != records[0].boot_id or r.sequence <= records[i-1].sequence)):
                    raise ValueError("one append requires one run and increasing positive sequences")
            raw = [(r, r.packed) for r in records]
            derived = points(records, self._previous(records))
            summaries = defaultdict(list)
            minutes=defaultdict(list)
            own=set()
            coarser=defaultdict(list)
            for bucket,row in rollups(derived,MINUTE):
                if row["scope"] not in HOUR_ONLY_SCOPES:
                    if ranked(row["component"],row["entity"]):minutes[bucket].append(row)
                    else:summaries[("minute",bucket,row["source"],row["component"],row["entity"])].append(row)
                    if row["scope"]=="managed_process":own.add(row["entity"])
                    coarser[("15-minute",bucket//QUARTER*QUARTER,row["source"],row["component"],row["entity"],row["signal"],row["unit"],row["scope"],row["kind"],row["scale"])].append(row)
                coarser[("hour",bucket//HOUR*HOUR,row["source"],row["component"],row["entity"],row["signal"],row["unit"],row["scope"],row["kind"],row["scale"])].append(row)
            for key,rows in coarser.items():
                on_time(deadline)
                summaries[key[:5]].append(merge_summaries(rows) if len(rows)>1 else rows[0])
            if prunes is None:self.maintain()
            held,lowers=self._top_minutes(records,minutes,own,summaries,deadline)
            on_time(deadline)
            if prunes==0:
                # A stopping save the limit refuses before merging fails now, not after
                # merging every row: its full reservation is larger still.
                _,count,payload=self._bound(records,raw,summaries)
                with self.budget.sqlite_write(self.db,self.path,rows=count,payload_bytes=payload):pass
            # Prepare once: a save that must prune to fit repeats only admission and commit.
            batch=self._prepare(records,raw,summaries,lowers,deadline)
            self._pruning_until_admitted(lambda:self._write(records,raw,batch,commit_lock,on_commit,deadline),prunes)
            self._held=held

    @staticmethod
    def _capacity_error(exc):
        return getattr(exc,"sqlite_errorcode",None)==sqlite3.SQLITE_FULL or str(exc) in {"managed_storage_limit","sqlite_checkpoint_headroom"}

    def _top_minutes(self,records,minutes,own,summaries,deadline=None):
        """Add each complete minute's top consumers to summaries; return the minute to hold.

        Saves are not aligned with minutes. The run's newest minute is held here,
        packed, until a later save completes it, so its rows are neither saved nor
        counted at every save. Ranking then saves only its top consumers, so a
        saved row always holds its whole minute. Later data for a ranked minute
        extends only the rows it kept. Stopping the run saves the held minute; a
        crash loses only its minute rows, never its readings or coarser rows."""
        first,last=records[0],records[-1]
        held=self._held if self._held is not None and self._held["run_id"]==first.run_id else None
        if not minutes:return held,{}
        lowers={};buckets=set(minutes)|({held["bucket"]} if held else set())
        newest=max(buckets)
        for bucket in sorted(buckets):
            if bucket==newest and bucket not in minutes:continue  # only older data arrived
            rows=minutes.get(bucket,[]);mine=own;lower=first.sequence
            if held and held["bucket"]==bucket:
                rows=held_rows(held,deadline)+rows;mine=own|held["own"];lower=held["lower"]
            if bucket==newest:
                groups=defaultdict(dict)
                for row in rows:
                    on_time(deadline)
                    group=groups[(row["source"],row["component"],row["entity"])];k=signal_key(row)
                    group[k]=merge_summaries([group[k],row]) if k in group else row
                packed={}
                for key,group in groups.items():
                    on_time(deadline)
                    packed[key]=pack_summaries(list(group.values()))
                held=dict(run_id=first.run_id,bucket=bucket,own=mine,lower=lower,upper=last.sequence,rows=packed)
                continue
            keep={entity for entity, in self.db.execute("""SELECT entity FROM summaries WHERE run_id=? AND resolution='minute'
                AND bucket_ns=? AND component!='self' AND (entity LIKE 'process:%' OR entity LIKE 'program:%')""",(first.run_id,bucket))}
            keep=keep or top_consumers(rows)|mine
            for row in rows:
                if row["entity"] in keep:
                    key=("minute",bucket,row["source"],row["component"],row["entity"])
                    summaries[key].append(row);lowers[key]=lower
        return held,lowers

    @staticmethod
    def _bound(records,raw,summaries):
        """The write's catalog, counted rows and the payload known before merging."""
        components=sum(len({r.reading.component,*(field_component(f,r.reading.component) for f in r.reading.fields)}) for r in records)
        catalog={(row["source"],row["signal"],row["component"],row["kind"],row["unit"],row["scope"],row["scale"]) for rows in summaries.values() for row in rows}
        count=2+len(records)+3*components+len(summaries)+len(catalog)+len({r.reading.source for r in records})+sum(any(f.kind=='event' for f in r.reading.fields) for r in records)
        return catalog,count,3*sum(len(p) for _,p in raw)

    def _prepare(self,records,raw,summaries,lowers,deadline=None):
        """Merge each summary row with its saved row and count the write's bound."""
        first,last=records[0],records[-1]
        catalog,count,payload=self._bound(records,raw,summaries)
        prepared=[];replaced_payload=0
        for (resolution,bucket,source,component,entity),rows in summaries.items():
            on_time(deadline)
            lower=lowers.get((resolution,bucket,source,component,entity),first.sequence)
            old=self.db.execute("SELECT sequence_min,payload FROM summaries WHERE run_id=? AND resolution=? AND bucket_ns=? AND source=? AND component=? AND entity=?",(first.run_id,resolution,bucket,source,component,entity)).fetchone()
            if old:replaced_payload+=len(old[1])
            merged={signal_key(row):row for row in unpack_summaries(old[1])} if old else {}
            for row in rows:
                k=signal_key(row)
                merged[k]=merge_summaries([merged[k],row]) if k in merged else row
            if old:lower=min(lower,old[0])
            prepared.append((first.run_id,lower,last.sequence,resolution,bucket,source,component,entity,pack_summaries(list(merged.values()))))
        for source in {r.reading.source for r in records}:
            old=self.db.execute('SELECT length(payload) FROM baselines WHERE source=?',(source,)).fetchone()
            if old:replaced_payload+=old[0]
        payload+=sum(len(row[-1]) for row in prepared)+replaced_payload
        return prepared,catalog,count,payload

    def _write(self,records,raw,batch,commit_lock,on_commit,deadline=None):
        prepared,catalog,count,payload=batch
        with self.budget.sqlite_write(self.db,self.path,rows=count,payload_bytes=payload):
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._interrupt_at(deadline)
                first, last = records[0], records[-1]
                self.db.execute("INSERT OR IGNORE INTO runs(run_id,boot_id,started_ns) VALUES(?,?,?)", (first.run_id,first.boot_id,first.reading.start.wall_ns))
                saved, boot = self.db.execute("SELECT last_sequence,boot_id FROM runs WHERE run_id=?",(first.run_id,)).fetchone()
                if boot!=first.boot_id:raise ValueError("run boot identity changed")
                if first.sequence <= saved:
                    raise ValueError("sequence already committed")
                self.db.executemany("INSERT INTO readings VALUES(?,?,?,?,?,?)",[(r.run_id,r.sequence,r.reading.end.wall_ns,r.reading.source,r.reading.component,p) for r,p in raw])
                self.db.executemany("INSERT INTO reading_components VALUES(?,?,?,?)",[(r.run_id,r.sequence,c,int(not r.reading.fields or any(field_component(f,r.reading.component)==c and is_series(c,f.entity,f.scope) for f in r.reading.fields))) for r,_ in raw for c in {r.reading.component,*(field_component(f,r.reading.component) for f in r.reading.fields)}])
                self.db.executemany(UPSERT_SUMMARY,prepared)
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
                    statuses=defaultdict(int);gaps=defaultdict(int)
                    for r in group:
                        statuses[r.reading.status]+=1
                        for f in r.reading.fields:
                            if f.status not in {"observed","measured_zero"}:statuses[f.status]+=1
                        # A fieldless reading's reason (no device, helper missing) is
                        # the explanation coverage gives without decoding the reading.
                        if not r.reading.fields and r.reading.status not in {"observed","measured_zero"}:
                            gaps[(r.reading.status,r.reading.reason or "source_status")]+=1
                    first_ns=min(r.reading.start.wall_ns for r in group)
                    last_ns=max(r.reading.end.wall_ns for r in group)
                    data={"statuses":dict(statuses),"reads":len(group)}
                    if gaps:data["gaps"]=[[status,reason,count] for (status,reason),count in sorted(gaps.items())]
                    self.db.execute("INSERT INTO coverage VALUES(?,?,?,?,?,?,?,?)",(first.run_id,last.sequence,resolution,source,component,first_ns,last_ns,encode(data)))
                self.db.execute("UPDATE runs SET last_sequence=?,last_saved_ns=? WHERE run_id=?",(last.sequence,max(r.reading.end.wall_ns for r in records),first.run_id))
                with commit_lock if commit_lock is not None else nullcontext():
                    self.db.execute("COMMIT")
                    if on_commit is not None:on_commit()
            except BaseException as exc:
                self._rollback(exc,deadline)
                raise
            finally:
                self.db.set_progress_handler(None,0)

    def maintain(self, required_bytes=0):
        """Prune raw, then minute, 15-minute, hour and finally rare incident history.

        Work starts well before the cap; if reserve cannot fit, fail without
        removing a file or discarding the collector's pending batch.
        """
        target = self.budget.limit_bytes*.90
        while self.budget.usage()["bytes"] + required_bytes > target:
            if not self.prune_oldest():
                break

    @contextmanager
    def _prune_precheck(self):
        """Bound only the read-only search; interruption never changes history."""
        steps=0;expired=False
        deadline=time.monotonic()+PRUNE_PRECHECK_SECONDS
        def progress():
            nonlocal steps,expired
            steps+=1000
            expired=steps>PRUNE_PRECHECK_STEPS or time.monotonic()>=deadline
            return int(expired)
        self.db.set_progress_handler(progress,1000)
        try:
            yield deadline
        except sqlite3.OperationalError:
            if expired:raise BudgetExceeded("prune_precheck_limit") from None
            raise
        finally:
            self.db.set_progress_handler(None,0)

    def _covered_raw(self,deadline,preserved):
        """The oldest raw window whose every reading is proven summarized, and whether
        an uncovered window came first. Uncovered windows found are added to
        `preserved`; windows already recorded as preserved are passed over, so each
        search resumes where the last one stopped."""
        skipped=False
        row = self.db.execute("SELECT MIN(wall_ns) FROM readings").fetchone()
        while row[0] is not None:
            if time.monotonic()>=deadline:raise BudgetExceeded("prune_precheck_limit")
            start = row[0]
            resume = self.db.execute("SELECT max(end_ns) FROM retention WHERE kind='raw_preserved' AND start_ns<=? AND end_ns>?",(start,start)).fetchone()[0]
            if resume is not None:
                skipped=True
                row=self.db.execute('SELECT MIN(wall_ns) FROM readings WHERE wall_ns>=?',(resume,)).fetchone()
                continue
            end = (start // MINUTE + 1) * MINUTE
            # Keep every proof condition, but seek recent coverage first.
            # EXISTS discards ORDER BY; the non-null scalar preserves it.
            missing = self.db.execute("""SELECT 1 FROM readings r WHERE wall_ns>=? AND wall_ns<? AND
            (SELECT s.start_ns FROM coverage s WHERE s.run_id=r.run_id AND s.resolution='minute'
             AND s.sequence_max>=r.sequence AND s.source=r.source AND s.start_ns<=r.wall_ns AND s.end_ns>=r.wall_ns
             ORDER BY s.start_ns DESC LIMIT 1) IS NULL LIMIT 1""",(start,end)).fetchone()
            if not missing:return (start,end),skipped or bool(preserved)
            if preserved and preserved[-1][1]==start//MINUTE*MINUTE:
                preserved[-1]=(preserved[-1][0],end)
            else:preserved.append((start,end))
            row=self.db.execute('SELECT MIN(wall_ns) FROM readings WHERE wall_ns>=?',(end,)).fetchone()
        return None,skipped or bool(preserved)

    def _prune_candidates(self):
        preserved=[]
        exhausted=False
        try:
            with self._prune_precheck() as deadline:
                window,skipped_raw=self._covered_raw(deadline,preserved)
        except BudgetExceeded as exc:
            # The search ran out before a covered window: prune coarser history now,
            # and record the uncovered windows found so the next search goes further.
            if str(exc)!="prune_precheck_limit":raise
            window,skipped_raw,exhausted=None,True,True
        with self._prune_precheck():
            batch=self._prune_batch(window,preserved,skipped_raw)
        if batch is None and exhausted:raise BudgetExceeded("prune_precheck_limit")
        return batch

    def _prune_batch(self,window,preserved,skipped_raw):
        summary_heads=[]
        if window:
            kind,(start,end)="raw",window
            table, condition, args = "readings", "wall_ns>=? AND wall_ns<?", (start,end)
        else:
            # Uncovered windows remain intact; covered raw still comes
            # before summaries, even when it follows a retained window.
            for kind,width in WIDTHS.items():
                summary_heads=self._summary_heads(kind)
                if summary_heads:
                    start=min(t for _,t in summary_heads)
                    end,table=start+width,"summaries"
                    break
            else:
                row=self.db.execute("SELECT MIN(wall_ns) FROM incidents").fetchone()
                if row[0] is None:return None
                kind,start,end="incident",row[0],(row[0]//MINUTE+1)*MINUTE
                table,condition,args="incidents","wall_ns < ?",(end,)
        if summary_heads:
            rows=[]
            for component,first in summary_heads:
                if first<end:
                    rows.extend(self.db.execute('''SELECT rowid,length(payload) FROM summaries
                        WHERE resolution=? AND component=? AND bucket_ns<? LIMIT ?''',
                        (kind,component,end,PRUNE_BATCH-len(rows))).fetchall())
                if len(rows)==PRUNE_BATCH:break
        else:
            rows=self.db.execute(f"SELECT rowid,length(payload) FROM {table} WHERE {condition} LIMIT ?",(*args,PRUNE_BATCH)).fetchall()
        page=self.db.execute('PRAGMA page_size').fetchone()[0]
        preserved=[(start,end) for start,end in preserved if not self.db.execute(
            "SELECT 1 FROM retention WHERE kind='raw_preserved' AND start_ns<=? AND end_ns>=?",(start,end)).fetchone()]
        return kind,start,end,table,rows,preserved,skipped_raw,page

    def prune_oldest(self):
        with self._mutex:
            batch=self._prune_candidates()
            if batch is None:return self._reclaim_free_pages()
            kind,start,end,table,rows,preserved,skipped_raw,page=batch
            while rows:
                payload_pages=sum((size+page-1)//page for _,size in rows)
                coverage=[]
                if kind in ('minute','hour'):
                    predicate="resolution='minute' AND end_ns<?" if kind=='minute' else 'end_ns<?'
                    with self._prune_precheck():
                        coverage=self.db.execute(f'SELECT rowid,length(payload) FROM coverage WHERE {predicate} LIMIT ?',
                                                 (end,len(rows))).fetchall()
                coverage_pages=sum((size+page-1)//page for _,size in coverage)
                vacuum_steps=min(PRUNE_BATCH,payload_pages+len(rows))
                # Bounded delete/index work, overflow pages, then bounded page moves.
                frames=64+(len(rows)+len(coverage)+len(preserved))*PRUNE_ROW_PAGES+payload_pages+coverage_pages+8*vacuum_steps
                try:
                    with self.budget.sqlite_write(self.db,self.path,0,max_growth_bytes=8*page,dirty_pages=frames):
                        self.db.execute("BEGIN IMMEDIATE")
                        try:
                            ids=[row[0] for row in rows]
                            placeholders=','.join('?' for _ in ids)
                            if table=="readings":
                                self.db.execute(f"DELETE FROM reading_components WHERE (run_id,sequence) IN (SELECT run_id,sequence FROM readings WHERE rowid IN ({placeholders}))",ids)
                            self.db.execute(f"DELETE FROM {table} WHERE rowid IN ({placeholders})",ids)
                            self.db.executemany('DELETE FROM coverage WHERE rowid=?',((row[0],) for row in coverage))
                            self.db.executemany("INSERT INTO retention VALUES('raw_preserved',?,?,'missing_summary_coverage')",preserved)
                            reason='storage_budget_raw_uncovered' if skipped_raw and kind!='raw' else 'storage_budget'
                            previous=self.db.execute("SELECT rowid FROM retention WHERE kind=? AND start_ns<=? AND end_ns>=? AND reason=?",(kind,start,start,reason)).fetchone()
                            if previous:self.db.execute("UPDATE retention SET end_ns=max(end_ns,?) WHERE rowid=?",(end,previous[0]))
                            else:self.db.execute("INSERT INTO retention VALUES(?,?,?,?)",(kind,start,end,reason))
                            for _ in range(min(vacuum_steps,self.db.execute("PRAGMA freelist_count").fetchone()[0])):
                                self.db.execute("PRAGMA incremental_vacuum(1)")
                            self.db.execute("COMMIT")
                        except BaseException:
                            if self.db.in_transaction:self.db.execute("ROLLBACK")
                            raise
                    return True
                except (sqlite3.OperationalError,BudgetExceeded) as exc:
                    if not self._capacity_error(exc) or len(rows)==1:raise
                    rows=rows[:max(1,len(rows)//2)]
            return False

    def _reclaim_free_pages(self):
        steps=min(PRUNE_BATCH,self.db.execute('PRAGMA freelist_count').fetchone()[0])
        before=self.db.execute('PRAGMA page_count').fetchone()[0]
        while steps:
            try:
                with self.budget.sqlite_write(self.db,self.path,max_growth_bytes=0,dirty_pages=64+8*steps):
                    self.db.execute('BEGIN IMMEDIATE')
                    try:
                        for _ in range(steps):self.db.execute('PRAGMA incremental_vacuum(1)')
                        self.db.execute('COMMIT')
                    except BaseException:
                        if self.db.in_transaction:self.db.execute('ROLLBACK')
                        raise
                return self.db.execute('PRAGMA page_count').fetchone()[0]<before
            except (sqlite3.OperationalError,BudgetExceeded) as exc:
                if not self._capacity_error(exc) or steps==1:raise
                steps=max(1,steps//2)
        return False

    def _summary_heads(self,resolution):
        # summaries_time orders component before time. Seek each component's
        # first row instead of rescanning all summaries for every prune batch.
        result=[];component=''
        while True:
            row=self.db.execute('''SELECT component,bucket_ns FROM summaries
                WHERE resolution=? AND component>? ORDER BY component,bucket_ns LIMIT 1''',
                (resolution,component)).fetchone()
            if row is None:return result
            component=row[0];result.append(row)

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
                pruned=[dict(r) for r in source.execute("SELECT * FROM retention WHERE kind!='raw_preserved' ORDER BY start_ns LIMIT 1001")]
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
