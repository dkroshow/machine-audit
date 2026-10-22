"""Small, portable source boundary. All timestamps are integer nanoseconds."""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
import json
import math
import re
import time
import zlib
from typing import Protocol

STATUSES = frozenset({"observed", "measured_zero", "unavailable", "not_enabled", "denied", "failed", "invalid", "unobserved", "pruned"})
KINDS = frozenset({"counter", "gauge", "state", "event", "rate", "accumulator"})
_NAME = re.compile(r"[a-zA-Z0-9_.:-]{1,160}\Z")
# Units and reasons are closed sets so no adapter can carry OS strings, host names or
# addresses out of the machine in those slots. Extend the set here, deliberately.
UNITS = frozenset({
    "bytes", "payload_bytes", "pages", "count", "operations", "reads", "threads", "pid",
    "seconds", "ms", "us", "ns", "unix_ns", "ticks", "mach_ticks", "minutes",
    "percent", "level", "boolean", "state", "integer", "version", "uuid", "sha256", "git_commit",
    "cpus", "CPUs", "logical CPUs", "LOAD_SCALE_1000", "Hz", "MHz", "Mbps",
    "degC", "watts", "rpm", "dB", "dBm", "source_units", "driver_units", "unknown_units",
})
_CODE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_LABEL = re.compile(r"[\w .()+-]{1,80}\Z", re.UNICODE)


def admit_label(label: str | None) -> str | None:
    """Admit a short display name, never paths, commands, addresses or arguments."""
    if label is None:
        return None
    if not isinstance(label, str) or not _LABEL.fullmatch(label) or ".." in label:
        return "redacted"
    return label


def process_identity(pid: int, start: int | str, boot_id: str) -> str:
    if pid < 0 or not _NAME.fullmatch(str(start)) or not _NAME.fullmatch(boot_id):
        raise ValueError("invalid process identity")
    return f"process:{boot_id}:{pid}:{start}"


@dataclass(frozen=True, slots=True)
class Clock:
    wall_ns: int
    awake_ns: int
    continuous_ns: int

    def __post_init__(self):
        if any(not isinstance(value,int) for value in (self.wall_ns,self.awake_ns,self.continuous_ns)):
            raise ValueError("clock values must be integer nanoseconds")

    @classmethod
    def now(cls) -> Clock:
        """Fallback clock; OS adapters supply sleep-aware clocks when available."""
        awake = time.monotonic_ns()
        clock_id = getattr(time, "CLOCK_BOOTTIME", None)
        continuous = time.clock_gettime_ns(clock_id) if clock_id is not None else awake
        return cls(time.time_ns(), awake, continuous)


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    value: int | float | str | bool | None
    unit: str
    kind: str = "gauge"
    status: str = "observed"
    entity: str = "host"
    label: str | None = None
    scope: str = "host"
    additive: bool = False
    scale: float = 1.0
    start: Clock | None = None
    end: Clock | None = None
    rate_unit: str | None = None
    component: str | None = None

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name) or not _NAME.fullmatch(self.entity) or not _NAME.fullmatch(self.scope):
            raise ValueError("invalid field identifier")
        if self.component is not None and not _NAME.fullmatch(self.component):
            raise ValueError("invalid field component")
        if self.start is not None and self.end is not None and self.end.awake_ns < self.start.awake_ns:
            raise ValueError("invalid field read bounds")
        if self.kind in {"counter","gauge","rate","accumulator"} and self.value is not None and not isinstance(self.value,(int,float)):
            raise ValueError("numeric field requires numeric value")
        if self.kind not in KINDS or self.status not in STATUSES:
            raise ValueError("invalid field kind or status")
        if self.unit not in UNITS or (self.rate_unit is not None and self.rate_unit not in UNITS):
            raise ValueError("unit is not an admitted unit")
        if not math.isfinite(self.scale) or self.scale <= 0:
            raise ValueError("invalid field scale")
        if isinstance(self.value, (float, int)) and not math.isfinite(self.value):
            raise ValueError("nonfinite reading")
        if self.value is None and self.status in {"observed", "measured_zero"}:
            raise ValueError("missing value needs an explicit gap status")
        if isinstance(self.value, str) and admit_label(self.value) != self.value:
            raise ValueError("state/event value is not privacy-admitted")
        object.__setattr__(self, "label", admit_label(self.label))


@dataclass(frozen=True, slots=True)
class Reading:
    source: str
    component: str
    start: Clock
    end: Clock
    fields: tuple[Field, ...] = ()
    status: str = "observed"
    reason: str | None = None
    version: str = "1"

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.source) or not _NAME.fullmatch(self.component):
            raise ValueError("invalid source identifier")
        if not _NAME.fullmatch(self.version):
            raise ValueError("invalid source version")
        if self.status not in STATUSES or self.end.awake_ns < self.start.awake_ns:
            raise ValueError("invalid reading status or bounds")
        if self.reason is not None and not _CODE.fullmatch(self.reason):
            raise ValueError("reason must be a code, not exception text, a host or an address")
        keys = [(f.name, f.entity) for f in self.fields]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate field in reading")
        object.__setattr__(self, "fields", tuple(self.fields))


class Source(Protocol):
    name: str
    cadence_s: float

    def read(self) -> Reading: ...


