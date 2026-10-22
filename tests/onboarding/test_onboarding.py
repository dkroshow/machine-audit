"""Operator choices must not become grants, and cancelled setup must not write."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from machine_audit.config import Config
from machine_audit.onboarding import _permission, doctor, setup


class SetupTests(unittest.TestCase):
    def test_identity_salt_is_private_random_and_never_the_exported_device_id(self):
        # Security review P1: the router hardware hash was keyed by device_id, which every export
        # carries, so the 48-bit address could be brute-forced from exported records.
        with tempfile.TemporaryDirectory() as parent:
            config = Config(data_dir=str(Path(parent) / "private"))
            salt = config.identity_salt()
            self.assertEqual(len(salt), 64)
            self.assertNotEqual(salt, config.device_id.encode())
            self.assertEqual(config.identity_salt(), salt)
            self.assertEqual(config.salt_path.stat().st_mode & 0o777, 0o600)
            self.assertNotEqual(Config(data_dir=parent).identity_salt(), salt)
            config.salt_path.write_text("short")
            with self.assertRaises(ValueError):
                config.identity_salt()

    def test_requested_access_is_not_permission(self):
        # An unchecked request cannot authorize a protected scan.
        self.assertEqual(_permission({}, "scanner", True),
                         {"requested": True, "status": "unchecked", "observed_ns": None})

    def test_json_setup_lists_decisions_without_writing(self):
        with tempfile.TemporaryDirectory() as parent:
            config = Config(data_dir=str(Path(parent) / "private"))
            output = io.StringIO()
            with redirect_stdout(output):
                result = setup(config, json_output=True)
            self.assertEqual(result, 2)
            self.assertFalse(config.root.exists())
            self.assertFalse(json.loads(output.getvalue())["saved"])

    def test_uninstalled_doctor_is_read_only_and_does_not_claim_ready(self):
        # Exercise the real local client with no token, not a fake permission probe.
        with tempfile.TemporaryDirectory() as parent:
            config = Config(data_dir=str(Path(parent) / "private"))
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(doctor(config, json_output=True), 1)
            report = json.loads(output.getvalue())
            self.assertFalse(report["ready"])
            self.assertEqual(report["collector"], "not_running")
            self.assertFalse(config.root.exists())

    def test_cancel_preserves_existing_choices(self):
        with tempfile.TemporaryDirectory() as parent:
            config = Config(data_dir=parent)
            config.save()
            before = config.path.read_bytes()
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["yes", "yes", "no", "host.example.ts.net", "no"]), redirect_stdout(io.StringIO()):
                self.assertEqual(setup(config), 1)
            self.assertEqual(config.path.read_bytes(), before)
            self.assertEqual(config.source_options, {})

    def test_scanner_exclusions_load_normalised_and_reject_relative_or_repeated_paths(self):
        # A relative entry would match nothing; a trailing-slash repeat would hide a typo.
        with tempfile.TemporaryDirectory() as parent:
            path = Path(parent) / "config.json"
            for exclusions in (["srv/private"], ["/srv/private", "/srv/private/"], "/srv/private"):
                path.write_text(json.dumps({"data_dir": parent, "scanner_exclusions": exclusions}))
                with self.assertRaisesRegex(ValueError, "scanner_exclusions"):
                    Config.load(path)
            path.write_text(json.dumps({"data_dir": parent, "scanner_exclusions": ["/srv/private/", "/srv/private2"]}))
            self.assertEqual(Config.load(path).scanner_exclusions, ["/srv/private", "/srv/private2"])

    def test_dashboard_hosts_accept_proxy_and_tunnel_names_and_refuse_a_scheme(self):
        for answer, expected in (("dash.example.org", ["dash.example.org"]), ("localhost:9000, dash.example.org", ["localhost:9000", "dash.example.org"]),
                                 ("", []), ("https://dash.example.org", None)):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as parent:
                config = Config(data_dir=parent)
                with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["no", "no", "no", answer, "yes"]), redirect_stdout(io.StringIO()):
                    result = setup(config)
                self.assertEqual(result, 2 if expected is None else 0)
                self.assertEqual(config.path.exists(), expected is not None)
                if expected is not None:
                    self.assertEqual(Config.load(config.path).allowed_hosts, expected)

    def test_old_tailscale_host_loads_as_allowed_hosts_without_rewriting(self):
        with tempfile.TemporaryDirectory() as parent:
            path = Path(parent) / "config.json"
            path.write_text(json.dumps({"data_dir": parent, "tailscale_host": "host.example.ts.net"}))
            before = path.read_bytes()
            config = Config.load(path)
            self.assertEqual(config.allowed_hosts, ["host.example.ts.net"])
            self.assertEqual(path.read_bytes(), before)
            config.save()
            self.assertEqual(json.loads(path.read_text())["allowed_hosts"], ["host.example.ts.net"])
            self.assertNotIn("tailscale_host", json.loads(path.read_text()))

    def test_confirmed_choices_persist_privately_without_claiming_grants(self):
        with tempfile.TemporaryDirectory() as parent:
            config = Config(data_dir=str(Path(parent) / "private"))
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["yes", "no", "yes", "host.example.ts.net", "yes"]), redirect_stdout(io.StringIO()):
                self.assertEqual(setup(config), 0)
            saved = Config.load(config.path)
            self.assertTrue(saved.source_options["scan_protected_requested"])
            self.assertFalse(saved.source_options["helper_requested"])
            self.assertTrue(saved.enabled['scanner'])
            self.assertTrue(saved.enabled['gpu'])
            self.assertEqual(saved.allowed_hosts, ["host.example.ts.net"])
            self.assertNotIn("full_disk_access", saved.source_options)
            self.assertEqual(saved.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(saved.root.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
