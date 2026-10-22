# Machine-audit: guide for coding agents

Before committing, run:

```sh
python3.12 scripts/check.py
```

This requires Python 3.12+ and Node 22.13+. It runs the suites sequentially
and stops on failure. Check reported skips before claiming coverage.
While editing, run the affected suite, for example:

```sh
python3.12 -m unittest discover -s tests/core -v
```

See [development](docs/development.md) for verification details and
[installation](docs/installation.md) for setup. Installation needs Xcode
Command Line Tools to compile the native scanner.

## Layout

- `machine_audit/`: collector, store, queries, CLI and HTTP server.
- `machine_audit/sources/`: OS adapters.
- `machine_audit/scanner/` and `native/scanner/`: filesystem map and native launcher.
- `dashboard/`: plain HTML and JavaScript; Node tests in `dashboard/test/`.
- `replay/`: recorded-input replay and browser verification.
- `tests/`: Python test suites with synthetic inputs.
- `docs/`: product, architecture, privacy and development documentation.

## Conventions

Runtime code has zero external dependencies: use the Python standard library
and platform interfaces. macOS is supported today; Linux sources and scanner
are built but not yet validated on a target ([Linux adapter](docs/linux-adapter.md)).
Errors print codes, never exception text.

[Privacy rules](docs/privacy.md) bind every new field and adapter. Read them
before changing collection, storage, queries or exports. The scanner's private
name store has a separate boundary from recorded readings.

## Never

- Put file names, paths, host names, user names, addresses, network names or
  real data in fields, logs, errors, tests or fixtures. Use synthetic test
  inputs only; keep scanner names within the privacy policy's private store.
- Add a network target without a README "Network activity" disclosure and
  an off switch.
- Commit secrets or local configuration.
- Weaken or delete tests to get a passing result.
- Claim checks passed without running them; report skips and unrun checks.
- Expand the task into unrelated cleanup or features.
- Add AI attribution to commits: no Co-authored-by, Generated-with or similar
  trailers.
