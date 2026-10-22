"""inotify on the scanner's hot set, between nightly walks. A changed folder goes to the
scanner's dirty file as volume and inode numbers. Paths are read from the scanner store
only to place watches; event names are read and dropped."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sqlite3
import struct

from machine_audit.scanner.store import hot_set
from ..macos import field

# inotify(7)
IN_MODIFY, IN_CLOSE_WRITE, IN_MOVED_FROM, IN_MOVED_TO, IN_CREATE, IN_DELETE = 0x2, 0x8, 0x40, 0x80, 0x100, 0x200
IN_DELETE_SELF, IN_MOVE_SELF, IN_Q_OVERFLOW, IN_IGNORED = 0x400, 0x800, 0x4000, 0x8000
IN_ONLYDIR, IN_DONT_FOLLOW, IN_EXCL_UNLINK = 0x01000000, 0x02000000, 0x04000000
CHANGED = IN_MODIFY | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
GONE = IN_DELETE_SELF | IN_MOVE_SELF | IN_IGNORED
EVENT = struct.Struct('=iIII')  # wd, mask, cookie, name length; the name follows
LIMIT = 8192  # watches at most, and never more than an eighth of max_user_watches
DIRTY = 'scanner-dirty.json'


def changes(buffer, watches):
    """({volume id: folder ids}, overflowed, ended watches) from raw events. A folder that went
    away dirties its parent; a queue overflow dirties every watched folder."""
    changed, overflowed, ended, offset = {}, False, set(), 0
    while offset + EVENT.size <= len(buffer):
        wd, mask, _, length = EVENT.unpack_from(buffer, offset)
        offset += EVENT.size + length
        if mask & IN_Q_OVERFLOW:
            overflowed = True
            targets = [(volume, folder) for volume, folder, _ in watches.values()]
        elif wd in watches:
            volume, folder, parent = watches[wd]
            targets = [(volume, parent)] if mask & GONE and parent else [(volume, folder)] if mask & CHANGED else []
            if mask & IN_IGNORED:
                ended.add(wd)
        else:
            continue
        for volume, folder in targets:
            changed.setdefault(volume, set()).add(folder)
    return changed, overflowed, ended


def record_dirty(data, budget, changed):
    """Merge {volume id: folder ids} into the dirty file in one short write. The budget lock is
    held only for that write; nothing waits while holding it."""
    path = data / DIRTY
    try:
        merged = {volume: set(ids) for volume, ids in json.loads(path.read_text())['volumes'].items()}
    except FileNotFoundError:
        merged = {}
    if all(ids <= merged.get(volume, set()) for volume, ids in changed.items()):
        return  # a busy folder already waiting costs no write
    for volume, ids in changed.items():
        merged.setdefault(volume, set()).update(ids)
    payload = json.dumps({'version': 1, 'volumes': {v: sorted(ids) for v, ids in merged.items()}}).encode()
    temporary = data / (DIRTY + '.new')
    with budget.reserve(len(payload)):
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
        os.replace(temporary, path)


def take_dirty(data):
    """Claim the dirty file by renaming it, so the watcher's next write starts a new one.
    Files claimed by an interrupted run are merged and claimed again."""
    try:
        os.replace(data / DIRTY, data / f'{DIRTY}.{os.getpid()}')
    except FileNotFoundError:
        pass
    taken, changed = sorted(data.glob(DIRTY + '.[0-9]*')), {}
    for path in taken:
        for volume, ids in json.loads(path.read_text())['volumes'].items():
            changed.setdefault(volume, set()).update(ids)
    return changed, taken


class Watcher:
    def __init__(self, root, data, budget):
        self.root, self.data, self.budget = Path(root), Path(data), budget
        self.store = self.data / 'scanner.sqlite'
        self.fd = self.version = None
        self.watches = {}
        self.overflows = 0
        self.libc = None

    def published(self):
        db = sqlite3.connect(self.store.as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            return db.execute('SELECT max(observed_ns) FROM volumes').fetchone()[0]
        finally:
            db.close()

    def rebuild(self):
        """New watches after each published map; the previous set closes with its descriptor."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd, self.watches = None, {}
        self.libc = self.libc or ctypes.CDLL(None, use_errno=True)
        fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), 'inotify_unavailable')
        self.fd = fd
        limit = min(LIMIT, int((self.root / 'proc/sys/fs/inotify/max_user_watches').read_text()) // 8)
        for volume, folder, parent, path in hot_set(self.store, limit):
            try:
                if str(os.stat(path, follow_symlinks=False).st_ino) != folder:
                    continue  # replaced since the walk; the next walk places it again
            except OSError:
                continue
            wd = self.libc.inotify_add_watch(fd, os.fsencode(path), CHANGED | GONE | IN_ONLYDIR | IN_DONT_FOLLOW | IN_EXCL_UNLINK)
            if wd >= 0:
                self.watches[wd] = (volume, folder, parent)

    def read(self):
        if not self.store.exists():
            yield field('self.watched_folders', None, 'count', status='unobserved')
            return
        version = self.published()
        if self.fd is None or version != self.version:
            self.rebuild()
            self.version = version
        changed = {}
        while True:
            try:
                buffer = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            found, overflowed, ended = changes(buffer, self.watches)
            for volume, folders in found.items():
                changed.setdefault(volume, set()).update(folders)
            self.overflows += overflowed
            for wd in ended:
                del self.watches[wd]
        if changed:
            record_dirty(self.data, self.budget, changed)
        yield field('self.watched_folders', len(self.watches), 'count')
        yield field('self.watch_dirtied', sum(len(f) for f in changed.values()), 'count')
        yield field('self.watch_overflows', self.overflows, 'count', 'counter')
