# Installation and migration

The first Mac installation is in an authorized live trial. This document does
not authorize installation, service changes or access grants on another device.
Native source correctness, scanner accounting and operating cost remain trial
gates; the optional administrator helper is not implemented.
The [architecture](architecture.md) owns runtime behavior and authentication.

## Initial machines

Big Mac is the first user. The two Ubuntu installations follow using the same
product guidance, adapted and verified by the agent on each machine.

| Target | Known inventory | Evidence still needed |
| --- | --- | --- |
| Big Mac | Intel Mac Pro 2019; 384 GiB RAM; AMD Radeon Pro W5500X, 8 GB VRAM | The new Python implementation's correctness and operating cost |
| Ubuntu AWS instance | Supplied inventory: EC2 t3a.large, x86-64, AMD EPYC 7571, 2 vCPUs, 8 GiB RAM, 3 GiB swap; 107 GB EBS disk with ext4; Ubuntu 26.04 LTS, kernel 7.0.0-1009-aws; framebuffer display, no GPU reported | Current configuration, exposed sources, permissions and cost |
| Ubuntu physical workstation | Physical machine running Ubuntu; Tailscale already present | Hardware, OS/kernel, GPU/driver and available sources, discovered during implementation |

The AWS values came from user-supplied command output on October 20, 2026.
They are an example-device inventory, not a Machine-audit validation result.
Both Ubuntu machines and Big Mac already use Tailscale. Network membership does
not imply that every device may view every dashboard; access follows the owner's
Tailscale policy and the application's authentication.

All seven components are in the first Big Mac trial: CPU, memory, GPU, storage,
network, power and heat. The dashboard and agent interface must expose their
available evidence and gaps. Missing hardware or permission is reported explicitly.

## Installation contract

Use the normal source-code installation path for the trial, with an explicit
Python runtime and dependencies. Keep application files separate from private
recordings and configuration. Provide documented ways to start, check, stop and
update Machine-audit, including its collector and scanner. Background startup must
use the target OS's normal service mechanism and only be enabled with approval.

Optional elevated access has a disclosed purpose and a separate grant. Installation
must remain useful without it. Updates preserve recordings and configuration; any
format migration needs a tested recovery path. Removing the application must not
silently remove its recorded history. Exact commands follow implementation and
must be tested before being published as instructions.

### Implemented first-install flow

The CLI separates saved choices (`setup`), a read-only installation plan
(`install --json`), file installation (`install`), a protected-directory check
(`permissions`), and explicit service control (`start` / `stop`). `doctor` reports
the last permission observation and recording status. Installation creates
disabled job definitions; it does not start either process. Existing installations
are refused until the update/recovery path has been reviewed.

Setup records the GPU reader choice; starting jobs never changes it. Doctor shows
when a saved choice needs a restart. Scheduled scans preserve the last explicit
permission observation and use the saved scan options. The scanner starts on its
hourly schedule, without forcing a full walk during collector startup.

To keep folders out of the filesystem map, list them in `config.json` as
`scanner_exclusions`, for example `["/srv/private"]`. Each entry is an absolute
path, matched exactly, including letter case; the folder and everything under
it are neither opened nor named. Configuration with a relative or repeated
entry is refused. An exclusion added to an existing map takes effect at the next
full walk. Storage coverage reports how many excluded folders it met, never
which. The first scanner store open after upgrading builds the index behind that
count once, across the whole existing map.

The plan and doctor check the dashboard port. Startup failures have a fixed-size,
private status record that doctor can read even before a recording run exists;
it stores safe error codes, never exception text. Interrupted installation staging
is preserved and blocks a blind retry. Successful preparation removes its empty
staging directory.

Service control preserves these transitions. A successful start submits jobs;
`doctor` must still confirm actual collection and permissions.

| Starting state | Action | Result |
| --- | --- | --- |
| Both jobs loaded | Start | Leave them running without restarting |
| One or both absent | Start | Start only absent jobs after confirmation |
| A new start fails | Recovery | Stop and disable only jobs touched by that attempt; report recovery failures |
| Any state | Stop | Attempt both jobs, even if one fails; preserve recordings |

Python source and dashboard assets are copied from a clean, committed revision
into a versioned application directory. The private recordings directory stays
separate. No package download is needed. The CLI uses the selected Python 3.12+
runtime in isolated mode rather than whichever `python3` happens to be on PATH.

On macOS, a small signed Scanner application embeds that Python runtime and runs
only the scanner entry point. It accepts no arbitrary code or file path. It must
run under launchd; a permission check cannot fall through into scanning. Python
source updates can retain the executable identity; changing that launcher or its
runtime needs a reviewed replacement. The first Big Mac installation confirmed
Full Disk Access through this launchd identity after the user's grant.
Other installations must check it independently. A successful probe
shows access to the check directory, not proof that every folder is readable.
The implementation follows [Python's isolated initialization](https://docs.python.org/3.12/c-api/init_config.html)
and [Apple's signing guidance](https://developer.apple.com/forums/thread/663889);
neither reference establishes that this particular installation has permission.

Disposable installation checks exercised the actual source copy and installed CLI
in a home path with spaces. Custom configuration, signed-bundle relocation,
disabled jobs, staging cleanup and port-conflict diagnostics passed. They did not
exercise launchd startup, Full Disk Access or native collection.

The October 21, 2026 Big Mac trial separately exercised actual launchd startup,
the protected-directory check, authenticated Tailscale access, a clean restart
and source replacement with the scanner executable unchanged. Prior recordings
and the predecessor installation were preserved. These checks do not establish
complete filesystem access, correct folder totals or acceptable monitoring cost.
The scanner and full process workload require their own native measurements.

## Big Mac first-user trial

1. Prepare the complete installable product and verify it against disposable data.
   Record the existing recorder's configuration and how to resume its services.
2. At the approved switch, pause the existing recorder and scanner, including any
   helper reads that would compete or interfere. Confirm they have stopped before
   starting Machine-audit; GPU reads can reset counters even when they look read-only.
3. Run Machine-audit with its own data directory. Keep the existing recorder's installation,
   settings and history intact; historical-data import is outside this first trial.
4. Verify every component, live and historical views, agent queries, dashboard access,
   and the [budget and failure behavior](architecture.md#failure-behavior). Measure
   collector and scanner cost under real workloads; retain the results and gaps.
5. If rollback is needed, stop Machine-audit's collector, scanner and optional helper,
   restore the prior dashboard route if changed, and resume the prior recorder's
   services. Preserve Machine-audit's trial data for investigation.

Keep notes outside the repository: record which services you paused, the versions
and data locations, the switch times and the verified rollback steps. Stop only
the monitoring services involved in the approved switch, never your workloads.
Readable old history remains in the predecessor's own store.

## Dashboard access

Assume Tailscale for the initial machines. The application remains bound to
loopback; [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)
provides a private HTTPS address for the dashboard. The installer configures the
one permitted hostname, verifies the intended viewers' access, and returns the
browser URL through the authenticated opening flow in the
[architecture](architecture.md#permissions-private-data-and-dashboard-access).
Keep the per-run application token requirement through the proxy; do not place
credentials in logs, ordinary query strings or shared setup notes.

Preserve existing Tailscale routes and policies; an agent must not reset unrelated
services or broaden access to make a connection work. Validate Host/Origin checks,
permitted and denied requests, and reopening after a restart. Public exposure via
Funnel is outside this rollout. The [Cloudflare log and feedback path](storage-approach.md#remote-retrieval)
serves recorded-data transfer and remains separate from live dashboard access.
