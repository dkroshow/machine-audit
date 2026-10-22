# Privacy policy

Machine-audit inspects machines that hold private work. Permission to inspect
a machine never implies permission to send its data anywhere. This page is the
one enforceable statement of what may be recorded, what may leave the machine,
and where each rule is enforced. [Privacy audit](privacy-audit.md) holds the
evidence behind it.

## What a reading may contain

| Slot | Allowed | Enforced by |
| --- | --- | --- |
| `name`, `scope`, `component`, `source` | Constants from code | `_NAME` in `model.py`; sources are code |
| `value` | Numbers, booleans, or a state value that passes `admit_label` | `Field.__post_init__` |
| `unit`, `rate_unit` | Members of the closed `UNITS` set | `Field.__post_init__` |
| `label` | A short display name from an OS label API, through `admit_label`; otherwise `redacted` | `admit_label` |
| `entity` | `kind:id` where the id is numeric, a hash, a UUID or an interface index | Source discipline; `_NAME` bounds shape only |
| `reason` | A code: letters, digits, underscore | `_CODE` in `model.py` |

A summary row adds one derived number, `who_rank`: the largest value `who`
ranks that bundle by, computed from the bundle's own measures (`who_rank` in
`model.py`). It holds nothing new in kind and is not exported.

Never, in any slot, export, log, error, test fixture or feedback: file or
folder names, paths, command lines, arguments, environment values, host names,
IP or hardware addresses, network names, user names, service, unit, container
or cgroup names, URLs, credentials, raw exception text, or captured screens.

## Identities

Hardware identities that must stay comparable over time (the router's address,
a Linux cgroup path) are stored only as an HMAC keyed by `identity.salt`: a
random secret created once in the data directory, mode 0600, never exported.
The `device_id` in every export is a correlation identifier, never a key.
Volume UUIDs, executable UUIDs and the device id are stable fingerprints of
specific hardware; they are not content. The transfer component's
documentation will say so when that component lands in this repository.

## What stays on the machine

- File and folder names live only in `scanner.sqlite`. Export opens the
  readings store alone and never the scanner store. The named map is served
  only to authenticated loopback or Tailscale callers.
- Configured `scanner_exclusions` are never named outside `config.json`;
  coverage reports a count.
- Runtime config, the API token and the identity salt stay in the data
  directory. Nothing prints them.
- HTTP access logging is off. CLI, startup and collector failures print codes.
  Service jobs have no stdout path.

## What may leave the machine

Transfer is a verbatim carrier of admitted records; its input contract above
is the control. Feedback carries the operator's own words only; automatic
context has no schema yet, so a submitter attaches nothing. Real-data uploads
stay held until the controls above and their review are complete, and the
owner releases the hold explicitly with the operator. A device key is issued
only after the receiving party has read this page.

## Rules for a new adapter

1. Labels come only from OS label APIs (`/proc/<pid>/comm`, an executable
   basename under a system path), never from cmdline, cwd, environment, mounts
   or unit files, and always through `admit_label`.
2. Cgroup and service identities are the salted hash plus the admitted leaf
   label; the path is not stored.
3. `unit`, `rate_unit`, `reason`, `entity` and `scope` are constants or
   numeric ids in the adapter, never derived from OS strings. A new unit is a
   reviewed change to `UNITS`.
4. Interface entities are indexes. No host names, addresses, SSIDs or user
   names in any field.
5. The scanner store is the only home for names; nothing from it enters
   readings, exports or feedback. Linux default exclusions: `/proc`, `/sys`,
   `/dev`, `/run`, `/tmp` and other users' homes. The Linux watcher's dirty
   file holds volume and inode numbers only.
6. Tests and fixtures use synthetic names and paths only.

Regression tests for the unit, reason and salt rules live in
`tests/core/test_review.py` and `tests/onboarding/test_onboarding.py`.
