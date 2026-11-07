"""SQLite readings and cumulative summaries share every atomic watermark."""
from __future__ import annotations
import base64
from collections import defaultdict
from contextlib import contextmanager, nullcontext
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import zlib
from .budget import Budget, BudgetExceeded
from .derive import points, rollups, merge_summaries, union_ns
from .model import PROCESS_ENTITIES, Record, field_component, is_series, who_rank
from .summary_dictionary import SUMMARY_DICTIONARY

MINUTE = 60_000_000_000
QUARTER = 15 * MINUTE
HOUR = 60 * MINUTE
# Stored summary resolutions, finest first.
WIDTHS = {"minute": MINUTE, "15-minute": QUARTER, "hour": HOUR}
# Retention, each from the question it serves (storage-approach.md#retention), counted back
# from the newest save. Per-pass process detail answers an event noticed the same day or the
# next morning; host readings and minute and 15-minute rows the last few days, so Friday can
# still be looked at on Monday. Hour rows and incidents are the long history: no retention.
PROCESS_RAW_RETENTION = 24 * HOUR
RECENT_RETENTION = 3 * 24 * HOUR
# The emergency order, finest and cheapest to lose first, and each tier's retention.
TIERS = (("process_raw", PROCESS_RAW_RETENTION), ("raw", RECENT_RETENTION), ("minute", RECENT_RETENTION),
         ("15-minute", RECENT_RETENTION), ("hour", None), ("incident", None))
# Above this share of the cap, history goes oldest first by TIERS, each deletion recorded
# as "storage_cap": an emergency, not a policy. The rest of the cap leaves room for the
# largest counted write's journal and checkpoint (a 4,096-row prune) and a scanner write.
EMERGENCY_LEVEL = 0.9
# A bucket keeps a process or program as its own row when it was at least this share of the
# bucket's use of a component by its kind; the rest of each kind become one row, so totals
# still add up (storage-approach.md#main-contributors).
SHARE = 0.01
MEMORY_RANK = ("process.phys_footprint", "process.resident_size")  # Mac footprint, else Linux resident set
IO_RANK = ("process.diskio_bytesread", "process.diskio_byteswritten")
REST_META = ("source", "component", "signal", "scope", "kind", "unit", "scale", "rate_unit", "additive", "method")
# The retention reason of buckets converted to main contributors and rest. The span stays
# answered, so coverage lists the record and reports no pruned gap.
CONVERTED = "main_contributors_kept"
# 3: process rows ranked by share with rest rows. A store saved at 2 kept a row for every
# process at 15-minute and hour grain; the collector converts it once, then marks it 3.
STORE_VERSION = 3
# Per-scope summary resolution (storage-approach.md): these scopes keep hour summaries only.
# Raw readings still answer recent intervals.
HOUR_ONLY_SCOPES = frozenset({"unit_detail"})
PRUNE_BATCH = 4096
PRUNE_ROW_PAGES = 16
PRUNE_PRECHECK_STEPS = 1_000_000
PRUNE_PRECHECK_SECONDS = 1.0
CHECKPOINT = "machine-audit-unsaved-v2"
CHECKPOINT_V1 = "machine-audit-unsaved-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, boot_id TEXT NOT NULL, started_ns INTEGER NOT NULL, stopped_ns INTEGER, last_sequence INTEGER NOT NULL DEFAULT 0, last_saved_ns INTEGER);
CREATE TABLE IF NOT EXISTS readings(run_id TEXT NOT NULL, sequence INTEGER NOT NULL, wall_ns INTEGER NOT NULL, source TEXT NOT NULL, component TEXT NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(run_id,sequence));
CREATE TABLE IF NOT EXISTS reading_components(run_id TEXT NOT NULL, sequence INTEGER NOT NULL, component TEXT NOT NULL, is_series INTEGER NOT NULL, PRIMARY KEY(component,run_id,sequence));
-- Series and coverage check each window reading's components by record.
CREATE INDEX IF NOT EXISTS reading_components_record ON reading_components(run_id,sequence);
CREATE INDEX IF NOT EXISTS readings_time ON readings(wall_ns,source);
CREATE INDEX IF NOT EXISTS readings_source_time ON readings(source,wall_ns);
CREATE TABLE IF NOT EXISTS signal_catalog(source TEXT NOT NULL, signal TEXT NOT NULL, component TEXT NOT NULL, kind TEXT NOT NULL, unit TEXT NOT NULL, scope TEXT NOT NULL, scale REAL NOT NULL, PRIMARY KEY(source,signal,scope));
CREATE TABLE IF NOT EXISTS summaries(id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, sequence_min INTEGER NOT NULL, sequence_max INTEGER NOT NULL, resolution TEXT NOT NULL, bucket_ns INTEGER NOT NULL, source TEXT NOT NULL, component TEXT NOT NULL, entity TEXT NOT NULL, payload BLOB NOT NULL, who_rank REAL);
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
    compressor = zlib.compressobj(6, zdict=SUMMARY_DICTIONARY)
    return compressor.compress(encode([2, [[keys,values] for keys,values in groups.items()]]).encode()) + compressor.flush()


UPSERT_SUMMARY = ("INSERT INTO summaries(run_id,sequence_min,sequence_max,resolution,bucket_ns,source,component,entity,who_rank,payload) VALUES(?,?,?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(run_id,resolution,bucket_ns,source,component,entity) DO UPDATE SET "
                  "sequence_min=excluded.sequence_min,sequence_max=excluded.sequence_max,who_rank=excluded.who_rank,payload=excluded.payload")


