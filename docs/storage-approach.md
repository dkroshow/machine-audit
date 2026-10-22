# Storage approach

The MVP preserves useful observations so humans and agents can investigate
incidents and improve the analysis later. SQLite is the proposed local store;
implementation has not started.

## Requirements

- **10 GB limit per device; alerts start at 5 GB.** Account for the database,
  write logs and separate filesystem index together, not as independent caps.
- **Preserve individual readings at collection cadence**, including process
  and GPU-client detail. Build summaries alongside them for efficient queries.
- Favor more useful evidence within that budget. Top-N selection, group-only
  storage and fixed short-window expiry are later pruning candidates, not MVP
  defaults. Choose reductions from measured growth and actual investigations.

**Check growth and prune old records before reaching 10 GB, keeping recording
continuous.** If the cap is reached, prune old records to make room and keep
writing. Prune in this order:

1. Oldest individual readings, after their summaries are safely stored.
2. Oldest minute summaries, retaining hourly summaries longer.
3. Retain rare incident records longer than routine readings.

Use consistent time cutoffs across components and processes. Show remaining
coverage and which history was removed. Exact cutoffs and reclaim amounts follow
measured storage growth; this order does not promise indefinite retention.
Pruning applies to Machine-audit's own records, not other files on the device.

The current implementation reserves space for a database and its possible
journal during writes. Its readings-store pruning target is about 42.5% of the
cap left after other managed files: a 2 GB scanner map pair leaves about 3.4 GB
for readings and summaries under the 10 GB cap. This conservative headroom is
an implementation limit awaiting trial acceptance, not a promised history span.
See the measured [recording costs](development.md#review-regressions-and-scale-measurements).

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
This bounded pruning path lets recovery remove raw readings, then minute
summaries, then hourly summaries, retaining incident history longest, even
when the ordinary whole-database write reservation no longer fits.

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

Minute and hourly summaries accelerate queries alongside the detailed store.
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
process counts. Mac scopes all keep minute and hour summaries.

Per-process rollups, per process instance and per signal, at minute and hourly
grain, serve the [query design](query-design.md)'s process table and spike
questions over long spans. Beyond the summary family above they keep the
highest pass rate with its time, retained as observed, plus seconds above a
small fixed set of levels and a small histogram of pass rates, both weighted by
each pass's covered duration. The histogram answers "time above a level" exactly only at a bin boundary;
inside a bin it bounds or estimates. All three describe pass-average rates: a
burst shorter than the process cadence is averaged inside its pass and cannot
be recovered. Rollups are keyed by process instance (pid, start time, boot).
Compatible additive fields sum across instances; peaks, time above and
histograms do not, so program-level spikes over pruned history need the query
design's time-aligned per-program rollup, with its grouping and coverage method
recorded. Every
rollup carries a method version and can be rebuilt from retained readings; what
it carries is fixed only once those readings are pruned. Rollup size and query
cost are unmeasured and follow the growth measurements below.

Summaries lose sample order, precise timing and the ability to regroup discarded
identities. A stored maximum can preserve an observed peak's magnitude, but not
its moment unless that time is retained. Nothing reconstructs activity between
reads. Shared [time and coverage rules](cpu-view.md#time-windows-and-coverage-working)
govern the selected interval in both interfaces.

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
