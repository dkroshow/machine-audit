"""One derivation path for retained readings, rollups, and interval queries."""
from __future__ import annotations
from collections import defaultdict
from .model import Record, Field, field_component

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
    entities = {f.entity for f in fields}
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
    merged=[]
    for start,end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1]=max(merged[-1][1],end)
        else:
            merged.append([start,end])
    return merged


def points(records: list[Record], previous: dict[str, Record] | None = None) -> list[dict]:
    previous = dict(previous or {})
    result = []
    for record in records:
        reading = record.reading
        prior = previous.get(reading.source)
        compatible = prior and (prior.run_id, prior.boot_id) == (record.run_id, record.boot_id)
        prior_fields = {(f.name, f.entity): f for f in source_fields(prior.reading)} if compatible else {}
        # Index original constituents once per pass, not once per process.
        prior_inputs = {(f.name, f.entity): f for f in prior.reading.fields} if compatible else {}
        inputs_by_entity = defaultdict(list)
        busy_inputs = []
        for f in reading.fields:
            if f.name in {"process.user_time", "process.system_time", "self.user_time", "self.system_time"}:
                inputs_by_entity[(f.name.split(".")[0], f.entity)].append(f)
            elif f.name in BUSY:
                busy_inputs.append(f)
        pass_points = []
        for field in source_fields(reading):
            end_clock = field.end or reading.end
            start_clock = field.start or reading.start
            p = dict(source=reading.source, component=field_component(field,reading.component), signal=field.name,
                     entity=field.entity, label=field.label, scope=field.scope, kind=field.kind,
                     unit=field.unit, additive=field.additive, scale=field.scale,
                     start_ns=end_clock.wall_ns, end_ns=end_clock.wall_ns,
                     rate_unit=field.rate_unit or (field.unit+"/s"),
                     status=field.status if reading.status in GOOD else reading.status,
                     value=field.value, total=None, covered_s=0.0, sleep_s=0.0,
                     run_id=record.run_id, sequence=record.sequence, method=METHOD,
                     awake_start_ns=end_clock.awake_ns, awake_end_ns=end_clock.awake_ns)
            old = prior_fields.get((field.name, field.entity))
            if field.kind == "counter" and p["status"] in GOOD:
                if not old or old.status not in GOOD or prior.reading.status not in GOOD:
                    p.update(status="unobserved", reason="baseline")
                elif (old.unit, old.kind, old.scale) != (field.unit, field.kind, field.scale):
                    p.update(status="invalid", reason="field_changed")
                else:
                    old_end = old.end or prior.reading.end
                    dt = (end_clock.awake_ns - old_end.awake_ns) / 1e9
                    continuous = (end_clock.continuous_ns - old_end.continuous_ns) / 1e9
                    delta = field.value - old.value
                    p["start_ns"] = old_end.wall_ns
                    if dt <= 0 or continuous < 0 or end_clock.wall_ns <= p["start_ns"] or delta < 0:
                        p.update(status="invalid", reason="clock_or_counter_reset")
                    else:
                        p.update(total=delta, covered_s=dt, value=delta * field.scale / dt,
                                 awake_start_ns=old_end.awake_ns,
                                 sleep_s=continuous-dt if continuous-dt >= SLEEP_TOLERANCE_S else 0.0,
                                 status="measured_zero" if delta == 0 else "observed")
            elif field.kind == "accumulator" and p["status"] in GOOD:
                dt = (end_clock.awake_ns - start_clock.awake_ns) / 1e9
                if dt <= 0:
                    p.update(status="invalid", reason="missing_accumulator_window")
                else:
                    p.update(start_ns=start_clock.wall_ns, awake_start_ns=start_clock.awake_ns, total=field.value,
                             covered_s=dt, value=field.value * field.scale / dt)
            elif field.kind == "rate" and p["status"] in GOOD:
                p.update(start_ns=start_clock.wall_ns, awake_start_ns=start_clock.awake_ns,
                         covered_s=(end_clock.awake_ns-start_clock.awake_ns)/1e9)
            if field.kind in {"state","event"}:
                p["start_ns"] = start_clock.wall_ns
            if field.name in {"process.cpu","self.cpu","cpu.busy"} and p["status"] in GOOD and old:
                prefix=field.name.split(".")[0]
                inputs = busy_inputs if field.name == "cpu.busy" else inputs_by_entity[(prefix, field.entity)]
                if any((f.name,f.entity) not in prior_inputs or prior_inputs[(f.name,f.entity)].status not in GOOD or f.value<prior_inputs[(f.name,f.entity)].value for f in inputs):
                    p.update(status="invalid",reason="constituent_reset",total=None,covered_s=0,value=None)
            if p["status"] in GOOD and p["value"] == 0:
                p["status"] = "measured_zero"
            pass_points.append(p)
        result.extend(pass_points)
        # Summing each pass first preserves simultaneous peaks and union elapsed time.
        groups = defaultdict(list)
        for p in pass_points:
            if p["additive"] and p["label"] is not None and p["component"]!="self":
                groups[(p["signal"], p["label"], p["unit"], p["kind"], p["scope"],p["scale"])].append(p)
        for (signal, label, unit, kind, scope,scale), members in groups.items():
            valid = [p for p in members if p["status"] in GOOD]
            p = dict(members[0], entity="program:"+label, label=label,
                     grouping="admitted-label", alignment="enclosing-source-pass",
                     instance_count=len(members), partial_members=len(valid) != len(members),
                     members=[m["entity"] for m in members])
            if valid:
                p["windows"]=union_ns([(m["awake_start_ns"],m["awake_end_ns"]) for m in valid])
                p.update(awake_start_ns=min(m["awake_start_ns"] for m in valid),
                         awake_end_ns=max(m["awake_end_ns"] for m in valid),
                         start_ns=min(m["start_ns"] for m in valid),
                         end_ns=max(m["end_ns"] for m in valid),
                         covered_s=sum(b-a for a,b in union_ns([(m["awake_start_ns"],m["awake_end_ns"]) for m in valid]))/1e9,
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
    result["statuses"] = {s:sum(r["statuses"].get(s, 0) for r in rows) for s in {s for r in rows for s in r["statuses"]}}
    result["status"] = ("measured_zero" if result["max"] == result["min"] == 0 else "observed") if valid else first["status"]
    result["above_s"] = {key:sum(r["above_s"].get(key,0) for r in rows) for key in first["above_s"]}
    result["histogram_s"] = [sum(r["histogram_s"][i] for r in rows) for i in range(len(first["histogram_s"]))]
    result["partial_boundary"] = any(r.get("partial_boundary",False) for r in rows)
    result["members"] = sorted({m for r in rows for m in r["members"]})
    result["instance_count"] = len(result["members"])
    result["partial_members"] = any(r["partial_members"] for r in rows)
    return result


def rollups(points_: list[dict], width_ns: int) -> list[tuple[int, dict]]:
    groups = defaultdict(list)
    for p in points_:
        bucket = max(p["end_ns"]-1, p["start_ns"]) // width_ns * width_ns
        key = (bucket, p["source"], p["signal"], p["entity"], p["scope"], p["unit"], p["kind"],p["scale"])
        groups[key].append(p)
    return [(key[0], summarize(items)) for key, items in groups.items()]
