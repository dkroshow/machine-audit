# Sources and scanner: implementation and evidence

The Mac trial is running; Linux support is being built. Python 3.12 and the
standard library are required, and installation compiles a native scanner
launcher. Imports do not start collection. Optional access and untested
hardware remain explicit gaps.

## Evidence ledger

| Path | Implemented | Verified here | Authorized Big Mac trial still needs |
| --- | --- | --- | --- |
| CPU | Original per-CPU ticks, runnable load, tick frequency and Mach timebase | SDK ABI layouts; synthetic contract | Actual values and independent CPU comparison |
| Memory | Original VM classes/counters, swap, pressure, reclaim/defrag swap-ins | SDK layouts; denied-read behavior | Field availability and source comparison |
| Process | One own-user rusage/task-VM pass; pid/start/exec, original counters, per-instance clocks; cached admitted labels | Synthetic original values, clocks, denied VM | Full process-count cost, retention growth, identity/restart |
| GPU | IOKit activity/memory/operating fields, raw reset accumulators, numeric client identity/time | Creator-string privacy check | Exclusive reader; reset/window semantics, device inventory and cost |
| Disk | Physical-medium IOKit operations/bytes/time; volume capacity/free/available/own-used | SDK/source inspection | Actual driver fields and volume comparison |
| Heat/power | SMC read-only methods, admission, separate power domains; exact fan fallback rule; battery/supply, thermal state, speed limits | Compiler layouts, synthetic admission | Intel values, costs, notification availability |
| Network | Paired timestamped ICMP, late/lost/no-path, dropouts, silent routers, connect override, own-traffic guard, salted router hardware identity, quiet baseline, DNS/connect, Wi-Fi, portal, optional bounded capacity test | Checksum/ARP fixtures; late/loss/dropout/silent-router rules | Kernel timestamps, every native/probe path, payload/wire cost and capacity method limits |
| Self | Existing process pass, storage/operation counters, scanner run checkpoints and recovery | Source boundaries; interrupted records retained | Independent cost comparison, short-lived child coverage |
| Scanner | Python metadata walk; direct hourly relists/new subtree walks; FSEvents replay; privacy; shared charges; staged publication; points and cursor queries | Disposable walk, arithmetic, shared-charge reuse, shallow updates, budget failure, permissions, cursor tests | Actual APFS extents, all volume layouts, full/hourly cost, stable FDA identity |

No live collector, GPU dictionary read, protected-folder probe or live
filesystem walk ran for this contribution. Portable fixtures do not prove
native behavior. Linux collectors are not implemented or validated.

### Big Mac native boundary correction

On 2026-10-21, bounded native probes confirmed three defects. The installed
`sys/proc_info.h` defines `PROC_UID_ONLY=4`; selector 2 filters process groups.
The old count-only call returned 1 PID; the corrected call returned 2,040,
matching a fresh numeric `ps` UID count. Listing alone used 0.820 ms CPU;
full per-process reads and their cost remain for owner acceptance.

Compiling the installed SDK selected `getmntinfo$INODE64` for its 2,168-byte
`statfs` (flags offset 64, mount offset 88). The old unsuffixed binding used
the legacy ABI. Separate and combined Data-mount `getattrlist` probes confirmed
that SPACEUSED precedes UUID: used bytes at offset 24, UUID at 32. Missing used
bytes retain a null value; missing UUID or short replies yield no volume.
The corrected Data-only probe returned one volume: capacity 2,000,796,545,024,
free/available 272,752,959,488, used 1,696,542,773,248 bytes. These are distinct
native readings, not inferred folder totals. Six focused tests pass; the three
new boundary regressions failed before correction. No process names, argv,
volume UUIDs, filesystem walk, GPU/SMC read, or service change was involved.

### Native directory opening

A real disposable root/child walk reproduced `directory_unreadable` when
Darwin's `O_NOFOLLOW` and `O_NOFOLLOW_ANY` were combined. Native directory opens
now use `O_NOFOLLOW_ANY` alone, matching the predecessor; the portable
path retains `O_NOFOLLOW`. The corrected tiny walk lists one entry and records
both directories. Intermediate and final symlinks remain refused, and a real
opened descriptor with the wrong inode is rejected before enumeration. Five
focused tests passed: three new native-open tests plus two existing scanner
tests. This correction changes no stored map/history or scan
cadence; no live filesystem walk was run.

