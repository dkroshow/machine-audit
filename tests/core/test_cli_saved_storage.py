"""CLI regressions for saved scanner answers when no collector is running."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import machine_audit
from machine_audit.budget import Budget
from machine_audit.config import Config
from machine_audit.model import Clock, Field, Reading, Record
from machine_audit.scanner.accounting import Blocks
from machine_audit.scanner.native import Volume
from machine_audit.scanner.store import MapStore
from machine_audit.scanner.walker import Node
from machine_audit.store import Store

NS = 1_000_000_000
GB = 1_000_000_000


class SavedStorageCliTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent / '.tmp'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config(data_dir=str(self.root))
        self.config.save()
        # A small cap keeps the fixture independent of the checkout's free space.
        budget = Budget(self.root, limit_bytes=64 * 1024**2, free_margin_bytes=0)
        readings = Store(self.config.store_path, budget)
        for sequence, value in enumerate((0, 7), 1):
            clock = Clock(sequence * NS, sequence * NS, sequence * NS)
            reading = Reading('synthetic.cpu', 'cpu', clock, clock,
                              (Field('cpu.counter', value, 'seconds', 'counter'),))
            readings.append([Record(reading, 'boot', 'run', sequence,
                                    clock.wall_ns, clock)])
        readings.close()
        scanner = MapStore(self.root / 'scanner.sqlite', budget)
        try:
            for volume_id, label in (('volume-a', 'A decoy'), ('volume-b', 'B target')):
                volume = Volume(volume_id, label, str(self.root), 'hfs', 1,
                                100 * GB, 50 * GB, 50 * GB, 50 * GB)
                for seconds, extra in ((100, 0), (200, 2 * GB), (300, 9 * GB)):
                    with patch('machine_audit.scanner.store.time.time_ns', return_value=seconds * NS):
                        scanner.prepare(volume, full=True)
                        prefix = volume_id[-1]
                        for node in (
                            Node(prefix + '-root', None, 'Root', 'directory', 0, 0, 'observed', None, 0),
                            Node(prefix + '-parent', prefix + '-root', 'Parent', 'directory', 0, 0, 'observed', None, 0),
                            Node(prefix + '-child-1', prefix + '-parent', 'Child one', 'directory', 2 * GB + extra, 0, 'observed', None, 0),
                            Node(prefix + '-child-2', prefix + '-parent', 'Child two', 'directory', 3 * GB + extra, 0, 'observed', None, 0),
                        ):
                            scanner.directory(node)
                        scanner.finish_volume(volume, Blocks(), [], full=True, lean=True)
        finally:
            scanner.close()
        # No token file: the real client takes its not-running path without a
        # socket attempt, so these tests cannot accidentally reach a live server.
        self.assertFalse(self.config.token_path.exists())

    def cli(self, *arguments):
        runtime = Path(machine_audit.__file__).resolve().parent.parent
        env = dict(os.environ, PYTHONPATH=str(runtime))
        result = subprocess.run(
            [sys.executable, '-m', 'machine_audit', '--config', str(self.config.path),
             *arguments, '--json'],
            cwd=runtime, env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_offline_children_keeps_volume_parent_page_size_and_cursor(self):
        first = self.cli('storage', '--action', 'children', '--volume-id', 'volume-b',
                         '--parent-id', 'b-parent', '--limit', '1')
        self.assertEqual([row['id'] for row in first['rows']], ['b-child-2'])
        self.assertTrue(first['truncated'])
        second = self.cli('storage', '--action', 'children', '--volume-id', 'volume-b',
                          '--parent-id', 'b-parent', '--limit', '1',
                          '--cursor', first['next_cursor'])
        self.assertEqual([row['id'] for row in second['rows']], ['b-child-1'])
        self.assertFalse(second['truncated'])

    def test_offline_growth_keeps_requested_interval_limit_and_cursor(self):
        options = ('storage', '--action', 'growth', '--volume-id', 'volume-b',
                   '--start', '1970-01-01T00:01:40Z',
                   '--end', '1970-01-01T00:03:20Z', '--limit', '1')
        first = self.cli(*options)
        self.assertEqual(len(first['rows']), 1)
        self.assertTrue(first['truncated'])
        second = self.cli(*options, '--cursor', first['next_cursor'])
        self.assertEqual(len(second['rows']), 1)
        self.assertNotEqual(first['rows'][0]['id'], second['rows'][0]['id'])
        for row in first['rows'] + second['rows']:
            self.assertTrue(row['id'].startswith('b-'))
            self.assertEqual((row['start_ns'], row['end_ns']), (100 * NS, 200 * NS))
            self.assertEqual(row['change'], 2 * GB)

    def test_offline_recording_series_still_uses_requested_arguments(self):
        result = self.cli('series', '--component', 'cpu', '--signals', 'cpu.counter',
                          '--start', '0', '--end', '3', '--limit', '3')
        self.assertEqual(result['coverage']['mode'], 'store_only')
        self.assertEqual(sum(row['total'] or 0 for row in result['rows']), 7)
        self.assertLessEqual(len(result['rows']), 3)


if __name__ == '__main__':
    unittest.main()
