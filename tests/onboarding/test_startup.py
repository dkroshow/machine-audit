"""Startup failures must be diagnosable before any collector run can be saved."""
from contextlib import redirect_stderr
import io
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from machine_audit.budget import BudgetExceeded
from machine_audit.config import Config
from machine_audit.cli import main
from machine_audit.onboarding import inspect
from machine_audit.startup import guarded_run, read


class StartupTests(unittest.TestCase):
    def test_restart_accepts_time_wait_connections_using_server_bind_rules(self):
        """The preflight must not reject a stopped server's TIME_WAIT connection."""
        from machine_audit.serve import make_server
        from machine_audit.startup import port_status
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0)); listener.listen()
            config = Config(data_dir=tmp, port=listener.getsockname()[1])
            with socket.create_connection(('127.0.0.1', config.port)) as client:
                connection, _ = listener.accept()
                connection.close()  # Server closes first, leaving its connection in TIME_WAIT.
                self.assertEqual(client.recv(1), b'')
            listener.close()
            self.assertEqual(port_status(config), 'available')
            def restart(ready):
                with make_server(None, 't' * 32, config.port):
                    ready()
                return 0
            self.assertEqual(guarded_run(config, restart), 0)
            self.assertEqual(read(config)['state'], 'stopped')

    def test_second_collector_reports_busy_without_exception_text(self):
        """A real competing collector lock keeps its safe startup failure code."""
        from machine_audit.collector import Collector
        from machine_audit.store import Store
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            config = Config(data_dir=tmp, port=listener.getsockname()[1])
            listener.close()
            store = Store(config.store_path)
            first, second = Collector(store), Collector(store)
            first.acquire()
            try:
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(guarded_run(config, lambda ready: second.acquire()), 2)
                self.assertEqual(read(config)['code'], 'collector_busy')
            finally:
                first.close()
                store.close()

    def test_port_conflict_records_failure_before_native_initialization(self):
        """A real occupied loopback port cannot enter sources, rotate a token, or silently restart."""
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen()
            config = Config(data_dir=tmp, port=listener.getsockname()[1])
            config.save()
            with redirect_stderr(io.StringIO()), patch('machine_audit.cli._run') as run:
                self.assertEqual(main(['--config', str(config.path), 'run']), 2)
            run.assert_not_called()
            self.assertFalse(config.token_path.exists())
            report = inspect(config)
            self.assertEqual(report['startup']['code'], 'port_in_use')
            self.assertEqual(report['port'], 'port_in_use')
            self.assertFalse(report['ready'])

    def test_repeated_failure_keeps_fixed_private_record_without_exception_text(self):
        """Budget/native failures before run creation leave bounded, sanitized doctor evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=tmp)
            def fail(ready):
                raise BudgetExceeded('private path and token must not escape')
            with patch('machine_audit.startup.port_status', return_value='available'), redirect_stderr(io.StringIO()) as output:
                for _ in range(3):
                    self.assertEqual(guarded_run(config, fail), 2)
            path = Path(tmp)/'startup.json'
            self.assertEqual(path.stat().st_size, 4096)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(read(config)['code'], 'budget_exhausted')
            self.assertNotIn('private path', path.read_text()+output.getvalue())

    def test_saved_gpu_choice_does_not_claim_live_change(self):
        """Doctor distinguishes a saved enabled choice from a still-disabled live source."""
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(data_dir=tmp, enabled={'gpu': True})
            runtime = {'collector': {'sources': [
                {'source': 'macos.cpu', 'status': 'observed'},
                {'source': 'macos.gpu', 'status': 'not_enabled'}]}}
            # Only the Mac gates its GPU reader on a saved choice.
            with patch('machine_audit.client.request', return_value=runtime), patch('sys.platform', 'darwin'):
                report = inspect(config)
            self.assertTrue(report['gpu']['active_after_restart'])
            self.assertFalse(report['ready'])
