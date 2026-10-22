"""Shared by the Linux sources and scanner: the kernel's text formats and salted identities."""
from __future__ import annotations

import errno
import hashlib
import hmac

from ..macos import error_status, field


def gap_status(exc):
    """A file the kernel does not provide, or a sensor with no value now, is unavailable rather than failed."""
    return 'unavailable' if getattr(exc, 'errno', None) in (errno.ENOENT, errno.EOPNOTSUPP, errno.ENODATA) else error_status(exc)


def keyed(text, separator=' '):
    """'key value' (cgroup, vmstat), 'Key: value unit' (meminfo, status, io) or KEY=value (uevent) lines."""
    return {key.strip(): value.split() for key, _, value in (line.partition(separator) for line in text.splitlines()) if value.strip()}


def kib(values, key):
    """The kernel's 'kB' is KiB; the byte value is exact."""
    value = values.get(key)
    return int(value[0]) * 1024 if value else None


def pressure(text):
    """PSI 'some' and 'full' lines; only the integer microsecond totals are kept."""
    return {kind: int(dict(pair.split('=', 1) for pair in pairs)['total'])
            for kind, *pairs in (line.split() for line in text.splitlines() if line.strip())}


def optional(path):
    """A file a controller or driver does not provide is absent, not zero."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def psi(path, name, **kw):
    """Host PSI totals; a missing file (PSI not built in or switched off) is an explicit gap."""
    try:
        totals = pressure(path.read_text())
    except OSError as exc:
        totals, kw = {}, dict(kw, status=gap_status(exc))
    for kind in ('some', 'full'):
        # The rate of a microsecond stall counter reads as percent of time stalled.
        yield field(f'{name}.pressure.{kind}', totals.get(kind), 'us', 'counter', scale=1e-4, rate_unit='percent', **kw)


def salted(salt, text):
    """A stable identity that reveals nothing about the path or id it came from."""
    return hmac.new(salt, text.encode(), hashlib.sha256).hexdigest()[:16]
