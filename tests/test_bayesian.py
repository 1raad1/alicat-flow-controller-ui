"""Numerical and persistence tests; no device connections."""

from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from flow_controller.domain.bayesian import SearchConfig, corrected_no, suggest
from flow_controller.core.optimisation import Experiment, MeasurementWindow, observed_condition


def config(**kwargs):
    return SearchConfig(power_kw=10, bounds=((.15, .65), (1.05, 1.6), (.5, .85)),
                        initial_points=4, window_seconds=5, **kwargs)


def extended_config(**kwargs):
    values = dict(
        power_kw=10,
        bounds=((.15, .65), (1.05, 1.6), (.5, .85), (8, 12), (.75, 1.0)),
        split_rich=.9, initial_points=6, window_seconds=5,
        optimise_power=True, optimise_split=True, candidate_pool_size=128)
    values.update(kwargs)
    return SearchConfig(**values)


def window_for(settings, point):
    targets = settings.targets(point)
    window = MeasurementWindow(settings, targets, {})
    stamp = datetime(2026, 8, 27, 12)
    for seconds in (0, 2, 5):
        window.add(stamp + timedelta(seconds=seconds), targets)
    return window.finish()


def synthetic_trial(settings, point, value):
    return {"point": list(point), "status": "completed", "window": window_for(settings, point),
            "result": {"corrected_no": value, "corrected_sem": 1.0}}


