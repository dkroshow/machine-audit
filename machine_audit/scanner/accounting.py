"""Physical space from shared blocks, worked out in memory for one run.

Block ranges exist only while a run computes pieces from them; physical
addresses are never saved. What the map keeps is which streams share how many
bytes, as per-folder byte terms."""
from __future__ import annotations
from array import array
from collections import defaultdict
from .errors import ScanError

GB = 10**9


def pieces(streams, *, max_ranges=16_000_000):
    """Split the bytes that streams hold into pieces, each held by one set of streams.

    `streams` yields (stream, allocated bytes, (start, length) ranges or None).
    A stream without ranges, or with fewer range bytes than its allocation (a
    resource fork, say), holds the rest privately. Past `max_ranges` ranges a
    stream counts by its allocation and `overflowed` is set: shared blocks may
    then count twice. Returns (order, private, shared, group, overflowed):
    streams by ordinal; each one's private bytes by ordinal; {frozenset of
    streams: bytes} for bytes several hold; and, for streams that share, their
    group, keyed by its least stream key (any other stream is its own group)."""
    order = []
    ranges = array('q')
    private = array('q')
    overflowed = False
    for stream, allocated, found in streams:
        i = len(order)
        order.append(stream)
        covered = 0
        if found and not overflowed:
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
                overflowed = True
            else:
                for start, end in merged:
                    ranges.extend((start, end, i))
                    covered += end - start
        private.append(max(0, allocated - covered))
    # Two sorted event lists, packed as position and ordinal: an end and a start
    # at one position bound no bytes between them.
    starts = sorted(ranges[k] << 32 | ranges[k + 2] for k in range(0, len(ranges), 3))
    ends = sorted(ranges[k + 1] << 32 | ranges[k + 2] for k in range(0, len(ranges), 3))
    del ranges
    shared = defaultdict(int)
    active = set()
    s = e = 0
    last = None
    while e < len(ends):
        position = ends[e] >> 32
        if s < len(starts):
            position = min(position, starts[s] >> 32)
        if active and position > last:
            if len(active) == 1:
                private[next(iter(active))] += position - last
            else:
                shared[frozenset(active)] += position - last
        while e < len(ends) and ends[e] >> 32 == position:
            active.discard(ends[e] & 0xFFFFFFFF)
            e += 1
        while s < len(starts) and starts[s] >> 32 == position:
            active.add(starts[s] & 0xFFFFFFFF)
            s += 1
        last = position
    del starts, ends
    parent = {}

    def root(i):
        while parent.setdefault(i, i) != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for members in shared:
        first, *others = members
        for other in others:
            a, b = root(first), root(other)
            if a != b:
                parent[b] = a
    groups = defaultdict(list)
    for i in parent:
        groups[root(i)].append(order[i])
    group = {}
    for members in groups.values():
        key = min(members, key=_order)
        for stream in members:
            group[stream] = key
    return order, private, {frozenset(order[i] for i in members): size for members, size in shared.items()}, group, overflowed


def _order(key):
    return (isinstance(key, str), key)


def terms(found, holders, parent_of, shapes):
    """Per-folder (physical, frees) terms of (streams, bytes) pieces. Summed over a folder's subtree they give
    its physical bytes, each piece once if any copy is below it, and what deleting
    it frees, each piece once if every copy is below it.

    `holders` maps a stream to the folders holding it; `parent_of` gives a folder's
    parent, None at the root. A piece held in one folder is a plain term there;
    otherwise it is +size at each holding folder and -size per extra branch where
    copies meet, so the ordinary bottom-up sum counts it once per folder.
    `shapes` caches each set of holding folders' shape across calls: the files
    of one folder, copied many times, share one."""
    out = defaultdict(lambda: [0, 0])
    for streams, size in found:
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
        node = d
        # A cycle ends at a folder already seen and leaves no root.
        while node not in seen:
            seen.add(node)
            parent = parent_of(node)
            if parent is None:
                roots.add(node)
                break
            children[parent] += 1
            only[parent] = node
            node = parent
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


def reconcile(volume_used, physical, *, supported=True, complete=True, previously_invalid=False, full=True, used_change=0,
              deferred=False):
    """Folders' physical space plus 'not placed in a folder' equals the volume's used
    space. The walk takes time while the volume changes, so the folders may exceed
    used space by up to max(1 GB, the change in used space during the walk); beyond
    that the folder figures are withheld, never rescaled.

    Deferred folders can hold back only an over-count. Data moved from a deferred
    folder into a re-listed one counts twice until the deferred folder is re-listed,
    so an update that left folders deferred and exceeds the tolerance is not judged:
    the map keeps its last verdict (`deferred` in the result). Within the tolerance
    it is judged as any update, so a folder deferred every hour (one too large to
    re-list more than daily) cannot keep a map invalid, and nothing deferred can make
    one invalid. An invalid map is judged again by every update."""
    tolerance = max(GB, abs(used_change or 0))
    if not supported:
        return {'valid': False, 'physical': None, 'not_placed': None, 'tolerance': None, 'status': 'unavailable'}
    if volume_used is None or physical is None or physical < 0:
        return {'valid': False, 'physical': None, 'not_placed': None, 'tolerance': tolerance, 'status': 'invalid'}
    if physical > volume_used + tolerance:
        if deferred and not full and not previously_invalid:
            return {'valid': True, 'physical': physical, 'not_placed': volume_used - physical, 'tolerance': tolerance,
                    'status': 'partial', 'deferred': True}
        return {'valid': False, 'physical': None, 'not_placed': None, 'tolerance': tolerance, 'status': 'invalid',
                'deferred': bool(deferred and not full)}
    return {'valid': True, 'physical': physical, 'not_placed': volume_used - physical, 'tolerance': tolerance,
            'status': 'observed' if complete else 'partial'}
