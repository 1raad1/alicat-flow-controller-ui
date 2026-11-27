"""Campaign plan, convergence readout and the suggest() diagnostics behind it."""

from dataclasses import replace
import json
import math
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QDialog

from flow_controller.core.optimisation import Experiment, MAX_EXTENSION_DETAIL, default_plan, validate_plan
from flow_controller.core.optimiser_controller import OptimiserController
from flow_controller.core.session import FlowSession, MODE_STAGED
from flow_controller.domain import convergence
from flow_controller.domain.bayesian import (ELSEWHERE_RADIUS, LCB_MULTIPLIER, LENGTH_SCALE_BOUNDS,
                                             suggest)
from flow_controller.domain.convergence import assess_convergence, replicate_noise
from flow_controller.ui.qt_optimiser import ExperimentDialog, OptimiserPane
from tests.test_bayesian import config, extended_config, synthetic_trial


BEST_POINT = [.4, 1.3, .7]


def completed(value, identifier=None, repeat_of=None, **extra):
    trial = {"status": "completed", "result": {"corrected_no": value}, **extra}
    if identifier is not None:
        trial["id"] = identifier
    if repeat_of is not None:
        trial["repeat_of"] = repeat_of
    return trial


def diagnosed(value=52.0, *, ei=.5, best=50.0, point=None, lcb=49.5, noise=.1, scales=(.3, .3, .3)):
    """A completed trial carrying what suggest() reported when it was proposed."""
    return completed(value, expected_improvement=ei, best_model_no=best,
                     best_model_point=list(point or BEST_POINT), lowest_lcb_elsewhere=lcb,
                     noise_sd_ppm=noise, matern_length_scales=list(scales))


def campaign(*last, initial=(60.0, 58.0, 55.0)):
    """Three old-style initial tests followed by the given diagnosed trials."""
    return [completed(value) for value in initial] + list(last)


def passing(**changes):
    """Three diagnosed trials that pass every minimise check; ``changes`` alter the last one."""
    return campaign(diagnosed(), diagnosed(), diagnosed(**changes))


def check(result, name):
    return next(item for item in result["checks"] if item["name"] == name)


