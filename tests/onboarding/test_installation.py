"""Exercise permission boundaries without installing jobs or reading user files."""
import json
import io
from contextlib import redirect_stdout
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from machine_audit.config import Config
from machine_audit.installation import build_scanner, scanner_executable, _install_files, control, COLLECTOR, SCANNER
from machine_audit.onboarding import observed_permissions
from machine_audit.scanner.permissions import AccessObservation
from machine_audit.scanner_entry import main


class InstallationTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent / '.tmp'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name).resolve()
        self.config = Config(data_dir=str(self.root / 'recordings'))
        self.config.save()

    def tearDown(self):
        self.temp.cleanup()

    def test_permission_check_never_becomes_a_scan_when_request_expires(self):
        """A delayed one-shot check must not fall through into a full disk walk."""
        request = self.config.root / 'scanner-request.json'
        request.write_text(json.dumps({'action': 'check_access', 'nonce': 'a'*32, 'expires_ns': 1}))
        request.chmod(0o600)
        with patch('machine_audit.scanner_entry.observe_access') as probe, patch('machine_audit.scanner.runner.run_scan') as scan:
            result = main({'config': str(self.config.path)}, check_only=True)
        self.assertEqual(result, 2)
        probe.assert_not_called()
        scan.assert_not_called()

    def test_check_only_reports_the_scanner_identity_without_running_the_scanner(self):
        """The onboarding probe returns its own identity, never requested access as proof."""
        nonce = 'b'*32
        request = self.config.root / 'scanner-request.json'
        request.write_text(json.dumps({'action': 'check_access', 'nonce': nonce, 'expires_ns': time.time_ns()+10**10}))
        request.chmod(0o600)
        observation = AccessObservation('observed', os.getpid(), os.getuid(), 'c'*64, time.time_ns())
        with patch('machine_audit.scanner_entry.observe_access', return_value=observation), patch('machine_audit.scanner.runner.run_scan') as scan:
            self.assertEqual(main({'config': str(self.config.path)}, check_only=True), 0)
        scan.assert_not_called()
        self.assertEqual(json.loads((self.config.root/'scanner-access.json').read_text())['nonce'], nonce)
        self.assertEqual(observed_permissions(self.config.root)['scanner']['status'], 'unchecked')
        self.config.source_options['scanner_identity_sha256'] = 'c'*64
        self.config.save()
        self.assertEqual(observed_permissions(self.config.root)['scanner']['status'], 'granted')
        self.config.source_options['scanner_identity_sha256'] = 'd'*64
        self.config.save()
        self.assertEqual(observed_permissions(self.config.root)['scanner']['status'], 'unchecked')

    def test_explicit_configuration_path_owns_its_permission_identity(self):
        """A custom --config must not read or overwrite data_dir/config.json."""
        custom = self.root/'chosen.json'
        self.config.source_options['scanner_identity_sha256'] = 'c'*64
        self.config.save(custom)
        loaded = Config.load(custom)
        self.assertEqual(loaded.path, custom)
        self.assertEqual(self.config.path, custom)
        result = {'status': 'observed', 'executable_sha256': 'c'*64, 'observed_ns': 123}
        observation_path = loaded.root/'scanner-access.json'
        observation_path.write_text(json.dumps(result)); observation_path.chmod(0o600)
        self.assertEqual(observed_permissions(loaded)['scanner']['status'], 'granted')

    def test_scheduled_scan_preserves_probe_and_passes_saved_options(self):
        """An unprotected run must retain the last explicit probe and handle the runner's result envelope."""
        self.config.source_options.update(scanner_grain='folders', scanner_full=True)
        self.config.enabled['scanner'] = True
        self.config.save()
        saved = self.config.root/'scanner-access.json'
        saved.write_bytes(b'{"status":"observed"}'); saved.chmod(0o600)
        observation = AccessObservation('unchecked', os.getpid(), os.getuid(), 'c'*64, time.time_ns())
        with patch('machine_audit.scanner_entry.observe_access', return_value=observation), patch('machine_audit.scanner.runner.run_scan', return_value={'status': 'observed', 'volumes': [], 'entries': 0}) as scan:
            self.assertEqual(main({'config': str(self.config.path)}), 0)
        self.assertEqual(saved.read_bytes(), b'{"status":"observed"}')
        self.assertEqual(scan.call_args.kwargs['grain'], 'folders')
        self.assertTrue(scan.call_args.kwargs['full'])
        self.config.enabled['scanner'] = False
        self.config.save()
        with patch('machine_audit.scanner_entry.observe_access') as probe:
            self.assertEqual(main({'config': str(self.config.path)}), 2)
        probe.assert_not_called()

    def test_scheduled_failure_prints_reason_json_and_exits_nonzero(self):
        self.config.enabled['scanner'] = True
        self.config.save()
        result = {'status': 'scan_failed', 'reason': 'invalid_scan_state', 'failure_recorded': True, 'entries': 123}
        observation = AccessObservation('unchecked', os.getpid(), os.getuid(), 'c'*64, time.time_ns())
        output = io.StringIO()
        with patch('machine_audit.scanner_entry.observe_access', return_value=observation), \
             patch('machine_audit.scanner.runner.run_scan', return_value=result), redirect_stdout(output):
            self.assertEqual(main({'config': str(self.config.path)}), 1)
        self.assertEqual(json.loads(output.getvalue()), result)

    def test_existing_installation_is_preserved_before_source_copy(self):
        """An unreviewed update must not replace an existing installation or recordings."""
        app = self.root/'application'
        app.mkdir(mode=0o700)
        original = b'{"revision":"existing"}'
        (app/'current.json').write_bytes(original)
        history = self.config.root/'history.sqlite'
        history.write_bytes(b'preserved synthetic history')
        with self.assertRaisesRegex(ValueError, 'existing_installation'):
            _install_files(self.config, {'root': str(app), 'cli': str(self.root/'bin'/'machine-audit')})
        self.assertEqual((app/'current.json').read_bytes(), original)
        self.assertEqual(history.read_bytes(), b'preserved synthetic history')

    def _service_fixture(self, loaded=(), fail_bootstrap=None, fail_disable=None):
        app = Path(tempfile.mkdtemp(prefix='service-fixture-', dir=self.root))
        (app/'current.json').write_text(json.dumps({'config': str(self.config.path)}))
        state = {'loaded': set(loaded), 'disabled': set(), 'commands': []}
        def command(args, **kwargs):
            state['commands'].append(args[1])
            verb = args[1]
            label = Path(args[-1]).stem if verb == 'bootstrap' else args[-1].split('/')[-1]
            if verb == 'enable': state['disabled'].discard(label)
            elif verb == 'disable':
                if label == fail_disable: raise RuntimeError('launchctl_failed')
                state['disabled'].add(label)
            elif verb == 'bootstrap':
                if label in state['loaded'] or label == fail_bootstrap:
                    raise RuntimeError('launchctl_failed')
                state['loaded'].add(label)
            elif verb == 'bootout': state['loaded'].discard(label)
            return b''
        def inspect(args, **kwargs):
            self.assertEqual(args[1], 'print')
            return subprocess.CompletedProcess(args, 0 if args[-1].split('/')[-1] in state['loaded'] else 113,
                                               stderr=b'Could not find service')
        self.enterContext(patch('machine_audit.installation.application_root', return_value=app))
        self.enterContext(patch('machine_audit.installation._command', side_effect=command))
        self.enterContext(patch('machine_audit.installation.subprocess.run', side_effect=inspect))
        self.enterContext(patch('machine_audit.installation.sys.stdin.isatty', return_value=True))
        self.enterContext(patch('machine_audit.onboarding._yes_no', return_value=True))
        return state

    @unittest.skipUnless(sys.platform == 'darwin', 'Mac launch jobs')
    def test_start_again_preserves_already_loaded_jobs(self):
        """Repeating start must not bootstrap or restart an already running service."""
        state = self._service_fixture(loaded=(COLLECTOR, SCANNER))
        self.assertEqual(control(self.config, 'start'), 0)
        self.assertEqual(state['loaded'], {COLLECTOR, SCANNER})

    @unittest.skipUnless(sys.platform == 'darwin', 'Mac launch jobs')
    def test_starting_missing_scanner_does_not_change_running_gpu_choice(self):
        """Repairing only the scanner must not claim an unchanged collector enabled GPU reads."""
        self.config.enabled['gpu'] = False
        self.config.save()
        state = self._service_fixture(loaded=(COLLECTOR,))
        self.assertEqual(control(self.config, 'start'), 0)
        self.assertFalse(Config.load(self.config.path).enabled['gpu'])
        self.assertEqual(state['loaded'], {COLLECTOR, SCANNER})
        self.assertNotIn('kickstart', state['commands'])

    def test_abandoned_preparation_is_preserved_and_blocks_retry(self):
        """Retrying an interrupted preparation must not pile up or replace its files."""
        app = self.root/'application'
        stage = app/'.prepare-synthetic'
        app.mkdir(mode=0o700)
        stage.mkdir(mode=0o700)
        evidence = stage/'partial'
        evidence.write_bytes(b'synthetic incomplete build')
        candidate = {'root': str(app), 'cli': str(self.root/'bin'/'machine-audit'),
                     'jobs': [COLLECTOR, SCANNER], 'release': str(app/'releases'/'candidate')}
        with patch('machine_audit.installation.Path.home', return_value=self.root):
            with self.assertRaisesRegex(ValueError, 'unfinished_preparation'):
                _install_files(self.config, candidate)
        self.assertEqual(evidence.read_bytes(), b'synthetic incomplete build')
        self.assertEqual(len(list(app.glob('.prepare-*'))), 1)

    @unittest.skipUnless(sys.platform == 'darwin', 'Mac launch jobs')
    def test_failed_second_start_rolls_back_only_new_jobs(self):
        """A scanner bootstrap failure must not leave new collection running unnoticed."""
        for prior in (set(), {COLLECTOR}):
            with self.subTest(already_loaded=prior):
                state = self._service_fixture(loaded=prior, fail_bootstrap=SCANNER)
                with self.assertRaises(RuntimeError): control(self.config, 'start')
                self.assertEqual(state['loaded'], prior)
                self.assertEqual(state['disabled'], {COLLECTOR, SCANNER} - prior)

    @unittest.skipUnless(sys.platform == 'darwin', 'Mac launch jobs')
    def test_stop_attempts_every_job_even_when_one_disable_fails(self):
        """A failed disable cannot prevent stopping the other monitoring process."""
        state = self._service_fixture(loaded=(COLLECTOR, SCANNER), fail_disable=SCANNER)
        with self.assertRaises(RuntimeError): control(self.config, 'stop')
        self.assertEqual(state['loaded'], set())
        self.assertIn(COLLECTOR, state['disabled'])

    @unittest.skipUnless(sys.platform == 'darwin', 'Mac scanner executable')
    def test_native_scanner_cannot_run_as_an_arbitrary_python_launcher(self):
        """The real signed entry rejects shell/agent invocation and arbitrary arguments."""
        app = self.root/'app'
        app.mkdir(mode=0o700)
        source = Path(__file__).resolve().parents[2]
        digest = build_scanner(app, source)
        executable = str(scanner_executable(app))
        identity = subprocess.run([executable, '--identity'], capture_output=True, text=True)
        self.assertEqual(identity.returncode, 0)
        self.assertIn('no collection', identity.stdout)
        for arguments in ([], ['--check-access'], ['-c', 'print(1)']):
            result = subprocess.run([executable, *arguments], capture_output=True)
            self.assertEqual(result.returncode, 2)
        self.assertEqual(build_scanner(app, source), digest)


if __name__ == '__main__':
    unittest.main()