Full publication now requires a completed root listing (`Walker.listed`),
not a nonzero entry count or merely a staged node. A denied root can stage a
denied node without listing; its pending buffer is discarded and publication
is skipped. The previous generation, volume status, children, points, hierarchy
and bookmark stay intact. A readable empty root still publishes with zero
entries. Other volumes proceed; mixed success reports `partial`, while all
attempted roots failing reports `root_unreadable`, including the stored run
status. Failures and gaps appear in per-volume results. Three added lifecycle
regressions cover these cases with disposable directories and stores; no live
map was modified.

A legacy failed generation without a root node does not establish a completed
full scan, even if it has a recent `full_ns`. The runner checks root presence
through the existing index before applying that timestamp to scheduling.
Constructed maps, including readable empty roots, retain the normal daily
full-scan limit. No stored timestamps or history are rewritten to enable retry.

## Collection contract

`create_sources(config, budget, operations=None, scanner=None)` supplies the
core Source protocol. `boot_id()` hashes kernel boot time. Native clocks use
Mach absolute and continuous time. Per-process fields retain actual read
bounds. Core routes individual memory and disk fields from the bundled CPU
pass, without another process scan.

Self metadata reports `self.config_identity` and the named
`self.install_revision` from `source_options.install_revision` beside it.
The revision uses the `git_commit` unit; absent or invalid revision values are
explicit gaps, never a guessed build identity.

Original process fields include `process.user_time`, `system_time`,
`runnable_time`, `child_user_time`, `child_system_time` in Mach ticks;
`diskio_bytesread`, `diskio_byteswritten`, `logical_writes` in bytes; and wakeup
counts. Footprint, resident, compressed and graphics ledgers remain separate
memory scopes. Converted CPU rates say CPUs; raw totals retain source units.
Reaped-child CPU is preserved but never added to observed child CPU.

GPU device reading requires `enabled.gpu` and the installation's exclusive-reader
gate. Selecting dictionary fields does not avoid AMD reset effects. Reset
accumulators retain their raw values with unobserved interval coverage. No rate
is invented from the dictionary-call duration. Driver percentages retain an
unknown window. Driver creator text contributes only a numeric PID.

Optional root sources report `not_enabled/shipped_helper_required`; no
administrator helper is shipped yet, and the old helper is not invoked.
Future integration needs a narrow helper protocol for raw rusage/task-VM
records and bounded package-power samples.
There is no configurable executable/command bridge.

Network latency is kernel-receive time minus send time. Router and internet
requests leave before waiting. Sleep invalidates pending probe evidence. Late
replies are distinct from lost replies. Window fields explicitly describe the
last five minutes; generic interval means of those fields are not newly computed
interval percentiles. Quiet baselines require 30 observed quiet minutes after
startup. Source readings retain enough detail for later recomputation. The
optional speed test defaults off, needs an explicit daily payload budget, and
never claims Python streams saturate the link. TCP/TLS overhead is not measured
by its payload byte count. No active capacity test ran here.

## Scanner entry and permissions

`machine_audit.scanner.run_scan(data_dir, *, full_disk_access=False, full=False,
grain='files', roots=None, access=None)` runs once, behind the owner's stable
launcher. `main(config, root=None)` is the existing CLI hook. Roots must be
exact discovered local volume mounts. The scanner changes no services, grants,
existing recorder or user files. The installation owns the coexistence gate.

`machine_audit.scanner.permissions.observe_access(probe=False)` returns an
`AccessObservation(status, pid, uid, executable_sha256, observed_ns)`.
`probe=False` reports unchecked and touches no protected folder. **Only an
explicitly approved install-time action under the deployed scanner identity may
call `probe=True`.** It enumerates one entry from the fixed Mail directory,
retains no name, and can cause a privacy prompt. Missing directory, denied,
failed and observed are distinct. No such probe ran here.

Protected scanning requires an observed grant from the same process/user and
interpreter, at most 60 seconds old, supplied directly as `access`. A requested
config setting grants nothing. An existing rich map without fresh proof is
preserved and marked `access_unverified`; it is not replaced by a lean map.
This is not evidence of revocation. Actual grant-loss handling remains the
owner's installation policy.

## Map, accounting and queries

`MapQuery(data_dir / 'scanner.sqlite').query(action, **params)` supports
`volumes`, `children`, `growth`. Shared block addresses exist only in bounded
memory. The private SQLite file holds names, identities, sizes and stream
charges; ordinary record exports exclude it. Child file rows have `kind=file`
and allocated `listed` bytes; their `uses` is null because folder charges do
not establish a file's reclaimable or physical bytes.

