"""Short local setup flow. Requested access is never proof of permission."""
from __future__ import annotations

from copy import deepcopy
import json
import os
import platform
from pathlib import Path
import stat
import sys

from .budget import free_space
from .config import Config


def observed_permissions(config_or_root: Config | Path) -> dict:
    """Read the scanner's last observation; do not run a permission probe."""
    result = {'scanner': {'status': 'unchecked'}, 'helper': {'status': 'not_enabled'}}
    root = config_or_root.root if isinstance(config_or_root, Config) else config_or_root
    path = root / 'scanner-access.json'
    try:
        info = path.lstat()
        if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 8192:
            return result
        record = json.loads(path.read_text())
        config = config_or_root if isinstance(config_or_root, Config) else Config.load(root / 'config.json')
        if record.get('executable_sha256') != config.source_options.get('scanner_identity_sha256'):
            return result
        status = {'observed': 'granted', 'denied': 'denied'}.get(record.get('status'), 'unchecked')
        result['scanner'] = {'status': status, 'observed_ns': record.get('observed_ns'),
                             'scope': 'protected_directory_probe', 'current_access': 'not_rechecked'}
    except (OSError, ValueError, TypeError):
        pass
    return result


def _permission(runtime: dict, name: str, requested: bool) -> dict:
    observation = runtime.get("permissions", {}).get(name, {})
    # Only the running component can report its permission, under its own identity.
    status = observation.get("status", "unchecked")
    if status not in {"granted", "denied", "unchecked", "not_enabled"}:
        status = "unchecked"
    return {"requested": requested, "status": status,
            "observed_ns": observation.get("observed_ns")}


def inspect(config: Config) -> dict:
    """Inspect configuration and the running service; never probe protected data."""
    from .client import ClientError, request
    from .startup import read as read_startup, port_status

    config.validate()
    runtime_ok = sys.version_info >= (3, 12)
    if not config.root.exists():
        directory = "not_created"
    elif config.root.is_symlink() or not config.root.is_dir():
        directory = "unsafe"
    else:
        info = config.root.stat()
        directory = "private" if info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & 0o077 else "not_private"
    try:
        runtime = request(config, "status")
        running = runtime.get("collector") is not None
        connection = "connected" if running else "not_running"
    except ClientError as exc:
        runtime = {}
        running = False
        connection = exc.code
    runtime.setdefault('permissions', observed_permissions(config))
    permissions = {
        "scanner": _permission(runtime, "scanner", bool(config.source_options.get("scan_protected_requested", False))),
        "helper": _permission(runtime, "helper", bool(config.source_options.get("helper_requested", False))),
    }
    actions = []
    if not runtime_ok:
        actions.append("Use Python 3.12 or newer.")
    if directory in {"unsafe", "not_private"}:
        actions.append("Choose a private data directory owned by this user.")
    if not running:
        actions.append("Install and start the collector, then run machine-audit doctor again.")
    for name, permission in permissions.items():
        if permission["requested"] and permission["status"] != "granted":
            if name == "scanner":
                actions.append("Grant Full Disk Access to the installed scanner, then confirm access through that scanner.")
            else:
                actions.append("The optional helper is not shipped yet; your choice is recorded for later.")
    collector = runtime.get("collector") or {}
    source_states = collector.get("sources", [])
    gpu_status = next((s.get('status') for s in source_states if s.get('source') == 'macos.gpu'), 'unobserved')
    gpu_configured = bool(config.enabled.get('gpu', False))
    gpu_pending = running and ((gpu_configured and gpu_status == 'not_enabled') or
                              (not gpu_configured and gpu_status in {'observed', 'measured_zero'}))
    if gpu_pending:
        actions.append('The saved GPU choice differs from live collection; it takes effect after a controlled restart.')
    startup = read_startup(config)
    port = 'collector_connected' if running else port_status(config)
    if not running and startup.get('state') == 'failed':
        actions.append('Last startup failed: ' + startup['code'] + '. Resolve it before starting again.')
    if not running and port != 'available':
        actions.append('The dashboard port is unavailable. Resolve the conflict before starting collection.')
    if running and not any(source.get("status") in {"observed", "measured_zero"} for source in source_states):
        actions.append("The collector has not confirmed any successful source readings; inspect machine-audit status.")
    if collector.get("save_error"):
        actions.append("Resolve the recording error shown by machine-audit status.")
    return {
        "schema_version": 1,
        "ready": runtime_ok and directory == "private" and running and not actions,
        "runtime": {"ok": runtime_ok, "python": platform.python_version(), "os": platform.system()},
        "data_directory": directory,
        "collector": connection,
        "sources": source_states,
        "gpu": {"configured": gpu_configured, "source_status": gpu_status, "active_after_restart": gpu_pending},
        "startup": startup,
        "port": port,
        "permissions": permissions,
        "storage": free_space(config.root),
        "dashboard": {"access": "tailscale" if config.tailscale_host else "local", "configured": config.tailscale_host is not None},
        "budget": runtime.get("budget"),
        "next_actions": actions,
    }


