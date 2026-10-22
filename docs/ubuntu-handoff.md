# Ubuntu implementation handoff

This is source for a coding agent to adapt on the target machine. **It is not
an Ubuntu installer.** Linux collection and service installation are not
implemented. The Mac build is in its first live trial; a successful Mac scan
does not establish Ubuntu correctness.

**Current handoff: preparation only.** Read the code and inspect the target's
inventory without starting collection or a filesystem scan. Return a short plan
and blocking questions, then wait for the operator's explicit go before editing,
installing or changing services. Before implementation, update to the final
source revision and review the completed Big Mac findings.

Read [privacy](privacy.md) first; its adapter rules are part of the result.

## Result to deliver

Adapt for the AWS instance and physical workstation, both on Tailscale. Cover
all seven components with readings or explicit hardware/access gaps. Keep the
shared dashboard and agent queries, visible monitoring cost, defensible folder
totals and short onboarding. Work on one actual machine first.

Read [installation](installation.md#initial-machines) for supplied inventory,
[Performance Components](machine-information.md) for meaning, and the component
views and their research for tentative Linux sources. Recheck those sources
against the installed kernel and drivers; do not invent parity for absent hardware.

## Reuse and adapt

| Area | Starting point and required work |
| --- | --- |
| Records and queries | Reuse the model, collector, store, budget and query modules and [API contract](api.md). Inspect `derive.py` and query defaults: CPU constituents and memory signals reflect Mac sources. Preserve units, identity, clocks, status and coverage; RSS is not physical footprint, and contention signals differ. |
| Collection | `sources/__init__.py` rejects Linux boot identity and supplies unavailable sources. Implement Linux clocks/adapters using accepted [cadences](storage-approach.md). Adapt the bundled process pass and self-cost mapping, which currently depend on Mac fields. |
| Filesystem map | Reuse the map store, queries and accounting. Replace the Darwin/FSEvents boundary in `scanner/runner.py` with Linux discovery, permissions and nightly walking. Start with listed bytes and explicit physical-accounting limits. Add `/proc`, `/sys`, `/dev`, `/run` and other users' homes as default scanner exclusions. |
| Installation | `installation.py` and permission handling are Mac-specific. Add Linux setup/doctor and explicit service control, normally systemd for the intended user. Use stable Python 3.12+ and separate source/configuration/recordings. The Mac installer cannot install Linux. |
| Dashboard and access | Reuse dashboard/API/CLI; check their default signals against Linux answers. Keep loopback binding, authentication and Host/Origin checks behind Tailscale. Preserve existing routes. |
| Logs and feedback | Being built separately; absent here. Preserve the export contract and coordinate integration before promising remote records on Big Mac. Do not build a second uploader. |

## Verification on each machine

Use [first build acceptance](build-acceptance.md), not a new test framework:

- Exercise install, start, doctor, dashboard/CLI, stop and restart in the actual
  user/service context. Preserve recordings.
- Compare source readings with independent OS observations over matching
  intervals. Check resets, process identity and denied/missing hardware.
- Follow readings through history and rollups to shared dashboard/agent answers.
  Keep missing data distinct from zero after restart and pruning.
- Verify folder-to-parent and volume totals on real filesystems. A labelled
  residual is not proof that shared-block accounting is correct.
- Measure collector/scanner/query CPU, memory, writes, growth and missed reads.
  Measure useful retention under the shared 10 GB cap and 5 GB warning.

Run focused checks, then `python3.12 scripts/check.py` on the candidate. Native
tests and Chrome discovery include Mac assumptions; adapt those boundaries
without weakening existing checks. Record skips and verify real hardware. The
[development guide](development.md) records Mac evidence and unresolved cost,
retention, buffering and shutdown limits; do not inherit a readiness claim.

## Brief to give the implementing agent

> Adapt this source snapshot for this Ubuntu machine. Read README and
> docs/ubuntu-handoff.md first. Reuse the core, storage, queries and dashboard;
> implement the missing Linux collection, scanner and installation boundaries.
> Preserve the Mac implementation and the accepted evidence/privacy/budget
> contracts. Discover this machine's actual kernel, filesystem and GPU. Keep all
> seven components visible, with honest gaps. Measure the product's own cost.
> Work in a private checkout and show a concise install/rollback plan before
> changing services or permissions. This document grants no deployment, upload,
> deletion, package installation or paid-service authority. Report what works on
> this machine, what remains unavailable, and exact verification evidence.

If received as a source archive, retain its revision/checksum receipt and start
a new private Git repository before changing it. The source installer expects
a Git revision; the archive deliberately contains no Git history or machine data.