class BayesianTests(unittest.TestCase):
    def test_reference_correction_and_standard_error(self):
        value, sem = corrected_no(100, 10, 15, 2)
        self.assertAlmostEqual(value, 100 * 5.9 / 10.9)
        self.assertAlmostEqual(sem, 2 * 5.9 / 10.9)
        self.assertEqual(corrected_no(0, 10, 15), (0, None))

    def test_dilution_does_not_look_like_an_improvement(self):
        first = corrected_no(100, 10, 15)[0]
        second = corrected_no(100 * (20.9 - 15) / (20.9 - 10), 15, 15)[0]
        self.assertAlmostEqual(first, second)

    def test_rejects_bad_readings_and_bounds(self):
        for no, o2 in ((float("nan"), 5), (-1, 5), (5001, 5), (10, 20.9), (10, float("inf"))):
            with self.subTest(no=no, o2=o2), self.assertRaises(ValueError):
                corrected_no(no, o2, 15)
        for bounds in (((0, .5), (1, 2), (.5, .8)),
                       ((.1, 1), (1, 2), (.5, .8)),
                       ((.1, .5), (1, 2), (.5, 1)),
                       ((.1, .5), (2, 1), (.5, .8))):
            with self.assertRaises(ValueError):
                SearchConfig(10, bounds)
        with self.assertRaises(ValueError):
            config().request([.3, 1.2, .7, 99])

    def test_initial_suggestions_reproducible_and_respect_limits(self):
        settings = config()
        first = suggest(settings, [], seed=7, pool_size=128)
        self.assertEqual(first, suggest(settings, [], seed=7, pool_size=128))
        targets = settings.targets(first["point"])
        limited = suggest(settings, [], {"rich_air": targets["rich_air"]}, seed=7, pool_size=128)
        self.assertLessEqual(settings.targets(limited["point"])["rich_air"], targets["rich_air"])
        with self.assertRaisesRegex(ValueError, "No candidate"):
            suggest(settings, [], {"rich_air": 0}, pool_size=32)

    def test_invalid_trials_are_not_modelled_as_zero_or_reoffered(self):
        settings = config()
        first = suggest(settings, [], seed=7, pool_size=128)
        invalid = dict(first, status="invalid", result=None)
        second = suggest(settings, [invalid], seed=7, pool_size=128)
        self.assertNotEqual(first["point"], second["point"])
        self.assertIn("initial", second["method"])

    def test_bayesian_phase_is_finite_with_duplicate_and_constant_observations(self):
        settings = config()
        trials = [synthetic_trial(settings, [.3, 1.2, .7], 40) for _ in range(4)]
        answer = suggest(settings, trials, seed=5, pool_size=64)
        self.assertIn("Bayesian", answer["method"])
        self.assertTrue(all(np.isfinite(answer[k]) for k in
                            ("predicted_no", "latent_sd", "expected_improvement")))
        self.assertGreaterEqual(answer["expected_improvement"], 0)
        settings.request(answer["point"])

    def test_bayesian_search_improves_a_synthetic_bowl(self):
        settings = config()
        optimum = np.array([.38, 1.29, .68])
        span = np.array([.5, .55, .35])
        trials = []
        for index in range(12):
            answer = suggest(settings, trials, seed=index + 10, pool_size=128)
            value = 10 + 100 * float(np.sum(((np.asarray(answer["point"]) - optimum) / span) ** 2))
            trials.append(synthetic_trial(settings, answer["point"], value))
        initial_best = min(t["result"]["corrected_no"] for t in trials[:4])
        final_best = min(t["result"]["corrected_no"] for t in trials)
        self.assertLess(final_best, initial_best)
        self.assertLess(final_best, 20)

    def test_optional_power_and_split_expand_search_to_five_dimensions(self):
        settings = extended_config()
        trials = []
        for index in range(settings.initial_points):
            answer = suggest(settings, trials, seed=30 + index, pool_size=128)
            self.assertEqual(len(answer["point"]), 5)
            request = settings.request(answer["point"])
            self.assertTrue(8 <= request.power_kw <= 12)
            self.assertTrue(.75 <= request.split_rich <= 1)
            value = 20 + 10 * sum((np.asarray(answer["point"]) -
                                   np.array([.35, 1.3, .68, 10, .9])) ** 2)
            trials.append(synthetic_trial(settings, answer["point"], value))
        answer = suggest(settings, trials, seed=99, pool_size=128)
        self.assertIn("Bayesian", answer["method"])
        self.assertEqual(len(answer["point"]), 5)
        self.assertTrue(np.isfinite(answer["expected_improvement"]))

    def test_extended_window_models_measured_power_and_split(self):
        settings = extended_config()
        point = [.3, 1.2, .7, 11, .8]
        window = window_for(settings, point)
        observed = settings.observed_vector(window)
        np.testing.assert_allclose(observed, point, rtol=1e-12)
        self.assertAlmostEqual(window["split_rich"], .8)
        self.assertAlmostEqual(window["power_kw"], 11)

    def test_dimension_controls_initial_design_and_candidate_pool_validation(self):
        with self.assertRaisesRegex(ValueError, "6 to 100"):
            extended_config(initial_points=5)
        with self.assertRaisesRegex(ValueError, "Candidate pool"):
            config(candidate_pool_size=32)
        with self.assertRaisesRegex(ValueError, "fuel-split bounds"):
            SearchConfig(10, ((.15, .65), (1.05, 1.6), (.5, .85), (.8, 1.1)),
                         split_rich=.9, initial_points=5, optimise_split=True)


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "experiment.fcbo.json"
        self.config = config()
        self.experiment = Experiment.create(self.path, self.config)
        self.point = [.3, 1.2, .7]

    def response_condition(self, offset=0):
        return {
            "target_flows": {"nh3_rich": 2.0 + offset, "rich_air": 10.0 + offset},
            "measured_flows": {"nh3_rich": 2.01 + offset, "rich_air": 10.02 + offset},
            "assignments": {"nh3_rich": "A", "rich_air": "B"},
            "captured_at": "2026-08-27T12:00:00+00:00",
            "request": {"point": [.3, 1.2, .7], "operator": "test"},
        }

    def response_run(self, *, successful=True, delay=12.5):
        samples = [
            {"timestamp": 100.0, "elapsed_s": -1.0, "no_ppm": 100.0,
             "seq": 1, "phase": "baseline", "flow_stable": True},
            {"timestamp": 101.0, "elapsed_s": 0.0, "no_ppm": 99.0,
             "seq": 2, "phase": "response", "flow_stable": False},
            {"timestamp": 102.0, "elapsed_s": 1.0, "no_ppm": 80.0,
             "seq": 3, "phase": "response", "flow_stable": True},
        ]
        return {"successful": successful, "source_id": "mexa-source",
                "recommended_delay_s": delay, "command_to_change_s": .5,
                "command_to_stable_s": 10.0, "flow_to_stable_s": 2.0,
                "t10_s": .6, "t50_s": 2.0, "t90_s": 8.0,
                "rise_10_90_s": 7.4, "baseline_no_ppm": 100.0,
                "final_no_ppm": 80.0, "criteria": {"band_ppm": 2.0},
                "caveat": "Synthetic fixture", "raw_samples": samples}

    def store_response_conditions(self):
        self.experiment.set_response_condition("A", self.response_condition())
        self.experiment.set_response_condition("B", self.response_condition(1))

    def test_round_trip_pending_window_and_completed_result(self):
        trial = self.experiment.add_trial({"point": self.point, "method": "test"})
        self.assertEqual(Experiment.load(self.path).pending["id"], trial["id"])
        self.experiment.record_window(window_for(self.config, self.point))
        reopened = Experiment.load(self.path)
        reopened.complete(100, 10, 2, "repeat later")
        result = Experiment.load(self.path)
        self.assertIsNone(result.pending)
        self.assertEqual(result.trials[0]["result"]["no_ppm"], 100)
        self.assertAlmostEqual(result.trials[0]["window"]["observed_point"][0], .3)

    def test_new_campaign_has_empty_analyser_response_and_legacy_files_load(self):
        self.assertEqual(self.experiment.data["analyser_response"],
                         {"conditions": {"A": None, "B": None},
                          "runs": [], "selected_run_id": None})
        self.assertEqual(self.experiment.response_delay_seconds, 0)
        self.assertEqual(self.experiment.total_live_logging_seconds,
                         self.config.window_seconds)
        data = deepcopy(self.experiment.data)
        del data["analyser_response"]
        self.path.write_text(json.dumps(data), encoding="utf-8")
        legacy = Experiment.load(self.path)
        self.assertIsNone(legacy.response_condition("A"))
        self.assertIsNone(legacy.selected_response_run)
        self.assertEqual(legacy.response_delay_seconds, 0)

    def test_response_result_round_trip_delay_and_condition_invalidation(self):
        self.store_response_conditions()
        result = self.response_run(delay=5)
        result["t10_s"] = None
        stored = self.experiment.record_response_run(result)
        reopened = Experiment.load(self.path)
        self.assertEqual(reopened.selected_response_run["id"], stored["id"])
        self.assertEqual(reopened.response_delay_seconds, 5)
        self.assertEqual(reopened.total_live_logging_seconds, 10)
        copy = reopened.response_condition("a")
        copy["target_flows"]["nh3_rich"] = 999
        self.assertNotEqual(copy, reopened.response_condition("A"))
        reopened.set_response_condition("B", self.response_condition(2))
        self.assertIsNone(reopened.selected_response_run)
        self.assertEqual(reopened.response_delay_seconds, 0)
        self.assertEqual(len(reopened.data["analyser_response"]["runs"]), 1)

    def test_unsuccessful_response_run_is_retained_but_not_selected(self):
        self.store_response_conditions()
        run = self.experiment.record_response_run(self.response_run(successful=False))
        self.assertIsNone(self.experiment.selected_response_run)
        self.assertEqual(self.experiment.data["analyser_response"]["runs"][0]["id"], run["id"])

    def test_response_history_is_bounded_to_latest_twenty_runs(self):
        self.store_response_conditions()
        first = self.experiment.record_response_run(self.response_run())
        latest = None
        for index in range(20):
            latest = self.experiment.record_response_run(self.response_run(delay=12.5 + index))
        runs = self.experiment.data["analyser_response"]["runs"]
        self.assertEqual(len(runs), 20)
        self.assertNotIn(first["id"], {run["id"] for run in runs})
        self.assertEqual(self.experiment.selected_response_run["id"], latest["id"])

    def test_response_history_retains_runs_referenced_by_saved_trials(self):
        self.store_response_conditions()
        referenced = self.experiment.record_response_run(self.response_run(delay=5))
        window = window_for(self.config, self.point)
        window.update(pre_window_delay_s=5, response_run_id=referenced["id"],
                      total_observation_s=window["duration_s"] + 5)
        self.experiment.add_trial({"point": self.point, "method": "test"})
        self.experiment.record_window(window)
        self.experiment.complete(100, 10)
        for index in range(20):
            self.experiment.record_response_run(self.response_run(delay=10 + index))
        identifiers = {run["id"] for run in self.experiment.data["analyser_response"]["runs"]}
        self.assertIn(referenced["id"], identifiers)
        self.assertEqual(len(identifiers), 20)
        self.assertEqual(
            Experiment.load(self.path).trials[0]["window"]["response_run_id"],
            referenced["id"])

    def test_response_persistence_rejects_malformed_and_oversized_data(self):
        bad = self.response_condition()
        bad["measured_flows"].pop("rich_air")
        with self.assertRaises(ValueError):
            self.experiment.set_response_condition("A", bad)
        bad = self.response_condition()
        bad["target_flows"]["nh3_rich"] = float("nan")
        with self.assertRaises(ValueError):
            self.experiment.set_response_condition("A", bad)
        bad["target_flows"]["nh3_rich"] = True
        with self.assertRaises(ValueError):
            self.experiment.set_response_condition("A", bad)
        self.store_response_conditions()
        bad_run = self.response_run()
        bad_run["raw_samples"][1]["no_ppm"] = float("inf")
        with self.assertRaises(ValueError):
            self.experiment.record_response_run(bad_run)
        oversized = self.response_run()
        oversized["caveat"] = "x" * 4001
        with self.assertRaises(ValueError):
            self.experiment.record_response_run(oversized)
        too_many = self.response_run()
        too_many["raw_samples"] = [dict(
            timestamp=float(i + 1), elapsed_s=float(i), no_ppm=1.0, seq=i + 1,
            phase="response") for i in range(4001)]
        with self.assertRaises(ValueError):
            self.experiment.record_response_run(too_many)

    def test_load_rejects_invalid_selected_response_and_deep_metadata(self):
        self.store_response_conditions()
        self.experiment.record_response_run(self.response_run())
        data = deepcopy(self.experiment.data)
        data["analyser_response"]["selected_run_id"] = "missing"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            Experiment.load(self.path)
        data = deepcopy(self.experiment.data)
        data["analyser_response"]["runs"][0]["criteria"] = {
            "a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            Experiment.load(self.path)

    def test_single_pending_and_invalid_not_zero(self):
        self.experiment.add_trial({"point": self.point, "method": "test"})
        with self.assertRaises(ValueError):
            self.experiment.add_trial({"point": self.point, "method": "test"})
        with self.assertRaises(ValueError):
            self.experiment.complete(0, 10)
        self.experiment.invalidate("Flame extinguished")
        self.assertIsNone(self.experiment.trials[0]["result"])
        self.assertEqual(Experiment.load(self.path).trials[0]["status"], "invalid")

    def test_failed_save_does_not_mutate_memory_or_previous_file(self):
        previous = self.path.read_bytes()
        with patch("flow_controller.core.optimisation.atomic_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.experiment.add_trial({"point": self.point, "method": "test"})
        self.assertEqual(self.experiment.trials, [])
        self.assertEqual(self.path.read_bytes(), previous)

    def test_load_rejects_tampered_correction(self):
        self.experiment.add_trial({"point": self.point, "method": "test"})
        self.experiment.record_window(window_for(self.config, self.point))
        self.experiment.complete(100, 10)
        data = deepcopy(self.experiment.data)
        data["trials"][0]["result"]["corrected_no"] = 0
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            Experiment.load(self.path)

    def test_csv_exports_and_protects_experiment_file(self):
        self.experiment.add_trial({"point": self.point, "method": "test"})
        self.experiment.record_window(window_for(self.config, self.point))
        self.experiment.complete(100, 10, notes="=formula")
        with self.assertRaises(ValueError):
            self.experiment.export_csv(self.path)
        path = self.path.with_suffix(".csv")
        self.experiment.export_csv(path)
        text = path.read_text(encoding="utf-8-sig")
        self.assertIn("corrected_no_ppm", text)
        self.assertIn("'=formula", text)

    def test_window_response_metadata_validates_and_exports(self):
        self.store_response_conditions()
        run = self.experiment.record_response_run(self.response_run(delay=12.5))
        window = window_for(self.config, self.point)
        window.update(pre_window_delay_s=12.5, response_run_id=run["id"],
                      total_observation_s=window["duration_s"] + 12.5)
        self.experiment.add_trial({"point": self.point, "method": "test"})
        self.experiment.record_window(window)
        self.experiment.complete(100, 10)
        reopened = Experiment.load(self.path)
        self.assertEqual(reopened.trials[0]["window"]["response_run_id"], run["id"])
        path = self.path.with_suffix(".csv")
        reopened.export_csv(path)
        header, row = path.read_text(encoding="utf-8-sig").splitlines()[:2]
        self.assertIn("pre_window_delay_s,response_run_id,total_observation_s", header)
        self.assertIn(f"12.5,{run['id']},17.5", row)

    def test_window_response_metadata_rejects_inconsistent_total(self):
        window = window_for(self.config, self.point)
        window.update(pre_window_delay_s=12.5, total_observation_s=5)
        self.experiment.add_trial({"point": self.point, "method": "test"})
        with self.assertRaises(ValueError):
            self.experiment.record_window(window)
        window = window_for(self.config, self.point)
        window["response_run_id"] = "unknown"
        with self.assertRaises(ValueError):
            self.experiment.record_window(window)

    def test_legacy_three_variable_file_loads_without_new_config_fields(self):
        data = deepcopy(self.experiment.data)
        data["schema"] = 1
        for key in ("optimise_power", "optimise_split", "candidate_pool_size"):
            data["config"].pop(key, None)
        self.path.write_text(json.dumps(data), encoding="utf-8")
        reopened = Experiment.load(self.path)
        self.assertEqual(reopened.config.variable_names,
                         ("h2_fraction", "phi_stage1", "phi_overall"))
        self.assertEqual(reopened.config.dimensions, 3)

    def test_extended_csv_exports_requested_and_observed_variables(self):
        settings = extended_config()
        path = self.path.with_name("extended.fcbo.json")
        experiment = Experiment.create(path, settings)
        point = [.3, 1.2, .7, 11, .8]
        experiment.add_trial({"point": point, "method": "test"})
        experiment.record_window(window_for(settings, point))
        experiment.complete(100, 10)
        csv_path = path.with_suffix(".csv")
        experiment.export_csv(csv_path)
        text = csv_path.read_text(encoding="utf-8-sig")
        self.assertIn("observed_split_rich", text)
        self.assertIn(",11.0,0.8,", text)

    def test_capture_requires_duration_tracking_and_fresh_timestamps(self):
        targets = self.config.targets(self.point)
        window = MeasurementWindow(self.config, targets, {})
        stamp = datetime.now()
        window.add(stamp, targets)
        with self.assertRaises(ValueError):
            window.finish()
        with self.assertRaises(ValueError):
            window.add(stamp, targets)
        wrong = dict(targets, rich_air=0)
        with self.assertRaises(ValueError):
            window.add(stamp + timedelta(seconds=1), wrong)

    def test_exact_bounds_survive_flow_arithmetic_roundoff(self):
        for point in ([.15, 1.05, .5], [.65, 1.6, .85]):
            result = window_for(self.config, point)
            np.testing.assert_allclose(result["observed_point"], point, rtol=1e-12)


if __name__ == "__main__":
    unittest.main()
