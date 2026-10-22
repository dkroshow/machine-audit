# Development

The first installation runs Python source and adjacent dashboard assets. Use
Python 3.12 or newer. There are no runtime dependencies beyond the standard
library and platform interfaces. No Python dependency installation or wheel build
is required. The `install` command needs Xcode Command Line Tools (`clang`) to
compile the native scanner. `python3 -m machine_audit --help` displays the command
surface; use an explicit 3.12 executable when the system `python3` is older.


## Trial machine operating system

Big Mac ran macOS 15.6.1 with AMD graphics kexts 7.0.0 for all trial
evidence recorded before 2026-10-22 04:00 UTC, including the first real
filesystem map and the GPU hang of 02:00 UTC that day. It has run macOS
26.7.1 (build 25G241, AMD kexts 7.0.1) since the planned update at 04:00 UTC.
GPU collection (`enabled.gpu`) is off on Big Mac by the operator's decision
and reads `exclusive_gpu_reader_required`; no evidence after that time
includes GPU readings.

## Portable verification

Run the final check once on the candidate being handed off:

```sh
python3.12 scripts/check.py
```

The command runs core, source, scanner, onboarding and replay tests, compilation,
dashboard tests and the Git whitespace check, sequentially. It stops on failure
and uses the same Python executable for browser replay. Node 22.13 or newer is
required. The browser test currently uses Chrome at its standard macOS location;
its absence is reported as a skip, not browser verification. Check reported skips
before claiming coverage. Run only one final check at a time on a shared machine.

Tests use disposable stores and synthetic inputs. They do not run live OS
collectors, install a service, scan real filesystems, query GPU counters, grant
permissions, or contact an external server. Temporary HTTP servers bind an
assigned loopback port and shut down within the test. While editing, run only
the affected suite, for example `python3.12 -m unittest discover -s tests/core -v`.

Test scratch directories live inside the checkout (`tests/<suite>/.tmp`).
Budget admits a SQLite write only when the filesystem has free space for the
unused part of the cap plus a 64 MiB margin. With the default 10 GB cap, a filesystem with
less free space refuses every write as `free_space_margin`. A test that writes
through a store should pass a small `limit_bytes`, as the core tests do. Setup
refuses a data directory on such a filesystem, and doctor shows the free bytes
required and available.

The integrated implementation of the October 21 trial build passed 141 Python tests and eight Node
checks, including headless Chrome across all dashboard views, with no skips.
These synthetic checks remain separate from the native trial observations below.

The tests cover original snapshots, resets and identity reuse, sleep clocks,
per-instance timing, program denominators and peaks, atomic query/save races,
save failure, pruning, cumulative summaries, private labels, file export and
hash read-back, hard SQLite growth limits, HTTP authentication/Host/Origin and
client redirect refusal. Replay validates answers, not native collection.

## Runtime boundaries

One collector holds `.collector.lock` for its data directory. The CLI obtains
it before replacing the per-run token. Each source has one timer thread and no
read queue. The writer, query server, source loops and dashboard all share the
collector process. A blocked native call cannot be forcibly cancelled safely.

A source's `read()` runs outside the collector lock in its own daemon thread;
there is no shared worker pool or overlapping replacement read. The IOKit
bindings use `ctypes.CDLL`, which releases the GIL during a native call, so a
blocked GPU property read does not hold up unrelated source threads, flushing,
or HTTP status requests. After ten cadences, status and saved state events mark
that source `hung`; its missing measurements remain unobserved, not zero.

This is thread isolation, not process isolation: a native extension that holds
the GIL, a process crash, or shared driver/adapter locks can still affect other
work (the SMC sources share an adapter lock). Source construction and boot/clock
initialization also run synchronously before collection starts. HTTP has its
own server and request threads, but storage contention can separately delay
queries or the writer.

Shutdown attempts to save completed buffered readings before joining source
threads.
`close()` returns failure after its deadline if a native read or the final
write remains stuck; it neither cancels the call nor records a clean stop.
The daemon finalizer retains the store and instance lock until old reads return
and their final readings are saved, preventing an overlapping collector while
that process remains alive; process exit abandons remaining daemon threads.
This preserves the deadline without claiming the blocked measurement completed.

Final saves do not prune: at the storage limit, pruning until a save fits can
outlast any stop deadline. A final save is checked against the limit before it
merges its summary rows (about 15 s at 2,000 processes), so a refusal is quick.
The final saves also give up 2 s before the stop deadline (`close()`, 10 s by
default): merging a 2,000-process save takes about 15 s. A deadline of 2 s or
less leaves no time to spill, so saving then continues in the background. If the limit refuses
a final save, or it runs out of time, the collector
writes the unsaved readings and the held minute to its own private spill file
in the data directory (`unsaved-<run>-<time>.json`, mode 0600, created
exclusively within the budget, never replacing an earlier one). It then stops
cleanly and records `stop_unsaved_budget` or `stop_unsaved_deadline` in
`startup.json`. The next start
saves each spill file under its original run, with ordinary pruning, records
that run's stop, and removes the file; readings already saved are not saved
twice. If that fails, the file stays for a later start and collection still
begins. If even the spill file does not fit, the stop fails with
`stop_unsaved_lost` and those readings are lost.

