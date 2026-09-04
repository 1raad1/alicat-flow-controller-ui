"""Pure numerical tests for joint NO/pressure response mapping."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from flow_controller.domain.bayesian import (
    SearchConfig, _fit_mapping_models, integrated_variance_reduction,
    predict_mapping, suggest,
)
from flow_controller.core.optimisation import Experiment


def config(**changes):
    values = dict(power_kw=10, bounds=((.15, .65), (1.05, 1.6), (.5, .85)),
                  initial_points=4, window_seconds=5, candidate_pool_size=128,
                  objective_mode="map_no_pressure")
    values.update(changes)
    return SearchConfig(**values)


def trial(point, no=40, pressure=5):
    return {"point": list(point), "status": "completed",
            "window": {"observed_point": list(point)},
            "result": {"corrected_no": no, "corrected_sem": .05},
            "pressure": {"rms_pa": pressure, "peak_abs_pa": pressure * 2,
                         "dominant_amplitude_pa": pressure * .8}}


def response_data():
    from scipy.stats import qmc
    unit = qmc.Sobol(3, scramble=True, seed=81).random_base2(4)
    points = np.asarray([.15, 1.05, .5]) + unit * np.asarray([.5, .55, .35])
    return [trial(point, 40 + 15 * np.sin(5 * x[0]), 5 + 3 * np.cos(5 * x[1]))
            for point, x in zip(points, unit)]


def dual_response_data():
    data = response_data()
    for index, item in enumerate(data):
        amplitude = item["pressure"]["dominant_amplitude_pa"]
        item["pressure"] = {"transducers": [
            {"id": "pressure_1", "metrics": {"dominant_amplitude_pa": amplitude}},
            {"id": "pressure_2", "metrics": {"dominant_amplitude_pa": amplitude * (1.2 + .02 * index)}},
        ]}
    return data


class PressureMappingTests(unittest.TestCase):
    def test_schema_three_flat_campaign_still_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "campaign.json"
            Experiment.create(path, config())
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema"] = 3
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = Experiment.load(path)
            self.assertEqual(loaded.data["schema"], 3)
    def test_dual_mapping_fits_each_peak_amplitude_independently(self):
        settings = config(pressure_metric="rms_pa")
        data = dual_response_data()
        result = predict_mapping(settings, data, [item["point"] for item in data])
        self.assertEqual(set(result), {"no_mean", "no_sd", "pressure_1_mean", "pressure_1_sd",
                                      "pressure_2_mean", "pressure_2_sd"})
        answer = suggest(settings, data, seed=7)
        self.assertEqual(answer["pressure_metric"], "dominant_amplitude_pa")
        self.assertAlmostEqual(answer["mapping_score"], .5 * answer["mapping_no_score"]
                               + .25 * answer["mapping_pressure_1_score"]
                               + .25 * answer["mapping_pressure_2_score"])
        for key in ("predicted_pressure_1_pa", "pressure_1_latent_sd_pa",
                    "predicted_pressure_2_pa", "pressure_2_latent_sd_pa",
                    "pressure_1_model", "pressure_2_model"):
            self.assertIn(key, answer)
        changed = deepcopy(data)
        changed[0]["pressure"]["transducers"][0]["metrics"]["dominant_amplitude_pa"] += 3
        updated = predict_mapping(settings, changed, [item["point"] for item in data])
        np.testing.assert_allclose(updated["pressure_2_mean"], result["pressure_2_mean"])
        self.assertFalse(np.allclose(updated["pressure_1_mean"], result["pressure_1_mean"]))
    def test_legacy_config_and_new_config_roundtrip(self):
        values = config().to_dict()
        for key in ("objective_mode", "pressure_metric", "mapping_no_weight"):
            values.pop(key)
        legacy = SearchConfig(**values)
        self.assertEqual(legacy.objective_mode, "minimise_no")
        self.assertEqual(SearchConfig(**json.loads(json.dumps(legacy.to_dict()))), legacy)
        settings = config(pressure_metric="peak_abs_pa", mapping_no_weight=.3)
        self.assertEqual(SearchConfig(**json.loads(json.dumps(settings.to_dict()))), settings)

    def test_invalid_mapping_configuration(self):
        for changes in ({"objective_mode": "minimise_pressure"},
                        {"pressure_metric": "unknown"},
                        *({"mapping_no_weight": value} for value in
                          (0, 1, -.1, 1.1, True, None, float("nan"), float("inf")))):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config(**changes)

    def test_completed_pressure_required_even_during_initial_design(self):
        for pressure in (None, {}, {"rms_pa": -1}, {"rms_pa": float("nan")},
                         {"rms_pa": float("inf")}, {"rms_pa": True}):
            item = trial([.3, 1.2, .7])
            item["pressure"] = pressure
            with self.subTest(pressure=pressure), self.assertRaises(ValueError):
                suggest(config(), [item])
        item["status"] = "invalid"
        self.assertIn("initial", suggest(config(), [item])["method"])
        item = trial([.3, 1.2, .7], pressure=0)
        self.assertIn("initial", suggest(config(), [item])["method"])

    def test_mapping_has_no_no_centred_candidates_and_auditable_scores(self):
        settings = config()
        data = response_data()
        answer = suggest(settings, data, seed=7)
        self.assertEqual(answer["candidate_pool_generated"], answer["sobol_candidate_count"])
        self.assertNotIn("expected_improvement", answer)
        self.assertIn("variance reduction", answer["method"])
        self.assertLessEqual(answer["mapping_reference_count"], 256)
        self.assertAlmostEqual(answer["mapping_score"],
                               .5 * (answer["mapping_no_score"] + answer["mapping_pressure_score"]))
        self.assertEqual(answer["pressure_model"]["response_units"], "Pa")
        self.assertEqual(answer["no_model"]["response_units"], "ppm")
        changed = deepcopy(data)
        changed[0]["pressure"]["peak_abs_pa"] += 1
        self.assertNotEqual(answer["training_data_sha256"],
                            suggest(settings, changed, seed=7)["training_data_sha256"])
        self.assertNotEqual(answer["training_data_sha256"], suggest(
            config(pressure_metric="peak_abs_pa"), data, seed=7)["training_data_sha256"])

    def test_response_models_are_independent_and_both_data_affect_acquisition(self):
        settings = config()
        data = response_data()
        points = [item["point"] for item in data]
        original = predict_mapping(settings, data, points)
        answer = suggest(settings, data, seed=17)
        for response, other in (("no", "pressure"), ("pressure", "no")):
            changed = deepcopy(data)
            for index, item in enumerate(changed):
                if response == "no":
                    item["result"]["corrected_no"] = 20 + 10 * (index % 3)
                else:
                    item["pressure"]["rms_pa"] = 2 + (index % 3)
            predictions = predict_mapping(settings, changed, points)
            np.testing.assert_allclose(predictions[other + "_mean"], original[other + "_mean"])
            self.assertFalse(np.allclose(predictions[response + "_mean"], original[response + "_mean"]))
            updated = suggest(settings, changed, seed=17)
            self.assertNotAlmostEqual(answer["mapping_score"], updated["mapping_score"], places=5)
            self.assertNotEqual(answer["point"], updated["point"])

    def test_pressure_rescaling_preserves_acquisition(self):
        settings = config()
        data = response_data()
        original = suggest(settings, data, seed=11)
        for multiplier in (.001, 1000):
            changed = deepcopy(data)
            for item in changed:
                item["pressure"]["rms_pa"] *= multiplier
            answer = suggest(settings, changed, seed=11)
            self.assertEqual(original["point"], answer["point"])
            self.assertAlmostEqual(original["mapping_score"], answer["mapping_score"], places=5)
            self.assertAlmostEqual(original["predicted_pressure_pa"],
                                   answer["predicted_pressure_pa"] / multiplier, places=4)

    def test_constant_duplicate_data_are_finite_and_reproducible(self):
        settings = config()
        data = [trial([.3, 1.2, .7], pressure=0) for _ in range(4)]
        answer = suggest(settings, data, seed=5)
        self.assertEqual(answer, suggest(settings, data, seed=5))
        for key in ("predicted_no", "latent_sd", "predicted_pressure_pa",
                    "pressure_latent_sd_pa", "mapping_score"):
            self.assertTrue(np.isfinite(answer[key]))
        self.assertGreaterEqual(answer["mapping_score"], 0)

    def test_mapping_respects_flow_ceilings(self):
        settings = config()
        data = response_data()
        ceiling = settings.targets([.4, 1.3, .7])["rich_air"]
        answer = suggest(settings, data, {"rich_air": ceiling}, seed=5)
        self.assertLessEqual(settings.targets(answer["point"])["rich_air"], ceiling)
        with self.assertRaisesRegex(ValueError, "No candidate"):
            suggest(settings, data, {"rich_air": 0})

    def test_slice_predictions_validate_initial_count_points_and_pressure(self):
        settings = config()
        data = response_data()
        with self.assertRaisesRegex(ValueError, "initial design"):
            predict_mapping(settings, data[:3], [[.3, 1.2, .7]])
        for point in ([.3, 1.2], [.9, 1.2, .7], [float("nan"), 1.2, .7]):
            with self.assertRaises(ValueError):
                predict_mapping(settings, data, [point])
        self.assertEqual(predict_mapping(settings, data, []),
                         {"no_mean": [], "no_sd": [], "pressure_mean": [], "pressure_sd": []})
        data[0].pop("pressure")
        with self.assertRaisesRegex(ValueError, "pressure"):
            predict_mapping(settings, data, [[.3, 1.2, .7]])

    def test_integrated_reduction_matches_conditioned_covariance(self):
        from scipy.linalg import cho_solve
        settings = config()
        model = _fit_mapping_models(settings, response_data(), seed=5)[1][0]
        reference = np.asarray([[.2, .3, .4], [.4, .5, .6], [.7, .6, .5]])
        candidates = np.asarray([[.3, .7, .2], [.5, .1, .8]])
        kernel = model.kernel_.k1
        locations = np.vstack([reference, candidates])
        cross = kernel(locations, model.X_train_)
        covariance = kernel(locations) - cross @ cho_solve((model.L_, True), cross.T)
        reference_cov = covariance[:3, :3]
        expected = []
        for index in range(2):
            col = covariance[:3, 3 + index]
            updated = reference_cov - np.outer(col, col) / (
                covariance[3 + index, 3 + index] + model.kernel_.k2.noise_level + 1e-8)
            expected.append((np.trace(reference_cov) - np.trace(updated)) / np.trace(reference_cov))
        actual, _, _ = integrated_variance_reduction(model, reference, candidates)
        np.testing.assert_allclose(actual, expected, rtol=1e-9, atol=1e-12)


if __name__ == "__main__":
    unittest.main()
