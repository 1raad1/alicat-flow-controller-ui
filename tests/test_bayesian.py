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


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "experiment.fcbo.json"
        self.config = config()
        self.experiment = Experiment.create(self.path, self.config)
        self.point = [.3, 1.2, .7]

    def test_round_trip_pending_window_and_completed_result(self):
        trial = self.experiment.add_trial({"point": self.point, "method": "test"})
        self.assertEqual(Experiment.load(self.path).pending["id"], trial["id"])
        self.experiment.record_window(window_for(self.config, self.point))
        reopened = Experiment.load(self.path)
        reopened.complete(100, 10, 2, "repeat later")
        result = Experiment.load(self.path)
        self.assertIsNone(result.pending)
        self.assertEqual(result.trials[0]["result"]["no_ppm"], 100)
        self.assertEqual(result.trials[0]["window"]["observed_point"][0], .3)

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
