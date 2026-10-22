"""Bounded historical FSEvents replay and update scheduling."""
from __future__ import annotations
from dataclasses import dataclass, field
import ctypes as C
import time
from machine_audit.sources.native import U32, U64, I32, bind

MUST_SCAN, USER_DROP, KERNEL_DROP, WRAPPED, HISTORY_DONE, ROOT_CHANGED = 1, 2, 4, 8, 16, 32


@dataclass
class Replay:
    paths: dict[str, bool] = field(default_factory=dict)
    last_good: int | None = None
    up_to: int | None = None
    lost: bool = False
    interrupted: bool = False
    text_bytes: int = 0

    def add(self, path, flags, event_id, max_paths=100_000, max_bytes=16*1024**2):
        if flags & (KERNEL_DROP | WRAPPED | ROOT_CHANGED):
            self.lost = True
            return False
        if flags & USER_DROP:
            self.interrupted = True
            return False
        if flags & HISTORY_DONE:
            self.up_to = event_id
            return False
        added = len(path.encode()) if path not in self.paths else 0
        if len(self.paths) + (path not in self.paths) > max_paths or self.text_bytes + added > max_bytes:
            self.interrupted = True
            return False
        self.paths[path] = self.paths.get(path, False) or bool(flags & MUST_SCAN)
        self.text_bytes += added
        self.last_good = event_id
        return True


def next_action(*, now_ns, full_ns, update_ns, bookmark, no_progress, events_lost=False, requested_full=False):
    day = 86400 * 10**9
    if full_ns is None or bookmark is None or events_lost or no_progress >= 3:
        return 'full'
    if now_ns - full_ns >= 7 * day:
        return 'full'
    if requested_full and now_ns - full_ns >= day:
        return 'full'
    if update_ns is None or now_ns - update_ns >= 3600 * 10**9:
        return 'update'
    return 'wait'


class FSEvents:
    """Own run loop, no resident watcher. Call only in the scanner process."""
    def __init__(self, io):
        self.cf = io.cf
        self.cs = C.CDLL('/System/Library/Frameworks/CoreServices.framework/CoreServices')
        self.io = io

    def current_id(self):
        return bind(self.cs, 'FSEventsGetCurrentEventId', U64)()

    def uuid(self,device):
        obj=bind(self.cs,'FSEventsCopyUUIDForDevice',C.c_void_p,I32)(device)
        if not obj:return None
        try:
            text=bind(self.cf,'CFUUIDCreateString',C.c_void_p,C.c_void_p,C.c_void_p)(None,obj)
            try:return self.io.decode(text)
            finally:self.io.release(text)
        finally:self.io.release(obj)

    def replay(self, device, since, path='', timeout=30):
        P = C.c_void_p
        result = Replay()
        callback_type = C.CFUNCTYPE(None, P, P, C.c_size_t, P, C.POINTER(U32), C.POINTER(U64))
        finished = False
        def receive(stream, context, count, paths, flags, ids):
            nonlocal finished
            if finished:
                return
            strings = C.cast(paths, C.POINTER(C.c_char_p))
            for i in range(count):
                if not result.add(strings[i].decode('utf-8', 'surrogateescape'), flags[i], ids[i]):
                    finished = True
                    break
        callback = callback_type(receive)
        string = self.io.string(None, path.encode(), 0x08000100)
        strings = (P * 1)(string)
        # CF array retains the string for the stream lifetime.
        callbacks = (P * 5).in_dll(self.cf, 'kCFTypeArrayCallBacks')
        array = bind(self.cf, 'CFArrayCreate', P, P, C.POINTER(P), C.c_long, P)(None, strings, 1, callbacks)
        class Context(C.Structure):
            _fields_ = [('version', C.c_long), ('info', P), ('retain', P), ('release', P), ('description', P)]
        context = Context()
        stream = bind(self.cs, 'FSEventStreamCreateRelativeToDevice', P, P, callback_type, C.POINTER(Context),
                      I32, P, U64, C.c_double, U32)(None, callback, C.byref(context), device, array, since, 0, 0)
        self.io.release(array)
        self.io.release(string)
        if not stream:
            raise OSError('events_create_failed')
        loop = bind(self.cf, 'CFRunLoopGetCurrent', P)()
        mode = P.in_dll(self.cf, 'kCFRunLoopDefaultMode')
        bind(self.cs, 'FSEventStreamScheduleWithRunLoop', None, P, P, P)(stream, loop, mode)
        try:
            if not bind(self.cs, 'FSEventStreamStart', C.c_bool, P)(stream):
                raise OSError('events_start_failed')
            deadline = time.monotonic() + timeout
            while not finished and time.monotonic() < deadline:
                bind(self.cf, 'CFRunLoopRunInMode', I32, P, C.c_double, C.c_bool)(mode, min(0.2, deadline-time.monotonic()), True)
            if not finished:
                result.interrupted = True
        finally:
            bind(self.cs, 'FSEventStreamStop', None, P)(stream)
            bind(self.cs, 'FSEventStreamInvalidate', None, P)(stream)
            bind(self.cs, 'FSEventStreamRelease', None, P)(stream)
        return result
