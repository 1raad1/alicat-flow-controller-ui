"""Temporary hosting tests use a fake helper and loopback synthetic samples only."""
import io
import json
import os
from pathlib import Path
import queue
import socket
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from flow_controller.core.mexa_controller import MexaController
from mexa_bridge.bridge import Bridge
from mexa_bridge.quick_tunnel import HostStatus, QuickTunnelHost, helper_environment
from mexa_bridge import wormhole
from flow_controller.ui.qt_mexa import MexaTab

FIXTURE = Path(__file__).parent / "fixtures" / "fake_wormhole.py"
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
    def test_helper_environment_strips_current_and_legacy_credentials(self):
        with patch.dict(os.environ, {"TUNNEL_TOKEN": "secret", "CF_API_KEY": "secret",
                                     "MEXA_RELAY_PUBLISH_KEY": "secret", "CLOUDFLARE_API_TOKEN": "secret",
                                     "WORMHOLE_TOKEN": "secret", "wormhole_key": "secret"}):
            env = helper_environment()
        self.assertFalse(any(key.upper().startswith(("TUNNEL_", "CF_", "MEXA_", "CLOUDFLARE_", "WORMHOLE_"))
                             for key in env))

    def test_status_repr_does_not_expose_access_keys(self):
        self.assertNotIn("secret", repr(HostStatus(publisher_key="secret", receiver_key="secret")))

    def test_output_reader_is_bounded_discards_oversized_lines_and_keeps_newest_event(self):
        events = queue.Queue(maxsize=2)
        stream = io.BytesIO(b"x" * 8000 + b" INF tunnel established url=https://bad.wormhole.bar\n"
                            b"INF tunnel established url=https://one.wormhole.bar\n"
                            b"INF status changed status=reconnecting\n"
                            b"INF reconnected url=https://two.wormhole.bar\n")
        QuickTunnelHost._read_output(stream, events, wormhole.tunnel_event)
        self.assertEqual(events.get_nowait(), ("disconnected", ""))
        self.assertEqual(events.get_nowait(), ("registered", "wss://two.wormhole.bar/mexa"))
        self.assertTrue(events.empty())


class HostLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.mode = "normal"
        self.prepare = patch("mexa_bridge.wormhole.prepare_helper", return_value=Path(sys.executable)).start()
        self.command = patch("mexa_bridge.wormhole.helper_command",
                             side_effect=lambda *args: [sys.executable, str(FIXTURE), self.mode]).start()
        self.spawn = patch("mexa_bridge.quick_tunnel.subprocess.Popen", wraps=subprocess.Popen).start()
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
        self.assertIn("reachability is not yet verified", status.message)
        child_args, child_options = self.spawn.call_args
        for key in (status.publisher_key, status.receiver_key):
            self.assertNotIn(key, repr(child_args))
            self.assertNotIn(key, repr(child_options))
        self.assertEqual(child_options["stdin"], subprocess.DEVNULL)
        self.assertEqual(child_options["creationflags"], subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        child_directory = Path(child_options["cwd"])
        self.assertNotEqual(child_directory, Path(sys.executable).parent)
        self.assertNotEqual(child_directory, Path.cwd())
        self.assertEqual(list(child_directory.iterdir()), [])
        port = int(status.local_url.split(":")[2].split("/")[0])
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        self.assertTrue(host.stop(wait=True))
        self.assertFalse(child_directory.exists())
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
        self.assertIn("443", host.status.message)
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
        tab.transport.setCurrentIndex(tab.transport.findData("host"))
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
            from mexa_bridge.protocol import simulated_cycle
            cycle = simulated_cycle(1)
            cycle.update(no_ppm=-2, valid=False, alarms=["no_out_of_range"], rpm=2400,
                         oil_temperature_c=85, options=15, afr=14.7, **{"lambda": 1.234})
            with patch("mexa_bridge.bridge.simulated_cycle", return_value=cycle), \
                 patch("mexa_bridge.bridge.StreamServer", side_effect=AssertionError("No LAN listener allowed")):
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
        tab.transport.setCurrentIndex(tab.transport.findData("host"))
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
        controller.start_temporary_host(SHARED, save_logs=False)
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
        controller.start_temporary_host(SHARED, save_logs=False)
        generation = controller._host_generation
        controller.stop_temporary_host()
        with patch.object(controller, "connect_temporary_host") as connect:
            controller._host_changed(generation, HostStatus("ready", "queued"))
            connect.assert_not_called()
        self.assertTrue(wait_for(lambda: controller.temporary_host is None))


if __name__ == "__main__":
    unittest.main()