| Transition | Action and visible evidence |
| --- | --- |
| source due → reading | Start one read; keep actual start/lateness |
| reading → completed/failed | Preserve original fields or a safe failure code |
| read spans scheduled ticks | Skip those ticks; record counts, never stack reads |
| read exceeds ten cadences | Show hung; retain the original thread |
| pending batch → committed | Commit raw readings and rollups, then advance the watermark under the query lock |
| save fails before commit | Keep the pending batch and watermark; retry at the next flush |
| buffer high-water reached | Suspend new reads, preserve completed ones, expose skipped coverage and the suspension span |
| stop requested with a read in flight | Save completed readings first, then wait for that read within the caller's close deadline |
| clean stop | Save, write a stop marker, release the instance lock |
| hung read or failed final save | Leave the run unclean; the next query shows the gap |

A query opens its SQLite read transaction under the same short lock that
protects commit/watermark advancement. It copies immutable packed references,
including source/component/status metadata, and then releases that lock.
Component and series admission precede tail decoding and the existing byte
bound. `around` shares the snapshot and decoded records across its subqueries.
Series classification is shared through `model.is_series`, including its
process-scope rule and self-cost exception. SQLite read connections register
that same function for catalog and summary selection; stored raw candidates
also receive a bounded field-level check for older permissive indexes.
Short `who` answers reuse their exact derived points for gap coverage. Longer
answers select non-CPU signals before deriving pending readings; CPU keeps the
full pass to preserve existing rank ties. Coverage of ten minutes or less derives
host and device passes in order, the same selection series uses, carrying each
source's prior reading and preserving all gap counts before output truncation.
Longer coverage takes its gap rows from host-series summaries and decodes no
pass: deriving an hour of one-second Linux host passes took 4-10 seconds.
Process sources keep their saved status counts; their per-process baseline,
reset and sleep gaps appear in `who`. Deriving every field of 2,000-process
passes made a one-hour coverage answer take about 30 seconds and still truncate.
Answers of an hour or more decode no unsaved pass while summaries cover them.
A record index on `reading_components` keeps each window reading's component
check from scanning older history; the first writable open after upgrading
builds it once.
Cumulative minute/hour rollups therefore remain coherent with captured raw tail data;
old readers use SQLite's prior snapshot. SQLite data work runs outside the
collector lock. No hourly reconstruction scan or duplicate summary history is
needed. Rollup metadata includes units, scale, source, scope and method.

## Cost and storage

`self.*` fields use ordinary live/history queries. The source adapter reuses
process observations for managed-process CPU/memory/I/O. Core health adds
source thread CPU, elapsed durations, due/done/late/skipped counts, query thread
CPU, flush thread CPU, response payload bytes and managed file sizes. These are
breakdowns of work already inside the measured process, not extra totals to
add. Browser rendering, unobserved helpers and kernel work remain uncovered.

All managed files live directly in the private data directory. SQLite writes
must use `Budget.sqlite_write`; generic `reserve` covers bounded non-SQLite
outputs. The SQLite guard limits pages and reserves database, WAL, shared
memory and checkpoint overlap; cache spilling is disabled. A pinned reader
can defer the next write. Pruning preserves incident records, source coverage,
current counter baselines and hourly summaries longer than raw/minute detail.

The default 64 MiB unsaved high-water is provisional. Completed in-flight
reads remain retained, so it is not a claimed hard process-memory ceiling.
The production bound and suspend-versus-drop policy need measured process-pass
sizes and explicit approval before unattended use. No unsaved-drop policy is
implemented. Full Big Mac cost, query latency during scans, storage growth,
permissions, source semantics and benefit remain trial measurements.

See [API contract](api.md) for source fields, query shapes, private client and
bounded export chunks. Source and scanner contracts remain platform-specific;
no portable test establishes Ubuntu or Big Mac source correctness.

### First native cost observation

On the Intel Mac trial, roughly 1,860 own-user processes produced about 39,000
fields per pass. A 60.026-second interval with the dashboard closed and no scan
used 16.76 collector CPU-seconds (0.279 CPU on average). Resident-memory samples
at the endpoints were 902 MB and 575 MB; these are not a peak bound. Managed
storage grew 15.50 MB. No save error or buffer suspension occurred, and no extra
CPU, memory or process reads were skipped. One network-probe tick was skipped.

