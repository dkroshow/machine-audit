# Storage approach

The MVP preserves useful observations so humans and agents can investigate
incidents and improve the analysis later, in one local SQLite store.

## Requirements

The readings store exists to answer four questions, for the present and looking back.
Every keep and retention rule below comes from one of them; none is a byte target.

1. **Was this machine constrained, when, and for how long?** Across CPU, memory, disk
   activity, network and disk space. Host rows (host, devices, volumes, units, self)
   answer it, at hour resolution for the whole span and finer for recent time.
2. **What were the main things responsible each time?** Each bucket's
   [main contributors](#main-contributors), with one rest row per kind so totals
   reconcile and the tail shows as a quantity.
3. **What is growing?** Program and host hour rows over the whole span.
4. **What happened in detail around a recent event?** Raw readings: process passes
   briefly after they are summarized, host readings longer, since they show spikes
   and cost little.

Disk space history comes from the host's volume readings (`storage.used`,
`storage.free` per volume), kept in hour rows for the whole span, and from the
scanner's generations.

- **10 GB limit per device; alerts start at 5 GB.** Account for the database,
  write logs and separate filesystem index together, not as independent caps. The
  limit is an emergency stop, not a retention policy: see [below](#the-cap-is-an-emergency-stop).
- **Preserve individual readings at collection cadence**, including process
  and GPU-client detail, for as long as their retention. Build summaries alongside
  them for efficient queries.

### Retention

Counted back from the newest save; each tier expires a little at every save, oldest
first, one counted transaction at a time, recorded with reason `retention`. Raw
readings expire only once proven summarized (below).

| History | Kept | Question | Why this value |
| --- | --- | --- | --- |
| Process passes (raw readings whose every field is process or workload evidence) | 24 hours | 4 | An event is usually looked at the same day or the next morning. After that, minute rows keep each minute's main contributors and rest, which is the useful part of a pass; the passes themselves are 90% of raw (Big Mac: 1.7 MB a minute). |
| Host raw readings (everything else) | 3 days | 1, 4 | Spike shape at the source's own cadence (1–60 s) for the last few days, so a problem from Friday can still be looked at on Monday. About a tenth of raw. |
| Minute and 15-minute rows | 3 days | 1, 2 | The resolutions views of hours and days use (minute rows for windows up to about 10 hours, 15-minute rows to about 6 days at the default 600 rows). Kept as long as the host readings they summarize, so the recent few days zoom to the minute. |
| Hour rows | Whole span | 1, 2, 3 | The long history. Host rows are never pruned ahead of anything else. |
| Incidents | Whole span | 1 | Rare, small records of what changed state. |

These are the only retention numbers. The 1% share that decides which processes and
programs keep their own rows is under [main contributors](#main-contributors).

### The cap is an emergency stop

Everything Machine-audit stores, the scanner's map included, shares the 10 GB cap.
Retention, not the cap, decides what history exists. Only above 90% of the cap does
history go to make room, oldest first in this order: process passes, host raw
readings, minute rows, 15-minute rows, hour rows, and incidents last. Host and
program hour rows are the last rows to go. Each such deletion is recorded with reason
`storage_cap`, which coverage reports as a pruned gap, and the collector records an
event, `self.storage_cap_pruning`, when emergency pruning starts and when it stops;
its status shows `storage_cap_pruning`. A save is always admitted: recording never
stops for the cap.

Why 90%: the last tenth of the cap leaves room for the largest counted write's
journal and checkpoint (a 4,096-row prune) and a concurrent scanner transaction, so a
save that needs room can still be admitted while history is pruned.

**Estimated outcome on Big Mac (reference only, an outcome not a target).** From
measured rates (raw 1.88 MB a minute; host rows, main contributors and the dictionary
applied to recorded rows), not yet measured in place:

| History | Estimated size |
| --- | ---: |
| Process passes, 24 hours | 2.4 GB |
| Host raw readings, 3 days | 0.8 GB |
| Minute rows, 3 days | about 0.8 GB |
| 15-minute rows and minute coverage, 3 days | about 0.15 GB |
| Hour rows and their coverage | about 12 MB a day, growing |
| Scanner map (not this store) | 3.7 GB |

That is about 7.8 GB at first. The hour rows grow with no retention, so on Big Mac the
cap is reached after about three months; from then the oldest process passes go first
and each deletion is recorded. About a third of the hour growth is coverage rows,
which are saved per save rather than per hour. A small Ubuntu box (0.32 MB of raw
readings a minute, its map 9 MB) stays far below the cap for years.

### Upgrade is one way

A store this release has written is version 3 once converted, its bundles use the
[preset dictionary](#main-contributors), and its stop checkpoints are format 3 (the
process passes of the unfinished minute). It still recovers formats 1 and 2 from the
releases before it. A release before it can read none of these, so going back means
starting a new store.

The scanner's map is format 4 once converted: folders measure physical space
([storage view](storage-view.md#source-to-record-to-answer)). Conversion retires
every map for one full walk and keeps points, tracked folders, hierarchy and run
evidence. Releases before it refuse format 4, so going back means restoring a copy
of the format 3 file taken before the upgrade, or starting a new map.

Counted writes reserve their transaction's growth and journal pages, rather
than another copy of the entire database. Emergency pruning starts at 90% of the
combined cap; admission can prune earlier when the pending transaction needs more
room. See the measured [recording costs](development.md#review-regressions-and-scale-measurements).

For page size `p`, a transaction declaring `R` row mutations and `P` payload
bytes reserves `F = 64 + 16R + ceil(P / (p - 64))` dirty-or-new pages. The
64-page fixed allowance covers metadata; 16 pages per mutation cover table and
index changes; payload pages cover overflow. Callers count every repeated write,
including component indexes, summaries, catalog, baseline, coverage and events.
The writer reserves its existing database once, at most `F*p` growth, a WAL of
`32 + min(F, admitted database pages)*(p+24)` bytes, and the corresponding
32 KiB shared-memory index chunks. Other managed files and active reservations
are added once. Admission reduces possible database growth to fit available
headroom and sets SQLite `max_page_count` accordingly. Cache spilling remains
off; each transaction checkpoints before the next admission.

Collector flushes supply actual row and packed-payload counts. Scanner buffered
inserts, sharing's stream and term rows among them, are bounded at 8,192
operations or 2 MiB of arguments, aggregate updates at 8,192 rows and
obsolete-generation deletes at 4,096 rows. Regressions measure their WAL frames against the declared allowance.
This factor is a product transaction contract, not a general SQLite guarantee.
Scanner sharing and ancestor totals are staged in counted batches. A full walk's
publication counts upper bounds for its point, hierarchy and metadata writes and
variable payload; one transaction keeps map rows, original points, hierarchy and
the volume pointer atomic. An update publishes its points, hierarchy, dirty-record
removal and delta pointer in one small counted transaction. It then merges the
delta in batches of at most 4,096 rows, each declaring its actual rows and payload:
first the base rows the delta removes, then one insert-or-update per staged row
that differs from its base row (null-safe, every column but the generation). A
staged row equal to its base row is neither copied nor counted. The merge does not
delete staged rows: once it ends, the generation is unpublished again and the next
preparation prunes it in batches that declare their actual rows. Its cost follows
the changed rows, not unrelated retained history or a re-listed folder's unchanged
files. A large update can still exceed available headroom before it publishes;
that refusal preserves the prior map, and the scanner recovers by one full walk (see
[source validation](source-validation.md#scanner-budget-recovery)).
An existing complete scanner schema needs no startup write. Schema creation
counts its fixed metadata and any rows used to build missing indexes. The
generic budget API retains the whole-database fallback for uncounted callers;
product scanner writes declare counts. Arbitrary SQL is not admitted through
the counted-write API.

Startup and flush both retry admission after pruning, without a fixed number
of history windows. Pruning uses at most 4,096 primary rows and 4,096 related
coverage rows per transaction, halving the batch when necessary. It reserves
64 fixed pages, 16 pages per removed row for table/index work, the payload's
overflow pages, and eight pages per incremental-vacuum step (at most 4,096).
Up to eight new database pages allow retention metadata to be recorded.
The shared reservation covers the existing database plus this bounded WAL,
possible database growth and WAL shared memory; it does not require a second
copy of the entire database to delete old data. `max_page_count` enforces
the admitted database-length limit. Each committed prune batch checkpoints
before the next admission. Retention records expose the affected time windows,
including partially removed windows; raw data without saved coverage is kept.
This bounded pruning path lets recovery follow the order above, retaining
incident history longest, even when the ordinary whole-database write
reservation no longer fits.

If crash recovery leaves a raw window without summary coverage, keep that
window and search forward for the oldest fully covered raw window. In the
emergency, only after the covered raw windows are gone may pruning continue
into summaries. Coverage evidence must match both sequence and
observation time; a later high sequence alone does not prove an earlier window
was summarized. Each retained window is recorded as `raw_preserved` with reason
`missing_summary_coverage`. These records appear in retention history, but are
excluded from pruned gaps and export `pruned_ranges`. Summary deletions made
while raw is retained carry reason `storage_cap_raw_uncovered`. Uncovered
raw must neither block startup recovery nor cause covered raw to accumulate.

The raw-pruning proof searches the existing coverage-time index backward from
each reading. It retains the run, source, sequence and time checks; any matching
fragment proves coverage. The read-only candidate search stops after one million
SQLite VM instructions or one second, checked every 1,000 instructions and
between windows. Each search passes over windows already recorded as
`raw_preserved`, so it resumes where the last one stopped. A search that runs
out before a covered window prunes the oldest summaries instead, with reason
`storage_cap_raw_uncovered`, and records the uncovered windows it found.
Only when nothing else remains does it report `prune_precheck_limit`, without
deleting history or discarding pending readings. Native I/O must return before
SQLite can check its progress limit. No additional index or background scan is
needed.
The regression uses 32,768 covered raw rows, an older uncovered row and 33,000
coverage fragments. Its two coverage proofs use about 4,000 VM instructions;
the previous query exceeds 100,000. This is synthetic query work, not a live
machine latency measurement.

Alerts appear in the dashboard and agent output, with status events;
external notification delivery is outside the current scope.

## Collection baseline

These are accepted starting cadences, to validate against measured cost on the
target machines. There is **no active-versus-idle slowdown**. Sleep, denied
access and failed reads remain explicit gaps. A platform supplies the fields it
exposes; matching cadence does not make different signals equivalent.

| Measurement | Cadence | Scope and first sources |
| --- | --- | --- |
| CPU usage and runnable demand | 1 s | macOS host CPU ticks and processor-set load; Ubuntu `/proc/stat` and supported host contention counters |
| Machine memory, swap and pressure | 1 s | macOS VM page classes/counters, swap usage, pressure level and swap-housekeeping counters; Ubuntu `meminfo`, `vmstat`, host memory PSI |
| User-process CPU, memory and disk | 30 s ([cost](#collector-cost)) | macOS `proc_pid_rusage` CPU, runnable time, footprint and disk bytes, plus `TASK_VM_INFO` swap-ins and decompressions; Ubuntu process CPU, `status` resident set and swap, and disk bytes where readable |
| System-process CPU and memory via Mac helper | About 60 s (planned) | Helper not shipped yet; separate privileged source, not an idle setting |
| Ubuntu workload-group memory | 30 s, in the same pass as its processes | cgroup usage, movement, PSI and event counters; use one non-overlapping level for totals |
| GPU device activity and memory | 5 s | Per GPU; IORegistry on macOS, supported driver interfaces on Ubuntu; include exposed engines, memory classes, clocks and limits |
| GPU client/process attribution | 10 s | macOS client time; Ubuntu driver client/engine time and memory where available |
| Disk operations, bytes and busy time; Ubuntu I/O pressure | 5 s | Per physical disk where exposed; Ubuntu `/proc/pressure/io` and group `io.pressure` |
| Main temperatures, reported power, CPU speed limit, battery | 5 s | Available device/sensor readings |
| Full temperature-sensor sweep | 60 s | Available sensors |
| Fans | 5 s | Keep the prototype's cost-based 60 s fallback for slow sensor reads |
| CPU-package power and frequency via optional helper | 30 s (planned) | Helper not shipped yet |
| Network traffic and Wi-Fi link | 10 s | Interface counters and available link fields |
| Router/internet latency and loss | 5 s | Existing alternating internet targets each receive a probe every 10 s |
| DNS and connection probes | 60 s | Existing bounded probes |
| Free disk space; optional process network attribution | 60 s | Separate capacity and attribution reads; the drive's parts are read live when shown |
| Workload memory limits | At discovery, then 60 s | Exposed limits and protections; record when changes are observed |
| GPU identity and fixed capabilities | At discovery and device changes | Inventory, not repeated full inspection |
| Thermal, pressure, network-path and diagnostic events | As observed | Preserve available event times separately from receipt times |
| Filesystem map | macOS: hourly change-log updates, weekly full scan. Ubuntu: nightly full re-walk | Separate scanner process; one walk of every file, stored at a per-machine grain; folder size points at 1 GB; see the [storage view](storage-view.md) |

The [component views](machine-information.md) explain source meanings and gaps.
The scanner's walk cost in Python is unmeasured and gates its cadences. New process
reads and Ubuntu GPU-client scans need cost measurements. Optional expensive/privileged diagnostics are not
routine baseline reads. The [GPU read-side-effect rule](gpu-view.md#read-side-effects)
applies even when only selected fields from a driver response are retained.

## Preserve readings; derive summaries

Keep source values in their original units with stable identity, observation
times, source/version and read status. Preserve cumulative counter snapshots;
derive validated differences later. Keep gauges as samples and events as
bounded, privacy-filtered records. Changed capacity or limits carry their
observation time, not an invented exact change time.

Each discovered process has an observation at each scheduled pass, or an explicit
gap; processes hidden from discovery remain a coverage limitation.
Lossless encoding may avoid repeating unchanged values only if it reconstructs
the original samples: initial values, every field change, identity/reset/status
transitions and complete read coverage. Sparse *derived effect rows* do not
replace memory gauges or source counter baselines. An unchanged reading differs
from a missing reading.

Minute, 15-minute and hourly summaries accelerate queries alongside the
detailed store.
Only the agreed retention policy may retire older detail. Counter summaries
retain totals and covered duration. Gauge summaries
retain count, sum, min, max and last, with weighting and method version; a
weighted mean needs its weights or time integral. Observed states retain readings
and notifications; durations are estimates. Events retain identity, reason and
bounded context separately from summary counts.

**Per-scope summary resolution.** A scope may keep hour summaries only, when
no minute-grain answer reads it and its values change slowly. Raw readings
still answer recent intervals, and pruning removes them as for any other
scope. The rule lives in one set in `store.py`. Its first user is the Linux
`unit_detail` scope: unit limits, memory breakdowns, pressure, throttling and
process counts. Mac scopes all keep every resolution.

Per-process and per-program rollups, per signal, for each bucket's main
contributors and one rest row per kind, serve the [query design](query-design.md)'s
process table and spike questions over long spans. Beyond the summary family above
they keep the highest pass rate with its time, retained as observed, plus seconds
above a small fixed set of levels and a small histogram of pass rates, both weighted
by each pass's covered duration. The histogram answers "time above a level" exactly
only at a bin boundary; inside a bin it bounds or estimates. All three describe
pass-average rates: a burst shorter than the process cadence is averaged inside its
pass and cannot be recovered. Rollups are keyed by process instance (pid, start time,
boot). Compatible additive fields sum across instances; peaks, time above and
histograms do not, so program-level spikes use the query design's time-aligned
per-program rollup, with its grouping and coverage method recorded. A program
rollup's instances stay listed in its `members`. Every rollup carries a method
version and can be rebuilt from retained readings; what it carries is fixed only
once those readings are pruned.

Summaries lose sample order, precise timing and the ability to regroup discarded
identities. A stored maximum can preserve an observed peak's magnitude, but not
its moment unless that time is retained. Nothing reconstructs activity between
reads. Shared [time and coverage rules](cpu-view.md#time-windows-and-coverage-working)
govern the selected interval in both interfaces.

### Summary resolutions

Every resolution keeps the same rows: every host, device, volume, unit and self
entity, and each bucket's [main contributors](#main-contributors) among processes
and programs, with one rest row for the others of each kind.

#### Main contributors

The question these rows answer is what was responsible for a component's use in an
interval. The answer is an 80/20 one: name what mattered and say how much the rest
added up to, rather than keep every process.

**The rule.** For each bucket, component and kind (process or program), an entity
keeps its own row when its use was at least `SHARE` of that kind's whole use of the
component in that bucket. Use is CPU seconds for CPU, bytes read and written for
disk, and footprint summed over passes for memory (Mac footprint, Linux resident
set), so a process present for part of the bucket counts for the part it was there.
The others of that kind become one rest row, entity `rest:process` or
`rest:program`, that reads like a single entity: its total is their combined total,
its count is the bucket's passes so its mean is their combined level, `entities`
says how many it holds, and `cut` is the use an entity needed to be kept. Totals
therefore add up, and a long tail that matters shows as a large rest. Host,
device, volume, unit and self rows are not ranked. `SHARE` and the rule live in
`store.py`, for the Mac and Linux alike.

**Why 1%.** An entity under 1% of an interval's use of a component did not drive
it. At most 100 entities of a kind can reach 1%, so a bucket can never keep more
than that, and a busy bucket with many real contributors keeps more rows than a
bucket one process dominates. Measured on Big Mac's recorded hours (2,000–3,900
processes and about 515 programs an hour, 24 CPUs), 1% kept:

| Each hour | Kept | Share of that use they hold |
| --- | ---: | ---: |
| CPU, programs | 7–10 | 90–96% |
| CPU, processes | 14–28 | 54–76% |
| Disk bytes, programs | 7–12 | 93–98% |
| Disk bytes, processes | 10–17 | 83–91% |
| Memory, processes | 3–6 | 31–46% |

Minutes kept about the same: 55–70 process and program rows a minute. Programs carry
CPU and disk almost entirely. Memory is the exception: one process holds about 30%
and thousands of small ones the rest, so the memory rest row holds 54–69% of process
footprint. Programs have no memory rows (footprints overlap and are not added), so
that tail is a quantity, not a list.

**One summary per minute, and only of what is kept.** Saves are not aligned with
minutes, so the collector keeps the process passes of the unfinished minute in memory
(their readings are already saved) and summarizes a minute once, when a later pass
completes it. It ranks the minute's processes and programs by their use, summarizes
only those kept, and builds each rest row straight from the others' points, so the
long tail costs one pass over its points and no summary per process. Fields of a pass
that never rank (failed process reads, Linux units) are summarized at once with the
host readings. Data arriving after the ranking extends the rows it kept and adds the
others to its rest.

A quarter hour or hour is ranked when the next one begins, or at a stop, from the
minute rows saved for it: each minute's main contributors and rest. Their rows are
sorted first, so the same readings give the same rows, to the last digit, in any run. An entity is a
main contributor of a quarter or hour only if it was one in at least one of its
minutes (under 1% of every minute is under 1% of the sum), and its row sums the
minutes it mattered in; its other minutes are in the rest, so totals still add up.
Replayed on a recorded quarter hour of Big Mac, this kept the same main contributors
as ranking every process over the whole quarter, except one marginal entity in three
of five lists; kept rows held 1–2 points less of the use and the worst kept row 78–84%
of its own. A rest row's entity count over a quarter or hour is the most it held in
one minute, a floor. Stopping the run summarizes the unfinished minute and ranks the
open quarter and hour; a stop that only checkpoints carries the unfinished minute's
passes. A crash loses only that minute's process and program rows: the next start
ranks the run's open quarter and hour from its saved minute rows.

Each save reserves room in proportion to the rows it writes. A save that does
not fit prunes the oldest history and tries again; it merges its rows once,
whatever the number of attempts.

**Answers across resolutions.** `series` uses minute rows; if they are pruned,
15-minute rows, then hour rows, and names the resolution it used. `who` uses
complete interior hours and, at each side, per entity and quarter hour: its
minute rows when it has one for every minute of that quarter hour inside the
window, otherwise its 15-minute row, marked `partial_boundary`. Each row names
the coarsest resolution that set its edges. An entity answers for the buckets it
mattered in and is part of the rest in the others; `who` lists each rest row as
one more row, so totals and the CPU accounting still reconcile. Coverage and
export are unchanged.

**Encoding.** A bundle is JSON compressed by zlib at level 6 with a fixed preset
dictionary of names from the code ([privacy](privacy.md#what-a-reading-may-contain)),
because each bundle is too short to teach zlib its own keys. The rows kept for the
whole span are mostly host rows, and they compress to about half with it (51–56%
smaller on Big Mac's host minute and hour rows), so it halves the cost of the long
history. A bundle names the dictionary by checksum, so those bytes can never change.
Bundles saved earlier decode as before; a release older than the dictionary cannot
read the newer ones.

**Stores saved before.** A version 2 store, from the release that kept every
process's 15-minute and hour rows, is converted once by the collector itself, in
its own saves: one prune transaction a save, at most 4,096 rows, hour rows first.
For each run and bucket it ranks the saved process rows with the same rule, writes
their rest rows and deletes the others. Only process rows change, as Kenny approved
(Oct 30): program, host, device, unit and self rows, minute rows and raw readings
stay. It records no progress of its own: a converted bucket's rest holds its cut,
so after a restart it finishes a bucket the same way and passes over finished ones.
While one bucket is part way, its rest and its not yet deleted rows both count, for
a few saves. Each converted bucket gets a retention record for its resolution with
reason `main_contributors_kept`, which coverage lists but does not report as a
pruned gap, and export leaves out of `pruned_ranges`. A row that cannot be read
stays. When nothing remains the store is marked version 3. A step that fails stops
converting until the next start, never costs the save, and shows as the collector
status's `conversion_error`. Version 3 cannot be opened by a release before it.

**Measured costs.** Before process rows were ranked at 15-minute and hour grain, a
day of Big Mac's hour rows cost about 550 MB on disk: about 230 MB of payload, 210 MB
of empty space inside pages, 37 MB of row overhead and 56 MB of indexes. An hour held
10,800 rows and 9.4 MB, 87% of it process rows, because about 2,600 instances
appeared an hour and 95% lived under an hour. Rows that a save wrote beside hour rows
were pruned earlier, which left pages 45% empty, and rewriting about 16,000
15-minute and hour rows a save matched the collector's 2.5–3.5 GB written an hour.
Replayed over three recorded hours, the share rule kept 274–315 host rows, 54–65
process and program rows and 5 rest rows: 0.15–0.17 MB with the dictionary, against
7,515–8,804 rows and 6.8–8.0 MB before.

## Collector cost

A monitor must cost so little that nobody notices it. The job gives the bar: the monitor
must never be one of the main contributors it reports, so Machine-audit's own CPU, the
collector and the scanner together, must stay under the 1% share of the machine's
process CPU in every hour. That is what the user pays, so the bar counts both, though
each is ranked as its own process. Big Mac's process CPU over the measured hours was
4.6–19 CPUs, which sets the bar at about 165 s of CPU an hour, 4.6% of one core, in its
quietest hour. The target leaves margin: 2% of one core (72 s an hour), under the bar
until the machine's load falls to 2 CPUs.

Before this release the collector used 1,350–1,500 s an hour on Big Mac (38–42% of a
core), about 5% of all process CPU: by its own rule, a main contributor. Replayed on 31
minutes of Big Mac's recorded readings (about 1,450 processes):

| | 0c633ad | This release, passes every 10 s | This release, passes every 30 s |
| --- | ---: | ---: | ---: |
| CPU a save (one a minute) | 19.5 s | 4.5 s | 1.6 s |
| Peak memory a save adds | 778 MiB | 307 MiB | 132–150 MiB |
| CPU a process pass | 0.76 s | 0.48 s | 0.48 s |

Most of the old save cost was re-merging and re-packing every process at minute,
quarter and hour on every save; most of the pass cost was Python copying and
re-validating 30,000 fields, not the OS reads (about 10%).

**Measured live.** With passes every 30 s, over four full hours on Big Mac (about 1,830
processes, whose CPU was 8.9–12.5 CPUs), the collector used 300–389 s an hour against a
bar of 321–449 s, and the scanner 28–182 s. The collector alone stayed under the bar
every hour, narrowly, and was never kept as a main contributor; with the scanner,
Machine-audit was over it in one hour (562 s against 449 s, in an hour with a long scan).
Neither would stay under in an hour as quiet as Big Mac's quietest (165 s), and both are
far from the margin. The collector's saves took 177–233 s an hour, the process pass
83–110 s, every other source 36–41 s.

**Reading the collector's cost.** `self.user_time` and `self.system_time` labelled
`collector` are the whole process. Its parts are `self.flush_cpu_s` (saves),
`self.source_read_thread_cpu_s` per source (each read and its admission) and
`self.query_cpu_s` (queries, encoding their answers included). On Big Mac the parts came
within 2–9 s an hour of the whole; the rest is startup, the minute's status and health
readings and the HTTP listener. The scanner is another process, labelled `scanner`, with
`self.scanner_cpu_total` per scan: 28–182 s an hour in those hours. The two labels stay
apart in the self rows; together they are Machine-audit's cost, the one the bar judges.

**Since then, with the same output.** Replayed on 31 minutes of Big Mac's readings at 30
s (about 1,900 processes), a save fell from 3.1 s to 1.9 s of CPU:

- A save takes the readings it admitted from the live cache instead of unpacking them
  again, which was a fifth of a save.
- The cycle collector's threshold is 100,000 objects, not Python's 700. It had run 3.5
  full collections a save, 15% of the save; now 0.04 s.
- Derivation looks each field's component up once, and each pass's prior fields once.

The threshold took a process pass from 0.58 s to 0.49 s. A `who` answer over 10 minutes
or less derives every pass in it; building only the fields it needs took 10 minutes
from 11.0 s and 369 MiB to 5.4 s and 261 MiB. For Big Mac's measured hours that
estimates saves at about 145 s an hour, the process pass about 90 s and other sources 37
s: about 280 s in all. That is under the bar in those hours but not in a quiet one, and
four times the margin.

**Eight fields a process.** A pass now reads, for each process, what answers who used
the CPU, memory and disk and who paid under memory pressure: user, system and runnable
time, footprint, bytes read and written, swap-ins and decompressions, and its age (on
the Mac only when the previous pass lacked it, the one case derive reads it). Ubuntu
keeps the resident set as its footprint, and swap. Dropped: reaped-child CPU (it overlaps children already counted), wakeups,
logical writes, the lifetime peak, compressed and graphics footprint, limit headroom,
the Mac resident set (footprint answers it), page-ins (the host's VM counters give them
every second) and the executable's UUID (nothing read it). `self.idle_wakeups` and
`self.interrupt_wakeups` were copies of the collector's own wakeups from the pass; they
are retired with it. Measured on this Mac (about 1,500 processes), from 22 fields a
process to 8:

| | 22 fields | 8 fields |
| --- | ---: | ---: |
| CPU a process pass, with its admission | 0.57–0.62 s | 0.28–0.29 s |
| Packed process pass | 358 kB | 183 kB |
| CPU a save (31 minutes of Big Mac's readings, replayed) | 2.05 s | 1.29 s |
| Peak memory a save adds | 243 MiB | 64 MiB |
| Store after 28 saves | 38 MB | 27 MB |

The replay keeps, in each recorded pass, only the fields this release reads. Building each
point from its field's template, with the same points, then took such a save from 1.32 s
to 1.20 s. For Big Mac's measured hours that estimates the collector's saves at about 85
s an hour, the process pass about 55 s, other sources 37 s: about 185 s. With the
scanner's 28–182 s, Machine-audit would be about 215–370 s, under the bar in each of those
hours, and three to five times the margin. What
remains is mostly building a full point for every field of every pass, and the per-read
cost of the 1-second sources.

**Process passes every 30 s.** Minute rows keep each minute's main contributors from two
passes, and the host's own readings stay at 1 s, so a constraint's timing and shape
come from the host series while the pass names who was responsible. A new process is
counted from its start: a process missing from the previous pass and younger than the
time since it (its `process.age`, read by the source) began at zero inside the
interval. On the recorded readings 30 s alone kept 96.6% of the CPU the 10 s passes
attributed, and with that counting 99.0%; at 10 s the counting adds 1.5%, including 782
processes in 31 minutes that lived a single pass and had none counted. What is lost: a
process's own peak is a 30-second average, and a process that starts and exits between
two passes is not seen.

## Paced full walks

The scanner's full walk was the one case where Machine-audit still crossed its own bar:
on Big Mac (Nov 10) a walk of all nine volumes cost 2,948 s of CPU in 65 minutes, against
a bar of 134–146 s that hour. The weekly walk is now paced by a CPU allowance per run;
the hourly schedule is the only scheduler.

**The allowance.** Each scheduled run may spend `max(room, 120 s)` of CPU, counted from
its start. Room is the bar for the trailing hour, 1% (`SHARE`) of the machine's process
CPU, minus the collector's own CPU in that hour, both read read-only from the collector's
store (about 0.3 s). When that store cannot answer, the floor governs. The floor keeps
hourly updates current in hours the collector alone fills: on Nov 7–10, 27–40% of hours
were over the bar from the collector and the routine update before any walk. A run
spends the allowance in this order:

1. Every volume's hourly update, including merging its delta into the base. An update
   that reaches the allowance defers the folders it has not re-listed through the
   deferral table; the next run re-lists deferred folders first, oldest first, so a
   backlog drains even at the floor. A merge that reaches it stops between batches:
   the delta stays published over the base, so the map is unchanged, and the next run
   finishes the merge before scanning. A single folder's re-list is not split. A
   deferred refresh of a folder no longer in the map is dropped: the listing that
   removed it covers it.
2. Paced walk work, which always gets at least 20 s even when the updates spent the
   allowance, so a walk of Big Mac (about 3,100 s with pruning and finishing) ends
   within a week. At the room measured on Nov 7–10 it takes about 1.5–2 days.
3. Pruning a replaced map, in what is left.

The operator's `scan --full` is unpaced. The run row records `allowance_ns`, `room_ns`
(null when unknown), CPU by stage (`stage_cpu`, ns: `walk` is listing, `sharing`,
`aggregate` is totals, `publication`, `merge`, `prune`) and `extent_reads`.

**The checkpoint.** One `walks` row per volume holds the walk's staged generation, its
phase, its start bookmark and event UUID, the entries listed and the folders still to
list as (inode, parent inode, name withheld): never names or paths. It is written in
the same transaction as the staged rows it describes, every 30 s of CPU and when the
walk pauses, so a stop, reboot or crash loses at most 30 s of work and the next run
resumes. A resumed folder's path is found again from its inode (`fsgetpath`), in memory
only; a folder gone since is skipped, and its parent's change event corrects the map.
Deferred name-withheld folders are found the same way. A failed attempt (an exception
the scanner catches) does not resume: its checkpoint is dropped and the retry rules
decide the next walk. So does a change of the volume's event UUID or name access.

**Each volume's update gets its share.** Updates go smallest first, by their last
run's entries, and each may spend what is left of the allowance divided by the volumes
still to update, so one volume's churn cannot starve the others and what a volume leaves
unused flows to the next. Before re-listing a folder, or a subtree when the re-list is
recursive, an update compares its size in the map (about 500 µs an entry: a re-list
costs 240-470 µs an entry on Big Mac, against 150-190 µs for the walk) with what is
left of its share; one that does not fit is deferred, never longer than a day, since a
single listing cannot pause part-way.

**Volumes walk one at a time:** a walk under way goes on first. Every volume keeps its
hourly update while its walk is under way or queued behind another's; only the walk
starting in a run skips that run's update.

**Phases.** Listing pauses between folders, so a folder is the unit of work. A folder the
previous map lists with more than 20,000 entries is listed only in a run whose remaining
share covers it (about 200 µs an entry; one of 416 k entries took 80 s on Big Mac); until
then the walk goes on with the other folders. When only such folders are left (phase
`large`), each waits for a run that covers it, but never longer than a day. Placing shared blocks needs every extent in
one process, and extents are never stored, so it runs whole; so does totalling folders.
Each waits for a run whose allowance covers its estimate (about 120 µs a file that may
share blocks, 30 µs a folder: 127 s and 47 s on Big Mac's startup volume), but never
longer than a day.

**The map meanwhile.** The published map, the previous full walk plus hourly deltas,
keeps serving and taking updates; the walk's generation is invisible to queries and
never pruned. The walk publishes by the same pointer switch as before, with its start
bookmark, so the next update replays every change since it began (on Big Mac, 9 hours
of changes touched 31.7 k folders with 402 k entries, not counting the over-20 k
folders limited to daily), paced by the same allowance. The weekly cadence counts from
the walk's start, which becomes the map's full-scan time. The reconciliation's
tolerance is every move of used space while the walk ran, in either direction. The
storage answer's volume rows carry `walk` (phase, entries, expected entries from the
previous walk, start, runs, CPU), and the tab says how far along it is.

## Saving, cost and retention

Saving in transactions once a minute is the starting design. The dashboard can
refresh every second from recent in-memory readings without forcing new source
reads. A crash can lose observations not yet saved. SQLite's write-ahead log
lets readers access history while collection continues.

Measure actual database growth, CPU, memory and bytes written on each target
with representative process counts before choosing reductions. For scale only,
2,000 processes × 8,640 ten-second reads × an assumed 64 bytes is 1.1 GB/day of
CPU payload. This is not measured SQLite growth; encoding, shared metadata,
indexes and additional fields change the result.

Earlier recorder reference figures: 52.5 MB for 72 hours of selected read logs,
59.8 MiB for about 14 days of minute records, and 4.1 MiB for 25 days of hourly
summaries. Those records did not retain every process reading and exclude the
filesystem index. They neither predict this MVP's size nor set its expiry rules.

The implementation must bound unsaved memory, expose collection/storage failures
and record any lost history. Budget-driven pruning keeps recording within the
cap; write failures and low device free space still need explicit handling.
Never hide missing or pruned history behind a healthy status.
Caching and query indexes should follow measured query needs.

## Remote retrieval

The planned remote flow is for devices to push copies of their collected
Machine-audit records to Cloudflare, then for Big Mac to fetch them. The scanner's
named map is excluded unless a separate privacy decision admits it
([storage view](storage-view.md#grain-process-and-cost)). The separate logs and
feedback capability owns upload scope, transfer budgets and retention.
Live dashboard access uses [Tailscale](installation.md#dashboard-access). It is
separate from uploading recorded logs; local storage limits are not upload limits.
Local pruning does not wait for upload acknowledgement. Remote retrieval must
expose history that expired before upload as missing coverage.

## Privacy

Process records use stable identities and admitted or sanitized labels. Full
paths, command lines, arguments and environment are not stored by default.
Preserving measurements does not mean retaining arbitrary raw responses or logs.

The filesystem index has its own store and access rules because file and folder
names are necessary there. Its separate privacy boundary does not give it a
separate storage budget.
