"""Offscreen Qt tests with fake telemetry and no serial worker."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import time
import uuid
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from flow_controller.core.session import FlowSession, MODE_STAGED
from flow_controller.core.optimiser_controller import OptimiserController
from flow_controller.domain.bayesian import SearchConfig
from flow_controller.ui.qt_main_window import MainWindow
from flow_controller.ui.qt_optimiser import ExperimentDialog, OptimiserPane
from flow_controller.ui.qt_operation_tab import OperationTab
from flow_controller.core.optimisation import Experiment
from mexa_bridge.protocol import simulated_cycle
from mexa_bridge.records import ReceivedSample, make_packet


class QtOptimiserTests(unittest.TestCase):
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
        self.session.selection = {"A": ("NH3", "Zone 1"), "B": ("H2", "Zone 1"),
                                  "C": ("Air", "Zone 1"), "D": ("Air", "Zone 2"),
                                  "P": ("CH4", "Pilot")}
        self.session._rebuild_assignments()
        self.session.set_operating_mode(MODE_STAGED)
        self.session.is_monitoring = True
        self.session.controllers_connected = True
        self.session.poll_interval_s = 1
        self.controller = OptimiserController(self.session)
        self.settings = SearchConfig(10, ((.15, .65), (1.05, 1.6), (.5, .85)),
                                     initial_points=4, window_seconds=5)
        self.controller.create(Path(self.directory.name) / "test.fcbo.json", self.settings)
        self.controller.experiment.add_trial({"point": [.3, 1.2, .7], "method": "test"})
        self.targets = self.settings.targets([.3, 1.2, .7])
        self.clock = datetime(2026, 8, 27, 12)
        self.datetime_patch = patch("flow_controller.core.optimiser_controller.datetime")
        self.mock_time = self.datetime_patch.start()
        self.mock_time.now.side_effect = lambda: self.clock
        self.addCleanup(self.datetime_patch.stop)
        self.addCleanup(self.cleanup_session)
        self.publish(0)

    def cleanup_session(self):
        self.controller.shutdown()
        self.session.is_monitoring = False
        self.session.controllers_connected = False
        self.session.shutdown()

    def publish(self, seconds, overrides=None):
        self.clock = datetime(2026, 8, 27, 12) + timedelta(seconds=seconds)
        samples = {}
        for role, unit in self.session.assignments.items():
            if unit:
                value = self.targets.get(role, 0)
                samples[unit] = {"flow": value, "sp": value}
        samples.update(overrides or {})
        self.session._publish_pass(self.clock, samples, list(samples))

    def test_prepare_populates_ui_but_never_queues_commands(self):
        tab = OperationTab(self.session, optimiser=self.controller)
        self.addCleanup(tab.close)
        self.controller.prepare_targets()
        self.assertTrue(self.session.setpoint_queue.empty())
        for unit, card in tab._cards.items():
            self.assertAlmostEqual(float(card.entry.text()), self.targets.get(tab._card_keys[unit], 0), places=5)
        self.assertEqual(float(tab._cards["P"].entry.text()), 0)

    def test_new_experiment_initial_points_follow_active_dimensions(self):
        dialog = ExperimentDialog()
        self.addCleanup(dialog.close)
        initial = dialog.entries["initial"]
        power = dialog.optional["power_kw"][0]
        split = dialog.optional["split_rich"][0]
        self.assertEqual(initial.text(), "4")
        for check, enabled, expected in (
                (power, True, "5"), (split, True, "6"),
                (power, False, "5"), (split, False, "4")):
            check.setChecked(enabled)
            self.assertEqual(initial.text(), expected)
        initial.setText("16")
        for check, enabled in ((split, True), (power, True), (split, False), (power, False)):
            check.setChecked(enabled)
            self.assertEqual(initial.text(), "16")

    def test_config_seeded_dialog_preserves_initial_points_and_optional_dimensions(self):
        for count in (6, 16):
            with self.subTest(initial_points=count):
                settings = SearchConfig(
                    10, ((.15, .65), (1.05, 1.6), (.5, .85), (8, 12), (.75, 1.0)),
                    initial_points=count, optimise_power=True, optimise_split=True)
                dialog = ExperimentDialog(settings)
                self.addCleanup(dialog.close)
                self.assertEqual(dialog.entries["initial"].text(), str(count))
                for key in ("power_kw", "split_rich"):
                    self.assertTrue(dialog.optional[key][0].isChecked())

    def test_new_experiment_dialog_adds_power_and_split_dimensions(self):
        dialog = ExperimentDialog(self.session.autocalc_request)
        self.addCleanup(dialog.close)
        for pair, values in zip(dialog.bounds, ((15, 65), (1.05, 1.6), (.5, .85))):
            for entry, value in zip(pair, values):
                entry.setText(str(value))
        power_check, power_pair, _ = dialog.optional["power_kw"]
        split_check, split_pair, _ = dialog.optional["split_rich"]
        power_check.setChecked(True)
        split_check.setChecked(True)
        for entry, value in zip(power_pair, (8, 12)):
            entry.setText(str(value))
        for entry, value in zip(split_pair, (75, 100)):
            entry.setText(str(value))
        self.assertEqual(dialog.entries["initial"].text(), "6")
        dialog.entries["pool"].setText("2048")
        dialog.entries["split"].setText("90")
        dialog.approved.setChecked(True)
        dialog.accept()
        self.assertEqual(dialog.config.dimensions, 5)
        self.assertEqual(dialog.config.initial_points, 6)
        self.assertEqual(dialog.config.variable_names[-2:], ("power_kw", "split_rich"))
        self.assertEqual(dialog.config.bounds[-2:], ((8.0, 12.0), (.75, 1.0)))
        self.assertEqual(dialog.config.candidate_pool_size, 2048)

    def test_power_dimension_round_trips_through_controller_window(self):
        settings = SearchConfig(
            10, ((.15, .65), (1.05, 1.6), (.5, .85), (8, 12)),
            initial_points=5, window_seconds=5, optimise_power=True,
            candidate_pool_size=128)
        self.controller.create(Path(self.directory.name) / "power.fcbo.json", settings)
        point = [.3, 1.2, .7, 11]
        self.controller.experiment.add_trial({"point": point, "method": "test"})
        self.targets = settings.targets(point)
        self.publish(0)
        self.controller.prepare_targets()
        self.controller.start_window(True, True)
        self.publish(2)
        self.publish(5)
        window = self.controller.finish_window()
        self.assertEqual(len(settings.observed_vector(window)), 4)
        self.assertAlmostEqual(window["power_kw"], 11)
        self.controller.complete(100, 10, basis_confirmed=True)
        self.assertIsNone(self.controller.experiment.pending)

    def test_pilot_can_be_physically_unassigned_for_measurement(self):
        del self.session.selection["P"]
        self.session._rebuild_assignments()
        self.publish(0)
        self.controller.prepare_targets()
        self.controller.start_window(pilot_off=True, settled=True)
        self.assertIsNotNone(self.controller.capture)

    def test_nonzero_pilot_and_unconfirmed_window_rejected(self):
        with self.assertRaises(ValueError):
            self.controller.start_window()
        for gas in ("NH3", "H2", "CH4"):
            with self.subTest(gas=gas):
                self.session.selection["P"] = (gas, "Pilot")
                self.session._rebuild_assignments()
                self.publish(0, {"P": {"flow": 1.0, "sp": 1.0}})
                with self.assertRaisesRegex(ValueError, "pilot"):
                    self.controller.start_window(True, True)
                self.assertIsNone(self.controller.capture)
        self.publish(0, {"P": {"flow": .01, "sp": .01}})
        with self.assertRaisesRegex(ValueError, "pilot"):
            self.controller.start_window(True, True)

    def test_complete_flow_window_and_manual_result(self):
        self.controller.start_window(True, True)
        self.publish(2)
        self.publish(5)
        window = self.controller.finish_window()
        self.assertEqual(window["duration_s"], 5)
        self.assertIsNotNone(self.controller.experiment.pending["window"])
        self.controller.complete(100, 10, basis_confirmed=True)
        self.assertIsNone(self.controller.experiment.pending)
        self.assertTrue(self.session.setpoint_queue.empty())

    def mexa(self, seq, **changes):
        source = getattr(self, "mexa_source", None) or str(uuid.uuid4())
        self.mexa_source = source
        packet = make_packet(simulated_cycle(seq), source, seq, simulated=False,
                             validated=True, dry=True, cycle_s=.7)
        packet["acquired_at"] = self.clock.astimezone(timezone.utc).isoformat()
        packet.update(changes)
        sample = ReceivedSample(packet, packet["acquired_at"], time.monotonic(),
                                str(Path(self.directory.name) / "received.jsonl"))
        self.session.mexa.latest = sample
        self.session.mexa.sample_received.emit(sample)
        return sample

    def start_live(self):
        clock = patch("mexa_bridge.records.time.time", side_effect=lambda: self.clock.timestamp())
        clock.start()
        self.addCleanup(clock.stop)
        self.mexa(1)
        self.controller.start_window(True, True, live=True)

    def test_reference_o2_copies_once_and_remains_manually_editable(self):
        dialog = ExperimentDialog(mexa=self.session.mexa)
        self.addCleanup(dialog.close)
        entry = dialog.entries["reference"]
        with patch("mexa_bridge.records.time.time", side_effect=lambda: self.clock.timestamp()):
            sample = self.mexa(1, o2_percent=8.123456789)
            dialog.record_reference_button.click()
            self.assertEqual(float(entry.text()), sample.packet["o2_percent"])
            self.assertIn(sample.packet["acquired_at"], dialog.reference_status.text())
            self.assertIn("fixed", dialog.reference_status.text())
            self.assertEqual(dialog.error.text(), "")

            self.mexa(2, o2_percent=9.87654321)
            self.app.processEvents()
            self.assertEqual(float(entry.text()), 8.123456789)
            dialog.record_reference_button.click()
            self.assertEqual(float(entry.text()), 9.87654321)

            self.assertFalse(entry.isReadOnly())
            entry.selectAll()
            QTest.keyClicks(entry, "12.5")
            self.assertEqual(entry.text(), "12.5")
            self.assertIn("Manually entered", dialog.reference_status.text())
            self.mexa(3, o2_percent=10.25)
            self.app.processEvents()
            self.assertEqual(entry.text(), "12.5")
        self.assertTrue(self.session.setpoint_queue.empty())

    def test_reference_o2_rejects_unusable_samples_without_changing_input(self):
        dialog = ExperimentDialog(mexa=self.session.mexa)
        self.addCleanup(dialog.close)
        cases = (
            ("missing", {}, "Fresh MEXA data"),
            ("stale", {"acquired_at": (self.clock - timedelta(seconds=6)).astimezone(
                timezone.utc).isoformat()}, "Stale"),
            ("simulated", {"simulated": True}, "Simulated"),
            ("unvalidated", {"validated": False}, "Validate the serial readings"),
            ("wrong_basis", {"basis": "unknown"}, "Uncorrected dry"),
            ("unlogged", {}, "Save received MEXA logs"),
            ("invalid", {"valid": False, "alarms": ["filter"]}, "filter"),
            ("at_air", {"o2_percent": 20.9}, "below 20.9"),
            ("above_air", {"o2_percent": 21.0}, "below 20.9"),
        )
        with patch("mexa_bridge.records.time.time", side_effect=lambda: self.clock.timestamp()):
            for name, changes, error in cases:
                with self.subTest(name=name):
                    sample = self.mexa(1, **changes)
                    if name == "missing":
                        self.session.mexa.latest = None
                    elif name == "unlogged":
                        self.session.mexa.latest = replace(sample, log_path="")
                    dialog.entries["reference"].setText("14.2500")
                    dialog.error.setText("")
                    dialog.record_reference_button.click()
                    self.assertEqual(dialog.entries["reference"].text(), "14.2500")
                    self.assertIn(error, dialog.error.text())
                    self.assertTrue(self.session.setpoint_queue.empty())

    def test_reference_o2_button_disabled_without_mexa(self):
        dialog = ExperimentDialog()
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.record_reference_button.isEnabled())
        self.assertFalse(dialog.entries["reference"].isReadOnly())

    def test_new_experiment_wires_mexa_and_persists_copied_reference(self):
        self.controller.invalidate("Replace fixture campaign")
        pane = OptimiserPane(self.controller)
        self.addCleanup(pane.close)
        path = Path(self.directory.name) / "copied-reference.fcbo.json"

        def accept_dialog(dialog):
            self.assertIs(dialog._mexa, self.session.mexa)
            dialog.record_reference_button.click()
            self.assertEqual(float(dialog.entries["reference"].text()), 7.654321)
            for pair, values in zip(dialog.bounds, ((15, 65), (1.05, 1.6), (.5, .85))):
                for entry, value in zip(pair, values):
                    entry.setText(str(value))
            dialog.entries["power"].setText("10")
            dialog.entries["split"].setText("100")
            dialog.approved.setChecked(True)
            dialog.accept()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(dialog.config.reference_o2, 7.654321)
            return dialog.result()

        with patch("mexa_bridge.records.time.time", side_effect=lambda: self.clock.timestamp()), \
                patch.object(ExperimentDialog, "exec", accept_dialog), \
                patch("flow_controller.ui.qt_optimiser.QFileDialog.getSaveFileName",
                      return_value=(str(path), "")):
            self.mexa(1, o2_percent=7.654321)
            pane._new()
        self.assertEqual(self.controller.experiment.config.reference_o2, 7.654321)
        self.assertEqual(Experiment.load(path).config.reference_o2, 7.654321)
        self.assertTrue(self.session.setpoint_queue.empty())

    def finish_live(self):
        self.start_live()
        for seq, seconds in ((2, 1), (3, 3), (4, 6)):
            self.publish(seconds)
            self.mexa(seq)
        return self.controller.finish_window()

    def test_live_capture_saves_means_and_round_trips_without_actuation(self):
        window = self.finish_live()
        self.assertEqual(window["mexa"]["samples"], 3)
        self.assertEqual(window["mexa"]["no_ppm"], 103)
        self.assertTrue(self.session.setpoint_queue.empty())
        self.controller.complete_from_mexa("live test", basis_confirmed=True)
        reopened = Experiment.load(self.controller.experiment.path)
        self.assertEqual(reopened.trials[0]["result"]["source"], "mexa_stream")
        self.assertEqual(reopened.trials[0]["result"]["no_ppm"], 103)
        self.assertIsNone(reopened.trials[0]["result"]["no_sem"])
        self.assertTrue(self.session.setpoint_queue.empty())

    def test_live_window_cannot_be_overridden_by_manual_input(self):
        self.finish_live()
        with self.assertRaisesRegex(ValueError, "saved MEXA means"):
            self.controller.complete(1, 5, basis_confirmed=True)
        with self.assertRaises(ValueError):
            self.controller.complete_from_mexa(basis_confirmed=False)
        self.assertIsNotNone(self.controller.experiment.pending)

    def test_live_link_loss_discards_window_without_touching_flows(self):
        self.start_live()
        self.session.mexa.interrupted.emit("Disconnected")
        self.assertIsNone(self.controller.capture)
        self.assertIsNone(self.controller.mexa_capture)
        self.assertIsNone(self.controller.experiment.pending["window"])
        self.assertTrue(self.session.setpoint_queue.empty())

    def test_live_invalid_reading_discards_window_immediately(self):
        self.start_live()
        self.publish(1)
        self.mexa(2, valid=False, alarms=["filter"])
        self.assertIsNone(self.controller.capture)
        self.assertIn("filter", self.controller.last_message)

    def test_live_stalled_stream_discards_even_without_flow_event(self):
        self.start_live()
        self.clock += timedelta(seconds=6)
        self.controller._check_live_freshness()
        self.assertIsNone(self.controller.capture)
        self.assertIn("Stale", self.controller.last_message)

    def test_live_sequence_gap_discards_window(self):
        self.start_live()
        self.publish(1)
        self.mexa(3)
        self.assertIsNone(self.controller.capture)

    def test_live_missing_stream_never_falls_back_to_manual(self):
        with self.assertRaises(ValueError):
            self.controller.start_window(True, True, live=True)
        self.assertIsNone(self.controller.capture)

    def test_unlogged_preview_stream_cannot_start_an_optimiser_window(self):
        from dataclasses import replace
        with patch("mexa_bridge.records.time.time", side_effect=lambda: self.clock.timestamp()):
            sample = self.mexa(1)
            self.session.mexa.latest = replace(sample, log_path="")
            with self.assertRaisesRegex(ValueError, "Save received MEXA logs"):
                self.controller.start_window(True, True, live=True)
        self.assertIsNone(self.controller.capture)
        self.assertTrue(self.session.setpoint_queue.empty())

    def test_simulated_stream_cannot_start_experimental_window(self):
        with patch("mexa_bridge.records.time.time", side_effect=lambda: self.clock.timestamp()):
            self.mexa(1, simulated=True)
            with self.assertRaisesRegex(ValueError, "Simulated"):
                self.controller.start_window(True, True, live=True)
        self.assertIsNone(self.controller.capture)

    def test_live_save_stores_utc_and_rejects_tampered_means_on_reload(self):
        import json
        self.finish_live()
        self.controller.complete_from_mexa(basis_confirmed=True)
        experiment = self.controller.experiment
        self.assertTrue(experiment.trials[0]["window"]["start"].endswith("+00:00"))
        experiment.data["trials"][0]["window"]["mexa"]["no_ppm"] += 1
        experiment.path.write_text(json.dumps(experiment.data))
        with self.assertRaisesRegex(ValueError, "captured means"):
            Experiment.load(experiment.path)

    def test_manual_capture_not_affected_by_unrelated_mexa_disconnect(self):
        self.controller.start_window(True, True)
        self.session.mexa.interrupted.emit("Disconnected")
        self.assertIsNotNone(self.controller.capture)

    def test_live_ui_uses_saved_values_and_locks_inputs(self):
        window = self.finish_live()
        tab = OperationTab(self.session, optimiser=self.controller)
        self.addCleanup(tab.close)
        pane = tab.optimiser_pane
        self.assertTrue(pane.inputs["no"].isReadOnly())
        self.assertAlmostEqual(float(pane.inputs["no"].text()), window["mexa"]["no_ppm"])
        self.assertFalse(pane.live.isEnabled())
        pane.basis.setChecked(True)
        pane._save()
        self.assertIsNone(self.controller.experiment.pending)

    def test_resume_keeps_live_provenance_and_no_connection_required_to_save(self):
        self.finish_live()
        self.controller.load(self.controller.experiment.path)
        self.session.mexa.disconnect_bridge()
        self.controller.complete_from_mexa(basis_confirmed=True)
        self.assertEqual(self.controller.experiment.trials[0]["result"]["source"], "mexa_stream")

    def test_raw_readings_cannot_be_saved_without_window_and_basis_confirmation(self):
        with self.assertRaises(ValueError):
            self.controller.complete(0, 10, basis_confirmed=True)
        self.controller.start_window(True, True)
        self.publish(2)
        self.publish(5)
        self.controller.finish_window()
        with self.assertRaises(ValueError):
            self.controller.complete(100, 10, basis_confirmed=False)
        self.assertEqual(self.controller.experiment.pending["status"], "pending")

    def test_flow_loss_at_finish_cannot_commit_a_window(self):
        self.controller.start_window(True, True)
        self.publish(2)
        self.publish(5)
        self.session._latest_samples["A"] = {"flow": None, "sp": None}
        with self.assertRaises(ValueError):
            self.controller.finish_window()
        self.assertIsNone(self.controller.experiment.pending["window"])

    def test_missing_reading_discards_capture_not_success(self):
        self.controller.start_window(True, True)
        self.publish(2, {"A": {"flow": None, "sp": None}})
        self.assertIsNone(self.controller.capture)
        self.assertIsNone(self.controller.experiment.pending["window"])
        self.assertIn("discarded", self.controller.last_message)

    def test_timestamp_gap_and_configuration_change_discard_capture(self):
        self.controller.start_window(True, True)
        self.publish(20)
        self.assertIsNone(self.controller.capture)
        self.controller.start_window(True, True)
        self.session.assignments_changed.emit(dict(self.session.assignments))
        self.assertIsNone(self.controller.capture)

    def test_limits_rechecked_and_duplicate_assignments_rejected(self):
        self.session.unit_prefs["C"] = {"max_flow": 1}
        with self.assertRaisesRegex(ValueError, "MAX FLOW"):
            self.controller.prepare_targets()
        self.session.unit_prefs.clear()
        self.session.selection["E"] = ("NH3", "Zone 1")
        self.session._rebuild_assignments()
        with self.assertRaisesRegex(ValueError, "Exactly one"):
            self.controller.prepare_targets()

    def test_freshness_and_wrong_flow_block_measurements(self):
        self.clock += timedelta(seconds=30)
        with self.assertRaisesRegex(ValueError, "Fresh"):
            self.controller.start_window(True, True)
        self.publish(30, {"A": {"flow": 0, "sp": self.targets["nh3_rich"]}})
        with self.assertRaisesRegex(ValueError, "proposed"):
            self.controller.start_window(True, True)

    def test_worker_returns_one_persisted_trial_without_actuation(self):
        self.controller.invalidate("Skip fixture point")
        loop = QEventLoop()
        self.controller.ask()
        self.controller.worker.finished.connect(loop.quit)
        QTimer.singleShot(15000, loop.quit)
        loop.exec()
        self.app.processEvents()
        self.assertFalse(self.controller.busy, self.controller.last_message)
        self.assertIsNotNone(self.controller.experiment.pending, self.controller.last_message)
        self.assertTrue(self.session.setpoint_queue.empty())

    def test_retheme_preserves_campaign_and_manual_text(self):
        self.session.is_monitoring = False
        self.session.controllers_connected = False
        window = MainWindow(self.session)
        self.addCleanup(window.close)
        window.optimiser.load(self.controller.experiment.path)
        window.optimiser_pane.inputs["no"].setText("123")
        window.optimiser_pane.set_collapsed(False, animate=False)
        old_controller = window.optimiser
        window._rebuild()
        self.app.processEvents()
        self.assertIs(window.optimiser, old_controller)
        self.assertEqual(window.optimiser_pane.inputs["no"].text(), "123")
        self.assertFalse(window.optimiser_pane.is_collapsed())
        self.assertFalse(hasattr(window, "agent_manager"))


if __name__ == "__main__":
    unittest.main()
