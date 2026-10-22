"""Linux sources read a bundle of named files written under a temporary root.
Formats follow proc(5) and the cgroup v2 documentation; captured bundles replace the synthetic one."""
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest

from machine_audit.budget import Budget
from machine_audit.collector import Collector
from machine_audit.config import Config
from machine_audit.derive import points, source_fields
from machine_audit.model import Clock, Field, Reading, Record
from machine_audit.query import Query
from machine_audit.sources.linux import sources
from machine_audit.sources.linux.devices import Devices
from machine_audit.sources.linux.host import LinuxHost
from machine_audit.sources.linux.network import LinuxNetwork, TcpProber
from machine_audit.sources.macos import ReadSource
from machine_audit.sources.self_cost import ManagedProcesses, SelfCost
from machine_audit.store import Store

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
        self.assertNotIn('memory.SyntheticFutureKey', memory)  # meminfo keeps the documented subset
        self.assertEqual(memory['memory.Zswap'].status, 'unavailable')  # a kept key this kernel lacks
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
        def derived(first, second):
            records = [Record(Reading('host', 'cpu', Clock(t, t, t), Clock(t, t, t), tuple(f)), 'boot', 'run', n, t, Clock(t, t, t))
                       for n, t, f in ((1, 10**9, first), (2, 2 * 10**9, second))]
            found = points(records)
            return (next(p['total'] for p in found if p['signal'] == 'cpu.busy' and p['total'] is not None),
                    next(p['value'] for p in reversed(found) if p['signal'] == 'cpu.capacity'))

        def mac(user, system, idle, nice):
            return [Field('cpu.' + s, v, 'ticks', 'counter', entity='cpu:0', additive=True, scale=0.01)
                    for s, v in (('user', user), ('system', system), ('idle', idle), ('nice', nice))]
        first = list(self.host.cpu())
        self.edit('proc/stat', 'cpu  4705 356 584 3699176 2011 0 128 0 0 0', 'cpu  4715 357 589 3699276 2061 2 131 7 4 0')
        # Host totals come from the aggregate line every second; capacity is the online CPU count.
        self.assertEqual(derived(first, list(self.host.cpu())), (10 + 1 + 5 + 2 + 3, 2))
        self.assertEqual(derived(mac(100, 50, 900, 4), mac(110, 55, 1000, 5)), (10 + 5 + 1, 1))
        # Per-CPU states (every 10 s) are detail and derive no second host busy series.
        per_cpu = list(self.host.per_cpu())
        self.assertEqual({f.entity for f in per_cpu}, {'cpu:0', 'cpu:1'})
        self.assertFalse(any(f.name == 'cpu.busy' for f in source_fields(Reading('linux.per_cpu', 'cpu', Clock(1, 1, 1), Clock(1, 1, 1), tuple(per_cpu)))))

    def test_who_ranks_unit_consumption_not_limits_coverage_or_pressure(self):
        # First Linux run: `who --component cpu` ranked unit.cpu.weight and unit.processes first.
        data = self.root / 'data'
        data.mkdir()
        store = Store(data / 'readings.sqlite', Budget(data, limit_bytes=20_000_000, free_margin_bytes=0))
        store.start_run('run', 'boot', 0)
        collector = Collector(store, run_id='run', boot_id='boot')
        start = Clock.now().wall_ns
        collector.ingest(ReadSource('linux.workloads', 'cpu', 10, self.host.workloads).read())
        collector.ingest(ReadSource('linux.unit_limits', 'memory', 60, self.host.unit_limits).read())
        self.edit('sys/fs/cgroup/system.slice/ssh.service/cpu.stat', 'usage_usec 600000', 'usage_usec 900000')
        collector.ingest(ReadSource('linux.workloads', 'cpu', 10, self.host.workloads).read())
        try:
            rows = Query(store, collector).who(start - 10**9, Clock.now().wall_ns + 10**9, component='cpu')['rows']
        finally:
            store.close()
        signals = {r['signal'] for r in rows}
        self.assertIn('unit.cpu.stat.usage_usec', signals)
        # First Linux re-run: a process.threads gauge filled 38 of the top 40 CPU rows.
        self.assertTrue(all(r['additive'] for r in rows), signals)
        # A unit ranks as itself; programs group processes only, so no unit is counted twice.
        units = [r for r in rows if r['scope'] == 'unit']
        self.assertTrue(units and all(r['entity'].startswith('cgroup:') for r in units))
        # Each workload ranks once, by its total; user and system time need --signal.
        self.assertEqual(len(rows), len({r['entity'] for r in rows}), signals)
        self.assertLessEqual(signals, {'unit.cpu.stat.usage_usec', 'process.cpu'})
        self.assertFalse(signals & {'unit.cpu.weight', 'unit.cpu.max.quota', 'unit.processes', 'unit.cpu.pressure.some',
                                    'unit.cpu.stat.nr_periods'})

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
        volume = next(f.entity for f in Devices(self.root, b'0' * 64).capacity() if f.status == 'observed')
        self.assertRegex(volume, r'\Avolume:[0-9a-f]{16}\Z')
        self.assertNotIn(f'{os.statvfs(self.root).f_fsid:x}', volume)  # the filesystem id is salted
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
        self.assertEqual(sorted(f.label for f in temperatures if f.name == 'heat.temperature' and f.status == 'observed'),
                         ['amdgpu edge', 'k10temp Tctl', 'mt7921_phy0 temp1', 'nvme Composite', 'nvme Composite', 'nvme Sensor 1'])
        self.assertEqual(len({f.entity for f in temperatures if f.label == 'nvme Composite'}), 3)
        # First Linux run: nvme drivers report -273150 for no reading and 65261850 for no limit.
        sentinels = [(f.name, f.value, f.status) for f in temperatures if f.value is None]
        self.assertEqual(sorted(sentinels), [('heat.temperature', None, 'unavailable'), ('heat.temperature_max', None, 'unavailable')])
        tctl = next(f for f in temperatures if f.label == 'k10temp Tctl')
        self.assertEqual((tctl.value, tctl.unit), (45.125, 'degC'))  # hwmon millidegrees
        power = by_name(devices.power())['power.average']
        self.assertEqual((power.label, power.value, power.unit), ('amdgpu PPT', 12.345, 'watts'))  # hwmon microwatts
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
        made = {s.name: s for s in sources(config, Budget(config.root, limit_bytes=20_000_000, free_margin_bytes=0), root=self.root)}
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
