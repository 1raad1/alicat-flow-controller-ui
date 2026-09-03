"""Paired measurement persistence, GUI orchestration and localhost UDP contracts."""

from copy import deepcopy
import csv
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import PropertyMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from flow_controller.core.optimisation import Experiment, MeasurementWindow
from flow_controller.core.optimiser_controller import OptimiserController
from flow_controller.core.session import FlowSession, MODE_STAGED
from flow_controller.core.udp_listener import MAX_PACKET_BYTES, UdpCommandListener
from flow_controller.domain.bayesian import SearchConfig


POINT = [.3, 1.2, .7]


def settings(mode="map_no_pressure"):
    return SearchConfig(10, ((.15, .65), (1.05, 1.6), (.5, .85)),
                        initial_points=4, window_seconds=5, objective_mode=mode)


def pressure_for(experiment):
    trial = experiment.pending or experiment.trials[-1]
    start = datetime.fromisoformat(trial["window"]["start"])
    return {
        "protocol": "flow-pressure-v1", "type": "pressure_summary", "request_id": "summary-request",
        "experiment_id": experiment.data["id"], "trial_id": trial["id"], "capture_id": trial["capture_id"],
        "start": start.isoformat(), "end": (start + timedelta(seconds=4.999)).isoformat(),
        "sample_rate_hz": 1000, "sample_count": 5000, "units": "Pa", "channel": "pressure",
        "calibration_id": "cal-1", "rms_pa": 2, "peak_abs_pa": 3,
        "dominant_frequency_hz": 125, "dominant_amplitude_pa": 2,
        "quality": {"clipped": False, "nonfinite": False},
        "analysis": {"id": "labview-v1", "band_hz": [50, 300], "window": "flattop",
                     "segment_samples": 1000, "overlap_samples": 500,
                     "detrend": "constant", "amplitude_convention": "rms_spectrum"},
    }


def record_flow_window(experiment, offset=0):
    config = experiment.config
    targets = config.targets(POINT)
    window = MeasurementWindow(config, targets, {})
    start = datetime(2026, 9, 3, 12) + timedelta(seconds=offset)
    for seconds in (0, 2, 5):
        window.add(start + timedelta(seconds=seconds), targets)
    experiment.record_window(window.finish())


class PressureExperimentIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.experiment = Experiment.create(self.root / "campaign.json", settings())
        self.experiment.add_trial({"point": POINT, "method": "test"})
        record_flow_window(self.experiment)

    def test_mapping_requires_pressure_then_freezes_persists_and_exports_pair(self):
        exp = self.experiment
        with self.assertRaisesRegex(ValueError, "pressure"):
            exp.complete(100, 10)
        payload = pressure_for(exp)
        self.assertTrue(exp.attach_pressure(payload))
        exp.complete(100, 10)
        trial = exp.trials[0]
        self.assertEqual(trial["condition_log"]["pressure"], trial["pressure"])
        self.assertIsNot(trial["condition_log"]["pressure"], trial["pressure"])
        payload["rms_pa"] = 999
        reloaded = Experiment.load(exp.path)
        self.assertEqual(reloaded.trials[0], trial)
        self.assertEqual(reloaded.trials[0]["condition_log"]["pressure"]["rms_pa"], 2)
        destination = self.root / "campaign.csv"
        reloaded.export_csv(destination)
        with destination.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(float(row["pressure_rms_pa"]), 2)
        self.assertEqual(row["capture_id"], trial["capture_id"])
        self.assertEqual(json.loads(row["pressure_summary_json"]), trial["pressure"])
        self.assertEqual(json.loads(row["condition_log_json"]), trial["condition_log"])

    def test_wrong_identity_stale_nonoverlap_and_clipped_rejected(self):
        for key in ("experiment_id", "trial_id", "capture_id"):
            payload = pressure_for(self.experiment)
            payload[key] = "wrong"
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.experiment.attach_pressure(payload)
        for shift in (-10, 10):
            payload = pressure_for(self.experiment)
            for key in ("start", "end"):
                payload[key] = (datetime.fromisoformat(payload[key]) + timedelta(seconds=shift)).isoformat()
            with self.subTest(shift=shift), self.assertRaisesRegex(ValueError, "window"):
                self.experiment.attach_pressure(payload)
        payload = pressure_for(self.experiment)
        payload["quality"]["clipped"] = True
        with self.assertRaisesRegex(ValueError, "clipped"):
            self.experiment.attach_pressure(payload)
        self.assertNotIn("pressure", self.experiment.pending)

    def test_duplicate_is_idempotent_after_completion_but_different_payload_rejected(self):
        payload = pressure_for(self.experiment)
        self.assertTrue(self.experiment.attach_pressure(payload))
        original_bytes = self.experiment.path.read_bytes()
        payload["request_id"] = "transport-retry"
        self.assertFalse(self.experiment.attach_pressure(payload))
        self.assertEqual(self.experiment.path.read_bytes(), original_bytes)
        self.experiment.complete(100, 10)
        completed_bytes = self.experiment.path.read_bytes()
        self.assertFalse(self.experiment.attach_pressure(payload))
        self.assertEqual(self.experiment.path.read_bytes(), completed_bytes)
        payload["rms_pa"] = 1.5
        with self.assertRaisesRegex(ValueError, "different pressure"):
            self.experiment.attach_pressure(payload)

    def test_campaign_signature_rejects_changed_analysis_on_second_trial(self):
        exp = self.experiment
        exp.attach_pressure(pressure_for(exp))
        exp.complete(100, 10)
        exp.add_trial({"point": POINT, "method": "repeat"})
        record_flow_window(exp, offset=30)
        payload = pressure_for(exp)
        payload["analysis"]["segment_samples"] = 2000
        with self.assertRaisesRegex(ValueError, "settings differ"):
            exp.attach_pressure(payload)
        self.assertIsNone(exp.pending.get("pressure"))

    def test_reset_capture_changes_identity_and_forbids_saved_window_reset(self):
        exp = self.experiment
        with self.assertRaisesRegex(ValueError, "saved window"):
            exp.reset_capture()
        exp.invalidate("retry")
        exp.add_trial({"point": POINT, "method": "retry"})
        previous = exp.pending["capture_id"]
        exp.reset_capture()
        self.assertNotEqual(previous, exp.pending["capture_id"])
        self.assertEqual(Experiment.load(exp.path).pending["capture_id"], exp.pending["capture_id"])

    def test_legacy_schema_one_and_two_no_only_are_loaded_without_rewrite(self):
        for schema in (1, 2):
            with self.subTest(schema=schema):
                path = self.root / f"legacy-{schema}.json"
                exp = Experiment.create(path, settings("minimise_no"))
                exp.add_trial({"point": POINT, "method": "test"})
                record_flow_window(exp)
                exp.complete(100, 10)
                legacy = deepcopy(exp.data)
                legacy["schema"] = schema
                legacy["config"].pop("objective_mode")
                legacy["config"].pop("pressure_metric")
                legacy["trials"][0].pop("capture_id")
                legacy["trials"][0].pop("condition_log")
                path.write_text(json.dumps(legacy), encoding="utf-8")
                before = path.read_bytes()
                loaded = Experiment.load(path)
                self.assertEqual(loaded.config.objective_mode, "minimise_no")
                self.assertEqual(loaded.data, json.loads(before))
                self.assertEqual(path.read_bytes(), before)


class PressureControllerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        preferences = patch.dict(os.environ, {
            "FLOW_CONTROLLER_UNIT_PREFS": str(self.root / "units.json"),
            "FLOW_CONTROLLER_COMBUSTION_PREFS": str(self.root / "combustion.json"),
        })
        preferences.start()
        self.addCleanup(preferences.stop)
        self.session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.session.selection = {"A": ("NH3", "Zone 1"), "B": ("H2", "Zone 1"),
                                  "C": ("Air", "Zone 1"), "D": ("Air", "Zone 2"),
                                  "P": ("CH4", "Pilot")}
        self.session.unit_prefs = {}
        self.session._rebuild_assignments()
        self.session.set_operating_mode(MODE_STAGED)
        self.session.is_monitoring = self.session.controllers_connected = True
        self.session.poll_interval_s = 1
        self.session.log_dir = str(self.root)
        self.session.log_destination = str(self.root / "flows.csv")
        self.controller = OptimiserController(self.session)
        self.controller.create(self.root / "mapping.json", settings())
        self.controller.experiment.add_trial({"point": POINT, "method": "test"})
        self.targets = settings().targets(POINT)
        self.clock = datetime(2026, 9, 3, 12)
        mock_datetime = patch("flow_controller.core.optimiser_controller.datetime")
        self.mock_datetime = mock_datetime.start()
        self.mock_datetime.now.side_effect = lambda: self.clock
        self.addCleanup(mock_datetime.stop)
        self.addCleanup(self.cleanup_session)
        self.publish(0)

    def cleanup_session(self):
        worker = self.controller.pressure_worker
        if worker:
            worker.wait(5000)
            self.app.processEvents()
        self.controller.shutdown()
        self.session.is_monitoring = self.session.controllers_connected = False
        self.session.shutdown()

    def publish(self, seconds):
        self.clock = datetime(2026, 9, 3, 12) + timedelta(seconds=seconds)
        samples = {unit: {"flow": self.targets.get(role, 0), "sp": self.targets.get(role, 0)}
                   for role, unit in self.session.assignments.items() if unit}
        self.session._publish_pass(self.clock, samples, list(samples))

    def packet(self, kind, request=None):
        return dict(request or self.controller.labview_request(), type=kind, request_id=f"request-{kind}")

    def capture_window(self):
        request = self.controller.arm_labview(True, True)
        self.assertTrue(self.controller.handle_labview_packet(self.packet("start", request))["ok"])
        self.publish(2)
        self.publish(5)
        self.assertTrue(self.controller.handle_labview_packet(self.packet("stop", request))["ok"])
        return request

    def await_import(self):
        deadline = time.monotonic() + 5
        while self.controller.pressure_worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertIsNone(self.controller.pressure_worker, "Pressure worker did not finish")

    def test_arm_requires_local_confirmations_and_fresh_telemetry(self):
        for pilot_off, settled in ((False, True), (True, False)):
            with self.assertRaisesRegex(ValueError, "Confirm"):
                self.controller.arm_labview(pilot_off, settled)
        self.clock += timedelta(seconds=20)
        with self.assertRaisesRegex(ValueError, "Fresh"):
            self.controller.arm_labview(True, True)
        self.assertFalse(self.controller.labview_armed)
        self.assertTrue(self.session.setpoint_queue.empty())

    def test_unarmed_unknown_protocol_and_bad_correlations_cannot_start(self):
        packet = self.packet("start")
        self.assertFalse(self.controller.handle_labview_packet(packet)["ok"])
        self.controller.arm_labview(True, True)
        for key in ("protocol", "experiment_id", "trial_id", "capture_id"):
            invalid = dict(packet, **{key: "wrong"})
            with self.subTest(key=key):
                self.assertFalse(self.controller.handle_labview_packet(invalid)["ok"])
        self.assertFalse(self.session.logging_active)
        self.assertIsNone(self.controller.capture)
        self.assertTrue(self.session.setpoint_queue.empty())

    def test_correlated_start_status_stop_duplicates_preserve_one_window_and_log(self):
        request = self.controller.arm_labview(True, True)
        packet = self.packet("start", request)
        ack = self.controller.handle_labview_packet(packet)
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["state"], "capturing")
        self.assertEqual(ack["capture_id"], request["capture_id"])
        capture, log = self.controller.capture, self.session.log_path
        self.assertIsNotNone(ack["window_start"])
        self.assertTrue(self.controller.handle_labview_packet(packet)["ok"])
        self.assertIs(capture, self.controller.capture)
        self.assertEqual(log, self.session.log_path)
        self.assertEqual(self.controller.handle_labview_packet(self.packet("status", request))["state"], "capturing")
        self.publish(2)
        self.publish(5)
        stop = self.packet("stop", request)
        self.assertEqual(self.controller.handle_labview_packet(stop)["state"], "window_saved")
        saved = self.controller.experiment.path.read_bytes()
        self.assertTrue(self.controller.handle_labview_packet(stop)["ok"])
        self.assertEqual(saved, self.controller.experiment.path.read_bytes())
        self.assertFalse(self.session.logging_active)
        self.assertTrue(Path(log).is_file())
        self.assertTrue(self.session.setpoint_queue.empty())

    def test_summary_attachment_and_stale_completed_stop_leave_new_log_open(self):
        request = self.capture_window()
        summary = pressure_for(self.controller.experiment)
        self.assertEqual(self.controller.handle_labview_packet(summary)["state"], "pressure_saved")
        self.controller.complete(100, 10, basis_confirmed=True)
        self.assertEqual(self.controller.handle_labview_packet(summary)["state"], "completed")
        self.controller.experiment.add_trial({"point": POINT, "method": "repeat"})
        self.publish(10)
        second = self.controller.arm_labview(True, True)
        self.assertTrue(self.controller.handle_labview_packet(second)["ok"])
        active_log = self.session.log_path
        ack = self.controller.handle_labview_packet(self.packet("stop", request))
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["state"], "completed")
        self.assertTrue(self.session.logging_active)
        self.assertEqual(self.session.log_path, active_log)
        self.assertIsNotNone(self.controller.capture)

    def test_configuration_disarms_and_discard_changes_capture_identity(self):
        request = self.controller.arm_labview(True, True)
        self.session.max_flow_changed.emit("A", 100)
        self.assertFalse(self.controller.labview_armed)
        self.assertFalse(self.controller.handle_labview_packet(request)["ok"])
        request = self.controller.arm_labview(True, True)
        self.assertTrue(self.controller.handle_labview_packet(request)["ok"])
        self.controller.cancel_window()
        self.assertNotEqual(request["capture_id"], self.controller.experiment.pending["capture_id"])
        self.assertFalse(self.controller.handle_labview_packet(request)["ok"])
        self.assertFalse(self.session.logging_active)

    def file_ready(self, raw_file):
        summary = pressure_for(self.controller.experiment)
        for key in ("end", "sample_count", "units", "rms_pa", "peak_abs_pa",
                    "dominant_frequency_hz", "dominant_amplitude_pa"):
            summary.pop(key)
        summary.update(type="file_ready", raw_file=str(raw_file), format="csv", column="pressure_pa")
        return summary

    def test_async_file_failure_retains_pending_and_reports_error_status(self):
        request = self.capture_window()
        ack = self.controller.handle_labview_packet(self.file_ready(self.root / "missing.csv"))
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["state"], "processing")
        self.await_import()
        status = self.controller.handle_labview_packet(self.packet("status", request))
        self.assertEqual(status["state"], "pressure_error")
        self.assertIn("Pressure import failed", status["pressure_error"])
        self.assertEqual(self.controller.experiment.pending["status"], "pending")
        self.assertNotIn("pressure", self.controller.experiment.pending)

    def test_real_csv_file_ready_end_to_end_then_no_save(self):
        import numpy as np
        self.capture_window()
        path = self.root / "capture.csv"
        values = 3 + 2 * np.sin(2 * np.pi * 125 * np.arange(5000) / 1000)
        with path.open("w", encoding="utf-8") as handle:
            handle.write("pressure_pa\n")
            handle.writelines(f"{value}\n" for value in values)
        packet = self.file_ready(path)
        self.assertTrue(self.controller.handle_labview_packet(packet)["ok"])
        worker = self.controller.pressure_worker
        self.assertTrue(self.controller.handle_labview_packet(packet)["ok"])
        self.assertIs(worker, self.controller.pressure_worker)
        self.await_import()
        trial = self.controller.experiment.pending
        self.assertAlmostEqual(trial["pressure"]["rms_pa"], np.sqrt(2), places=9)
        self.assertEqual(trial["pressure"]["dominant_frequency_hz"], 125)
        self.controller.complete(100, 10, basis_confirmed=True)
        reloaded = Experiment.load(self.controller.experiment.path)
        self.assertEqual(reloaded.trials[0]["status"], "completed")
        self.assertEqual(reloaded.trials[0]["condition_log"]["pressure"]["capture_id"], packet["capture_id"])

    def test_legacy_log_stop_retains_csv_behavior_and_requires_arming_for_window(self):
        self.session._on_udp_command("log")
        self.assertTrue(self.session.logging_active)
        self.assertIsNone(self.controller.capture)
        self.session._on_udp_command("stop")
        self.assertFalse(self.session.logging_active)
        self.controller.arm_labview(True, True)
        self.session._on_udp_command("log")
        self.assertIsNotNone(self.controller.capture)
        self.publish(2)
        self.publish(5)
        self.session._on_udp_command("stop")
        self.assertIsNotNone(self.controller.experiment.pending["window"])
        self.assertFalse(self.session.logging_active)

    def test_delayed_live_start_reports_actual_averaging_boundary(self):
        baseline = SimpleNamespace(problem=lambda **_: "", log_path=str(self.root / "mexa.jsonl"),
                                   packet={"source_id": "daq-source", "seq": 1,
                                           "acquired_at": self.clock.astimezone(timezone.utc).isoformat()})
        clock = [100.0]
        self.controller._clock = lambda: clock[0]
        with patch.object(Experiment, "response_delay_seconds", new_callable=PropertyMock, return_value=3), \
                patch.object(self.session.mexa, "checked_sample", return_value=baseline):
            request = self.controller.arm_labview(True, True, live=True)
            start = self.controller.handle_labview_packet(request)
            self.assertEqual(start["state"], "waiting_for_analyser")
            self.assertIsNone(start["window_start"])
            self.assertFalse(self.controller.handle_labview_packet(self.packet("stop", request))["ok"])
            self.assertTrue(self.session.logging_active)
            self.publish(3)
            clock[0] += 3
            self.controller._check_live_freshness()
            status = self.controller.handle_labview_packet(self.packet("status", request))
            self.assertEqual(status["state"], "capturing")
            self.assertEqual(status["window_start"], self.clock.astimezone(timezone.utc).isoformat())
            self.assertEqual(status["minimum_recording_s"], 5)
            self.controller.cancel_window()

    def test_real_tdms_selected_channel_end_to_end(self):
        import numpy as np
        try:
            from nptdms import ChannelObject, TdmsWriter
        except ImportError:
            self.skipTest("Optional npTDMS dependency is not installed")
        self.capture_window()
        path = self.root / "capture.tdms"
        values = 10 + 4 * np.sin(2 * np.pi * 125 * np.arange(5000) / 1000)
        with TdmsWriter(path) as writer:
            writer.write_segment([ChannelObject("DAQ", "pressure", values),
                                  ChannelObject("DAQ", "unused", np.zeros(16))])
        packet = self.file_ready(path)
        packet.update(format="tdms", group="DAQ", scale_pa_per_unit=2)
        self.assertTrue(self.controller.handle_labview_packet(packet)["ok"])
        self.await_import()
        trial = self.controller.experiment.pending
        self.assertAlmostEqual(trial["pressure"]["rms_pa"], 8 / np.sqrt(2), places=9)
        self.assertAlmostEqual(trial["pressure"]["dominant_amplitude_pa"], 8 / np.sqrt(2), places=9)
        self.assertEqual(trial["pressure"]["dominant_frequency_hz"], 125)
        self.controller.complete(100, 10, basis_confirmed=True)
        self.assertEqual(Experiment.load(self.controller.experiment.path).trials[0]["status"], "completed")


