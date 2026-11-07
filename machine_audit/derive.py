"""One derivation path for retained readings, rollups, and interval queries."""
from __future__ import annotations
from collections import defaultdict
from .model import PROCESS_AGE, Record, Field, field_component

METHOD = "observed-windows-v1"
LEVELS = (0.5, 1.0, 2.0, 4.0, 8.0)
GOOD = {"observed", "measured_zero"}
# Busy CPU states (cpu-view.md): user plus system; Linux nice is user time, and
# Linux irq and softirq are system time. Absent states add nothing.
BUSY = ("cpu.user","cpu.system","cpu.nice","cpu.irq","cpu.softirq")
# A read's awake and continuous clocks come from two calls, nanoseconds to microseconds
# apart. A difference below this is that gap, not a suspend.
SLEEP_TOLERANCE_S = 0.001


def source_fields(reading):
    fields = list(reading.fields)
    indexed = {(f.name, f.entity): f for f in fields}
    # In field order: a set's order changes with each process's hash seed, and so would the
    # order in which every later sum adds these totals.
    entities = dict.fromkeys(f.entity for f in fields)
    host_busy = []
    for entity in entities:
        for prefix in ("process","self"):
            user = indexed.get((prefix+".user_time",entity))
            system = indexed.get((prefix+".system_time",entity))
            if user and system and (prefix+".cpu",entity) not in indexed:
                good = user.status in GOOD and system.status in GOOD and user.unit == system.unit and user.scale == system.scale
                fields.append(Field(prefix+".cpu",user.value+system.value if good else None,user.unit,"counter",
                                    status="observed" if good else "invalid",entity=entity,label=user.label,
                                    scope=user.scope,additive=True,scale=user.scale,component="self" if prefix=="self" else "cpu",start=user.start,end=user.end,rate_unit="CPUs"))
        cpu_parts = [indexed.get((name,entity)) for name in BUSY]
        if cpu_parts[0] and cpu_parts[1]:
            parts=[f for f in cpu_parts if f is not None]
            if all(f.status in GOOD and f.unit==parts[0].unit and f.scale==parts[0].scale for f in parts):
                host_busy.append((entity,parts))
    if host_busy and not any(f.name=="cpu.busy" for f in fields):
        first=host_busy[0][1][0]
        fields.append(Field("cpu.busy",sum(f.value for _,parts in host_busy for f in parts),first.unit,"counter",
                            scale=first.scale,rate_unit="CPUs"))
        # Linux host totals arrive as one aggregate entity; its CPU count is cpu.logical.
        # On the Mac, cpu.logical equals the per-CPU entity count.
        logical=next((f for f in fields if f.name=="cpu.logical" and f.status in GOOD),None)
        capacity=logical.value if logical else len(host_busy)
        fields.append(Field("cpu.capacity",capacity,"logical CPUs"))
        runnable=next((f for f in fields if f.name=="cpu.runnable" and f.status in GOOD),None)
        if runnable:
            fields.append(Field("cpu.excess_runnable",max(0,runnable.value*runnable.scale-capacity),"threads"))
    return fields


def union_ns(intervals):
    # A gauge sample is an instant: it adds no covered time, only list length.
    merged=[]
    for start,end in sorted(i for i in intervals if i[1]>i[0]):
        if merged and start <= merged[-1][1]:
            merged[-1][1]=max(merged[-1][1],end)
        else:
            merged.append([start,end])
    return merged


