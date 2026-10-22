"""Independent non-overlapping source loops; one owner commits the unsaved tail."""
from __future__ import annotations
from collections import Counter, deque
from dataclasses import dataclass
import threading
import time
import sqlite3
import uuid
import os
import fcntl
from .model import Clock, Field, Reading, Record, field_component, is_series
from . import __version__


@dataclass
class SourceState:
    source: object
    due_ns: int = 0
    started_ns: int | None = None
    completed_ns: int | None = None
    running: bool = False
    skipped: int = 0
    status: str = "unobserved"
    lateness_s: float = 0.0
    duration_s: float | None = None
    reads_done: int = 0
    late_reads: int = 0
    cpu_s: float = 0.0


@dataclass(frozen=True, slots=True)
class _Pending:
    payload: bytes
    source: str
    sequence: int
    start_ns: int
    end_ns: int
    components: frozenset[str]
    series_components: frozenset[str]
    status: str
    field_statuses: tuple[tuple[str, int], ...]
    has_events: bool

    @classmethod
    def from_record(cls, record):
        reading = record.reading
        components = {reading.component}
        series_components = set() if reading.fields else {reading.component}
        statuses = Counter()
        has_events = False
        for field in reading.fields:
            component = field_component(field, reading.component)
            components.add(component)
            if is_series(component, field.entity, field.scope):
                series_components.add(component)
            statuses[field.status] += 1
            has_events = has_events or field.kind == "event"
        return cls(record.packed, reading.source, record.sequence,
                   reading.start.wall_ns, reading.end.wall_ns,
                   frozenset(components), frozenset(series_components),
                   reading.status, tuple(statuses.items()), has_events)

    def matches(self, component=None, series_only=False):
        components = self.series_components if series_only else self.components
        return component in components if component else bool(components)


