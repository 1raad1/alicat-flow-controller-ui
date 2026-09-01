"""Offscreen integration tests for the guarded analyser-response workflow."""

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import math
import os
from pathlib import Path
from queue import Empty
import tempfile
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMessageBox

from flow_controller.core.optimisation import Experiment
from flow_controller.core.optimiser_controller import OptimiserController
from flow_controller.core.session import FlowSession, MODE_STAGED
from flow_controller.domain.bayesian import SearchConfig
from flow_controller.mexa.protocol import simulated_cycle
from flow_controller.mexa.records import ReceivedSample, make_packet
from flow_controller.ui.qt_operation_tab import OperationTab


class QtAnalyserResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        preferences = patch.dict(os.environ, {
            "FLOW_CONTROLLER_UNIT_PREFS": str(Path(self.directory.name) / "units.json"),
            "FLOW_CONTROLLER_COMBUSTION_PREFS": str(Path(self.directory.name) / "combustion.json"),
        })
        preferences.start()
        self.addCleanup(preferences.stop)
        self.session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.session.unit_prefs = {}
        self.session.selection = {
            "A": ("NH3", "Zone 1"), "B": ("H2", "Zone 1"),
            "C": ("Air", "Zone 1"), "D": ("Air", "Zone 2"),
            "P": ("CH4", "Pilot"),
        }
        self.session._rebuild_assignments()
        self.session.set_operating_mode(MODE_STAGED)
        self.session.is_monitoring = True
        self.session.controllers_connected = True
        self.session._set_estop_armed(True)
        self.session.poll_interval_s = 1
        self.wall = datetime(2026, 8, 27, 12)
        self.mono = 1000.0
        self.datetime_patches = [
            patch("flow_controller.core.optimiser_controller.datetime"),
            patch("flow_controller.core.analyser_response_controller.datetime"),
        ]
        for item in self.datetime_patches:
            mocked = item.start()
            mocked.now.side_effect = lambda tz=None: (
                self.wall.replace(tzinfo=timezone.utc) if tz else self.wall)
            self.addCleanup(item.stop)
        self.time_patch = patch(
            "flow_controller.mexa.records.time.time",
            side_effect=lambda: self.wall.astimezone(timezone.utc).timestamp())
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.controller = OptimiserController(self.session, clock=lambda: self.mono)
        config = SearchConfig(
            10, ((.15, .65), (1.05, 1.6), (.5, .85)),
            initial_points=4, window_seconds=5)
        self.controller.create(Path(self.directory.name) / "response.fcbo.json", config)
        self.controller.experiment.add_trial({"point": [.3, 1.2, .7], "method": "test"})
        self.targets = config.targets([.3, 1.2, .7])
        self.source = str(uuid.uuid4())
        self.seq = 0
        self.addCleanup(self.cleanup_session)
        self.publish(0, self.targets)

    def cleanup_session(self):
        self.session._ramps.cancel_all()
        self.controller.shutdown()
        self.session.is_monitoring = False
        self.session.controllers_connected = False
        self.session.shutdown()

    def publish(self, seconds, role_flows):
        self.wall = datetime(2026, 8, 27, 12) + timedelta(seconds=seconds)
        samples = {}
        for role, unit in self.session.assignments.items():
            if unit:
                value = float(role_flows.get(role, 0.0))
                samples[unit] = {"flow": value, "sp": value}
        self.session._publish_pass(self.wall, samples, list(samples))

    def mexa(self, seconds, *, no_ppm=50.0, seq=None, log=True, **changes):
        self.wall = datetime(2026, 8, 27, 12) + timedelta(seconds=seconds)
        self.seq = self.seq + 1 if seq is None else seq
        packet = make_packet(
            simulated_cycle(self.seq), self.source, self.seq,
            simulated=False, validated=True, dry=True, cycle_s=.7)
        packet["acquired_at"] = self.wall.astimezone(timezone.utc).isoformat()
        packet["no_ppm"] = no_ppm
        packet.update(changes)
        sample = ReceivedSample(
            packet, packet["acquired_at"], time.monotonic(),
            str(Path(self.directory.name) / "received.jsonl") if log else "")
        self.session.mexa.latest = sample
        self.session.mexa.sample_received.emit(sample)
        return sample

    def conditions(self, *, role="nh3_rich", delta=5.0):
        first = dict(self.targets)
        second = dict(first)
        second[role] += delta
        self.publish(0, first)
        self.controller.response.store_condition("A")
        self.publish(0, second)
        self.controller.response.store_condition("B")
        self.publish(0, first)
        return first, second

    def start_response(self, *, role="nh3_rich"):
        first, second = self.conditions(role=role)
        self.mexa(0)
        self.controller.response.start(confirmed=True)
        return first, second

    def baseline_until_command(self):
        for second in range(1, 17):
            self.mexa(second, no_ppm=50 + .02 * math.sin(second))
        self.assertEqual(self.controller.response.phase, "response")

    def drain_commands(self):
        result = []
        while True:
            try:
                result.append(self.session.setpoint_queue.get_nowait())
            except Empty:
                return result

    def response_run(self, delay=5.0):
        samples = [
            {"timestamp": 100.0, "elapsed_s": -1.0, "no_ppm": 50.0,
             "seq": 1, "phase": "baseline", "flow_stable": True},
            {"timestamp": 101.0, "elapsed_s": 0.0, "no_ppm": 50.0,
             "seq": 2, "phase": "response", "flow_stable": False},
            {"timestamp": 102.0, "elapsed_s": 1.0, "no_ppm": 80.0,
             "seq": 3, "phase": "response", "flow_stable": True},
        ]
        return {
            "successful": True, "source_id": self.source,
            "recommended_delay_s": delay, "command_to_change_s": .5,
            "command_to_stable_s": 3.0, "flow_to_stable_s": 2.0,
            "t10_s": .6, "t50_s": 1.0, "t90_s": 2.0,
            "rise_10_90_s": 1.4, "baseline_no_ppm": 50.0,
            "final_no_ppm": 80.0, "criteria": {"fixture": 1},
            "caveat": "Synthetic controller fixture", "raw_samples": samples,
        }

    def test_storing_conditions_is_read_only_and_start_guards_confirmation_a_and_mexa(self):
        first, second = self.conditions()
        self.assertEqual(self.drain_commands(), [])
        self.mexa(0)
        with self.assertRaisesRegex(ValueError, "Confirm"):
            self.controller.response.start(confirmed=False)
        self.assertEqual(self.drain_commands(), [])
        self.publish(0, second)
        with self.assertRaisesRegex(ValueError, "condition A"):
            self.controller.response.start(confirmed=True)
        self.assertEqual(self.drain_commands(), [])
        self.publish(0, first)
        self.session.mexa.latest = replace(self.session.mexa.latest, log_path="")
        with self.assertRaisesRegex(ValueError, "Save received MEXA logs"):
            self.controller.response.start(confirmed=True)
        self.assertEqual(self.drain_commands(), [])
        self.mexa(0)
        self.publish(6, first)
        with self.assertRaisesRegex(ValueError, "Stale"):
            self.controller.response.start(confirmed=True)
        self.assertEqual(self.drain_commands(), [])

    def test_baseline_commands_b_through_normal_session_api(self):
        _first, second = self.start_response()
        with patch.object(
                self.session, "set_role_setpoint",
                wraps=self.session.set_role_setpoint) as setter:
            self.baseline_until_command()
        setter.assert_called_once_with("nh3_rich", second["nh3_rich"])
        self.assertEqual(self.drain_commands(), [("A", second["nh3_rich"])])

    def test_configured_ramp_policy_is_not_bypassed(self):
        self.session.set_ramp_disabled("C", False)
        self.start_response(role="rich_air")
        self.assertIn("ramped", self.controller.response.transition_text())
        with patch.object(self.session, "start_ramp", return_value=True) as ramp:
            self.baseline_until_command()
        self.assertTrue(ramp.called)
        self.assertEqual(ramp.call_args.args[:2], ("rich_air", self.controller.response.condition("B")["target_flows"]["rich_air"]))
        self.assertEqual(self.drain_commands(), [])

    def test_b_flow_gate_then_detector_result_is_persisted_and_selected(self):
        _first, condition_b = self.start_response()
        self.baseline_until_command()
        for timestamp in range(17, 40):
            value = 50.0 + 50.0 * (1.0 - math.exp(-(timestamp - 16) / 2.0))
            self.mexa(timestamp, no_ppm=value)
        self.assertIsNone(self.controller.experiment.selected_response_run)
        self.publish(40, condition_b)
        self.publish(44, condition_b)
        for timestamp in range(40, 66):
            self.mexa(timestamp, no_ppm=100.0)
            if self.controller.response.phase == "complete":
                break
        run = self.controller.experiment.selected_response_run
        self.assertIsNotNone(run)
        self.assertGreaterEqual(run["recommended_delay_s"], 5)
        self.assertEqual(self.controller.experiment.response_delay_seconds, run["recommended_delay_s"])
        self.assertEqual(run["provenance"], str(Path(self.directory.name) / "received.jsonl"))

    def test_cancel_link_loss_and_configuration_change_send_no_recovery_commands(self):
        for event in ("cancel", "link", "configuration"):
            with self.subTest(event=event):
                self.start_response()
                self.drain_commands()
                if event == "cancel":
                    self.controller.response.cancel()
                elif event == "link":
                    self.session.mexa.interrupted.emit("lost")
                else:
                    self.session.mode_changed.emit(self.session.operating_mode)
                self.assertFalse(self.controller.response.active)
                self.assertEqual(self.drain_commands(), [])
                self.controller.response.cancel() if self.controller.response.active else None

    def test_flow_outside_confirmed_transition_envelope_cancels(self):
        _first, second = self.start_response()
        self.baseline_until_command()
        self.drain_commands()
        outside = dict(second)
        outside["nh3_rich"] += 20
        self.publish(17, outside)
        self.assertFalse(self.controller.response.active)
        self.assertIn("transition envelope", self.controller.response.last_message)
        self.assertEqual(self.drain_commands(), [])

    def test_invalid_simulated_unlogged_and_gapped_mexa_are_rejected(self):
        variants = [
            ("simulated", {"simulated": True}, "Simulated"),
            ("unvalidated", {"validated": False}, "Validate"),
            ("invalid", {"valid": False, "alarms": ["filter"]}, "filter"),
        ]
        for name, changes, message in variants:
            with self.subTest(name=name):
                self.conditions()
                self.mexa(0, **changes)
                with self.assertRaisesRegex(ValueError, message):
                    self.controller.response.start(confirmed=True)
        self.start_response()
        self.mexa(1)
        self.mexa(2, seq=self.seq + 2)
        self.assertFalse(self.controller.response.active)
        self.assertIn("sequence", self.controller.response.last_message)

    def test_unstable_baseline_times_out_without_actuation(self):
        first, _second = self.conditions()
        self.publish(0, first)
        self.mexa(0)
        clock = [0.0]
        self.controller.response._wall_clock = lambda: clock[0]
        self.controller.response.start(confirmed=True)
        clock[0] = 121.0
        self.controller.response._tick()
        self.assertFalse(self.controller.response.active)
        self.assertIn("baseline", self.controller.response.last_message)
        self.assertEqual(self.drain_commands(), [])

    def test_response_timeout_uses_elapsed_time_across_clock_offset(self):
        clock = [2_000_000_000.0]
        self.controller.response._wall_clock = lambda: clock[0]
        first, _second = self.start_response()
        self.baseline_until_command()
        self.publish(16, first)
        clock[0] += 1.0
        self.controller.response._tick()
        self.assertTrue(self.controller.response.active)

    def test_response_ui_exists_and_confirmation_refusal_sends_nothing(self):
        self.conditions()
        self.mexa(0)
        tab = OperationTab(self.session, optimiser=self.controller)
        self.addCleanup(tab.close)
        pane = tab.optimiser_pane
        self.assertGreaterEqual(pane.tabs.indexOf(pane.response_start_button.parentWidget()), -1)
        self.assertEqual(set(pane.response_store_buttons), {"A", "B"})
        self.assertEqual(pane.response_start_button.text(), "Start A → B response test")
        with patch("flow_controller.ui.qt_optimiser.QMessageBox.warning",
                   return_value=QMessageBox.StandardButton.No):
            pane._start_response()
        self.assertFalse(self.controller.response.active)
        self.assertEqual(self.drain_commands(), [])

    def test_calibrated_delay_excludes_transient_samples_and_records_provenance(self):
        self.conditions()
        run = self.controller.experiment.record_response_run(self.response_run(delay=5))
        self.publish(0, self.targets)
        self.mexa(0, no_ppm=10)
        self.controller.start_window(True, True, live=True)
        self.assertIsNotNone(self.controller.settle_wait)
        self.assertIsNone(self.controller.capture)
        self.mono += 4
        self.mexa(4, no_ppm=999)
        self.controller._check_live_freshness()
        self.assertIsNone(self.controller.mexa_capture)
        self.mono += 1
        self.publish(5, self.targets)
        self.controller._check_live_freshness()
        self.assertIsNotNone(self.controller.mexa_capture)
        self.mexa(5, no_ppm=100)
        for second, value in ((7, 102), (10, 104)):
            self.publish(second, self.targets)
            self.mexa(second, no_ppm=value)
        window = self.controller.finish_window()
        self.assertEqual(window["mexa"]["no_ppm"], 102)
        self.assertEqual(window["pre_window_delay_s"], 5)
        self.assertEqual(window["response_run_id"], run["id"])
        self.assertEqual(window["total_observation_s"], 10)

    def test_delay_cancellation_and_freshness_fault_discard_cleanly(self):
        self.conditions()
        self.controller.experiment.record_response_run(self.response_run(delay=5))
        for fault in ("cancel", "link", "stale"):
            with self.subTest(fault=fault):
                self.publish(0, self.targets)
                self.mexa(0)
                self.controller.start_window(True, True, live=True)
                if fault == "cancel":
                    self.controller.cancel_window()
                elif fault == "link":
                    self.session.mexa.interrupted.emit("lost")
                else:
                    self.wall += timedelta(seconds=6)
                    self.controller._check_live_freshness()
                self.assertIsNone(self.controller.settle_wait)
                self.assertIsNone(self.controller.capture)
                self.assertIsNone(self.controller.mexa_capture)
                self.assertIsNone(self.controller.experiment.pending["window"])
                self.assertEqual(self.drain_commands(), [])


if __name__ == "__main__":
    unittest.main()