def summary_values(run_id, lower, upper, resolution, bucket, source, component, entity, rows):
    """One bundle to upsert, with the bound `who` ranks it by; the payload stays last."""
    return (run_id, lower, upper, resolution, bucket, source, component, entity, who_rank(rows), pack_summaries(rows))


def signal_key(row):
    return tuple(row[k] for k in ("signal", "unit", "kind", "scope", "scale"))


def record_components(record):
    """Each component a reading's fields go to, and whether any of its fields there is a
    host series: worked out once per reading, not once per field per use."""
    default = record.reading.component
    series = {default: not record.reading.fields}
    for f in record.reading.fields:
        component = field_component(f, default)
        series[component] = series.get(component, False) or is_series(component, f.entity, f.scope)
    return series


def ranked(component, entity):
    """Whether this row is a process or program that a bucket keeps only when it mattered."""
    return component != "self" and entity.startswith(PROCESS_ENTITIES)


def use(rows):
    """Each entity's use of each component in a bucket: CPU seconds, bytes read and
    written, and footprint summed over passes (Mac footprint, else Linux resident set),
    keyed by (component, entity). Rest rows count as their kind's own entity."""
    cpu_io, memory = defaultdict(float), {}
    for row in rows:
        key, signal = (row["component"], row["entity"]), row["signal"]
        if signal == "process.cpu" and row["total"] is not None:
            cpu_io[key] += row["total"]*row["scale"]
        elif signal in IO_RANK and row["total"] is not None:
            cpu_io[key] += row["total"]
        elif signal in MEMORY_RANK and row["sum"] is not None:
            memory[key] = max(memory.get(key, (-2, 0)), (-MEMORY_RANK.index(signal), row["sum"]))
    return {**cpu_io, **{key: value for key, (_, value) in memory.items()}}


def kind_of(entity):
    return "program" if entity.startswith(("program:", "rest:program")) else "process"


def main_contributors(rows):
    """The (component, entity) pairs a bucket keeps, and each component and kind's cut:
    SHARE of that kind's whole use of the component, a rest row's included."""
    used = use(rows)
    totals = defaultdict(float)
    for (component, entity), value in used.items():totals[(component, kind_of(entity))] += value
    cuts = {key: SHARE*total for key, total in totals.items()}
    keep = {key for key, value in used.items() if not key[1].startswith("rest:") and value > 0 and value >= cuts[(key[0], kind_of(key[1]))]}
    return keep, cuts


def rest_rows(dropped, rows, cuts):
    """One row per source, component, kind and signal for the entities a bucket did not
    keep, read as one entity: its count is the bucket's passes, so its mean is their
    combined level and its total their combined total. `entities` says how many it holds;
    `cut` is the use an entity needed to be kept."""
    passes = defaultdict(int)
    for row in rows:
        key = (row["source"], row["component"], signal_key(row))
        passes[key] = max(passes[key], row["count"])
    groups = defaultdict(list)
    for row in dropped:groups[(row["source"], row["component"], kind_of(row["entity"]), signal_key(row))].append(row)
    result = []
    for (source, component, kind, key), group in groups.items():
        first, n = group[0], passes[(source, component, key)]
        windows = defaultdict(list)
        for row in group:
            for run, spans in row.get("covered_windows", {}).items():windows[run].extend(spans)
        covered = {run: union_ns(spans) for run, spans in windows.items()}
        totals = [row["total"] for row in group if row["total"] is not None]
        rest = dict({k: first[k] for k in REST_META}, entity="rest:"+kind, label=None, aggregation="rest-of-bucket",
                    cut=cuts.get((component, kind)), members=[],
                    count=n, sample_count=n, sum=sum(row["sum"] for row in group), total=sum(totals) if totals else None,
                    covered_windows=covered, covered_s=sum(b-a for spans in covered.values() for a, b in spans)/1e9,
                    start_ns=min(row["start_ns"] for row in group), end_ns=max(row["end_ns"] for row in group),
                    min=None, max=None, max_ns=None, last=None, last_ns=None, peak_rate=None, peak_ns=None,
                    statuses={}, sleep_s=0.0, above_s={}, histogram_s=[], partial_members=False, value=None)
        rest["entities"] = rest["instance_count"] = sum(row.get("entities", 1) for row in group)
        rest["mean"] = rest["sum"]/n if n else None
        if rest["total"] is not None and rest["covered_s"]:rest["mean_rate"] = rest["total"]*first["scale"]/rest["covered_s"]
        rest["status"] = "observed" if rest["sum"] or rest["total"] else "measured_zero"
        result.append(rest)
    return result


def held_rows(held):
    rows=[]
    for payload in held["rows"].values():
        rows.extend(unpack_summaries(payload))
    return rows


def merged_bundle(payload,rows):
    """A stored bundle's rows, if any, with `rows` merged in: one row per signal."""
    merged={signal_key(row):row for row in unpack_summaries(payload)} if payload else {}
    for row in rows:
        k=signal_key(row)
        merged[k]=merge_summaries([merged[k],row]) if k in merged else row
    return list(merged.values())


def extended(held,bucket,rows,run_id,lower,upper):
    """The bucket held after `rows` join it. Only rows' entities are unpacked, and the
    held bucket passed in is left as it was, so a save that fails changes nothing."""
    previous=held is not None and held["bucket"]==bucket
    packed=dict(held["rows"]) if previous else {}
    groups=defaultdict(list)
    for row in rows:groups[(row["source"],row["component"],row["entity"])].append(row)
    for key,group in groups.items():packed[key]=pack_summaries(merged_bundle(packed.get(key),group))
    return dict(run_id=run_id,bucket=bucket,lower=held["lower"] if previous else lower,upper=upper,rows=packed)