Map generations publish only after totals and points commit. Writes use the
shared SQLite budget, one transaction per reservation. The last published map
survives failed construction. Replaced/abandoned generations are reclaimable;
size points and run evidence remain. Shared charges survive unchanged updates.
Changed partial streams can use a private-byte lower bound until the next full
map, explicitly marked in gaps. Uses above measured volume use are withheld,
never rescaled. Unsupported filesystems expose listed sizes without a physical
residual. Parent/child totals are stored; no query walks the filesystem.

Queries take `volume_id`, `parent_id`, `limit` (1–2000), and an opaque `cursor`.
Growth takes Unix-second `start/end` or integer `start_ns/end_ns`. Nonzero
OFFSET is rejected. Cursors bind the map generation and observation, action,
volume, parent or interval, metric and sort rule; growth also binds the selected
historical hierarchy. Stale/incompatible cursors fail explicitly.
Children read two indexed bounded candidate sets. Growth reads at most `limit`
tracked identities, two indexed boundary points per identity, and an indexed
historical identity/parent lookup. It does not read the full map.

Replies carry schema_version, shape, action, status, coverage, rows, truncated
and next_cursor. Growth preserves original endpoint values, actual point times
and metric; missing baselines or incompatible metrics give null change. Every
tracked identity is reachable by cursor, including sub-GB and zero changes.
No ancestor is suppressed. A parent's total includes its subfolders: Projects
+10 GB and nested Transcripts +3 GB are both shown, not added together or
replaced by an inferred residual. Coverage declares
`growth_grouping=original_totals_with_overlap`, `nested_totals_overlap=true`
and `global_growth_complete=false`, including after the final page.

Volume rows include `id,label,observed_ns,full_scan_ns,listed,uses,volume_used,
not_attributed,capacity,free,available,valid,status,mounted,gaps`. Children include
`id,parent_id,label,kind,listed,uses,status,observed_ns`. Private names are null.
Growth includes `id,parent_id,parent_label,label,metric,start_metric,end_metric,start_ns,end_ns,
start_bytes,end_bytes,change,status,hierarchy_observed_ns,hierarchy_status`.
`start_metric` and `end_metric` identify each original observation's measure;
each is null if that endpoint has no point. The compatibility field `metric`
remains the end metric. A uses-to-listed transition keeps both sizes with
their distinct labels and a null change.

Historical hierarchy observations are retained at each committed map
publication without backdating. Growth selects the latest observation at or
before the requested interval end. Parent IDs and labels come only from that
hierarchy, even after a later move. Coverage includes the selected
`hierarchy_observed_ns` and epoch status (`observed` or `unobserved`). Row
`hierarchy_status` is `observed`, `not_observed` for an explicitly absent
identity, or `unobserved` when no epoch/identity is available. An observed root
has a null parent with `observed` status. `parent_unobserved` retains an observed
parent ID whose historical row is absent; its label stays null. Only tracked
folders have historical label rows, so an untracked direct parent's name can
be unavailable. No current parent or label is substituted. Null private labels
also stay null.

## Remaining measurement and design limits

Native full and hourly Python cost remains unmeasured. Compact-map synthetic
cost is recorded below. The shared SQLite budget prevents overflow but can
pause collector saves while held. Measure collector lateness, retained size,
checkpoint peaks, and both map grains before unattended use. A failed
block map or incomplete event replay remains visible; synthetic success is
not proof of APFS correctness.

The scanner records CPU, peak resident memory, native written bytes when
available, entries and timestamps from start through progress checkpoints.
Interrupted runs retain their last checkpoint and are identified when the next
scanner acquires the single-instance lock. Checkpoint gaps remain unknown.
Browser rendering, indirect kernel work and unobserved short-lived processes
remain explicit self-cost gaps. Measuring cost does not establish benefit.

## Compact map publication

Private format 2 uses SQLite `WITHOUT ROWID` map tables, integer generation and
volume keys, and lossless unsigned-inode encoding into signed 64-bit keys.
Nonnumeric fixture IDs occupy a separate TEXT namespace. Shared-block range
records reference an ordinal, preserving the distinction between unsigned
clone IDs and negative partial-copy IDs. File names stay in their rows;
directory paths are reconstructed from parent/name, with a 1,024-ancestor
bound and explicit missing/cycle errors. The public query shapes and IDs stay
the same. Legacy-format stores are refused intact; this build uses fresh stores
and does not attempt a live migration.

