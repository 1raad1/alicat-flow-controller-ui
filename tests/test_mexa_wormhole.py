"""Wormhole provider tests: fake child and synthetic loopback data only."""
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from flow_controller.core.mexa_controller import MexaController
from flow_controller.mexa import wormhole
from flow_controller.mexa.bridge import Bridge
from flow_controller.mexa.quick_tunnel import HostError, QuickTunnelHost, helper_environment
from flow_controller.ui.qt_mexa import MexaTab
from tests.test_mexa_quick_tunnel import SHARED, wait_for

FIXTURE = Path(__file__).parent / "fixtures" / "fake_wormhole.py"


class WormholeHelperTests(unittest.TestCase):
    def test_registration_is_atomic_and_only_known_log_lines_are_accepted(self):
        for prefix in ("INF", "6:49PM INF", "\x1b[90m6:49PM\x1b[0m INF"):
            for event in ("tunnel established", "reconnected"):
                self.assertEqual(wormhole.tunnel_event(prefix + " " + event + " url=https://abc-123.wormhole.bar"),
                                 ("registered", "wss://abc-123.wormhole.bar/mexa"))
        for suffix in (".evil.test", "/secret", "?key=secret", ":443", "#secret", "@evil.test"):
            self.assertIsNone(wormhole.tunnel_event("INF tunnel established url=https://abc.wormhole.bar" + suffix))
        for line in ("INF status changed status=online", "Forwarding: https://old.wormhole.bar -> http://localhost:1234",
                     "INF request path=https://abc.wormhole.bar", "INF tunnel established url=https://relay.wormhole.bar",
                     "INF tunnel established url=https://-bad.wormhole.bar", "private arbitrary data"):
            self.assertIsNone(wormhole.tunnel_event(line), line)
        self.assertEqual(wormhole.tunnel_event("6:49PM INF status changed status=reconnecting"), ("disconnected", ""))

    def test_command_has_no_inspector_login_custom_domain_or_credentials(self):
        self.assertEqual(wormhole.helper_command("C:/Program Files/wormhole.exe", "unused.yml", 43123),
                         ["C:/Program Files/wormhole.exe", "http", "43123", "--headless", "--no-inspect"])
        with patch.dict(os.environ, {"WORMHOLE_TOKEN": "secret", "wormhole_key": "secret"}):
            self.assertFalse(any(name.upper().startswith("WORMHOLE_") for name in helper_environment()))

    def test_errors_and_oversized_output_are_sanitized(self):
        for line in ("x509: private-data", "blocked-due-to-malware.ucl.ac.uk private-data"):
            kind, message = wormhole.tunnel_event(line)
            self.assertEqual(kind, "error")
            self.assertNotIn("private-data", message)
            self.assertIn("not bypassed", message)
        events = queue.Queue(maxsize=128)
        stream = io.BytesIO(b"x" * 8000 + b" INF tunnel established url=https://bad.wormhole.bar\n"
                            b"INF tunnel established url=https://good.wormhole.bar\n")
        QuickTunnelHost._read_output(stream, events, wormhole.tunnel_event)
        self.assertEqual(events.get_nowait(), ("registered", "wss://good.wormhole.bar/mexa"))
        self.assertTrue(events.empty())

    def download(self, directory, *, content=b"synthetic-executable", archive_hash=None,
                 binary_hash=None, member="wormhole.exe", stop=None, response_url="https://github.com/asset"):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(member, content)
            archive.writestr("../NEVER-EXTRACT.txt", b"must not be extracted")
        archive_bytes = data.getvalue()
        response = io.BytesIO(archive_bytes)
        response.url = response_url
        target = Path(directory) / "wormhole.exe"
        with patch.object(wormhole, "default_helper_path", return_value=target), \
             patch.object(wormhole, "ARCHIVE_SIZE", len(archive_bytes)), \
             patch.object(wormhole, "ARCHIVE_SHA256", archive_hash or hashlib.sha256(archive_bytes).hexdigest()), \
             patch.object(wormhole, "WINDOWS_SIZE", len(content)), \
             patch.object(wormhole, "WINDOWS_SHA256", binary_hash or hashlib.sha256(content).hexdigest()), \
             patch.object(wormhole.urllib.request, "urlopen", return_value=response) as fetch:
            result = wormhole.prepare_helper("", stop or threading.Event(), lambda message: None)
            return result, fetch.call_count

    def test_zip_and_executable_are_verified_and_cache_is_rechecked(self):
        with tempfile.TemporaryDirectory() as directory:
            target, requests = self.download(directory)
            self.assertEqual(requests, 1)
            self.assertEqual(target.read_bytes(), b"synthetic-executable")
            self.assertEqual(list(Path(directory).iterdir()), [target])
            self.assertEqual(self.download(directory), (target, 0))
            with self.assertRaisesRegex(HostError, "existing file was not replaced"):
                self.download(directory, content=b"different")
            self.assertEqual(target.read_bytes(), b"synthetic-executable")

    def test_bad_zip_hash_binary_hash_layout_or_plaintext_leave_no_files(self):
        for options, message in (({"archive_hash": "0" * 64}, "ZIP failed SHA-256"),
                                 ({"binary_hash": "0" * 64}, "executable failed SHA-256"),
                                 ({"member": "../wormhole.exe"}, "unexpected executable layout"),
                                 ({"response_url": "http://example.test/file"}, "must use HTTPS")):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(HostError, message):
                    self.download(directory, **options)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cancel_leaves_no_partial_download(self):
        with tempfile.TemporaryDirectory() as directory:
            stop = threading.Event()
            stop.set()
            with self.assertRaisesRegex(HostError, "cancelled"):
                self.download(directory, stop=stop)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_invalid_manual_path_and_unknown_provider_rejected(self):
        with self.assertRaises(HostError):
            wormhole.prepare_helper("relative.exe", threading.Event(), lambda message: None)
        with self.assertRaises(HostError):
            QuickTunnelHost(lambda status: None, provider="unknown")


class WormholeLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.mode = "normal"
        self.prepare = patch.object(wormhole, "prepare_helper", return_value=Path(sys.executable)).start()
        patch.object(wormhole, "helper_command", side_effect=lambda *args: [sys.executable, str(FIXTURE), self.mode]).start()
        self.addCleanup(patch.stopall)

    def start_host(self, timeout=2):
        statuses = []
        host = QuickTunnelHost(statuses.append, provider="wormhole", startup_timeout=timeout)
        self.addCleanup(lambda: host.stop(wait=True))
        host.start()
        return host, statuses

    def test_start_stop_releases_listener_process_and_keys(self):
        host, _ = self.start_host()
        self.assertTrue(wait_for(lambda: host.status.state == "ready"))
        status, process = host.status, host.process
        port = int(status.local_url.split(":")[2].split("/")[0])
        self.assertNotEqual(status.publisher_key, status.receiver_key)
        self.assertEqual(status.public_url, "wss://test-one.wormhole.bar/mexa")
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        self.assertTrue(host.stop(wait=True))
        self.assertIsNotNone(process.poll())
        self.assertEqual(host.status.publisher_key, "")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=.3)

    def test_recovery_requires_new_registration_and_preserves_local_keys(self):
        for mode in ("recover", "change"):
            with self.subTest(mode=mode):
                self.mode = mode
                host, statuses = self.start_host()
                self.assertTrue(wait_for(lambda: sum(s.state == "ready" for s in statuses) >= 2))
                ready = [s for s in statuses if s.state == "ready"]
                self.assertIn("reconnecting", [s.state for s in statuses])
                self.assertEqual(ready[0].publisher_key, ready[-1].publisher_key)
                self.assertEqual(ready[0].local_url, ready[-1].local_url)
                if mode == "change":
                    self.assertIn("test-two", ready[-1].public_url)
                    self.assertIn("copy the new URL", ready[-1].message)
                host.stop(wait=True)

    def test_stale_banner_online_and_repeated_retries_cannot_fake_readiness(self):
        for mode in ("stale", "retry", "silent"):
            with self.subTest(mode=mode):
                self.mode = mode
                host, statuses = self.start_host(timeout=1)
                self.assertTrue(wait_for(lambda: host.status.state == "failed", seconds=4))
                self.assertIn("443", host.status.message)
                self.assertNotIn("7844", host.status.message)
                self.assertEqual(sum(s.state == "ready" for s in statuses), 1 if mode == "stale" else 0)
                self.assertIsNone(host.process)

    def test_exit_and_filter_errors_do_not_expose_raw_output(self):
        for mode, expected in (("exit", "Wormhole helper exited"), ("blocked", "UCL is blocking")):
            self.mode = mode
            host, statuses = self.start_host()
            self.assertTrue(wait_for(lambda: host.status.state == "failed"))
            self.assertIn(expected, host.status.message)
            self.assertNotIn("secret-value", str(statuses))

    def test_provider_ui_defaults_to_wormhole_and_resets_consent_and_helper(self):
        controller = MexaController()
        self.addCleanup(controller.shutdown)
        tab = MexaTab(controller)
        self.addCleanup(tab.close)
        tab.transport.setCurrentIndex(2)
        self.assertEqual(tab.tunnel_provider.currentData(), "wormhole")
        self.assertIn("443", tab.connection_help.text())
        self.assertNotIn("7844", tab.connection_help.text())
        tab.host_consent.setChecked(True)
        tab.helper_path.setText("C:/selected/wormhole.exe")
        tab.tunnel_provider.setCurrentIndex(1)
        self.assertFalse(tab.host_consent.isChecked())
        self.assertEqual(tab.helper_path.text(), "")
        self.assertIn("Cloudflare", tab.host_consent.text())
        self.assertIn("7844", tab.connection_help.text())
        self.assertFalse(tab.start_host.isEnabled())

    def test_controller_auto_connects_and_logs_invalid_channels_without_hardware(self):
        controller = MexaController()
        self.addCleanup(controller.shutdown)
        tab = MexaTab(controller)
        self.addCleanup(tab.close)
        tab.transport.setCurrentIndex(2)
        tab.host_consent.setChecked(True)
        tab.token.setText(SHARED)
        with tempfile.TemporaryDirectory() as directory:
            tab.directory.setText(directory)
            tab._start_host()
            self.assertTrue(wait_for(lambda: controller.client is not None))
            self.assertEqual(controller.temporary_host.provider, "wormhole")
            self.assertFalse(tab.tunnel_provider.isEnabled())
            from flow_controller.mexa.protocol import simulated_cycle
            cycle = simulated_cycle(1)
            cycle.update(no_ppm=-2, valid=False, alarms=["no_out_of_range"], rpm=2400,
                         oil_temperature_c=85, options=15, afr=14.7, **{"lambda": 1.234})
            with patch("flow_controller.mexa.bridge.simulated_cycle", return_value=cycle), \
                 patch("flow_controller.mexa.bridge.StreamServer", side_effect=AssertionError("No LAN listener")):
                bridge = Bridge(host="127.0.0.1", port=61234, token=SHARED, serial_port="NEVER",
                                simulated=True, save_logs=False, transport="relay",
                                relay_url=controller.host_status.local_url, relay_key=controller.host_status.publisher_key)
                try:
                    self.assertTrue(wait_for(lambda: controller.latest is not None))
                    self.assertIn("INVALID", tab.readings.text())
                    with self.assertRaises(ValueError):
                        controller.checked_sample()
                    logged = json.loads(controller.log.path.read_text().splitlines()[0])
                    self.assertEqual(logged["no_ppm"], -2)
                    self.assertEqual(logged["oil_temperature_c"], 85)
                    process = controller.temporary_host.process
                    tab.stop_host.click()
                    self.assertIsNone(controller.latest)
                    self.assertTrue(wait_for(lambda: controller.temporary_host is None))
                    self.assertIsNotNone(process.poll())
                    self.assertEqual(tab.public_url.text(), "")
                finally:
                    bridge.stop()
                    controller.disconnect_bridge()

    def test_stop_during_startup_and_shutdown_do_not_restart_receiver(self):
        self.mode = "silent"
        controller = MexaController()
        self.addCleanup(controller.shutdown)
        controller.start_temporary_host(SHARED, save_logs=False)
        self.assertTrue(wait_for(lambda: controller.temporary_host.process is not None))
        host = controller.temporary_host
        process = host.process
        controller.shutdown()
        self.assertIsNone(controller.client)
        self.assertIsNone(controller.temporary_host)
        self.assertFalse(host.thread.is_alive())
        self.assertIsNotNone(process.poll())


if __name__ == "__main__":
    unittest.main()
