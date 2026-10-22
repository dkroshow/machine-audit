"""cgroup v2 unit discovery and identity. Paths stay inside this module; readings
carry a salted hash, the directory inode and a leaf label for admission."""
from __future__ import annotations

import hashlib
import hmac
import os

# Kept memory.stat keys: usage classes and movement named in memory-view.md.
# Parsed by key, so keys a newer kernel adds are ignored until reviewed.
MEMORY_STAT = {k: 'bytes' for k in (
    'anon', 'file', 'kernel', 'kernel_stack', 'pagetables', 'shmem', 'file_mapped', 'file_dirty',
    'file_writeback', 'swapcached', 'slab_reclaimable', 'slab_unreclaimable', 'sock', 'zswap')}
MEMORY_STAT.update({k: 'count' for k in (
    'pgfault', 'pgmajfault', 'workingset_refault_anon', 'workingset_refault_file', 'pgscan', 'pgsteal',
    'zswpin', 'zswpout')})
CPU_STAT = {'usage_usec': 'us', 'user_usec': 'us', 'system_usec': 'us', 'nr_periods': 'count',
            'nr_throttled': 'count', 'throttled_usec': 'us'}
MEMORY_EVENTS = ('low', 'high', 'max', 'oom', 'oom_kill', 'oom_group_kill')


def units(base):
    """Yield (cgroup path, directory) for the first non-slice cgroup below each slice:
    one non-overlapping level, so unit totals never count a process twice."""
    stack = ['']
    while stack:
        rel = stack.pop()
        try:
            entries = list(os.scandir(os.path.join(base, rel.lstrip('/'))))
        except FileNotFoundError:
            if not rel:
                raise
            continue  # the slice went away during the walk
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                child = rel + '/' + entry.name
                if entry.name.endswith('.slice'):
                    stack.append(child)
                else:
                    yield child, entry.path


def identity(salt, rel, directory):
    """A unit restarted under the same path gets a new inode, so its first read is a baseline."""
    digest = hmac.new(salt, rel.encode(), hashlib.sha256).hexdigest()[:16]
    return f'cgroup:{digest}:{os.stat(directory).st_ino}'


def leaf_name(rel):
    """A templated unit is named by its template; the instance can carry private names.
    Mount, automount and swap units are named by their type: their names are escaped paths."""
    leaf = rel.rsplit('/', 1)[-1]
    kind = leaf.rpartition('.')[2]
    if kind in ('mount', 'automount', 'swap'):
        return kind
    template, at, instance = leaf.partition('@')
    if not at:
        return leaf
    return template + '.' + instance.rpartition('.')[2] if '.' in instance else template


def pids(directory):
    """Processes in a unit, including its delegated sub-cgroups."""
    found = []
    for path, _, _ in os.walk(directory):
        try:
            with open(os.path.join(path, 'cgroup.procs')) as stream:
                found.extend(int(p) for p in stream.read().split())
        except FileNotFoundError:
            pass  # the sub-cgroup went away during the walk
    return found
