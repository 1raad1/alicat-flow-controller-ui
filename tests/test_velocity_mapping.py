"""Bulk-velocity search variable and larger-transducer pressure mapping."""

from copy import deepcopy
import csv
import json
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np

from flow_controller.core.optimisation import Experiment, MeasurementWindow, SCHEMA
from flow_controller.domain import combustion
from flow_controller.domain.bayesian import (
    SearchConfig, _fit_mapping_models, integrated_variance_reduction, predict_mapping, suggest,
)
from flow_controller.domain.rql import power_for_stage1_velocity, stage1_bulk_velocity
from tests import test_qt_optimiser as qt_base
from tests.test_pressure_integration import dual_pressure_for
from tests.test_pressure_mapping import response_data


DIAMETER_MM = 25.0
AREA_MM2 = math.pi * DIAMETER_MM ** 2 / 4
BOUNDS = ((.15, .65), (1.05, 1.6), (.5, .85), (4.0, 9.0))
POINT = [.3, 1.2, .7, 6.0]


def velocity_config(**changes):
    values = dict(power_kw=10, bounds=BOUNDS, initial_points=5, window_seconds=5,
                  candidate_pool_size=128, throughput="bulk_velocity",
                  inlet_area_mm2=AREA_MM2)
    values.update(changes)
    return SearchConfig(**values)


def larger_config(**changes):
    values = dict(power_kw=10, bounds=BOUNDS[:3], initial_points=4, window_seconds=5,
                  candidate_pool_size=128, objective_mode="map_no_pressure",
                  pressure_metric="rms_pa", pressure_acquisition="larger_transducer")
    values.update(changes)
    return SearchConfig(**values)


def larger_response_data():
    """Transducer 2 is the larger reading on even trials, transducer 1 on odd ones."""
    data = response_data()
    for index, item in enumerate(data):
        base = item["pressure"]["rms_pa"]
        first, second = (base, base * 1.5) if index % 2 == 0 else (base * 1.4, base)
        item["pressure"] = {"transducers": [
            {"id": "pressure_1", "metrics": {"rms_pa": first, "dominant_amplitude_pa": 1.0}},
            {"id": "pressure_2", "metrics": {"rms_pa": second, "dominant_amplitude_pa": 1.0}},
        ]}
    return data