def _print_report(report: dict) -> None:
    print(f"Python {report['runtime']['python']}: {'ready' if report['runtime']['ok'] else 'upgrade needed'}")
    print(f"Collector: {report['collector']}; data directory: {report['data_directory']}")
    print(f"Free space: {report['storage']['free_bytes']:,} bytes; recordings need {report['storage']['required_free_bytes']:,}")
    for name, title in (("scanner", "Protected disk areas"), ("helper", "System process and power detail")):
        permission = report["permissions"][name]
        wanted = "requested" if permission["requested"] else "optional, not requested"
        print(f"{title}: {permission['status']} ({wanted})")
        if name == 'scanner' and permission['observed_ns']:
            print('  Last scanner check only; individual denied folders remain visible in the map.')
    for action in report["next_actions"]:
        print(f"Next: {action}")


def doctor(config: Config, *, json_output: bool = False) -> int:
    report = inspect(config)
    if json_output:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)
    return 0 if report["ready"] else 1


def _yes_no(prompt: str, default: bool) -> bool:
    while True:
        answer = input(f"{prompt} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        if not answer:
            return default
        if answer in {"yes", "y"}:
            return True
        if answer in {"no", "n"}:
            return False
        print("Enter yes or no.")


def setup(config: Config, *, json_output: bool = False) -> int:
    """Save choices only after confirmation. Installing is a separate explicit action."""
    if json_output or not sys.stdin.isatty():
        result = {"status": "decisions_needed", "saved": False,
                  "decisions": ["protected_disk_areas", "optional_helper", "exclusive_gpu_reader", "tailscale_hostname"],
                  "next_action": "Run machine-audit setup in a terminal to choose access and confirm settings."}
        print(json.dumps(result, indent=2) if json_output else result["next_action"])
        return 2
    print("Machine-audit setup")
    print("Records performance; never changes your workloads.")
    print("Storage limit: 10 GB; warning from 5 GB. Monitoring cost stays visible.")
    proposed = deepcopy(config)
    try:
        proposed.source_options["scan_protected_requested"] = _yes_no(
            "Include protected disk areas? Requires Full Disk Access for the scanner.",
            bool(config.source_options.get("scan_protected_requested", False)))
        proposed.source_options["helper_requested"] = _yes_no(
            "Record interest in system process and package power detail for later? The optional helper is not shipped yet.",
            bool(config.source_options.get("helper_requested", False)))
        proposed.enabled['scanner'] = True
        proposed.enabled['gpu'] = _yes_no(
            "Read GPU statistics? Other GPU recorders must be paused before starting.",
            bool(config.enabled.get('gpu', False)))
        current_host = config.tailscale_host or ""
        answer = input(f"Tailscale hostname [{current_host or 'local dashboard only'}]; '-' for local only: ").strip()
        proposed.tailscale_host = None if answer == "-" else (answer or current_host or None)
        proposed.validate()
        space = free_space(proposed.root)
        if space["free_bytes"] < space["required_free_bytes"]:
            print(f"Not enough free space for recordings: {space['required_free_bytes']:,} bytes required on the "
                  f"data directory's filesystem, {space['free_bytes']:,} available. Nothing was saved.")
            return 2
        print(f"Dashboard: {proposed.tailscale_host or 'this machine only'}")
        print(f"Private recordings: {proposed.root}")
        print("Access choices are requests. The running scanner must confirm permission; the helper is not shipped yet, so its choice is recorded for later.")
        if not _yes_no("Save these choices?", True):
            print("Cancelled; no settings changed.")
            return 1
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled; no settings changed.")
        return 1
    except ValueError:
        print("Invalid settings. Use a bare Tailscale hostname ending in .ts.net; nothing was saved.")
        return 2
    proposed.save()
    # The caller may continue with the same validated configuration instance.
    config.__dict__.update(proposed.__dict__)
    print("Saved. Next: machine-audit install, then machine-audit doctor.")
    print("Installation will show its changes before it applies them.")
    return 0