This short observation does not establish sustained growth, low overhead or
product value. Operation-cost counters are recorded when an operation finishes;
a flush crossing a query boundary can contribute cost from before that boundary.
Use process CPU for the interval total, and do not add operation breakdowns to it.

Authenticated Tailscale browser checks rendered all ten views on real data. A
follow-up on the October 21 trial build measured one-hour Memory, Storage and Coverage views at
12.1, 9.1 and 17.8 seconds. These are individual page loads, not controlled
benchmarks; query cost remains material despite synthetic improvements. Two
controlled source replacements retained the scanner executable and its observed
Full Disk Access grant, preserved recordings, and verified clean stop markers.
The first full filesystem walk is still pending accounting and cost acceptance.

The Intel Mac restart recorded a one-off walk peak RSS of 1.2 GB after pruning
a 13.6 M-entry abandoned generation, versus 462 MB on a clean store; this
lifetime peak includes startup work and is not the walk's current resident memory.

### First Linux validation (dev box, October 22, 2026)

Revision 614fb7f on our own dev box: AWS t3a.large (2 vCPU, 8 GiB, 40 GB), Ubuntu
24.04.5, kernel 7.0 (the AWS kernel, not 6.8), Python 3.12.3, systemd 255,
cgroup v2 with only `cpu memory pids` delegated at the root, ping sockets closed.
31 unit cgroups and 120 processes.

**Run.** As an unprivileged user, a fresh clone passed `python3.12
scripts/check.py` end to end in 1 min 51 s. That covered core 68, sources 23,
scanner 53 (11 Darwin skips), onboarding 35 (5 Mac skips), replay 9,
compileall, dashboard node 6 of 9 (3 skipped: no Chrome) and `git diff
--check`. Node 22 was unpacked in that user's home for the dashboard tests.
Then the "Linux (Ubuntu) install" sequence, exactly: `useradd`, a clone as the
service user, `setup --non-interactive` with one test exclusion,
`install --yes`, the two printed root commands and `start`. All exited 0;
`doctor` reported ready with `Map timer: active, next run …`.

**Ten minutes under the units** (05:50–06:00 UTC, nothing else running):

| Measure | Dev box | First user's Target A |
| --- | --- | --- |
| Collector CPU (cgroup) | 26.6 s, 4.4% of a core | about 5% |
| Collector memory peak (cgroup) | 88.8 MB | 122 MB |
| Data directory growth | 7.14 MB, 0.71 MB a minute | 1.07 MB a minute |
| Units and processes | 31, 120 | 57, 280 |

Sources: 21 observed. These were gaps, each named: no cpufreq, fans, thermal
zones, hwmon power, RAPL or GPU (`no_devices`), `icmp_not_permitted` (internet
probe by TCP connect), other users' process I/O and GPU clients
(`not_enabled`). The one hwmon temperature is a driver sentinel and reads
unavailable. The resolver is the systemd-resolved stub (5.6 ms lookup).
`who` ranked one row per workload: CPU by unit and process totals, memory by
unit `memory.current`. Storage showed only our own process: other users' I/O
is denied, and with no `io` controller delegated no unit has `io.stat`.
Coverage listed 3,833 gap rows in ten minutes. About 1,700 of them repeated,
every 5 s, fields that can never be observed here: router ping fields with ICMP
closed, probe byte counts in TCP mode, the sentinel temperature and mains
state. Sources reading `no_devices` add no rows. A later build lists such a
run of reads as one row with its count.

**Scanner.** `scan --full` as the service user walked 97,285 entries in 9.4 s
wall, 4.2 s CPU, `peak_rss_bytes` 35.7 MB (`/usr/bin/time` agreed: 34.8 MB).
Root and `/boot` are observed with uses equal to listed and
`physical_residual_not_computed`. The EFI volume is a `denied` gap and the run
completed. The test exclusion and `/tmp` show only as `excluded_roots: 2`,
unnamed. The watcher held 6 folders. A file touched in a watched test folder
put that folder's inode into `scanner-dirty.json` (mode 0600, 67 bytes,
numbers only). The next `scan` was an update of 728 entries and consumed the
file. The hourly timer's own run was also an update, exit 0.

**Restart.** The OS's unattended-upgrades upgraded `liblzma5` at 06:03 and
needrestart restarted the collector. It wrote a clean stop marker (last save 2 s
before) and a new run began a second later. A manual `stop` and `start` gave
the same. After the queries above, systemd recorded a 337 MB collector peak at
that stop: the ten-minute `who` and coverage answers are computed inside the
collector. `install --upgrade --yes` to the installed revision refused with
`revision_already_installed`.

**Not run:** the browser dashboard and access through Tailscale, a night-window
full walk under the timer, growth beyond ten minutes, and any hardware sensor,
GPU or ICMP path (absent here).