class Collector:
    def __init__(self, store, sources=(), *, boot_id="unknown", run_id=None,
                 clock=Clock.now, tail_limit_bytes=64*1024**2, flush_s=60):
        self.store = store
        self.sources = list(sources)
        if len({s.name for s in self.sources}) != len(self.sources):
            raise ValueError("source names must be unique")
        if any(s.cadence_s <= 0 for s in self.sources):
            raise ValueError("source cadence must be positive")
        self.boot_id = boot_id
        self.run_id = run_id or str(uuid.uuid4())
        self.clock = clock
        self.tail_limit_bytes = tail_limit_bytes
        self.flush_s = flush_s
        self.lock = threading.RLock()
        self._flush_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.tail = []
        self.tail_bytes = 0
        self.sequence = 0
        self.committed = 0
        self.cache = {}
        self.baselines = {}
        self.states = {s.name:SourceState(s) for s in self.sources}
        self.threads = []
        self.last_save_error = None
        self.last_save_ns = None
        self.operations = {"flush_count":0,"flush_elapsed_s":0.0,"query_count":0,"query_elapsed_s":0.0,"http_sent_bytes":0}
        self._started = False
        self._instance_fd = None
        self._paused_since = None
        self._transitions = {}
        self._closer = None
        self._close_result = False

    @staticmethod
    def _save_error(exc):
        if isinstance(exc, sqlite3.Error):
            return getattr(exc,"sqlite_errorname","sqlite_storage_error").lower()
        if isinstance(exc, (OSError, RuntimeError)) and str(exc) in {"managed_storage_limit","free_space_margin","checkpoint_pinned_by_reader","sqlite_checkpoint_headroom","prune_precheck_limit"}:
            return str(exc)
        return type(exc).__name__

    def acquire(self):
        if self._instance_fd is not None:return
        fd=os.open(self.store.path.parent/".collector.lock",os.O_RDWR|os.O_CREAT,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RuntimeError("collector_already_running") from None
        self._instance_fd=fd

    def start(self):
        if self._started:
            raise RuntimeError("collector already started")
        self.acquire()
        self.store.start_run(self.run_id,self.boot_id,self.clock().wall_ns)
        self._started = True
        for source in self.sources:
            thread = threading.Thread(target=self._loop,args=(source,),name=source.name,daemon=True)
            self.threads.append(thread)
            thread.start()

    def ingest(self, reading: Reading, due_wall_ns=None):
        """Keep completed evidence even when concurrent reads reach the high-water mark."""
        with self.lock:
            received = self.clock()
            self.sequence += 1
            record = Record(reading,self.boot_id,self.run_id,self.sequence,
                            received.wall_ns if due_wall_ns is None else due_wall_ns,received)
            size = len(record.packed)
            self.tail.append((_Pending.from_record(record),size))
            self.tail_bytes += size
            cadence = self.states[reading.source].source.cadence_s if reading.source in self.states else 1
            cache = self.cache.setdefault(reading.source,deque(maxlen=2 if any(f.entity.startswith("process:") for f in reading.fields) else min(122, max(2,int(120/cadence)+2))))
            cache.append(record)
            return record

    def _loop(self, source):
        cadence_ns = int(source.cadence_s*1e9)
        due = time.monotonic_ns()
        while not self.stop_event.is_set():
            self.stop_event.wait(max(0,(due-time.monotonic_ns())/1e9))
            if self.stop_event.is_set():
                break
            with self.lock:
                state = self.states[source.name]
                if self.tail_bytes >= self.tail_limit_bytes:
                    state.status = "buffer_suspended"
                    state.skipped += 1
                    self._paused_since = self._paused_since or self.clock().wall_ns
                    due += cadence_ns
                    continue
                state.due_ns = due
                state.started_ns = time.monotonic_ns()
                state.running = True
                state.lateness_s = max(0,(state.started_ns-due)/1e9)
            before = self.clock()
            cpu_before = time.thread_time()
            try:
                reading = source.read()
                if not isinstance(reading,Reading) or reading.source != source.name:
                    raise ValueError("source_contract")
            except Exception as exc:
                reading = Reading(source.name,getattr(source,"component","unknown"),before,self.clock(),
                                  status="denied" if isinstance(exc,PermissionError) else "failed",
                                  reason=type(exc).__name__)
            self.ingest(reading, before.wall_ns-int(state.lateness_s*1e9))
            with self.lock:
                state.reads_done += 1
                state.late_reads += int(state.lateness_s > 0.001)
                state.cpu_s += time.thread_time()-cpu_before
                state.completed_ns = time.monotonic_ns()
                state.duration_s = (state.completed_ns-state.started_ns)/1e9
                state.running = False
                state.status = reading.status
                cadence_ns=int(source.cadence_s*1e9)
                due += cadence_ns
                missed = max(0,(state.completed_ns-due)//cadence_ns+1)
                state.skipped += missed
                due += missed*cadence_ns

    def source_status(self):
        now = time.monotonic_ns()
        with self.lock:
            return [{"source":name,"component":getattr(s.source,"component","unknown"),
                     "status":"hung" if s.running and now-s.started_ns > s.source.cadence_s*10e9 else s.status,
                     "running":s.running,"lateness_s":s.lateness_s,"duration_s":s.duration_s,
                     "skipped":s.skipped,"cadence_s":s.source.cadence_s,
                     "reads_done":s.reads_done,"reads_due":s.reads_done+s.skipped+int(s.running),
                     "late_reads":s.late_reads,"read_thread_cpu_s":s.cpu_s}
                    for name,s in self.states.items()]

    def _status_events(self):
        for state in self.source_status():
            key = state["status"]
            if self._transitions.get(state["source"]) != key:
                self._transitions[state["source"]] = key
                now = self.clock()
                self.ingest(Reading("collector.status","self",now,now,(
                    Field("self.source_state",state["status"],"state","event",entity=state["source"]),
                    Field("self.source_skipped",state["skipped"],"ticks","counter",entity=state["source"]),
                    Field("self.source_lateness",state["lateness_s"],"seconds",entity=state["source"]),
                )))

        warning=self.store.budget.usage()["warning"]
        if self._transitions.get("budget_warning") is not warning:
            self._transitions["budget_warning"]=warning
            now=self.clock()
            self.ingest(Reading("collector.budget","self",now,now,(
                Field("self.storage_warning",warning,"boolean","event"),
            )))

    def _health_reading(self):
        with self.lock:
            if self.tail_bytes>=self.tail_limit_bytes:return
        now=self.clock()
        fields=[Field("self.build_version",__version__,"version","state")]
        for state in self.source_status():
            for key,unit,kind in (("reads_done","reads","counter"),("reads_due","ticks","counter"),("late_reads","reads","counter"),("skipped","ticks","counter"),("read_thread_cpu_s","seconds","counter")):
                fields.append(Field("self.source_"+key,state[key],unit,kind,entity=state["source"]))
        usage=self.store.budget.usage()
        fields.append(Field("self.managed_storage",usage["bytes"],"bytes"))
        fields.append(Field("self.storage_warning",bool(usage["warning"]),"boolean","state"))
        with self.lock:
            for key,value in self.operations.items():
                fields.append(Field("self."+key,value,"bytes" if key.endswith("bytes") else "seconds" if key.endswith("_s") else "operations","counter"))
        self.ingest(Reading("collector.health","self",now,now,tuple(fields)))

    def flush(self):
        """Drain the entry snapshot; keep later arrivals for the next flush."""
        with self._flush_lock:
            self._status_events()
            self._health_reading()
            with self.lock:
                target=self.sequence
            while True:
                with self.lock:
                    batch=[];size=0
                    for pending,n in self.tail:
                        if pending.sequence>target:break
                        if batch and size+n>4*1024**2:break
                        batch.append((pending,n));size+=n
                if not batch:
                    return True
                began = time.monotonic()
                cpu_began=time.thread_time()
                records=[]
                def committed():
                    for pending,_ in batch:self.baselines[pending.source]=pending
                    self.committed=batch[-1][0].sequence
                    self.tail=[(r,size) for r,size in self.tail if r.sequence>self.committed]
                    self.tail_bytes=sum(size for _,size in self.tail)
                try:
                    records=[Record.unpack(p.payload) for p,_ in batch]
                    self.store.append(records,commit_lock=self.lock,on_commit=committed)
                except Exception as exc:
                    with self.lock:
                        self.last_save_error = self._save_error(exc)
                    return False
                finally:
                    with self.lock:
                        self.operations["flush_count"] += 1
                        self.operations["flush_elapsed_s"] += time.monotonic()-began
                        self.operations["flush_cpu_s"] = self.operations.get("flush_cpu_s",0)+time.thread_time()-cpu_began
                with self.lock:
                    self.last_save_error = None
                    self.last_save_ns = self.clock().wall_ns
                    if self.tail_bytes < self.tail_limit_bytes and self._paused_since is not None:
                        start_ns=self._paused_since
                        self._paused_since=None
                        now=self.clock()
                        start=Clock(start_ns,now.awake_ns,now.continuous_ns)
                        self.ingest(Reading("collector.buffer","self",start,now,(
                            Field("self.buffer_suspension","buffer_limit","state","event",status="unobserved",start=start,end=now),
                        ),status="unobserved",reason="buffer_limit"))

    def snapshot(self, start_ns=None, end_ns=None):
        with self.lock:
            # The preceding source read is necessary to derive the first delta.
            # Packed references and admission metadata are immutable. Queries
            # decode only selected evidence, after opening their SQLite snapshot.
            selected = []
            previous = {}
            for r,_ in self.tail:
                t = r.end_ns
                if start_ns is not None and t < start_ns:
                    previous[r.source] = r
                elif end_ns is None or t <= end_ns:
                    selected.append(r)
            return {"run_id":self.run_id,"committed":self.committed,
                    "tail":list(previous.values())+selected,"baselines":list(self.baselines.values())}

    def recent_records(self, component=None):
        """Immutable references avoid copying a whole process history per request."""
        now=self.clock().wall_ns
        with self.lock:
            result=[]
            for source,cache in self.cache.items():
                if not cache:continue
                latest=cache[-1]
                if component and latest.reading.component!=component and not any(field_component(f,latest.reading.component)==component for f in latest.reading.fields):continue
                cadence=self.states[source].source.cadence_s if source in self.states else 1
                result.append((source,now-latest.reading.end.wall_ns>max(2*cadence,5)*1e9,list(cache)))
            return result

    def status(self):
        with self.lock:
            return {"run_id":self.run_id,"committed":self.committed,"sequence":self.sequence,
                    "tail_bytes":self.tail_bytes,"tail_limit_bytes":self.tail_limit_bytes,
                    "unsaved_since_ns":self.tail[0][0].start_ns if self.tail else None,
                    "buffer_suspended_since_ns":self._paused_since,"save_error":self.last_save_error,
                    "last_save_ns":self.last_save_ns,"sources":self.source_status(),
                    "operations":dict(self.operations)}

    def run(self):
        self.start()
        try:
            while not self.stop_event.wait(self.flush_s):
                self.flush()
        finally:
            clean = self.close()
        return clean

    def close(self, timeout_s=10):
        """One deadline covers every reader and the final write.

        Save completed evidence before waiting for an uncancellable native call.
        A timed-out close leaves the instance lock held. The finalizer can only
        release it after all old native reads and the writer have completed.
        """
        self.stop_event.set()
        with self.lock:
            if self._closer is None or (not self._closer.is_alive() and not self._close_result):
                def finish():
                    try:
                        saved=self.flush()
                        for thread in self.threads:thread.join()
                        # An in-flight read can finish during the first save or
                        # the joins. Preserve its completion before clean stop.
                        if self.tail:saved=self.flush()
                        while saved and self.tail:saved=self.flush()
                        if self._started and saved:self.store.stop_run(self.run_id,self.clock().wall_ns)
                    except Exception as exc:
                        self.last_save_error=self._save_error(exc);saved=False
                    with self.lock:
                        self._close_result=saved
                        if saved and self._instance_fd is not None:
                            fcntl.flock(self._instance_fd,fcntl.LOCK_UN)
                            os.close(self._instance_fd);self._instance_fd=None
                self._closer=threading.Thread(target=finish,name="collector-close",daemon=True)
                self._closer.start()
            closer=self._closer
        closer.join(max(0,timeout_s))
        return not closer.is_alive() and self._close_result
