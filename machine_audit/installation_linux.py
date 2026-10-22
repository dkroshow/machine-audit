"""Linux installation: a source snapshot, a CLI and three systemd unit files, written by the
dedicated service user. Root places the units and controls them; nothing here runs as root
except start and stop, which only call systemctl."""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import re
import shlex
import subprocess
import sys

from .config import Config
from .installation import _command, _private_directory, _snapshot, _write

COLLECTOR, SCANNER, TIMER = 'machine-audit.service', 'machine-audit-scan.service', 'machine-audit-scan.timer'
# Hardening every unit shares; the data directory is the only writable path. MemoryMax is
# argued in installation.md; an OOM kill stops the unit with result oom-kill, never silently.
HARDENING = ('UMask=0077\nNoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nReadWritePaths={data}\n'
             'MemoryMax=1G\nOOMPolicy=stop\n')


def application_root() -> Path:
    return Path.home() / '.local' / 'lib' / 'machine-audit'


def plan(config: Config) -> dict:
    if os.geteuid() == 0 or sys.version_info < (3, 12):
        raise ValueError('use_python_3_12_as_the_service_user')
    config.validate()
    source = Path(__file__).resolve().parent.parent
    revision = _command(['git', '-C', str(source), 'rev-parse', 'HEAD']).decode().strip()
    _command(['git', '-C', str(source), 'diff', '--quiet', 'HEAD', '--',
              'machine_audit', 'dashboard', 'native', 'pyproject.toml'])
    root = application_root()
    from .startup import port_status
    return {'schema_version': 1, 'source': str(source), 'revision': revision,
            'release': str(root / 'releases' / revision), 'root': str(root),
            'python': str(Path(sys.executable).resolve()), 'config': str(config.path), 'data': str(config.root),
            'user': pwd.getpwuid(os.getuid()).pw_name, 'units': {name: str(root / 'units' / name) for name in (COLLECTOR, SCANNER, TIMER)},
            'cli': str(Path.home() / '.local' / 'bin' / 'machine-audit'), 'starts_services': False,
            'port_status': port_status(config)}


def unit_files(candidate: dict) -> dict[str, str]:
    # systemd splits ExecStart on whitespace; refuse rather than quote an unusual path.
    paths = [candidate['python'], candidate['release'], candidate['config'], candidate['data'], candidate['user']]
    if not all(re.fullmatch(r'[A-Za-z0-9._/@+-]+', p) for p in paths):
        raise ValueError('unit_path_needs_review')
    # -B: the release is read-only under ProtectSystem=strict, and -I ignores PYTHONDONTWRITEBYTECODE.
    command = ' '.join([candidate['python'], '-I', '-B', candidate['release'] + '/machine_audit/bootstrap.py', '--config', candidate['config']])
    hardening = HARDENING.format(data=candidate['data'])
    return {
        COLLECTOR: f'''[Unit]
Description=Machine-audit collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={candidate['user']}
ExecStart={command} run
Restart=on-failure
RestartSec=30
CPUWeight=20
{hardening}
[Install]
WantedBy=multi-user.target
''',
        SCANNER: f'''[Unit]
Description=Machine-audit filesystem map

[Service]
Type=oneshot
User={candidate['user']}
ExecStart={command} scan
Nice=10
IOSchedulingClass=idle
CPUWeight=10
{hardening}''',
        TIMER: f'''[Unit]
Description=Machine-audit map: hourly updates; a night run walks in full

[Timer]
OnCalendar=hourly
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
''',
    }


KEEP_RELEASES = 3  # previous releases kept beside the current one; older ones move to retired/


def retire(root: Path, cli: Path, revision: str) -> None:
    """Set the installed markers aside under the old revision's name; nothing is deleted."""
    moves = [(root / 'current.json', root / f'previous-{revision}.json'), (root / 'units', root / f'units.{revision}'),
             (cli, cli.with_name(f'{cli.name}.{revision}'))]
    if any(new.exists() or new.is_symlink() for _, new in moves):
        raise ValueError('retired_markers_already_exist')
    for old, new in moves:
        if old.exists() or old.is_symlink():
            os.rename(old, new)


