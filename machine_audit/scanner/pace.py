"""The scanner's CPU allowance for one scheduled run (storage-approach.md#collector-cost).

Machine-audit must never be one of the main contributors it reports: its CPU stays
under SHARE of the machine's process CPU in every hour. The room left for the
scanner is that share of the trailing hour, minus the collector's own CPU in it,
both read from the collector's own store. A run may spend the room, but never less
than FLOOR_NS, so hourly updates keep the map current in hours the collector alone
fills; a paced full walk always gets WALK_FLOOR_NS, so it ends inside a week."""
from __future__ import annotations
from collections import defaultdict
from pathlib import Path
import time

HOUR_NS = 3600 * 10**9
FLOOR_NS = 120 * 10**9
WALK_FLOOR_NS = 20 * 10**9
# Placing shared blocks and totalling folders each run as one piece; a piece waits
# for an hour whose allowance covers its estimate, but never longer than a day.
# Measured on Big Mac's startup volume: 127 s for 1.06 M files that may share
# blocks, 41 s to total 1.55 M folders.
SHARING_NS_PER_HOLDER = 120_000
TOTALS_NS_PER_FOLDER = 30_000
PIECE_WAIT_NS = 24 * HOUR_NS
# A folder with more direct entries than this is large: an update re-lists it at most
# daily, and a paced walk lists it only in a run whose share covers it, for it cannot
# pause part-way through one folder (Big Mac, Nov 11: one of 416 k entries took 80 s).
LARGE_FOLDER_ENTRIES = 20_000
LIST_NS_PER_ENTRY = 200_000


def room_from(cpu, own, share):
    """Room in ns from two `who` answers over the same hour: the machine's process
    CPU (signal process.cpu) and Machine-audit's own rows (component self). None when
    the answer cannot support it. Program and process rows describe the same CPU, so
    the larger kind's total is the machine's."""
    if not cpu.get('totals_complete'):return None
    kinds=defaultdict(float)
    for row in cpu['rows']:
        if row.get('signal')=='process.cpu' and row.get('total') is not None:
            kinds['program' if row['entity'].startswith(('program:','rest:program')) else 'process']+=row['total']*row['scale']
    collector=sum(row['total']*row['scale'] for row in own['rows'] if row.get('label')=='collector'
                  and row.get('signal') in ('self.user_time','self.system_time') and row.get('total') is not None)
    process=max(kinds.values(),default=0)
    if process<=0:return None
    return int((share*process-collector)*1e9)


def room_ns(root, now_ns):
    """The trailing hour's room, or None when the collector's store cannot say."""
    path=Path(root)/'readings.sqlite'
    if not path.is_file():return None
    try:
        from machine_audit.store import Store, SHARE
        from machine_audit.query import Query
        store=Store(path,read_only=True)
        try:
            query=Query(store);start=now_ns-HOUR_NS
            cpu=query.who(start,now_ns,'process.cpu','cpu',2000)
            own=query.who(start,now_ns,None,'self',2000)
        finally:store.close()
        return room_from(cpu,own,SHARE)
    except Exception:
        # The floors then govern; the run row records that the room was unknown.
        return None


class Allowance:
    """CPU this run may spend, counted from its start. `limit` None is unpaced: the
    operator's forced scan."""
    def __init__(self, limit, room=None):
        self.limit, self.room = limit, room
        self.start = self.clock()
        self.walk_until = None

    @staticmethod
    def clock():
        return time.process_time_ns()

    def spent(self):
        return self.clock()-self.start

    def exhausted(self):
        """Updates stop here and defer the rest of their folders."""
        return self.limit is not None and self.spent()>=self.limit

    def _walk_limit(self):
        if self.limit is None:return None
        if self.walk_until is None:self.walk_until=max(self.limit,self.spent()+WALK_FLOOR_NS)
        return self.walk_until

    def walk_open(self):
        """Whether walk work may go on. The first call fixes the walk's share: what
        the updates left, but at least the walk floor."""
        limit=self._walk_limit()
        return limit is None or self.spent()<limit

    def covers(self, cost_ns):
        limit=self._walk_limit()
        return limit is None or limit-self.spent()>=cost_ns


def allowance(root, *, force, now_ns=None):
    if force:return Allowance(None)
    room=room_ns(root,now_ns or time.time_ns())
    return Allowance(max(room or 0,FLOOR_NS),room)
