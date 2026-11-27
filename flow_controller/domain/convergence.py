"""Campaign stopping guide from what the model reported at each suggestion.

No method can prove that a global minimum has been found from a finite number
of tests. A "converged" result here means: this is the best point found, and,
given the tests run so far and a model whose own uncertainty is trusted, there
is no evidence of a better region. Every check uses what the model reported
when each test was suggested (stored with the trial); nothing is refitted.

Minimise NO needs four signs at once, each held for the last N model
suggestions: (1) small expected improvement against an absolute ppm threshold
set by the smallest improvement that matters or the measurement noise,
whichever is larger; (2) no region away from the model's best point whose
lower confidence bound is better than the best estimate by more than that
threshold; (3) a stable best-point location; (4) a minimum number of completed
tests. Mapping needs (1) small reductions in average map uncertainty over the
last N suggestions and (2) the minimum number of completed tests.

Pure: no Qt, no file I/O and no model fitting.
"""

from __future__ import annotations

import math

from .bayesian import LENGTH_SCALE_BOUNDS, VARIABLE_LABELS


DEFAULT_MIN_IMPROVEMENT_PPM = 1.0
DEFAULT_GAIN_FRACTION = 0.02
DEFAULT_CONSECUTIVE = 3
MAX_CONSECUTIVE = 20
MAX_MIN_IMPROVEMENT_PPM = 5000.0
MAX_MIN_TESTS = 500
MIN_TESTS_PER_VARIABLE = 5
# Best-point stability: Chebyshev distance as a fraction of each variable's range.
LOCATION_TOLERANCE = 0.05
# Bound warning: best point within this fraction of a variable's range of a limit.
BOUND_TOLERANCE = 0.02
MINIMISE_KEYS = ("expected_improvement", "best_model_no", "best_model_point", "lowest_lcb_elsewhere")


def default_min_tests(config):
    """Default minimum completed tests: 5 per variable, never below the initial design."""
    return max(MIN_TESTS_PER_VARIABLE * config.dimensions, config.initial_points)


def _number(value, message):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(message)
    return value


def validate_convergence_settings(config, *, min_improvement_ppm=DEFAULT_MIN_IMPROVEMENT_PPM,
                                  gain_fraction=DEFAULT_GAIN_FRACTION,
                                  consecutive=DEFAULT_CONSECUTIVE, min_tests=None):
    """Return the four cleaned settings as a dict, or raise ValueError.

    ``min_tests`` None resolves to :func:`default_min_tests`.
    """
    improvement = _number(min_improvement_ppm, "Smallest NO improvement that matters must be a number.")
    if not 0 < improvement <= MAX_MIN_IMPROVEMENT_PPM:
        raise ValueError(
            f"Smallest NO improvement that matters must be above 0 and at most {MAX_MIN_IMPROVEMENT_PPM:g} ppm.")
    gain = _number(gain_fraction, "Mapping convergence threshold must be a number.")
    if not 0 < gain <= 1:
        raise ValueError("Mapping convergence threshold must be above 0% and at most 100%.")
    if type(consecutive) is not int or not 1 <= consecutive <= MAX_CONSECUTIVE:
        raise ValueError(
            f"Consecutive suggestions meeting the criteria must be a whole number from 1 to {MAX_CONSECUTIVE}.")
    if min_tests is None:
        min_tests = default_min_tests(config)
    if type(min_tests) is not int or not config.initial_points <= min_tests <= MAX_MIN_TESTS:
        raise ValueError(
            f"Minimum completed tests before convergence must be a whole number from "
            f"{config.initial_points} (the initial space-filling points) to {MAX_MIN_TESTS}.")
    return {"min_improvement_ppm": improvement, "gain_fraction": gain,
            "consecutive": consecutive, "min_tests": min_tests}