class MinimiseConvergenceTests(unittest.TestCase):
    def assess(self, trials, **kwargs):
        kwargs.setdefault("min_tests", 6)
        return assess_convergence(config(), trials, **kwargs)

    def assert_only_failure(self, result, name):
        self.assertEqual(result["state"], "not_converged")
        self.assertEqual(result["headline"], "Not converged")
        failed = [item["name"] for item in result["checks"] if not item["passed"]]
        self.assertEqual(failed, [name])
        self.assertEqual(result["detail"], check(result, name)["text"])

    def test_all_checks_passing_is_converged_with_best_estimate_and_observed_minimum(self):
        result = self.assess(passing())
        self.assertEqual(result["state"], "converged")
        self.assertEqual(result["headline"], "Converged")
        self.assertEqual([item["name"] for item in result["checks"]],
                         ["expected_improvement", "no_better_elsewhere", "best_location_stable",
                          "min_tests"])
        self.assertTrue(all(item["passed"] for item in result["checks"]))
        self.assertEqual(result["metric"], "expected_improvement_ppm")
        self.assertEqual(result["threshold"], 1.0)
        self.assertEqual(result["noise_source"], "model fit")
        self.assertIn("The model's best estimate is 50 ppm at H2 fraction 0.4, stage-1 phi 1.3, "
                      "overall phi 0.7", result["detail"])
        self.assertIn("the lowest observed corrected NO is 52 ppm", result["detail"])
        self.assertIn("(smallest improvement that matters)", check(result, "expected_improvement")["text"])

    def test_expected_improvement_fails_on_its_own(self):
        result = self.assess(passing(ei=1.5))
        self.assert_only_failure(result, "expected_improvement")
        self.assertIn("0.5, 0.5, 1.5 ppm; the threshold is 1 ppm", result["detail"])

    def test_better_region_elsewhere_fails_on_its_own(self):
        result = self.assess(passing(lcb=48.9))
        self.assert_only_failure(result, "no_better_elsewhere")
        self.assertIn("In 1 of the last 3 suggestions", result["detail"])
        self.assertIn("lower bound 48.9 ppm against a best estimate of 50 ppm", result["detail"])

    def test_moving_best_location_fails_on_its_own(self):
        moved = [.4 + .06 * .5, 1.3, .7]
        result = self.assess(campaign(diagnosed(point=moved), diagnosed(), diagnosed()))
        self.assert_only_failure(result, "best_location_stable")
        self.assertIn("moved by up to 6% of a variable's range; the tolerance is 5%", result["detail"])

    def test_minimum_tests_fails_on_its_own_and_defaults_to_five_per_variable(self):
        result = self.assess(passing(), min_tests=7)
        self.assert_only_failure(result, "min_tests")
        self.assertEqual(result["detail"], "6 tests completed; convergence needs at least 7.")
        result = assess_convergence(config(), passing())
        self.assert_only_failure(result, "min_tests")
        self.assertIn("at least 15", result["detail"])

    def test_values_exactly_at_each_threshold_pass(self):
        result = self.assess(passing(ei=1.0, lcb=49.0))
        self.assertEqual(result["state"], "converged")
        self.assertEqual(result["threshold"], 1.0)
        moved = [.4 + .05 * .5, 1.3, .7]
        trials = campaign(diagnosed(point=moved), diagnosed(), diagnosed())
        distance = abs(moved[0] - BEST_POINT[0]) / (.65 - .15)
        with patch.object(convergence, "LOCATION_TOLERANCE", distance):
            result = self.assess(trials)
        self.assertTrue(check(result, "best_location_stable")["passed"])
        self.assertEqual(result["state"], "converged")

    def test_no_candidate_far_from_best_point_passes_the_elsewhere_check(self):
        result = self.assess(campaign(diagnosed(lcb=None), diagnosed(lcb=None), diagnosed(lcb=None)))
        self.assertEqual(result["state"], "converged")
        self.assertTrue(check(result, "no_better_elsewhere")["passed"])
        self.assertNotIn("lowest lower bound elsewhere", check(result, "no_better_elsewhere")["text"])

    def test_trials_without_diagnostics_are_skipped(self):
        old_style = [completed(50.0 + index, expected_improvement=.1, mapping_score=None)
                     for index in range(8)]
        result = self.assess(old_style)
        self.assertEqual(result["state"], "insufficient")
        self.assertEqual(result["headline"], "Convergence not yet assessable: 0 of 3 model "
                                             "suggestions with convergence diagnostics")
        self.assertEqual(result["checks"], [])
        self.assertEqual(result["warnings"], [])
        result = self.assess(old_style + [diagnosed(), diagnosed()])
        self.assertEqual(result["state"], "insufficient")
        self.assertIn("2 of 3", result["headline"])
        result = self.assess(old_style + [diagnosed(), diagnosed(ei=float("nan")), diagnosed(),
                                          diagnosed()])
        self.assertEqual(result["state"], "converged")

    def test_initial_design_is_reported_before_any_check(self):
        result = self.assess([diagnosed(), diagnosed()], min_tests=None)
        self.assertEqual(result["state"], "initial_design")
        self.assertEqual(result["detail"], "2 of 4 initial space-filling tests completed.")

    def test_fitted_noise_above_the_smallest_improvement_raises_the_threshold(self):
        result = self.assess(passing(ei=1.5, noise=2.0))
        self.assertEqual(result["state"], "converged")
        self.assertEqual(result["threshold"], 2.0)
        self.assertEqual(result["noise_source"], "model fit")
        self.assertIn("(fitted noise)", check(result, "expected_improvement")["text"])

    def test_replicate_noise_overrides_fitted_noise_and_needs_two_degrees_of_freedom(self):
        repeats = [completed(50.0, "a"), completed(52.0, "a2", "a"),
                   completed(60.0, "b"), completed(63.0, "b2", "b")]
        noise, dof = replicate_noise(repeats)
        self.assertEqual(dof, 2)
        self.assertAlmostEqual(noise, math.sqrt(6.5 / 2))
        trials = repeats + [diagnosed(), diagnosed(), diagnosed(ei=1.5, noise=.1)]
        result = self.assess(trials, min_tests=7)
        self.assertEqual(result["noise_source"], "replicates")
        self.assertAlmostEqual(result["noise_sd_ppm"], math.sqrt(3.25))
        self.assertAlmostEqual(result["threshold"], math.sqrt(3.25))
        self.assertEqual(result["state"], "converged")
        self.assertIn("(measured noise from repeats)", check(result, "expected_improvement")["text"])
        self.assertFalse(any("has not been measured" in text for text in result["warnings"]))
        # Replicates replace a larger fitted noise rather than taking the maximum.
        result = self.assess(repeats + [diagnosed(), diagnosed(), diagnosed(noise=5.0)], min_tests=7)
        self.assertAlmostEqual(result["threshold"], math.sqrt(3.25))
        # A repeat of a repeat joins the original group.
        chained = repeats[:2] + [completed(51.0, "a3", "a2")]
        self.assertEqual(replicate_noise(chained)[1], 2)

        pair = [completed(50.0, "a"), completed(52.0, "a2", "a")]
        self.assertEqual(replicate_noise(pair)[1], 1)
        result = self.assess(pair + [completed(60.0), diagnosed(), diagnosed(), diagnosed(ei=1.5)])
        self.assertEqual(result["noise_source"], "model fit")
        self.assertEqual(result["threshold"], 1.0)
        self.assert_only_failure(result, "expected_improvement")

    def test_each_warning(self):
        result = self.assess(passing())
        self.assertEqual(result["warnings"], [
            "Measurement noise has not been measured: repeat one completed test 3 to 5 times.",
            "Before reporting, repeat the best test 2 to 3 times and run a few tests away from it "
            "to check the model's predictions."])
        lower = [.15 + .01 * .5, 1.3, .85]
        result = self.assess(campaign(*(diagnosed(point=lower) for _ in range(3))))
        self.assertIn("The best point sits at the lower limit of H2 fraction; the true minimum "
                      "may lie outside the search bounds.", result["warnings"])
        self.assertIn("The best point sits at the upper limit of overall phi; the true minimum "
                      "may lie outside the search bounds.", result["warnings"])
        inside = [.15 + .03 * .5, 1.3, .7]
        result = self.assess(campaign(*(diagnosed(point=inside) for _ in range(3))))
        self.assertFalse(any("limit of" in text for text in result["warnings"]))
        result = self.assess(passing(scales=(.3, .999 * LENGTH_SCALE_BOUNDS[1], .3)))
        self.assertIn("The model currently treats stage-1 phi as having almost no effect, so its "
                      "uncertainty there may be too small.", result["warnings"])
        self.assertEqual(sum("almost no effect" in text for text in result["warnings"]), 1)
        result = self.assess(passing(ei=1.5))
        self.assertEqual(result["state"], "not_converged")
        self.assertFalse(any(text.startswith("Before reporting") for text in result["warnings"]))

    def test_settings_are_validated(self):
        trials = passing()
        for kwargs in ({"min_improvement_ppm": 0}, {"min_improvement_ppm": 5000.5},
                       {"min_improvement_ppm": float("nan")}, {"gain_fraction": 0},
                       {"gain_fraction": 1.01}, {"consecutive": 0}, {"consecutive": 21},
                       {"consecutive": 2.0}, {"consecutive": True}, {"min_tests": 3},
                       {"min_tests": 501}, {"min_tests": 6.0}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    assess_convergence(config(), trials, **kwargs)
        result = assess_convergence(config(), trials, min_improvement_ppm=5000, gain_fraction=1,
                                    consecutive=1, min_tests=4)
        self.assertEqual(result["state"], "converged")


class MappingConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.settings = config(objective_mode="map_no_pressure")

    def trials(self, *scores):
        return [completed(50.0) for _ in range(3)] + [completed(50.0, mapping_score=score)
                                                       for score in scores]

    def test_gain_and_minimum_tests_checks(self):
        result = assess_convergence(self.settings, self.trials(.01, .02, .015), min_tests=6)
        self.assertEqual(result["state"], "converged")
        self.assertEqual([item["name"] for item in result["checks"]], ["gain", "min_tests"])
        self.assertEqual(result["metric"], "mapping_score")
        self.assertEqual(result["threshold"], .02)
        self.assertEqual(result["warnings"], [])
        result = assess_convergence(self.settings, self.trials(.01, .03, .015), min_tests=6)
        self.assertEqual(result["state"], "not_converged")
        self.assertEqual([item["name"] for item in result["checks"] if not item["passed"]], ["gain"])
        self.assertIn("1%, 3%, 1.5%", result["detail"])
        result = assess_convergence(self.settings, self.trials(.01, .02, .015), min_tests=7)
        self.assertEqual([item["name"] for item in result["checks"] if not item["passed"]],
                         ["min_tests"])
        result = assess_convergence(self.settings, self.trials(.01, .03), min_tests=6,
                                    gain_fraction=.05, consecutive=2)
        self.assertEqual(result["state"], "not_converged")
        self.assertFalse(check(result, "min_tests")["passed"])
        result = assess_convergence(self.settings, self.trials(.01, .02), min_tests=5)
        self.assertEqual(result["state"], "insufficient")
        self.assertIn("2 of 3 model suggestions", result["headline"])


class SuggestDiagnosticsTests(unittest.TestCase):
    def test_minimise_suggestion_reports_json_safe_deterministic_diagnostics(self):
        for settings, points in (
                (config(), [[.2, 1.1, .55], [.6, 1.5, .8], [.4, 1.3, .7], [.3, 1.2, .6],
                            [.5, 1.4, .65], [.25, 1.45, .75]]),
                (extended_config(), [[.2, 1.1, .55, 9, .8], [.6, 1.5, .8, 11, .9],
                                     [.4, 1.3, .7, 10, .85], [.3, 1.2, .6, 8.5, .95],
                                     [.5, 1.4, .65, 11.5, .78], [.25, 1.45, .75, 10.5, .88],
                                     [.45, 1.25, .62, 9.5, .82]])):
            with self.subTest(dimensions=settings.dimensions):
                trials = [synthetic_trial(settings, point,
                                          40 + 3000 * (point[0] - .35) ** 2 + 5 * point[1])
                          for point in points]
                first = suggest(settings, trials)
                second = suggest(settings, trials)
                self.assertEqual(json.dumps(first, allow_nan=False, sort_keys=True),
                                 json.dumps(second, allow_nan=False, sort_keys=True))
                self.assertEqual(first["algorithm_version"], "fcbo-matern52-nei-v1")
                for key in ("best_model_no", "best_model_point", "lowest_lcb_elsewhere",
                            "lcb_multiplier", "elsewhere_radius", "noise_sd_ppm"):
                    self.assertIn(key, first)
                self.assertEqual(first["lcb_multiplier"], LCB_MULTIPLIER)
                self.assertEqual(first["elsewhere_radius"], ELSEWHERE_RADIUS)
                self.assertTrue(math.isfinite(first["best_model_no"]))
                self.assertGreater(first["noise_sd_ppm"], 0)
                self.assertTrue(first["lowest_lcb_elsewhere"] is None
                                or math.isfinite(first["lowest_lcb_elsewhere"]))
                observed = np.array([settings.observed_vector(trial["window"]) for trial in trials])
                distances = np.max(np.abs(observed - np.array(first["best_model_point"])), axis=1)
                self.assertLess(distances.min(), 1e-9)
                # The optimiser works in log space, so a scale at the bound can read 3.0000000000000004.
                self.assertTrue(all(scale <= LENGTH_SCALE_BOUNDS[1] * (1 + 1e-9)
                                    for scale in first["matern_length_scales"]))
                # The stored suggestion feeds the readout without further conversion.
                assessment = assess_convergence(settings, trials + [dict(first, status="pending")],
                                                consecutive=1)
                self.assertIn(assessment["state"], ("converged", "not_converged"))


class ExperimentPlanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = config()

    def path(self, name="plan.fcbo.json"):
        return Path(self.directory.name) / name

    def test_default_and_custom_plans_are_saved_and_reloaded(self):
        experiment = Experiment.create(self.path(), self.settings)
        expected = {"planned_tests": None, "min_tests": 15, "min_improvement_ppm": 1.0,
                    "gain_fraction": .02, "consecutive": 3, "extensions": []}
        self.assertEqual(experiment.plan, expected)
        self.assertEqual(default_plan(self.settings), expected)
        stored = json.loads(self.path().read_text(encoding="utf-8"))
        self.assertEqual(stored["plan"], expected)
        experiment = Experiment.create(self.path("custom.fcbo.json"), self.settings, planned_tests=30,
                                       min_tests=20, min_improvement_ppm=2.5, gain_fraction=.05,
                                       consecutive=4)
        reopened = Experiment.load(experiment.path)
        self.assertEqual(reopened.plan, {"planned_tests": 30, "min_tests": 20,
                                         "min_improvement_ppm": 2.5, "gain_fraction": .05,
                                         "consecutive": 4, "extensions": []})
        self.assertEqual(reopened.convergence()["state"], "initial_design")

    def test_invalid_plans_are_refused_at_creation(self):
        for kwargs, message in (
                ({"planned_tests": 3}, "from 4"), ({"planned_tests": 501}, "to 500"),
                ({"planned_tests": True}, "whole number"), ({"planned_tests": 10}, "cannot be fewer"),
                ({"min_tests": 3}, "Minimum completed tests"), ({"min_improvement_ppm": -1}, "above 0"),
                ({"consecutive": 21}, "1 to 20"), ({"gain_fraction": 2}, "100%")):
            with self.subTest(**kwargs):
                with self.assertRaisesRegex(ValueError, message):
                    Experiment.create(self.path(), self.settings, **kwargs)
                self.assertFalse(self.path().exists())

    def test_malformed_saved_plans_are_refused_on_load(self):
        Experiment.create(self.path(), self.settings, planned_tests=20)
        original = json.loads(self.path().read_text(encoding="utf-8"))
        record = {"at": "2026-01-01T00:00:00+00:00", "from": 20, "to": 25, "completed": 20,
                  "state": "not_converged", "detail": ""}
        for change in (
                lambda plan: plan.update(extra=1), lambda plan: plan.pop("min_tests"),
                lambda plan: plan.update(min_tests=None), lambda plan: plan.update(planned_tests=True),
                lambda plan: plan.update(planned_tests=14), lambda plan: plan.update(consecutive=0),
                lambda plan: plan.update(min_improvement_ppm=0), lambda plan: plan.update(extensions={}),
                lambda plan: plan.update(extensions=[{"to": 25}]),
                lambda plan: plan.update(extensions=[dict(record, to=0)]),
                lambda plan: plan.update(extensions=[dict(record, detail="x" * (MAX_EXTENSION_DETAIL + 1))]),
                lambda plan: plan.update(extensions=[record] * 101)):
            data = json.loads(json.dumps(original))
            change(data["plan"])
            self.path().write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                Experiment.load(self.path())
        data = json.loads(json.dumps(original))
        data["plan"]["extensions"] = [record]
        self.path().write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(Experiment.load(self.path()).plan["extensions"], [record])
        del data["plan"]
        self.path().write_text(json.dumps(data), encoding="utf-8")
        legacy = Experiment.load(self.path())
        self.assertEqual(legacy.plan, default_plan(self.settings))
        self.assertFalse(legacy.plan_reached)

    def test_validate_plan_requires_planned_tests_at_least_minimum(self):
        plan = dict(default_plan(self.settings), planned_tests=15)
        validate_plan(plan, self.settings)
        with self.assertRaisesRegex(ValueError, r"Planned completed tests \(14\) cannot be fewer than "
                                                r"the minimum completed tests before convergence \(15\)"):
            validate_plan(dict(plan, planned_tests=14), self.settings)

    def test_extend_plan_records_each_extension(self):
        experiment = Experiment.create(self.path(), self.settings, planned_tests=4, min_tests=4)
        experiment.data["trials"] = [completed(50.0 + index) for index in range(5)]
        self.assertTrue(experiment.plan_reached)
        self.assertEqual(experiment.extend_plan(3), 8)
        record = experiment.plan["extensions"][-1]
        self.assertEqual({key: record[key] for key in ("from", "to", "completed", "state")},
                         {"from": 4, "to": 8, "completed": 5, "state": "insufficient"})
        self.assertFalse(experiment.plan_reached)
        for bad in (0, -1, 2.0, True):
            with self.assertRaisesRegex(ValueError, "at least 1"):
                experiment.extend_plan(bad)
        with self.assertRaisesRegex(ValueError, "500-test limit"):
            experiment.extend_plan(493)
        stored = json.loads(self.path().read_text(encoding="utf-8"))["plan"]
        self.assertEqual(stored["planned_tests"], 8)
        self.assertEqual(len(stored["extensions"]), 1)

        open_ended = Experiment.create(self.path("open.fcbo.json"), self.settings, min_tests=4)
        self.assertEqual(open_ended.extend_plan(5), 5)
        self.assertIsNone(open_ended.plan["extensions"][0]["from"])
        self.assertEqual(Experiment.load(open_ended.path).planned_tests, 5)
        default = Experiment.create(self.path("default.fcbo.json"), self.settings)
        with self.assertRaisesRegex(ValueError, "cannot be fewer"):
            default.extend_plan(5)
        self.assertIsNone(Experiment.load(default.path).planned_tests)


class PlanControllerTests(unittest.TestCase):
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
        self.settings = config()
        self.addCleanup(self.cleanup_session)

    def cleanup_session(self):
        self.controller.worker = None
        self.controller.shutdown()
        self.session.is_monitoring = False
        self.session.controllers_connected = False
        self.session.shutdown()

    def create(self, name="plan.fcbo.json", **kwargs):
        self.controller.create(Path(self.directory.name) / name, self.settings, **kwargs)
        return self.controller.experiment

    def fill(self, experiment, count):
        points = ([.2, 1.1, .55], [.6, 1.5, .8], [.4, 1.3, .7], [.3, 1.2, .6], [.5, 1.4, .65])
        experiment.data["trials"] = [
            dict(synthetic_trial(self.settings, points[index], 50.0 + index), id=f"t{index}",
                 number=index + 1, method="test") for index in range(count)]

    def test_create_passes_every_plan_setting(self):
        experiment = self.create(planned_tests=12, min_tests=6, min_improvement_ppm=3.0,
                                 gain_fraction=.04, consecutive=5)
        self.assertEqual(experiment.plan, {"planned_tests": 12, "min_tests": 6,
                                           "min_improvement_ppm": 3.0, "gain_fraction": .04,
                                           "consecutive": 5, "extensions": []})

    def test_ask_stops_at_the_plan_while_repeats_and_extension_continue(self):
        experiment = self.create(planned_tests=4, min_tests=4)
        self.fill(experiment, 4)
        with self.assertRaisesRegex(ValueError, r"The planned 4 tests are complete \(convergence not "
                                                r"yet assessable: .*\)\. Extend the plan to continue\."):
            self.controller.ask()
        self.assertIsNone(self.controller.worker)
        self.controller.repeat("t1")
        self.assertEqual(experiment.pending["repeat_of"], "t1")
        self.assertEqual(self.controller.extend_plan(2), 6)
        self.assertEqual(self.controller.last_message, "Plan extended to 6 completed tests. No flows changed.")
        self.controller.invalidate("not needed")
        with patch("flow_controller.core.optimiser_controller.SuggestionWorker", MagicMock()) as worker:
            self.controller.ask()
        worker.return_value.start.assert_called_once_with()
        self.assertTrue(self.controller.busy)
        with self.assertRaisesRegex(ValueError, "Wait for the background calculation"):
            self.controller.extend_plan(1)
        self.assertEqual(self.controller.experiment.planned_tests, 6)
        self.controller.worker = None

    def test_pane_reports_plan_convergence_and_notes(self):
        experiment = self.create(planned_tests=4, min_tests=4)
        pane = OptimiserPane(self.controller)
        self.addCleanup(pane.close)
        self.assertTrue(pane.plan_status.wordWrap())
        self.assertEqual(pane.plan_status.text(),
                         "Plan: 0 of 4 tests completed · Convergence not assessed: initial design "
                         "in progress.\n0 of 4 initial space-filling tests completed.")
        self.assertEqual(pane.extend_button.text(), "Extend plan")
        self.assertTrue(pane.ask_button.isEnabled())
        self.fill(experiment, 4)
        for index in range(1, 4):
            experiment.data["trials"][index].update(
                expected_improvement=.2, best_model_no=50.0, best_model_point=[.4, 1.3, .7],
                lowest_lcb_elsewhere=49.5, noise_sd_ppm=.1, matern_length_scales=[.3, .3, .3])
        pane.refresh()
        lines = pane.plan_status.text().split("\n")
        self.assertEqual(lines[0], "Plan: 4 of 4 tests completed · Converged. Extend the plan to "
                                   "suggest more tests.")
        self.assertTrue(lines[1].startswith("The model's best estimate is 50 ppm at H2 fraction 0.4"))
        self.assertEqual(lines[2:], [
            "Note: Measurement noise has not been measured: repeat one completed test 3 to 5 times.",
            "Note: Before reporting, repeat the best test 2 to 3 times and run a few tests away "
            "from it to check the model's predictions."])
        self.assertFalse(pane.ask_button.isEnabled())
        self.assertIn("Extend the plan", pane.ask_button.toolTip())
        self.assertTrue(pane.extend_button.isEnabled())
        pane.extend_count.setValue(3)
        pane.extend_button.click()
        self.assertEqual(experiment.planned_tests, 7)
        self.assertTrue(pane.ask_button.isEnabled())
        self.assertEqual(pane.ask_button.toolTip(), "")
        self.assertTrue(pane.plan_status.text().startswith("Plan: 4 of 7 tests completed · Converged."))
        self.controller.worker = object()
        pane.refresh()
        self.assertFalse(pane.extend_button.isEnabled())
        self.assertFalse(pane.extend_count.isEnabled())
        self.controller.worker = None

    def test_open_ended_pane_offers_to_set_a_plan(self):
        self.create(min_tests=4)
        pane = OptimiserPane(self.controller)
        self.addCleanup(pane.close)
        self.assertTrue(pane.plan_status.text().startswith("No planned test count: 0 tests completed · "))
        self.assertEqual(pane.extend_button.text(), "Set plan")
        pane.extend_count.setValue(6)
        pane.extend_button.click()
        self.assertEqual(self.controller.experiment.planned_tests, 6)
        self.assertEqual(pane.extend_button.text(), "Extend plan")

    def test_new_experiment_passes_dialog_plan_to_the_campaign(self):
        pane = OptimiserPane(self.controller)
        self.addCleanup(pane.close)
        path = Path(self.directory.name) / "new.fcbo.json"

        def accept_dialog(dialog):
            fill_dialog(dialog)
            dialog.entries["planned"].setText("24")
            dialog.entries["min_tests"].setText("12")
            dialog.entries["min_improvement"].setText("2.5")
            dialog.entries["consecutive"].setText("4")
            dialog.accept()
            return dialog.result()

        with patch.object(ExperimentDialog, "exec", accept_dialog), \
                patch("flow_controller.ui.qt_optimiser.QFileDialog.getSaveFileName",
                      return_value=(str(path), "")):
            pane._new()
        self.assertEqual(Experiment.load(path).plan, {
            "planned_tests": 24, "min_tests": 12, "min_improvement_ppm": 2.5,
            "gain_fraction": .02, "consecutive": 4, "extensions": []})


def fill_dialog(dialog):
    for pair, values in zip(dialog.bounds, ((15, 65), (1.05, 1.6), (.5, .85))):
        for entry, value in zip(pair, values):
            entry.setText(str(value))
    dialog.entries["power"].setText("10")
    dialog.entries["split"].setText("100")
    dialog.approved.setChecked(True)


class PlanDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def dialog(self, request=None):
        dialog = ExperimentDialog(request)
        self.addCleanup(dialog.close)
        return dialog

    def test_defaults_labels_and_mode_dependent_fields(self):
        dialog = self.dialog()
        expected = {"planned": "30", "min_tests": "15", "min_improvement": "1", "gain": "2",
                    "consecutive": "3"}
        self.assertEqual({key: dialog.entries[key].text() for key in expected}, expected)
        self.assertEqual(dialog.entries["planned"].accessibleName(), "Planned completed tests (blank = no plan)")
        self.assertEqual(dialog.entries["min_tests"].accessibleName(),
                         "Minimum completed tests before convergence")
        self.assertEqual(dialog.entries["min_improvement"].accessibleName(),
                         "Smallest NO improvement that matters (ppm)")
        self.assertEqual(dialog.entries["gain"].accessibleName(), "Mapping convergence threshold (%)")
        self.assertEqual(dialog.entries["consecutive"].accessibleName(),
                         "Consecutive suggestions meeting the criteria")
        self.assertIn("3 to 5 is recommended", dialog.entries["consecutive"].toolTip())
        self.assertTrue(dialog.entries["min_improvement"].isEnabled())
        self.assertFalse(dialog.entries["gain"].isEnabled())
        dialog.objective_mode.setCurrentIndex(dialog.objective_mode.findData("map_no_pressure"))
        self.assertFalse(dialog.entries["min_improvement"].isEnabled())
        self.assertTrue(dialog.entries["gain"].isEnabled())
        mapped = self.dialog(replace(config(), objective_mode="map_no_pressure"))
        self.assertTrue(mapped.entries["gain"].isEnabled())
        self.assertFalse(mapped.entries["min_improvement"].isEnabled())

    def test_plan_defaults_follow_searched_variables_until_edited(self):
        dialog = self.dialog()
        power = dialog.optional["power_kw"][0]
        split = dialog.optional["split_rich"][0]
        split.setChecked(True)
        self.assertEqual((dialog.entries["planned"].text(), dialog.entries["min_tests"].text()),
                         ("40", "20"))
        power.setChecked(True)
        self.assertEqual((dialog.entries["planned"].text(), dialog.entries["min_tests"].text()),
                         ("50", "25"))
        dialog.entries["min_tests"].setText("12")
        power.setChecked(False)
        self.assertEqual((dialog.entries["planned"].text(), dialog.entries["min_tests"].text()),
                         ("40", "12"))

    def test_accept_parses_and_validates_the_plan(self):
        dialog = self.dialog()
        fill_dialog(dialog)
        dialog.entries["planned"].setText("")
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual((dialog.planned_tests, dialog.min_tests, dialog.min_improvement_ppm,
                          dialog.gain_fraction, dialog.consecutive), (None, 15, 1.0, .02, 3))
        for key, text, message in (
                ("planned", "3", "Planned completed tests must be a whole number from 4"),
                ("planned", "10", "cannot be fewer than the minimum completed tests"),
                ("planned", "20.5", "Planned completed tests must be a whole number."),
                ("min_tests", "2", "Minimum completed tests before convergence must be a whole number from 4"),
                ("min_improvement", "0", "Smallest NO improvement that matters must be above 0"),
                ("gain", "150", "Mapping convergence threshold must be above 0%"),
                ("consecutive", "2.5", "Consecutive suggestions meeting the criteria must be a whole number."),
                ("consecutive", "25", "from 1 to 20")):
            with self.subTest(key=key, text=text):
                dialog = self.dialog()
                fill_dialog(dialog)
                dialog.entries[key].setText(text)
                dialog.accept()
                self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
                self.assertIn(message, dialog.error.text())


if __name__ == "__main__":
    unittest.main()
