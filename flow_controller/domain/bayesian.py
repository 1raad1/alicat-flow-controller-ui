"""One-point Bayesian design for the pilot-off NH3/H2 experiment.

No Qt, persistence, serial I/O or actuation. The acquisition integrates over
uncertain latent baseline values (Monte Carlo noisy expected improvement).
Parameter bounds and flow ceilings are known constraints, not learned safety.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import warnings

import numpy as np

from .rql import AutoCalcRequest, auto_calc


AIR_O2 = 20.9  # dry volume %, the fixed reporting convention
PARAMETERS = ("H2 fraction", "stage-1 phi", "overall phi")


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
    initial_points: int = 16
    window_seconds: float = 30.0

    def __post_init__(self):
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
        if (isinstance(self.initial_points, bool)
                or not isinstance(self.initial_points, int)
                or not 4 <= self.initial_points <= 100):
            raise ValueError("Initial design must contain 4 to 100 completed points.")
        if len(self.bounds) != 3 or any(len(pair) != 2 for pair in self.bounds):
            raise ValueError("Supply lower and upper bounds for all three variables.")
        bounds = tuple(tuple(finite(v, name) for v in pair)
                       for name, pair in zip(PARAMETERS, self.bounds))
        object.__setattr__(self, "bounds", bounds)
        for name, (lo, hi) in zip(PARAMETERS, bounds):
            if not 0 < lo < hi:
                raise ValueError(f"{name}: require 0 < lower < upper.")
        if bounds[0][1] >= 1:
            raise ValueError("H2 bounds must lie strictly between 0 and 100%.")
        if bounds[1][0] < 1 or bounds[2][1] >= 1:
            raise ValueError("This rich/lean experiment requires stage-1 phi >= 1 and overall phi < 1.")

    def request(self, point):
        if len(point) != 3:
            raise ValueError("An operating point must have three coordinates.")
        point = tuple(finite(v, name) for name, v in zip(PARAMETERS, point))
        for value, (lo, hi) in zip(point, self.bounds):
            if not lo <= value <= hi:
                raise ValueError("Candidate is outside this experiment's bounds.")
        return AutoCalcRequest(self.power_kw, *point, split_rich=self.split_rich)

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


def suggest(config, trials, limits=None, seed=0, pool_size=1024):
    """Return a feasible suggestion, never a flow command.

    Initial design uses space-filling maximin selection from a Sobol pool.
    Later calls use a Matérn-5/2 GP, fitted residual observation noise and NEI.
    Rejected/invalid points are excluded from the candidate pool, not given
    fabricated zero emissions. Repeats are explicit operator actions.
    """
    from scipy.stats import qmc
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel
    from threadpoolctl import threadpool_limits

    rng = np.random.default_rng(seed)
    bounds = np.array(config.bounds)
    lower, span = bounds[:, 0], bounds[:, 1] - bounds[:, 0]
    pool = qmc.Sobol(3, scramble=True, seed=seed).random_base2(int(math.ceil(math.log2(pool_size))))
    completed = [t for t in trials if t["status"] == "completed"]
    if completed:
        best = min(completed, key=lambda t: t["result"]["corrected_no"])
        centre = (np.asarray(best["point"]) - lower) / span
        pool = np.vstack((pool, np.clip(centre + rng.normal(0, .08, (128, 3)), 0, 1)))
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
    if len(completed) < config.initial_points:
        if len(tried):
            scores = np.linalg.norm(pool[:, None, :] - tried[None, :, :], axis=2).min(axis=1)
            index = int(np.argmax(scores))
        else:
            index = int(np.argmin(np.linalg.norm(pool - .5, axis=1)))
        return {"point": (lower + pool[index] * span).tolist(), "method": "Space-filling initial design"}

    x = np.array([t["window"]["observed_point"] for t in completed])
    x = (x - lower) / span
    y = np.array([t["result"]["corrected_no"] for t in completed])
    centre, scale = float(y.mean()), max(float(y.std()), 1.0)
    errors = np.array([t["result"].get("corrected_sem") or 0 for t in completed]) / scale
    kernel = ConstantKernel(1.0, (1e-3, 1e3)) * Matern(
        [.3] * 3, (.03, 3.0), nu=2.5) + WhiteKernel(.01, (1e-6, 1.0))
    gp = GaussianProcessRegressor(kernel=kernel, alpha=errors ** 2 + 1e-8,
                                 n_restarts_optimizer=1, random_state=seed)
    with threadpool_limits(limits=1), warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        gp.fit(x, (y - centre) / scale)
        acquisition, mean, std = noisy_expected_improvement(gp, np.unique(x, axis=0), pool, rng)
    index = int(np.argmax(acquisition))
    return {"point": (lower + pool[index] * span).tolist(),
            "method": "Bayesian noisy expected improvement",
            "predicted_no": float(mean[index] * scale + centre),
            "latent_sd": float(std[index] * scale),
            "expected_improvement": float(acquisition[index] * scale)}
