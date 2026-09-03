"""Focused offscreen checks for mapping configuration and the operator workflow."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
from PySide6.QtCore import Qt, QEventLoop, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog

from tests import test_qt_optimiser as qt_base
from flow_controller.ui.qt_optimiser import ExperimentDialog, OptimiserPane


class QtPressureMappingTests(unittest.TestCase):
    setUpClass = classmethod(qt_base.QtOptimiserTests.setUpClass.__func__)
    setUp = qt_base.QtOptimiserTests.setUp
    cleanup_session = qt_base.QtOptimiserTests.cleanup_session
    publish = qt_base.QtOptimiserTests.publish

    def mapping_campaign(self):
        config = replace(self.settings, objective_mode="map_no_pressure")
        self.controller.create(Path(self.directory.name) / "mapping.fcbo.json", config)
        self.controller.experiment.add_trial({"point": [.3, 1.2, .7], "method": "test"})
        return config

    def window(self):
        self.controller.start_window(True, True)
        self.publish(2)
        self.publish(5)
        return self.controller.finish_window()

    def pane(self):
        pane = OptimiserPane(self.controller)
        self.addCleanup(pane.close)
        return pane

    def test_mapping_config_roundtrip_and_legacy_default(self):
        config = replace(self.settings, objective_mode="map_no_pressure",
                         pressure_metric="dominant_amplitude_pa", mapping_no_weight=.7,
                         reference_o2=12, window_seconds=45)
        dialog = ExperimentDialog(config)
        self.addCleanup(dialog.close)
        dialog.approved.setChecked(True)
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.config, config)
        legacy = ExperimentDialog(self.settings)
        self.addCleanup(legacy.close)
        self.assertEqual(legacy.objective_mode.currentData(), "minimise_no")
        self.assertFalse(legacy.pressure_metric.isEnabled())
        self.assertFalse(legacy.entries["mapping_weight"].isEnabled())

    def test_mapping_weight_rejects_endpoints(self):
        for weight in ("0", "1"):
            dialog = ExperimentDialog(replace(self.settings, objective_mode="map_no_pressure"))
            self.addCleanup(dialog.close)
            dialog.entries["mapping_weight"].setText(weight)
            dialog.approved.setChecked(True)
            dialog.accept()
            self.assertIsNone(dialog.config)
            self.assertIn("weight", dialog.error.text().lower())

    def test_pressure_gate_summary_and_busy_state(self):
        self.mapping_campaign()
        pane = self.pane()
        self.assertFalse(pane.pressure_import_button.isEnabled())
        self.window()
        pane.refresh()
        self.assertFalse(pane.save_button.isEnabled())
        self.assertTrue(pane.pressure_import_button.isEnabled())
        self.assertFalse(pane.arm_button.isEnabled())
        self.controller.experiment.pending["pressure"] = {
            "rms_pa": 12.5, "peak_abs_pa": 35, "dominant_frequency_hz": 210,
            "dominant_amplitude_pa": 10}
        pane.refresh()
        self.assertTrue(pane.save_button.isEnabled())
        self.assertFalse(pane.pressure_import_button.isEnabled())
        self.assertIn("12.5 Pa", pane.pressure_label.text())
        self.assertIn("210 Hz", pane.pressure_label.text())
        with patch.object(type(self.controller), "busy", new_callable=unittest.mock.PropertyMock,
                          return_value=True):
            pane.refresh()
            self.assertFalse(pane.save_button.isEnabled())
            self.assertFalse(pane.pressure_import_button.isEnabled())

    def test_legacy_save_without_pressure_and_export_unarmed(self):
        pane = self.pane()
        self.assertTrue(pane.labview_export_button.isEnabled())
        self.window()
        pane.refresh()
        self.assertTrue(pane.save_button.isEnabled())

    def test_trigger_and_import_actions_forward_current_choices(self):
        pane = self.pane()
        pane.pilot.setChecked(True)
        pane.settled.setChecked(True)
        pane.live.setChecked(False)
        with patch.object(self.controller, "arm_labview", create=True) as arm:
            pane._arm_labview()
            arm.assert_called_once_with(pilot_off=True, settled=True, live=False)
        with patch("flow_controller.ui.qt_optimiser.QFileDialog.getOpenFileName",
                   return_value=("pressure.json", "")), \
                patch.object(self.controller, "import_pressure", create=True) as importer:
            pane._import_pressure()
            importer.assert_called_once_with("pressure.json")
        with patch("flow_controller.ui.qt_optimiser.QFileDialog.getSaveFileName",
                   return_value=("request.json", "")), \
                patch.object(self.controller, "export_labview_request", create=True) as exporter:
            pane._export_labview()
            exporter.assert_called_once_with("request.json")

    def test_trial_ids_are_selectable_and_armed_state_disables_manual_start(self):
        request = {"protocol": "flow-pressure-v1", "type": "start", "experiment_id": "exp-1",
                   "trial_id": "trial-2", "capture_id": "capture-3", "request_id": "request-4"}
        with patch.object(self.controller, "labview_request", return_value=request, create=True), \
                patch.object(self.controller, "labview_armed", True, create=True):
            pane = self.pane()
            self.assertIn("capture-3", pane.labview_ids.text())
            self.assertTrue(pane.labview_ids.textInteractionFlags()
                            & Qt.TextInteractionFlag.TextSelectableByMouse)
            self.assertFalse(pane.start_button.isEnabled())
            self.assertFalse(pane.arm_button.isEnabled())
            self.assertTrue(pane.disarm_button.isEnabled())

    def test_observed_scatter_and_background_slice_render(self):
        config = self.mapping_campaign()
        window = self.window()
        base = deepcopy(self.controller.experiment.pending)
        trials = []
        for number in range(1, 5):
            trial = deepcopy(base)
            trial.update(id=f"trial-{number}", number=number, status="completed",
                         result={"corrected_no": 100 + number},
                         pressure={"rms_pa": 10 + number}, window=deepcopy(window))
            trials.append(trial)
        self.controller.experiment.data["trials"] = trials
        pane = self.pane()
        self.assertEqual(len(pane.outcome_plot.listDataItems()[0].xData), 4)
        self.assertIn("RMS", pane.history.item(0).text())
        pane.map_y.setCurrentIndex(pane.map_x.currentIndex())
        self.assertNotEqual(pane.map_x.currentData(), pane.map_y.currentData())
        self.assertIn("bounds midpoints", pane.map_slice_label.text())
        pane.history.setCurrentRow(0)
        self.assertIn("measured test 1", pane.map_slice_label.text())
        caller = threading.get_ident()
        fit_threads = []
        captured = []

        def predict(settings, observations, points):
            fit_threads.append(threading.get_ident())
            captured.append(points.copy())
            return {"no_mean": np.arange(len(points)), "no_sd": np.full(len(points), 2),
                    "pressure_mean": np.arange(len(points)) / 2,
                    "pressure_sd": np.full(len(points), 3)}

        with patch("flow_controller.domain.bayesian.predict_mapping", side_effect=predict):
            pane._refresh_maps()
            deadline = time.monotonic() + 5
            while pane.map_worker is not None and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertIsNone(pane.map_worker)
        self.assertNotEqual(fit_threads, [caller])
        self.assertEqual(captured[0].shape, (400, config.dimensions))
        self.assertEqual(pane.map_images[0].image.shape, (20, 20))
        pane.map_uncertainty.setChecked(True)
        self.assertTrue(np.all(pane.map_images[0].image == 2))
        self.assertTrue(np.all(pane.map_images[1].image == 3))
        self.assertFalse(pane.map_plots[0].listDataItems())  # No projected observations.
        pane.show()
        self.app.processEvents()
        self.assertFalse(pane.grab().isNull())
        pane.map_x.setCurrentIndex((pane.map_x.currentIndex() + 1) % config.dimensions)
        self.assertIsNone(pane.map_images[0].image)

    def test_real_gp_predictions_reach_both_map_images(self):
        from tests.test_pressure_mapping import config, response_data
        settings = config()
        self.controller.create(Path(self.directory.name) / "real-map.fcbo.json", settings)
        trials = response_data()
        for number, trial in enumerate(trials, 1):
            trial.update(id=f"synthetic-{number}", number=number, method="Synthetic fixture")
        self.controller.experiment.data["trials"] = trials
        pane = self.pane()
        self.assertTrue(pane.map_refresh_button.isEnabled())
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        pane._refresh_maps()
        worker = pane.map_worker
        worker.finished.connect(loop.quit)
        timer.start(15000)
        loop.exec()
        timer.stop()
        self.assertIsNone(pane.map_worker, "Real mapping prediction exceeded 15 seconds")
        self.assertIsNotNone(pane._map_result, pane.map_status.text())
        for image in pane.map_images:
            self.assertEqual(image.image.shape, (20, 20))
            self.assertTrue(np.isfinite(image.image).all())
            self.assertGreater(float(np.ptp(image.image)), 0)
        pane.map_uncertainty.setChecked(True)
        for image in pane.map_images:
            self.assertTrue(np.isfinite(image.image).all())
            self.assertTrue((image.image >= 0).all())


if __name__ == "__main__":
    unittest.main()
