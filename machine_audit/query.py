"""Shared bounded answers for the dashboard, agents, and stored-record replay."""
from __future__ import annotations
from collections import defaultdict
from contextlib import contextmanager
import json
import time
import threading
import math
import zlib
import heapq
from operator import itemgetter
from dataclasses import replace
from itertools import chain, groupby, takewhile
from .derive import points, rollups, summarize, merge_summaries, GOOD, BUSY
from .store import MINUTE, QUARTER, HOUR, WIDTHS, unpack_summaries
from .model import PROCESS_ENTITIES, Record, field_component, is_process_evidence, is_series

# Without a named signal, each workload ranks once, by its component total: CPU time, written
# bytes, and the memory view's footprint (Mac footprint; a Linux unit's charge, which already
# holds its processes' resident sets). Components need an explicit signal.
WHO_TOTALS = {"cpu": {"process.cpu", "unit.cpu.stat.usage_usec"},
              "storage": {"process.diskio_byteswritten", "unit.io.stat.wbytes"},
              "memory": {"process.phys_footprint", "unit.memory.current"}}

MAX_ROWS = 2000
MAX_SCAN = 20000
MAX_QUERY_BYTES = 32*1024**2
RESOLUTIONS = tuple(WIDTHS)
# One measure of an entity within a summary bundle.
SUMMARY_KEY = itemgetter("signal","unit","kind","scope","scale")

def bounded_payloads(cursor):
    result=[];size=0
    for row in cursor:
        payload=row[0]
        decoded=Record.decoded_size(payload)
        if len(result)>=MAX_SCAN or size+decoded>MAX_QUERY_BYTES:
            return result,True
        result.append(payload);size+=decoded
    return result,False


def measured(function):
    def call(self,*args,**kwargs):
        depth=getattr(self._local,"measure_depth",0)
        self._local.measure_depth=depth+1
        began=time.monotonic();cpu=time.thread_time()
        try:
            return function(self,*args,**kwargs)
        finally:
            self._local.measure_depth=depth
            if depth==0 and self.collector:
                with self.collector.lock:
                    ops=self.collector.operations
                    ops["query_count"]=ops.get("query_count",0)+1
                    ops["query_elapsed_s"]=ops.get("query_elapsed_s",0)+time.monotonic()-began
                    ops["query_cpu_s"]=ops.get("query_cpu_s",0)+time.thread_time()-cpu
    return call


