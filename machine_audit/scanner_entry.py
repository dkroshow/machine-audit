"""The installed scanner's fixed entry; never imported by the collector."""
from __future__ import annotations
from dataclasses import asdict
import json
import os
import time

from .budget import Budget
from .config import Config
from .scanner.permissions import observe_access


def main(installation: dict, *, check_only=False) -> int:
    config = Config.load(installation['config'])
    # A permission-check request is consumed only by this fixed entry point.
    # Normal scheduled runs perform the same check only when the user selected
    # protected scanning. No request can supply a path, program, or Python code.
    request_path = config.root / 'scanner-request.json'
    request = None
    if check_only and request_path.exists():
        info = request_path.lstat()
        if request_path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 8192:
            return 2
        request = json.loads(request_path.read_text())
        if (set(request) != {'action', 'nonce', 'expires_ns'} or
                request['action'] != 'check_access' or
                not isinstance(request['nonce'], str) or len(request['nonce']) != 32 or
                not isinstance(request['expires_ns'], int)):
            return 2
        if time.time_ns() > request['expires_ns']:
            return 2
    if check_only and request is None:
        return 2
    if request is None and not config.enabled.get('scanner', False):
        return 2
    requested = bool(config.source_options.get('scan_protected_requested'))
    observation = observe_access(probe=request is not None or requested)
    result = dict(asdict(observation), nonce=request['nonce'] if request else None)
    path = config.root / 'scanner-access.json'
    budget = Budget(config.root)
    if request is not None or requested:
        with budget.reserve(8192):
            if path.is_symlink():
                return 2
            temporary = config.root / ('scanner-access-' + str(os.getpid()) + '.new')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(result, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
    if request is not None:
        # The CLI owns and removes its request; checking access never scans.
        return 0 if observation.status == 'observed' else 1
    from .scanner.runner import run_scan
    result = run_scan(config.root, full_disk_access=requested, access=observation,
                      full=bool(config.source_options.get('scanner_full', False)),
                      grain=config.source_options.get('scanner_grain', 'files'),
                      exclusions=config.scanner_exclusions)
    print(json.dumps(result))
    return 0 if result['status'] == 'observed' else 1