def points(records: list[Record], previous: dict[str, Record] | None = None) -> list[dict]:
    previous = dict(previous or {})
    result = []
    # Each source's latest pass and its fields with derived totals, by name and entity: the
    # next pass of the source derives from them without deriving them again.
    indexed = {}
    for record in records:
        reading = record.reading
        prior = previous.get(reading.source)
        compatible = prior and (prior.run_id, prior.boot_id) == (record.run_id, record.boot_id)
        if not compatible:
            prior_fields = {}
        elif reading.source in indexed and indexed[reading.source][0] is prior:
            prior_fields = indexed[reading.source][1]
        else:
            prior_fields = {(f.name, f.entity): f for f in source_fields(prior.reading)}
        # Constituents are never derived (a total is added only where a pass has none), so
        # the same index gives them as read.
        prior_inputs = prior_fields
        inputs_by_entity = defaultdict(list)
        busy_inputs = []
        # A process missing from the prior pass and younger than the time since it started
        # there: its counters began at zero inside this interval.
        prior_entities = {f.entity for f in prior.reading.fields} if compatible and prior.reading.status in GOOD else None
        ages = {f.entity: f.value for f in reading.fields if f.name == PROCESS_AGE and f.status in GOOD}
        for f in reading.fields:
            if f.name in {"process.user_time", "process.system_time", "self.user_time", "self.system_time"}:
                inputs_by_entity[(f.name.split(".")[0], f.entity)].append(f)
            elif f.name in BUSY:
                busy_inputs.append(f)
        pass_points = []
        # Summing each pass first preserves simultaneous peaks and union elapsed time.
        groups = defaultdict(list)
        # A pass repeats a few field descriptors over every process: each point starts as a
        # copy of its descriptor's template.
        templates = {}
        status_of_reading = reading.status
        good_reading = status_of_reading in GOOD
        fields = source_fields(reading)
        indexed[reading.source] = (record, {(f.name, f.entity): f for f in fields})
        for field in fields:
            name, kind = field.name, field.kind
            if name == PROCESS_AGE:continue
            end_clock = field.end or reading.end
            start_clock = field.start or reading.start
            descriptor = (name, field.unit, kind, field.scope, field.additive, field.scale, field.rate_unit, field.component)
            template = templates.get(descriptor)
            if template is None:
                component = field_component(field, reading.component)
                template = templates[descriptor] = (
                    dict(source=reading.source, component=component, signal=name, entity=None, label=None,
                         scope=field.scope, kind=kind, unit=field.unit, additive=field.additive, scale=field.scale,
                         start_ns=None, end_ns=None, rate_unit=field.rate_unit or (field.unit+"/s"), status=None,
                         value=None, total=None, covered_s=0.0, sleep_s=0.0, run_id=record.run_id,
                         sequence=record.sequence, method=METHOD, awake_start_ns=None, awake_end_ns=None),
                    # Programs group instances by label; a Linux unit is already one workload.
                    field.additive and component != "self" and field.scope not in ("unit", "unit_detail"))
            p = template[0].copy()
            entity, label, value = field.entity, field.label, field.value
            p["entity"] = entity
            p["label"] = label
            p["start_ns"] = p["end_ns"] = end_clock.wall_ns
            status = p["status"] = field.status if good_reading else status_of_reading
            p["value"] = value
            p["awake_start_ns"] = p["awake_end_ns"] = end_clock.awake_ns
            old = prior_fields.get((name, entity))
            age = ages.get(entity) if not old and prior_entities is not None and entity not in prior_entities else None
            if kind == "counter" and status in GOOD and age and 0 < age <= (end_clock.awake_ns-prior.reading.end.awake_ns)/1e9:
                span = int(age*1e9)
                p["total"] = value
                p["covered_s"] = age
                p["value"] = value*field.scale/age
                p["start_ns"] = end_clock.wall_ns-span
                p["awake_start_ns"] = end_clock.awake_ns-span
                p["status"] = "measured_zero" if value == 0 else "observed"
            elif kind == "counter" and status in GOOD:
                if not old or old.status not in GOOD or prior.reading.status not in GOOD:
                    p["status"] = "unobserved"
                    p["reason"] = "baseline"
                elif (old.unit, old.kind, old.scale) != (field.unit, kind, field.scale):
                    p["status"] = "invalid"
                    p["reason"] = "field_changed"
                else:
                    old_end = old.end or prior.reading.end
                    dt = (end_clock.awake_ns - old_end.awake_ns) / 1e9
                    continuous = (end_clock.continuous_ns - old_end.continuous_ns) / 1e9
                    delta = value - old.value
                    p["start_ns"] = old_end.wall_ns
                    if dt <= 0 or continuous < 0 or end_clock.wall_ns <= old_end.wall_ns or delta < 0:
                        p["status"] = "invalid"
                        p["reason"] = "clock_or_counter_reset"
                    else:
                        p["total"] = delta
                        p["covered_s"] = dt
                        p["value"] = delta * field.scale / dt
                        p["awake_start_ns"] = old_end.awake_ns
                        p["sleep_s"] = continuous-dt if continuous-dt >= SLEEP_TOLERANCE_S else 0.0
                        p["status"] = "measured_zero" if delta == 0 else "observed"
            elif kind == "accumulator" and status in GOOD:
                dt = (end_clock.awake_ns - start_clock.awake_ns) / 1e9
                if dt <= 0:
                    p["status"] = "invalid"
                    p["reason"] = "missing_accumulator_window"
                else:
                    p["start_ns"] = start_clock.wall_ns
                    p["awake_start_ns"] = start_clock.awake_ns
                    p["total"] = value
                    p["covered_s"] = dt
                    p["value"] = value * field.scale / dt
            elif kind == "rate" and status in GOOD:
                p["start_ns"] = start_clock.wall_ns
                p["awake_start_ns"] = start_clock.awake_ns
                p["covered_s"] = (end_clock.awake_ns-start_clock.awake_ns)/1e9
            if kind in {"state","event"}:
                p["start_ns"] = start_clock.wall_ns
            if name in {"process.cpu","self.cpu","cpu.busy"} and p["status"] in GOOD and old:
                prefix=name.split(".")[0]
                inputs = busy_inputs if name == "cpu.busy" else inputs_by_entity[(prefix, entity)]
                if any((f.name,f.entity) not in prior_inputs or prior_inputs[(f.name,f.entity)].status not in GOOD or f.value<prior_inputs[(f.name,f.entity)].value for f in inputs):
                    p["status"] = "invalid"
                    p["reason"] = "constituent_reset"
                    p["total"] = None
                    p["covered_s"] = 0
                    p["value"] = None
            if p["status"] in GOOD and p["value"] == 0:
                p["status"] = "measured_zero"
            pass_points.append(p)
            if template[1] and label is not None:
                groups[(name, label, field.unit, kind, field.scope, field.scale)].append(p)
        result.extend(pass_points)
        for (signal, label, unit, kind, scope,scale), members in groups.items():
            valid = [p for p in members if p["status"] in GOOD]
            p = dict(members[0], entity="program:"+label, label=label,
                     grouping="admitted-label", alignment="enclosing-source-pass",
                     instance_count=len(members), partial_members=len(valid) != len(members),
                     members=[m["entity"] for m in members])
            if valid:
                spans = [(m["awake_start_ns"],m["awake_end_ns"]) for m in valid]
                p["windows"] = windows = union_ns(spans)
                p.update(awake_start_ns=min(a for a,_ in spans),
                         awake_end_ns=max(b for _,b in spans),
                         start_ns=min(m["start_ns"] for m in valid),
                         end_ns=max(m["end_ns"] for m in valid),
                         covered_s=sum(b-a for a,b in windows)/1e9,
                         value=sum(m["value"] for m in valid))
                totals = [m["total"] for m in valid if m["total"] is not None]
                p["total"] = sum(totals) if totals else None
                if p["total"] is not None and p["covered_s"]:
                    p["value"] = p["total"]*p["scale"]/p["covered_s"]
                p["status"] = "measured_zero" if p["value"] == 0 else "observed"
            result.append(p)
        previous[reading.source] = record
    return result


