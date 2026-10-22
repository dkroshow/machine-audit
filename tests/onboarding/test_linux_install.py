"""Linux installation: unit files carry the run model agreed with the first user, service
control needs root, and setup asks only what applies on Linux."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from machine_audit.cli import main
from machine_audit.config import Config
from machine_audit.installation_linux import COLLECTOR, SCANNER, TIMER, control, install, unit_files
from machine_audit.onboarding import doctor, map_timer, setup
import subprocess
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
                         'NoNewPrivileges=yes', 'PrivateTmp=yes', 'UMask=0077', 'OOMPolicy=stop'):
                self.assertIn(line, lines, name)
            # -B: the release is read-only to the unit, so no bytecode is written there.
            self.assertIn(f"ExecStart=/usr/bin/python3.12 -I -B {CANDIDATE['release']}/machine_audit/bootstrap.py "
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

    def test_unattended_setup_takes_its_answers_as_flags_and_asks_nothing(self):
        # First Ubuntu install: setup needed a terminal, which a nologin service user cannot open.
        with tempfile.TemporaryDirectory() as parent:
            path = Path(parent) / 'private' / 'config.json'
            argv = ['--config', str(path), 'setup', '--non-interactive', '--helper', 'yes',
                    '--hosts', 'dash.example.org', '--exclude', '/srv/private', '--exclude', '/home']
            with patch('sys.platform', 'linux'), patch('sys.stdin.isatty', return_value=False), \
                 patch('builtins.input', side_effect=AssertionError('asked')), redirect_stdout(io.StringIO()), free_bytes(REQUIRED_FREE):
                self.assertEqual(main(argv), 0)
                saved = Config.load(path)
                self.assertEqual((saved.source_options['helper_requested'], saved.allowed_hosts, saved.scanner_exclusions,
                                  saved.source_options['scanner_grain']), (True, ['dash.example.org'], ['/srv/private', '/home'], 'folders'))
                path.unlink()
                self.assertEqual(main(argv[:-1] + ['relative/path']), 2)  # refused, nothing saved
                self.assertFalse(path.exists())

    def test_install_yes_writes_the_installation_without_a_terminal(self):
        with tempfile.TemporaryDirectory() as parent:
            config = Config(data_dir=str(Path(parent) / 'data'))
            config.save()
            root = Path(parent) / 'app'
            candidate = dict(CANDIDATE, release=str(root / 'releases' / 'abc123'), root=str(root), config=str(config.path),
                             data=str(config.root), revision='abc123', source=parent, cli=str(Path(parent) / 'bin' / 'machine-audit'),
                             units={name: str(root / 'units' / name) for name in (COLLECTOR, SCANNER, TIMER)})
            with patch('machine_audit.installation_linux.plan', return_value=candidate), \
                 patch('machine_audit.installation_linux._snapshot', side_effect=lambda c: Path(c['release']).mkdir(parents=True)), \
                 patch('sys.stdin.isatty', return_value=False), patch('machine_audit.onboarding._yes_no', side_effect=AssertionError('asked')), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(install(config, yes=True), 0)
            self.assertEqual(sorted(p.name for p in (root / 'units').iterdir()), sorted((COLLECTOR, SCANNER, TIMER)))
            self.assertTrue((root / 'current.json').is_file() and Path(candidate['cli']).is_file())

    def upgrade(self, parent, prepare):
        config = Config(data_dir=str(Path(parent) / 'data'))
        config.save()
        root = Path(parent) / 'app'
        candidate = dict(CANDIDATE, release=str(root / 'releases' / 'new5'), root=str(root), config=str(config.path),
                         data=str(config.root), revision='new5', source=parent, cli=str(Path(parent) / 'bin' / 'machine-audit'),
                         units={name: str(root / 'units' / name) for name in (COLLECTOR, SCANNER, TIMER)})
        prepare(root, Path(candidate['cli']))
        output = io.StringIO()
        with patch('machine_audit.installation_linux.plan', return_value=candidate), \
             patch('machine_audit.installation_linux._snapshot', side_effect=lambda c: Path(c['release']).mkdir(parents=True)), \
             patch('sys.stdin.isatty', return_value=False), redirect_stdout(output):
            return install(config, yes=True, upgrade=True), root, Path(candidate['cli']), output.getvalue()

    def test_upgrade_renames_the_old_installation_and_keeps_three_previous_releases(self):
        # First Ubuntu upgrade: the operator renamed current.json, units/ and the CLI by hand.
        def installed(root, cli):
            root.mkdir(mode=0o700)
            for age, name in enumerate(('old1', 'old2', 'old3', 'old4')):
                (root / 'releases' / name).mkdir(parents=True)
                os.utime(root / 'releases' / name, (1_000_000 + age, 1_000_000 + age))
            (root / 'units').mkdir()
            (root / 'units' / COLLECTOR).write_text('old unit')
            (root / 'current.json').write_text(json.dumps({'revision': 'old4'}))
            cli.parent.mkdir()
            cli.write_text('old cli')
        with tempfile.TemporaryDirectory() as parent:
            result, root, cli, _ = self.upgrade(parent, installed)
            self.assertEqual(result, 0)
            self.assertEqual((root / 'units.old4' / COLLECTOR).read_text(), 'old unit')
            self.assertEqual(json.loads((root / 'previous-old4.json').read_text())['revision'], 'old4')
            self.assertEqual(cli.with_name('machine-audit.old4').read_text(), 'old cli')
            self.assertEqual(json.loads((root / 'current.json').read_text())['revision'], 'new5')
            self.assertEqual(sorted(p.name for p in (root / 'releases').iterdir()), ['new5', 'old2', 'old3', 'old4'])
            self.assertEqual([p.name for p in (root / 'retired').iterdir()], ['old1'])  # moved, never deleted

    def test_upgrade_refuses_when_nothing_is_installed(self):
        with tempfile.TemporaryDirectory() as parent:
            result, root, cli, output = self.upgrade(parent, lambda root, cli: None)
            self.assertEqual((result, json.loads(output)['reason']), (2, 'nothing_to_upgrade'))
            self.assertFalse(root.exists() or cli.exists())

    def test_doctor_reads_the_timer_without_privilege_and_refuses_root(self):
        shown = 'LoadState=loaded\nActiveState=active\nNextElapseUSecRealtime=Wed 2026-10-22 04:00:00 UTC\n'
        with patch('machine_audit.onboarding.subprocess.run', return_value=subprocess.CompletedProcess([], 0, shown, '')):
            self.assertEqual(map_timer(), {'state': 'active', 'next': 'Wed 2026-10-22 04:00:00 UTC'})
        with patch('machine_audit.onboarding.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'LoadState=not-found\n', '')):
            self.assertEqual(map_timer()['state'], 'not_installed')
        output = io.StringIO()
        with patch('sys.platform', 'linux'), patch('machine_audit.onboarding.os.geteuid', return_value=0), redirect_stdout(output):
            self.assertEqual(doctor(Config()), 2)  # root's config would be the wrong one
        self.assertIn('sudo -u <service user> -H', output.getvalue())


if __name__ == '__main__':
    unittest.main()