class BulkVelocityThroughputTests(unittest.TestCase):
    def test_targets_deliver_the_requested_inlet_velocity_and_both_phis(self):
        settings = velocity_config()
        for point in (POINT, [.6, 1.55, .52, 8.5], [.16, 1.06, .84, 4.1]):
            with self.subTest(point=point):
                targets = settings.targets(point)
                self.assertAlmostEqual(stage1_bulk_velocity(targets, AREA_MM2), point[3], places=10)
                stage1 = targets["nh3_rich"] + targets["h2_rich"] + targets["rich_air"]
                self.assertAlmostEqual(combustion.bulk_velocity(stage1, DIAMETER_MM), point[3], places=10)
                calc = combustion.CombustionCalculator()
                self.assertAlmostEqual(
                    calc.phi(targets["nh3_rich"], targets["h2_rich"], targets["rich_air"]), point[1])
                self.assertAlmostEqual(
                    calc.phi(targets["nh3_rich"], targets["h2_rich"],
                             targets["rich_air"] + targets["lean_air"]), point[2])
                self.assertAlmostEqual(
                    targets["h2_rich"] / (targets["nh3_rich"] + targets["h2_rich"]), point[0])

    def test_thermal_input_is_derived_and_matches_the_fuel_delivered(self):
        settings = velocity_config()
        values = settings.values(POINT)
        targets = settings.targets(POINT)
        self.assertAlmostEqual(
            values["power_kw"],
            combustion.power_kw({"NH3": targets["nh3_rich"], "H2": targets["h2_rich"]}))
        self.assertAlmostEqual(settings.request(POINT).power_kw, values["power_kw"])
        # Velocity is proportional to firing rate with everything else held.
        doubled = settings.values(POINT[:3] + [POINT[3] * 1.5])["power_kw"]
        self.assertAlmostEqual(doubled, values["power_kw"] * 1.5)
        # Richer stage 1 carries less air per unit fuel, so more power per m/s.
        richer = settings.values([.3, 1.5, .7, 6.0])["power_kw"]
        self.assertGreater(richer, values["power_kw"])

    def test_fuel_split_scales_total_power_but_not_inlet_velocity(self):
        settings = velocity_config(bounds=BOUNDS + ((.6, 1.0),), optimise_split=True,
                                   initial_points=6)
        self.assertEqual(settings.variable_names[-2:], ("bulk_velocity", "split_rich"))
        point = POINT + [.8]
        targets = settings.targets(point)
        self.assertAlmostEqual(stage1_bulk_velocity(targets, AREA_MM2), 6.0, places=10)
        self.assertAlmostEqual(settings.values(point)["power_kw"],
                               power_for_stage1_velocity(6.0, AREA_MM2, .3, 1.2) / .8)
        self.assertGreater(targets["nh3_lean"], 0)

    def test_configuration_rules(self):
        settings = velocity_config()
        self.assertEqual(settings.variable_names,
                         ("h2_fraction", "phi_stage1", "phi_overall", "bulk_velocity"))
        self.assertEqual(settings.dimensions, 4)
        self.assertEqual(SearchConfig(**json.loads(json.dumps(settings.to_dict()))), settings)
        for changes in ({"inlet_area_mm2": None}, {"inlet_area_mm2": 0},
                        {"inlet_area_mm2": float("nan")}, {"inlet_area_mm2": 1e9},
                        {"throughput": "mass_flow"}, {"bounds": BOUNDS[:3]},
                        {"bounds": BOUNDS[:3] + ((9, 4),)}, {"bounds": BOUNDS[:3] + ((0, 4),)},
                        {"optimise_power": True, "bounds": BOUNDS + ((8, 12),)},
                        {"pressure_acquisition": "smaller_transducer"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                velocity_config(**changes)
        legacy = settings.to_dict()
        for key in ("throughput", "inlet_area_mm2", "pressure_acquisition"):
            legacy.pop(key)
        legacy["bounds"] = legacy["bounds"][:3]
        legacy["initial_points"] = 4
        older = SearchConfig(**legacy)
        self.assertEqual((older.throughput, older.pressure_acquisition), ("power", "mean_of_transducers"))
        self.assertEqual(older.values(POINT[:3])["power_kw"], 10)

    def test_suggestions_stay_inside_velocity_limits_and_flow_ceilings(self):
        settings = velocity_config()
        answer = suggest(settings, [], seed=3)
        self.assertEqual(len(answer["point"]), 4)
        for value, (low, high) in zip(answer["point"], BOUNDS):
            self.assertTrue(low <= value <= high)
        ceiling = settings.targets([.4, 1.3, .7, 5.0])["rich_air"]
        limited = suggest(settings, [], {"rich_air": ceiling}, seed=3)
        self.assertLessEqual(settings.targets(limited["point"])["rich_air"], ceiling)

    def test_measured_velocity_trains_the_model_and_is_exported(self):
        with tempfile.TemporaryDirectory() as folder:
            experiment = Experiment.create(Path(folder) / "velocity.json", velocity_config())
            self.assertEqual(experiment.data["schema"], SCHEMA)
            experiment.add_trial({"point": POINT, "method": "test"})
            targets = experiment.config.targets(POINT)
            # The meters settle 1% below target: the model must see what was measured.
            measured = {role: flow * .99 for role, flow in targets.items()}
            window = MeasurementWindow(experiment.config, targets, {})
            from datetime import datetime, timedelta
            start = datetime(2026, 9, 3, 12)
            for seconds in (0, 2, 5):
                window.add(start + timedelta(seconds=seconds), measured)
            experiment.record_window(window.finish())
            observed = experiment.config.observed_vector(experiment.pending["window"])
            self.assertEqual(len(observed), 4)
            self.assertAlmostEqual(observed[3], 6.0 * .99)
            experiment.complete(100, 10)
            reloaded = Experiment.load(experiment.path)
            log = reloaded.trials[0]["condition_log"]["requested_condition"]
            self.assertEqual(log["variables"]["bulk_velocity"], 6.0)
            self.assertAlmostEqual(log["variables"]["power_kw"],
                                   power_for_stage1_velocity(6.0, AREA_MM2, .3, 1.2))
            destination = Path(folder) / "velocity.csv"
            reloaded.export_csv(destination)
            with destination.open(encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["throughput"], "bulk_velocity")
            self.assertAlmostEqual(float(row["inlet_area_mm2"]), AREA_MM2)
            self.assertAlmostEqual(float(row["bulk_velocity_m_s"]), 6.0)
            self.assertAlmostEqual(float(row["observed_bulk_velocity_m_s"]), 6.0 * .99)
            self.assertAlmostEqual(float(row["power_kw"]), log["variables"]["power_kw"])
            self.assertAlmostEqual(float(row["observed_power_kw"]), float(row["power_kw"]) * .99)

    def test_measured_velocity_outside_the_limits_is_rejected(self):
        settings = velocity_config()
        targets = settings.targets(POINT)
        window = {"observed_point": POINT[:3],
                  "mean_flows": {role: flow * 2 for role, flow in targets.items()}}
        self.assertAlmostEqual(settings.observed_vector(window)[3], 12.0)
        from flow_controller.core.optimisation import validate_window
        capture = MeasurementWindow(settings, targets, {})
        from datetime import datetime, timedelta
        start = datetime(2026, 9, 3, 12)
        for seconds in (0, 2, 5):
            capture.add(start + timedelta(seconds=seconds), targets)
        saved = capture.finish()
        tighter = velocity_config(bounds=BOUNDS[:3] + ((6.5, 9.0),))
        with self.assertRaisesRegex(ValueError, "bulk_velocity is outside"):
            validate_window(saved, tighter)


class LargerTransducerMappingTests(unittest.TestCase):
    def test_each_transducer_and_the_larger_are_modelled(self):
        settings, data = larger_config(), larger_response_data()
        points = [item["point"] for item in data]
        result = predict_mapping(settings, data, points)
        self.assertEqual(set(result), {
            f"{name}_{suffix}" for name in ("no", "pressure_1", "pressure_2", "pressure_larger")
            for suffix in ("mean", "sd")})
        models = _fit_mapping_models(settings, data, seed=0)
        self.assertEqual(len(models), 4)
        expected = [max(m["metrics"]["rms_pa"] for m in item["pressure"]["transducers"])
                    for item in data]
        _gp, centre, scale = models[3]
        np.testing.assert_allclose(models[3][0].y_train_ * scale + centre, expected)
        self.assertEqual(predict_mapping(settings, data, []), {key: [] for key in result})

    def test_only_the_larger_reading_drives_the_next_test(self):
        settings, data = larger_config(), larger_response_data()
        answer = suggest(settings, data, seed=7)
        self.assertEqual(answer["pressure_acquisition"], "larger_transducer")
        self.assertEqual(answer["pressure_metric"], "rms_pa")
        self.assertIn("larger-pressure", answer["method"])
        self.assertAlmostEqual(answer["mapping_score"], .5 * answer["mapping_no_score"]
                               + .5 * answer["mapping_pressure_larger_score"])
        self.assertEqual(answer["mapping_pressure_score"], answer["mapping_pressure_larger_score"])
        self.assertEqual(answer["predicted_pressure_pa"], answer["predicted_pressure_larger_pa"])
        for key in ("predicted_pressure_1_pa", "predicted_pressure_2_pa",
                    "pressure_1_model", "pressure_2_model", "pressure_larger_model"):
            self.assertIn(key, answer)
        # Lowering the smaller reading leaves the larger, and so the choice, alone.
        quieter = deepcopy(data)
        quieter[0]["pressure"]["transducers"][0]["metrics"]["rms_pa"] *= .5
        unchanged = suggest(settings, quieter, seed=7)
        self.assertEqual(unchanged["point"], answer["point"])
        self.assertAlmostEqual(unchanged["mapping_score"], answer["mapping_score"])
        self.assertNotEqual(unchanged["predicted_pressure_1_pa"], answer["predicted_pressure_1_pa"])
        # Changing the larger reading changes the model the search uses.
        louder = deepcopy(data)
        for index, item in enumerate(louder):
            item["pressure"]["transducers"][index % 2 == 0]["metrics"]["rms_pa"] *= 1 + index % 3
        changed = suggest(settings, louder, seed=7)
        self.assertNotAlmostEqual(changed["mapping_score"], answer["mapping_score"], places=5)
        self.assertNotEqual(changed["training_data_sha256"], answer["training_data_sha256"])

    def test_larger_score_is_the_variance_reduction_of_the_larger_model(self):
        from scipy.stats import qmc
        settings, data = larger_config(), larger_response_data()
        answer = suggest(settings, data, seed=7)
        model = _fit_mapping_models(settings, data, seed=7)[3][0]
        pool = qmc.Sobol(3, scramble=True, seed=7).random_base2(7)
        bounds = np.asarray(settings.bounds)
        candidate = (np.asarray(answer["point"]) - bounds[:, 0]) / (bounds[:, 1] - bounds[:, 0])
        score, _mean, _sd = integrated_variance_reduction(model, pool[:256], candidate[None, :])
        self.assertAlmostEqual(float(score[0]), answer["mapping_pressure_larger_score"])

    def test_metric_is_required_on_both_transducers(self):
        settings, data = larger_config(), larger_response_data()
        data[0]["pressure"]["transducers"][1]["metrics"].pop("rms_pa")
        with self.assertRaisesRegex(ValueError, "rms_pa for each transducer"):
            suggest(settings, data)

    def test_equal_share_campaigns_are_unchanged(self):
        data = larger_response_data()
        settings = larger_config(pressure_acquisition="mean_of_transducers")
        answer = suggest(settings, data, seed=7)
        self.assertEqual(answer["pressure_metric"], "dominant_amplitude_pa")
        self.assertNotIn("pressure_acquisition", answer)
        self.assertNotIn("predicted_pressure_larger_pa", answer)
        self.assertEqual(len(_fit_mapping_models(settings, data, seed=0)), 3)


class QtVelocityMappingTests(unittest.TestCase):
    setUpClass = classmethod(qt_base.QtOptimiserTests.setUpClass.__func__)
    setUp = qt_base.QtOptimiserTests.setUp
    cleanup_session = qt_base.QtOptimiserTests.cleanup_session
    publish = qt_base.QtOptimiserTests.publish

    def dialog(self, *args, **kwargs):
        from flow_controller.ui.qt_optimiser import ExperimentDialog
        dialog = ExperimentDialog(*args, **kwargs)
        self.addCleanup(dialog.close)
        return dialog

    def test_dialog_sets_velocity_limits_and_makes_power_a_result(self):
        dialog = self.dialog(inlet_area_mm2=AREA_MM2, dual_pressure=True)
        velocity, pair, _ = dialog.optional["bulk_velocity"]
        power = dialog.optional["power_kw"][0]
        self.assertFalse(dialog.entries["inlet_area"].isEnabled())
        self.assertAlmostEqual(float(dialog.entries["inlet_area"].text()), AREA_MM2)
        power.setChecked(True)
        velocity.setChecked(True)
        self.assertFalse(power.isChecked())
        self.assertFalse(power.isEnabled())
        self.assertFalse(dialog.entries["power"].isEnabled())
        self.assertTrue(dialog.entries["inlet_area"].isEnabled())
        self.assertEqual(dialog.entries["initial"].text(), "5")
        for entries, values in zip(dialog.bounds + [pair], ((15, 65), (1.05, 1.6), (.5, .85), (4, 9))):
            for entry, value in zip(entries, values):
                entry.setText(str(value))
        dialog.objective_mode.setCurrentIndex(dialog.objective_mode.findData("map_no_pressure"))
        self.assertTrue(dialog.pressure_metric.isEnabled())
        self.assertEqual(dialog.pressure_metric.currentData(), "rms_pa")
        dialog.approved.setChecked(True)
        dialog.entries["inlet_area"].setText("")
        dialog.accept()
        self.assertIsNone(dialog.config)
        self.assertIn("inlet area", dialog.error.text().lower())
        dialog.entries["inlet_area"].setText(str(AREA_MM2))
        dialog.accept()
        config = dialog.config
        self.assertEqual(config.throughput, "bulk_velocity")
        self.assertEqual(config.bounds, BOUNDS)
        self.assertEqual(config.inlet_area_mm2, AREA_MM2)
        self.assertEqual((config.pressure_metric, config.pressure_acquisition),
                         ("rms_pa", "larger_transducer"))
        again = self.dialog(config)
        again.approved.setChecked(True)
        again.accept()
        self.assertEqual(again.config, config)
        velocity.setChecked(False)
        self.assertTrue(power.isEnabled())
        self.assertEqual(dialog.entries["initial"].text(), "4")

    def test_equal_share_choice_keeps_the_spectral_amplitude_rule(self):
        dialog = self.dialog(dual_pressure=True)
        dialog.objective_mode.setCurrentIndex(dialog.objective_mode.findData("map_no_pressure"))
        dialog.pressure_acquisition.setCurrentIndex(
            dialog.pressure_acquisition.findData("mean_of_transducers"))
        self.assertFalse(dialog.pressure_metric.isEnabled())
        self.assertEqual(dialog.pressure_metric.currentData(), "dominant_amplitude_pa")

    def test_velocity_campaign_runs_through_the_controller_window(self):
        from flow_controller.ui.qt_optimiser import point_text
        settings = velocity_config()
        self.controller.create(Path(self.directory.name) / "velocity.fcbo.json", settings)
        self.controller.experiment.add_trial({"point": POINT, "method": "test"})
        self.targets = settings.targets(POINT)
        self.publish(0)
        self.assertEqual(self.controller.prepare_targets(), self.targets)
        self.assertAlmostEqual(self.session.autocalc_request.power_kw,
                               settings.values(POINT)["power_kw"])
        self.controller.start_window(True, True)
        self.publish(2)
        self.publish(5)
        window = self.controller.finish_window()
        self.assertAlmostEqual(settings.observed_vector(window)[3], 6.0)
        self.assertNotAlmostEqual(window["power_kw"], 10, places=1)
        self.controller.complete(100, 10, basis_confirmed=True)
        self.assertIsNone(self.controller.experiment.pending)
        text = point_text(settings, POINT)
        self.assertIn("inlet velocity 6 m/s", text)
        self.assertIn("kW (derived)", text)

    def test_maps_show_larger_transducer_and_calculated_power(self):
        from flow_controller.ui.qt_optimiser import MappingWorker, OptimiserPane
        settings = velocity_config(objective_mode="map_no_pressure",
                                   pressure_acquisition="larger_transducer")
        self.controller.create(Path(self.directory.name) / "maps.fcbo.json", settings)
        pane = OptimiserPane(self.controller)
        self.addCleanup(pane.close)
        self.assertEqual(pane.map_x.count(), 4)
        self.assertEqual(pane.map_x.itemData(3), "bulk_velocity")
        context = {"x": np.linspace(.15, .65, 20), "y": np.linspace(4, 9, 20),
                   "x_name": "h2_fraction", "y_name": "bulk_velocity", "metric": "rms_pa",
                   "feasible": np.ones(400, dtype=bool),
                   "transducers": [{"id": "pressure_1", "label": "Combustor"},
                                   {"id": "pressure_2", "label": "Plenum"}]}
        xx, yy = np.meshgrid(context["x"], context["y"])
        points = np.tile(POINT, (400, 1))
        points[:, 0], points[:, 3] = xx.ravel(), yy.ravel()
        predictions = {f"{key}_{suffix}": np.full(400, value + (10 if suffix == "sd" else 0))
                       for value, key in enumerate(("no", "pressure_1", "pressure_2", "pressure_larger"), 1)
                       for suffix in ("mean", "sd")}
        worker = MappingWorker(settings, [], points, context)
        from unittest.mock import patch
        with patch("flow_controller.domain.bayesian.predict_mapping", return_value=predictions):
            result, _context = worker._compute()
        worker._release()
        np.testing.assert_allclose(
            result["power_kw_mean"], [settings.values(point)["power_kw"] for point in points])
        pane._map_result = (result, context)
        pane._draw_maps()
        self.assertEqual([float(image.image[0, 0]) for image in pane.map_images[:4]], [1, 2, 3, 4])
        self.assertIn("Larger of the two", pane.map_plots[3].getPlotItem().titleLabel.text)
        self.assertIn("RMS", pane.map_plots[1].getPlotItem().titleLabel.text)
        self.assertIn("Thermal input", pane.map_plots[4].getPlotItem().titleLabel.text)
        power_image = pane.map_images[4].image.copy()
        self.assertGreater(float(np.ptp(power_image)), 0)
        pane.map_uncertainty.setChecked(True)
        self.assertEqual([float(image.image[0, 0]) for image in pane.map_images[:4]], [11, 12, 13, 14])
        np.testing.assert_allclose(pane.map_images[4].image, power_image)
        self.assertIn("calculated", pane.map_plots[4].getPlotItem().titleLabel.text)

    def test_history_reports_each_rms_and_the_larger(self):
        from flow_controller.ui.qt_optimiser import OptimiserPane
        settings = larger_config()
        self.controller.create(Path(self.directory.name) / "larger.fcbo.json", settings)
        self.controller.experiment.add_trial({"point": POINT[:3], "method": "test"})
        self.targets = settings.targets(POINT[:3])
        self.publish(0)
        self.controller.start_window(True, True)
        self.publish(2)
        self.publish(5)
        self.controller.finish_window()
        experiment = self.controller.experiment
        experiment.attach_pressure(dual_pressure_for(experiment))
        experiment.complete(100, 10)
        self.assertIn("rms_pa per transducer", experiment.data["objective"])
        pane = OptimiserPane(self.controller)
        self.addCleanup(pane.close)
        text = pane.history.item(0).text()
        for fragment in ("Combustor 2 Pa", "Plenum 4 Pa", "larger 4 Pa"):
            self.assertIn(fragment, text)
        self.assertIn("search on the larger transducer", pane.summary.text())
        destination = Path(self.directory.name) / "larger.csv"
        experiment.export_csv(destination)
        with destination.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual((float(row["pressure_1_rms_pa"]), float(row["pressure_2_rms_pa"]),
                          float(row["pressure_larger_pa"])), (2, 4, 4))
        self.assertEqual(row["pressure_acquisition"], "larger_transducer")


if __name__ == "__main__":
    unittest.main()
