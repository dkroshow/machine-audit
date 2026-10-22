# Query design

How the dashboard and agents ask about a selected interval and what an answer
contains. This is the step after raw parsing: readings are saved as-is, the
[storage approach](storage-approach.md) derives summaries beside them, and one
query module answers from both. The [architecture](architecture.md) owns the
process, store and transport; the component views own what each signal means.
A design agreed in conversation, not built or validated.

Humans and agents are two equal users of one layer. The dashboard draws from
it; an agent gets the same answer as JSON with units, status and coverage
stated. Neither interface interprets evidence the other cannot see.

## Five answer shapes

An earlier prototype's dashboard, read tab by tab, asks five kinds of question
repeatedly. Each becomes one answer shape that works the same for every
component. A component contributes named signals, not its own routes.

| Shape | Question | Answered from |
| --- | --- | --- |
| now | What is happening at this moment, with a short trail | The collector's recent-read cache; no database read |
| series | How did these signals move over this interval | Raw readings, minute or hourly rollups by span |
| who | Which workloads used or waited for this resource over this interval | Per-instance and per-program rollups; raw passes while retained |
| around | What was the machine doing just before and after this moment | Series plus who, centered on a time, with events |
| coverage | What was measured, missed, denied or pruned in this interval | Read status, gaps and retention cutoffs |

On-demand probes that start new collection (such as a CPU diagnosis
or a network test) are not queries and are outside this document.

**now.** One object per request: the latest reading per requested source, its
recent trail (the last two minutes at source cadence), and the latest process
pass ranked on request. It is answered from a **recent-read cache**: the
collector keeps, per source, the latest reading and a fixed number of recent
readings as read-only copies, whether or not they have been saved. This cache
is separate from the unsaved batch the architecture flushes each minute, which
cannot supply a slow source's latest value or a two-minute trail after a
flush. *now* never reports its entries as unsaved and *series* never reads
them, so nothing is counted twice. After a start the cache is empty and the
answer says so; the store-only command line has no cache and answers no
*now*.

```
ask:    now cpu
answer: t=14:32:07, cpu: {busy_cpus: 3.1, usable_cpus: 24, peak_this_minute: 11.2,
          excess_runnable: {value: 2, status: observed}, trail: [120 one-second points]}
        processes: {pass_t: 14:32:00, window_s: 10, top: [...], remainder_s: 0.4,
          uncovered: ["other users' processes"]}
```

**series.** Rows of equal width for one or more named signals over the
selected interval. The caller picks the interval; the layer picks the finest
stored resolution that fits a row cap and names it. Counter rows carry total
and covered seconds. Gauge rows carry count, mean, min, max with its time, and
last. A row with nothing measured is present and marked, never zero or omitted.
Gaps (sleep, collector stopped, denied, pruned) are spans inside the answer so
a chart can shade them and an agent can read them.

```
ask:    series cpu.busy, cpu.excess_runnable  from 14:00 to 16:00
answer: resolution=minute, covered=14:00–15:47
        rows: [{t: 14:00, busy_s: 38.2, awake_s: 60,
                excess_runnable: {mean: 0.1, max: 2, max_t: 14:00:41, n: 60}}, ...]
        gaps: [{kind: sleep, 14:31–14:52}, {kind: no_collector, 15:47–16:00}]
        signals: {cpu.excess_runnable: {kind: gauge, unit: threads, status: observed}}
```

**who.** Workloads ranked over the interval, by use and separately by waiting,
with the grouping method, the permission level that produced them, and what
was uncovered. Where the accounting is additive, as CPU time is, the answer
also gives the attributed total and the remainder against the whole machine.
Memory footprints overlap and are never subtracted from host use; memory's
*who* carries coverage only, as the [memory view](memory-view.md) requires.

