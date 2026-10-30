# Storage approach

The MVP preserves useful observations so humans and agents can investigate
incidents and improve the analysis later. SQLite is the proposed local store;
implementation has not started.

## Requirements

- **10 GB limit per device; alerts start at 5 GB.** Account for the database,
  write logs and separate filesystem index together, not as independent caps.
- **Preserve individual readings at collection cadence**, including process
  and GPU-client detail. Build summaries alongside them for efficient queries.
- Favor more useful evidence within that budget. Summaries keep only each
  bucket's top processes ([summary resolutions](#summary-resolutions));
  fixed short-window expiry remains a later candidate.
  Choose reductions from measured growth and actual investigations.

**Check growth and prune old records before reaching 10 GB, keeping recording
continuous.** If the cap is reached, prune old records to make room and keep
writing. Two floors, counted back from the newest save, set the order, so that no
tier of history can starve another:

1. Individual readings older than 48 hours, oldest first, after their summaries are
   safely stored.
2. Minute summaries, then 15-minute summaries, older than 48 hours.
3. Hour summaries older than 14 days.
4. Only if that is not enough, the floors give: readings inside the 48 hours, oldest
   first, then minute, 15-minute and last hour summaries. Each of these deletions
   carries reason `fine_floor_unmet`.
5. Rare incident records last, longer than routine readings.

Use consistent time cutoffs across components and processes. Show remaining
coverage and which history was removed. This order does not promise indefinite
retention. Pruning applies to Machine-audit's own records, not other files on the
device.

**The floors and the 10 GB cap.** Pruning starts at 90% of the cap and everything
Machine-audit stores shares it, including the scanner's map. The floors hold only
while the room left covers them, and the cap always wins: a save still fits and
recording continues. On a box where it cannot hold both floors, the newest detail
and the last 14 days of hour rows stay, raw readings give first, and coverage reports
each removed span as `pruned` with reason `fine_floor_unmet`. Hour rows of the last
14 days outrank raw inside the 48 hours because they are about 50 times cheaper a day
and the only record that goes back further. Without that floor, a box that cannot
hold 48 hours of raw would keep no hour rows older than the 48 hours.

| Per 48 hours on disk (Oct 2026 rates) | Big Mac | Small Ubuntu box |
| --- | ---: | ---: |
| Raw readings | 5.6 GB | about 1.5 GB |
| Minute rows | 1.2 GB | together about 1.2 GB |
| 15-minute rows and coverage | 0.4 GB | |
| Scanner map | 3.7 GB | 0.01 GB |
| 14 days of hour rows | 0.7–1.1 GB | unmeasured |
| Total against the 9 GB pruning level | 11.6–12 GB | about 3 GB |

