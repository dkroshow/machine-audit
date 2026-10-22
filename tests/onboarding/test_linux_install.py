"""Linux installation: unit files carry the run model agreed with the first user, service
control needs root, and setup asks only what applies on Linux."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from machine_audit.config import Config
from machine_audit.installation_linux import COLLECTOR, SCANNER, TIMER, control, unit_files
from machine_audit.onboarding import setup
from test_onboarding import REQUIRED_FREE, free_bytes

CANDIDATE = {'python': '/usr/bin/python3.12', 'release': '/var/lib/monitor/.local/lib/machine-audit/releases/abc123',
             'config': '/var/lib/monitor/.local/share/machine-audit/config.json',
             'data': '/var/lib/monitor/.local/share/machine-audit', 'user': 'monitor'}


class LinuxInstallTests(unittest.TestCase):
    def test_units_run_as_the_service_user_with_only_the_data_directory_writable(self):
        units = unit_files(CANDIDATE)
        for name, verb in ((COLLECTOR, 'run'), (SCANNER, 'scan')):
            lines = units[name].splitlines()
            for line in ('User=monitor', 'ProtectSystem=strict', 'ReadWritePaths=' + CANDIDATE['data'],
                         'NoNewPrivileges=yes', 'PrivateTmp=yes', 'UMask=0077'):
                self.assertIn(line, lines, name)
            self.assertIn(f"ExecStart=/usr/bin/python3.12 -I {CANDIDATE['release']}/machine_audit/bootstrap.py "
                          f"--config {CANDIDATE['config']} {verb}", lines)
        self.assertIn('IOSchedulingClass=idle', units[SCANNER].splitlines())
        self.assertIn('OnCalendar=hourly', units[TIMER].splitlines())
        with self.assertRaisesRegex(ValueError, 'unit_path_needs_review'):
            unit_files(dict(CANDIDATE, data='/var/lib/monitor/my data'))  # systemd would split it

    def test_service_control_without_root_prints_the_commands_and_runs_nothing(self):
        output = io.StringIO()
        with patch('machine_audit.installation_linux.os.geteuid', return_value=1000), \
             patch('machine_audit.installation_linux.subprocess.run') as run, redirect_stdout(output):
            self.assertEqual(control(Config(), 'start'), 2)
        run.assert_not_called()
        self.assertIn('sudo systemctl enable --now machine-audit.service machine-audit-scan.timer', output.getvalue())

    def test_setup_asks_no_mac_access_questions_and_maps_folders(self):
        with tempfile.TemporaryDirectory() as parent:
            config = Config(data_dir=str(Path(parent) / 'private'))
            # Helper interest, dashboard hosts, save: no protected-area or GPU-reader question.
            with patch('sys.platform', 'linux'), patch('sys.stdin.isatty', return_value=True), \
                 patch('builtins.input', side_effect=['no', '', 'yes']), redirect_stdout(io.StringIO()), free_bytes(REQUIRED_FREE):
                self.assertEqual(setup(config), 0)
            saved = Config.load(config.path)
            self.assertEqual((saved.enabled, saved.source_options['scanner_grain']), ({'scanner': True}, 'folders'))
            self.assertNotIn('scan_protected_requested', saved.source_options)


if __name__ == '__main__':
    unittest.main()
