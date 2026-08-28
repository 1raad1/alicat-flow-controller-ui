"""Home-host setup tests, also runnable on Linux without Qt or instrument I/O."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from flow_controller.mexa.relay_host import configure, hostname, load_config, main, unit_quote


class RelayHostTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "config"

    def test_hostname_validation_prevents_config_injection(self):
        self.assertEqual(hostname("Relay.Example.NET."), "relay.example.net")
        for value in ("localhost", "127.0.0.1", "https://relay.example.net", "relay.example.net/path",
                      "relay.example.net:443", "relay.example.net {", "relay.example.net\n}",
                      "relay.example.net;test", "*.example.net", "bad_.example.net", "relay.local", ".example.net"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hostname(value)

    def test_private_keys_persist_across_reconfiguration(self):
        first = configure(self.directory, "relay.example.net")
        self.assertNotEqual(first["publisher_key"], first["receiver_key"])
        self.assertEqual(len(first["publisher_key"]), 64)
        self.assertEqual(load_config(self.directory), first)
        second = configure(self.directory, "new.example.net", 8766)
        self.assertEqual(second["publisher_key"], first["publisher_key"])
        self.assertEqual(second["receiver_key"], first["receiver_key"])
        self.assertEqual(second["hostname"], "new.example.net")
        self.assertEqual(second["port"], 8766)

    def test_generated_proxy_and_user_service_do_not_contain_keys(self):
        config = configure(self.directory, "relay.example.net")
        caddy = (self.directory / "Caddyfile").read_text()
        service = (self.directory / "mexa-relay.service").read_text()
        self.assertEqual(caddy, "relay.example.net {\n    reverse_proxy 127.0.0.1:8765\n}\n")
        self.assertIn("flow_controller.mexa.relay_host --config-dir", service)
        self.assertIn("NoNewPrivileges=true", service)
        for key in ("publisher_key", "receiver_key"):
            self.assertNotIn(config[key], caddy + service)
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"host.json", "Caddyfile", "mexa-relay.service"})

    def test_setup_hides_keys_until_explicit_show_keys(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--config-dir", str(self.directory), "setup", "--hostname", "relay.example.net"]), 0)
        config = load_config(self.directory)
        self.assertNotIn(config["publisher_key"], output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--config-dir", str(self.directory), "show-keys"]), 0)
        self.assertIn(config["publisher_key"], output.getvalue())
        self.assertIn(config["receiver_key"], output.getvalue())

    def test_corrupt_existing_config_is_not_silently_replaced(self):
        configure(self.directory, "relay.example.net")
        path = self.directory / "host.json"
        path.write_text("bad-private-test-config", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no keys were printed or replaced"):
            configure(self.directory, "other.example.net")
        self.assertEqual(path.read_text(), "bad-private-test-config")

    def test_privileged_backend_port_refused(self):
        with self.assertRaises(ValueError):
            configure(self.directory, "relay.example.net", 443)
        self.assertFalse(self.directory.exists())

    def test_systemd_paths_are_literal_and_cannot_inject_lines(self):
        self.assertEqual(unit_quote("/a path/with%percent"), '"/a path/with%%percent"')
        for value in ("/path\nExecStart=bad", "/path\r", "/path\0", "/path/$ENV"):
            with self.assertRaises(ValueError):
                unit_quote(value)

    @unittest.skipUnless(os.name == "posix", "POSIX ownership and permissions")
    def test_posix_key_permissions_enforced(self):
        configure(self.directory, "relay.example.net")
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        path = self.directory / "host.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "must be private"):
            load_config(self.directory)

    @unittest.skipUnless(os.name == "posix", "POSIX symlink test")
    def test_symlinked_config_directory_refused(self):
        target = Path(self.temporary.name) / "target"
        target.mkdir()
        self.directory.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            configure(self.directory, "relay.example.net")
        self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