def summarize(items: list[dict]) -> dict:
    first = items[0]
    meta = {k: first[k] for k in ("source", "component", "signal", "entity", "label", "scope", "kind", "unit", "scale", "rate_unit", "additive", "method")}
    valid = [p for p in items if p["status"] in GOOD and isinstance(p["value"], (float, int))]
    values = [p["value"] for p in valid]
    durations = [p["covered_s"] for p in valid]
    total = sum(p["total"] for p in valid if p["total"] is not None)
    has_total = any(p["total"] is not None for p in valid)
    peak = max(valid, key=lambda p: p["value"]) if valid else None
    members = set(m for p in items for m in p.get("members", [p["entity"]]))
    result = dict(meta, start_ns=min(p["start_ns"] for p in items),
                  end_ns=max(p["end_ns"] for p in items), count=len(valid),
                  sample_count=len(items), sum=sum(values), total=total if has_total else None,
                  covered_s=sum(durations), mean=(sum(values)/len(values) if values else None),
                  min=min(values) if values else None, max=peak["value"] if peak else None,
                  max_ns=peak["end_ns"] if peak else None,
                  last=valid[-1]["value"] if valid else None,
                  last_ns=valid[-1]["end_ns"] if valid else None,
                  statuses=dict((s, sum(p["status"] == s for p in items)) for s in sorted({p["status"] for p in items})),
                  status=("measured_zero" if values and all(v == 0 for v in values) else "observed") if values else first["status"],
                  sleep_s=sum(p["sleep_s"] for p in items),
                  peak_rate=peak["value"] if peak and has_total else None,
                  peak_ns=peak["end_ns"] if peak and has_total else None,
                  above_s={str(level): sum(p["covered_s"] for p in valid if p["value"] >= level) for level in LEVELS} if first["signal"] in {"process.cpu","self.cpu","cpu.busy"} else {},
                  histogram_s=[sum(p["covered_s"] for p in valid if (float('-inf') if i == 0 else LEVELS[i-1]) <= p["value"] < (LEVELS[i] if i < len(LEVELS) else float('inf'))) for i in range(len(LEVELS)+1)] if first["signal"] in {"process.cpu","self.cpu","cpu.busy"} else [],
                  instance_count=len(members), members=sorted(members),
                  partial_members=any(p.get("partial_members", False) for p in items),
                  value=items[-1]["value"] if not values else None,
                  aggregation="sample-mean" if first["kind"] in {"gauge", "state", "event"} else "covered-awake-rate")
    windows = defaultdict(list)
    for p in valid:
        windows[p["run_id"]].extend(p.get("windows",[(p["awake_start_ns"],p["awake_end_ns"])]))
    result["covered_windows"] = {run:union_ns(spans) for run,spans in windows.items()}
    result["covered_s"] = sum(b-a for spans in result["covered_windows"].values() for a,b in spans)/1e9
    if has_total and result["covered_s"]:
        result["mean_rate"] = total * first["scale"] / result["covered_s"]
    return result


