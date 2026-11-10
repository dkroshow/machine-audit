"""Physical space from shared blocks, worked out in memory for one run.

Block ranges exist only while a run computes pieces from them; physical
addresses are never saved. What the map keeps is which streams share how many
bytes, as per-folder byte terms."""
from __future__ import annotations
from array import array
from collections import defaultdict
from .errors import ScanError

GB = 10**9
MAX_ANCESTRY = 1024


def pieces(extents, allocated, *, max_ranges=16_000_000):
    """Split the bytes held by `allocated` streams into pieces, each held by one set of streams.

    `extents` maps some streams to their (start, length) byte ranges. A stream
    without ranges, or with fewer range bytes than its allocation (a resource
    fork, say), holds the rest privately. Returns ({frozenset(streams): bytes},
    {stream: group}); streams that share any byte form one group, keyed by its
    least stream key."""
    order = list(allocated)
    ordinal = {s: i for i, s in enumerate(order)}
    ranges = array('q')
    covered = [0] * len(order)
    for stream, found in extents.items():
        i = ordinal[stream]
        merged = []
        for start, length in sorted(found):
            if start < 0 or length <= 0:
                raise ValueError('invalid_extent')
            end = start + length
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        if len(ranges) // 3 + len(merged) > max_ranges:
            raise OverflowError('block_map_budget')
        for start, end in merged:
            ranges.extend((start, end, i))
            covered[i] += end - start
    events = []
    for k in range(0, len(ranges), 3):
        start, end, i = ranges[k:k + 3]
        events.append(((start << 1 | 1) << 32) | i)
        events.append(((end << 1) << 32) | i)
    del ranges
    events.sort()
    found = defaultdict(int)
    active = set()
    last = None
    for event in events:
        position = event >> 33
        if active and position > last:
            key = frozenset(active)
            found[key] += position - last
        if event >> 32 & 1:
            active.add(event & 0xFFFFFFFF)
        else:
            active.discard(event & 0xFFFFFFFF)
        last = position
    del events
    parent = list(range(len(order)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    result = defaultdict(int)
    for members, size in found.items():
        first, *rest = members
        for other in rest:
            a, b = root(first), root(other)
            if a != b:
                parent[b] = a
        result[frozenset(order[i] for i in members)] += size
    for i, stream in enumerate(order):
        rest = allocated[stream] - covered[i]
        if rest > 0:
            result[frozenset((stream,))] += rest
    groups = defaultdict(list)
    for i, stream in enumerate(order):
        groups[root(i)].append(stream)
    group = {}
    for members in groups.values():
        key = min(members, key=_order)
        for stream in members:
            group[stream] = key
    return dict(result), group


def _order(key):
    return (isinstance(key, str), key)


def terms(found, holders, parent_of):
    """Per-folder (physical, frees) terms. Summed over a folder's subtree they give
    its physical bytes, each piece once if any copy is below it, and what deleting
    it frees, each piece once if every copy is below it.

    `holders` maps a stream to the folders holding it; `parent_of` gives a folder's
    parent, None at the root. A piece held in one folder is a plain term there;
    otherwise it is +size at each holding folder and -size per extra branch where
    copies meet, so the ordinary bottom-up sum counts it once per folder."""
    out = defaultdict(lambda: [0, 0])
    shapes = {}
    for streams, size in found.items():
        dirs = frozenset(d for s in streams for d in holders.get(s, ()))
        if not dirs:
            continue
        if len(dirs) == 1:
            term = out[next(iter(dirs))]
            term[0] += size
            term[1] += size
            continue
        if dirs not in shapes:
            shapes[dirs] = _shape(dirs, parent_of)
        weights, top = shapes[dirs]
        for d, weight in weights:
            out[d][0] += size * weight
        out[top][1] += size
    return {d: tuple(t) for d, t in out.items() if t != [0, 0]}


def _shape(dirs, parent_of):
    children = defaultdict(int)
    only = {}
    seen = set()
    roots = set()
    for d in dirs:
        node, steps = d, 0
        while node not in seen:
            seen.add(node)
            parent = parent_of(node)
            if parent is None:
                roots.add(node)
                break
            children[parent] += 1
            only[parent] = node
            node = parent
            steps += 1
            if steps > MAX_ANCESTRY:
                raise ScanError('directory_ancestry_limit')
    if len(roots) != 1:
        raise ScanError('missing_directory_ancestor')
    top = next(iter(roots))
    while top not in dirs and children[top] == 1:
        top = only[top]
    return [(a, 1 - children[a]) for a in seen if children[a] != 1], top


def asterisk(physical, listed, frees):
    """Kenny's rule: a folder is marked when Finder shows at least 10% and 1 GB
    more than its physical space, or when at least 10% and 1 GB of it is shared
    with copies outside it. Returns the note's figures, or None."""
    if physical is None:
        return None
    note = {}
    bar = max(GB, physical / 10)
    if listed is not None and listed - physical >= bar:
        note['listed'] = listed
    if frees is not None and physical - frees >= bar:
        note['frees'] = frees
    return note or None


def reconcile(volume_used, physical, *, supported=True, complete=True, previously_invalid=False, full=True, used_change=0):
    """Folders' physical space plus 'not placed in a folder' equals the volume's used
    space. The walk takes time while the volume changes, so the folders may exceed
    used space by up to max(1 GB, the change in used space during the walk); beyond
    that the folder figures are withheld, never rescaled."""
    tolerance = max(GB, abs(used_change or 0))
    if not supported:
        return {'valid': False, 'physical': None, 'not_placed': None, 'tolerance': None, 'status': 'unavailable'}
    if (volume_used is None or physical is None or physical < 0 or physical > volume_used + tolerance
            or (previously_invalid and not full)):
        return {'valid': False, 'physical': None, 'not_placed': None, 'tolerance': tolerance, 'status': 'invalid'}
    return {'valid': True, 'physical': physical, 'not_placed': volume_used - physical, 'tolerance': tolerance,
            'status': 'observed' if complete else 'partial'}
