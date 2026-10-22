"""Linux sources read a bundle of named files written under a temporary root.
Formats follow proc(5) and the cgroup v2 documentation; captured bundles replace the synthetic one."""
import os
from pathlib import Path
import re
import tempfile
import unittest

from machine_audit.budget import Budget
from machine_audit.config import Config
from machine_audit.derive import points
from machine_audit.model import Clock, Field, Reading, Record
from machine_audit.sources.linux import sources
from machine_audit.sources.linux.host import LinuxHost
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
        self.assertEqual(sorted(units.values()), ['getty.service', 'init.scope', 'machine-audit.service',
                                                  'ssh.service', 'user.service', 'worker.service'])
        self.assertTrue(all(re.fullmatch(r'cgroup:[0-9a-f]{16}:\d+', e) for e in units))
        user = next(e for e, label in units.items() if label == 'user.service')
        self.assertEqual(by_name(fields, user)['unit.processes'].value, 1)  # held in a nested app.slice
        getty = by_name(fields, next(e for e, label in units.items() if label == 'getty.service'))
        self.assertNotIn('unit.memory.current', getty)  # no memory controller: absent, not zero
        text = repr(fields)
        for private in ('slice', 'tty1', '@', '/'):
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

    def test_linux_factory_covers_every_component_with_readings_or_named_gaps(self):
        config = Config(data_dir=str(self.root / 'data'))
        made = sources(config, Budget(config.root), root=self.root)
        readings = {s.name: s.read() for s in made}
        self.assertEqual({s.component for s in made}, {'cpu', 'memory', 'gpu', 'storage', 'network', 'power', 'heat', 'self'})
        for name in ('linux.cpu', 'linux.memory', 'linux.workloads', 'self.process'):
            self.assertEqual(readings[name].status, 'observed', name)
        self.assertEqual((readings['platform.heat'].status, readings['platform.heat'].reason),
                         ('unavailable', 'linux_sources_not_implemented'))
        self.assertEqual(readings['linux.other_user_io'].reason, 'shipped_helper_required')


if __name__ == '__main__':
    unittest.main()
