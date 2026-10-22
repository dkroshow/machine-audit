# Replay and dashboard checks

Replay records enter the same `Store.append`, `Query` and HTTP paths as normal
collection. The dashboard contains no second rate or performance-answer engine.
It displays the query's values, units, covered windows, source scopes and gaps.

## Run the checks

Use Python 3.12 or later and Node 22 or later. No dependency install is needed.
From the repository root:

```sh
python3.12 -m unittest replay.test_replay replay.test_storage_replay -v
node --test dashboard/test/model.test.mjs
MACHINE_AUDIT_PYTHON=python3.12 node --test dashboard/test/render.test.mjs
```

The render check requires Google Chrome on macOS. It starts its own loopback
server on an unused port and a fresh headless browser profile, then stops only
those processes. It checks desktop and mobile layouts, all ten views, live
cache readings, peak inspection, token removal and missing values. Chrome
absence is reported as a skip, never a rendering pass.

`MACHINE_AUDIT_TEST_ROOT` can name a private scratch directory. Otherwise the
checks use `replay/.scratch/`. Disposable databases, files and browser profiles
stay there; no live recorder, service, protected folder or system permission is
used. `MACHINE_AUDIT_SCREENSHOTS` can name an existing scratch directory for
representative PNGs from the render check. Screenshots contain synthetic data.

## Scenarios and independent expectations

All current records in `replay/scenarios.py` are **synthetic**, with invented
names and values. They are scenario inputs, not captured macOS output.
Expected facts are stated here and in the test, independently of `derive`.
CPU input uses the original per-CPU tick fields and scaled runnable gauge,
not a prefilled CPU total. The stored 24,000 busy ticks remain ticks; the shared
derivation reports a four-CPU rate. A regression also rejects missing bins that
overlap those measured counter windows.

| Scenario | Inputs | Required answer |
| --- | --- | --- |
| Busy, unconstrained comparison | Eight usable CPUs; four CPU-seconds per second; four runnable tasks | 240 CPU-seconds in one minute, with demand below capacity |
| Contention evidence | The same CPU work, but 12 runnable tasks from the twentieth second | The same 240 CPU-seconds, with separately visible demand above capacity; no invented waiting time |
| Concurrent program instances | Two instances each use one CPU for the same minute | One compatible program group, 120 CPU-seconds over 60 elapsed seconds, two-CPU mean and peak |
| Memory strain and recovery | Available memory falls to 500 MB and returns to 7 GB; 80 swap-in pages; 2,000,000 microseconds of stall time | Movement remains in pages, stalls in source microseconds, and recovery does not erase the earlier activity |
| Overlapping memory | Two three-GB footprints with the same program name | Two instance rows; no invented six-GB physical total or host remainder |
| Shared storage and growth | Two four-GB files share two GB; one nested folder gains one GB | Listed 8 → 9 GB, uses 6 → 7 GB, a 4 GB residual, and original growth retained for both parent and child, with nested overlap explicit |
| Original folder growth | A tracked folder gains 50 MB; parents gain 10 GB while children gain or lose 1 GB; another parent stays unchanged while its child loses 1 GB | All changes remain visible, including 50 MB, zero and negative values; original endpoint sizes and actual observation times remain visible |
| Historical folder placement and missing evidence | A child moves after the selected interval; another tracked folder is absent at its end | The earlier interval shows the earlier parent. The absent folder's size and growth remain unknown, not zero |
| Different endpoint measures | A separate volume records 4 GB of Uses, then 6 GB of Listed bytes | Each endpoint retains its own measure; growth stays unknown rather than subtracting incompatible sizes |
| Invalid storage accounting | Attributed use exceeds the measured volume total, or physical accounting is unsupported | Uses and residual withheld; listed bytes remain visible |

Storage values are numeric fixture metadata; no gigabyte files are allocated.
`replay/storage_scenario.py` enters through `MapStore` and the same storage query
adapter used by the running application. Synthetic block coordinates are never
read from a device.