def retire_releases(root: Path, current: str) -> None:
    previous = sorted((p for p in (root / 'releases').iterdir() if p.name != current),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    for release in previous[KEEP_RELEASES:]:
        (root / 'retired').mkdir(mode=0o700, exist_ok=True)
        if not (root / 'retired' / release.name).exists():
            os.rename(release, root / 'retired' / release.name)


def placement(candidate: dict) -> list[str]:
    return ['sudo install -m 0644 ' + ' '.join(candidate['units'].values()) + ' /etc/systemd/system/',
            'sudo systemctl daemon-reload']


def install(config: Config, *, json_output=False, yes=False, upgrade=False) -> int:
    try:
        candidate = plan(config)
        units = unit_files(candidate)
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({'status': 'not_ready', 'reason': str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__}))
        return 2
    if json_output or (not sys.stdin.isatty() and not yes):
        print(json.dumps(dict(candidate, place_units=placement(candidate)), indent=2))
        return 0
    root, cli = Path(candidate['root']), Path(candidate['cli'])
    previous = None
    if upgrade:
        if not (root / 'current.json').is_file():
            print(json.dumps({'status': 'not_ready', 'reason': 'nothing_to_upgrade'}))
            return 2
        previous = json.loads((root / 'current.json').read_text())['revision']
        if previous == candidate['revision']:
            print(json.dumps({'status': 'not_ready', 'reason': 'revision_already_installed'}))
            return 2
        print(f'Upgrade from {previous}: its markers are renamed with that suffix, never deleted.')
    print('Install this source revision for the service user ' + candidate['user'] + '.')
    print(f"Application: {candidate['root']}\nRecordings: {config.root}")
    print('Creates a CLI and three systemd unit files here. Root places them; nothing starts.')
    from .onboarding import _yes_no
    if not yes and not _yes_no('Install these files?', True):
        return 1
    try:
        _private_directory(root)
        _private_directory(config.root)
        if not config.path.is_file():
            raise ValueError('run_setup_first')
        if not upgrade and ((root / 'current.json').exists() or (root / 'units').exists()):
            raise ValueError('existing_installation_requires_reviewed_update')
        if not upgrade and (cli.exists() or cli.is_symlink()):
            raise ValueError('cli_path_already_exists')
        _snapshot(candidate)  # before any marker moves, so a failed copy leaves the installation as it was
        if upgrade:
            retire(root, cli, previous)
        (root / 'units').mkdir(mode=0o700)
        for name, text in units.items():
            _write(root / 'units' / name, text.encode(), 0o644)
        cli.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        command = [candidate['python'], '-I', candidate['release'] + '/machine_audit/bootstrap.py', '--config', candidate['config']]
        _write(cli, ('#!/bin/sh\nexec ' + shlex.join(command) + ' "$@"\n').encode(), 0o700)
        config.source_options['install_revision'] = candidate['revision']
        config.save()
        from .startup import StartupStatus
        StartupStatus(config)
        _write(root / 'current.json', json.dumps(candidate, indent=2).encode())
        if upgrade:
            retire_releases(root, Path(candidate['release']).name)
    except (ValueError, RuntimeError, OSError) as exc:
        print('Installation stopped: ' + (str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__))
        return 2
    print('Installed; monitoring has not started. As an administrator, place the units:')
    for line in placement(candidate):
        print('  ' + line)
    print('Then start with machine-audit start, run as root, and check with machine-audit doctor.')
    return 0


def control(config: Config, action: str) -> int:
    """systemctl for the product's own units; needs root, and says the exact command when not."""
    commands = ([['systemctl', 'enable', '--now', COLLECTOR, TIMER]] if action == 'start' else
                [['systemctl', 'disable', '--now', TIMER, COLLECTOR], ['systemctl', 'stop', SCANNER]])
    if os.geteuid() != 0:
        print('Service control needs root. Run:')
        for command in commands:
            print('  sudo ' + shlex.join(command))
        return 2
    failures = []
    for command in commands:
        # Every command runs, so a failed disable still stops the scanner; systemctl text stays unprinted.
        if subprocess.run(command, capture_output=True, timeout=60).returncode:
            failures.append(command[1])
    if failures:
        print('Service control incomplete: ' + ', '.join(failures))
        return 2
    print('Started the collector and the map timer. Run machine-audit doctor to confirm collection.' if action == 'start' else
          'Stopped Machine-audit. Recordings were preserved.')
    return 0
