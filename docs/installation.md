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
| Ubuntu cloud instance | Ubuntu cloud instance, 2 vCPU class, no GPU | Current configuration, exposed sources, permissions and cost |
| Ubuntu physical workstation | Physical machine running Ubuntu | Hardware, OS/kernel, GPU/driver and available sources, discovered during implementation |

The Ubuntu rows describe planned target classes, not validated installations.
Network membership does not imply that every device may view every dashboard;
access follows the user's network policy and the application's authentication.

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

### Linux (Ubuntu) install

The collector and scanner run as a dedicated system user under system units;
no user unit or linger is needed. Not yet validated on a target.

1. As an administrator, create the user, with its home under `/var/lib`:
   `sudo useradd --system --create-home --home-dir /var/lib/machine-audit --shell /usr/sbin/nologin machine-audit`.
2. Clone the source as that user, so Git trusts the checkout:
   `sudo -u machine-audit -H git clone <repository> /var/lib/machine-audit/source`.
3. From that checkout, run `sudo -u machine-audit -H python3.12 -m machine_audit setup`.
   Linux setup asks only about the helper and dashboard hosts, and maps folders
   and large files (`scanner_grain: folders`).
4. Add `scanner_exclusions` to that user's
   `~/.local/share/machine-audit/config.json` if needed. `/proc`, `/sys`, `/dev`,
   `/run`, `/tmp` and other users' homes are never entered.
5. Run `sudo -u machine-audit -H python3.12 -m machine_audit install`. It
   writes a release, a CLI (`~/.local/bin/machine-audit`) and three unit files,
   then prints the two root commands that place them in `/etc/systemd/system`.
6. Start with `sudo /var/lib/machine-audit/.local/bin/machine-audit start`,
   which runs `systemctl enable --now machine-audit.service machine-audit-scan.timer`.
   Check with `sudo -u machine-audit -H /var/lib/machine-audit/.local/bin/machine-audit doctor`.

`machine-audit.service` runs the collector; `machine-audit-scan.timer` runs
`machine-audit-scan.service` hourly, and a run between 01:00 and 05:00 walks in
full once a day. Every unit has `User=`, `ProtectSystem=strict`,
`ReadWritePaths=` the data directory only, `NoNewPrivileges=yes`,
`PrivateTmp=yes`, `UMask=0077`, `MemoryMax=1G` and `OOMPolicy=stop`; the
collector has `CPUWeight=20`, the scanner `Nice=10`, `IOSchedulingClass=idle`
and `CPUWeight=10`. Python runs with `-B`: the release is read-only to the
units, and `-I` ignores `PYTHONDONTWRITEBYTECODE`. `stop` disables and stops
all three and preserves recordings.

`MemoryMax=1G`, from measurements. The scanner: about 30 MB of interpreter,
a 16 MiB block-map cap (empty on ext4), a 2 MiB write buffer, and a working
set the first Ubuntu walk measured at 60.7 MB peak for 613,000 entries
(about 50 bytes an entry beyond the interpreter; a clean 13.6 M-entry Mac
walk used 462 MB, 34 bytes an entry). 1 GB covers about 19 M entries. The
collector: 137 MB measured on 2 vCPUs with 280 processes; its unsaved
buffer stops at 64 MiB, and Linux passes carry far fewer fields than the
Mac's 2,000-process passes. An OOM kill stops the unit with result
`oom-kill`: the scanner's run record then reads `interrupted`, and the
collector restarts with its unsaved span shown as a gap.
Without root, `start` and `stop` print the exact `systemctl` commands.

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
5. If rollback is needed, stop Machine-audit's collector and scanner,
   restore the prior dashboard route if changed, and resume the prior recorder's
   services. Preserve Machine-audit's trial data for investigation.

Keep notes outside the repository: record which services you paused, the versions
and data locations, the switch times and the verified rollback steps. Stop only
the monitoring services involved in the approved switch, never your workloads.
Readable old history remains in the predecessor's own store.

## Dashboard access

The dashboard binds to loopback (`127.0.0.1:8765` by default) and, by default,
admits only loopback hosts. You can front it with a proxy of your choice and
verify viewers' access yourself. List each exact `host` or `host:port` the
browser will use in `allowed_hosts` (setup asks for them): a
[Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve) name,
a reverse-proxy virtual host, or an ssh tunnel's local `localhost:port`. Each
entry is lower-case, without a scheme or path; both its `https://` and `http://`
Origins are admitted. Setup does not configure the proxy. An older
`tailscale_host` setting is read as a one-entry list and saved in the new form
at the next setup. See the
[architecture](architecture.md#permissions-private-data-and-dashboard-access).
Keep the per-run application token requirement through the proxy; do not place
credentials in logs, ordinary query strings or shared setup notes.

Preserve existing Tailscale routes and policies; an agent must not reset unrelated
services or broaden access to make a connection work. Validate Host/Origin checks,
permitted and denied requests, and reopening after a restart. Public exposure via
Funnel is outside this rollout. The [Cloudflare log and feedback path](storage-approach.md#remote-retrieval)
serves recorded-data transfer and remains separate from live dashboard access.

## Uninstall

There is no uninstall command. Stop the two launch agents, then remove the CLI,
application files and job definitions:

```sh
machine-audit stop
rm ~/.local/bin/machine-audit
rm -r ~/Library/Application\ Support/machine-audit
rm ~/Library/LaunchAgents/local.machine-audit.collector.plist
rm ~/Library/LaunchAgents/local.machine-audit.scanner.plist
```

Keep `~/.local/share/machine-audit` unless you choose to delete it separately.
It contains recordings, config, the API token and identity salt. If you granted
Full Disk Access, you can remove the "Machine-audit Scanner" entry in System
Settings.