**Upgrade to 6a06973** on the same box: `stop`, checkout as the service user,
`install --upgrade --yes` (exit 0), the two root commands, `start`. The old
markers took the 614fb7f suffix, and both releases remained. `doctor` reported
ready within 20 s, after the new index was built on first open. The same
ten-minute coverage answer took 1.7 s with 1,925 rows (3,833 on 614fb7f).
`who` took 4.0 s for CPU and 4.6 s for memory. Those queries raised the
collector's cgroup peak from 35.6 MB to 353.6 MB, and 217 MB stayed
resident: one short-interval query decodes up to its 32 MiB bound inside the
collector, and the server runs up to four at once. `scan --full` walked 97,486
entries in 2.9 s (warm caches) with `peak_rss_bytes` 35.6 MB.

**Upgrade to 227cc8f**, unattended as before: `install --upgrade --yes` renamed
the 6a06973 markers and kept three releases, and `doctor` was ready 2 s after
`start`. Coverage for the ten minutes before the upgrade took 1.65 s and
listed 17 gap rows (1,925 on 6a06973), raising the collector's cgroup peak
from 36 MB to 58 MB. `scan --full` walked 97,776 entries in 2.9 s, with
`peak_rss_bytes` 35.7 MB; the forced run retried the denied EFI volume. A
file touched in the watched test folder reached the dirty file within 15 s.
The next `scan` updated 3 entries, left `/boot` current and did not retry
the denied EFI volume, and consumed the dirty file.

**Upgrade to 907ef94** (top-consumer minute rows, 15-minute rows, the held
newest minute): `install --upgrade --yes` exited 0, renamed the 227cc8f markers
and kept four releases; `doctor` was ready 2 s after `start`. In the next ten
minutes the collector saved 9 times with no save error or restart, used 31.2 s
of CPU (5.0% of a core; 26.6 s on 614fb7f), 15.6 s of it saving, and peaked at
96 MB. The data directory grew 6.98 MB, 0.68 MB a minute. Coverage for those
ten minutes took 3.1 s and listed 201 gap rows, all in the first minute after
the restart: 180 counter baselines, 16 source statuses and 5 from the previous
run's end. `who` (raw, 75 rows) took 5.2 s for CPU and 5.1 s for memory,
raising the collector's peak to 288 MB. `scan --full` walked 97,894 entries in
2.9 s with `peak_rss_bytes` 35.7 MB, and retried the denied EFI volume.

## Review regressions and scale measurements

Run the portable boundary checks in a complete source checkout with Python 3.12:

```sh
python3.12 -m unittest discover -s tests/core -p 'test_core.py' -v
python3.12 -m unittest discover -s tests/core -p 'test_review.py' -v
python3.12 -m unittest discover -s tests/core -p 'test_scale.py' -v
```

The scale suite uses synthetic native structures, not live OS observations. It
covers 500 processes through the actual source adapter, exact raw recovery, and
hour/day answers for 600 programs and 2,000 memory instances. The long-answer
check verifies total accounting even when only seven output rows are requested.
Do not run it concurrently with a representative scanner benchmark.

A source-shaped two-pass profile at 2,000 processes / 42,000 fields reduced
counter derivation from 35.611 to 0.734 thread CPU seconds by indexing constituent
fields once per pass. Explicit ordinary-JSON conversion fell from 0.486 to 0.239
seconds. Seven-pass, one-minute fixtures through `Collector.ingest` and `flush`
measured:

| Processes | Ingest CPU | Flush CPU | Managed bytes | Process peak RSS |
| --- | ---: | ---: | ---: | ---: |
| 500 | 0.234 s | 3.386 s | 5.79 MB | 247 MB |
| 2,000 | 1.341 s | 15.199 s | 23.01 MB | 908 MB |

The 2,000-process fixture retained 1.79 MB of packed raw readings. Its raw
short-interval `who` query took 4.49 CPU seconds. RSS includes native mocks and
fixture records; these numbers exclude live acquisition and demonstrate
material remaining cost. They do not establish low production overhead or
benefit. The hard budget remains conservative; usable history and whole-map
fit still require combined measurements and an explicit decision.

An independent two-minute, 2,000-process fixture measured 10.7–13.7 CPU seconds
per flush and 11.3 MB of store growth in the second minute. That growth rate
extrapolates to about 16 GB/day; it is not a day-long or native measurement.
Per-instance minute summaries account for 5.6 MB/min and raw readings about
2 MB/min. The current cap therefore cannot imply days of detailed history.
Keep the accepted signals until a measured retention tradeoff is approved.

The full-day regression retains a dense first hour followed by hourly readings
through 24 hours, for all 600 programs and 2,000 memory instances. Five queries
(two hour/day CPU-memory pairs and one seven-row page) took 5.01 CPU seconds in
a fresh process with 102 MB peak RSS. The synthetic fixture occupied 202.75 MB.
These query measurements exclude building the fixture and do not measure native
sampling cost.
