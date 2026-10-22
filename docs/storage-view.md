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
note, not a part. Ubuntu: `statvfs` per mount gives capacity, free and
available (free less the root reserve) per filesystem. One filesystem per
partition adds trivially; btrfs, LVM thin pools and ZFS share pools and their
snapshots sit in no folder. The Ubuntu layout is unverified; the record
carries the filesystem type, and the readout says when parts do not add up
rather than forcing them to.

**The map.** One walk of every local volume lists every folder and file:
allocated size, link count, dates, whether the file may share blocks. The
walk visits every file on every machine, because a folder's size is only
known by visiting its contents; what is *stored* is a per-machine
[grain](#grain-process-and-cost). Each folder keeps three figures:

- **Listed:** each file at its allocated size, every copy in full, as `du`
  and Finder count. Can exceed what the disk holds.
- **Uses:** each shared block once, in the folder of its oldest copy. The
  readouts lead with it, listed beside it when at least 10% and 1 GB larger.
  APFS clones, partly changed copies and hard links are the cases;
  the prototype's stream and block-map method carries, including its known
  limit: copies made since the last full scan can count shared blocks twice
  until the next one. **Unsupported on Ubuntu by default:** ext4 shares
  nothing beyond hard links, counted once by inode; btrfs and XFS reflinks
  would need the `FIEMAP` shared-extent flag, unverified. Ubuntu folders
  report listed and say uses was not computed.
- **Frees:** what deleting would give back. Never exact, never adds up.

**Reconciling the map with the volume.** Attributed uses plus a separately
labelled residual, **not attributed**, equal the volume's used space from the
same run only when the run passes the validity check: if the folders' uses
ever exceed what the volume holds, the folder figures are withheld until the
next full scan, never rescaled. On Ubuntu, listed is not a physical measure,
so used and listed are shown side by side and no residual is claimed.

**Change.** Every update saves a size point for each folder of 1 GB or more
(10^9 bytes) whose size changed since its last point, uses beside listed. The
1 GB test and the reported growth use uses where it was computed and listed
otherwise, the metric named on every point; the two are never subtracted
from each other. Lifecycle rules, so growth answers are honest:

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
| Which program is writing | `proc_pid_rusage` bytes written, own processes, 10 s process pass | `/proc/<pid>/io` `write_bytes`, own processes; other users' denied without privilege |
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
| Full scan | Weekly; also first run, a lost change log, or on request (at most daily) | Nightly: the update *is* a re-walk |
| Update | Hourly: replay the file system event log since the last bookmark, re-list only changed folders; folders with more than about 20,000 direct entries at most daily | None between walks |
| Why | The event service keeps a persistent log whether or not anyone listens | Change notifications exist only while a process listens; per-folder watches cost kernel memory, need a raised limit and lose events on overflow; mount-wide watching needs root. A nightly walk is what Linux's own `locate` does |
| History grain | Hourly points | Daily points |
| Map age shown as | "updated 14:00, full scan Sunday" | "as of 02:00 today" |

The prototype's recovery rules carry: an interrupted log read restarts from the
last event received; three hourly runs without progress force a full scan. If
the nightly walk measures too costly on Ubuntu, a resident watcher is the next
candidate, chosen on that evidence.

## Grain, process and cost

**Grain per machine.** Storing fewer rows does not make the walk cheaper; it
makes the store smaller and keeps fewer names. *Every file* (Big Mac): file
browsing, largest-file lists, "this file appeared Tuesday". *Folders, large
files and shared-block files* (default elsewhere): every folder; files above
a threshold, working figure 100 MB; every file that may share blocks or is
hard-linked, because uses needs them. Both grains keep the same change
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
budget-blocked, and the collector keeps recording under the cap. Change
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
uses against the prototype's figures; the gaps list against its listing. On
Ubuntu: filesystem layout and whether parts add up; nightly walk cost;
`/proc/<pid>/io` access for own and other users' processes; whether `FIEMAP`
reports shared extents, before uses is offered.
