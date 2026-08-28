"""Receiver lifecycle and standalone views, using only loopback/synthetic data."""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from flow_controller.core.mexa_controller import MexaController
from flow_controller.mexa.app import BridgeWindow
from flow_controller.mexa.bridge import Bridge
from flow_controller.mexa.records import AuditLog, ReceivedSample, make_packet, utc_now
from flow_controller.mexa.protocol import simulated_cycle
from flow_controller.ui.qt_mexa import MexaTab
import uuid


class QtMexaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.controller = MexaController()
        self.addCleanup(self.controller.shutdown)

    def packet(self):
        return make_packet(simulated_cycle(1), str(uuid.uuid4()), 1, simulated=True,
                           validated=False, dry=True, cycle_s=.7)

    def receive(self, p=None):
        self.controller.log = AuditLog(self.directory.name, "receiver")
        self.controller._receive(self.controller.generation, p or self.packet())
        return self.controller.latest

    def wait_for(self, predicate, seconds=4):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            threading.Event().wait(.01)
        return predicate()

    def test_receiver_logs_each_reading_before_emission(self):
        captured = []
        self.controller.sample_received.connect(lambda sample: captured.append(
            json.loads(Path(sample.log_path).read_text())["seq"]))
        self.receive()
        self.assertEqual(captured, [1])
        with self.assertRaisesRegex(ValueError, "Simulated"):
            self.controller.checked_sample()

    def test_log_failure_interrupts_and_clears_last_value(self):
        self.receive()
        interrupted = []
        self.controller.interrupted.connect(interrupted.append)
        with patch.object(self.controller.log, "write", side_effect=OSError("disk full")):
            self.controller._receive(self.controller.generation, self.packet())
        self.assertIsNone(self.controller.latest)
        self.assertTrue(interrupted)
        self.assertIn("log failed", self.controller.status)

    def test_old_generation_callbacks_ignored(self):
        self.receive()
        generation = self.controller.generation
        self.controller.disconnect_bridge()
        self.controller._receive(generation, self.packet())
        self.controller._set_status(generation, False, "obsolete")
        self.assertIsNone(self.controller.latest)
        self.assertEqual(self.controller.status, "MEXA disconnected")

    def test_snapshot_blanks_stale_invalid_and_future_data(self):
        sample = self.receive()
        now = datetime.now()
        self.assertTrue(self.controller.csv_snapshot(now)["mexa_valid"])
        for stamp in (now + timedelta(seconds=6), now - timedelta(seconds=1)):
            snapshot = self.controller.csv_snapshot(stamp)
            self.assertIsNone(snapshot["mexa_no_ppm"])
            self.assertFalse(snapshot["mexa_valid"])
        sample.packet["valid"] = False
        self.assertIsNone(self.controller.csv_snapshot(now)["mexa_no_ppm"])

    def test_views_start_disconnected_without_io(self):
        with patch("flow_controller.mexa.app.Bridge") as bridge:
            window = BridgeWindow()
            tab = MexaTab(self.controller)
            self.addCleanup(window.close)
            self.addCleanup(tab.close)
            self.assertTrue(window.start_button.isEnabled())
            self.assertFalse(window.stop_button.isEnabled())
            self.assertFalse(window.validated.isChecked())
            self.assertTrue(tab.connect_button.isEnabled())
            bridge.assert_not_called()

    def test_full_simulated_bridge_to_qt_receiver_log_and_view(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        key = "loopback-integration-test-shared-key"
        bridge = Bridge(host="127.0.0.1", port=port, token=key, serial_port="NEVER",
                        directory=self.directory.name, simulated=True, dry=True)
        self.addCleanup(bridge.stop)
        self.controller.connect_bridge("127.0.0.1", port, key, self.directory.name)
        self.assertTrue(self.wait_for(lambda: self.controller.latest is not None))
        tab = MexaTab(self.controller)
        self.addCleanup(tab.close)
        self.assertIn("SIMULATION", tab.readings.text())
        self.assertIn("Simulated", tab.quality.text())
        records = self.controller.log.path.read_text().splitlines()
        self.assertEqual(len(records), 1)
        self.assertTrue(json.loads(records[0])["simulated"])
        self.controller.disconnect_bridge()
        self.assertIsNone(self.controller.latest)


if __name__ == "__main__":
    unittest.main()
