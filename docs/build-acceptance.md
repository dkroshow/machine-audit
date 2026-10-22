# First build acceptance

Build the complete product for its first installation on Big Mac. CPU, memory,
GPU, storage, network, power and heat are in scope. The reviewed component,
[storage](storage-approach.md) and [query](query-design.md) contracts govern
implementation. Unavailable hardware or permissions remain visible gaps.

## Five priorities

1. **Prove what monitoring costs.** The readily available Monitoring cost view
   shows live and historical CPU, memory, storage growth, disk I/O and network
   where measurable. Include collector, helpers, scanner, queries and transfers;
   avoid double counting and name missing coverage. Compare recorded values with
   independent OS observations on the same intervals. Include the cost of
   measuring ourselves. This evidence lets users judge whether the benefit
   outweighs the cost; measuring cost alone does not establish that benefit.
2. **Folder sums reconcile with known totals.** Check child-to-parent accounting
   and volume reconciliation, with shared blocks counted once where supported.
   The [storage view](storage-view.md) defines listed versus uses and the named
   residual. Report gaps and invalid reconciliation; never adjust folder sizes
   to manufacture agreement. Test shared files, excluded areas and incomplete
   scans before comparing the first real Big Mac map with its volume totals.
   Growth keeps each tracked folder's original change, including small and zero
   changes. Parent totals include subfolders; never sum them with their children.
3. **Quick, clear installation.** A concise CLI onboarding flow checks the runtime,
   explains permission choices, records decisions and confirms resulting access.
   Say what works, what is missing and the next action. Keep permission-bearing
   identities stable where possible; do not promise that Python or a wrapper
   inherits existing grants. Exercise the flow in a disposable install location
   before the real installation. Users enter passwords and grant OS access
   themselves; normal updates should avoid repeating those steps where possible.
4. **Test logs and feedback end to end.** Supply a repeatable test that uploads
   a scrubbed sample, submits feedback, confirms receipt, and fetches the records
   on Big Mac into a separate per-device replica. Check retry and duplicate
   handling, missing ranges and read-back content. Coordinate the real format
   with the Cloudflare owner; do not assume arbitrary SQLite row cursors are safe.
   Scanner names remain excluded. Distinguish local tests from a real Worker
   round trip; record transfer cost and never commit credentials.
5. **Big Mac is the first installation.** Prepare the concrete install and rollback
   steps, then use the [migration procedure](installation.md#big-mac-first-user-trial)
   at the authorized switch. Preserve the existing recorder and its history. Verify every
   component, permissions, totals, monitoring cost, historical queries, dashboard
   and Tailscale access under real workloads. Ubuntu validation follows on the
   actual target machines.

## Evidence before and during the trial

Use a small set of checks that catch real failures: counter resets and identity
changes, interval and summary accuracy, snapshot coherence, privacy and access,
budget reservations, pruning, restart and save failure. Destructive paths run
only on disposable data. No forced overload, disk exhaustion or restart of the
user's workloads is needed.

Replay scenarios develop alongside implementation: CPU contention with a busy
but unconstrained comparison, memory strain and recovery, and folder growth.
Scrubbed incident records and synthetic scenarios retain their provenance. Ask
ordinary performance questions through the real query path and inspect the
dashboard; do not create a separate answer engine for tests. Stored-record
replay proves query behavior, not collection correctness on a real OS.

An independent code review checks the integrated candidate against these goals,
the component contracts, correctness, simplicity, privacy and measured cost.
Record measured results, limitations and untested cases. Compilation, mock
responses and passing portable tests do not establish platform support.

## Current implementation gate

Synthetic checks and independent review verified the corrected process saves,
interval queries, scanner scale, shutdown and shared storage-cap enforcement.
The [development guide](development.md) and [source validation](source-validation.md)
record measurements and remaining limits.

The authorized Big Mac trial has verified installation, launchd startup,
Full Disk Access through the scanner identity, authenticated Tailscale access
and controlled source replacement. Native cost, query latency, folder accounting,
usable storage headroom and retention still need acceptance before unattended
operation. Ordinary shutdown under a long final flush remains a known limit;
controlled replacements have verified clean stop markers. Buffer suspension
policy remains provisional; the real Cloudflare round trip remains a separate
dependency. Keep the accepted
10 GB cap, cadences, original readings and folder-accounting rules while
measuring these limits; reducing evidence requires a separate decision.