Rows are programs, that is process instances grouped by admitted label, so a
restarted program is one row and its instance count stays available.
Aggregation is per signal: only compatible additive fields sum across
instances, CPU time among them, and memory footprints follow the memory
view's overlap rules inside a program group as well as against the host. A
program's elapsed time is the union of its members' covered intervals under
the recorded coverage method, with partial member coverage explicit; summed
instance-seconds, if kept, are named as such and never used as that
denominator. Two processes each using 10 CPU-seconds in the same 10-second
window are 20 CPU-seconds over 10 elapsed seconds, 2 CPUs, not 1. Peaks,
time above a level and histograms do not sum: the maximum of a sum is neither
the maximum of the maxima nor their sum, and per-instance summaries lose
whether two instances were busy at the same moment. Two instances that each
peak at 4 cores show a program peak of 8 cores if the peaks were in the same
window and 4 if they were not; instance rollups alone cannot tell which. See
[program spikes](#program-spikes).

**around.** A moment plus a span on each side (a minute each way by default):
the series for the component's headline signals at the finest retained
resolution, who was busy and who waited, and the events in the span (pressure
level changes, thermal state, limit events, collector restarts). This is the
click-through from a spike. It composes series, who and coverage; it adds no
new storage.

**coverage.** For an interval: which sources read successfully, were late,
denied, failed or not enabled; sleep and collector gaps; where retained detail
ends and only summaries remain. Every other
shape embeds the coverage it depends on; this shape answers it directly.

**Monitoring cost.** The product brief defines the
[self-cost evidence](product-brief.md#cost-of-monitoring). It uses these same shapes:
*now* for recent cost, *series* for history, and *around* for a cost spike, with
available component breakdowns and *coverage*. The dashboard gives this view a
direct entry; agents can request it without searching all process rows. Cost
includes all measured Machine-audit components, not only the collector process.

## Interval and resolution rules

- **Any interval.** The caller gives two times, or a span ending now. One
  minute, five minutes, an hour and a day are examples for a slider, not a
  fixed menu. The layer rejects nothing it can cover in part.
- **Covered span, not requested span.** The answer returns the bounds it
  covered. A request that cuts a stored window is answered by the covered
  windows, as the [time and coverage rules](cpu-view.md#time-windows-and-coverage-working)
  say; the method is visible in the answer.
- **Resolution follows the span and a row cap.** Raw readings for a few
  minutes, minute rollups for hours, hourly rollups for days. The answer
  names its resolution and the cadence behind it. Nothing is interpolated.
- **Peaks keep their meaning.** A maximum is the maximum of the samples the
  rollup saw, with its time. A process peak is a 10-second pass rate; a host
  peak is a 1-second reading. Answers say which, and never mix them.
- **Bounded work.** A small fixed number of queries run at once. Each answer
  is bounded by its interval, a row cap and a tail copy limited to what it
  asked for. A longer span is answered from coarser rollups or paged with a
  cursor, and the answer says so. Nothing is truncated silently.

## Status vocabulary

Every signal in every answer carries one status. The dashboard renders it;
an agent branches on it. Zero is a measurement, never a stand-in.

| Status | Meaning |
| --- | --- |
| observed | Read and valid for this window |
| measured_zero | Read, and the value was zero |
| unavailable | This OS or device has no such signal |
| not_enabled | Optional access exists and was not turned on |
| denied | The OS refused the read |
| failed | The read errored; the error is recorded |
| invalid | Read, but a counter went backwards or identity changed; this field only |
| unobserved | No read covered this span (sleep, collector stopped, skipped) |
| pruned | Detail was retained once and has been removed under the budget |

## The process table

The use that shaped this design: on the CPU page, a slider for the selected
span and a table of programs, one row per program, sortable by what happened
to it over that span.

| Program | now | 5 min | 1 h | 24 h | peak (24 h) | hours hot | waited |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Chrome | 2.1 cores | 1.8 | 0.9 | 0.4 | 11.2 at 14:32 | 3 | 0.3 threads |
| mds_stores | 0 | 0 | 0.1 | 0.6 | 7.8 at 03:10 | 1 | not measured |
| Outside your programs | 0.4 | | | | | | |

Over a day an average hides what matters. The table is built for spikes: the
highest pass rate and when, time spent above a few fixed levels, and the
per-hour maxima for a sparkline. Sorting a day by "hours hot" separates a
program that spiked once from one that spikes all day. Clicking a peak asks
*around* that time.

The short columns come from the coherent snapshot of store plus unsaved
tail, the long columns from per-program rollups; sorting is one query over one
table. The recent-read cache serves only *now*, with each entry's actual read
time and a stale or missing status visible. The
same table serves memory (footprint, growth, swap-ins per program) with the
memory view's caution that footprints overlap and do not sum to host use.

### Program spikes

The day-long program spike view needs time-aligned program totals, which
instance rollups cannot supply once the passes behind them are gone. The
working design, to validate: at each pass the collector also derives the
per-program total of each compatible additive signal (instances summed by
admitted label, with the grouping and coverage method recorded on the row),
and rolls that series up per minute and per hour with the same spike fields
as an instance. Members' actual read times differ within a pass, so the row
records how they were aligned rather than treating the pass as one instant.
Instance rollups and instance identity are kept unchanged beside it; the
program rollup is a bounded additional derived summary, rebuildable from
retained passes, and its grouping can be revised while they exist. Without
it, program peaks, hours hot and histograms over pruned history are
unavailable, not approximate, and the answer must say so.

## Kept ready, or computed on request

**Kept ready** (written with the readings, per
[preservation contract](storage-approach.md#preserve-readings-derive-summaries)):

- Minute and hourly rollups per host signal, carrying the summary family their
  kind requires, with max and its time.
- Minute and hourly rollups per process instance and per signal, with the
  spike fields listed in the storage approach, and the proposed per-program
  rollups above. These are the additions the query design makes to the
  architecture's rollups. Their size and query cost are not measured; they
  follow the storage approach's rule of measuring growth before choosing
  reductions.
- Coverage per source per minute: reads due, done, late, skipped, failed.
- The recent-read cache for *now*, owned by the collector.

**Computed on request:** ranking a single pass; summing additive instance
fields by label; reading raw readings for a short window; composing *around*.
None of these touch more than the interval asked for.

Rollups are caches. Each carries a method version and is rebuildable from
retained readings. Choosing a rollup field wrongly costs a rebuild while the
detail exists. The only choices that cannot be revisited are what a rollup
carries once its readings are pruned, and the process identity it is keyed
on. The storage approach lists both.

## Pruned history

Pruning follows the [retention order](storage-approach.md#requirements):
oldest readings first, then oldest minute rollups, hourly and incident records
longest. Answers over a span that crosses a cutoff say where detail ends:

- *series* names its resolution per span and marks the pruned range.
- *who* keeps every instance's rollup; there is no top-N floor and nothing
  is dropped below a size. Additive totals over complete covered windows stay
  exact, and a new grouping's additive totals can still be formed from
  retained instance totals. What is lost is within-window order, the detailed
  readings and burst shape around the retained peak time, and a new
  grouping's peaks, time above and histograms; the answer names which fields
  are exact and which are unavailable for the span.
- *around* a pruned time returns rollup rows and events, and says the pass
  behind a peak is gone.
- A maximum survives pruning with its time; the shape of the burst around it
  does not.

## Memory, briefly

The shapes hold for memory without CPU's dimensions. *now* and *series* carry
usage by page class, movement counters by kind in source units, and the
pressure state with its observation times. *who* ranks by footprint and
separately by movement experienced (page-ins, swap-ins, decompressions), with
overlapping scopes kept apart as the [memory view](memory-view.md) requires.
*around* a pressure change shows who grew and who paid. Linux PSI stall time
and macOS pressure level are different signals with different statuses, never
one "waiting" number.

## Not in this document

Endpoint paths, the JSON schema, the row cap's value, the fixed "time above"
levels, dashboard layout, and remote upload. These follow from the shapes
above and from measured growth; the builder records them and submits them
for review.
