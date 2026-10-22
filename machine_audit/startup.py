"""Bounded startup evidence, including failures before a recording run exists."""
import errno
import json
import os
import socket
import sqlite3
import stat
import sys
import time

from .budget import Budget, BudgetExceeded

SIZE = 4096
STATES = {'prepared', 'starting', 'running', 'stopped', 'failed'}
CODES = {'port_in_use', 'port_unavailable', 'budget_exhausted', 'permission_denied',
         'storage_failed', 'collector_busy', 'initialization_failed', 'shutdown_incomplete'}


def port_status(config):
    """Try a loopback bind, without listening or contacting another process."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            # Match HTTPServer so a stopped server's TIME_WAIT sockets do not block restart.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', config.port))
        return 'available'
    except OSError as exc:
        return 'port_in_use' if exc.errno == errno.EADDRINUSE else 'port_unavailable'


def _valid_file(info):
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077 and info.st_size == SIZE


def read(config):
    try:
        fd = os.open(config.root / 'startup.json', os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            if not _valid_file(os.fstat(stream.fileno())):
                return {'state': 'unknown'}
            result = json.loads(stream.read(SIZE))
        if (set(result) != {'state', 'code', 'at_ns'} or result['state'] not in STATES
                or result['code'] not in CODES | {None} or type(result['at_ns']) is not int):
            return {'state': 'unknown'}
        return result
    except (OSError, ValueError, TypeError):
        return {'state': 'unknown'}


class StartupStatus:
    def __init__(self, config):
        self.path = config.root / 'startup.json'
        budget = Budget(config.root)
        if not self.path.exists():
            with budget.reserve(SIZE):
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(self._bytes('prepared', None))
                    stream.flush()
                    os.fsync(stream.fileno())

    @staticmethod
    def _bytes(state, code):
        if state not in STATES or code not in CODES | {None}:
            raise ValueError('invalid_startup_status')
        return json.dumps({'state': state, 'code': code, 'at_ns': time.time_ns()}).encode().ljust(SIZE, b' ')

    def write(self, state, code=None):
        # Installation reserves this fixed allocation. Updates cannot grow it,
        # including when the recorder cannot admit another database transaction.
        fd = os.open(self.path, os.O_WRONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r+b') as stream:
            if not _valid_file(os.fstat(stream.fileno())):
                raise ValueError('invalid_startup_file')
            stream.write(self._bytes(state, code))
            stream.flush()
            os.fsync(stream.fileno())


def guarded_run(config, run):
    """Run the collector callback; it calls ready once its server can answer."""
    status = None
    try:
        status = StartupStatus(config)
        status.write('starting')
        port = port_status(config)
        if port != 'available':
            status.write('failed', port)
            print('Collection did not start: ' + port, file=sys.stderr)
            return 2
        result = run(lambda: status.write('running'))
        status.write('stopped' if result == 0 else 'failed', None if result == 0 else 'shutdown_incomplete')
        return result
    except Exception as exc:
        code = ('budget_exhausted' if isinstance(exc, BudgetExceeded) else
                'permission_denied' if isinstance(exc, PermissionError) else
                'collector_busy' if isinstance(exc, BlockingIOError) or
                    isinstance(exc, RuntimeError) and str(exc) == 'collector_already_running' else
                'storage_failed' if isinstance(exc, sqlite3.Error) else
                'port_in_use' if isinstance(exc, OSError) and exc.errno == errno.EADDRINUSE else
                'initialization_failed')
        try:
            if status is not None:
                status.write('failed', code)
        except (OSError, ValueError):
            pass  # A damaged/inaccessible status file is unknown to doctor.
        print('Collection failed: ' + code + '; run machine-audit doctor.', file=sys.stderr)
        return 2