Opening SQLite and configuring its journal reserve 256 KiB before the first
connection can allocate files. That reservation ends before schema creation
uses the shared SQLite write budget. Failed initialization closes the
connection; a startup budget refusal leaves existing stores intact. An
automatic SQLite rollback preserves its original failure code.

Full scans retain the published map while building a compact replacement.
Writes flush at 8,192 queued operations or 2 MiB of argument data. Directory
totals use a directory-only working set and short staging transactions; files
remain in SQLite. The final transaction publishes the volume pointer, original
size points and observed hierarchy together. A failure leaves the previous
complete map and its history available.

Hourly updates stage changed directory listings, files and shared-block rows.
Only changed folders and affected ancestors are recalculated, then applied in
one publication transaction. Unchanged files are neither copied nor rewritten.
Moves update both old and new ancestor chains. Retired full generations are
reclaimed before the next full build, not by scanning the active map each hour.
Sharing and ancestor totals commit only to the unpublished staging generation
in bounded batches. Final publication counts the changed rows and variable
payload for its shared DB/WAL/SHM reservation; it does not reserve a second copy
of unrelated map/history pages. Its atomic transaction retains the previous
map and history if admission or publication fails.

## Compact map measurement

`python3.12 -m tests.scanner.measure_compact` defaults to 10,001 synthetic
entries. The coordinated representative run used `--directories 200000
--files 800000 --replace`: 1,000,001 entries, directory listing metadata and all
indexes, followed by a full replacement and one changed-directory update.
It used numeric sizes and synthetic names; it did not walk a filesystem.

| Phase | Thread CPU | Last publication transaction |
| --- | ---: | ---: |
| Initial metadata staging | 10.414 s | — |
| Initial aggregation and publication | 3.335 s | 0.126 s |
| Replacement metadata staging | 11.071 s | — |
| Replacement aggregation and publication | 3.849 s | 0.136 s |
| Hourly update publication, 26 changed rows | 0.051 s | 0.051 s |

The initial map occupied 74,149,888 managed bytes (74.15 bytes per entry).
The retained pair occupied 150,884,352 bytes. That included 60.04 MB of file
rows, 32.12 MB of file indexes, 23.91 MB of directory rows, 24.88 MB of directory
indexes and 9.81 MB of listing metadata. Peak combined managed size was
159,021,576 bytes, with an 11,441,272-byte peak WAL. The process reached
175,869,952 bytes of resident memory, including the fixture and directory
aggregation arrays. The longest measured write transaction used 0.161 s CPU.
All original totals and the update's added 8,192 bytes were checked.

At this fixture's name lengths and directory mix, 13.5 million entries project
to about 1.00 GB per map and 2.04 GB for the retained pair. This is a projection,
not a measured Big Mac map or a bound for all filenames, history and sharing
metadata. The conservative budget still requires roughly twice the database
space plus other managed files. If admission fails, the last complete map stays
published; no file grain, cadence, history or accounting mode is silently reduced.

## Scheduled replay regression

Ordinary events whose path no longer exists now shallow-list the nearest
known directory. New child directories still receive an initial recursive
walk; `MUST_SCAN` retains recursive semantics. Private directories can be
resolved by inode through the walker's no-follow directory open, without
persisting event paths or exposing their contents. Every ancestor's path policy
is checked before that open, including protected directories absent from the map.
A shallow parent listing
cannot remove a child's previously observed privacy boundary.

Disposable-directory regressions reproduce the old amplification: an ordinary
missing path visited 33 entries instead of the required three, and a private
event visited three instead of one. Both now visit only the required entries.
A 150-directory recursive update previously committed 304 transactions;
buffered directory-reset records reduce it to at most four before publication.
Reset and insert order is preserved, including repeat visits after a staged
file was removed. Publication remains atomic, and failure keeps the prior map.
These checks do not measure Big Mac hourly throughput or prove event-history
availability. Counted insert/charge/aggregate and final-publication writes use
the shared write contract in `storage-approach.md`.

### Scheduled-run recovery