The multi-component preview also includes separate graphics activity and memory,
router and internet probes, interface traffic, power-source domains, temperature,
cooling and monitor cost. Unavailable wall power, GPU waiting and disabled
capacity testing stay explicit. These records exercise presentation and query
semantics; they do not validate an OS source.

## Evidence boundaries

- **Parser/source tests** belong with the sources. Real captures must retain their
  source and units, be reviewed for privacy, and be explicitly identified.
- **Stored replay** proves ingestion, retained queries, grouping and presentation
  against known inputs. It does not prove a real machine exposes these fields.
- **Actual OS evidence** comes from the authorized Big Mac trial, with source
  access, measured overhead and comparable observation windows recorded there.
  No real Big Mac incident fixture is included in this replay set.

The browser draws rates already derived by the API, while details retain raw-unit
counter totals. Source names and units remain visible in each evidence disclosure.
The display never turns a denied, unavailable, pruned or unobserved value into
zero. Genuine measured zeros are drawn and labelled.

Storage pages use the scanner's opaque cursors. The general browser check uses
a 201-folder synthetic map for child continuation. The focused original-growth
check uses nine tracked folders and requests two rows per page through the real
HTTP route. It checks every row, previous-page navigation, endpoint formatting,
historical parent labels, missing values, and the visible “Includes subfolders”
notice. It does not substitute an answer engine or fetch the entire map.

Growth retains every tracked folder's original totals, including zero and small
changes. Parent totals include child totals: a parent that grows 10 GB with a
child that grows 1 GB still grew 10 GB, not 11 GB. Nested rows or pages must not
be added. The parent column uses the saved hierarchy at or before the interval
end and shows its observation time. Unknown placement and withheld names stay
explicit. Endpoint observations can be older than the requested bounds or the
placement observation; the table shows each time separately.
Each endpoint also shows its own measure. A change from Uses to Listed bytes
does not establish growth, even though both sizes are in bytes.

To run only this browser regression:

```sh
MACHINE_AUDIT_PYTHON=python3.12 node --test --test-name-pattern 'original folder growth' dashboard/test/render.test.mjs
```

The scanner owns the primary calculation tests. The focused browser regression
checks the user-visible result, including legitimate changes that suppression
would hide. Both use synthetic input metadata; no large files are allocated.

The crowded-dashboard browser case adds 620 synthetic CPU fields to exceed the
generic 600-row response limit while memory still has recorded fixture readings.
Overview requests each component separately, with 75 rows each and at most two
requests in flight. A partial component sample stays visible as partial; it does
not imply missing measurements in another component. The same case includes a
long synthetic build identity and checks cost-view containment on desktop and
mobile. Run it with `--test-name-pattern 'overview coverage'`, or preview it with
`--dashboard-state crowded`. No real machine identifiers are in the fixture.

## A local representative preview

For a manual check, start the synthetic preview from this checkout:

```sh
python3.12 -m replay.preview --ready-file replay/.scratch/preview.json
```

The preview writes its loopback origin and temporary token to that private file.
Open `<origin>/#token=<token>` locally. The page removes the token from the URL
and keeps it in memory; refreshing requires a fresh authenticated opening. This
is the same page and header authentication used behind a configured Tailscale
origin. The preview does not configure Tailscale or start collection sources.

Stop it with Ctrl-C. Use a new ready-file name for another run. The preview's
measurements are explicitly labelled synthetic in the page.
Use `--storage-state paged` for the multi-page map, or `invalid` and
`unsupported` for the reconciliation cases. Use `--storage-state growth` for
the original-growth examples. Its private ready file includes `storage_interval`
in Unix seconds; select those bounds to inspect the child before its later move.
The same fixture's “Uses to Listed” volume demonstrates incompatible endpoint
measures and an unknown growth value.

## Interface lineage

The implementation inspected the dashboards of an earlier private prototype.
The useful patterns carried forward are its persistent navigation, dedicated
resource views, shared intervals, source context, gap-aware charts, workload
peaks and storage drilldown. Machine-audit uses its own shared query API, not the
prototype's polling server or its health grades.
