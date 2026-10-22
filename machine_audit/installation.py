"""Source installation and explicit control of this product's own launch jobs."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import stat
import subprocess
import sys
import sysconfig
import tarfile
import time
import uuid

from .config import Config

COLLECTOR = 'local.machine-audit.collector'
SCANNER = 'local.machine-audit.scanner'
PERMISSION = 'local.machine-audit.permission-check'


def application_root() -> Path:
    return Path.home() / 'Library' / 'Application Support' / 'machine-audit'


def scanner_executable(root: Path) -> Path:
    return root / 'Machine-audit Scanner.app' / 'Contents' / 'MacOS' / 'scanner'


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError('installation_directory_symlink')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('installation_directory_not_private')


def _write(path: Path, data: bytes, mode=0o600) -> None:
    if path.is_symlink() or path.exists():
        raise ValueError('installation_path_exists')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink()


def _command(args: list[str], *, timeout=60) -> bytes:
    completed = subprocess.run(args, capture_output=True, timeout=timeout)
    if completed.returncode:
        # Compiler, launchctl and filesystem errors can include private paths.
        raise RuntimeError(Path(args[0]).name + '_failed')
    return completed.stdout


def plan(config: Config) -> dict:
    if sys.platform != 'darwin':
        raise ValueError('installation_not_validated_on_this_platform')
    if os.getuid() == 0 or sys.version_info < (3, 12):
        raise ValueError('use_python_3_12_as_the_device_user')
    config.validate()
    source = Path(__file__).resolve().parent.parent
    revision = _command(['git', '-C', str(source), 'rev-parse', 'HEAD']).decode().strip()
    _command(['git', '-C', str(source), 'diff', '--quiet', 'HEAD', '--',
              'machine_audit', 'dashboard', 'native', 'pyproject.toml'])
    root = application_root()
    from .startup import port_status
    return {'schema_version': 1, 'source': str(source), 'revision': revision,
            'release': str(root / 'releases' / revision), 'root': str(root),
            'python': str(Path(sys.executable).resolve()), 'python_home': sys.base_prefix,
            'config': str(config.path), 'scanner': str(scanner_executable(root)),
            'cli': str(Path.home() / '.local' / 'bin' / 'machine-audit'),
            'jobs': [COLLECTOR, SCANNER], 'starts_services': False,
            'permission_status': 'unchecked', 'helper': 'not_installed',
            'port_status': port_status(config)}


def build_scanner(root: Path, source: Path) -> str:
    """Build once for this runtime; source-only updates reuse identical bytes."""
    source_bytes = (source / 'native' / 'scanner' / 'main.c').read_bytes()
    inputs = hashlib.sha256(source_bytes + sys.base_prefix.encode()).hexdigest()
    receipt = root / 'scanner-build.json'
    executable = scanner_executable(root)
    if receipt.exists():
        previous = json.loads(receipt.read_text())
        if previous['inputs'] != inputs:
            raise ValueError('scanner_runtime_changed_requires_reviewed_replacement')
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        if digest != previous['sha256']:
            raise ValueError('scanner_identity_changed')
        return digest
    contents = executable.parent.parent
    executable.parent.mkdir(parents=True, mode=0o700, exist_ok=False)
    info = {'CFBundleIdentifier': 'local.machine-audit.scanner',
            'CFBundleName': 'Machine-audit Scanner', 'CFBundleExecutable': 'scanner',
            'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1',
            'LSUIElement': True, 'LSMinimumSystemVersion': '12.0'}
    _write(contents / 'Info.plist', plistlib.dumps(info))
    library = Path(sysconfig.get_config_var('LIBDIR'))
    version = f'{sys.version_info.major}.{sys.version_info.minor}'
    _command(['/usr/bin/clang', str(source / 'native' / 'scanner' / 'main.c'),
              '-o', str(executable), '-I' + sysconfig.get_paths()['include'],
              '-L' + str(library), '-lpython' + version, '-Wl,-rpath,' + str(library),
              '-DMACHINE_AUDIT_PYTHON_HOME=' + json.dumps(sys.base_prefix),
              '-mmacosx-version-min=12.0'], timeout=120)
    _command(['/usr/bin/codesign', '--sign', '-', '--identifier',
              'local.machine-audit.scanner', str(contents.parent)])
    _command(['/usr/bin/codesign', '--verify', '--strict', str(contents.parent)])
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    _write(receipt, json.dumps({'inputs': inputs, 'sha256': digest}).encode())
    return digest


def _snapshot(candidate: dict) -> None:
    target = Path(candidate['release'])
    if target.exists():
        raise ValueError('release_already_present_review_before_reusing')
    target.mkdir(parents=True, mode=0o700)
    archive = _command(['git', '-C', candidate['source'], 'archive', candidate['revision'],
                        'machine_audit', 'dashboard', 'native', 'pyproject.toml'])
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        for member in files:
            relative = Path(member.name)
            if relative.is_absolute() or '..' in relative.parts or not (member.isdir() or member.isfile()):
                raise ValueError('invalid_source_archive')
            destination = target / relative
            if member.isdir():
                destination.mkdir(mode=0o700, exist_ok=True)
            else:
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _write(destination, files.extractfile(member).read())


def job_plists(candidate: dict) -> dict[str, dict]:
    bootstrap = str(Path(candidate['release']) / 'machine_audit' / 'bootstrap.py')
    return {
        COLLECTOR: {'Label': COLLECTOR,
                    'ProgramArguments': [candidate['python'], '-I', bootstrap, '--config', candidate['config'], 'run'],
                    'RunAtLoad': True, 'KeepAlive': True, 'Disabled': True,
                    'ThrottleInterval': 30, 'Umask': 0o077},
        SCANNER: {'Label': SCANNER, 'ProgramArguments': [candidate['scanner']],
                  'StartInterval': 3600, 'ProcessType': 'Background', 'LowPriorityIO': True,
                  'Disabled': True, 'Nice': 10, 'Umask': 0o077},
    }


def install(config: Config, *, json_output=False) -> int:
    if sys.platform != 'darwin':
        from .installation_linux import install as linux
        return linux(config, json_output=json_output)
    try:
        candidate = plan(config)
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({'status': 'not_ready', 'reason': str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__}))
        return 2
    if json_output or not sys.stdin.isatty():
        print(json.dumps(candidate, indent=2))
        return 0
    print('Install this source revision with the selected Python runtime.')
    print(f"Application: {candidate['root']}\nRecordings: {config.root}")
    print('Creates a CLI and two background job definitions. Starts neither job.')
    print('Full Disk Access and the optional administrator helper are separate choices.')
    from .onboarding import _yes_no
    if not _yes_no('Install these files?', True):
        return 1
    try:
        _install_files(config, candidate)
    except (ValueError, RuntimeError, OSError) as exc:
        print('Installation stopped: ' + (str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__))
        return 2
    print('Installed; monitoring has not started.')
    print('Next: machine-audit start after the predecessor monitoring services are paused.')
    return 0


def _install_files(config: Config, candidate: dict) -> None:
    root, cli = Path(candidate['root']), Path(candidate['cli'])
    _private_directory(root)
    _private_directory(config.root)
    if not config.path.is_file():
        raise ValueError('run_setup_first')
    if (root / 'current.json').exists():
        raise ValueError('existing_installation_requires_reviewed_update')
    if cli.exists() or cli.is_symlink():
        raise ValueError('cli_path_already_exists')
    jobs = Path.home() / 'Library' / 'LaunchAgents'
    jobs.mkdir(parents=True, exist_ok=True)
    if jobs.is_symlink() or any((jobs / (label + '.plist')).exists() for label in candidate['jobs']):
        raise ValueError('job_path_already_exists')
    if scanner_executable(root).parent.parent.parent.exists() or Path(candidate['release']).exists():
        raise ValueError('unfinished_installation_requires_review')
    if next(root.glob('.prepare-*'), None) is not None:
        raise ValueError('unfinished_preparation_requires_review')
    # Build privately before publishing any launch job or permission identity.
    stage = root / ('.prepare-' + uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    staged = dict(candidate, release=str(stage / 'release'))
    _snapshot(staged)
    candidate['scanner_sha256'] = build_scanner(stage, Path(staged['release']))
    Path(candidate['release']).parent.mkdir(mode=0o700, exist_ok=True)
    os.rename(staged['release'], candidate['release'])
    os.rename(scanner_executable(stage).parent.parent.parent, scanner_executable(root).parent.parent.parent)
    os.rename(stage / 'scanner-build.json', root / 'scanner-build.json')
    stage.rmdir()  # Only this invocation's now-empty staging directory.
    for label, contents in job_plists(candidate).items():
        _write(jobs / (label + '.plist'), plistlib.dumps(contents))
    cli.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = [candidate['python'], '-I', str(Path(candidate['release']) / 'machine_audit' / 'bootstrap.py'),
               '--config', candidate['config']]
    _write(cli, ('#!/bin/sh\nexec ' + shlex.join(command) + ' "$@"\n').encode(), 0o700)
    config.source_options['scanner_identity_sha256'] = candidate['scanner_sha256']
    config.source_options['install_revision'] = candidate['revision']
    config.save()
    from .startup import StartupStatus
    StartupStatus(config)
    _write(root / 'current.json', json.dumps(candidate, indent=2).encode())


def _job_loaded(domain: str, label: str) -> bool:
    result = subprocess.run(['/bin/launchctl', 'print', domain + '/' + label],
                            capture_output=True, timeout=10)
    if result.returncode == 0:
        return True
    if result.returncode == 113 and b'Could not find service' in result.stderr:
        return False
    raise RuntimeError('service_state_unknown')


def _stop_jobs(domain: str, labels) -> list[str]:
    failures = []
    for label in labels:
        try:
            _command(['/bin/launchctl', 'disable', domain + '/' + label])
        except (OSError, RuntimeError, subprocess.SubprocessError):
            failures.append(label + ':disable')
        try:
            if _job_loaded(domain, label):
                _command(['/bin/launchctl', 'bootout', domain + '/' + label])
        except (OSError, RuntimeError, subprocess.SubprocessError):
            failures.append(label + ':stop')
    return failures


def control(config: Config, action: str) -> int:
    """Explicit CLI action; never called by install or setup."""
    if action not in {'start', 'stop'}:
        return 2
    if sys.platform != 'darwin':
        from .installation_linux import control as linux
        return linux(config, action)
    root = application_root()
    if not (root / 'current.json').is_file():
        print('Run machine-audit install first.')
        return 2
    candidate = json.loads((root / 'current.json').read_text())
    if candidate['config'] != str(config.path):
        print('This configuration does not belong to the installation.')
        return 2
    domain = f'gui/{os.getuid()}'
    jobs = Path.home() / 'Library' / 'LaunchAgents'
    if action == 'start':
        missing = [label for label in (COLLECTOR, SCANNER) if not _job_loaded(domain, label)]
        if not missing:
            print('Machine-audit jobs are already loaded. Run machine-audit doctor to check collection.')
            return 0
        print('Starts Machine-audit collection and its hourly scanner. GPU reads can interfere with other recorders.')
        from .onboarding import _yes_no
        if not sys.stdin.isatty() or not _yes_no('Are predecessor monitoring reads paused, and may monitoring start?', False):
            return 1
        touched = []
        try:
            for label in missing:
                touched.append(label)
                _command(['/bin/launchctl', 'enable', domain + '/' + label])
                _command(['/bin/launchctl', 'bootstrap', domain, str(jobs / (label + '.plist'))])
        except (OSError, RuntimeError, subprocess.SubprocessError):
            failures = _stop_jobs(domain, reversed(touched))
            if failures:
                print('Startup recovery incomplete: ' + ', '.join(failures))
            else:
                print('Startup failed; jobs started by this attempt are stopped and disabled.')
            raise RuntimeError('startup_recovery_incomplete' if failures else 'startup_failed') from None
        print('Machine-audit jobs submitted. Run machine-audit doctor to confirm collection.')
    else:
        failures = _stop_jobs(domain, (SCANNER, COLLECTOR))
        if failures:
            print('Stop incomplete: ' + ', '.join(failures))
            raise RuntimeError('stop_incomplete')
        print('Stopped Machine-audit. Recordings and predecessor services were preserved.')
    return 0


def check_access(config: Config) -> int:
    """Ask once, then run only the fixed permission probe through launchd."""
    if sys.platform != 'darwin' or not sys.stdin.isatty():
        print('Run machine-audit permissions in a terminal on the installed Mac.')
        return 2
    root = application_root()
    installed = json.loads((root / 'current.json').read_text())
    if installed['config'] != str(config.path):
        raise ValueError('installation_config_mismatch')
    executable = scanner_executable(root)
    if hashlib.sha256(executable.read_bytes()).hexdigest() != installed['scanner_sha256']:
        raise ValueError('scanner_identity_changed')
    print('In System Settings > Privacy & Security > Full Disk Access, add:')
    print(executable.parent.parent.parent)
    print('This grants the scanner access. Do not grant Terminal, the agent harness, or a general Python interpreter.')
    from .onboarding import _yes_no
    if not _yes_no('Check access now through that scanner? This reads one protected directory entry without saving its name.', False):
        return 1
    nonce = uuid.uuid4().hex
    request_path = config.root / 'scanner-request.json'
    request = {'action': 'check_access', 'nonce': nonce, 'expires_ns': time.time_ns() + 30_000_000_000}
    from .budget import Budget
    with Budget(config.root).reserve(8192):
        _write(request_path, json.dumps(request).encode())
    submitted = False
    try:
        _command(['/bin/launchctl', 'submit', '-l', PERMISSION, '--', str(executable), '--check-access'])
        submitted = True
        deadline = time.monotonic() + 15
        result_path = config.root / 'scanner-access.json'
        while time.monotonic() < deadline:
            if result_path.is_file() and not result_path.is_symlink():
                result = json.loads(result_path.read_text())
                if result.get('nonce') == nonce:
                    print('Protected directory check: ' + result['status'])
                    if result['status'] == 'unavailable':
                        print('The check directory is absent; this does not prove denial or a grant.')
                    return 0 if result['status'] == 'observed' else 1
            time.sleep(0.1)
        print('No permission result arrived. Monitoring was not started.')
        return 1
    finally:
        if submitted:
            # This transient job and request were created by this invocation.
            # The scanner and collector service definitions are untouched.
            subprocess.run(['/bin/launchctl', 'remove', PERMISSION], capture_output=True)
        if request_path.is_file() and json.loads(request_path.read_text()).get('nonce') == nonce:
            request_path.unlink()
