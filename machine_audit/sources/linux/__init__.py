"""Linux sources. Each reads /proc, /sys and cgroup v2 files under one root: '/' in service."""
from __future__ import annotations

from .. import GapSource, config_identity
from ..macos import ReadSource
from ..self_cost import ManagedProcesses, SelfCost
from .host import LinuxHost, read_boot_id


def boot_id(root='/'):
    return read_boot_id(root)


def sources(config, budget, operations=None, scanner=None, root='/'):
    host = LinuxHost(root, config.identity_salt())
    host.config_identity = config_identity(config)
    host.install_revision = config.source_options.get('install_revision')
    own = SelfCost(host, budget, operations, scanner, registry=ManagedProcesses(host.start('self')),
                   memory='process.resident_size')
    host.managed = own.registry
    workloads = (ReadSource('linux.workloads', 'cpu', 10, host.workloads) if (host.cgroup / 'cgroup.controllers').exists()
                 else GapSource('linux.workloads', 'cpu', 10, 'cgroup_v2_required', 'unavailable'))
    return [
        ReadSource('linux.cpu', 'cpu', 1, host.cpu),
        ReadSource('linux.memory', 'memory', 1, host.memory),
        workloads,
        ReadSource('self.process', 'self', 10, own.processes),
        ReadSource('self.storage', 'self', 60, own.storage),
        ReadSource('self.scanner', 'self', 60, own.scanner_runs),
        GapSource('linux.other_user_io', 'storage', 10, 'shipped_helper_required'),
    ] + [GapSource('platform.' + c, c, 60, 'linux_sources_not_implemented', 'unavailable')
         for c in ('storage', 'network', 'gpu', 'power', 'heat')]