def _gain(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _point(value, dimensions):
    if not isinstance(value, (list, tuple)) or len(value) != dimensions:
        return None
    values = [_finite(item) for item in value]
    return None if any(item is None for item in values) else values


def _suggestions(count):
    return "suggestion" if count == 1 else f"{count} suggestions"


def _last(count):
    return "the last suggestion" if count == 1 else f"the last {count} suggestions"


def _ppm(values):
    return ", ".join(f"{value:.3g}" for value in values)


def replicate_noise(trials):
    """Pooled SD (ppm) of corrected NO over repeat groups, and its degrees of freedom.

    A group is an original completed test plus its completed repeats (``repeat_of``
    is followed back to the original). Returns ``(None, 0)`` with no replicates.
    """
    by_id = {trial.get("id"): trial for trial in trials if trial.get("id")}

    def root(trial):
        seen = set()
        while trial.get("repeat_of") in by_id and trial.get("id") not in seen:
            seen.add(trial.get("id"))
            trial = by_id[trial["repeat_of"]]
        return trial.get("id") or id(trial)

    groups = {}
    for trial in trials:
        if trial.get("status") != "completed":
            continue
        value = _finite((trial.get("result") or {}).get("corrected_no"))
        if value is not None:
            groups.setdefault(root(trial), []).append(value)
    squares, dof = 0.0, 0
    for values in groups.values():
        if len(values) >= 2:
            mean = sum(values) / len(values)
            squares += sum((value - mean) ** 2 for value in values)
            dof += len(values) - 1
    return (math.sqrt(squares / dof) if dof else None), dof


def _check(name, passed, text):
    return {"name": name, "passed": bool(passed), "text": text}


def _min_tests_check(completed, min_tests):
    passed = completed >= min_tests
    return _check("min_tests", passed,
                  f"{completed} tests completed; convergence needs at least {min_tests}.")


def _labels(config):
    return [VARIABLE_LABELS[name] for name in config.variable_names]


def assess_convergence(config, trials, *, min_improvement_ppm=DEFAULT_MIN_IMPROVEMENT_PPM,
                       gain_fraction=DEFAULT_GAIN_FRACTION, consecutive=DEFAULT_CONSECUTIVE,
                       min_tests=None):
    """Assess the campaign against the convergence checks; a stopping guide only.

    No method can prove a global minimum from finite tests. "converged" means the
    best point found, with no evidence of a better region given this coverage and a
    model whose own uncertainty is trusted. The checks use what the model reported
    when each test was suggested, not a refit.

    Returns a dict with ``state`` ("initial_design", "insufficient", "not_converged"
    or "converged"), ``headline``, ``detail``, ``metric``, ``checks`` (a list of
    ``{"name", "passed", "text"}``), ``warnings`` (plain sentences that never change
    the state), ``threshold``, ``noise_sd_ppm`` and ``noise_source``.
    """
    settings = validate_convergence_settings(
        config, min_improvement_ppm=min_improvement_ppm, gain_fraction=gain_fraction,
        consecutive=consecutive, min_tests=min_tests)
    consecutive, min_tests = settings["consecutive"], settings["min_tests"]
    mapping = config.objective_mode == "map_no_pressure"
    completed = [trial for trial in trials if trial.get("status") == "completed"]
    result = {"state": None, "headline": "", "detail": "",
              "metric": "mapping_score" if mapping else "expected_improvement_ppm",
              "checks": [], "warnings": [], "threshold": None,
              "noise_sd_ppm": None, "noise_source": None}
    if len(completed) < config.initial_points:
        result.update(
            state="initial_design",
            headline="Convergence not assessed: initial design in progress",
            detail=(f"{len(completed)} of {config.initial_points} initial space-filling "
                    "tests completed."))
        return result
    if mapping:
        _assess_mapping(result, trials, completed, settings["gain_fraction"], consecutive, min_tests)
    else:
        _assess_minimise(result, config, trials, completed, settings["min_improvement_ppm"],
                         consecutive, min_tests)
    return result


def _insufficient(result, found, consecutive, what):
    result.update(
        state="insufficient",
        headline=f"Convergence not yet assessable: {found} of {consecutive} {what}",
        detail=f"Convergence is judged on the last {_suggestions(consecutive)} from the model.")


def _finish(result, checks):
    converged = all(check["passed"] for check in checks)
    result.update(checks=checks, state="converged" if converged else "not_converged",
                  headline="Converged" if converged else "Not converged")
    if not converged:
        result["detail"] = " ".join(check["text"] for check in checks if not check["passed"])
    return converged


def _assess_mapping(result, trials, completed, gain_fraction, consecutive, min_tests):
    gains = [gain for gain in (_gain(trial.get("mapping_score")) for trial in trials
                               if "mapping_score" in trial) if gain is not None]
    if len(gains) < consecutive:
        _insufficient(result, len(gains), consecutive, "model suggestions")
        return
    values = gains[-consecutive:]
    shown = ", ".join(f"{value * 100:.3g}%" for value in values)
    lead = ("The last suggestion would reduce" if consecutive == 1 else
            f"The last {consecutive} suggestions would each reduce")
    gain = _check("gain", all(value <= gain_fraction for value in values),
                  f"{lead} average map uncertainty by {shown}; "
                  f"the threshold is {gain_fraction * 100:.3g}%.")
    result["threshold"] = gain_fraction
    checks = [gain, _min_tests_check(len(completed), min_tests)]
    if _finish(result, checks):
        result["detail"] = " ".join(check["text"] for check in checks)


def _assess_minimise(result, config, trials, completed, min_improvement, consecutive, min_tests):
    dimensions = config.dimensions
    considered = []
    for trial in trials:
        if not all(key in trial for key in MINIMISE_KEYS):
            continue
        improvement = _gain(trial["expected_improvement"])
        best = _finite(trial["best_model_no"])
        point = _point(trial["best_model_point"], dimensions)
        lcb = trial["lowest_lcb_elsewhere"]
        if lcb is not None:
            lcb = _finite(lcb)
            if lcb is None:
                continue
        if improvement is None or best is None or point is None:
            continue
        considered.append({"trial": trial, "improvement": improvement, "best": best,
                           "point": point, "lcb": lcb})
    if len(considered) < consecutive:
        _insufficient(result, len(considered), consecutive,
                      "model suggestions with convergence diagnostics")
        return
    recent = considered[-consecutive:]
    latest = recent[-1]
    noise, dof = replicate_noise(trials)
    if noise is not None and dof >= 2:
        source = "replicates"
    else:
        noise = _gain(latest["trial"].get("noise_sd_ppm"))
        source = "model fit" if noise is not None else None
    threshold = max(min_improvement, noise or 0.0)
    if noise is not None and noise > min_improvement:
        reason = "measured noise from repeats" if source == "replicates" else "fitted noise"
    else:
        reason = "smallest improvement that matters"
    result.update(threshold=threshold, noise_sd_ppm=noise, noise_source=source)
    last = _last(consecutive)

    improvements = [item["improvement"] for item in recent]
    lead = "Expected improvement of the last suggestion was" if consecutive == 1 else \
        f"Expected improvement of the last {consecutive} suggestions was"
    improvement_check = _check(
        "expected_improvement", all(value <= threshold for value in improvements),
        f"{lead} {_ppm(improvements)} ppm; the threshold is {threshold:.3g} ppm ({reason}).")

    better = [item for item in recent
              if item["lcb"] is not None and item["lcb"] < item["best"] - threshold]
    if better:
        worst = min(better, key=lambda item: item["lcb"] - item["best"])
        text = (f"In {len(better)} of {last} the model allowed a region away from its best point "
                f"to be more than {threshold:.3g} ppm better (lower bound {worst['lcb']:.3g} ppm "
                f"against a best estimate of {worst['best']:.3g} ppm).")
    else:
        lows = [item["lcb"] for item in recent if item["lcb"] is not None]
        text = (f"In {last} no region away from the model's best point had a lower bound more "
                f"than {threshold:.3g} ppm below the best estimate"
                + (f" (lowest lower bound elsewhere {min(lows):.3g} ppm)." if lows else "."))
    elsewhere_check = _check("no_better_elsewhere", not better, text)

    ranges = [upper - lower for lower, upper in config.bounds]
    moved = max(max(abs(value - reference) / span
                    for value, reference, span in zip(item["point"], latest["point"], ranges))
                for item in recent)
    stable_check = _check(
        "best_location_stable", moved <= LOCATION_TOLERANCE,
        f"Over {last} the model's best point moved by up to {moved * 100:.3g}% of a variable's "
        f"range; the tolerance is {LOCATION_TOLERANCE * 100:.3g}%.")

    checks = [improvement_check, elsewhere_check, stable_check,
              _min_tests_check(len(completed), min_tests)]
    converged = _finish(result, checks)
    labels = _labels(config)
    if converged:
        where = ", ".join(f"{label} {value:.4g}" for label, value in zip(labels, latest["point"]))
        observed = min(float(trial["result"]["corrected_no"]) for trial in completed)
        result["detail"] = (f"The model's best estimate is {latest['best']:.3g} ppm at {where}; "
                            f"the lowest observed corrected NO is {observed:.3g} ppm.")

    warnings = result["warnings"]
    for label, value, (lower, upper), span in zip(labels, latest["point"], config.bounds, ranges):
        if (value - lower) / span <= BOUND_TOLERANCE:
            warnings.append(f"The best point sits at the lower limit of {label}; the true minimum "
                            "may lie outside the search bounds.")
        elif (upper - value) / span <= BOUND_TOLERANCE:
            warnings.append(f"The best point sits at the upper limit of {label}; the true minimum "
                            "may lie outside the search bounds.")
    scales = latest["trial"].get("matern_length_scales")
    if isinstance(scales, (list, tuple)) and len(scales) == len(labels):
        for label, scale in zip(labels, scales):
            scale = _finite(scale)
            if scale is not None and scale >= .999 * LENGTH_SCALE_BOUNDS[1]:
                warnings.append(f"The model currently treats {label} as having almost no effect, "
                                "so its uncertainty there may be too small.")
    if source != "replicates":
        warnings.append("Measurement noise has not been measured: repeat one completed test 3 to 5 times.")
    if converged:
        warnings.append("Before reporting, repeat the best test 2 to 3 times and run a few tests "
                        "away from it to check the model's predictions.")
