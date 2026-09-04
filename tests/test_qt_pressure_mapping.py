"""Focused offscreen checks for mapping configuration and the operator workflow."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
from PySide6.QtCore import Qt, QCoreApplication, QEvent, QEventLoop, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog
from shiboken6 import isValid

from tests import test_qt_optimiser as qt_base
from flow_controller.ui.qt_optimiser import ExperimentDialog, OptimiserPane, TdmsSourceDialog
from flow_controller.ui import qt_optimiser


class ApplicationWorkerTests(unittest.TestCase):
    setUpClass = classmethod(qt_base.QtOptimiserTests.setUpClass.__func__)

    def workers(self):
        yield qt_optimiser.TdmsInspectionWorker("sample.tdms"), qt_optimiser._TDMS_INSPECTORS
        yield qt_optimiser.MappingWorker(None, [], np.zeros((1, 1)), {}), qt_optimiser._MAP_WORKERS

    def finish(self, worker, registry):
        self.assertTrue(worker.wait(5000), "Worker did not finish")
        self.app.processEvents()
        self.assertNotIn(worker, registry)

    def test_worker_errors_reach_ui_and_release_ownership(self):
        for worker, registry in self.workers():
            with self.subTest(worker=type(worker).__name__):
                errors, results = [], []
                worker.failed.connect(errors.append)
                worker.succeeded.connect(results.append)
                with patch.object(type(worker), "_compute", side_effect=ValueError("Unreadable data")):
                    worker.start()
                    self.finish(worker, registry)
                self.assertEqual(errors, ["Unreadable data"])
                self.assertEqual(results, [])

    def test_interruption_suppresses_both_success_and_error(self):
        for raises in (False, True):
            for worker, registry in self.workers():
                with self.subTest(worker=type(worker).__name__, raises=raises):
                    entered, release = threading.Event(), threading.Event()
                    errors, results = [], []
                    worker.failed.connect(errors.append)
                    worker.succeeded.connect(results.append)

                    def compute():
                        entered.set()
                        release.wait(5)
                        if raises:
                            raise ValueError("Cancelled operation failed")
                        return "Cancelled result"

                    with patch.object(type(worker), "_compute", side_effect=compute):
                        worker.start()
                        try:
                            self.assertTrue(entered.wait(5))
                            worker.requestInterruption()
                        finally:
                            release.set()
                            self.finish(worker, registry)
                    self.assertEqual(errors, [])
                    self.assertEqual(results, [])

    def test_inspection_outlives_destroyed_dialog(self):
        entered, release = threading.Event(), threading.Event()

        def inspect(_path):
            entered.set()
            release.wait(5)
            return []

        dialog = TdmsSourceDialog(dual=False)
        with patch("flow_controller.ui.qt_optimiser.inspect_tdms", side_effect=inspect):
            dialog.inspect_sample("sample.tdms")
            worker = dialog.worker
            try:
                self.assertTrue(entered.wait(5))
                dialog.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertFalse(isValid(dialog))
                self.assertTrue(isValid(worker))
                self.assertIs(worker.parent(), self.app)
                self.assertIn(worker, qt_optimiser._TDMS_INSPECTORS)
            finally:
                release.set()
                self.finish(worker, qt_optimiser._TDMS_INSPECTORS)


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
        old_mapping = ExperimentDialog(replace(
            self.settings, objective_mode="map_no_pressure", pressure_metric="peak_abs_pa"))
        self.addCleanup(old_mapping.close)
        self.assertTrue(old_mapping.pressure_metric.isEnabled())
        self.assertEqual(old_mapping.pressure_metric.currentData(), "peak_abs_pa")
        new_mapping = ExperimentDialog(replace(
            self.settings, objective_mode="map_no_pressure", pressure_metric="peak_abs_pa"),
            dual_pressure=True)
        self.addCleanup(new_mapping.close)
        self.assertFalse(new_mapping.pressure_metric.isEnabled())
        self.assertEqual(new_mapping.pressure_metric.currentData(), "dominant_amplitude_pa")

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
        self.assertFalse(pane.start_button.isEnabled())
        self.assertIn("Arm LabVIEW trigger", pane.start_button.toolTip())
        self.window()
        pane.refresh()
        self.assertFalse(pane.save_button.isEnabled())
        self.assertFalse(pane.pressure_import_button.isEnabled())
        self.controller.experiment.pending["window"]["labview_capture"] = {"start": "fixture"}
        pane.refresh()
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

    def test_legacy_save_without_pressure_and_json_controls_hidden(self):
        pane = self.pane()
        self.assertTrue(pane.start_button.isEnabled())
        self.assertTrue(pane.labview_export_button.isHidden())
        self.assertTrue(pane.labview_ids.isHidden())
        self.assertEqual(pane.pressure_import_button.text(), "Choose TDMS file…")
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
                   return_value=("pressure.tdms", "")), \
                patch.object(self.controller, "import_tdms", create=True) as importer:
            pane._import_tdms()
            importer.assert_called_once_with("pressure.tdms")

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
        for image in pane.map_images[:2]:
            self.assertEqual(image.image.shape, (20, 20))
            self.assertTrue(np.isfinite(image.image).all())
            self.assertGreater(float(np.ptp(image.image)), 0)
        pane.map_uncertainty.setChecked(True)
        for image in pane.map_images[:2]:
            self.assertTrue(np.isfinite(image.image).all())
            self.assertTrue((image.image >= 0).all())

    def test_tdms_source_inspection_excludes_spectra_and_requires_calibration(self):
        dialog = TdmsSourceDialog(dual=False)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.entries["folder"].text(), "")
        self.assertEqual(dialog.entries["scale_pa_per_unit"].text(), "")
        self.assertEqual(dialog.entries["calibration_id"].text(), "")
        self.assertFalse(dialog.use_trigger_time.isChecked())
        main_thread = threading.get_ident()
        inspection_threads = []

        def inspect(path):
            inspection_threads.append(threading.get_ident())
            return [
                {"group": "FFT spectrum", "channel": "pressure", "samples": 4096,
                 "sample_rate_hz": None, "start": None, "unit": "Volts", "is_spectrum": True},
                {"group": "raw", "channel": "PD_CC_3_1", "samples": 21082,
                 "sample_rate_hz": 10000, "start": "2026-08-20T12:00:00+00:00",
                 "unit": "Volts", "is_spectrum": False},
                {"group": "converted", "channel": "PD_CC_3_1", "samples": 21082,
                 "sample_rate_hz": 10000, "start": "2026-08-20T12:00:00+00:00",
                 "unit": "Volts", "is_spectrum": False}]

        with patch("flow_controller.ui.qt_optimiser.inspect_tdms", side_effect=inspect):
            loop = QEventLoop()
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(loop.quit)
            dialog.inspect_sample(Path(self.directory.name) / "sample.tdms")
            dialog.worker.finished.connect(loop.quit)
            timer.start(5000)
            loop.exec()
            timer.stop()
        self.assertIsNone(dialog.worker)
        self.assertNotEqual(inspection_threads, [main_thread])
        self.assertEqual(dialog.channel_picker.count(), 2)
        self.assertEqual(dialog.entries["group"].text(), "converted")
        self.assertEqual(dialog.entries["channel"].text(), "PD_CC_3_1")
        self.assertIn("Volts", dialog.metadata.text())
        self.assertIn("21,082 samples", dialog.metadata.text())
        self.assertIn("10000 Hz", dialog.metadata.text())
        self.assertEqual(Path(dialog.entries["folder"].text()), Path(self.directory.name))
        self.assertEqual(dialog.entries["scale_pa_per_unit"].text(), "")
        dialog.accept()
        self.assertIsNone(dialog.source)
        dialog.entries["scale_pa_per_unit"].setText("1")
        dialog.entries["calibration_id"].setText("Explicit conversion calibration")
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted, dialog.error.text())
        self.assertEqual(dialog.source["scale_pa_per_unit"], 1)
        self.assertEqual(dialog.source["min_recording_s"], 1)
        self.assertIsNone(dialog.source["sample_rate_hz"])
        self.assertIsNone(dialog.source["band_high_hz"])

    def test_tdms_source_folder_picker_and_roundtrip(self):
        from flow_controller.domain.tdms_capture import validate_tdms_source
        profile = validate_tdms_source({
            "folder": self.directory.name, "group": "raw", "channel": "pressure",
            "scale_pa_per_unit": 250, "offset_pa": -1, "calibration_id": "sensor-2026",
            "sample_rate_hz": 10000, "min_recording_s": 1.25,
            "band_high_hz": 2000, "use_trigger_time": True})
        dialog = TdmsSourceDialog(profile)
        self.addCleanup(dialog.close)
        with patch("flow_controller.ui.qt_optimiser.QFileDialog.getExistingDirectory",
                   return_value=self.directory.name):
            dialog._choose_folder()
        dialog.accept()
        self.assertEqual(dialog.source, profile, dialog.error.text())

    def test_pressure_unit_shortcuts_are_explicit_and_keep_calibration_required(self):
        dialog = TdmsSourceDialog(dual=False)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.pressure_units.currentData(), "custom")
        self.assertEqual(dialog.entries["scale_pa_per_unit"].text(), "")
        dialog.entries["folder"].setText(self.directory.name)
        dialog.entries["group"].setText("converted")
        dialog.entries["channel"].setText("PD_CC_3_1")
        for units, scale in (("pa", "1"), ("kpa", "1000")):
            dialog.pressure_units.setCurrentIndex(dialog.pressure_units.findData(units))
            self.assertEqual(dialog.entries["scale_pa_per_unit"].text(), scale)
            self.assertTrue(dialog.entries["scale_pa_per_unit"].isReadOnly())
        dialog.accept()
        self.assertIsNone(dialog.source)
        self.assertIn("calibration_id", dialog.error.text())
        dialog.entries["calibration_id"].setText("Operator-confirmed stored kPa")
        dialog.accept()
        self.assertEqual(dialog.source["scale_pa_per_unit"], 1000)
        dialog.pressure_units.setCurrentIndex(dialog.pressure_units.findData("custom"))
        self.assertFalse(dialog.entries["scale_pa_per_unit"].isReadOnly())

    def test_dual_tdms_source_accepts_independent_calibrations_and_shared_analysis(self):
        dialog = TdmsSourceDialog()
        self.addCleanup(dialog.close)
        dialog.entries["folder"].setText(self.directory.name)
        for key, value in {
                "sample_rate_hz": "10000", "min_recording_s": "2",
                "band_low_hz": "40", "band_high_hz": "2000",
                "segment_samples": "1024", "overlap_samples": "512"}.items():
            dialog.entries[key].setText(value)
        configured = (
            ("pressure_1", "Combustor", "raw", "PD_1", "250", "-2", "cal-a", "-10", "10"),
            ("pressure_2", "Plenum", "converted", "PD_2", "1000", "3", "cal-b", "-5", "5"),
        )
        for sensor_id, label, group, channel, scale, offset, calibration, low, high in configured:
            fields = dialog.sensor_entries[sensor_id]
            for key, value in (("label", label), ("group", group), ("channel", channel),
                               ("scale_pa_per_unit", scale), ("offset_pa", offset),
                               ("calibration_id", calibration), ("clip_min", low),
                               ("clip_max", high)):
                fields[key].setText(value)
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted, dialog.error.text())
        self.assertEqual([item["id"] for item in dialog.source["transducers"]],
                         ["pressure_1", "pressure_2"])
        self.assertEqual([item["label"] for item in dialog.source["transducers"]],
                         ["Combustor", "Plenum"])
        self.assertEqual(dialog.source["transducers"][0]["scale_pa_per_unit"], 250)
        self.assertEqual(dialog.source["transducers"][1]["offset_pa"], 3)
        self.assertEqual(dialog.source["band_high_hz"], 2000)

    def test_dual_tdms_source_rejects_ambiguous_labels_and_channels(self):
        dialog = TdmsSourceDialog()
        self.addCleanup(dialog.close)
        dialog.entries["folder"].setText(self.directory.name)
        for sensor_id in dialog.TRANSDUCER_IDS:
            fields = dialog.sensor_entries[sensor_id]
            fields["group"].setText("raw")
            fields["channel"].setText("pressure")
            fields["scale_pa_per_unit"].setText("1")
            fields["calibration_id"].setText(sensor_id)
        dialog.sensor_entries["pressure_1"]["label"].setText("Sensor")
        dialog.sensor_entries["pressure_2"]["label"].setText("sensor")
        dialog.accept()
        self.assertIn("labels must be distinct", dialog.error.text())
        dialog.sensor_entries["pressure_2"]["label"].setText("Other sensor")
        dialog.accept()
        self.assertIn("group/channel selections must be distinct", dialog.error.text())

    def test_dual_source_and_pending_summary_render_both_labels(self):
        self.mapping_campaign()
        pane = self.pane()
        source = {
            "folder": self.directory.name,
            "transducers": [
                {"id": "pressure_1", "label": "Combustor", "group": "raw", "channel": "PD_1"},
                {"id": "pressure_2", "label": "Plenum", "group": "converted", "channel": "PD_2"},
            ],
        }
        pressure = {"transducers": [
            {"id": "pressure_1", "label": "Combustor", "metrics": {
                "rms_pa": 10, "peak_abs_pa": 20, "dominant_frequency_hz": 125,
                "dominant_amplitude_pa": 8}},
            {"id": "pressure_2", "label": "Plenum", "metrics": {
                "rms_pa": 30, "peak_abs_pa": 50, "dominant_frequency_hz": 250,
                "dominant_amplitude_pa": 24}},
        ]}
        self.controller.experiment.pending["pressure"] = pressure
        with patch.object(type(self.controller), "tdms_source", new_callable=unittest.mock.PropertyMock,
                          create=True, return_value=source):
            pane.refresh()
        self.assertIn("Combustor: raw / PD_1", pane.tdms_source_label.text())
        self.assertIn("Plenum: converted / PD_2", pane.tdms_source_label.text())
        self.assertIn("Combustor: RMS 10 Pa", pane.pressure_label.text())
        self.assertIn("Plenum: RMS 30 Pa", pane.pressure_label.text())
        self.assertIn("250 Hz", pane.pressure_label.text())
        trial = self.controller.experiment.pending
        trial.update(status="completed", result={"corrected_no": 42}, pressure=pressure)
        pane.refresh()
        self.assertIn("Combustor 8 Pa RMS", pane.history.item(0).text())
        self.assertIn("Plenum 24 Pa RMS", pane.history.item(0).text())
        self.assertEqual(len(pane.outcome_plot.listDataItems()), 2)

    def test_dual_map_render_uses_three_stable_response_keys(self):
        self.mapping_campaign()
        pane = self.pane()
        context = {
            "x": np.linspace(.1, .2, 20), "y": np.linspace(1, 1.2, 20),
            "x_name": "h2_fraction", "y_name": "phi_stage1",
            "metric": "dominant_amplitude_pa",
            "transducers": [
                {"id": "pressure_1", "label": "Combustor"},
                {"id": "pressure_2", "label": "Plenum"},
            ],
        }
        result = {}
        for key, value in (("no", 1), ("pressure_1", 2), ("pressure_2", 3)):
            result[f"{key}_mean"] = np.full(400, value)
            result[f"{key}_sd"] = np.full(400, value + 10)
        pane._map_result = (result, context)
        pane._draw_maps()
        self.assertEqual([float(image.image[0, 0]) for image in pane.map_images], [1, 2, 3])
        self.assertIn("Combustor", pane.map_plots[1].getPlotItem().titleLabel.text)
        self.assertIn("Plenum", pane.map_plots[2].getPlotItem().titleLabel.text)
        pane.map_uncertainty.setChecked(True)
        self.assertEqual([float(image.image[0, 0]) for image in pane.map_images], [11, 12, 13])

    def test_source_settings_and_legacy_tail_controls(self):
        pane = self.pane()
        source = {"folder": self.directory.name, "group": "converted", "channel": "pressure"}
        with patch.object(type(self.controller), "tdms_source", new_callable=unittest.mock.PropertyMock,
                          create=True, return_value=source):
            pane.refresh()
            self.assertIn("converted / pressure", pane.tdms_source_label.text())
            self.assertTrue(pane.tdms_source_button.isEnabled())
            self.controller.start_window(True, True)
            with patch.object(type(self.controller), "legacy_capture_active",
                              new_callable=unittest.mock.PropertyMock, create=True, return_value=True), \
                    patch.object(type(self.controller), "legacy_collecting_after_stop",
                                 new_callable=unittest.mock.PropertyMock, create=True, return_value=True), \
                    patch.object(type(self.controller), "labview_tail_remaining_s",
                                 new_callable=unittest.mock.PropertyMock, create=True, return_value=12.5):
                pane.refresh()
                self.assertFalse(pane.finish_button.isEnabled())
                self.assertTrue(pane.cancel_button.isEnabled())
                self.assertFalse(pane.tdms_source_button.isEnabled())
                self.assertIn("12.5 s", pane.labview_status.text())
                self.assertIn("At least 12.5 s remaining", pane.labview_status.text())
                self.assertIn("waiting for full fresh NO/flow coverage", pane.labview_status.text())
                self.assertIn("Keep this condition steady", pane.labview_status.text())


if __name__ == "__main__":
    unittest.main()
