"""One-point Bayesian design for the pilot-off NH3/H2 experiment.

No Qt, persistence, serial I/O or actuation. NO optimisation integrates over
uncertain latent baseline values (Monte Carlo noisy expected improvement).
Joint NO/pressure mapping instead reduces integrated latent response variance.
Parameter bounds and flow ceilings are known constraints, not learned safety.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import warnings

import numpy as np

from .gas_properties import O2_CORRECTION_AIR_PERCENT
from .rql import AutoCalcRequest, auto_calc


AIR_O2 = O2_CORRECTION_AIR_PERCENT  # compatibility name: reporting convention
BASE_VARIABLES = ("h2_fraction", "phi_stage1", "phi_overall")
OPTIONAL_VARIABLES = ("power_kw", "split_rich")
VARIABLE_LABELS = {
    "h2_fraction": "H2 fraction",
    "phi_stage1": "stage-1 phi",
    "phi_overall": "overall phi",
    "power_kw": "thermal input",
    "split_rich": "stage-1 fuel split",
}
# Public compatibility name used by older callers and documentation.
PARAMETERS = tuple(VARIABLE_LABELS[key] for key in BASE_VARIABLES)


def finite(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number.")
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be a finite number.") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number.")
    return number


@dataclass(frozen=True)
class SearchConfig:
    power_kw: float
    bounds: tuple[tuple[float, float], ...]
    split_rich: float = 1.0
    reference_o2: float = 15.0
    initial_points: int | None = None
    window_seconds: float = 30.0
    optimise_power: bool = False
    optimise_split: bool = False
    candidate_pool_size: int | None = None
    objective_mode: str = "minimise_no"
    pressure_metric: str = "rms_pa"
    mapping_no_weight: float = 0.5

    def __post_init__(self):
        if self.objective_mode not in ("minimise_no", "map_no_pressure"):
            raise ValueError("Objective mode must be minimise_no or map_no_pressure.")
        if self.pressure_metric not in ("rms_pa", "peak_abs_pa", "dominant_amplitude_pa"):
            raise ValueError("Pressure metric must be rms_pa, peak_abs_pa or dominant_amplitude_pa.")
        weight = finite(self.mapping_no_weight, "Mapping NO weight")
        if not 0 < weight < 1:
            raise ValueError("Mapping NO weight must be strictly between 0 and 1 so both responses count.")
        object.__setattr__(self, "mapping_no_weight", weight)
        for name in ("power_kw", "split_rich", "reference_o2", "window_seconds"):
            object.__setattr__(self, name, finite(getattr(self, name), name))
        if self.power_kw <= 0:
            raise ValueError("Thermal input must be greater than zero.")
        if not 0 < self.split_rich <= 1:
            raise ValueError("Stage-1 fuel split must be greater than 0 and at most 100%.")
        if not 0 <= self.reference_o2 < AIR_O2:
            raise ValueError("Reference O2 must be between 0 and 20.9%, exclusive at 20.9.")
        if not 5 <= self.window_seconds <= 3600:
            raise ValueError("Measurement window must be between 5 and 3600 seconds.")
        for name in ("optimise_power", "optimise_split"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be true or false.")
        dimensions = 3 + self.optimise_power + self.optimise_split
        if self.initial_points is None:
            object.__setattr__(self, "initial_points", dimensions + 1)
        if (isinstance(self.initial_points, bool)
                or not isinstance(self.initial_points, int)
                or not dimensions + 1 <= self.initial_points <= 100):
            raise ValueError(
                f"A {dimensions}-variable initial design must contain "
                f"{dimensions + 1} to 100 completed points.")
        pool_size = self.candidate_pool_size
        if pool_size is None:
            pool_size = max(1024, 256 * dimensions)
        if (isinstance(pool_size, bool) or not isinstance(pool_size, int)
                or not 64 <= pool_size <= 65_536):
            raise ValueError("Candidate pool size must be an integer from 64 to 65536.")
        object.__setattr__(self, "candidate_pool_size", pool_size)
        if len(self.bounds) != dimensions or any(len(pair) != 2 for pair in self.bounds):
            raise ValueError(f"Supply lower and upper bounds for all {dimensions} variables.")
        names = self.variable_names
        bounds = tuple(tuple(finite(v, name) for v in pair)
                       for name, pair in zip(names, self.bounds))
        object.__setattr__(self, "bounds", bounds)
        for name, (lo, hi) in zip(names, bounds):
            if not 0 < lo < hi:
                raise ValueError(f"{VARIABLE_LABELS[name]}: require 0 < lower < upper.")
        if bounds[0][1] >= 1:
            raise ValueError("H2 bounds must lie strictly between 0 and 100%.")
        if bounds[1][0] < 1 or bounds[2][1] >= 1:
            raise ValueError("This rich/lean experiment requires stage-1 phi >= 1 and overall phi < 1.")
        values = dict(zip(names, bounds))
        if self.optimise_power and not values["power_kw"][0] <= self.power_kw <= values["power_kw"][1]:
            raise ValueError("Nominal thermal input must lie inside its optimisation bounds.")
        if self.optimise_split:
            lo, hi = values["split_rich"]
            if hi > 1:
                raise ValueError("Stage-1 fuel-split bounds cannot exceed 100%.")
            if not lo <= self.split_rich <= hi:
                raise ValueError("Nominal stage-1 fuel split must lie inside its optimisation bounds.")

    @property
    def variable_names(self):
        names = list(BASE_VARIABLES)
        enabled = {"power_kw": self.optimise_power, "split_rich": self.optimise_split}
        names.extend(name for name in OPTIONAL_VARIABLES if enabled[name])
        return tuple(names)

    @property
    def dimensions(self):
        return len(self.variable_names)

    def values(self, point):
        if len(point) != self.dimensions:
            raise ValueError(f"An operating point must have {self.dimensions} coordinates.")
        values = {name: finite(value, VARIABLE_LABELS[name])
                  for name, value in zip(self.variable_names, point)}
        for name, (lo, hi) in zip(self.variable_names, self.bounds):
            if not lo <= values[name] <= hi:
                raise ValueError(f"{VARIABLE_LABELS[name]} is outside this experiment's bounds.")
        values.setdefault("power_kw", self.power_kw)
        values.setdefault("split_rich", self.split_rich)
        return values

    def request(self, point):
        values = self.values(point)
        return AutoCalcRequest(values["power_kw"], values["h2_fraction"],
                               values["phi_stage1"], values["phi_overall"],
                               split_rich=values["split_rich"])

    def observed_vector(self, window):
        point = list(window["observed_point"])
        if len(point) != 3:
            raise ValueError("Measured operating point must have three combustion coordinates.")
        if self.optimise_power:
            point.append(finite(window["power_kw"], "Measured thermal input"))
        if self.optimise_split:
            split = window.get("split_rich")
            if split is None:
                flows = window["mean_flows"]
                rich = flows.get("nh3_rich", 0) + flows.get("h2_rich", 0)
                lean = flows.get("nh3_lean", 0) + flows.get("h2_lean", 0)
                split = rich / (rich + lean)
            point.append(finite(split, "Measured stage-1 fuel split"))
        return point

    def targets(self, point):
        targets = auto_calc(self.request(point))
        if any(not math.isfinite(value) or value < 0 for value in targets.values()):
            raise ValueError("These settings produce non-finite or negative flow targets.")
        return targets

    def to_dict(self):
        return asdict(self)


def corrected_no(no_ppm, o2_percent, reference_o2, no_sem=None):
    """Return corrected dry NO and optional NO-only standard error.

    O2 uncertainty is not propagated: the UI and saved metadata say so.
    Inputs must be uncorrected, dry, co-averaged analyser readings.
    """
    no = finite(no_ppm, "NO")
    oxygen = finite(o2_percent, "O2")
    reference = finite(reference_o2, "Reference O2")
    if not 0 <= no <= 5000:
        raise ValueError("NO must be within the MEXA-584L range of 0 to 5000 ppm.")
    if not 0 <= oxygen < AIR_O2 or not 0 <= reference < AIR_O2:
        raise ValueError("O2 correction requires 0 <= O2 < 20.9%.")
    factor = (AIR_O2 - reference) / (AIR_O2 - oxygen)
    sem = None if no_sem is None else finite(no_sem, "NO standard error")
    if sem is not None and sem <= 0:
        raise ValueError("Standard error must be positive, or left blank if unknown.")
    return no * factor, None if sem is None else sem * factor


def _latent_posterior(gp, baseline, candidates):
    """Latent posterior blocks, excluding observation (WhiteKernel) noise."""
    from scipy.linalg import solve_triangular
    kernel = gp.kernel_.k1
    all_x = np.vstack((baseline, candidates))
    cross = kernel(all_x, gp.X_train_)
    v = solve_triangular(gp.L_, cross.T, lower=True, check_finite=False)
    mean = cross @ gp.alpha_
    n = len(baseline)
    cov_bb = kernel(baseline) - v[:, :n].T @ v[:, :n]
    cov_bc = kernel(baseline, candidates) - v[:, :n].T @ v[:, n:]
    variance = np.maximum(kernel.diag(candidates) - np.sum(v[:, n:] ** 2, axis=0), 0)
    return mean[:n], mean[n:], cov_bb, cov_bc, variance


def noisy_expected_improvement(gp, baseline, candidates, rng, draws=128):
    """Integrate conditional analytic EI over posterior baseline fantasies."""
    from scipy.linalg import cho_solve
    from scipy.special import ndtr
    mu_b, mu_c, cov_b, cov_bc, var_c = _latent_posterior(gp, baseline, candidates)
    jitter = max(1e-10, float(np.max(np.diag(cov_b))) * 1e-8)
    chol = np.linalg.cholesky(cov_b + np.eye(len(baseline)) * jitter)
    fantasies = mu_b[:, None] + chol @ rng.standard_normal((len(baseline), draws))
    weights = cho_solve((chol, True), cov_bc, check_finite=False)
    means = mu_c[None, :] + (fantasies - mu_b[:, None]).T @ weights
    sigma = np.sqrt(np.maximum(var_c - np.sum(cov_bc * weights, axis=0), 1e-12))
    improvement = fantasies.min(axis=0)[:, None] - means
    z = improvement / sigma[None, :]
    ei = improvement * ndtr(z) + sigma[None, :] * np.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    return np.maximum(ei.mean(axis=0), 0), mu_c, np.sqrt(var_c)


def _pressure_values(config, completed):
    values = []
    dual = None
    for trial in completed:
        pressure = trial.get("pressure")
        current_dual = isinstance(pressure, dict) and "transducers" in pressure
        if dual is None:
            dual = current_dual
        elif dual != current_dual:
            raise ValueError("Completed mapping trials cannot mix single and dual pressure summaries.")
        if current_dual:
            entries = pressure.get("transducers")
            if (not isinstance(entries, list) or len(entries) != 2
                    or [item.get("id") for item in entries if isinstance(item, dict)] != ["pressure_1", "pressure_2"]):
                raise ValueError("Completed mapping trials require pressure_1 and pressure_2 summaries.")
            row = []
            for item in entries:
                metrics = item.get("metrics")
                if not isinstance(metrics, dict) or "dominant_amplitude_pa" not in metrics:
                    raise ValueError("Dual pressure mapping requires dominant_amplitude_pa for each transducer.")
                value = finite(metrics["dominant_amplitude_pa"], "Pressure dominant_amplitude_pa")
                if value < 0:
                    raise ValueError("Pressure amplitude must be nonnegative.")
                row.append(value)
            values.append(row)
            continue
        if not isinstance(pressure, dict) or config.pressure_metric not in pressure:
            raise ValueError(f"Completed mapping trials require pressure {config.pressure_metric}.")
        value = finite(pressure[config.pressure_metric], f"Pressure {config.pressure_metric}")
        if value < 0:
            raise ValueError("Pressure amplitude must be nonnegative.")
        values.append(value)
    return np.asarray(values, dtype=float)


def _fit_mapping_models(config, completed, seed):
    """Independent, standardized latent response models with fitted white noise.

    NO's supplied SEM contributes known observation variance. Pressure summaries
    have no inferred SEM: pressure observation noise is fitted from the data.
    """
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel
    from threadpoolctl import threadpool_limits

    bounds = np.asarray(config.bounds)
    x = np.asarray([config.observed_vector(t["window"]) for t in completed])
    x = (x - bounds[:, 0]) / (bounds[:, 1] - bounds[:, 0])
    no = np.asarray([finite(t["result"]["corrected_no"], "Corrected NO") for t in completed])
    pressure = _pressure_values(config, completed)
    errors = np.asarray([0 if t["result"].get("corrected_sem") is None
                         else finite(t["result"]["corrected_sem"], "Corrected NO SEM")
                         for t in completed])
    if np.any(errors < 0):
        raise ValueError("Corrected NO SEM must be nonnegative.")
    models = []
    responses = [(no, errors)]
    if pressure.ndim == 2:
        responses.extend((pressure[:, index], np.zeros(len(completed))) for index in range(2))
    else:
        responses.append((pressure, np.zeros(len(completed))))
    for response, sem in responses:
        centre, scale = float(response.mean()), float(response.std())
        # Use actual response units even below 1 Pa; a unit-size floor would
        # change the acquisition when the same pressure data are rescaled.
        if scale == 0:
            scale = max(abs(centre), 1.0)
        kernel = ConstantKernel(1.0, (1e-3, 1e3)) * Matern(
            [.3] * config.dimensions, (.03, 3.0), nu=2.5) + WhiteKernel(.01, (1e-6, 1.0))
        gp = GaussianProcessRegressor(kernel=kernel, alpha=(sem / scale) ** 2 + 1e-8,
                                     n_restarts_optimizer=1, random_state=seed)
        with threadpool_limits(limits=1), warnings.catch_warnings():
            warnings.simplefilter("ignore", ConvergenceWarning)
            gp.fit(x, (response - centre) / scale)
        models.append((gp, centre, scale))
    return models


def integrated_variance_reduction(gp, reference, candidates):
    """Fractional reduction in mean reference latent variance from one sample.

    Conditional GP covariance is independent of the as-yet-unobserved response.
    Future observation variance includes fitted white noise, never latent-only
    noise-free precision. Reference locations are fixed across all candidates.
    """
    _, mean, covariance, cross, variance = _latent_posterior(gp, reference, candidates)
    reference_variance = max(float(np.maximum(np.diag(covariance), 0).mean()), 1e-12)
    observation_variance = variance + float(gp.kernel_.k2.noise_level) + 1e-8
    reduction = np.mean(cross ** 2, axis=0) / observation_variance
    return np.maximum(reduction / reference_variance, 0), mean, np.sqrt(variance)


def _mapping_model_metadata(model, units):
    gp, centre, scale = model
    return {
        "fitted_kernel": str(gp.kernel_),
        "signal_variance": float(gp.kernel_.k1.k1.constant_value),
        "matern_length_scales": np.atleast_1d(gp.kernel_.k1.k2.length_scale).astype(float).tolist(),
        "white_noise_level": float(gp.kernel_.k2.noise_level),
        "response_centre": centre, "response_scale": scale, "response_units": units,
    }


def predict_mapping(config, trials, points):
    """Predict latent means and SDs in ppm/Pa at bounded feasible slice points.

    Uses deterministic model fits (seed 0); no candidate selection or actuation.
    Flow ceilings are campaign-specific and are applied by the slice caller.
    """
    from threadpoolctl import threadpool_limits
    if config.objective_mode != "map_no_pressure":
        raise ValueError("Mapping predictions require map_no_pressure mode.")
    completed = [trial for trial in trials if trial["status"] == "completed"]
    if len(completed) < config.initial_points:
        raise ValueError("Mapping predictions require the completed initial design.")
    points = list(points)
    for point in points:
        config.targets(point)
    pressure_values = _pressure_values(config, completed)
    dual = pressure_values.ndim == 2
    result = ({"no_mean": [], "no_sd": [], "pressure_1_mean": [], "pressure_1_sd": [],
               "pressure_2_mean": [], "pressure_2_sd": []} if dual else
              {"no_mean": [], "no_sd": [], "pressure_mean": [], "pressure_sd": []})
    if not points:
        return result
    bounds = np.asarray(config.bounds)
    x = (np.asarray(points, dtype=float) - bounds[:, 0]) / (bounds[:, 1] - bounds[:, 0])
    with threadpool_limits(limits=1):
        models = _fit_mapping_models(config, completed, seed=0)
        names = ("no", "pressure_1", "pressure_2") if dual else ("no", "pressure")
        for name, (gp, centre, scale) in zip(names, models):
            _, mean, _, _, variance = _latent_posterior(gp, np.empty((0, config.dimensions)), x)
            result[name + "_mean"] = (mean * scale + centre).tolist()
            result[name + "_sd"] = (np.sqrt(variance) * scale).tolist()
    return result


def suggest(config, trials, limits=None, seed=0, pool_size=None):
    """Return a feasible suggestion, never a flow command.

    Initial design uses space-filling maximin selection from a Sobol pool.
    Later calls use Matérn-5/2 GPs with fitted residual observation noise:
    NEI for NO minimisation, or integrated variance reduction for joint mapping.
    Rejected/invalid points are excluded from the candidate pool, not given
    fabricated zero emissions. Repeats are explicit operator actions.
    """
    from scipy.stats import qmc
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel
    from threadpoolctl import threadpool_limits

    rng = np.random.default_rng(seed)
    dimensions = config.dimensions
    pool_size = config.candidate_pool_size if pool_size is None else pool_size
    bounds = np.array(config.bounds)
    lower, span = bounds[:, 0], bounds[:, 1] - bounds[:, 0]
    pool = qmc.Sobol(dimensions, scramble=True, seed=seed).random_base2(
        int(math.ceil(math.log2(pool_size))))
    sobol_count = len(pool)
    mapping = config.objective_mode == "map_no_pressure"
    completed = [t for t in trials if t["status"] == "completed"]
    dual_mapping = False
    if mapping:
        pressure_values = _pressure_values(config, completed)
        dual_mapping = pressure_values.ndim == 2
        # Keep quadrature locations independent of response values and of the
        # exclusion of previous trial locations from the suggestion pool.
        reference = np.asarray([candidate for candidate in pool
            if all(config.targets(lower + candidate * span).get(role, 0) <= ceiling
                   for role, ceiling in (limits or {}).items())])[:256]
    if completed and not mapping:
        best = min(completed, key=lambda t: t["result"]["corrected_no"])
        centre = (np.asarray(best["point"]) - lower) / span
        pool = np.vstack((pool, np.clip(
            centre + rng.normal(0, .08, (128, dimensions)), 0, 1)))
    generated_count = len(pool)
    tried = np.array([(np.asarray(t["point"]) - lower) / span for t in trials])
    if len(tried):
        distance = np.linalg.norm(pool[:, None, :] - tried[None, :, :], axis=2).min(axis=1)
        pool = pool[distance > 1e-4]
    feasible = []
    for candidate in pool:
        point = lower + candidate * span
        targets = config.targets(point)
        if all(targets.get(role, 0) <= ceiling for role, ceiling in (limits or {}).items()):
            feasible.append(candidate)
    if not feasible:
        raise ValueError("No candidate in this sampling pool fits the current flow ceilings. Review bounds and limits.")
    pool = np.asarray(feasible)
    training_payload = [{
        "id": trial.get("id"),
        "observed": config.observed_vector(trial["window"]),
        "corrected_no": trial["result"]["corrected_no"],
        "corrected_sem": trial["result"].get("corrected_sem"),
    } for trial in completed]
    if mapping:
        effective_pressure_metric = "dominant_amplitude_pa" if dual_mapping else config.pressure_metric
        for payload, trial in zip(training_payload, completed):
            payload.update(pressure=trial["pressure"], pressure_metric=effective_pressure_metric)
    hash_payload = ({"pressure_metric": effective_pressure_metric, "trials": training_payload}
                    if mapping else training_payload)
    provenance = {
        "algorithm_version": "fcbo-sobol-maximin-v1",
        "seed": int(seed),
        "candidate_pool_requested": int(pool_size),
        "sobol_candidate_count": int(sobol_count),
        "candidate_pool_generated": int(generated_count),
        "candidate_count_feasible": int(len(pool)),
        "flow_ceilings_slpm": {str(role): finite(value, f"Flow ceiling for {role}")
                               for role, value in (limits or {}).items()},
        "training_trial_count": len(completed),
        "training_data_sha256": hashlib.sha256(json.dumps(
            hash_payload, sort_keys=True, allow_nan=False,
            separators=(",", ":")).encode("utf-8")).hexdigest(),
    }
    if mapping:
        provenance.update(objective_mode=config.objective_mode,
                          pressure_metric=effective_pressure_metric,
                          mapping_no_weight=config.mapping_no_weight)
    if len(completed) < config.initial_points:
        if len(tried):
            scores = np.linalg.norm(pool[:, None, :] - tried[None, :, :], axis=2).min(axis=1)
            index = int(np.argmax(scores))
        else:
            index = int(np.argmin(np.linalg.norm(pool - .5, axis=1)))
        return {"point": (lower + pool[index] * span).tolist(),
                "method": "Space-filling initial design", **provenance}

    if mapping:
        with threadpool_limits(limits=1):
            models = _fit_mapping_models(config, completed, seed)
            no_score, no_mean, no_sd = integrated_variance_reduction(models[0][0], reference, pool)
            pressure_results = [integrated_variance_reduction(model[0], reference, pool)
                                for model in models[1:]]
        weight = config.mapping_no_weight
        pressure_score = sum(item[0] for item in pressure_results) / len(pressure_results)
        score = weight * no_score + (1 - weight) * pressure_score
        index = int(np.argmax(score))
        _, no_centre, no_scale = models[0]
        response = {
            **provenance,
            "point": (lower + pool[index] * span).tolist(),
            "method": "Bayesian NO/pressure integrated variance reduction",
            "algorithm_version": "fcbo-matern52-no-pressure-ivr-v1",
            "predicted_no": float(no_mean[index] * no_scale + no_centre),
            "latent_sd": float(no_sd[index] * no_scale),
            "mapping_score": float(score[index]),
            "mapping_no_score": float(no_score[index]),
            "mapping_pressure_score": float(pressure_score[index]),
            "mapping_reference_count": len(reference),
            "no_model": _mapping_model_metadata(models[0], "ppm"),
        }
        if len(models) == 2:
            pressure_mean, pressure_sd = pressure_results[0][1:]
            _, pressure_centre, pressure_scale = models[1]
            response.update(
                predicted_pressure_pa=float(pressure_mean[index] * pressure_scale + pressure_centre),
                pressure_latent_sd_pa=float(pressure_sd[index] * pressure_scale),
                pressure_model=_mapping_model_metadata(models[1], "Pa"))
        else:
            response["method"] = "Bayesian NO/two-pressure integrated variance reduction"
            predictions = []
            for number, (model, values) in enumerate(zip(models[1:], pressure_results), 1):
                mean, sd = values[1], values[2]
                _, centre, scale = model
                predicted = float(mean[index] * scale + centre)
                uncertainty = float(sd[index] * scale)
                predictions.append((predicted, uncertainty))
                response[f"predicted_pressure_{number}_pa"] = predicted
                response[f"pressure_{number}_latent_sd_pa"] = uncertainty
                response[f"pressure_{number}_model"] = _mapping_model_metadata(model, "Pa")
                response[f"mapping_pressure_{number}_score"] = float(values[0][index])
            worst = max(range(2), key=lambda item: predictions[item][0])
            response["predicted_pressure_pa"] = predictions[worst][0]
            response["pressure_latent_sd_pa"] = predictions[worst][1]
            response["pressure_model"] = response[f"pressure_{worst + 1}_model"]
            response["algorithm_version"] = "fcbo-matern52-no-dual-pressure-ivr-v1"
        return response

    x = np.array([config.observed_vector(t["window"]) for t in completed])
    x = (x - lower) / span
    y = np.array([t["result"]["corrected_no"] for t in completed])
    centre, scale = float(y.mean()), max(float(y.std()), 1.0)
    errors = np.array([t["result"].get("corrected_sem") or 0 for t in completed]) / scale
    kernel = ConstantKernel(1.0, (1e-3, 1e3)) * Matern(
        [.3] * dimensions, (.03, 3.0), nu=2.5) + WhiteKernel(.01, (1e-6, 1.0))
    gp = GaussianProcessRegressor(kernel=kernel, alpha=errors ** 2 + 1e-8,
                                 n_restarts_optimizer=1, random_state=seed)
    with threadpool_limits(limits=1), warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        gp.fit(x, (y - centre) / scale)
        acquisition, mean, std = noisy_expected_improvement(gp, np.unique(x, axis=0), pool, rng)
    index = int(np.argmax(acquisition))
    provenance.update({
        "algorithm_version": "fcbo-matern52-nei-v1",
        "fitted_kernel": str(gp.kernel_),
        "signal_variance": float(gp.kernel_.k1.k1.constant_value),
        "matern_length_scales": np.atleast_1d(
            gp.kernel_.k1.k2.length_scale).astype(float).tolist(),
        "white_noise_level": float(gp.kernel_.k2.noise_level),
        "response_centre_ppm": centre,
        "response_scale_ppm": scale,
        "monte_carlo_draws": 128,
    })
    return {"point": (lower + pool[index] * span).tolist(),
            "method": "Bayesian noisy expected improvement",
            "predicted_no": float(mean[index] * scale + centre),
            "latent_sd": float(std[index] * scale),
            "expected_improvement": float(acquisition[index] * scale),
            **provenance}