def unpack_summaries(payload, keep=None):
    """Rows of one stored bundle. `keep(signal, kind, component)` drops rows before
    their dictionaries are built."""
    # A stream written before the dictionary simply does not ask for it.
    stream = zlib.decompressobj(zdict=SUMMARY_DICTIONARY)
    data = stream.decompress(payload)
    if not stream.eof:
        raise ValueError("truncated_summary")
    version, groups = json.loads(data)
    if version != 2:
        raise ValueError("unsupported_summary_encoding")
    if keep is None:
        return [dict(zip(keys,values)) for keys,rows in groups for values in rows]
    result=[]
    for keys,rows in groups:
        signal,kind,component=(keys.index(k) for k in ("signal","kind","component"))
        result.extend(dict(zip(keys,values)) for values in rows if keep(values[signal],values[kind],values[component]))
    return result


class Store:
    def __init__(self, path: str | Path, budget: Budget | None = None, *, read_only=False):
        self.path = Path(path)
        if read_only and not self.path.is_file():
            raise FileNotFoundError("no saved Machine-audit history")
        self.budget = budget or Budget(self.path.parent)
        if self.path.parent.resolve() != self.budget.root.resolve() or self.path.is_symlink():
            raise ValueError("store must be a regular file in the budget directory")
        self._mutex = threading.RLock()
        self._held = {}  # resolution -> the run's newest bucket of ranked rows
        self._conversion = None  # where converting a version 2 store resumes
        self.conversion_error = None
        self.cap_pruning = False  # whether the last save had to prune at the emergency level
        if read_only:
            self.db=sqlite3.connect(f"file:{self.path}?mode=ro",uri=True,check_same_thread=False,isolation_level=None)
            if self.db.execute("PRAGMA user_version").fetchone()[0] not in (2,STORE_VERSION):
                self.db.close();raise ValueError("unsupported_private_store_schema")
            return
        with self.budget.reserve(256 * 1024):
            self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=5)
            saved=self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
            version=self.db.execute("PRAGMA user_version").fetchone()[0] if saved else STORE_VERSION
            if version not in (2,STORE_VERSION):
                self.db.close();raise ValueError("unsupported_private_store_schema")
            os.chmod(self.path, 0o600)
            self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript(SCHEMA)
            if "who_rank" not in {row[1] for row in self.db.execute("PRAGMA table_info(summaries)")}:
                # Rows saved before stay NULL and `who` decodes them as it always has.
                self.db.execute("ALTER TABLE summaries ADD COLUMN who_rank REAL")
            # The first ranked bucket dates the column: later NULLs mean nothing ranks.
            self.db.execute("CREATE INDEX IF NOT EXISTS summaries_ranked ON summaries(resolution,bucket_ns) WHERE who_rank IS NOT NULL")
            if not saved:self.db.execute(f"PRAGMA user_version={STORE_VERSION}")
            if version==2:self._conversion=["hour",None,None]
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

    def stop_run(self, run_id, stopped_ns):
        """Mark the run stopped and save its held buckets, which are now complete."""
        self.append([],run_id=run_id,stopped_ns=stopped_ns)

    def _ranked(self,run_id,resolution,bucket,rows):
        """A complete bucket's rows to save: its main contributors as they are, and rest
        rows for the others. Entities it already saved fix who is kept, so late data
        extends them and adds to the rest, and rows an older release saved keep their tail."""
        saved={(component,entity) for component,entity in self.db.execute("""SELECT component,entity FROM summaries
            WHERE run_id=? AND resolution=? AND bucket_ns=? AND component!='self' AND (entity LIKE 'process:%' OR entity LIKE 'program:%')""",
            (run_id,resolution,bucket))}
        groups=defaultdict(list)
        for row in rows:groups[(row["source"],row["component"],row["entity"])].append(row)
        rows=[row for group in groups.values() for row in merged_bundle(None,group)]  # held and new fragments
        keep,cuts=(saved,{}) if saved else main_contributors(rows)
        kept=[row for row in rows if (row["component"],row["entity"]) in keep]
        return kept+rest_rows([row for row in rows if (row["component"],row["entity"]) not in keep],rows,cuts)

    def _stop_rows(self,held,run_id):
        """Each held bucket's main contributors and rest, saved when its run stops, and
        the saved payload they replace."""
        prepared=[];replaced=0
        for resolution,bucket in held.items():
            groups=defaultdict(list)
            for row in self._ranked(run_id,resolution,bucket["bucket"],held_rows(bucket)):
                groups[(row["source"],row["component"],row["entity"])].append(row)
            for key,group in groups.items():
                values,old=self._merged(run_id,(resolution,bucket["bucket"],*key),group,bucket["lower"],bucket["upper"])
                prepared.append(values);replaced+=old
        return prepared,replaced

    def _pruning_until_admitted(self,write):
        """Run a write, pruning the oldest history between attempts until it fits."""
        while True:
            try:
                return write()
            except (sqlite3.OperationalError,BudgetExceeded) as exc:
                if not self._capacity_error(exc) or not self.prune_oldest():raise

    def spill(self, run_id, stopped_ns, payloads):
        """Checkpoint a stopping run: its unsaved readings, held buckets and saved
        sequence, for the next start to save. The file is written under a hidden name
        and published only once complete and synced, so a stop killed mid-write never
        leaves a partial checkpoint for recovery. Publication never replaces another
        stop's file. A killed write leaves its hidden file, which recovery ignores."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}",run_id):raise ValueError("invalid run id")
        with self._mutex:
            committed=self.db.execute("SELECT last_sequence FROM runs WHERE run_id=?",(run_id,)).fetchone()[0]
            data=encode({"format":CHECKPOINT,"run_id":run_id,"stopped_ns":stopped_ns,"committed":committed,
                         "records":[base64.b64encode(p).decode() for p in payloads],
                         "held":{resolution:{"bucket":held["bucket"],"lower":held["lower"],"upper":held["upper"],
                                             "rows":[[*key,base64.b64encode(payload).decode()] for key,payload in held["rows"].items()]}
                                 for resolution,held in self._held.items() if held["run_id"]==run_id}}).encode()
            directory=self.path.parent
            path=directory/f"unsaved-{run_id}-{stopped_ns}.json"
            partial=directory/f".{path.name}.partial"
            # Until the partial name is removed the file has two names, counted twice.
            with self.budget.reserve(2*len(data)):
                fd=os.open(partial,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                try:
                    with os.fdopen(fd,"wb") as stream:
                        stream.write(data);stream.flush();os.fsync(stream.fileno())
                    os.link(partial,path)
                    directory_fd=os.open(directory,os.O_RDONLY)
                    try:os.fsync(directory_fd)
                    finally:os.close(directory_fd)
                finally:
                    partial.unlink(missing_ok=True)
            return path

    def recover_unsaved(self):
        """Save each checkpoint under its own run, then remove it, and return the
        errors of checkpoints kept. Each is saved in one transaction with its run's
        stop, so a run with a stop marker has already saved it."""
        kept=[]
        with self._mutex:
            for path in sorted(self.path.parent.glob("unsaved-*.json")):
                try:
                    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),"rb") as stream:
                        data=json.loads(stream.read())
                    # An older release's checkpoint holds a minute only.
                    if data.get("format")==CHECKPOINT_V1:data["held"]={"minute":data["held"]} if data["held"] else {}
                    elif data.get("format")!=CHECKPOINT:raise ValueError("unsupported_unsaved_file")
                    run_id,stopped_ns,held=data["run_id"],data["stopped_ns"],data["held"]
                    records=[Record.unpack(base64.b64decode(p)) for p in data["records"]]
                    row=self.db.execute("SELECT last_sequence,stopped_ns,boot_id FROM runs WHERE run_id=?",(run_id,)).fetchone()
                    sequences=[r.sequence for r in records]
                    # Its readings must be this run's own unsaved tail, before anything
                    # is saved or the file removed.
                    if (row is None or any(r.run_id!=run_id or r.boot_id!=row[2] for r in records)
                            or sequences!=sorted(set(sequences)) or any(s<=data["committed"] for s in sequences)):
                        raise ValueError("unsaved_checkpoint_mismatch")
                    last,stopped,_=row
                    if stopped is not None:
                        # Saved by an earlier recovery that stopped before removing the file.
                        if stopped!=stopped_ns or last<max((r.sequence for r in records),default=data["committed"]):
                            raise ValueError("unsaved_checkpoint_mismatch")
                    else:
                        # A save after the checkpoint (an older release's recovery) leaves
                        # its held buckets stale; it is kept rather than saved wrong.
                        if last!=data["committed"]:raise ValueError("unsaved_checkpoint_mismatch")
                        self._held={resolution:dict(run_id=run_id,bucket=h["bucket"],lower=h["lower"],upper=h["upper"],
                                                    rows={tuple(row[:3]):base64.b64decode(row[3]) for row in h["rows"]})
                                    for resolution,h in held.items()}
                        try:self.append(records,run_id=run_id,stopped_ns=stopped_ns)
                        finally:self._held={}
                    path.unlink()
                except Exception as exc:
                    kept.append(exc)
        return kept

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

    def append(self, records: list[Record], *, run_id=None, stopped_ns=None, commit_lock=None, on_commit=None):
        """Save records. With `stopped_ns`, the same transaction also saves the held
        buckets and marks the run stopped; `run_id` names the run when there are no
        records."""
        if not records and stopped_ns is None:
            return
        with self._mutex:
            for i, r in enumerate(records):
                if r.sequence < 1 or (i and (r.run_id != records[0].run_id or r.boot_id != records[0].boot_id or r.sequence <= records[i-1].sequence)):
                    raise ValueError("one append requires one run and increasing positive sequences")
            if records and run_id is not None and run_id!=records[0].run_id:
                raise ValueError("records belong to another run")
            run_id=records[0].run_id if records else run_id
            if run_id is None:raise ValueError("a stop requires its run")
            raw = [(r, r.packed, record_components(r)) for r in records]
            summaries = defaultdict(list)
            held={resolution:h for resolution,h in self._held.items() if h["run_id"]==run_id}
            lowers={};catalog=set()
            if records:
                derived = points(records, self._previous(records))
                buckets={resolution:defaultdict(list) for resolution in WIDTHS}
                coarser=defaultdict(list)
                for bucket,row in rollups(derived,MINUTE):
                    # From every row: a signal only held rows carry is still known.
                    catalog.add((row["source"],row["signal"],row["component"],row["kind"],row["unit"],row["scope"],row["scale"]))
                    if row["scope"] not in HOUR_ONLY_SCOPES:
                        if ranked(row["component"],row["entity"]):buckets["minute"][bucket].append(row)
                        else:summaries[("minute",bucket,row["source"],row["component"],row["entity"])].append(row)
                        coarser[("15-minute",bucket//QUARTER*QUARTER,row["source"],row["component"],row["entity"],row["signal"],row["unit"],row["scope"],row["kind"],row["scale"])].append(row)
                    coarser[("hour",bucket//HOUR*HOUR,row["source"],row["component"],row["entity"],row["signal"],row["unit"],row["scope"],row["kind"],row["scale"])].append(row)
                for key,rows in coarser.items():
                    resolution,bucket,source,component,entity=key[:5]
                    row=merge_summaries(rows) if len(rows)>1 else rows[0]
                    if ranked(component,entity):buckets[resolution][bucket].append(row)
                    else:summaries[key[:5]].append(row)
                self.maintain()
                held,lowers=self._top_buckets(records,buckets,summaries,held)
            stop=None
            if stopped_ns is not None:
                stop=(stopped_ns,*self._stop_rows(held,run_id));held={}
            # Prepare once: a save that must prune to fit repeats only admission and commit.
            batch=self._prepare(records,raw,summaries,lowers,catalog) if records else ([],set(),0,0)
            self._pruning_until_admitted(lambda:self._write(records,raw,batch,commit_lock,on_commit,run_id,stop))
            self._held=held

    @staticmethod
    def _capacity_error(exc):
        return getattr(exc,"sqlite_errorcode",None)==sqlite3.SQLITE_FULL or str(exc) in {"managed_storage_limit","sqlite_checkpoint_headroom"}

    def _top_buckets(self,records,buckets,summaries,held):
        """Add each complete bucket's main contributors and rest to summaries; return the
        buckets to hold, per resolution, and each saved row's first sequence.

        Saves are not aligned with buckets. Each resolution's newest bucket is held here,
        packed, until a later save completes it, so its rows are neither saved nor
        counted at every save. Ranking then saves its main contributors and rest, once,
        so a saved row always holds its whole bucket. Later data for a ranked bucket
        extends the rows it kept and its rest. Stopping the run saves the held buckets;
        a crash loses only their process and program rows, never readings or host rows."""
        first,last=records[0],records[-1]
        lowers={};kept={}
        for resolution,arrived in buckets.items():
            current=held.get(resolution)
            if not arrived:
                if current:kept[resolution]=current
                continue
            found=set(arrived)|({current["bucket"]} if current else set())
            newest=max(found)
            for bucket in sorted(found):
                if bucket==newest:
                    if bucket in arrived:current=extended(current,bucket,arrived[bucket],first.run_id,first.sequence,last.sequence)
                    continue
                rows=arrived.get(bucket,[]);lower=first.sequence
                if current and current["bucket"]==bucket:
                    rows=held_rows(current)+rows;lower=current["lower"]
                for row in self._ranked(first.run_id,resolution,bucket,rows):
                    key=(resolution,bucket,row["source"],row["component"],row["entity"])
                    summaries[key].append(row);lowers[key]=lower
            if current and current["bucket"]==newest:kept[resolution]=current
        return kept,lowers

    @staticmethod
    def _bound(records,raw,summaries,catalog):
        """The write's counted rows and the payload known before merging."""
        components=sum(len(parts) for _,_,parts in raw)
        count=2+len(records)+3*components+len(summaries)+len(catalog)+len({r.reading.source for r in records})+sum(any(f.kind=='event' for f in r.reading.fields) for r in records)
        return count,3*sum(len(p) for _,p,_ in raw)

    def _merged(self,run_id,key,rows,lower,upper):
        """One summary row merged with its saved row, and that row's payload size."""
        old=self.db.execute("SELECT sequence_min,payload FROM summaries WHERE run_id=? AND resolution=? AND bucket_ns=? AND source=? AND component=? AND entity=?",(run_id,*key)).fetchone()
        return summary_values(run_id,min(lower,old[0]) if old else lower,upper,*key,merged_bundle(old and old[1],rows)),len(old[1]) if old else 0

    def _prepare(self,records,raw,summaries,lowers,catalog):
        """Merge each summary row with its saved row and count the write's bound."""
        first,last=records[0],records[-1]
        count,payload=self._bound(records,raw,summaries,catalog)
        prepared=[];replaced_payload=0
        for key,rows in summaries.items():
            values,old=self._merged(first.run_id,key,rows,lowers.get(key,first.sequence),last.sequence)
            prepared.append(values);replaced_payload+=old
        for source in {r.reading.source for r in records}:
            old=self.db.execute('SELECT length(payload) FROM baselines WHERE source=?',(source,)).fetchone()
            if old:replaced_payload+=old[0]
        payload+=sum(len(row[-1]) for row in prepared)+replaced_payload
        return prepared,catalog,count,payload

    def _write(self,records,raw,batch,commit_lock,on_commit,run_id,stop=None):
        prepared,catalog,count,payload=batch
        if stop is not None:
            count+=1+len(stop[1]);payload+=stop[2]+sum(len(row[-1]) for row in stop[1])
        with self.budget.sqlite_write(self.db,self.path,rows=count,payload_bytes=payload):
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if records:self._save_records(records,raw,prepared,catalog)
                if stop is not None:
                    self.db.executemany(UPSERT_SUMMARY,stop[1])
                    if self.db.execute("UPDATE runs SET stopped_ns=? WHERE run_id=?",(stop[0],run_id)).rowcount!=1:
                        raise ValueError("unknown run")
                with commit_lock if commit_lock is not None else nullcontext():
                    self.db.execute("COMMIT")
                    if on_commit is not None:on_commit()
            except BaseException:
                if self.db.in_transaction:self.db.execute("ROLLBACK")
                raise

    def _save_records(self,records,raw,prepared,catalog):
        first, last = records[0], records[-1]
        self.db.execute("INSERT OR IGNORE INTO runs(run_id,boot_id,started_ns) VALUES(?,?,?)", (first.run_id,first.boot_id,first.reading.start.wall_ns))
        saved, boot = self.db.execute("SELECT last_sequence,boot_id FROM runs WHERE run_id=?",(first.run_id,)).fetchone()
        if boot!=first.boot_id:raise ValueError("run boot identity changed")
        if first.sequence <= saved:
            raise ValueError("sequence already committed")
        self.db.executemany("INSERT INTO readings VALUES(?,?,?,?,?,?)",[(r.run_id,r.sequence,r.reading.end.wall_ns,r.reading.source,r.reading.component,p) for r,p,_ in raw])
        self.db.executemany("INSERT INTO reading_components VALUES(?,?,?,?)",[(r.run_id,r.sequence,c,int(series)) for r,_,parts in raw for c,series in parts.items()])
        self.db.executemany(UPSERT_SUMMARY,prepared)
        self.db.executemany("INSERT OR REPLACE INTO signal_catalog VALUES(?,?,?,?,?,?,?)",sorted(catalog))
        latest = {}
        coverage = defaultdict(list)
        for r,p,parts in raw:
            latest[r.reading.source]=(r,p)
            for resolution,width in (("minute",MINUTE),("hour",HOUR)):
                for component in parts:
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

    def maintain(self, required_bytes=0):
        """Expire what has outlived its retention, take one step of converting a version 2
        store, and only above the emergency level prune oldest first.

        If reserve cannot fit, fail without removing a file or discarding the
        collector's pending batch.
        """
        self._expire()
        if self._conversion:
            try:self._convert_step()
            except Exception as exc:
                # Converting is never worth a lost save: it stops until the next start and says why.
                self._conversion=None
                self.conversion_error=getattr(exc,"sqlite_errorname",None) or (str(exc) if isinstance(exc,BudgetExceeded) else type(exc).__name__)
        target = self.budget.limit_bytes*EMERGENCY_LEVEL
        if self.budget.usage()["bytes"] + required_bytes <= target:self.cap_pruning=False
        while self.budget.usage()["bytes"] + required_bytes > target:
            if not self.prune_oldest():
                break

    def _expire(self):
        """Delete each tier's history older than its retention, oldest first, one counted
        transaction at a time. A backlog (after an upgrade) clears in the first saves. A
        save at the cap expires nothing and leaves it to the emergency path."""
        newest=self.db.execute("SELECT MAX(last_saved_ns) FROM runs").fetchone()[0]
        if newest is None:return
        for kind,retention in TIERS:
            if retention is None:continue
            # Whole minutes, so no covered raw reading is older than an expired summary.
            before=(newest-retention)//MINUTE*MINUTE
            while True:
                try:
                    batch=self._oldest(kind,before)
                    if batch is None or not self._delete(batch,"retention"):break
                except (sqlite3.OperationalError,BudgetExceeded) as exc:
                    if not self._capacity_error(exc) and str(exc)!="prune_precheck_limit":raise
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

    def _raw_filter(self,kind):
        """The readings of one raw tier: per-process and per-workload passes (every field
        their source has saved is process evidence), or everything else."""
        series=defaultdict(bool)
        for source,component,scope in self.db.execute("SELECT source,component,scope FROM signal_catalog"):
            series[source]|=is_series(component,"host",scope)
        detail=sorted(source for source,value in series.items() if not value)
        return f"source {'IN' if kind=='process_raw' else 'NOT IN'} ({','.join('?'*len(detail))})",detail

    def _covered_raw(self,deadline,preserved,kind):
        """The oldest window of one raw tier whose every reading is proven summarized, and
        whether an uncovered window came first. Uncovered windows found are added to
        `preserved`; windows already recorded as preserved are passed over, so each
        search resumes where the last one stopped."""
        skipped=False
        where,args=self._raw_filter(kind)
        row = self.db.execute(f"SELECT MIN(wall_ns) FROM readings WHERE {where}",args).fetchone()
        while row[0] is not None:
            if time.monotonic()>=deadline:raise BudgetExceeded("prune_precheck_limit")
            start = row[0]
            resume = self.db.execute("SELECT max(end_ns) FROM retention WHERE kind='raw_preserved' AND start_ns<=? AND end_ns>?",(start,start)).fetchone()[0]
            if resume is not None:
                skipped=True
                row=self.db.execute(f'SELECT MIN(wall_ns) FROM readings WHERE wall_ns>=? AND {where}',(resume,*args)).fetchone()
                continue
            end = (start // MINUTE + 1) * MINUTE
            # Keep every proof condition, but seek recent coverage first.
            # EXISTS discards ORDER BY; the non-null scalar preserves it.
            missing = self.db.execute(f"""SELECT 1 FROM readings r WHERE wall_ns>=? AND wall_ns<? AND {where} AND
            (SELECT s.start_ns FROM coverage s WHERE s.run_id=r.run_id AND s.resolution='minute'
             AND s.sequence_max>=r.sequence AND s.source=r.source AND s.start_ns<=r.wall_ns AND s.end_ns>=r.wall_ns
             ORDER BY s.start_ns DESC LIMIT 1) IS NULL LIMIT 1""",(start,end,*args)).fetchone()
            if not missing:return (start,end),skipped or bool(preserved)
            if preserved and preserved[-1][1]==start//MINUTE*MINUTE:
                preserved[-1]=(preserved[-1][0],end)
            else:preserved.append((start,end))
            row=self.db.execute(f'SELECT MIN(wall_ns) FROM readings WHERE wall_ns>=? AND {where}',(end,*args)).fetchone()
        return None,skipped or bool(preserved)

    def _oldest(self,kind,before=math.inf):
        """One tier's oldest batch ending at or before `before`, or None. Raw goes only once
        proven summarized: a raw search also returns the uncovered windows it found, and
        whether it ran out before reaching a covered one."""
        batch=dict(kind=kind,rows=[],preserved=[],skipped=False,exhausted=False)
        if kind in ("process_raw","raw"):
            try:
                with self._prune_precheck() as deadline:
                    window,batch["skipped"]=self._covered_raw(deadline,batch["preserved"],kind)
            except BudgetExceeded as exc:
                if str(exc)!="prune_precheck_limit":raise
                window,batch["skipped"],batch["exhausted"]=None,True,True
            if window is not None and window[1]<=before:
                where,args=self._raw_filter(kind)
                with self._prune_precheck():
                    batch.update(start=window[0],end=window[1],table="readings",rows=self.db.execute(
                        f"SELECT rowid,length(payload) FROM readings WHERE wall_ns>=? AND wall_ns<? AND {where} LIMIT ?",(*window,*args,PRUNE_BATCH)).fetchall())
        elif kind in WIDTHS:
            with self._prune_precheck():
                heads=self._summary_heads(kind)
                if not heads or min(t for _,t in heads)+WIDTHS[kind]>before:return None
                start=min(t for _,t in heads);end=start+WIDTHS[kind];rows=[]
                for component,first in heads:
                    if first<end:
                        rows.extend(self.db.execute("SELECT rowid,length(payload) FROM summaries WHERE resolution=? AND component=? AND bucket_ns<? LIMIT ?",
                            (kind,component,end,PRUNE_BATCH-len(rows))).fetchall())
                    if len(rows)==PRUNE_BATCH:break
            batch.update(start=start,end=end,table="summaries",rows=rows)
        else:
            row=self.db.execute("SELECT MIN(wall_ns) FROM incidents").fetchone()
            if row[0] is None or row[0]>=before:return None
            end=(row[0]//MINUTE+1)*MINUTE
            batch.update(start=row[0],end=end,table="incidents",rows=self.db.execute(
                "SELECT rowid,length(payload) FROM incidents WHERE wall_ns<? LIMIT ?",(end,PRUNE_BATCH)).fetchall())
        batch["preserved"]=[(a,b) for a,b in batch["preserved"] if not self.db.execute(
            "SELECT 1 FROM retention WHERE kind='raw_preserved' AND start_ns<=? AND end_ns>=?",(a,b)).fetchone()]
        return batch if batch["rows"] or batch["preserved"] or batch["skipped"] or batch["exhausted"] else None

    def _delete(self,batch,reason):
        """Delete one batch with its related coverage and retention record; with no rows,
        only record the uncovered windows it found, so the next search passes them."""
        kind,end,preserved=batch["kind"],batch.get("end"),batch["preserved"]
        if not batch["rows"]:
            if preserved:
                with self.budget.sqlite_write(self.db,self.path,rows=len(preserved)):
                    self.db.executemany("INSERT INTO retention VALUES('raw_preserved',?,?,'missing_summary_coverage')",preserved)
            return False
        def coverage(count):
            if kind not in ('minute','hour'):return []
            predicate="resolution='minute' AND end_ns<?" if kind=='minute' else 'end_ns<?'
            with self._prune_precheck():
                return self.db.execute(f'SELECT rowid,length(payload) FROM coverage WHERE {predicate} LIMIT ?',(end,count)).fetchall()
        return bool(self._delete_batch(batch["table"],batch["rows"],(kind,batch["start"],end,reason),preserved,coverage))

    def prune_oldest(self):
        """The emergency path: the first tier with history, oldest batch first, recorded as
        "storage_cap". Uncovered raw stays: summaries pruned while it does are recorded
        "storage_cap_raw_uncovered", with the uncovered windows found on the way."""
        with self._mutex:
            carried=[];uncovered=exhausted=False
            for kind,_ in TIERS:
                batch=self._oldest(kind)
                if batch is None:continue
                exhausted|=batch["exhausted"]
                if not batch["rows"]:
                    carried+=batch["preserved"];uncovered|=batch["skipped"];continue
                batch["preserved"]=carried+batch["preserved"]
                if kind in ("process_raw","raw"):uncovered=False
                if self._delete(batch,"storage_cap_raw_uncovered" if uncovered else "storage_cap"):
                    self.cap_pruning=True
                    return True
            if carried:self._delete(dict(kind="raw",rows=[],preserved=carried),"storage_cap")
            if exhausted:raise BudgetExceeded("prune_precheck_limit")
            return self._reclaim_free_pages()

    def _delete_batch(self,table,rows,retention,preserved=(),coverage=lambda count:(),insert=()):
        """Delete up to `rows` (rowid, payload size) in one counted transaction with the
        related coverage and its retention record, and upsert `insert` summary rows,
        halving the deletes when it does not fit. Return the number of rows deleted."""
        kind,start,end,reason=retention
        page=self.db.execute('PRAGMA page_size').fetchone()[0]
        inserted_pages=sum((len(row[-1])+page-1)//page for row in insert)
        while rows:
            payload_pages=sum((size+page-1)//page for _,size in rows)
            related=coverage(len(rows))
            coverage_pages=sum((size+page-1)//page for _,size in related)
            vacuum_steps=min(PRUNE_BATCH,payload_pages+len(rows))
            # Bounded delete/index work, overflow pages, then bounded page moves.
            frames=64+(len(rows)+len(related)+len(preserved)+len(insert))*PRUNE_ROW_PAGES+payload_pages+coverage_pages+inserted_pages+8*vacuum_steps
            try:
                with self.budget.sqlite_write(self.db,self.path,0,max_growth_bytes=(8+inserted_pages+len(insert))*page,dirty_pages=frames):
                    self.db.execute("BEGIN IMMEDIATE")
                    try:
                        if insert:self.db.executemany(UPSERT_SUMMARY,insert)
                        ids=[row[0] for row in rows]
                        placeholders=','.join('?' for _ in ids)
                        if table=="readings":
                            self.db.execute(f"DELETE FROM reading_components WHERE (run_id,sequence) IN (SELECT run_id,sequence FROM readings WHERE rowid IN ({placeholders}))",ids)
                        self.db.execute(f"DELETE FROM {table} WHERE rowid IN ({placeholders})",ids)
                        self.db.executemany('DELETE FROM coverage WHERE rowid=?',((row[0],) for row in related))
                        self.db.executemany("INSERT INTO retention VALUES('raw_preserved',?,?,'missing_summary_coverage')",preserved)
                        previous=self.db.execute("SELECT rowid FROM retention WHERE kind=? AND start_ns<=? AND end_ns>=? AND reason=?",(kind,start,start,reason)).fetchone()
                        if previous:self.db.execute("UPDATE retention SET end_ns=max(end_ns,?) WHERE rowid=?",(end,previous[0]))
                        else:self.db.execute("INSERT INTO retention VALUES(?,?,?,?)",(kind,start,end,reason))
                        for _ in range(min(vacuum_steps,self.db.execute("PRAGMA freelist_count").fetchone()[0])):
                            self.db.execute("PRAGMA incremental_vacuum(1)")
                        self.db.execute("COMMIT")
                    except BaseException:
                        if self.db.in_transaction:self.db.execute("ROLLBACK")
                        raise
                return len(rows)
            except (sqlite3.OperationalError,BudgetExceeded) as exc:
                if not self._capacity_error(exc) or len(rows)==1:raise
                rows=rows[:max(1,len(rows)//2)]
        return 0

    def _convert_step(self):
        """One bounded step of converting a version 2 store, whose 15-minute and hour rows
        held every process: hour rows first, run by run, bucket by bucket. A step writes at
        most one prune transaction. It needs no record of progress: a converted bucket's
        rest rows hold its cut, so a step after a restart finishes it the same way and
        passes over finished buckets. Once nothing remains the store is marked 3."""
        while self._conversion:
            resolution,run,bucket=self._conversion
            if run is not None:
                bucket=self.db.execute("SELECT MIN(bucket_ns) FROM summaries WHERE run_id=? AND resolution=? AND bucket_ns>=?",(run,resolution,bucket)).fetchone()[0]
            if run is None or bucket is None:
                row=self.db.execute("SELECT run_id FROM runs WHERE run_id>? ORDER BY run_id LIMIT 1",("" if run is None else run,)).fetchone()
                if row is not None:self._conversion=[resolution,row[0],-2**63];continue
                if resolution=="hour":self._conversion=["15-minute",None,None];continue
                with self.budget.sqlite_write(self.db,self.path,rows=1):
                    self.db.execute(f"PRAGMA user_version={STORE_VERSION}")
                self._conversion=None;return
            self._conversion=[resolution,run,bucket]
            if self._convert_bucket(run,resolution,bucket):return
            self._conversion=[resolution,run,bucket+1]

    def _convert_bucket(self,run_id,resolution,bucket):
        """Delete one batch of a saved bucket's process rows that its main contributors do
        not keep, writing its rest rows with the first. Return whether it wrote. Rows that
        cannot be read stay. Only process rows change: Kenny approved deleting those."""
        found=self.db.execute("""SELECT id,component,entity,sequence_min,sequence_max,payload FROM summaries WHERE run_id=? AND resolution=?
            AND bucket_ns=? AND component!='self' AND (entity LIKE 'process:%' OR entity='rest:process')""",(run_id,resolution,bucket)).fetchall()
        rows=defaultdict(list);ids={};unread=set()
        for identifier,component,entity,_,_,payload in found:
            ids[(component,entity)]=(identifier,len(payload))
            try:rows[component].extend(unpack_summaries(payload))
            except (zlib.error,ValueError):unread.add((component,entity))
        doomed=[];rests=[]
        for component,bundle in rows.items():
            rest=[row for row in bundle if row["entity"]=="rest:process"]
            if rest and rest[0].get("cut") is not None:
                # Converted, or part way: the cut decides; its rest already counts the others.
                cut=rest[0]["cut"]
                drop={key for key,value in use(bundle).items() if not key[1].startswith("rest:") and not (value>0 and value>=cut)}
                drop|={(component,row["entity"]) for row in bundle if row["entity"]!="rest:process"}-set(use(bundle))
            else:
                keep,cuts=main_contributors(bundle)
                drop={(component,row["entity"]) for row in bundle if row["entity"]!="rest:process"}-keep
                if not drop:continue
                # A rest without a cut (a recovered tail) joins the new rest as one more member.
                fresh=rest_rows([row for row in bundle if (component,row["entity"]) in drop]+rest,bundle,cuts)
                lower,upper=min(r[3] for r in found),max(r[4] for r in found)
                for source in {row["source"] for row in fresh}:
                    rests.append(summary_values(run_id,lower,upper,resolution,bucket,source,component,"rest:process",[row for row in fresh if row["source"]==source]))
            doomed.extend(ids[key] for key in sorted(drop-unread) if key in ids)
        if not doomed:return False
        return bool(self._delete_batch("summaries",doomed[:PRUNE_BATCH],(resolution,bucket,bucket+WIDTHS[resolution],CONVERTED),insert=rests))

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
                pruned=[dict(r) for r in source.execute("SELECT * FROM retention WHERE kind!='raw_preserved' AND reason!=? ORDER BY start_ns LIMIT 1001",(CONVERTED,))]
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
