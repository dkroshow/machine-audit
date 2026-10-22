# Development

The first installation runs Python source and adjacent dashboard assets. Use
Python 3.12 or newer. There are no runtime dependencies beyond the standard
library and platform interfaces. No Python dependency installation or wheel build
is required. The `install` command needs Xcode Command Line Tools (`clang`) to
compile the native scanner. `python3 -m machine_audit --help` displays the command
surface; use an explicit 3.12 executable when the system `python3` is older.

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

| Transition | Action and visible evidence |
| --- | --- |
| source due → reading | Start one read; keep actual start/lateness |
| reading → completed/failed | Preserve original fields or a safe failure code |
| read spans scheduled ticks | Skip those ticks; record counts, never stack reads |
| read exceeds ten cadences | Show hung; retain the original thread |
| pending batch → committed | Commit raw readings and rollups, then advance the watermark under the query lock |
| save fails before commit | Keep the pending batch and watermark; retry at the next flush |
| buffer high-water reached | Suspend new reads, preserve completed ones, expose skipped coverage and the suspension span |
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
full pass to preserve existing rank ties. Coverage derives whole passes in order,
carrying each source's prior reading and preserving all gap counts before output
truncation.
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
