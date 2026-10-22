# Local API and source contract

Version 1. Python 3.12+, standard library only. This document names the shared
contract used by sources, replay, scanner, CLI and dashboard.

## Source input

`machine_audit.model.Source` has `name`, `cadence_s`, and `read() -> Reading`.
Each source owns one timer thread; reads never overlap themselves. A Reading
contains source, component, Clock start/end, tuple of Field, status, safe reason
code and source version. Clock has wall_ns, awake_ns and continuous_ns; adapters
must use their native sleep-aware clocks. `Clock.now()` is a fallback and does
not claim sleep detection on macOS.

Field(name, value, unit, kind='gauge', status='observed', entity='host',
label=None, scope='host', additive=False, scale=1.0). Kinds: counter, gauge,
state, event, rate (source-window rate), accumulator (read/reset). `scale`
converts counter source units to displayed rate numerator (nanosecond CPU
counters use 1e-9); the original value and unit survive unchanged.
`unit` and `rate_unit` must be members of the closed `UNITS` set in
`machine_audit/model.py`, and a reading `reason` is a code (letters, digits,
underscore). An OS-derived string is never a unit; extend `UNITS` in a reviewed
change. Use `process_identity(pid, start, boot_id)` for instance identity.
Labels pass `admit_label`; never pass paths, argv, network addresses or
exception text.
Each read contains at most one field per (name, entity). Bundled process reads
contain every observed instance and its fields in that single reading.

Names are source-specific: recommended `cpu.busy`, `cpu.runnable`,
`memory.available`, `process.cpu`, `process.memory`, `process.write`,
`gpu.utilization`, `disk.read`, `disk.write`, `network.receive`, `network.send`,
`power.watts`, `heat.temperature`. Missing fields keep explicit statuses.
The source builder owns final source/field names; clients discover actual names
from `/api/now` or `/api/catalog`, never infer OS equivalence from these examples.
Components: cpu, memory, gpu, storage, network, power, heat, self.

## Store, replay, scanner

`Store(path, budget=None)` opens a private SQLite store. Use a flat managed
data directory. Replay calls `store.append(records)` with Record objects,
then `Query(store).series(...)` etc. Record(Reading, boot_id, run_id, sequence,
due_wall_ns, received). Sequence increases strictly per collector run.
`store.start_run(run_id, boot_id, started_ns)` and `stop_run` record lifecycle.
A batch inserts raw readings and updates cumulative minute/hour summaries
atomically. Query read transactions and commit/watermark advancement share
a short lock; existing SQLite readers retain their old summary snapshot.

The scanner shares `Budget(data_dir).reserve(peak_bytes)` as a context manager;
reserve replacement database + WAL + checkpoint peak *before* writing and hold
the context through finishing those writes. It serializes with collector writes
across processes. BudgetExceeded leaves the last map intact: mark stale/budget
blocked. All managed files are directly inside data_dir; private names stay in
scanner SQLite. Scanner query adapter: `query.storage = adapter` where
`adapter.query(action, **params)` handles `volumes`, `children`, `growth` read-only.
Core delegates `/api/storage?action=...`; it never exports scanner tables.
A scanner cost Reading uses component `self`, source `self.scanner`, numeric
fields only; record CPU seconds separately from elapsed seconds.

## HTTP and query methods

Server binds 127.0.0.1. API requires `Authorization: Bearer <token>` on every
request, exact allowed Host, and an allowed Origin if present. Dashboard loads
`/#token=<token>`, retains the token in memory and erases the fragment. Never
put tokens in query strings or browser storage. No API CORS wildcard.

The server runs at most 4 API queries at once. When no slot is free, a request
waits up to 2 seconds. At most 4 requests may wait; further requests receive
HTTP 503 immediately. A wait that expires also returns HTTP 503. The body is
`{"error":"query_capacity"}`. Clients should retry this error with backoff.
The dashboard waits 0.5, 1, then 2 seconds, making at most 4 attempts in total;
it does not retry other HTTP errors.