def merge_summaries(rows: list[dict]) -> dict:
    """Merge disjoint fragments; never average averages or add observed peaks."""
    first = rows[0]
    result = dict(first)
    for key in ("count", "sample_count", "sum", "covered_s", "sleep_s"):
        result[key] = sum(r.get(key, 0) for r in rows)
    totals = [r["total"] for r in rows if r["total"] is not None]
    result["total"] = sum(totals) if totals else None
    result["mean"] = result["sum"] / result["count"] if result["count"] else None
    windows = defaultdict(list)
    for row in rows:
        for run,spans in row.get("covered_windows",{}).items():
            windows[run].extend(spans)
    result["covered_windows"] = {run:union_ns(spans) for run,spans in windows.items()}
    result["covered_s"] = sum(b-a for spans in result["covered_windows"].values() for a,b in spans)/1e9
    if result["total"] is not None and result["covered_s"]:
        result["mean_rate"] = result["total"]*result["scale"]/result["covered_s"]
    valid = [r for r in rows if r["max"] is not None]
    if valid:
        peak = max(valid, key=lambda r:r["max"])
        last = max(valid, key=lambda r:r["last_ns"])
        result.update(min=min(r["min"] for r in valid), max=peak["max"], max_ns=peak["max_ns"],
                      peak_rate=peak["peak_rate"], peak_ns=peak["peak_ns"], last=last["last"], last_ns=last["last_ns"])
    result["value"] = max(rows,key=lambda r:r["end_ns"]).get("value")
    result["start_ns"] = min(r["start_ns"] for r in rows)
    result["end_ns"] = max(r["end_ns"] for r in rows)
    result["statuses"] = {s:sum(r["statuses"].get(s, 0) for r in rows) for s in sorted({s for r in rows for s in r["statuses"]})}
    result["status"] = ("measured_zero" if result["max"] == result["min"] == 0 else "observed") if valid else first["status"]
    result["above_s"] = {key:sum(r["above_s"].get(key,0) for r in rows) for key in first["above_s"]}
    result["histogram_s"] = [sum(r["histogram_s"][i] for r in rows) for i in range(len(first["histogram_s"]))]
    result["partial_boundary"] = any(r.get("partial_boundary",False) for r in rows)
    result["members"] = sorted({m for r in rows for m in r["members"]})
    result["instance_count"] = len(result["members"])
    if "entities" in first:
        # A rest row names no members; the most entities it held in one bucket is a floor.
        result["entities"] = result["instance_count"] = max(r["entities"] for r in rows)
    result["partial_members"] = any(r["partial_members"] for r in rows)
    return result


def bucket_of(p: dict, width_ns: int) -> int:
    """The bucket a point belongs to: the one holding its last instant."""
    return max(p["end_ns"]-1, p["start_ns"]) // width_ns * width_ns


def rollups(points_: list[dict], width_ns: int) -> list[tuple[int, dict]]:
    groups = defaultdict(list)
    for p in points_:
        key = (bucket_of(p, width_ns), p["source"], p["signal"], p["entity"], p["scope"], p["unit"], p["kind"],p["scale"])
        groups[key].append(p)
    return [(key[0], summarize(items)) for key, items in groups.items()]
