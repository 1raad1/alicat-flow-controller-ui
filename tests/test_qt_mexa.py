"""Receiver lifecycle and standalone views, using only loopback/synthetic data."""

from datetime import datetime, timedelta
import json
import csv
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMessageBox

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
        self.controller._receiving = True
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
            self.assertFalse(window.save_logs.isChecked())
            self.assertFalse(window.directory.isEnabled())
            self.assertTrue(tab.save_logs.isChecked())
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

    def test_all_source_and_receiver_logging_combinations(self):
        for source_logging, receiver_logging in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(source=source_logging, receiver=receiver_logging):
                directory = Path(self.directory.name) / f"{source_logging}-{receiver_logging}"
                probe = socket.socket()
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
                probe.close()
                key = "optional-logging-integration-test-key"
                bridge = Bridge(host="127.0.0.1", port=port, token=key, serial_port="NEVER",
                                directory=directory / "source", save_logs=source_logging,
                                simulated=True, dry=True)
                try:
                    self.controller.connect_bridge("127.0.0.1", port, key, directory / "receiver",
                                                   save_logs=receiver_logging)
                    self.assertTrue(self.wait_for(lambda: self.controller.latest is not None))
                    sample = self.controller.latest
                    self.assertEqual(bool(bridge.log), source_logging)
                    self.assertEqual(bool(self.controller.log), receiver_logging)
                    self.assertEqual(bool(sample.log_path), receiver_logging)
                    self.assertEqual((directory / "source").exists(), source_logging)
                    self.assertEqual((directory / "receiver").exists(), receiver_logging)
                    self.assertTrue(self.controller.csv_snapshot(datetime.now())["mexa_valid"])
                    tab = MexaTab(self.controller)
                    try:
                        self.assertIn("SIMULATION", tab.readings.text())
                        self.assertFalse(tab.save_logs.isEnabled())
                        self.assertEqual(tab.save_logs.isChecked(), receiver_logging)
                    finally:
                        tab.close()
                finally:
                    self.controller.disconnect_bridge()
                    self.assertTrue(bridge.stop())

    def test_preview_receiver_never_opens_a_log_and_cannot_supply_live_capture(self):
        p = self.packet()
        p.update(simulated=False, validated=True)
        with patch("flow_controller.core.mexa_controller.StreamClient"), patch(
                "flow_controller.core.mexa_controller.AuditLog", side_effect=AssertionError("Unexpected disk write")) as log:
            self.controller.connect_bridge("127.0.0.1", 61234, "a" * 40, save_logs=False)
            generation = self.controller.generation
            self.controller._receive(generation, p)
            self.assertEqual(self.controller.latest.log_path, "")
            self.assertTrue(self.controller.csv_snapshot(datetime.now())["mexa_valid"])
            with self.assertRaisesRegex(ValueError, "Save received MEXA logs"):
                self.controller.checked_sample()
            self.controller.disconnect_bridge()
            self.controller._receive(generation, p)
            self.controller._receive(self.controller.generation, p)
            self.assertIsNone(self.controller.latest)
            log.assert_not_called()

    def test_reconnect_enables_logging_without_relabeling_preview_sample(self):
        with patch("flow_controller.core.mexa_controller.StreamClient"):
            p = self.packet()
            p.update(simulated=False, validated=True)
            self.controller.connect_bridge("127.0.0.1", 61234, "a" * 40, save_logs=False)
            old_generation = self.controller.generation
            self.controller._receive(old_generation, p)
            preview_sample = self.controller.latest
            self.controller.disconnect_bridge()
            self.controller.connect_bridge("127.0.0.1", 61234, "a" * 40, self.directory.name, save_logs=True)
            self.controller._receive(old_generation, p)
            self.assertIsNone(self.controller.latest)
            self.controller._receive(self.controller.generation, p)
            self.assertTrue(self.controller.checked_sample().log_path)
            self.assertEqual(preview_sample.log_path, "")

    def test_logging_checkbox_controls_folder_fields(self):
        window = BridgeWindow()
        tab = MexaTab(self.controller)
        self.addCleanup(window.close)
        self.addCleanup(tab.close)
        window.save_logs.setChecked(True)
        self.assertTrue(window.directory.isEnabled())
        window.save_logs.setChecked(False)
        self.assertFalse(window.directory.isEnabled())
        tab.save_logs.setChecked(False)
        self.assertFalse(tab.directory.isEnabled())
        self.assertFalse(self.controller.settings["save_logs"])
        rebuilt = MexaTab(self.controller)
        self.addCleanup(rebuilt.close)
        self.assertFalse(rebuilt.save_logs.isChecked())

    def test_bridge_ui_starts_stream_only_without_a_folder(self):
        with patch("flow_controller.mexa.app.Bridge") as bridge:
            bridge.return_value.log = None
            bridge.return_value.running = True
            bridge.return_value.stop.return_value = True
            window = BridgeWindow()
            self.addCleanup(window.close)
            window.simulated.setChecked(True)
            window.directory.clear()
            window._start()
            self.assertFalse(bridge.call_args.kwargs["save_logs"])
            self.assertIn("Stream only", window.log_label.text())
            self.assertFalse(window.save_logs.isEnabled())

    def test_invalid_values_visible_but_excluded_from_valid_csv_and_optimiser(self):
        p = self.packet()
        p.update(no_ppm=-3, o2_percent=26, valid=False, simulated=False,
                 validated=True, alarms=["no_out_of_range", "o2_out_of_range"])
        sample = self.receive(p)
        tab = MexaTab(self.controller)
        window = BridgeWindow()
        self.addCleanup(tab.close)
        self.addCleanup(window.close)
        with patch("flow_controller.mexa.app.Bridge") as fake:
            fake.return_value.running = True
            fake.return_value.stop.return_value = True
            window.bridge = fake.return_value
            window._sample(p)
            for label in (tab.readings, window.readings):
                self.assertIn("INVALID", label.text())
                self.assertIn("-3", label.text())
                self.assertIn("26.00", label.text())
            self.assertIn("0–5000", tab.quality.text())
            self.assertIn("Network: connected", tab.network.text())
            snapshot = self.controller.csv_snapshot(datetime.now())
            self.assertFalse(snapshot["mexa_valid"])
            self.assertIsNone(snapshot["mexa_no_ppm"])
            self.assertEqual(snapshot["mexa_reported_no_ppm"], -3)
            self.assertEqual(snapshot["mexa_reported_o2_percent"], 26)
            self.assertIn("out of range", snapshot["mexa_quality"])
            from flow_controller.core.csv_logger import CsvLogger
            flow_log = CsvLogger()
            flow_path = Path(self.directory.name) / "flows.csv"
            flow_log.start(flow_path, {}, mexa=True)
            self.assertTrue(flow_log.write_row({}, (None, None, None), mexa=snapshot))
            flow_log.stop()
            with flow_path.open(newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["mexa_reported_no_ppm"], "-3")
            self.assertEqual(row["mexa_reported_o2_percent"], "26")
            self.assertEqual(row["mexa_no_ppm"], "")
            self.assertEqual(row["mexa_valid"], "False")
            with self.assertRaisesRegex(ValueError, "out of range"):
                self.controller.checked_sample()
            saved = json.loads(Path(sample.log_path).read_text())
            self.assertEqual(saved["no_ppm"], -3)
            sample.packet["acquired_at"] = (datetime.now().astimezone() - timedelta(seconds=10)).isoformat()
            tab.refresh()
            self.assertNotIn("-3", tab.readings.text())
            self.assertIsNone(self.controller.csv_snapshot(datetime.now())["mexa_reported_no_ppm"])

    def test_out_of_range_stream_reaches_receiver_without_disconnect(self):
        from flow_controller.mexa.protocol import decode_cycle
        from flow_controller.mexa.transport import StreamServer
        frames = {key: bytearray.fromhex(value) for key, value in self.packet()["raw"].items()}
        frames["channels"][21:23] = bytes.fromhex("17 70")  # 6000 ppm, above the configured range
        frames["channels"][-1] = -sum(frames["channels"][:-1]) & 255
        cycle = decode_cycle({key: bytes(value) for key, value in frames.items()})
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        key = "out-of-range-network-test-shared-key"
        server = StreamServer("127.0.0.1", port, key)
        self.addCleanup(server.stop)
        self.controller.connect_bridge("127.0.0.1", port, key, self.directory.name)
        source = str(uuid.uuid4())
        for seq in (1, 2):
            p = make_packet(cycle, source, seq, simulated=False, validated=True, dry=True, cycle_s=.7)
            server.publish(p)
            self.assertTrue(self.wait_for(lambda: self.controller.latest is not None
                                         and self.controller.latest.packet["seq"] == seq))
            self.assertEqual(self.controller.latest.packet["no_ppm"], 6000)
            self.assertFalse(self.controller.latest.packet["valid"])
            self.assertIn("receiving records", self.controller.link_status)
        self.assertEqual(len(self.controller.log.path.read_text().splitlines()), 2)

    def test_mode_buttons_require_enablement_confirmation_and_fresh_state(self):
        with patch("flow_controller.mexa.app.Bridge") as factory:
            window = BridgeWindow()
            self.addCleanup(window.close)
            self.assertFalse(window.meas_button.isEnabled())
            self.assertFalse(window.standby_button.isEnabled())
            window.bridge = factory.return_value
            window.bridge.running = True
            window.bridge.can_request_mode.return_value = True
            window.bridge.stop.return_value = True
            window._tick()
            self.assertFalse(window.meas_button.isEnabled())
            window.enable_controls.setChecked(True)
            self.assertTrue(window.meas_button.isEnabled())
            with patch("flow_controller.mexa.app.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
                window._mode("meas")
            window.bridge.request_mode.assert_not_called()
            window.validated.setChecked(True)
            with patch("flow_controller.mexa.app.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window._mode("meas")
            window.bridge.request_mode.assert_called_once_with("meas")
            self.assertFalse(window.validated.isChecked())
            window.bridge.can_request_mode.return_value = False
            window._tick()
            self.assertFalse(window.meas_button.isEnabled())
            self.assertFalse(window.standby_button.isEnabled())

    def test_local_only_listener_hint_and_network_status_are_explicit(self):
        window = BridgeWindow()
        self.addCleanup(window.close)
        self.assertIn("LOCAL PC ONLY", window.listener_label.text())
        self.assertIn("Not listening", window.listener_label.text())
        window.host.setText("10.97.74.19")
        self.assertNotIn("LOCAL PC ONLY", window.listener_label.text())
        self.assertIn("10.97.74.19:61234", window.listener_label.text())
        self.controller._set_status(self.controller.generation, False, "TCP connection timed out")
        tab = MexaTab(self.controller)
        self.addCleanup(tab.close)
        self.assertIn("TCP connection timed out", tab.network.text())


if __name__ == "__main__":
    unittest.main()