GET routes: `/api/now`, `/api/catalog`, `/api/series`, `/api/who`,
`/api/around`, `/api/coverage`, `/api/storage`, `/api/status`.
Common parameters: `component`, comma-separated `signals`, `start` and `end`
(Unix seconds), `limit` (1..2000, default 600). `/api/who` takes `signal`,
`/api/around` takes `at` and `span` seconds. Times in payloads use integer
nanoseconds. Query methods take start_ns/end_ns as integers, signals as list,
component optional, limit default 600. `now(component=None)` uses only live
cache; offline returns status unavailable. All other shapes capture one coherent
store/tail snapshot. `/api/catalog` returns discovered signals and components.

Every answer has `schema_version: 1`, `shape`, `status`, and `coverage`.
Intervals have `requested: {start_ns,end_ns}`, `covered`, `resolution`, `rows`,
`gaps`, `truncated`, `next_start_ns` when paging. A field missing read after read
is one gap row spanning those reads, with `entity` and a `reads` count; a good
read of it ends the row. Coverage `gaps` also contains per-source rows alongside
collector gaps. These rows have exactly `source`, `status`, `reason`, `start_ns`,
`end_ns`, and `reads`. They appear when a source reading overlaps the requested
window, has no fields, and has a status outside the good set (`observed`,
`measured_zero`). Consecutive readings of that source in the same collector run
with the same status and reason merge into one row; `reads` counts them. A later
good reading or a reading with fields ends the row. The bounds are clipped to
the requested window. `reason` is a [reason code](#source-input).

Rows carry `start_ns`, `end_ns`,
`signal`, `entity`, `label`, `scope`, `unit`, `kind`, `status`, plus aggregate
fields (`total`, `covered_s`, `mean`, `min`, `max`, `max_ns`, `last`, `count`,
`peak_rate`, `peak_ns`, `above_s`, `histogram_s` as appropriate).
`now` returns `sources: [{source, component, status, stale, latest, trail}]`;
latest is a Record dictionary with bounded fields and an explicit field_count;
raw trail is empty, while trail_rows carries bounded derived history. `who` returns ranked program
rows (label, instance_count, total, covered_s, mean_rate, peak_rate and coverage)
for additive fields; overlapping memory remains separate instance rows.
`brief=1` (on `now`, `who` and `series` only; any other value or route is refused)
returns a shape of a few kilobytes. Brief `series` keeps its rows and reduces
coverage as below. Brief `who` keeps the top five rows and the answer's
totals: `total_rows`, `totals_complete`, `accounting`, `covered`, `resolution`.
Brief `now` gives, for each component with a workload total (cpu, memory,
storage), `components[c].top`: the five largest workloads by that total, chosen
as `who` chooses them, and `components[c].host`: the host totals beside it
(`cpu.busy`, `cpu.capacity`; `disk.read`, `disk.write`; installed, available and
swap memory, by platform). Rows keep `source`, `signal`, `entity`, `label`,
`kind`, `unit`, `rate_unit`, `status` and their measures (`value`, `mean`, `mean_rate`,
`total`, `peak_rate`). Coverage keeps only per-source status counts and
`gap_count`; there are no gap rows and no per-lane rows.
`around` returns nested series/who/coverage plus events. `/api/status` reports
collector/source health, budget and measured operation cost; no tokens.
`collector.operations.queries` breaks query cost down per query method (`series`,
`who`, ...): `count`, `elapsed_s` and `cpu_s`, beside the totals. Every query
answer carries a `Server-Timing` header, `query;dur=<ms>, cpu;dur=<ms>`: its own
elapsed and CPU milliseconds, with no tokens or row content.

Self-cost uses ordinary `self.*` fields and the same shapes. Operation durations
are elapsed seconds, never CPU. Process CPU/memory and scanner costs are recorded
only when measured; browser, helpers or kernel work not observed remain gaps.

Export is a read transaction snapshot keyed by (device_id, run_id, sequence),
not SQLite rowid. It contains only core Records, run identity, snapshot watermarks
and explicit pruned ranges. An acknowledgement must refer to those identities;
no uploader is provided here. Scanner names are excluded structurally.

## Implemented timing, privacy, and bounds

`Field.start` / `Field.end` optionally carry an entity's actual Clock bounds;
process passes must populate them. Source bounds enclose the whole pass. Raw
user/system CPU snapshots stay distinct; `derive.source_fields` constructs
process.cpu and host cpu.busy without adding them to stored raw records.
`total` always remains in original source units. `scale` converts that total to
the numerator of `mean_rate` / `peak_rate`; `rate_unit` states the rate's unit.
`now.rows` uses the same derivation over recent cache alongside raw source metadata.

The source is the privacy boundary: only executable/process labels obtained
from the relevant OS label API may be passed to `admit_label`. The helper merely
rejects unsafe syntax; it cannot identify an argv string disguised as a name.
State and event values must come from source-owned fixed enums/codes. Raw
exception messages, command lines, paths and addresses are never valid inputs.

Use `Budget.sqlite_write(connection, path, growth_bytes=...)` for SQLite writes.
It requires a truncatable WAL, disables cache spills, bounds database pages,
and reserves the worst-case database + one frame per page + SHM + checkpoint
coexistence. SQLite's page limit aborts oversized transactions. Callers roll
back on failure; a post-write size check is not the safety mechanism. A pinned
reader can delay another write. A large staged export can also block database
writes until managed space becomes available. The scanner must keep the previous
map until its replacement transaction completes and include both stores in this budget.
`usage().bytes` reports physical storage; `checkpoint_reservation_bytes` is a
separate allowance for pending WAL copies. Generic writes and other stores'
admission keep that allowance available. Before its own checkpoint, SQLite
admission checks the remaining logical-to-physical database growth, so a pinned
checkpoint cannot exceed the cap before the new write is considered.

The buffer policy is not yet approved for unattended use. Current bounded
fallback suspends new reads at a configurable 64 MiB high-water mark, preserving
completed readings and exposing suspension/skipped ticks. In-flight source
reads can add one completed Reading each; representative source size bounds
are still required.
This is a coverage loss, not a claim that overflow retention was approved.
Representative size/cost measurements must determine the production setting.

## Bundled components and coverage

`Field.component` can override its Reading's component. `field_component` is the
single compatibility map for bundled process memory/I/O and self fields; the
store indexes that result. Thus `who?component=memory` finds footprint data in
a CPU process pass without another OS process walk. Self fields retain process
identities in series, including overlapping per-instance memory scopes.
`model.is_series` defines eligibility for pending records, stored indexes and
queries. A `process` scope is workload evidence even when a failed read has
only a `pid:` identity; so are the Linux `unit` and `unit_detail` scopes, and only
`unit` fields (CPU time, `memory.current`, `io.stat`) rank in `who`, each unit
as its own row; programs never group unit fields. These failures remain in raw records, coverage counts
and `who`; they are excluded from host/device series. Component `self` remains
eligible regardless of its process identity or scope.

Old `reading_components.is_series` flags are not rewritten. Queries use catalog
scopes to reject process-only sources before decoding, then check admitted
fields with the same predicate. Historically mixed sources may still require
bounded decoding; if their candidates exhaust the allowance, `truncated` stays
true. This is bounded read-time handling, not a migration of old indexes.

Coverage fragments and incident records survive pruning of raw readings.
The last committed source baseline is kept for the next counter difference.
An around request shares one captured watermark/read transaction across its
series, who, coverage and events. Empty series bins have null measures and
status unobserved; bucket_start_ns/end_ns describe their grid, while observed
start_ns/end_ns retain actual source bounds. Counter summaries preserve
constituent-reset invalidity; a rising system counter cannot hide a reset user
counter inside a synthetic CPU total.

The original global field-count limit was removed: it rejected a normal large
process pass. Until representative memory measurements establish a source
bound and the owner accepts the overflow policy, the high-water suspension is
not an unattended-operating guarantee. It preserves completed readings,
records the suspension interval, and never grows a repeated failure-event queue.

`now.trail_rows` contains server-derived host/device points from the recent
cache. `now.rows` contains the latest derived points, including programs. Both
are bounded by `limit`; `truncated` names any limit. Raw `sources[].latest`
fields are also bounded; `field_count` and `truncated` describe that view.
Full process raw trails are queried through the interval shapes instead of
repeatedly transferring every process sample during dashboard refresh.

The shared `client.request(config, shape, params=None)` never uses environment
HTTP proxies, follows redirects, or sends the token off loopback. It bounds JSON
responses at 8 MiB and raises `ClientError.code` without source/server text.
`not_running` permits the CLI's explicit store-only fallback.

`Store.export_chunk(path, device_id=..., run_id=..., after_sequence=0,
limit=1000, max_bytes=8388608, manifest_path=None)` writes two new files directly
in the managed directory: a SQLite chunk and JSON manifest. Both fit one shared
reservation. The chunk has ordinary `records` (run_id, sequence, wall_ns,
payload), its run row, metadata and SQLite user_version=1. The manifest includes
schema/format, device/run identity, first/last sequence, count, wall bounds,
snapshot watermark, pruned ranges, SHA-256, byte length and creation_ns. A
manifest appears only after complete chunk creation. Neither file contains
scanner tables. Existing destinations are refused; transfer/acknowledgement
and disposal policy remain with the transfer component.

Without a `signal`, `who` ranks each workload once, by its component total
(`WHO_TOTALS` in `query.py`: CPU time for `cpu`, written bytes for `storage`,
and for `memory` the Mac footprint or a Linux unit's `memory.current`).
Components such as user time, child time, wakeups, read bytes, resident and
compressed memory need an explicit `signal`. Failed-read evidence still appears.
`who` returns CPU accounting separately from ranking. Original-unit program
totals are converted to CPU-seconds only for comparison. A remainder is given
only when host and attributed observation bounds match; mismatches and negative
reconciliation stay explicit. Memory scopes never receive an additive total.
Self fields retain only managed-instance rows; they do not gain duplicate
program rollups. Core health records elapsed and thread CPU separately, plus
source due/done/skipped/late counters and the build version.

Collector commit and watermark advancement share the lock used to start a
query's SQLite snapshot. Cumulative minute/hour summaries replace old values
inside that transaction. This keeps long-range rows compact without retaining
a separate summary version for every save. Source-specific counters, scopes,
units and scale remain separate. Late source records still merge into their
actual observation window.

Storage queries pass `volume_id`, `parent_id` and opaque `cursor` to the scanner
adapter. A paged response returns `next_cursor`; pass it unchanged for the next
page. CLI equivalents are `storage --action children|growth --volume-id ...
--parent-id ... --cursor ...`. Nonzero offset is not the scanner's paging API.

When the server is absent, the CLI opens the recording database and its adjacent
`scanner.sqlite` read-only. Storage queries use the same parameter dictionary
as live requests, including volume, parent, cursor, interval and row limit.
Keep the same volume, parent and interval when continuing with a cursor.
No filesystem scan or collector start is needed to inspect this saved map.

## Compact store and summary boundaries

The private SQLite schema is version 2. Raw Records use a versioned, compressed
encoding with shared field descriptions, entities and integer Clock triples.
The original values, numeric types, units, per-field clocks and statuses survive
an exact round trip. `Record.to_dict()` and exported `records.payload` remain
ordinary Record JSON. Pre-release disposable schema versions are rejected;
there is no automatic deletion or migration.

Minute and hour summaries bundle every signal for a source/component/entity.
No accepted signal is omitted. Hour summaries merge minute statistics using
union coverage, original peak times and original counter totals. CPU threshold
histograms apply only to `cpu.busy`, `process.cpu` and `self.cpu`; byte/page gauges
have empty CPU histograms. Component and signal filters still apply within each
bundle. Queries decode matching interval/entity bundles, not the entire history.

Each summary row also stores one number, `who_rank` (nullable): the largest
value `who` ranks a row of that bundle by without a named signal (its mean
rate, or mean for gauges), or NULL when nothing in it ranks. A window's merged
rate or mean is a weighted average of its buckets', so this bounds it. `who`
without a named signal over saved rows (an hour or more, or no unsaved tail)
reads entities from the highest bound down and stops once no unread entity can
enter the top `limit`; an unread entity counts once in `total_rows`, and
`covered` and the partial flags describe the rows read. Rows saved before the
column hold NULL up to its first ranked bucket and are always read, as before;
opening an older store adds the column and rewrites nothing. Among equal values
`who` keeps and lists the earlier source and entity first.

`who` accepts intervals up to 31 days. It combines complete interior hours with
15-minute and minute rows at the sides, merges one entity at a time, and ranks
only the output. Minute rows hold only each minute's top processes and programs;
an entity without a minute row for every minute of a side's quarter hour answers
that quarter hour from its 15-minute row, marked `partial_boundary`. `total_rows` counts all matching entities; `totals_complete` is not made
false merely because the requested output page is shorter. CPU accounting uses
all matching program rows, including those outside the returned page. Memory
footprints remain separate instance scopes.

Stored summary resolutions are `minute` (every entity except processes and
programs, which keep only each minute's top consumers), `15-minute` and `hour`
(every entity); see [summary resolutions](storage-approach.md#summary-resolutions).
`series` reports `resolution` as `raw`, `minute`, `15-minute` or `hour`. Summary
rows carry `summary_resolution`; in `who` it names the coarsest resolution that
set the row's edges.

`series` uses raw readings for ten minutes or less. Longer windows use the finest
stored resolution with no more rows per signal than `limit`: minute up to 10 hours
at the default 600, then 15-minute, then hour. Each signal answers at the finest
resolution it has from there up, so one with only hour rows (for example after
pruning) keeps them and its rows say `summary_resolution: hour`, while the others
keep minute rows; `resolution` names the finest used. Rows then merge into equal
bins: with `n` signals each gets `limit // n` bins over the window, and a bin
merges the summary rows that end in it the way hour rows merge minutes (totals
add, peaks keep their original values and times). So a series answer has at most
`limit` rows. Bin rows name their edges in `bucket_start_ns`/`bucket_end_ns` and
omit stored-bucket identities, merge state and `who` grouping fields.

A series with neither `component` nor `signals` is bounded, for agents that ask
for "the machine". Before reading any row it chooses, from the signal catalog,
each component's host totals (the brief set above) or else its first two host
signals by source and name, and names them in `selected_signals`. Its resolution
then follows from `limit // len(selected_signals)` rows per signal, so an hour
reads 15-minute rows at the default limit. It costs about one component's
series; ask with `component` or `signals` for the rest.

The dashboard asks for what each view renders:

| View | Requests |
| --- | --- |
| Every view, once | `catalog` |
| Overview, history | per card (CPU, Memory, Graphics, Storage, Network, Power, Heat, Monitoring cost): `series?brief=1` with that `component`, three host `signals` (below) and `limit=12`; the card shows each signal's newest of four bins, one per signal. No coverage request: the overview renders none |
| Overview, live | `now?brief=1` |
| A component, history | `series?component=…` (its coverage explains an empty view) and `who?component=…&signal=…`; `status` instead of `who` on Monitoring cost; the storage map on Storage |
| A component, live | `now?component=…` |
| Coverage | `coverage` and `status` |

Each overview card asks for the first three of its preferred signals the catalog
holds, then its other host signals by source and name (`cardPreferred` in
`dashboard/model.mjs`; Linux names first, Mac names after):

| Card | Preferred signals |
| --- | --- |
| CPU | `cpu.busy`, `cpu.load_1m`, `cpu.pressure.some`, `cpu.runnable`, `cpu.capacity` |
| Memory | `memory.MemAvailable`, `memory.free_count`, `memory.pressure.some`, `memory.pressure_level`, `memory.SwapFree`, `memory.swap_used` |
| Storage | `disk.read`, `disk.write`, `io.pressure.some`, `disk.read_latency` |
| Network | `network.receive`, `network.send`, `network.ping_rtt` |
| Power | `power.watts`, `power.average`, `power.input`, `power.supply`, `power.mains_online`, `power.battery_capacity`, `power.battery_present` |
| Heat | `heat.temperature`, `heat.thermal_pressure`, `heat.zone_temperature`, `heat.fan_rpm`, `heat.cpu_speed_limit` |
| Monitoring cost | `self.cpu`, `self.memory`, `self.storage` |

At most three requests run at once, so one server slot stays free for other
clients.

Answers of an hour or more come from summaries. The unsaved tail (at most the
minute since the last save) is decoded only when no save covers the window yet,
so `covered.end_ns` can stop at the last save. Once a save covers it, coverage
of more than ten minutes takes its gap rows from host-series summaries without
decoding readings; the tail still adds its status counts. It reads the coarsest
resolution that still gives the window four buckets (15-minute rows for one to
four hours, hour rows beyond, minute rows below an hour), so gap edges fall on
those buckets. One row per source, status and reason spans consecutive buckets
whose host-series rows hold that unmeasured status (reason `summary_status`,
the bucket's most frequent one) or sleep (`window_contains_sleep`); `reads` sums
the unmeasured samples of all its signals, `sleep_s` counts each bucket's sleep
once, and `summary_resolution` names the resolution read. Read-level reasons such as `baseline` appear
in coverage of ten minutes or less and in `who`. A source that reports a status
without fields (for example `unavailable` with `no_devices`) keeps its reason in
saved coverage, so that gap row appears at every window length. A summary row
whose stored payload cannot be decoded (damage on disk) is skipped and reported
as a gap with status `unreadable` and reason `summary_damaged`, naming the
source, bucket and `summary_resolution`; the rest of the answer is unaffected.

Summary selection uses aligned bucket identities. An observed counter window
may cross a requested edge; it is retained whole and marked `partial_boundary`.
`start_ns`/`end_ns` remain actual observations. `summary_bucket_start_ns` and
`summary_bucket_end_ns` identify stored buckets. These are not prorated totals
for the requested interval. Raw reads are bounded by the decoded private payload
size, including baselines and pending readings, before constructing records.
Component and series eligibility apply before this byte admission. An unrelated
process pass cannot consume a host-series query's allowance. Source status counts
remain available even when that query does not select the source's raw fields.
Admission remains at complete-Reading granularity: a mixed reading containing
eligible host fields still decodes as a whole.

Summary gauge buckets are half-open. A sample exactly on an aligned bucket
boundary goes in the following bucket and is excluded when that boundary is
the requested `end_ns`. The raw `who` path currently includes that endpoint.
Callers should account for this difference when comparing resolutions.

`Budget.sqlite_write` retains the conservative database + full possible WAL +
SHM + checkpoint bound. `growth_bytes` is accepted for existing callers but does
not impose a guessed payload-to-pages ceiling. SQLite can allocate any pages
that fit the shared hard bound. Optional `max_growth_bytes` imposes an explicit
additional page-count ceiling for bounded transactions; it is a hard limit,
not an estimate of payload expansion. Callers still own BEGIN/COMMIT/ROLLBACK. This can
leave close to half the managed limit unavailable for retained database pages;
it does not establish that a full-volume scanner map fits.

Admission holds `.budget.lock` only while checking and updating reservations.
Each active reservation has a 512-byte private record and its own liveness lock.
Other writers include its remaining peak allowance, subtracting bytes already
present for the same SQLite database. Generic reservations keep their entire
allowance until they finish. SQLite work, checkpoints, and waits run without the
shared lock. Temporary reservation contention retries outside that lock; actual
storage exhaustion still raises `BudgetExceeded`. A dead writer's unlocked
reservation is reclaimed, while its database/WAL bytes remain counted. There
is no PID-based liveness inference.

The scanner computes full-scan sharing outside a transaction and commits charges
in batches of at most 4,096 rows with a 16 MiB additional page ceiling. These rows
belong to the unpublished generation; final publication remains atomic. Both
collector and scanner must use this reservation protocol together. Do not run
an older writer concurrently: it cannot account for active reservation records.

Shutdown waits at most ten seconds total, including readers and the final save.
If that deadline expires, it returns failure and keeps the instance lock. A
finalizer releases it only after old readers finish and the final save succeeds.
The unsaved tail holds compressed evidence; only the recent two process reads
remain decoded for live queries. Interval snapshots capture immutable packed
references and selection/status metadata under the collector lock. Decoding
occurs after the SQLite snapshot opens and the lock releases. Nested `around`
answers reuse decoded records within that same snapshot; event records use the
same byte bound and report overflow in `coverage.truncated`.
Each flush captures a sequence after its health
readings, then drains through that sequence in bounded transactions (4 MiB of
compressed records, allowing one complete larger record). Later arrivals wait
for the next flush. Shutdown drains pending evidence under the same deadline.
Failed saves retain the pending readings and expose safe SQLite/budget cause
codes. The unsaved-buffer suspension decision remains provisional.

The default 8 MiB export chunk cannot hold a roughly 18 MiB ordinary JSON
Record from a 2,000-process pass. Such a caller needs an explicitly budgeted
larger chunk (for example `max_bytes=67108864`), or a separately agreed export
format. Private compression does not silently change the export format.
