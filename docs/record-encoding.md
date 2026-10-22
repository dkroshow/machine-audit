# Change-only record encoding (experimental)

Status: not built; see the decision below. Nothing in the code uses this design.

## Format

A raw readings row holds a **keyframe**, today's full `MR1` record, or a
**delta** (`MD1`) holding only what changed since the same source's previous
pass in the same run:

- the full record header: source, clocks, status, identity and sequence;
- field order, as runs copied from the previous pass plus new fields in full.
  Fields are keyed by name and entity. Process starts and exits do not force
  a keyframe, because a busy Mac has them in most passes;
- for each kept field, its new value, or the whole field when its status,
  label or description changed;
- per-field read clocks, which change every pass, as runs of identical clock
  pairs, each a difference from the previous clock.

A source writes a keyframe on its first pass in a run, its first pass in each
wall-clock minute, and whenever a delta would not be smaller. Raw pruning
removes whole minutes, so it never strands a delta.

The magic bytes are the format flag: old `MR1` stores read unchanged.
A `Store(delta_records=True)` flag, off by default, would turn it on.

## Decoding

One store function decodes readings rows for query, derive, summaries and
export. It rebuilds the exact Record: packing it again gives the original
`MR1` bytes. A delta's base has the same run and source and an earlier
sequence. If the base is missing, the reader gets an explicit gap, never a
guess: the same header, no fields, status `pruned`, reason
`delta_base_missing`.

## State, recovery and export

The previous pass is the committed `baselines` row, written in the same
transaction, or the previous record in the batch. A new run starts with
keyframes; a rolled-back write leaves no state. Unsaved records, `baselines`
and `incidents` stay full. Export chunks hold ordinary Record JSON, so export
decodes each delta, reading its keyframe even from before the chunk.

## Measured size

Two minutes of 2,000-process passes through the real macOS adapter.
"Scattered" changes 25% of processes and replaces 5 per pass.

| Fixture | Store growth | Raw before | Raw after |
| --- | ---: | ---: | ---: |
| `test_scale` (all change) | 10.8 MB/min | 1.47 MB/min | 0.34 MB/min |
| Scattered | 14.3 MB/min | 2.84 MB/min | 0.74 MB/min |

Raw falls 74–77%, but raw is 14–20% of growth: total falls about 10–15%.
Five-minute keyframes would cut raw 86–89% if pruning removed five-minute
windows. A pass costs about 0.3 s CPU to encode and 0.2–0.5 s to decode.

## Decision

A scratch prototype rebuilt every pass exactly. It cut raw readings by
74–77%, but raw readings were only 14–20% of store growth, so total growth
would fall about 10–15%. That gain does not yet justify a new storage format
plus the encode and decode cost, so the owner stopped the work at this note
(2026-10-22). Revisit after the top-rows summary change lands and growth is
measured again: raw readings will then be a larger share of growth.