class PressureUdpIntegrationTests(unittest.TestCase):
    def test_same_socket_json_ack_sender_legacy_and_bad_datagrams(self):
        ready = threading.Event()
        packets, commands = queue.Queue(), queue.Queue()
        listener = UdpCommandListener(port=0, on_ready=lambda *_: ready.set(), on_command=commands.put)

        def acknowledge(packet, sender):
            packets.put((packet, sender))
            listener.reply(sender, {"protocol": "flow-pressure-v1", "type": "ack", "ok": True,
                                    "request_id": packet["request_id"]})

        listener._on_packet = acknowledge
        listener.start()
        self.addCleanup(listener.stop)
        self.assertTrue(ready.wait(2))
        address = listener._socket.getsockname()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(client.close)
        client.bind(("127.0.0.1", 0))
        client.settimeout(2)
        packet = {"protocol": "flow-pressure-v1", "type": "status", "request_id": "udp-1"}
        client.sendto(json.dumps(packet).encode(), address)
        data, sender = client.recvfrom(65536)
        self.assertEqual(sender, address)
        self.assertEqual(json.loads(data)["request_id"], "udp-1")
        received, original_sender = packets.get(timeout=2)
        self.assertEqual(received, packet)
        self.assertEqual(original_sender, client.getsockname())
        for command in (b" LOG \n", b"stop"):
            client.sendto(command, address)
        self.assertEqual([commands.get(timeout=2), commands.get(timeout=2)], ["log", "stop"])
        for invalid in (b'{"broken":', b"{" + b"x" * MAX_PACKET_BYTES):
            client.sendto(invalid, address)
            reply, _ = client.recvfrom(65536)
            self.assertFalse(json.loads(reply)["ok"])
        self.assertTrue(packets.empty())
        listener.stop()
        listener._thread.join(timeout=2)
        self.assertFalse(listener._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
