"""A permission request is not a permission observation."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import sys
import time


@dataclass(frozen=True)
class AccessObservation:
    status: str
    pid: int
    uid: int
    executable_sha256: str
    observed_ns: int


def _identity():
    executable=Path(sys.executable).resolve()
    with executable.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def observe_access(*, probe=False):
    """Call with probe=True only from the explicitly approved scanner launcher.

    The probe enumerates a fixed protected directory without retaining a name.
    It can cause a privacy prompt. Normal collector/setup calls use probe=False.
    """
    identity=_identity()
    if not probe:
        return AccessObservation('unchecked',os.getpid(),os.getuid(),identity,time.time_ns())
    try:
        with os.scandir(Path.home()/'Library'/'Mail') as entries:
            next(entries,None)
        status='observed'
    except PermissionError:
        status='denied'
    except FileNotFoundError:
        status='unavailable'
    except OSError:
        status='failed'
    return AccessObservation(status,os.getpid(),os.getuid(),identity,time.time_ns())


def same_process_grant(observation):
    return (isinstance(observation,AccessObservation) and observation.status=='observed'
            and observation.pid==os.getpid() and observation.uid==os.getuid()
            and observation.executable_sha256==_identity()
            and 0<=time.time_ns()-observation.observed_ns<=60*10**9)