@dataclass(frozen=True)
class Record:
    reading: Reading
    boot_id: str
    run_id: str
    sequence: int
    due_wall_ns: int
    received: Clock

    def __post_init__(self):
        if not _NAME.fullmatch(self.boot_id) or not _NAME.fullmatch(self.run_id) or not isinstance(self.sequence,int) or self.sequence<1:
            raise ValueError("invalid record identity")
        if not isinstance(self.due_wall_ns,int):raise ValueError("invalid due timestamp")

    def to_dict(self) -> dict:
        def clock(c):
            return {"wall_ns":c.wall_ns,"awake_ns":c.awake_ns,"continuous_ns":c.continuous_ns} if c else None
        r = self.reading
        fields = [dict(name=f.name,value=f.value,unit=f.unit,kind=f.kind,status=f.status,
                       entity=f.entity,label=f.label,scope=f.scope,additive=f.additive,scale=f.scale,
                       start=clock(f.start),end=clock(f.end),rate_unit=f.rate_unit,component=f.component)
                  for f in r.fields]
        return dict(reading=dict(source=r.source,component=r.component,start=clock(r.start),end=clock(r.end),
                                 fields=fields,status=r.status,reason=r.reason,version=r.version),
                    boot_id=self.boot_id,run_id=self.run_id,sequence=self.sequence,
                    due_wall_ns=self.due_wall_ns,received=clock(self.received))

    @cached_property
    def packed(self) -> bytes:
        """Lossless private storage format; API/export still use ordinary Records."""
        tables = [[], [], []]
        indexes = [{}, {}, {}]
        def intern(table, value):
            # Numeric metadata keeps its type even though 1 == 1.0 in Python.
            key = (tuple(value),type(value[5]),type(value[6])) if table == 0 else tuple(value)
            if key not in indexes[table]:
                indexes[table][key] = len(tables[table])
                tables[table].append(value)
            return indexes[table][key]
        def clock(c):
            return intern(2, [c.wall_ns, c.awake_ns, c.continuous_ns]) if c else None
        r = self.reading
        header = [r.source,r.component,clock(r.start),clock(r.end),r.status,r.reason,r.version,
                  self.boot_id,self.run_id,self.sequence,self.due_wall_ns,clock(self.received)]
        fields = []
        for f in r.fields:
            descriptor = [f.name,f.unit,f.kind,f.status,f.scope,f.additive,f.scale,f.rate_unit,f.component]
            fields.append([intern(0,descriptor),intern(1,[f.entity,f.label]),f.value,clock(f.start),clock(f.end)])
        data=json.dumps([1,header,tables,fields],separators=(",",":"),ensure_ascii=True,allow_nan=False).encode()
        return b"MR1"+len(data).to_bytes(4,"big")+zlib.compress(data,1)

    @staticmethod
    def decoded_size(payload: bytes) -> int:
        if payload[:3]!=b"MR1" or len(payload)<7:
            raise ValueError("unsupported_record_encoding")
        return int.from_bytes(payload[3:7],"big")

    @classmethod
    def unpack(cls, payload: bytes) -> Record:
        size=cls.decoded_size(payload)
        if size>256*1024**2:raise ValueError("record_size_limit")
        decoder=zlib.decompressobj()
        data=decoder.decompress(payload[7:],size+1)
        if len(data)!=size or not decoder.eof or decoder.unused_data:
            raise ValueError("invalid_record_size")
        version,h,tables,rows = json.loads(data)
        if version != 1:
            raise ValueError("unsupported_record_encoding")
        descriptors,entities,clock_values = tables
        clocks = [Clock(*c) for c in clock_values]
        def clock(i):
            return clocks[i] if i is not None else None
        fields = []
        for d,e,value,start,end in rows:
            name,unit,kind,status,scope,additive,scale,rate_unit,component = descriptors[d]
            entity,label = entities[e]
            fields.append(Field(name,value,unit,kind,status,entity,label,scope,additive,scale,clock(start),clock(end),rate_unit,component))
        source,component,start,end,status,reason,version,boot,run,sequence,due,received = h
        record = cls(Reading(source,component,clock(start),clock(end),tuple(fields),status,reason,version),boot,run,sequence,due,clock(received))
        record.__dict__["packed"] = payload
        return record

    @classmethod
    def from_dict(cls, data: dict) -> Record:
        r = data["reading"]
        return cls(Reading(r["source"], r["component"], Clock(**r["start"]), Clock(**r["end"]),
                           tuple(Field(**dict(f, start=Clock(**f["start"]) if f.get("start") else None, end=Clock(**f["end"]) if f.get("end") else None)) for f in r["fields"]), r.get("status", "observed"),
                           r.get("reason"), r.get("version", "1")),
                   data["boot_id"], data["run_id"], data["sequence"], data["due_wall_ns"], Clock(**data["received"]))


def field_component(field: Field, default: str) -> str:
    """A bundled process read supplies several components without extra OS reads."""
    if field.component:
        return field.component
    name=field.name
    if name.startswith("self."):
        return "self"
    if name.startswith(("process.diskio", "process.logical_writes")):
        return "storage"
    if name in {"process.phys_footprint","process.resident_size","process.compressed","process.ledger_swapins","process.decompressions","process.limit_bytes_remaining"} or name.startswith("process.ledger_tag_graphics"):
        return "memory"
    return default


PROCESS_ENTITIES = ("process:", "program:")


def is_process_evidence(entity: str, scope: str) -> bool:
    """Failed PID reads retain process scope even without a process identity.
    Linux unit cgroups are workload groups, not host series: `unit` fields rank like
    programs and `unit_detail` fields (limits, breakdowns, pressure) describe them."""
    return scope in ("process", "unit", "unit_detail") or entity.startswith(PROCESS_ENTITIES)


def is_series(component: str, entity: str, scope: str) -> bool:
    return component == "self" or not is_process_evidence(entity, scope)
