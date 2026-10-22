"""Linux sources. Each reads /proc, /sys and cgroup v2 files under one root: '/' in service."""
from __future__ import annotations

from machine_audit.model import Clock
from .. import GapSource, capacity_test, config_identity
from ..macos import ReadSource
from ..self_cost import ManagedProcesses, SelfCost
from .devices import Devices
from .host import LinuxHost, read_boot_id
from .network import LinuxNetwork, icmp_permitted


def boot_id(root='/'):
    return read_boot_id(root)


def sources(config, budget, operations=None, scanner=None, root='/'):
    salt = config.identity_salt()
    host = LinuxHost(root, salt)
    devices = Devices(host.root, salt)
    host.config_identity = config_identity(config)
    host.install_revision = config.source_options.get('install_revision')
    own = SelfCost(host, budget, operations, scanner, registry=ManagedProcesses(host.start('self')),
                   memory='process.resident_size')
    host.managed = own.registry
    icmp = icmp_permitted()
    network = LinuxNetwork(host.root, Clock.now, salt, icmp)
    cgroup_v2 = (host.cgroup / 'cgroup.controllers').exists()
    rapl = any(host.root.glob('sys/class/powercap/*/energy_uj'))
    return [
        ReadSource('linux.cpu', 'cpu', 1, host.cpu),
        ReadSource('linux.memory', 'memory', 1, host.memory),
        ReadSource('linux.workloads', 'cpu', 10, host.workloads) if cgroup_v2 else
        GapSource('linux.workloads', 'cpu', 10, 'cgroup_v2_required', 'unavailable'),
        ReadSource('linux.unit_limits', 'memory', 60, host.unit_limits) if cgroup_v2 else
        GapSource('linux.unit_limits', 'memory', 60, 'cgroup_v2_required', 'unavailable'),
        ReadSource('linux.disk', 'storage', 5, devices.disks),
        ReadSource('linux.capacity', 'storage', 60, devices.capacity),
        GapSource('linux.other_user_io', 'storage', 10, 'shipped_helper_required'),
        ReadSource('linux.network', 'network', 10, network.interfaces),
        ReadSource('linux.probes', 'network', 5, network.probes),
        ReadSource('linux.dns_connect', 'network', 60, network.dns_connect),
        ReadSource('linux.wifi', 'network', 10, network.wifi),
        ReadSource('linux.portal', 'network', 5, network.portal),
        capacity_test(config, budget, Clock.now, 'linux.capacity_test'),
        ReadSource('linux.temperatures', 'heat', 5, devices.temperatures),
        ReadSource('linux.fans', 'heat', 5, devices.fans),
        ReadSource('linux.thermal_zones', 'heat', 5, devices.thermal_zones),
        ReadSource('linux.cpu_frequency', 'heat', 5, devices.frequency),
        ReadSource('linux.power', 'power', 5, devices.power),
        ReadSource('linux.supply', 'power', 5, devices.supply),
        GapSource('linux.package_power', 'power', 30, 'shipped_helper_required') if rapl else
        GapSource('linux.package_power', 'power', 30, 'no_devices', 'unavailable'),
        ReadSource('linux.gpu', 'gpu', 5, devices.gpu),
        GapSource('linux.gpu_clients', 'gpu', 10, 'drm_fdinfo_not_implemented'),
        ReadSource('self.process', 'self', 10, own.processes),
        ReadSource('self.storage', 'self', 60, own.storage),
        ReadSource('self.scanner', 'self', 60, own.scanner_runs),
    ] + ([] if icmp else [GapSource('linux.icmp', 'network', 5, 'icmp_not_permitted')])
