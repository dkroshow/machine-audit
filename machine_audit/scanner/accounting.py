"""Bounded in-memory shared-block allocation. Physical addresses are never saved."""
from __future__ import annotations
from array import array
from dataclasses import dataclass
import heapq


@dataclass(frozen=True)
class Stream:
    key: int
    holder: str
    allocated: int
    private: int | None
    partial: bool


class Blocks:
    def __init__(self, max_bytes=16 * 1024**2, max_calls=2_000_000):
        self.max_bytes, self.max_calls = max_bytes, max_calls
        self.ranges = array('Q')
        self.range_keys = []
        self.streams = {}
        self.complete = set()
        self.estimated = set()

    def add(self, stream, ranges):
        if stream.key in self.streams:
            return
        self.streams[stream.key] = stream
        ordinal=len(self.range_keys)
        self.range_keys.append(stream.key)
        first = len(self.ranges)
        calls = 0
        try:
            for start, length in ranges:
                calls += 1
                if calls > self.max_calls or len(self.ranges) * self.ranges.itemsize + 24 > self.max_bytes:
                    raise OverflowError('block_map_budget')
                if start < 0 or length <= 0:
                    raise ValueError('invalid_extent')
                self.ranges.extend((start, start + length, ordinal))
        except (OSError, OverflowError, ValueError):
            del self.ranges[first:]
            self.estimated.add(stream.key)
            return
        self.complete.add(stream.key)

    def charges(self):
        # A complete stream maps its bytes once. Failed partial maps count private
        # bytes only; they cannot inflate shared use. The estimate remains visible.
        charges = {k: (0 if k in self.complete else (s.private or 0) if s.partial else s.allocated)
                   for k, s in self.streams.items()}
        events = []
        for i in range(0, len(self.ranges), 3):
            a, b, ordinal = self.ranges[i:i + 3]
            key=self.range_keys[ordinal]
            events.append((a, 1, key))
            events.append((b, -1, key))
        events.sort()
        active, heap, last, i = {}, [], None, 0
        while i < len(events):
            position = events[i][0]
            while heap and heap[0][1] not in active:
                heapq.heappop(heap)
            if last is not None and heap:
                charges[heap[0][1]] += position - last
            while i < len(events) and events[i][0] == position:
                _, direction, key = events[i]
                active[key] = active.get(key, 0) + direction
                if active[key] == 0:
                    del active[key]
                elif direction == 1:
                    heapq.heappush(heap, (abs(key),key))
                i += 1
            last = position
        self.ranges = array('Q')
        return charges


def reconcile(volume_used, attributed, *, supported=True, complete=True, previously_invalid=False, full=True):
    if not supported:
        return {'valid': False, 'uses': None, 'not_attributed': None, 'status': 'unavailable'}
    if volume_used is None or attributed is None or attributed < 0 or attributed > volume_used or (previously_invalid and not full):
        return {'valid': False, 'uses': None, 'not_attributed': None, 'status': 'invalid'}
    return {'valid': True, 'uses': attributed, 'not_attributed': volume_used - attributed,
            'status': 'observed' if complete else 'partial'}