The synthetic 64%-full store regression keeps 8 MiB of unrelated payload,
reopens the existing schema, and publishes a changed folder. It checks every
write's declared row count and actual WAL frame count against the shared bound.
Its final transaction made 28 row mutations under a 39-row declaration and
wrote 19 WAL frames. Peak managed bytes were 8.64 MB under a 13.32 MB cap.
Existing rollback tests still preserve published children, points and hierarchy
after a failure in the final transaction. No live store or native scan was used.
Update point reservations count every changed identity, including removed
subtree identities absent from the staged nodes. The removed-subtree regression
keeps terminal unknown points for all three removed tracked folders.

Three quiet replay updates keep the no-progress count unchanged. An update
with events and an unchanged bookmark increments it; advancing that bookmark
resets it. Three such failed advances still request a full map. Quiet hours do
not request a full map, and killed unpublished runs change neither counter nor
bookmark. Missing roots/bookmarks, seven-day age, changed event UUID, and kernel
drop/wrap/root-change indications retain their existing full-scan fallbacks.

An ordinary exception during a started macOS run now records a reason such as
`os_error_OSError`, `invalid_scan_state_ValueError` or `unexpected_error_TypeError`,
ends its run row, and returns `status=scan_failed` plus that reason and the
separate `exception_type`. Budget/SQLite failures retain their status codes and
append the exception type to their reason too. Messages are never included.
JSON reports `failure_recorded=false` if failure persistence
also fails; exception text and paths are never returned. Both scanner CLI
entry points emit the result and return nonzero on failure. A process killed
outside Python still cannot finalize its own run; the next start marks it
interrupted. Exit code 1 and bytes written alone cannot identify the exception
in a historical run that predates these reason codes.

### Replay decisions and filesystem races

The run row and scanner result now retain `volume_id`, `action`, `trigger` and
`stage` for the current or last volume. Stages distinguish discovery, replay,
preparation, walking, sharing, aggregation and publication. Result volume rows
also retain their action and trigger. Nullable columns extend existing format-2
run history without replacing any map or inventing past diagnostic evidence.
Older scanner writers with a positional 12-column run insert cannot write this
extended table; rollback must retain the explicit-column insert. Existing
read-only consumers remain compatible.

Full triggers distinguish a missing full map, missing bookmark, repeated event
non-progress, the weekly deadline, an eligible explicit request, event UUID
mismatch and the specific kernel-drop/wrap/root-change flag. A recursive event
at the volume root, or a deferred whole-root update, uses the full walker and
full staging path. Recursive child
events remain incremental subtree updates. The `partial_stream_needs_full_map`
gap does not schedule a full scan; it waits for an independently required full.
There is no fixed ten-hour FSEvents expiration rule in this implementation.

A selected directory replaced by a file before it opens records
`root_not_directory`. An unreadable replay root keeps that volume's previous
map, points, hierarchy and bookmark, while other volumes may finish. Missing or
cyclic map ancestry still blocks publication, because publishing it would make
the totals wrong. Such invariants now carry specific source-owned codes, for
example `missing_directory_ancestor_ScanError`, instead of only the broad
`invalid_scan_state` category. Arbitrary exception messages remain private.

Disposable regressions reproduce the old directory-replacement failure and
the old incremental writer used for a recursive volume-root event. They also
verify diagnostic-column migration, saved action/trigger/stage on failure,
specific invariant codes, and the fact that a partial-stream gap does not
force an hourly full. These tests cannot identify an old live exception or
event flag whose diagnostic evidence was never recorded.

Deferred folder refreshes leave `dirty` only when the matching staged listing
publishes. Admission failure or transaction rollback keeps the refresh pending;
unobserved folders are not cleared. Cleanup joins each dirty identity to the
listing primary key and counts the deletions in the publication reservation.

Failed-volume recovery: an explicit full request retries a failed attempt even
inside the successful map's daily cooldown. Access checks still run first;
published map times, points and hierarchy change only on successful publication.
Unreadable root attempts now save their safe reason in `volumes.status`, while
invariant exceptions already save their specific code there. A partial run at
stage `complete` means a root/relist was unreadable, not an uncaught invariant.

A disposable tree reproduced `cyclic_directory_ancestry` when `stat()` denied a
queued child: the old handler reused the previous directory's inode and stored
a self-parent. Each iteration now starts without an inode; failed metadata reads
record the denied gap without inventing identity. Separate synthetic cloud,
other-account and own-store exclusions publish without breaking ancestry. This
does not prove which exception caused the historical Big Mac failure.
