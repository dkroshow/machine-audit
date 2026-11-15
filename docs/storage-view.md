# Storage view

Implementation guidance for [Storage](machine-information.md#storage). The
[storage approach](storage-approach.md) owns cadences, preservation and the
budget; the [product brief](product-brief.md) owns the human/agent questions;
the shared [time and coverage rules](cpu-view.md#time-windows-and-coverage-working)
apply.

The storage scanner of an earlier private prototype is the reference design.
The Machine-audit docs own the design here; the prototype's rules and
measurements are evidence for
what carries to Python and Ubuntu, what does not, and what must be measured
first.

## What the map must let a person do

Storage is a matter of visibility: what is on the drive, and how that changes
over time. An initial scan builds the map; regular updates keep it current.
On any adapted OS, a human or agent can:

1. **See how much is free,** now and as a trend, with the rest of the drive
   split into parts that add up to its capacity.
2. **Open the drive from the top down.** Start at the drive's parts, open a
   volume to its top folders ranked by size, open any folder to its children.
   Every folder's total is stored, so each step is a query of the map, never
   a walk; a folder with many children pages under the shared answer bounds.
3. **See growth over any interval:** an hour, a day, a week. Which folders
   grew or shrank by 1 GB or more, by how much, and which program was writing.

Humans and agents get the same answers. What the map cannot see is shown
beside what it can, never hidden.

**Volumes.** A laptop has one volume; Big Mac has eight. Every mounted local
volume is a top-level entry with its own parts, map, reconciliation and
history. The view opens on the startup volume and the person or agent
selects another, as the prototype's Storage tab does. A volume keeps its
identity and history across unmount and remount, with the absent span a gap.
On macOS an external drive is walked only with Full Disk Access, because the
OS would otherwise ask the user; without the grant it is a named gap with
its live parts still shown.

## Source to record to answer

**Capacity and its parts.** Free space is read once a minute as a gauge and
live when shown. The parts are read live too, and a run of the map also saves
the parts it saw, so a map readout shows its run's parts with the run's time
and live free space separately. macOS: `statfs` per disk and `getattrlist`
(`ATTR_VOL_SPACEUSED`) per volume give capacity, free, available to programs
and each volume's own used bytes; the rest is not mounted; the parts add to
the byte; a container's volumes share one free pool; purgeable space is a
note, not a part.

**Consistent with Finder.** Every size is decimal (1 GB = 10^9 bytes), as
Finder and macOS settings show them. Free space is shown as macOS reports it:
`statfs` available, the figure `diskutil` and About This Mac give, from the
collector's minute reading (`storage.available`), with the time it was read.
The map's own capacity figures are from its scan's start and can be an hour
old, so the storage answer replaces them with the newer live reading and says
which it used (`capacity_source`). Finder shows more: it counts what macOS would
purge on demand as available. The capacity source also reads that figure,
`NSURLVolumeAvailableCapacityForImportantUsageKey`, as
`storage.available_important` (about 7–13 ms of CPU a volume, about 5 s an
hour on Big Mac's nine volumes), and the view shows the difference as its own
line: purgeable, which macOS frees on demand and Finder counts as available,
so Finder shows free plus purgeable. Folders differ from Finder where copies
share blocks; the asterisk's note says so beside the number.

Ubuntu: `statvfs` per mount gives capacity, free and
available (free less the root reserve) per filesystem. One filesystem per
partition adds trivially; btrfs, LVM thin pools and ZFS share pools and their
snapshots sit in no folder. The Ubuntu layout is unverified; the record
carries the filesystem type, and the readout says when parts do not add up
rather than forcing them to.

**The map.** One walk of every local volume lists every folder and file:
allocated size, link count, dates, whether the file may share blocks. The
walk visits every file on every machine, because a folder's size is only
known by visiting its contents; what is *stored* is a per-machine
[grain](#grain-process-and-cost). Each folder keeps three figures, and the
main view shows one of them:

- **Physical:** the space the folder's files take on the disk, each block
  counted once however many of its files hold it. A block shared with a copy
  outside the folder counts here and in the copy's folder too: it is part of
  what fills the disk wherever a copy is. This is the headline and the only
  size in the main view. A file's row shows its allocated size, which is its
  own physical space.
- **Listed:** each entry at its allocated size, every copy and every hard link
  in full, as Finder counts. It can exceed what the disk holds. Copying,
  moving or backing the folder up to another disk would take about this much.
- **Frees:** the blocks every copy of which is inside the folder: about what
  deleting it would give back while snapshots are not holding the blocks.

A folder is marked with an asterisk when Finder shows at least 10% and 1 GB
more than its physical space, or when at least 10% and 1 GB of its physical
space is shared with copies outside it. The asterisk opens one plain note:
it shares data with copies elsewhere (APFS clones, hard links, snapshots);
Finder and du show X, and copying, moving or backing up elsewhere would take
about X; deleting it frees about Y (or "frees less") while the other copies
remain. X is listed and Y is frees; the query returns both, and the rule
lives in `accounting.asterisk`.

**How physical is measured.** APFS flags every file that may share blocks.
Only those files need their extents: on Big Mac 1.5 M of 13.5 M files, in
477 k clone ids. Files that share a clone id share all their extents, so a run
reads one file of each clone id and every changed clone, by inode through
`/.vol/<device>/<inode>` with `F_LOG2PHYS_EXT`, about 70 µs of CPU each. Every
other file's physical size is its allocation. A sweep over the extents splits
the shared bytes into pieces, each held by one set of streams, and joins
streams that share any piece into a sharing group. Each piece becomes byte
terms on folders: plus its size at each folder holding a copy, minus its size
for each extra branch where copies meet. Summed over a subtree, as every other
total is, the terms count the piece once in every folder holding a copy. A
second term at the copies' common ancestor gives frees. Extents exist only in
memory during the run; the map keeps stream keys, groups and byte terms,
never block addresses. The pass reads holders a page at a time and writes only
between pages: an open statement pins the WAL that every counted write must
checkpoint first, and on Big Mac that failed the first two full walks at the
start of sharing. On Big Mac's startup volume (1.55 M folders, 1.06 M holders,
267 k streams, Nov 10 2026) the whole pass took 127 s of CPU and 215 s, with
560 k extent reads and a peak of 471 MiB.

An hourly update re-reads the extents of the streams whose holders changed and
of every stream in their groups, so a changed copy is measured against the
unchanged copies it shares blocks with; an unchanged stream in a re-listed
folder keeps its terms. Reads stop when the update's share of the run is spent,
like its listing: a stream left unread counts only the bytes APFS says are its
own, the rest unplaced (`unplaced_shared`), and the run reports
`sharing_reread_capped`. Neither ever counts a block its partner holds. (A fixed
cap of 20,000 reads, which then counted each stream by its allocation, made a
catch-up after a paced walk count 62.5 GB of clone pairs twice on Big Mac, Nov 14.)
A map over its used space also re-reads, together, the streams it counts in full
though APFS says they share blocks, so a pair counted apart is measured as one
group again and the map can be judged valid before the next walk. A copy made since the last
full walk and already changed when the update sees it has its own stream and
a partner the update cannot know without saved addresses. APFS reports how
many of its bytes are private; the rest is counted where the partner is. Its
own folder then shows less than its physical space until the next full walk,
the volume total stays exact, and the run reports
`shared_copy_placed_at_full_walk` with the bytes (`unplaced_shared`).

**Measured cost** (Big Mac, Nov 10 2026, a scratch map of a tree where half
of the 372 k entries are shared copies: a browser's application, its 104
code-signing clones and a 33 GB database). A full walk took 69–77 s of CPU
against 73–78 s for the previous release on the same tree, with the same 4,411
extent reads and a lower peak memory (150 MiB against 169–174 MiB): extents are
read once per clone id, as before, and the folder tree is loaded only after the
sharing pass. An hourly update that re-walks one clone of that family, and so
re-reads the whole family's 1,957 streams, took 7.3 s against 107.6 s; an
update that touches no shared file reads no extents. The startup volume's full
walk (14.9 M entries, 2,135 s of CPU before this change) reads extents for
about 0.5 M streams, about 35 s, as it did before.

**Unsupported on Ubuntu by default:** ext4 shares nothing beyond hard links,
which count once per folder by inode; btrfs and XFS reflinks would need the
`FIEMAP` shared-extent flag, unverified. Ubuntu folders report physical as
allocated space with hard links once, and no residual is claimed.

**Reconciling the map with the volume.** The volume's root total, physical in
folders, plus a separately labelled line, **not placed in a folder**, equal
the volume's used space read after the walk. Not placed covers snapshots,
file-system records and areas the scanner cannot read. Folders may exceed used
space by up to the larger of 1 GB and the change in used space during the walk
(for a paced walk, every move of used space while it ran), because files change
while the walk runs; beyond that the folder figures are withheld, never
rescaled, until an update reconciles again or the next full scan. Deferred
folders can hold back only an over-count: data moved from a deferred folder
into a re-listed one counts twice until the deferred folder is re-listed, so an
update that left folders deferred and exceeds the tolerance is not judged
(`reconciliation_deferred`) and the map keeps its last verdict. Within the
tolerance it is judged, so a large folder deferred every hour cannot keep a map
invalid. Folders the map holds as denied do not count as deferred: they have no
measured size, and they are still retried hourly. While the verdict waits, the
tab shows "not placed" as pending ("folders exceed used space by X until N
deferred folders are re-listed"), never a negative figure.

A file hard-linked after a folder holding it was listed is a plain file in that
folder and a holder in the re-listed one, so an update also re-lists the folders
that still hold a newly linked file as a plain file; every link is then a holder
and the file counts once. When the published map is invalid, the update checks
every hard link this way, which repairs a map an earlier release left counting
links twice. Finding those folders reads the volume's file rows once, however many
links there are: about 17 s on Big Mac's startup volume (11.9 M file rows), and only
in an update that meets new links or repairs an invalid map. The re-lists it finds
are paced like any other listing; those past the allowance are deferred. Rows on one page can add to more
than their folder when they share blocks with each other; the page states that
amount ("shared between the rows") when it is at least 1 GB, so rows, that
line and not placed add up. On Ubuntu used and listed are shown side by side
and no residual is claimed.

**Change.** Every update saves a size point for each folder of 1 GB or more
(10^9 bytes) whose size changed since its last point. The 1 GB test and the
reported growth use physical where it was computed and listed otherwise, the
metric named on every point; the two are never subtracted from each other.
Points saved before physical space was measured carry the metric `uses`, so
growth across that change is not claimed. Lifecycle rules, so growth answers are honest:

- A folder is tracked by the identity its filesystem exposes (volume plus
  file id on APFS, device plus inode on Linux), not by path. A rename or move
  within the same filesystem keeps the identity and records the new path; a
  move across filesystems is a new identity. Ids can be reused after
  deletion, so continuity is uncertain until validated on each target, as
  with cgroup identity in the [architecture](architecture.md#source-boundary).
- When a tracked folder falls below 1 GB or is gone, the update saves a
  terminal point that says which: shrunk below the line, with the measured
  size; confirmed deleted; denied; or not observed this run. Denied and not
  observed mean unknown, never zero and never evidence of deletion.
  Subtracting the last size would otherwise report no shrink.
- A first qualifying point establishes observed size, not growth; without an
  earlier comparable observation, growth is unknown.
- An interval is answered from each tracked folder's last point at or before
  its start and at or before its end, returning those actual observation
  times. A nightly map cannot prove nothing changed within an hour, and no
  point is invented at the requested boundary.
- Show each tracked folder's original sizes and change, including small and
  zero changes. Label folder totals **Includes subfolders**: a parent growing
  10 GB and its child growing 3 GB means the child's growth is already inside
  the parent's 10 GB. Keep both rows; never add nested totals or hide a parent
  because a child changed. Show observed historical placement when available;
  missing placement stays unknown. Pagination must preserve every tracked row.

**Attribution.** Three questions, three sources, never merged:

| Question | macOS | Ubuntu |
| --- | --- | --- |
| Which folder holds it | The map | The map |
| Which program is writing | `proc_pid_rusage` bytes written, own processes, 30 s process pass | `/proc/<pid>/io` `write_bytes`, own processes; other users' denied without privilege |
| Which program has a folder open or as its working folder | Per-process vnode info, own processes, on request | `/proc/<pid>/cwd` and `fd`, own processes, on request |

**What the map cannot see,** recorded per run by name, never by guessed
size: folders the OS refused; folders skipped by rule (other accounts' homes,
counted but not entered; cloud-only files; backups; user exclusions);
protected system areas. macOS's two-list rule carries: folders that would make
the OS ask the user are never opened without Full Disk Access, the rest are
tried and the refusal recorded; a map without the grant is `lean`, and a map
built with it is removed when the grant goes. Ubuntu's usual gaps are other
users' homes and root-only directories such as parts of `/var/lib`.
Configured exclusions appear as the `excluded` gap with `excluded_roots`, a
count of excluded folders on the volume; their paths never leave `config.json`.

## Initial scan, then updates

Same model on both operating systems; different mechanism, stated on the map.

| | macOS | Ubuntu |
| --- | --- | --- |
| Full scan | Weekly; also first run, a lost change log, or on request (at most daily); paced over hours within a CPU allowance ([paced full walks](storage-approach.md#paced-full-walks)) | Nightly: the update *is* a re-walk |
| Update | Hourly: replay the file system event log since the last bookmark, re-list only changed folders; folders with more than about 20,000 direct entries at most daily | Hourly: re-list the hot folders an inotify watch in the collector saw change (folders of 1 GB or more, or directly holding a file of 100 MB or more; at most 8,192 and an eighth of `max_user_watches`), with the same daily limit for large folders |
| Why | The event service keeps a persistent log whether or not anyone listens | Change notifications exist only while a process listens; per-folder watches cost kernel memory, need a raised limit and lose events on overflow; mount-wide watching needs root. So the walk is nightly, as Linux's own `locate` does, and only a bounded hot set is watched; an overflow re-lists every hot folder |
| History grain | Hourly points | Daily points; hourly for hot folders that changed |
| Map age shown as | "updated 14:00, full scan Sunday" | "as of 02:00 today" |

The prototype's recovery rules carry: an interrupted log read restarts from the
last event received; three hourly runs without progress force a full scan. On
Ubuntu, changes made while the collector is stopped wait for the nightly walk.

## Grain, process and cost

**Grain per machine.** Storing fewer rows does not make the walk cheaper; it
makes the store smaller and keeps fewer names. *Every file* (Big Mac): file
browsing, largest-file lists, "this file appeared Tuesday". *Folders, large
files and shared-block files* (default elsewhere): every folder; files above
a threshold, working figure 100 MB; every file that may share blocks or is
hard-linked, because physical space needs them. Both grains keep the same change
points; the first Big Mac run measures the difference.

**Its own process.** `machine-audit scan` runs beside the collector at
background CPU and I/O priority, on its schedule and on request, so a long
walk never blocks the collector's thread. It still shares CPU, memory and
I/O: the collector's lateness and missed reads during scans are measured and
gate the schedule. Scanner CPU, peak memory and bytes written count in the
product's own cost, for interrupted runs as well as complete ones; the
scanner writes a run record at start and updates it as it goes, and the self
source reads it. The query module reads the map read-only. On Big Mac it
must not run a full scan while the prototype's scanner does: two walks put the
event service minutes behind for every program.

**Its own store, one budget.** Names are necessary here, so the map is its
own SQLite file in the private data directory, with the prototype's name
boundary: sizes without names for mail, messages, browser profiles, media
libraries and app containers (Ubuntu equivalents to be listed and reviewed);
never contents, never block addresses. The store stays out of ordinary
collector logs and exports; sending any of it off the machine is a separate,
explicit privacy decision. It counts inside the shared 10 GB budget: scanner
writes, a replacement map being built, and write-ahead-log and checkpoint
growth all reserve space through the
[shared-budget mechanism](architecture.md#failure-behavior). When an update
cannot fit, the last complete map is kept, the map is marked stale and
budget-blocked, and the collector keeps recording under the cap. Other volumes
still update; the blocked volume recovers by one full walk, which waits for
freed space if it is refused too ([recovery](source-validation.md#scanner-budget-recovery)). Change
points are retained under the agreed pruning order; no thinning of them is
defined yet, and none is applied silently.

**Cost is unmeasured in Python and gates every cadence above.** The prototype
walks 13.5 million entries with a bulk listing call in about 22 minutes at
background priority; its measured figures for the same disk are the
comparison. A plain Python walk (`os.scandir` plus `stat`) is slower per
entry by an unknown factor. Measure it during the approved
[Big Mac trial](installation.md#big-mac-first-user-trial), with the prototype paused,
against the earlier recorded measurements. Within a small multiple, Python wins on
simplicity; if not, the same bulk call through `ctypes`, and only then a
compiled scanner, with the numbers as the reason. The choice is local to the
scanner; the collector stays Python. Ubuntu's nightly walk is measured on its
target the same way.

## Activity and waiting

The disk as a performance resource: small counters, read beside memory
movement when useful, never as proof of cause. Fields keep their source
names and units under the [preservation contract](storage-approach.md#preserve-readings-derive-summaries).

| Signal | macOS | Ubuntu | Kind |
| --- | --- | --- | --- |
| Operations and request time per physical disk | IOKit `IOBlockStorageDriver` statistics: operations and total time, read and write, 5 s (the fields the prototype reads); byte and busy-time fields are candidates until verified | `/proc/diskstats`: completions, sectors, time spent per request type, weighted time, and I/Os in progress, 5 s ([iostats](https://docs.kernel.org/admin-guide/iostats.html)) | Counter snapshots; I/Os in progress is a gauge, kept as a sample |
| Waiting on I/O, machine-wide | Unavailable in the surveyed interfaces | `/proc/pressure/io`: `some` and `full` totals kept distinct, 5 s ([PSI](https://docs.kernel.org/accounting/psi.html)) | Counter, integer microseconds |
| Per workload group | Unavailable | `io.stat` (activity) and `io.pressure` (waiting), one non-overlapping level, kept apart | Counters |
| Which disk backs a volume | Mount records and IOKit parent chains, on request | `/sys/block` and the mount table, on request | Fact at discovery |

A disk that appears or disappears is unknown for that span. No routine
per-process disk-waiting source is chosen on either OS; Linux delay
accounting is an optional path with a disclosed purpose. This grows only when
an incident shows the need.

## Access and validation

The scanner runs as the user, opens files only in event-only mode to ask
where blocks lie, never reads contents, never opens cloud-only files. Full
Disk Access is meant for the scanner alone, with a disclosed purpose, and the
`storage.protected_areas` switch carries; whether macOS attaches the grant to
a Python scanner command rather than to the interpreter or launcher must be
validated before that isolation is promised. Root-helper sizing of protected
system areas is a later, separate source. No remediation path: deleting is
the person's decision.

Validate on Big Mac: full-scan and hourly-update time, CPU, bytes written and
peak memory at both grains, and collector lateness while they run; listed and
physical against measured clone folders; the gaps list against its listing. On
Ubuntu: filesystem layout and whether parts add up; nightly walk cost;
`/proc/<pid>/io` access for own and other users' processes; whether `FIEMAP`
reports shared extents, before shared blocks are counted once.