Big Mac cannot hold the fine floor with its scanner map under the cap. By these
estimates it keeps about 24 hours of raw readings (22–26), 48 hours of minute and
15-minute rows and 14 days of hour rows, and records the rest as `fine_floor_unmet`.
Big Mac's raw figure is measured at 1.88 MB a minute, 90% of it process passes; its
summary figures apply the top-consumer rule and the dictionary to measured rows, and
are not yet measured in place. The Ubuntu figures are the
[recorded day](development.md#review-regressions-and-scale-measurements) doubled at
its 1.66 disk-to-payload ratio.

Counted writes reserve their transaction's growth and journal pages, rather
than another copy of the entire database. The readings store starts proactive
pruning at 90% of the combined cap; admission can prune earlier when the pending
transaction needs more room. Neither number promises a particular history span.
See the measured [recording costs](development.md#review-regressions-and-scale-measurements).

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
inserts are bounded at 8,192 operations or 2 MiB of arguments, charge writes at
4,096 rows, aggregate updates at 8,192 rows and obsolete-generation deletes at
4,096 rows. Regressions measure their WAL frames against the declared allowance.
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
window and search forward for the oldest fully covered raw window. At each
floor, only after the covered raw windows it allows are gone may pruning
continue into summaries. Coverage evidence must match both sequence and
observation time; a later high sequence alone does not prove an earlier window
was summarized. Each retained window is recorded as `raw_preserved` with reason
`missing_summary_coverage`. These records appear in retention history, but are
excluded from pruned gaps and export `pruned_ranges`. Summary deletions made
while raw is retained carry reason `storage_budget_raw_uncovered`. Uncovered
raw must neither block startup recovery nor cause covered raw to accumulate.

The raw-pruning proof searches the existing coverage-time index backward from
each reading. It retains the run, source, sequence and time checks; any matching
fragment proves coverage. The read-only candidate search stops after one million
SQLite VM instructions or one second, checked every 1,000 instructions and
between windows. Each search passes over windows already recorded as
`raw_preserved`, so it resumes where the last one stopped. A search that runs
out before a covered window prunes the oldest summaries instead, with reason
`storage_budget_raw_uncovered`, and records the uncovered windows it found.
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
| User-process CPU, memory, writes and wake-ups | 10 s | macOS `proc_pid_rusage` plus `TASK_VM_INFO` memory effects, graphics footprint and limit headroom; Ubuntu process CPU and `status` memory fields, other counters where available |
| System-process CPU and memory via Mac helper | About 60 s (planned) | Helper not shipped yet; separate privileged source, not an idle setting |
| Ubuntu workload-group memory | 10 s | cgroup usage, movement, PSI and event counters; use one non-overlapping level for totals |
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

Per-process rollups, per process instance and per signal, for each bucket's
top consumers, and per-program rollups for every program at 15-minute and
hourly grain, serve the [query design](query-design.md)'s process table and
spike questions over long spans. Beyond the summary family above they keep the
highest pass rate with its time, retained as observed, plus seconds above a
small fixed set of levels and a small histogram of pass rates, both weighted by
each pass's covered duration. The histogram answers "time above a level" exactly only at a bin boundary;
inside a bin it bounds or estimates. All three describe pass-average rates: a
burst shorter than the process cadence is averaged inside its pass and cannot
be recovered. Rollups are keyed by process instance (pid, start time, boot).
Compatible additive fields sum across instances; peaks, time above and
histograms do not, so program-level spikes over pruned history use the query
design's time-aligned per-program rollup, with its grouping and coverage method
recorded. A rollup's instances stay listed in its `members`, so instance counts
survive when an instance's own rows do not. Every
rollup carries a method version and can be rebuilt from retained readings; what
it carries is fixed only once those readings are pruned. Rollup size and query
cost are unmeasured and follow the growth measurements below.

Summaries lose sample order, precise timing and the ability to regroup discarded
identities. A stored maximum can preserve an observed peak's magnitude, but not
its moment unless that time is retained. Nothing reconstructs activity between
reads. Shared [time and coverage rules](cpu-view.md#time-windows-and-coverage-working)
govern the selected interval in both interfaces.

### Summary resolutions

| Resolution | Rows kept |
| --- | --- |
| Minute | Every entity, except processes and programs: only each minute's top consumers |
| 15-minute | Every entity, except processes: only each quarter hour's top consumers |
| Hour | Every entity, except processes: only each hour's top consumers |

**Top consumers.** A bucket keeps process rows for the union of the top 25 by
CPU time, the top 25 by memory (Mac footprint, Linux resident set) and the top
25 by disk bytes read and written, plus the collector's own processes: at most
75, plus our own, in each component. Minutes rank programs the same way. Every
program keeps 15-minute and hour rows, so `who` answers additive totals (CPU,
disk bytes, wake-ups, page-ins) exactly for all of them over 15 minutes or more,
and `who` for a named gauge lists each bucket's top instances. Host, device,
unit and self rows keep every bucket. The rule, `TOP_N` and `RANKED` live in
`store.py`, for the Mac and Linux alike.

A bucket is ranked once it is complete. Saves are not aligned with buckets, so
the collector holds the run's newest minute, quarter hour and hour of ranked
rows in memory, packed, until a later save completes each one, then saves only
its top consumers, once. A saved row always holds its whole bucket; data
arriving after the ranking extends only kept rows. Until then `who` answers
that minute from 15-minute rows where they exist, and a process in an open
quarter hour from the minute rows of its top minutes. Stopping the run saves
the held buckets; a stop that only checkpoints carries them in the checkpoint.
A crash loses only their ranked rows: an open minute's top consumers and an
open quarter hour's and hour's processes. Its readings and its host, device,
unit and program 15-minute and hour rows are already saved.

Each save reserves room in proportion to the rows it writes. A save that does
not fit prunes the oldest history and tries again; it merges its rows once,
whatever the number of attempts.

**Answers across resolutions.** `series` uses minute rows; if they are pruned,
15-minute rows, then hour rows, and names the resolution it used. `who` uses
complete interior hours and, at each side, per entity and quarter hour: its
minute rows when it has one for every minute of that quarter hour inside the
window, otherwise its 15-minute row, marked `partial_boundary`. Each row names
the coarsest resolution that set its edges. History saved before 15-minute
rows existed answers from its minute rows. An instance answers for the buckets
it topped, so a gauge's mean over a long span is the mean of those buckets.
Coverage and export are unchanged.

**Encoding.** A bundle is JSON compressed by zlib at level 6 with a fixed preset
dictionary of names from the code ([privacy](privacy.md#what-a-reading-may-contain)),
because each bundle is too short to teach zlib its own keys. Over Big Mac's recorded
rows it saves 39–47% against the earlier level 1 without a dictionary. A bundle names
the dictionary by checksum, so those bytes can never change. Bundles saved earlier
decode as before; a release older than the dictionary cannot read the newer ones.

**Rows saved before the rule.** `machine-audit thin-process-rows [--batches N]`
applies the rule to 15-minute and hour rows an earlier release saved: for each
run and bucket it ranks the saved process rows with the same function and deletes
the rest. Only those rows go; minute rows, program, host, device, unit and self
rows and raw readings stay. Each delete is one prune transaction, so it is counted
the same way and holds at most 4,096 rows. Nothing records its progress: a thinned
bucket ranks to the same top and offers nothing more to delete, so running it again
resumes, and a bucket stopped part way finishes the same. It reads in short
transactions and takes turns with the collector through the budget, so it can run
while the collector saves. A thinned bucket gets a retention record for its
resolution with reason `top_consumers_kept`. Coverage lists it but reports no pruned
gap, and export leaves it out of `pruned_ranges`, because the span still answers. A
row that cannot be read stays and is counted in `rows_unread`.

**Measured and estimated cost.** A Mac-shaped synthetic run (2,000
processes, 600 program labels, 10 s passes, saves every 60 s not aligned
with minutes) measured:

| Per minute | Before | After |
| --- | ---: | ---: |
| New summary rows | 7,200 | about 320, plus 7,200 15-minute rows each quarter hour (480 a minute) |
| Store growth | 12.6 MB | about 3.3 MB (2.6–5.2), of which raw readings 2.0 MB |
| Save CPU | about 14 s | about 18 s |
| Rows reserved per save | about 14,400 | about 14,800 |

The 11.3 MB a minute recorded for Big Mac's 2,000 processes (16 GB a day, in
[development](development.md#review-regressions-and-scale-measurements)) scales
to about 3 MB a minute, about 4.3 GB a day, against the 10 GB cap.

**Hour rows on Big Mac.** Before process rows were ranked at 15-minute and hour
grain, a day of hour rows cost about 550 MB on disk: about 230 MB of payload,
210 MB of empty space inside pages, 37 MB of row overhead and 56 MB of indexes.
An hour held 10,800 rows and 9.4 MB, 87% of it process rows, because about 2,600
instances appeared an hour and 95% lived under an hour; 43% of those bundles came
from a single pass and held no counter data. Rows that a save wrote beside hour
rows were pruned earlier, which left pages 45% empty, and rewriting about 16,000
15-minute and hour rows a save matched the collector's 2.5–3.5 GB written an
hour. The shipped ranking, replayed over three recorded hours, kept 1,410–1,771
rows of 7,275–22,334 and 1.2–2.1 MB of 6.6–18.6 MB.

Target B (Ubuntu, estimate from the [Linux adapter](linux-adapter.md)'s
table, not measured): about 750 minute rows a minute today. Its roughly 115
sampled processes fall to at most 51 (other users' disk bytes are denied, so
the disk list holds only our own), about 200 to 90 rows; programs about 98 to
70; units, host and devices are unchanged. With 15-minute rows (about 50 a
minute) it writes about 660 rows a minute, roughly 10% fewer. Its growth, about
1.7 MB a minute (Target A's 1.07 MB scaled by units and processes), is mostly
raw workload records and unit rows, so it falls by about 5%.

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
