# Machine-audit

Machine-audit is a monitoring framework that gives humans and agents a shared
view of a machine's health and performance. It helps them understand resource
strain, investigate its causes, and judge whether changes improve performance,
while making its own operating cost visible. Humans use a dashboard; agents
use a query interface backed by the same evidence.

Built for my own workflow. One maintainer, best effort.
Developed privately since 2026.

The first Mac, nicknamed Big Mac, is in an installation trial. Native collection,
permissions, operating cost and folder accounting are still being validated;
this is not yet a validated release.

Linux is not implemented yet. It is being built with the first external user.

Issues and pull requests are welcome.

MIT. See [LICENSE](LICENSE).

## Install and use

Requirements: macOS only today; Python 3.12+; Xcode Command Line Tools; a git
checkout. The installer compiles the native scanner with `/usr/bin/clang`.
It copies a clean, committed revision and refuses uncommitted changes under
`machine_audit/`, `dashboard/`, `native/` or `pyproject.toml`.

```sh
git clone https://github.com/dkroshow/machine-audit.git
cd machine-audit
python3.12 -m machine_audit setup      # interactive; saves ~/.local/share/machine-audit/config.json
python3.12 -m machine_audit install    # interactive; shows its plan, then installs
```

Installation puts the CLI at `~/.local/bin/machine-audit`; make sure
`~/.local/bin` is on your PATH. Application files go under
`~/Library/Application Support/machine-audit`, with two disabled launch agents
in `~/Library/LaunchAgents`. Installation starts nothing.
`python3.12 -m machine_audit install --json` prints the plan without installing.

```sh
machine-audit permissions              # optional: Full Disk Access check for the scanner
machine-audit start                    # asks before starting the two launch agents
machine-audit doctor
machine-audit open                     # dashboard in the browser
machine-audit now                      # current readings as JSON
machine-audit stop
```

`machine-audit doctor` on the trial Mac prints:

```
Python 3.12.12: ready
Collector: connected; data directory: private
Protected disk areas: granted (requested)
  Last scanner check only; individual denied folders remain visible in the map.
System process and power detail: not_enabled (optional, not requested)
Next: The collector has not confirmed any successful source readings; inspect machine-audit status.
```

Other query commands are `catalog`, `status`, `series`, `who`, `coverage`,
`around` and `storage`. See the [API reference](docs/api.md) and
[installation guide](docs/installation.md).

State on disk: recordings and config live in `~/.local/share/machine-audit`.
Setup states a 10 GB storage limit, with a warning from 5 GB. The dashboard
server binds to `127.0.0.1:8765` by default with a per-run token. You configure
any proxy yourself; Tailscale Serve is one option. Setup's optional Tailscale
hostname only adds that hostname to the allowed Host/Origin values.

## Network activity

By default, `macos.probes` sends an ICMP ping every 5 seconds to the default
router and to one public address, alternating `1.1.1.1` and `8.8.8.8`.
Every 60 seconds, `macos.dns_connect` performs a DNS lookup of `www.apple.com`
and a TCP connect to it on port 443, with no application data sent.
`macos.portal` makes an HTTP GET of
`http://captive.apple.com/hotspot-detect.html` about 5 seconds after startup or
a network path change, and after a new internet dropout, at most once every
5 minutes.

The optional speed test is off by default. It downloads and uploads synthetic
data over HTTPS to `speed.cloudflare.com`, at most hourly, only when
`enabled.network_speed` is true and the daily budget
`source_options.network_speed_daily_bytes` (default 0) still has room for one
run of `source_options.network_speed_run_bytes` (default 10 MB). There is no
telemetry or upload of recordings; nothing is sent anywhere else. The log
upload and feedback component described in the product documents is being
built on a separate branch and is not in this repository.

To turn a source off, set it to false under `enabled` in
`~/.local/share/machine-audit/config.json`. For example, merge these settings
into the existing config to disable the default probes:

```json
{"enabled": {"macos.probes": false, "macos.dns_connect": false, "macos.portal": false}}
```

Then restart with `machine-audit stop` and `machine-audit start`. To disable the
optional speed test, set `enabled.network_speed` to false.

## Product documents

- [Vision](docs/vision.md): purpose, ambitions and principles.
- [Product brief](docs/product-brief.md): users, questions, platforms and boundaries.
- [Performance components](docs/machine-information.md): resource evidence and attribution.
- [Storage view](docs/storage-view.md): filesystem map, disk activity and waiting.
- [Storage approach](docs/storage-approach.md): history, costs and retention.
- [Architecture](docs/architecture.md): collection, storage, queries and interfaces.
- [Query design](docs/query-design.md): answer shapes, resolution and coverage.
- [Privacy](docs/privacy.md): permitted readings, local data and adapter rules.
- [Installation and migration](docs/installation.md): trial, access and rollback.
- [First build acceptance](docs/build-acceptance.md): cost, accounting and trial checks.
- [Development](docs/development.md): build and verification commands.
- [Ubuntu handoff](docs/ubuntu-handoff.md): planned platform work and checks.
