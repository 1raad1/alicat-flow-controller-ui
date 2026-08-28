"""Temporary hosting tests use a fake helper and loopback synthetic samples only."""
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
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from flow_controller.core.mexa_controller import MexaController
from flow_controller.mexa.bridge import Bridge
from flow_controller.mexa.quick_tunnel import (
    HostError, HostStatus, QuickTunnelHost, helper_command, helper_environment, prepare_helper, tunnel_event,
)
from flow_controller.ui.qt_mexa import MexaTab

FIXTURE = Path(__file__).parent / "fixtures" / "fake_cloudflared.py"
SHARED = "synthetic-analyser-shared-key-" + "s" * 32


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        threading.Event().wait(.01)
    return bool(predicate())


class HelperTests(unittest.TestCase):
    def test_only_expected_public_url_and_connection_events_are_exposed(self):
        self.assertEqual(tunnel_event("| https://abc-def.trycloudflare.com |"),
                         ("url", "wss://abc-def.trycloudflare.com/mexa"))
        for line in ("https://abc.trycloudflare.com.evil.test", "https://abc.trycloudflare.com/secret",
                     "https://user:secret@abc.trycloudflare.com", "https://abc.trycloudflare.com?key=secret",
                     "https://abc.trycloudflare.com:443", "http://abc.trycloudflare.com",
                     "https://-abc.trycloudflare.com", "https://abc-.trycloudflare.com",
                     "https://evil.test/https://abc.trycloudflare.com"):
            self.assertIsNone(tunnel_event(line), line)
        self.assertEqual(tunnel_event("INF Registered tunnel connection connIndex=0"), ("connected", ""))
        self.assertIsNone(tunnel_event("arbitrary error with private data"))

    def test_helper_command_is_loopback_only_and_overrides_existing_configuration(self):
        args = helper_command("C:/Program Files/cloudflared.exe", "C:/temp/private/config.yml", 43210)
        self.assertEqual(args[0], "C:/Program Files/cloudflared.exe")
        self.assertEqual(args[args.index("--url") + 1], "http://127.0.0.1:43210")
        self.assertEqual(args[args.index("--protocol") + 1], "http2")
        self.assertEqual(args[args.index("--metrics") + 1], "127.0.0.1:0")
        self.assertIn("--no-autoupdate", args)
        self.assertIn("--config", args)
        self.assertNotIn("--token", args)

    def test_certificate_and_filter_diagnostics_are_specific_and_sanitized(self):
        for raw, expected in (("x509: certificate is valid for blocked-due-to-malware.ucl.ac.uk secret=private", "UCL is blocking"),
                              ("x509: failed to verify certificate secret=private", "TLS certificate verification failed"),
                              ("failed to request quick Tunnel private-body", "Could not create")):
            event, detail = tunnel_event(raw)
            self.assertEqual(event, "error")
            self.assertIn(expected, detail)
            self.assertNotIn("private", detail)

    def test_cloudflare_and_mexa_environment_credentials_are_not_inherited(self):
        with patch.dict(os.environ, {"TUNNEL_TOKEN": "secret", "CF_API_KEY": "secret",
                                     "MEXA_RELAY_PUBLISH_KEY": "secret", "CLOUDFLARE_API_TOKEN": "secret"}):
            env = helper_environment()
        self.assertFalse(any(key.upper().startswith(("TUNNEL_", "CF_", "MEXA_", "CLOUDFLARE_")) for key in env))

    def test_status_repr_does_not_expose_access_keys(self):
        self.assertNotIn("secret", repr(HostStatus(publisher_key="secret", receiver_key="secret")))

    def download(self, directory, content, expected=None, stop=None):
        target = Path(directory) / "helper.exe"
        response = io.BytesIO(content)
        response.url = "https://release-assets.githubusercontent.com/asset"
        with patch("flow_controller.mexa.quick_tunnel.default_helper_path", return_value=target), \
             patch("flow_controller.mexa.quick_tunnel.WINDOWS_SIZE", len(content)), \
             patch("flow_controller.mexa.quick_tunnel.WINDOWS_SHA256", expected or hashlib.sha256(content).hexdigest()), \
             patch("flow_controller.mexa.quick_tunnel.urllib.request.urlopen", return_value=response):
            return prepare_helper("", stop or threading.Event(), lambda message: None)

    def test_download_verified_before_install_and_cached_helper_is_rechecked(self):
        with tempfile.TemporaryDirectory() as directory:
            target = self.download(directory, b"synthetic-binary")
            self.assertEqual(target.read_bytes(), b"synthetic-binary")
            self.assertEqual(self.download(directory, b"synthetic-binary"), target)
            with self.assertRaisesRegex(HostError, "existing file was not replaced"):
                self.download(directory, b"different-binary")
            self.assertEqual(target.read_bytes(), b"synthetic-binary")

    def test_hash_mismatch_and_cancel_leave_no_executable_or_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(HostError, "SHA-256"):
                self.download(directory, b"bad-download", expected="0" * 64)
            self.assertEqual(list(Path(directory).iterdir()), [])
            stop = threading.Event()
            stop.set()
            with self.assertRaisesRegex(HostError, "cancelled"):
                self.download(directory, b"cancelled-download", stop=stop)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_output_reader_is_bounded_and_discards_oversized_lines(self):
        events = queue.Queue(maxsize=128)
        stream = io.BytesIO(b"x" * 8000 + b" https://bad.trycloudflare.com\n"
                            b"INF Registered tunnel connection\n")
        QuickTunnelHost._read_output(stream, events)
        self.assertEqual(events.get_nowait(), ("connected", ""))
        self.assertTrue(events.empty())


class HostLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.mode = "normal"
        self.prepare = patch("flow_controller.mexa.quick_tunnel.prepare_helper", return_value=Path(sys.executable)).start()
        self.command = patch("flow_controller.mexa.quick_tunnel.helper_command",
                             side_effect=lambda *args: [sys.executable, str(FIXTURE), self.mode]).start()
        self.addCleanup(patch.stopall)

    def start_host(self, timeout=2):
        statuses = []
        host = QuickTunnelHost(statuses.append, startup_timeout=timeout)
        self.addCleanup(lambda: host.stop(wait=True))
        host.start()
        return host, statuses

    def test_start_stop_releases_child_listener_and_keys(self):
        host, statuses = self.start_host()
        self.assertTrue(wait_for(lambda: host.status.state == "ready"))
        status, process = host.status, host.process
        self.assertNotEqual(status.publisher_key, status.receiver_key)
        self.assertEqual(len(status.publisher_key), 64)
        port = int(status.local_url.split(":")[2].split("/")[0])
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        self.assertTrue(host.stop(wait=True))
        self.assertIsNotNone(process.poll())
        self.assertEqual(host.status.state, "stopped")
        self.assertEqual(host.status.publisher_key, "")
        self.assertEqual(host.status.public_url, "")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=.3)

    def test_restart_uses_fresh_keys(self):
        first, _ = self.start_host()
        self.assertTrue(wait_for(lambda: first.status.state == "ready"))
        key = first.status.publisher_key
        first.stop(wait=True)
        second, _ = self.start_host()
        self.assertTrue(wait_for(lambda: second.status.state == "ready"))
        self.assertNotEqual(second.status.publisher_key, key)

    def test_child_exit_is_sanitized_and_listener_closed(self):
        self.mode = "exit"
        host, statuses = self.start_host()
        self.assertTrue(wait_for(lambda: host.status.state == "failed"))
        self.assertIn("helper exited", host.status.message)
        self.assertNotIn("secret-value", str(statuses))
        self.assertIsNone(host.process)

    def test_ucl_block_reason_survives_fast_child_exit(self):
        self.mode = "blocked"
        host, _ = self.start_host()
        self.assertTrue(wait_for(lambda: host.status.state == "failed"))
        self.assertIn("UCL is blocking", host.status.message)
        self.assertIsNone(host.process)

    def test_startup_timeout_and_stop_during_startup_clean_up(self):
        self.mode = "silent"
        host, _ = self.start_host(timeout=.25)
        self.assertTrue(wait_for(lambda: host.status.state == "failed"))
        self.assertIn("7844", host.status.message)
        self.assertIsNone(host.process)
        second, _ = self.start_host(timeout=10)
        self.assertTrue(wait_for(lambda: second.process is not None))
        process = second.process
        self.assertTrue(second.stop(wait=True))
        self.assertIsNotNone(process.poll())
        self.assertEqual(second.status.state, "stopped")

    def test_recovery_keeps_same_session_keys_and_notifies_interruption(self):
        self.mode = "recover"
        host, statuses = self.start_host()
        self.assertTrue(wait_for(lambda: sum(s.state == "ready" for s in statuses) >= 2))
        self.assertIn("reconnecting", [s.state for s in statuses])
        ready = [s for s in statuses if s.state == "ready"]
        self.assertEqual(ready[0], ready[1])

    def test_controller_ui_auto_connects_local_and_logs_simulated_invalid_channels(self):
        controller = MexaController()
        self.addCleanup(controller.shutdown)
        tab = MexaTab(controller)
        self.addCleanup(tab.close)
        tab.transport.setCurrentIndex(2)
        tab.tunnel_provider.setCurrentIndex(1)  # Existing Cloudflare lifecycle coverage
        self.assertFalse(tab.start_host.isEnabled())
        self.assertFalse(tab.connect_button.isEnabled())
        self.assertFalse(tab.host.isEnabled())
        tab.host_consent.setChecked(True)
        self.assertTrue(tab.start_host.isEnabled())
        tab.token.setText(SHARED)
        interruptions = []
        controller.interrupted.connect(interruptions.append)
        with tempfile.TemporaryDirectory() as directory:
            tab.directory.setText(directory)
            tab._start_host()
            self.assertFalse(tab.transport.isEnabled())
            self.assertFalse(tab.token.isEnabled())
            self.assertTrue(wait_for(lambda: controller.client is not None))
            self.assertEqual(controller.settings["relay_url"], controller.host_status.local_url)
            self.assertTrue(tab.copy_url.isEnabled())
            self.assertTrue(tab.copy_publisher.isEnabled())
            self.assertEqual(tab.public_url.text(), controller.host_status.public_url)
            self.assertEqual(tab.publisher_key.text(), controller.host_status.publisher_key)
            from flow_controller.mexa.protocol import simulated_cycle
            cycle = simulated_cycle(1)
            cycle.update(no_ppm=-2, valid=False, alarms=["no_out_of_range"], rpm=2400,
                         oil_temperature_c=85, options=15, afr=14.7, **{"lambda": 1.234})
            with patch("flow_controller.mexa.bridge.simulated_cycle", return_value=cycle), \
                 patch("flow_controller.mexa.bridge.StreamServer", side_effect=AssertionError("No LAN listener allowed")):
                bridge = Bridge(host="127.0.0.1", port=61234, token=SHARED, serial_port="NEVER",
                                simulated=True, save_logs=False, transport="relay",
                                relay_url=controller.host_status.local_url, relay_key=controller.host_status.publisher_key)
                try:
                    self.assertTrue(wait_for(lambda: controller.latest is not None))
                    self.assertIn("INVALID", tab.readings.text())
                    self.assertEqual(controller.latest.packet["rpm"], 2400)
                    self.assertEqual(controller.latest.packet["no_ppm"], -2)
                    with self.assertRaises(ValueError):
                        controller.checked_sample()
                    logged = json.loads(controller.log.path.read_text().splitlines()[0])
                    self.assertEqual(logged["oil_temperature_c"], 85)
                    process = controller.temporary_host.process
                    tab.stop_host.click()
                    self.assertIsNone(controller.latest)
                    self.assertIsNone(controller.client)
                    self.assertTrue(interruptions)
                    self.assertTrue(wait_for(lambda: controller.temporary_host is None))
                    self.assertIsNotNone(process.poll())
                    self.assertEqual(tab.public_url.text(), "")
                    self.assertEqual(tab.publisher_key.text(), "")
                finally:
                    bridge.stop()
                    controller.disconnect_bridge()

    def test_no_consent_or_invalid_shared_key_never_starts_host(self):
        controller = MexaController()
        self.addCleanup(controller.shutdown)
        tab = MexaTab(controller)
        self.addCleanup(tab.close)
        tab.transport.setCurrentIndex(2)
        tab._start_host()
        self.assertIsNone(controller.temporary_host)
        tab.host_consent.setChecked(True)
        tab.token.setText("short")
        tab._start_host()
        self.assertIsNone(controller.temporary_host)
        self.prepare.assert_not_called()

    def test_shutdown_and_queued_ready_event_cannot_restart_receiver(self):
        controller = MexaController()
        self.addCleanup(controller.shutdown)
        controller.start_temporary_host(SHARED, save_logs=False, provider="cloudflare")
        generation = controller._host_generation
        self.assertTrue(wait_for(lambda: controller.client is not None))
        status, process = controller.host_status, controller.temporary_host.process
        controller.shutdown()
        controller._host_changed(generation, status)
        self.assertIsNone(controller.client)
        self.assertIsNone(controller.temporary_host)
        self.assertIsNotNone(process.poll())

    def test_stop_cancels_pending_auto_connect(self):
        controller = MexaController()
        self.addCleanup(controller.shutdown)
        controller.start_temporary_host(SHARED, save_logs=False, provider="cloudflare")
        generation = controller._host_generation
        controller.stop_temporary_host()
        with patch.object(controller, "connect_temporary_host") as connect:
            controller._host_changed(generation, HostStatus("ready", "queued"))
            connect.assert_not_called()
        self.assertTrue(wait_for(lambda: controller.temporary_host is None))


if __name__ == "__main__":
    unittest.main()
