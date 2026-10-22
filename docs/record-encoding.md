# Change-only record encoding (experimental)

Status: proposal. Off by default; the default write path is unchanged.

## Format

A raw readings row holds either a **keyframe** or a **delta**. A keyframe is
today's full `MR1` record. A delta (`MD1`) stores only what changed since the
previous pass of the same source in the same run:

- the full record header: source, component, read clocks, status, reason,
  version, run identity, sequence and receipt times;
- the field order, as runs copied from the previous pass plus new fields in
  full. Fields are keyed by name and entity. A started process adds fields;
  an exited process is not copied. Neither forces a keyframe, because a busy
  Mac starts or exits processes in most passes;
- for each kept field, its new value, or the whole field when its status,
  label or description changed;
- per-field read clocks, which change every pass. They are stored as runs of
  identical clock pairs, each as a difference from the previous clock.

A source writes a keyframe on its first pass in a run, on its first pass in
each wall-clock minute, and whenever a delta would not be smaller. Raw pruning
removes whole minutes, so it never strands a delta behind a removed keyframe.

The magic bytes are the format flag. `MR1` rows from old stores read
unchanged. `Store(delta_records=True)` turns the experiment on.

## Decoding

One store function decodes readings rows for every reader: query, derive,
summaries and export. It rebuilds the exact Record; packing it again gives the
original `MR1` bytes. A delta's base is the same run and source, at an earlier
sequence. If the base row is missing, the reader gets an explicit gap: the
same header, no fields, status `pruned`, reason `delta_base_missing`. It never
guesses.

## State, recovery and export

The previous pass is the store's committed `baselines` row, written in the
same transaction, or the previous record in the same batch. It belongs to one
run, so a new run starts with keyframes. A rolled-back write leaves no state
behind. The collector's unsaved records, `baselines` and `incidents` stay full.

Export chunks already hold ordinary Record JSON. Export decodes each delta,
reading its keyframe from the store even when that keyframe precedes the
chunk, so a chunk never contains a delta.

## Measured size

Two minutes of 2,000-process, 10 s passes through the real macOS adapter.
"Scattered" changes 25% of processes per pass and replaces 5 per pass.

| Fixture | Store growth | Raw before | Raw after |
| --- | ---: | ---: | ---: |
| Every process changes (`test_scale`) | 10.8 MB/min | 1.47 MB/min | 0.34 MB/min |
| Scattered | 14.3 MB/min | 2.84 MB/min | 0.74 MB/min |

Raw falls 74–77%, but raw is 14–20% of store growth, so total growth falls
about 10–15%. Summaries make up most of the rest. Five-minute keyframes would
cut raw by 86–89%, if pruning removed five-minute windows. Encoding costs
about 0.3 s CPU per pass; decoding a pass costs 0.2–0.5 s.