class Query:
    def __init__(self, store, collector=None, storage=None, config=None):
        self.store = store
        self.collector = collector
        self.storage = storage
        self.config = config
        self._local = threading.local()

    @staticmethod
    def _base(shape, status="observed"):
        return {"schema_version":1,"shape":shape,"status":status,"coverage":{}}

    @staticmethod
    def _bounds(start_ns,end_ns,limit):
        if not isinstance(start_ns,int) or not isinstance(end_ns,int) or start_ns >= end_ns:
            raise ValueError("start must precede end")
        if not 1 <= limit <= MAX_ROWS:
            raise ValueError("limit must be 1..2000")

    @contextmanager
    def _snapshot(self,start_ns,end_ns):
        if getattr(self._local,"snapshot",None) is not None:
            yield self._local.snapshot
            return
        # Damaged summary payloads met by this query; coverage reports them.
        self._local.damaged=set()
        if self.collector:
            with self.collector.lock:
                captured=self.collector.snapshot(start_ns,end_ns)
                context=self.store.read()
                db=context.__enter__()
            captured["decoded"]={}
            try:
                yield db,captured
            finally:
                context.__exit__(None,None,None)
        else:
            with self.store.read() as db:
                yield db,None

    @staticmethod
    def _watermark(alias, captured, args):
        if captured:
            args.extend([captured["run_id"],captured["committed"]])
            return f" AND ({alias}.run_id != ? OR {alias}.sequence <= ?)"
        return ""

    @staticmethod
    def _decode_pending(captured, pending):
        # Nested around answers share this cache and the same immutable tail.
        decoded=captured["decoded"]
        if pending.sequence not in decoded:
            decoded[pending.sequence]=Record.unpack(pending.payload)
        return decoded[pending.sequence]

    @staticmethod
    def _raw_selection(component, series_only, args):
        clause=""
        if component:
            clause+=" AND EXISTS(SELECT 1 FROM reading_components c WHERE c.run_id=r.run_id AND c.sequence=r.sequence AND c.component=?"+(" AND c.is_series=1" if series_only else "")+")"
            args.append(component)
        elif series_only:
            clause+=" AND EXISTS(SELECT 1 FROM reading_components c WHERE c.run_id=r.run_id AND c.sequence=r.sequence AND c.is_series=1)"
        if series_only:
            # Older index flags can over-admit PID failures. Source scope can
            # reject process-only sources without decoding their large payloads.
            clause+=" AND (NOT EXISTS(SELECT 1 FROM signal_catalog sc WHERE sc.source=r.source) OR EXISTS(SELECT 1 FROM signal_catalog sc WHERE sc.source=r.source AND is_series(sc.component,'host',sc.scope)"
            if component:
                clause+=" AND sc.component=?";args.append(component)
            clause+="))"
        return clause

    def _raw(self,db,captured,start_ns,end_ns,component=None,series_only=False):
        args = [start_ns,end_ns]
        clause = self._watermark("r",captured,args)
        clause += self._raw_selection(component,series_only,args)
        rows,overflow = bounded_payloads(db.execute("SELECT r.payload FROM readings r WHERE r.wall_ns>=? AND r.wall_ns<=?"+clause+" ORDER BY r.wall_ns,r.sequence LIMIT ?",args+[MAX_SCAN+1]))
        records = [Record.unpack(r) for r in rows]
        used=sum(Record.decoded_size(r) for r in rows)
        seen={(r.run_id,r.sequence) for r in records}
        def admit(payload=None,pending=None):
            nonlocal used,overflow
            if pending is not None and (captured["run_id"],pending.sequence) in seen:return
            payload=payload if payload is not None else pending.payload
            size=Record.decoded_size(payload)
            if used+size>MAX_QUERY_BYTES:
                overflow=True;return
            record=self._decode_pending(captured,pending) if pending is not None else Record.unpack(payload)
            if (record.run_id,record.sequence) not in seen:
                seen.add((record.run_id,record.sequence));records.append(record);used+=size
        # One baseline per source, restricted to the same coherent watermark.
        sources = db.execute("SELECT source FROM baselines LIMIT 1000").fetchall()
        for source, in sources:
            a = [source,start_ns]
            c = self._watermark("r",captured,a)
            c += self._raw_selection(component,series_only,a)
            row = db.execute("SELECT payload FROM readings r WHERE source=? AND wall_ns<?"+c+" ORDER BY wall_ns DESC,sequence DESC LIMIT 1",a).fetchone()
            if row:
                admit(payload=row[0])
        if captured:
            for r in captured.get("baselines",[])+captured["tail"]:
                if r.matches(component,series_only):admit(pending=r)
        if series_only:
            # Catalogs may mix host and process scopes. Recheck bounded decoded
            # candidates; false positives still count against the byte budget.
            records=[r for r in records if not r.reading.fields or any(is_series(field_component(f,r.reading.component),f.entity,f.scope) and (not component or field_component(f,r.reading.component)==component) for f in r.reading.fields)]
        unique = {(r.run_id,r.sequence):r for r in records}
        return sorted(unique.values(),key=lambda r:(r.reading.end.wall_ns,r.sequence)),overflow

    def _iter_summaries(self,db,captured,start_ns,end_ns,resolution,signals=None,
                        component=None,program_only=False,series_only=False,spans=None,stored=None,keep=None):
        """Matching summary rows in source, entity, bucket order. `stored`, a one-item
        list, accumulates the stored payload bytes read, for callers that bound them;
        `keep` is passed to unpack_summaries."""
        width = WIDTHS[resolution]
        spans = spans or [(resolution,start_ns//width*width,end_ns)]
        args=[];parts=[]
        for res,lo,hi in spans:
            if lo < hi:
                parts.append("(resolution=? AND bucket_ns>=? AND bucket_ns<?)")
                args.extend((res,lo,hi))
        if not parts:return
        clause="("+" OR ".join(parts)+")"
        if captured:
            clause+=" AND (run_id != ? OR sequence_max<=?)"
            args.extend([captured["run_id"],captured["committed"]])
        if component:
            clause+=" AND component=?";args.append(component)
        if signals:
            clause+=" AND source IN (SELECT source FROM signal_catalog WHERE signal IN ("+",".join("?" for _ in signals)+"))"
            args.extend(signals)
        if series_only:
            # Process entities fail is_series by name alone; skip them before the per-row
            # catalog check, which calls back into Python for every catalog signal.
            clause+=" AND (summaries.component='self' OR ("+" AND ".join("summaries.entity NOT LIKE ?" for _ in PROCESS_ENTITIES)+"))"
            args.extend(prefix+"%" for prefix in PROCESS_ENTITIES)
            clause+=" AND EXISTS(SELECT 1 FROM signal_catalog sc WHERE sc.source=summaries.source AND sc.component=summaries.component AND is_series(sc.component,summaries.entity,sc.scope))"
        if program_only:clause+=" AND entity LIKE 'program:%'"
        # Sort compact row identities, never compressed payloads, then fetch payloads
        # in batches. The snapshot keeps them stable while a writer replaces rows.
        selected=db.execute("SELECT id,bucket_ns,resolution,source FROM summaries WHERE "+clause+" ORDER BY source,entity,bucket_ns",args).fetchall()
        for lo in range(0,len(selected),500):
            batch=selected[lo:lo+500]
            payloads=dict(db.execute("SELECT id,payload FROM summaries WHERE id IN ("+",".join("?"*len(batch))+")",[b[0] for b in batch]))
            for identifier,bucket,res,source in batch:
                payload=payloads[identifier]
                if stored is not None:stored[0]+=len(payload)
                try:rows=unpack_summaries(payload,keep)
                except (zlib.error,ValueError):
                    # A payload damaged on disk (crash, recovery) is a reported gap,
                    # not a failed answer for every window that contains it.
                    self._local.damaged.add((source,bucket,res));continue
                for row in rows:
                    if series_only and not is_series(row["component"],row["entity"],row["scope"]):continue
                    if signals and row["signal"] not in signals:continue
                    if row["start_ns"] >= end_ns or row["end_ns"] < start_ns:continue
                    row["summary_bucket_start_ns"]=bucket
                    row["summary_bucket_end_ns"]=bucket+WIDTHS[res]
                    row["summary_resolution"]=res
                    row["partial_boundary"]=row["start_ns"]<start_ns or row["end_ns"]>end_ns
                    yield row

    @staticmethod
    def _saved(db,start_ns,end_ns):
        """Whether a save covers part of the window. An hour or longer answers from saved
        summaries; until a save covers it, the unsaved tail answers."""
        return db.execute("SELECT 1 FROM summaries WHERE resolution='hour' AND bucket_ns>? AND bucket_ns<? LIMIT 1",(start_ns-HOUR,end_ns)).fetchone() is not None

    @staticmethod
    def _lane(row):
        return (row["source"],row["signal"],row["entity"],row["unit"],row["scale"])

    def _summary(self,db,captured,start_ns,end_ns,resolution,signals=None,component=None):
        """Host-series rows, each lane at the finest stored resolution it has from
        `resolution` up (pruning can leave a source only hour rows). The unsaved tail joins
        windows under an hour, and longer ones that no save covers yet."""
        result=[];stored=[0];overflow=False;lanes=set();finest=None
        for res in RESOLUTIONS[RESOLUTIONS.index(resolution):]:
            found=[]
            for row in self._iter_summaries(db,captured,start_ns,end_ns,res,signals,component,series_only=True,stored=stored):
                if component and row["component"]!=component or self._lane(row) in lanes:continue
                # Bounded by the stored payload bytes read, not by re-encoding each row.
                if len(result)+len(found)>=MAX_SCAN or stored[0]>MAX_QUERY_BYTES:
                    overflow=True;break
                found.append(row)
            finest=finest or (res if found else None)
            lanes.update(self._lane(r) for r in found);result.extend(found)
            if overflow:break
        if captured and captured["tail"] and (end_ns-start_ns<HOUR or not self._saved(db,start_ns,end_ns)):
            raw,more=self._raw(db,captured,max(start_ns,min(r.end_ns for r in captured["tail"])),end_ns,component,True)
            overflow=overflow or more
            tail_points=[p for p in points(raw) if p["run_id"]==captured["run_id"] and p["sequence"]>captured["committed"] and (not signals or p["signal"] in signals)]
            result.extend(r for _,r in rollups(tail_points,WIDTHS[finest or resolution]) if r["start_ns"]<end_ns and r["end_ns"]>=start_ns)
        # With nothing stored, every resolution down to hour was tried.
        return result,overflow,finest or (resolution if result else RESOLUTIONS[-1])

    @staticmethod
    def _resolution(start_ns,end_ns,limit):
        """The finest stored resolution whose rows per lane fit the requested limit."""
        return next((r for r in RESOLUTIONS[:-1] if -(-(end_ns-start_ns)//WIDTHS[r])<=limit),"hour")

    def _who_summaries(self,db,captured,start_ns,end_ns,signal,component):
        # Full interior hours; 15-minute and minute rows only for the sides. No
        # input LIMIT can silently turn the first part of a day into the whole day.
        lo=(start_ns+HOUR-1)//HOUR*HOUR;hi=end_ns//HOUR*HOUR
        sides=[(start_ns,lo),(hi,end_ns)] if lo<hi else [(start_ns,end_ns)]
        spans=[("hour",lo,hi)] if lo<hi else []
        for a,b in sides:
            spans+=[("15-minute",a//QUARTER*QUARTER,b),("minute",a//MINUTE*MINUTE,b)]
        # Rows outside the window still show which minutes have a row; filter after choosing.
        # Without a named signal only component totals rank; skip the rest unbuilt.
        keep=None if signal else self._ranks
        wide=self._iter_summaries(db,captured,start_ns//QUARTER*QUARTER,end_ns+QUARTER,"hour",[signal] if signal else None,component,program_only=signal=="process.cpu",spans=spans,keep=keep)
        rows=(row for _,group in groupby(wide,key=lambda r:(r["source"],r["entity"])) for row in self._edge_rows(group,start_ns,end_ns)
              if row["start_ns"]<end_ns and row["end_ns"]>=start_ns)
        first=next(rows,None)
        if first is None:
            rows=self._iter_summaries(db,captured,start_ns,end_ns,"hour",[signal] if signal else None,component,program_only=signal=="process.cpu",keep=keep)
        else:
            rows=chain((first,),rows)
        current=None;merged={};edges={}
        def done():
            for key,rows in merged.items():
                row=merge_summaries(rows) if len(rows)>1 else rows[0]  # once, not pairwise
                # The coarsest grain that set an edge; hour only when no side row was used.
                row["summary_resolution"]=max(edges.get(key,{"hour"}),key=WIDTHS.get)
                yield row
        for row in rows:
            row["partial_boundary"]=row["start_ns"]<start_ns or row["end_ns"]>end_ns
            entity=(row["source"],row["entity"])
            if current is not None and entity!=current:
                yield from done();merged={};edges={}
            current=entity
            key=SUMMARY_KEY(row)
            merged.setdefault(key,[]).append(row)
            if row["summary_resolution"]!="hour":edges.setdefault(key,set()).add(row["summary_resolution"])
        yield from done()

    @staticmethod
    def _edge_rows(rows,start_ns,end_ns):
        """Per quarter hour of a side: minute rows when they cover each of its minutes in
        the window, otherwise the 15-minute rows. Minute rows ending before a 15-minute row
        begins predate 15-minute rows and still count."""
        key=SUMMARY_KEY
        quarters=defaultdict(lambda:([],[]))
        for row in rows:
            if row["summary_resolution"]=="hour":yield row
            else:quarters[row["summary_bucket_start_ns"]//QUARTER*QUARTER][row["summary_resolution"]=="minute"].append(row)
        for bucket,(coarse,fine) in quarters.items():
            needed=range(max(bucket,start_ns)//MINUTE*MINUTE,min(bucket+QUARTER,end_ns),MINUTE)
            if {r["summary_bucket_start_ns"] for r in fine}>=set(needed):
                yield from fine;continue
            yield from coarse
            begins={}
            for r in coarse:begins[key(r)]=min(begins.get(key(r),r["start_ns"]),r["start_ns"])
            yield from (r for r in fine if r["end_ns"]<begins.get(key(r),math.inf))

    @measured
    def now(self,component=None,limit=600):
        if not 1<=limit<=MAX_ROWS:raise ValueError("limit must be 1..2000")
        groups=self.collector.recent_records(component) if self.collector else []
        latest=[];trail=[];sources=[];raw_remaining=limit;truncated=False
        groups.sort(key=lambda group:any(f.entity.startswith("process:") for f in group[2][-1].reading.fields))
        for source,stale,records in groups:
            process_pass=any(f.entity.startswith("process:") for f in records[-1].reading.fields)
            history=records[-2:] if process_pass else records
            selected=[replace(r,reading=replace(r.reading,fields=tuple(f for f in r.reading.fields if not component or field_component(f,r.reading.component)==component))) for r in history]
            derived=points(selected)
            for p in derived:
                p["stale"]=stale
                if p["sequence"]==records[-1].sequence:latest.append(p)
                if not process_pass and not p["entity"].startswith("cpu:"):trail.append(p)
            raw_latest=selected[-1]
            available=len(raw_latest.reading.fields)
            fields=raw_latest.reading.fields[:raw_remaining]
            raw_remaining-=len(fields)
            raw=replace(raw_latest,reading=replace(raw_latest.reading,fields=fields)).to_dict()
            sources.append(dict(source=source,component=raw_latest.reading.component,status=raw_latest.reading.status,
                                stale=stale,latest=raw,trail=[],field_count=available,truncated=len(fields)<available,
                                trail_mode="derived-trail-rows"))
            truncated=truncated or len(fields)<available
        latest.sort(key=lambda p:(p["entity"].startswith(("process:","program:")),p["signal"]!="process.cpu",-(p["value"] if isinstance(p["value"],(int,float)) else 0)))
        result=self._base("now","observed" if sources else "unavailable")
        result.update(rows=latest[:limit],trail_rows=trail[-limit:],sources=sources,
                      truncated=truncated or len(latest)>limit or len(trail)>limit,
                      coverage={"mode":"live_cache" if self.collector else "store_only","reason":None if sources else "no_recent_readings",
                                "process_trail":"latest pass only; interval detail through who"})
        return result

    @measured
    def catalog(self):
        fields={}
        with self.store.read() as db:
            for row in db.execute("SELECT source,signal,component,kind,unit,scope,scale FROM signal_catalog LIMIT 2000"):
                p=dict(row);fields[(p["source"],p["signal"])]=p
        if self.collector:
            for source,_,records in self.collector.recent_records():
                for f in records[-1].reading.fields:
                    fields[(source,f.name)]=dict(source=source,component=field_component(f,records[-1].reading.component),signal=f.name,kind=f.kind,unit=f.unit,scope=f.scope)
        result=self._base("catalog")
        result.update(signals=sorted(fields.values(),key=lambda p:(p["component"],p["signal"])),components=sorted({p["component"] for p in fields.values()}))
        return result

    @staticmethod
    def _summary_gaps(rows,start_ns,end_ns):
        """Summary rows with an unmeasured status, or with sleep, become gap rows per
        source, status and reason over consecutive buckets. `reads` sums the samples of
        every lane; sleep counts each bucket once, since its lanes share one sleep."""
        buckets=defaultdict(dict)
        for row in rows:
            bad={s:n for s,n in row["statuses"].items() if s not in GOOD}
            if bad:key=(row["source"],max(bad,key=bad.get),"summary_status")
            elif row["sleep_s"]:key=(row["source"],"unobserved","window_contains_sleep")
            else:continue
            span=buckets[key].setdefault(row["summary_bucket_start_ns"],
                {"start_ns":row["start_ns"],"end_ns":row["end_ns"],"reads":0,"sleep_s":0.0,
                 "bucket_end_ns":row["summary_bucket_end_ns"],"summary_resolution":row["summary_resolution"]})
            span["start_ns"]=min(span["start_ns"],row["start_ns"]);span["end_ns"]=max(span["end_ns"],row["end_ns"])
            span["reads"]+=sum(bad.values());span["sleep_s"]=max(span["sleep_s"],row["sleep_s"])
        gaps=[]
        for (source,status,reason),spans in buckets.items():
            gap=None
            for bucket in sorted(spans):
                span=spans[bucket]
                if gap and gap["bucket_end_ns"]==bucket:
                    gap.update(end_ns=span["end_ns"],bucket_end_ns=span["bucket_end_ns"],reads=gap["reads"]+span["reads"],sleep_s=gap["sleep_s"]+span["sleep_s"])
                else:
                    gap=dict(span,source=source,status=status,reason=reason);gaps.append(gap)
        for gap in gaps:
            del gap["bucket_end_ns"]
            gap.update(start_ns=max(start_ns,gap["start_ns"]),end_ns=min(end_ns,gap["end_ns"]))
        return sorted(gaps,key=lambda g:(g["start_ns"],g["source"]))

    def _coverage(self,db,captured,start_ns,end_ns,records=(),component=None,derived=None,summary_gaps=None):
        """Reuse derived points only when they came from this exact record selection.
        A window answered from summaries passes their `summary_gaps` instead."""
        retention=[dict(r) for r in db.execute("SELECT * FROM retention WHERE end_ns>? AND start_ns<? LIMIT 2001",(start_ns,end_ns))]
        states=defaultdict(lambda:defaultdict(int))
        args=["minute" if end_ns-start_ns<=HOUR else "hour",start_ns,end_ns]
        clause=""
        if captured:
            clause+=" AND (run_id!=? OR sequence_max<=?)";args.extend([captured["run_id"],captured["committed"]])
        if component:
            clause+=" AND component=?";args.append(component)
        persisted=db.execute("SELECT source,payload,start_ns,end_ns,run_id FROM coverage WHERE resolution=? AND end_ns>=? AND start_ns<=?"+clause+" ORDER BY start_ns LIMIT ?",args+[MAX_SCAN+1]).fetchall()
        gaps=[];saved_gaps={}
        for source,payload,row_start,row_end,run_id in persisted[:MAX_SCAN]:
            data=json.loads(payload)
            for status,count in data["statuses"].items():states[source][status]+=count
            if records:continue  # decoded readings explain their own gaps below
            # Saved fieldless reasons; one run's consecutive rows with the same
            # reason form one gap, as decoded readings do.
            for status,reason,count in data.get("gaps",()):
                gap=saved_gaps.get((source,status,reason))
                if gap and gap["run_id"]==run_id:
                    gap["end_ns"]=min(end_ns,row_end);gap["reads"]+=count
                else:
                    gap={"source":source,"status":status,"reason":reason,"start_ns":max(start_ns,row_start),"end_ns":min(end_ns,row_end),"reads":count,"run_id":run_id}
                    saved_gaps[(source,status,reason)]=gap;gaps.append(gap)
        for gap in saved_gaps.values():del gap["run_id"]
        for source,bucket,res in sorted(getattr(self._local,"damaged",())):
            gaps.append({"status":"unreadable","reason":"summary_damaged","source":source,"start_ns":max(start_ns,bucket),
                         "end_ns":min(end_ns,bucket+WIDTHS[res]),"summary_resolution":res})
        if captured:
            for record in captured["tail"]:
                if start_ns<=record.end_ns<=end_ns and record.matches(component):
                    states[record.source][record.status]+=1
                    for status,count in record.field_statuses:
                        if status not in GOOD:states[record.source][status]+=count
        runs=[dict(r) for r in db.execute("SELECT * FROM runs WHERE started_ns<? ORDER BY started_ns",(end_ns,))]
        cursor=start_ns
        for index,run in enumerate(runs):
            start=max(start_ns,run["started_ns"])
            if start>cursor:gaps.append(dict(status="unobserved",reason="no_collector",start_ns=cursor,end_ns=start))
            last=run["last_saved_ns"] or run["started_ns"]
            if captured and captured["run_id"]==run["run_id"] and captured["tail"]:
                last=max(last,max(r.end_ns for r in captured["tail"]))
            stopped=run["stopped_ns"]
            next_start=runs[index+1]["started_ns"] if index+1<len(runs) else end_ns
            active=captured and captured["run_id"]==run["run_id"]
            effective_stop=min(end_ns,stopped or next_start)
            if not active and effective_stop>last:
                gaps.append(dict(status="unobserved",reason="after_last_save",start_ns=max(start_ns,last),end_ns=effective_stop,run_id=run["run_id"]))
            cursor=max(cursor,effective_stop if not stopped else min(stopped,end_ns))
        if cursor<end_ns:gaps.append(dict(status="unobserved",reason="no_collector",start_ns=cursor,end_ns=end_ns))
        gaps.extend(dict(r,status="pruned") for r in retention if r['kind']!='raw_preserved')
        empty_rows={}
        for record in records:
            reading=record.reading
            if reading.fields or reading.status in GOOD:
                empty_rows.pop(reading.source,None)
                continue
            reading_start,reading_end=reading.start.wall_ns,reading.end.wall_ns
            if reading_end<start_ns or reading_start>end_ns:continue
            reason=reading.reason or "source_status"
            previous=empty_rows.get(reading.source)
            if previous and previous[0]==record.run_id and (previous[1]["status"],previous[1]["reason"])==(reading.status,reason):
                previous[1]["end_ns"]=min(end_ns,reading_end)
                previous[1]["reads"]+=1
            else:
                row={"source":reading.source,"status":reading.status,"reason":reason,
                     "start_ns":max(start_ns,reading_start),"end_ns":min(end_ns,reading_end),"reads":1}
                gaps.append(row)
                empty_rows[reading.source]=(record.run_id,row)
        if summary_gaps is not None:gaps.extend(summary_gaps)
        def streamed_points():
            previous={}
            for record in records:
                # points copies this mapping. Advance it only after the whole
                # pass, preserving source baselines and per-pass program groups.
                yield from points([record],previous)
                previous[record.reading.source]=record
        # A field missing read after read (a closed ICMP path, an absent sensor) is one gap
        # row spanning those reads, with their count. A good read of it, or a reading of its
        # source that does not carry it, ends the row.
        open_rows={};record=None;carried=set()
        for p in derived if derived is not None else streamed_points():
            key=(p["run_id"],p["source"],p["signal"],p["entity"])
            if (p["run_id"],p["sequence"])!=record:
                if record:
                    for stale in [k for k in open_rows if k[:2]==(record[0],source) and k not in carried]:del open_rows[stale]
                record,source,carried=(p["run_id"],p["sequence"]),p["source"],set()
            carried.add(key)
            if p["end_ns"]<start_ns or p["start_ns"]>end_ns:continue
            if p["status"] not in GOOD or p["sleep_s"]:
                status=p["status"] if not p["sleep_s"] else "unobserved"
                reason=p.get("reason","window_contains_sleep" if p["sleep_s"] else "source_status")
                row=open_rows.get(key)
                if row and (row["status"],row["reason"])==(status,reason) and not p["sleep_s"]:
                    row["end_ns"]=min(end_ns,p["end_ns"]);row["reads"]+=1
                    continue
                row={"status":status,"reason":reason,"start_ns":max(start_ns,p["start_ns"]),"end_ns":min(end_ns,p["end_ns"]),
                     "source":p["source"],"signal":p["signal"],"entity":p["entity"],"sleep_s":p["sleep_s"],"reads":1}
                gaps.append(row);open_rows[key]=row
            else:
                open_rows.pop(key,None)
        return {"sources":{k:dict(v) for k,v in states.items()},"gaps":gaps[:MAX_ROWS],"gap_count":len(gaps),"retention":retention[:MAX_ROWS],
                "truncated":len(persisted)>MAX_SCAN or len(gaps)>MAX_ROWS or len(retention)>MAX_ROWS,"status_count_method":"overlapping saved source windows plus unsaved reads",
                "mode":"store_and_tail" if captured else "store_only"}

    @measured
    def series(self,start_ns,end_ns,signals=None,component=None,limit=600):
        self._bounds(start_ns,end_ns,limit)
        signals=list(signals or [])
        if len(signals)>32:
            raise ValueError("at most 32 signals")
        resolution="raw" if end_ns-start_ns<=10*MINUTE else self._resolution(start_ns,end_ns,limit)
        with self._snapshot(start_ns,end_ns) as (db,captured):
            records=[];rows=[]
            if resolution=="raw":
                width=1_000_000_000
                records,overflow=self._raw(db,captured,start_ns,end_ns,component,series_only=True)
                selected=[replace(r,reading=replace(r.reading,fields=tuple(f for f in r.reading.fields if is_series(field_component(f,r.reading.component),f.entity,f.scope) and (not component or field_component(f,r.reading.component)==component)))) for r in records]
                derived=[p for p in points(selected) if p["start_ns"]>=start_ns and p["end_ns"]<=end_ns]
                rows=[r for _,r in rollups(derived,width) if not signals or r["signal"] in signals]
                # Raw history may have been pruned; use retained summaries, explicitly.
                if not rows:resolution="minute"
            if resolution!="raw":
                rows,overflow,resolution=self._summary(db,captured,start_ns,end_ns,resolution,signals,component)
                width=WIDTHS[resolution]
            lanes={self._lane(r):r for r in rows}
            for requested in signals:
                if not any(key[1]==requested for key in lanes):
                    lanes[("unknown",requested,"host",None,1.0)]=dict(source="unknown",signal=requested,entity="host",component=component,kind=None,unit=None)
            # Output rows use equal-width bins; no interpolated values fill holes.
            bins=max(1,limit//max(1,len(lanes)))
            width=max(width,math.ceil((end_ns-start_ns)/bins))
            grouped=defaultdict(list)
            for row in rows:
                bucket=min(bins-1,max(0,(max(row["end_ns"]-1,row["start_ns"])-start_ns)//width))
                grouped[(bucket,row["source"],row["signal"],row["entity"],row["unit"],row["scale"])].append(row)
            merged=[]
            count=math.ceil((end_ns-start_ns)/width)
            for bucket in range(count):
                for key,meta in sorted(lanes.items()):
                    items=grouped.get((bucket,*key))
                    if not items and meta.get("kind") in {"counter","rate","accumulator"}:
                        lo=start_ns+bucket*width;hi=min(end_ns,start_ns+(bucket+1)*width)
                        covering=[r for r in rows if (r["source"],r["signal"],r["entity"],r["unit"],r["scale"])==key and r["status"] in GOOD and r["start_ns"]<hi and r["end_ns"]>lo]
                        if covering:
                            continue
                    row=merge_summaries(items) if items else {k:meta.get(k) for k in ("source","component","signal","entity","label","scope","kind","unit","rate_unit")}
                    if not items:row.update(start_ns=start_ns+bucket*width,end_ns=min(end_ns,start_ns+(bucket+1)*width),status="unobserved",count=0,total=None,mean=None,min=None,max=None,last=None,covered_s=0)
                    row["bucket_start_ns"]=start_ns+bucket*width
                    row["bucket_end_ns"]=min(end_ns,start_ns+(bucket+1)*width)
                    # A bin is one entity's merged rows: drop merge state, grouping fields and
                    # the first stored bucket's identity; bucket_start/end_ns name the bin.
                    for key in ("covered_windows","members","instance_count","partial_members","summary_bucket_start_ns","summary_bucket_end_ns"):
                        row.pop(key,None)
                    merged.append(row)
            coverage=self._coverage(db,captured,start_ns,end_ns,records,component)
        truncated=overflow or len(merged)>limit
        rows=merged[:limit]
        result=self._base("series","observed" if rows else "unobserved")
        measured=[r for r in rows if r["status"] in GOOD]
        result["status"]="observed" if measured else "unobserved"
        covered={"start_ns":min(r["start_ns"] for r in measured),"end_ns":max(r["end_ns"] for r in measured)} if measured else None
        result.update(requested={"start_ns":start_ns,"end_ns":end_ns},covered=covered,resolution=resolution,
                      rows=rows,gaps=coverage["gaps"],coverage=coverage,truncated=truncated,row_width_ns=width,
                      next_start_ns=covered["end_ns"] if truncated and covered else None)
        return result

    @measured
    def who(self,start_ns,end_ns,signal=None,component=None,limit=600):
        self._bounds(start_ns,end_ns,limit)
        if end_ns-start_ns>31*24*HOUR:raise ValueError("who interval must not exceed 31 days")
        with self._snapshot(start_ns,end_ns) as (db,captured):
            records,overflow=self._raw(db,captured,start_ns,end_ns,component) if end_ns-start_ns<=10*MINUTE else ([],False)
            records=self._who_records(records,signal)
            all_points=points(records)
            host_rows=[p for p in all_points if p["signal"]=="cpu.busy" and p["status"] in GOOD and p["start_ns"]>=start_ns and p["end_ns"]<=end_ns]
            groups=defaultdict(list)
            for p in all_points:
                if p["start_ns"]<start_ns or p["end_ns"]>end_ns or (signal and p["signal"]!=signal) or (component and p["component"]!=component):continue
                if self._who_row(p):
                    groups[(p["source"],p["signal"],p["entity"],p["unit"],p["scale"],p["scope"])].append(p)
            raw_rows=[summarize(v) for v in groups.values()]
            resolution="raw"
            if not raw_rows or overflow or end_ns-start_ns>10*MINUTE:
                candidates=self._who_summaries(db,captured,start_ns,end_ns,signal,component)
                resolution="summary";overflow=False
                # Tail fragments remain in the same source/instance aggregation.
                tail={}
                if captured and captured["tail"] and (end_ns-start_ns<HOUR or not self._saved(db,start_ns,end_ns)):
                    lo=max(start_ns,min(r.end_ns for r in captured["tail"]))
                    pending,more=self._raw(db,captured,lo,end_ns,component)
                    overflow=overflow or more
                    # CPU rank ties inherit the full pass's entity order.
                    # Preserve that input, including entities without CPU fields.
                    tail_records=pending if signal in {"process.cpu","self.cpu"} else self._who_records(pending,signal)
                    pending_points=[p for p in points(tail_records) if p["run_id"]==captured["run_id"] and p["sequence"]>captured["committed"] and (not signal or p["signal"]==signal) and (not component or p["component"]==component)]
                    for _,r in rollups(pending_points,end_ns-start_ns):
                        k=tuple(r[x] for x in ("source","signal","entity","unit","scale","scope"))
                        tail[k]=merge_summaries([tail[k],r]) if k in tail else r
                def with_tail(rows):
                    for r in rows:
                        k=tuple(r[x] for x in ("source","signal","entity","unit","scale","scope"))
                        extra=tail.pop(k,None)
                        yield merge_summaries([r,extra]) if extra else r
                    yield from tail.values()
                candidates=with_tail(candidates)
                if signal=="process.cpu":
                    host_rows=list(self._who_summaries(db,captured,start_ns,end_ns,"cpu.busy","cpu"))
            else:candidates=iter(raw_rows)
            # Only output is paged. All candidate programs contribute to totals;
            # each entity is merged and discarded before reading the next one.
            heap=[];count=0;attributed=None;lo=None;hi=None;partial=False;boundary=False
            for r in candidates:
                if not self._who_row(r):continue
                if signal is None and not self._ranks(r["signal"],r["kind"],r["component"]):continue
                count+=1;lo=r["start_ns"] if lo is None else min(lo,r["start_ns"]);hi=r["end_ns"] if hi is None else max(hi,r["end_ns"])
                partial=partial or r.get("partial_members",False);boundary=boundary or r.get("partial_boundary",False)
                if signal=="process.cpu" and r["total"] is not None:attributed=(attributed or 0)+r["total"]*r["scale"]
                entry=(r.get("mean_rate",r.get("mean")) or 0,count,r)
                if len(heap)<limit:heapq.heappush(heap,entry)
                elif entry[0]>heap[0][0]:heapq.heapreplace(heap,entry)
            rows=[entry[2] for entry in sorted(heap,reverse=True)]
            coverage=self._coverage(db,captured,start_ns,end_ns,records,component,derived=all_points)
        accounting={"additive":False,"reason":"signal_scopes_not_additive"}
        if signal=="process.cpu":
            host=[r for r in host_rows if r["total"] is not None]
            total=sum(r["total"]*r["scale"] for r in host) if host else None
            host_bounds=[min(r["start_ns"] for r in host),max(r["end_ns"] for r in host)] if host else None
            process_bounds=[lo,hi] if lo is not None else None
            matched=host_bounds is not None and host_bounds==process_bounds and len({r["source"] for r in host})==1
            compatible=matched and attributed is not None and total>=attributed
            accounting={"additive":True,"unit":"CPU seconds","attributed":attributed,"host_total":total,
                        "remainder":total-attributed if compatible else None,"status":"observed" if compatible else "invalid" if matched else "unobserved",
                        "reason":None if compatible else "attributed_exceeds_host" if matched else "unmatched_or_missing_host_window",
                        "attributed_bounds":process_bounds,"host_bounds":host_bounds,"partial":overflow or partial}
        result=self._base("who","observed" if rows else "unobserved")
        result.update(requested={"start_ns":start_ns,"end_ns":end_ns},covered={"start_ns":lo,"end_ns":hi} if lo is not None else None,
                      rows=rows,resolution=resolution,coverage=coverage,gaps=coverage["gaps"],
                      grouping="admitted-label-for-additive-fields; instance-for-overlapping-scopes",
                      denominator="union of aligned member windows in awake seconds",partial_boundary=boundary,
                      truncated=overflow or count>limit,total_rows=count,totals_complete=not overflow,accounting=accounting)
        return result

    @staticmethod
    def _who_records(records,signal):
        if not signal:return records
        wanted={signal}
        if signal in {"process.cpu","self.cpu"}:
            prefix=signal.split(".")[0];wanted.update((prefix+".user_time",prefix+".system_time","cpu.busy")+BUSY)
        return [replace(r,reading=replace(r.reading,fields=tuple(f for f in r.reading.fields if f.name in wanted))) for r in records]

    @staticmethod
    def _ranks(signal,kind,component):
        """Without a named signal: the component total, plus states and events (failed
        reads, identities), which remain evidence."""
        return kind in ("state","event") or signal in WHO_TOTALS.get(component,(signal,))

    @staticmethod
    def _who_row(row):
        return is_process_evidence(row["entity"],row["scope"]) and row["scope"]!="unit_detail" and (row["entity"].startswith("program:") or not row["additive"] or row["component"]=="self" or row["scope"]=="unit")

    @measured
    def coverage(self,start_ns,end_ns,component=None,limit=600):
        self._bounds(start_ns,end_ns,limit)
        with self._snapshot(start_ns,end_ns) as (db,captured):
            if end_ns-start_ns>10*MINUTE and self._saved(db,start_ns,end_ns):
                # Longer windows take gap rows from host-series summaries and decode no
                # pass; the unsaved tail still adds its status counts.
                # Gap edges come from the coarsest resolution that still gives the window
                # four buckets, so an hour reads quarter-hour rows, not every minute lane.
                saved=dict(captured,tail=[]) if captured else None
                resolution=next((r for r in reversed(RESOLUTIONS) if (end_ns-start_ns)//WIDTHS[r]>=4),RESOLUTIONS[0])
                stored=[0]
                rows=takewhile(lambda _:stored[0]<=MAX_QUERY_BYTES,
                               (r for r in self._iter_summaries(db,saved,start_ns,end_ns,resolution,component=component,series_only=True,stored=stored)
                                if not component or r["component"]==component))
                # Read them before _coverage, which names the damaged rows they meet.
                summary_gaps=self._summary_gaps(rows,start_ns,end_ns)
                coverage=self._coverage(db,captured,start_ns,end_ns,component=component,summary_gaps=summary_gaps)
                overflow=stored[0]>MAX_QUERY_BYTES
            else:
                # Whole process passes cost a derived point per field; their saved
                # status counts stay in coverage and per-process gaps stay in who.
                records,overflow=self._raw(db,captured,start_ns,end_ns,component,series_only=True)
                coverage=self._coverage(db,captured,start_ns,end_ns,records,component)
        result=self._base("coverage")
        result.update(requested={"start_ns":start_ns,"end_ns":end_ns},coverage=coverage,gaps=coverage["gaps"],truncated=overflow)
        return result

    @measured
    def around(self,at_ns,span_ns=MINUTE,component=None,signals=None,limit=600):
        start,end=at_ns-span_ns,at_ns+span_ns
        result=self._base("around")
        with self._snapshot(start,end) as snapshot:
            self._local.snapshot=snapshot
            try:
                result.update(series=self.series(start,end,signals,component,limit),who=self.who(start,end,component=component,limit=limit),coverage=self.coverage(start,end,component)["coverage"])
                db,captured=snapshot
                args=[start,end]
                clause=""
                if captured:
                    clause=" AND (run_id!=? OR sequence<=?)";args.extend([captured["run_id"],captured["committed"]])
                payloads,overflow=bounded_payloads(db.execute("SELECT payload FROM incidents WHERE wall_ns>=? AND wall_ns<=?"+clause+" LIMIT ?",args+[limit]))
                events=[Record.unpack(payload).to_dict() for payload in payloads]
                used=sum(Record.decoded_size(payload) for payload in payloads)
                if captured:
                    for pending in captured["tail"]:
                        if len(events)>=limit:break
                        if not start<=pending.end_ns<=end or not pending.has_events:continue
                        size=Record.decoded_size(pending.payload)
                        if used+size>MAX_QUERY_BYTES:
                            overflow=True;break
                        events.append(self._decode_pending(captured,pending).to_dict());used+=size
                result["coverage"]["truncated"] = result["coverage"]["truncated"] or overflow
                result["events"]=events[:limit]
            finally:
                self._local.snapshot=None
        return result

    @measured
    def status(self):
        result=self._base("status")
        result.update(collector=self.collector.status() if self.collector else None,budget=self.store.budget.usage(),coverage={"mode":"live" if self.collector else "store_only"})
        from .onboarding import observed_permissions
        result['permissions'] = observed_permissions(self.config or self.store.path.parent)
        return result

    @measured
    def storage_query(self,action="volumes",**params):
        if self.storage is None:
            return dict(self._base("storage","unavailable"),reason="scanner_adapter_not_available",rows=[])
        return self.storage.query(action,**params)
