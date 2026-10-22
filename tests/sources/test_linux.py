"""Linux sources read a bundle of named files written under a temporary root.
Formats follow proc(5) and the cgroup v2 documentation; captured bundles replace the synthetic one."""
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest

from machine_audit.budget import Budget
from machine_audit.config import Config
from machine_audit.derive import points
from machine_audit.model import Clock, Field, Reading, Record
from machine_audit.sources.linux import sources
from machine_audit.sources.linux.devices import Devices
from machine_audit.sources.linux.host import LinuxHost
from machine_audit.sources.linux.network import LinuxNetwork, TcpProber
from machine_audit.sources.macos import ReadSource
from machine_audit.sources.self_cost import ManagedProcesses, SelfCost

BUNDLE = Path(__file__).parent / 'linux' / 'synthetic.txt'
SECTION = re.compile(r'^==> (/.+) <==\n', re.M)


def materialize(bundle, root):
    """Write each named section of a bundle as a file under root."""
    parts = SECTION.split(Path(bundle).read_text())
    for path, text in zip(parts[1::2], parts[2::2]):
        target = Path(root) / path.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.strip('\n') + '\n' if text.strip() else '')


def by_name(fields, entity=None):
    return {f.name: f for f in fields if entity is None or f.entity == entity}


def pid(field):
    return int(field.entity.split(':')[2]) if field.entity.startswith('process:') else None


class LinuxSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        materialize(BUNDLE, self.root)
        self.host = LinuxHost(self.root, b'0' * 64)

    def tearDown(self):
        self.tmp.cleanup()

    def edit(self, rel, old, new):
        path = self.root / rel
        path.write_text(path.read_text().replace(old, new, 1))

    def test_keyed_files_parse_by_key_when_a_kernel_adds_or_moves_keys(self):
        # Kernel 7.0 adds memory.stat and vmstat keys; the bundle leads each file with an unknown key.
        memory = by_name(self.host.memory())
        self.assertEqual(memory['memory.MemTotal'].value, 8140988 * 1024)
        self.assertEqual(memory['memory.Active_anon'].value, 1205312 * 1024)
        self.assertEqual((memory['memory.HugePages_Total'].unit, memory['memory.HugePages_Total'].value), ('count', 0))
        self.assertEqual(memory['memory.pgfault'].value, 902211)
        self.assertEqual((memory['memory.pgpgin'].unit, memory['memory.pgpgin'].value), ('bytes', 1048576 * 1024))
        self.assertEqual(memory['memory.zswpin'].status, 'unavailable')
        self.assertEqual(memory['memory.pressure.full'].value, 1200)
        units = [f for f in self.host.workloads() if f.label == 'ssh.service']
        stat = by_name(units)
        self.assertEqual(stat['unit.memory.stat.anon'].value, 1536000)
        self.assertEqual(stat['unit.memory.stat.file'].value, 3072000)
        self.assertEqual(stat['unit.cpu.stat.usage_usec'].value, 600000)
        self.assertFalse(any('synthetic' in f.name for f in units))

    def test_units_are_one_non_overlapping_level_hashed_and_named_by_template(self):
        fields = self.host.workloads()
        units = {f.entity: f.label for f in fields if f.scope == 'unit'}
        # mnt-ProjectX.mount names a path. The host name 'ubuntu' also redacts the legitimate
        # ubuntu-advantage.service: the known cost of the name filter.
        self.assertEqual(sorted(units.values()), ['getty.service', 'init.scope', 'machine-audit.service', 'mount',
                                                  'redacted', 'ssh.service', 'user.service', 'worker.service'])
        self.assertTrue(all(re.fullmatch(r'cgroup:[0-9a-f]{16}:\d+', e) for e in units))
        user = next(e for e, label in units.items() if label == 'user.service')
        self.assertEqual(by_name(fields, user)['unit.processes'].value, 1)  # held in a nested app.slice
        getty = by_name(fields, next(e for e, label in units.items() if label == 'getty.service'))
        self.assertNotIn('unit.memory.current', getty)  # no memory controller: absent, not zero
        text = repr(fields)
        for private in ('slice', 'tty1', '@', '/', 'ProjectX', 'ubuntu', '3f2a7c1e'):  # last: the raw boot id
            self.assertNotIn(private, text)

    def test_unit_samples_top_cpu_since_last_pass_largest_resident_and_own_processes(self):
        self.host.managed = ManagedProcesses(self.host.start('self'))
        self.host.managed.register(5007, 6007, 'scanner')

        def sampled():
            return {pid(f) for f in self.host.workloads() if f.name == 'process.user_time' and pid(f) > 5000}
        # First pass ranks by lifetime CPU; 5006 has the largest resident set; 5007 is ours.
        self.assertEqual(sampled(), {5001, 5002, 5003, 5006, 5007})
        self.edit('proc/5004/stat', ' 5 5 0 0 ', ' 505 5 0 0 ')
        self.edit('proc/5005/stat', ' 3 2 0 0 ', ' 303 2 0 0 ')
        self.edit('proc/5001/stat', ' 600 300 0 0 ', ' 700 300 0 0 ')
        self.assertEqual(sampled(), {5004, 5005, 5001, 5006, 5007})
        worker = by_name([f for f in self.host.process_latest.fields if f.label == 'worker.service'])
        self.assertEqual((worker['unit.processes'].value, worker['unit.processes_sampled'].value), (7, 5))
        own = by_name(SelfCost(self.host, None, registry=self.host.managed, memory='process.resident_size').processes())
        self.assertEqual((own['self.memory'].label, own['self.memory'].value), ('scanner', 8000 * 1024))
        self.assertEqual(own['self.write'].value, 8192 * 5007)

    def test_denied_or_missing_files_are_gaps_and_unreadable_mandatory_files_fail(self):
        os.chmod(self.root / 'proc/5001/io', 0)  # another user's process
        (self.root / 'proc/pressure/cpu').unlink()  # PSI not built in
        process = by_name(f for f in self.host.workloads() if pid(f) == 5001)
        self.assertIn('process.user_time', process)
        self.assertNotIn('process.diskio_byteswritten', process)
        self.assertEqual(by_name(self.host.cpu())['cpu.pressure.some'].status, 'unavailable')
        (self.root / 'proc/meminfo').unlink()
        reading = ReadSource('linux.memory', 'memory', 1, self.host.memory).read()
        self.assertEqual((reading.status, reading.reason, reading.fields), ('failed', 'native_read_failed', ()))

    def test_busy_cpu_is_user_and_system_states_and_mac_busy_is_unchanged(self):
        # cpu-view.md: Linux nice is user time and irq/softirq are system time; iowait,
        # steal and guest (already inside user) are not added. macOS keeps user+system+nice.
        def busy(first, second):
            records = [Record(Reading('host', 'cpu', Clock(t, t, t), Clock(t, t, t), tuple(f)), 'boot', 'run', n, t, Clock(t, t, t))
                       for n, t, f in ((1, 10**9, first), (2, 2 * 10**9, second))]
            return next(p['total'] for p in points(records) if p['signal'] == 'cpu.busy' and p['total'] is not None)

        def mac(user, system, idle, nice):
            return [Field('cpu.' + s, v, 'ticks', 'counter', entity='cpu:0', additive=True, scale=0.01)
                    for s, v in (('user', user), ('system', system), ('idle', idle), ('nice', nice))]
        first = list(self.host.cpu())
        self.edit('proc/stat', 'cpu0 2342 170 300 1849500 1000 0 100 0 0 0',
                  'cpu0 2352 171 305 1849600 1050 2 103 7 4 0')
        self.assertEqual(busy(first, list(self.host.cpu())), 10 + 1 + 5 + 2 + 3)
        self.assertEqual(busy(mac(100, 50, 900, 4), mac(110, 55, 1000, 5)), 10 + 5 + 1)

    def test_unit_limit_max_means_no_limit_not_a_number(self):
        limits = list(self.host.unit_limits())
        ssh = by_name(f for f in limits if f.label == 'ssh.service')
        worker = by_name(f for f in limits if f.label == 'worker.service')
        self.assertEqual((ssh['unit.memory.max'].value, ssh['unit.memory.max'].status), (None, 'not_enabled'))
        self.assertEqual((ssh['unit.cpu.max.quota'].status, ssh['unit.cpu.max.period'].value), ('not_enabled', 100000))
        self.assertEqual((worker['unit.memory.max'].value, worker['unit.cpu.max.quota'].value), (1073741824, 50000))

    def test_disks_are_whole_physical_devices_and_unit_io_counts_a_request_once(self):
        # A request through LVM appears on dm-0 and on the nvme disk beneath it.
        disks = list(Devices(self.root, b'0' * 64).disks())
        self.assertEqual({f.entity for f in disks if f.name == 'disk.read'}, {'disk:259:0', 'disk:259:3'})
        first = by_name(disks, 'disk:259:0')
        self.assertEqual(first['disk.read'].value, 80000 * 512)  # diskstats sectors are 512 bytes
        self.assertEqual((first['disk.flush_operations'].value, first['disk.in_progress'].kind), (9, 'gauge'))
        self.assertEqual(by_name(disks)['io.pressure.full'].value, 41000)
        ssh = by_name(f for f in self.host.workloads() if f.label == 'ssh.service')
        self.assertEqual((ssh['unit.io.stat.rbytes'].value, ssh['unit.io.pressure.some'].component), (4096, 'storage'))

    def test_interfaces_are_indexes_down_links_are_states_and_addresses_stay_private(self):
        network = LinuxNetwork(self.root, Clock.now, b'0' * 64, icmp=False)
        fields = list(network.interfaces()) + list(network.wifi())
        self.assertEqual({f.entity for f in fields if f.name == 'network.receive'},
                         {'interface:2', 'interface:3', 'interface:4', 'interface:5'})  # loopback skipped
        down = by_name(fields, 'interface:3')
        self.assertEqual((down['network.link_state'].value, down['network.link_state'].status), ('down', 'observed'))
        self.assertEqual(down['network.link_speed'].status, 'unavailable')
        self.assertEqual(by_name(fields)['network.tcp.RetransSegs'].value, 25)
        # Gateway 0101A8C0 is little-endian; the lower metric of two default routes wins.
        self.assertEqual(network.path(), {'Router': '192.168.1.1', 'PrimaryInterface': 'eth0'})
        session = network.session_id('192.168.1.1')
        self.assertRegex(session, r'\A[0-9a-f]{16}\Z')
        router = TcpProber().ping_many({'router': '192.168.1.1'})['router']
        self.assertEqual((router.outcome, router.rtt_ns), ('icmp_not_permitted', None))
        text = repr(fields) + session
        for private in ('eth0', 'wlan0', '192.168', '02:00:5e', '025e'):
            self.assertNotIn(private, text)

    def test_heat_power_and_gpu_come_from_named_chips_and_a_suspended_gpu_is_not_read(self):
        devices = Devices(self.root, b'0' * 64)
        temperatures = list(devices.temperatures())
        self.assertEqual(sorted(f.label for f in temperatures if f.name == 'heat.temperature'),
                         ['amdgpu edge', 'k10temp Tctl', 'mt7921_phy0 temp1', 'nvme Composite', 'nvme Composite'])
        self.assertEqual(len({f.entity for f in temperatures if f.label == 'nvme Composite'}), 2)
        tctl = next(f for f in temperatures if f.label == 'k10temp Tctl')
        self.assertEqual((tctl.value, tctl.unit), (45.125, 'degC'))  # hwmon millidegrees
        power = by_name(devices.power())['power.average']
        self.assertEqual((power.label, power.value, power.unit), ('amdgpu PPT', 12345000, 'uW'))
        gpu = by_name(devices.gpu())
        self.assertEqual((gpu['gpu.utilization'].value, gpu['gpu.vram_used'].value), (7, 536870912))
        for status in ('sys/class/drm/card1/device/power/runtime_status', 'sys/class/hwmon/hwmon1/device/power/runtime_status'):
            (self.root / status).write_text('suspended\n')
        os.chmod(self.root / 'sys/class/drm/card1/device/gpu_busy_percent', 0)  # a read would fail the test
        self.assertEqual([f.name for f in devices.gpu()], ['gpu.runtime_status'])
        edge = next(f for f in devices.temperatures() if f.label == 'amdgpu edge')
        self.assertEqual((edge.value, edge.status), (None, 'unobserved'))

    def test_host_without_sensors_gpu_or_cpufreq_covers_every_component_with_named_gaps(self):
        for absent in ('sys/class/hwmon', 'sys/class/drm', 'sys/devices/system/cpu'):
            shutil.rmtree(self.root / absent)
        config = Config(data_dir=str(self.root / 'data'))
        made = {s.name: s for s in sources(config, Budget(config.root), root=self.root)}
        self.assertEqual({s.component for s in made.values()},
                         {'cpu', 'memory', 'gpu', 'storage', 'network', 'power', 'heat', 'self'})
        # Probes and the resolver check send packets; tests never contact a network.
        readings = {name: s.read() for name, s in made.items() if name not in ('linux.probes', 'linux.dns_connect')}
        for name in ('linux.cpu', 'linux.memory', 'linux.workloads', 'linux.disk', 'linux.network', 'self.process'):
            self.assertEqual(readings[name].status, 'observed', name)
        for name in ('linux.temperatures', 'linux.fans', 'linux.thermal_zones', 'linux.power', 'linux.gpu', 'linux.cpu_frequency'):
            self.assertEqual((readings[name].status, readings[name].reason), ('unavailable', 'no_devices'), name)
        self.assertEqual(readings['linux.other_user_io'].reason, 'shipped_helper_required')
        self.assertEqual(readings['linux.gpu_clients'].status, 'not_enabled')


if __name__ == '__main__':
    unittest.main()
