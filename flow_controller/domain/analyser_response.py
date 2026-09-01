"""Incremental detection of a measured NO step response.

The detector reports the response of the complete measurement path.  Its
timings include gas transport, analyser processing and sampling delays; they
must not be described as the analyser's intrinsic response time.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import statistics


@dataclass(frozen=True)
class ResponseCriteria:
    """Fixed, auditable acceptance criteria, expressed in seconds and ppm."""

    baseline_min_samples: int = 6
    baseline_min_span_s: float = 15.0
    baseline_window_s: float = 30.0
    baseline_drift_abs_ppm: float = 1.0
    baseline_drift_rel: float = 0.02
    departure_samples: int = 3
    departure_abs_ppm: float = 2.0
    departure_sd_multiplier: float = 4.0
    departure_rel: float = 0.02
    relative_mean_floor_ppm: float = 10.0
    stable_min_samples: int = 6
    stable_window_s: float = 20.0
    stable_drift_abs_ppm: float = 1.5
    stable_drift_rel_amplitude: float = 0.05
    stable_sd_abs_ppm: float = 1.0
    stable_sd_rel_amplitude: float = 0.05
    max_gap_s: float = 5.0
    timeout_s: float = 900.0
    max_samples: int = 4_000
    recommendation_factor: float = 1.25
    recommendation_quantum_s: float = 5.0
    recommendation_min_s: float = 5.0
    recommendation_max_s: float = 3600.0

    def __post_init__(self) -> None:
        numeric = {
            name: value for name, value in vars(self).items()
            if name not in {"baseline_min_samples", "departure_samples",
                            "stable_min_samples", "max_samples"}
        }
        if any(not math.isfinite(value) or value <= 0 for value in numeric.values()):
            raise ValueError("All response criteria must be finite and positive.")
        for name in ("baseline_min_samples", "departure_samples",
                     "stable_min_samples", "max_samples"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if self.baseline_window_s < self.baseline_min_span_s:
            raise ValueError("The baseline window cannot be shorter than its minimum span.")
        if self.recommendation_max_s < self.recommendation_min_s:
            raise ValueError("The recommendation maximum must not be below its minimum.")


@dataclass(frozen=True)
class ResponseSample:
    """One accepted NO reading with its acquisition identity."""

    timestamp_s: float
    no_ppm: float
    source_id: str
    sequence: int


@dataclass(frozen=True)
class WindowMetrics:
    """Mean, sample standard deviation, least-squares slope and time span."""

    mean_ppm: float
    sd_ppm: float
    slope_ppm_per_s: float
    span_s: float
    sample_count: int


@dataclass(frozen=True)
class AnalyserResponseResult:
    """Completed system-response measurement.

    ``t10_s``, ``t50_s`` and ``t90_s`` are command-to-crossing times for the
    first three-sample sustained crossings of 10, 50 and 90 percent of the
    final signed change. ``rise_10_90_s`` is t90 minus t10 (also for a falling
    response). ``sample_resolution_s`` is the median accepted sample interval.
    """

    criteria: ResponseCriteria
    baseline: WindowMetrics
    final: WindowMetrics
    signed_amplitude_ppm: float
    command_at: float
    change_at: float
    stable_window_start: float
    stable_window_end: float
    command_to_change_s: float
    command_to_stable_s: float
    t10_s: float | None
    t50_s: float | None
    t90_s: float | None
    rise_10_90_s: float | None
    sample_resolution_s: float
    source_id: str
    first_sequence: int
    last_sequence: int
    raw_accepted_samples: tuple[ResponseSample, ...]
    quality: str
    caveats: tuple[str, ...]
    flow_reached_at: float | None = None
    flow_to_stable_s: float | None = None
    recommended_delay_s: float | None = None

    def with_flow_reached_at(self, flow_reached_at: float) -> "AnalyserResponseResult":
        """Add the controller's flow-settled time and calculate a logging delay.

        The recommendation is 1.25 times the larger of flow-to-stability and
        sampling resolution, rounded upward to 5 s and bounded to 5..3600 s.
        """
        value = _finite(flow_reached_at, "flow_reached_at")
        if not self.command_at <= value <= self.stable_window_end:
            raise ValueError("flow_reached_at must lie between command and stability times.")
        # Stability begins at the first sample in the accepted trailing
        # window.  The window end is when that conclusion becomes available,
        # not when the response first became stable.
        flow_to_stable = max(0.0, self.stable_window_start - value)
        raw = self.criteria.recommendation_factor * max(
            flow_to_stable, self.sample_resolution_s
        )
        quantum = self.criteria.recommendation_quantum_s
        rounded = math.ceil(raw / quantum) * quantum
        recommended = min(
            self.criteria.recommendation_max_s,
            max(self.criteria.recommendation_min_s, rounded),
        )
        return replace(
            self,
            flow_reached_at=value,
            flow_to_stable_s=flow_to_stable,
            recommended_delay_s=recommended,
        )


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number.") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number.")
    return number


def _metrics(samples: list[ResponseSample]) -> WindowMetrics:
    values = [sample.no_ppm for sample in samples]
    times = [sample.timestamp_s for sample in samples]
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if len(values) >= 2 else 0.0
    time_mean = statistics.fmean(times)
    denominator = sum((time - time_mean) ** 2 for time in times)
    slope = (sum((time - time_mean) * (value - mean)
                 for time, value in zip(times, values)) / denominator
             if denominator else 0.0)
    return WindowMetrics(mean, sd, slope, times[-1] - times[0], len(samples))


class AnalyserResponseDetector:
    """Stateful, hardware-independent detector suitable for a Qt controller.

    Add baseline readings until :attr:`baseline_ready` is true, call
    :meth:`begin_step` when issuing the step, then add post-step readings.
    Pass ``allow_stable=True`` only after the flow controller confirms that the
    destination condition has been reached.
    """

    def __init__(self, criteria: ResponseCriteria | None = None) -> None:
        self.criteria = criteria or ResponseCriteria()
        self._samples: list[ResponseSample] = []
        self._baseline_window: list[ResponseSample] = []
        self._post_step: list[ResponseSample] = []
        self._baseline: WindowMetrics | None = None
        self._command_at: float | None = None
        self._change_at: float | None = None
        self._departure_sign: int | None = None
        self._departure_streak: list[ResponseSample] = []
        self._result: AnalyserResponseResult | None = None
        self._state = "baseline"
        self._failure_reason: str | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def failure_reason(self) -> str | None:
        return self._failure_reason

    @property
    def baseline_ready(self) -> bool:
        return self._state == "ready"

    @property
    def baseline_metrics(self) -> WindowMetrics | None:
        return self._baseline

    @property
    def result(self) -> AnalyserResponseResult | None:
        return self._result

    @property
    def command_at(self) -> float | None:
        return self._command_at

    @property
    def change_detected(self) -> bool:
        return self._change_at is not None

    @property
    def departure_threshold_ppm(self) -> float | None:
        if self._baseline is None:
            return None
        c = self.criteria
        return max(
            c.departure_abs_ppm,
            c.departure_sd_multiplier * self._baseline.sd_ppm,
            c.departure_rel * max(abs(self._baseline.mean_ppm),
                                  c.relative_mean_floor_ppm),
        )

    def cancel(self, reason: str = "Cancelled by caller.") -> None:
        if self._state in {"completed", "failed", "cancelled"}:
            raise RuntimeError(f"Cannot cancel a {self._state} detector.")
        self._state = "cancelled"
        self._failure_reason = str(reason)

    def check_timeout(self, now_s: float) -> None:
        """Fail with ``TimeoutError`` when response acquisition exceeds timeout."""
        now = _finite(now_s, "now_s")
        if self._state == "response" and self._command_at is not None:
            if now - self._command_at > self.criteria.timeout_s:
                self._state = "failed"
                self._failure_reason = "Response measurement timed out."
                raise TimeoutError(self._failure_reason)

    def begin_step(self, command_time_s: float) -> None:
        """Freeze the ready baseline at the time the step command is issued."""
        if not self.baseline_ready or self._baseline is None:
            raise RuntimeError("A stable baseline is required before beginning the step.")
        command = _finite(command_time_s, "command_time_s")
        if self._samples and command < self._samples[-1].timestamp_s:
            raise ValueError("command_time_s cannot precede the latest baseline sample.")
        self._command_at = command
        self._state = "response"

    def add_sample(
        self,
        timestamp_s: float,
        no_ppm: float,
        source_id: str,
        sequence: int,
        *,
        allow_stable: bool = False,
    ) -> AnalyserResponseResult | None:
        """Validate and consume one reading, returning the result on completion."""
        if self._state in {"completed", "failed", "cancelled"}:
            raise RuntimeError(f"Cannot add samples to a {self._state} detector.")
        sample = self._validated_sample(timestamp_s, no_ppm, source_id, sequence)
        self._samples.append(sample)

        if self._state in {"baseline", "ready"}:
            self._update_baseline(sample)
            return None

        assert self._command_at is not None and self._baseline is not None
        if sample.timestamp_s < self._command_at:
            return self._fail(ValueError(
                "Post-step sample timestamp cannot precede the command time."))
        self._post_step.append(sample)
        self._update_departure(sample)
        if self._change_at is not None and allow_stable:
            return self._try_complete()
        return None

    def _validated_sample(self, timestamp_s, no_ppm, source_id, sequence):
        timestamp = _finite(timestamp_s, "timestamp_s")
        value = _finite(no_ppm, "no_ppm")
        if not 0.0 <= value <= 5000.0:
            return self._fail(ValueError("no_ppm must be between 0 and 5000 ppm."))
        if not isinstance(source_id, str) or not source_id.strip():
            return self._fail(ValueError("source_id must be a non-empty string."))
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            return self._fail(ValueError("sequence must be a non-negative integer."))
        if len(self._samples) >= self.criteria.max_samples:
            return self._fail(ValueError("The response measurement sample cap was reached."))
        if self._samples:
            previous = self._samples[-1]
            if source_id != previous.source_id:
                return self._fail(ValueError("The NO source changed during measurement."))
            if sequence != previous.sequence + 1:
                return self._fail(ValueError("NO sample sequence numbers must be contiguous."))
            gap = timestamp - previous.timestamp_s
            if gap <= 0:
                return self._fail(ValueError("NO sample timestamps must increase monotonically."))
            if gap > self.criteria.max_gap_s:
                return self._fail(ValueError("The gap between NO samples exceeded the limit."))
        return ResponseSample(timestamp, value, source_id, sequence)

    def _fail(self, error):
        self._state = "failed"
        self._failure_reason = str(error)
        raise error

    def _update_baseline(self, sample: ResponseSample) -> None:
        cutoff = sample.timestamp_s - self.criteria.baseline_window_s
        self._baseline_window = [item for item in self._baseline_window
                                 if item.timestamp_s >= cutoff]
        self._baseline_window.append(sample)
        metrics = _metrics(self._baseline_window)
        self._baseline = metrics
        enough = (metrics.sample_count >= self.criteria.baseline_min_samples
                  and metrics.span_s >= self.criteria.baseline_min_span_s)
        drift_band = max(
            self.criteria.baseline_drift_abs_ppm,
            self.criteria.baseline_drift_rel
            * max(abs(metrics.mean_ppm), self.criteria.relative_mean_floor_ppm),
        )
        stable = abs(metrics.slope_ppm_per_s) * metrics.span_s <= drift_band
        self._state = "ready" if enough and stable else "baseline"

    def _update_departure(self, sample: ResponseSample) -> None:
        assert self._baseline is not None
        # The reported change is the first sustained departure.  Freeze it so
        # a later overshoot or return through the baseline cannot rewrite it.
        if self._change_at is not None:
            return
        difference = sample.no_ppm - self._baseline.mean_ppm
        threshold = self.departure_threshold_ppm
        assert threshold is not None
        sign = 1 if difference >= threshold else (-1 if difference <= -threshold else 0)
        if not sign:
            self._departure_streak.clear()
            return
        previous_sign = (1 if self._departure_streak
                         and self._departure_streak[-1].no_ppm >= self._baseline.mean_ppm
                         else -1)
        if self._departure_streak and sign != previous_sign:
            self._departure_streak.clear()
        self._departure_streak.append(sample)
        if len(self._departure_streak) >= self.criteria.departure_samples:
            self._change_at = self._departure_streak[0].timestamp_s
            self._departure_sign = sign

    def _try_complete(self) -> AnalyserResponseResult | None:
        assert self._baseline is not None and self._command_at is not None
        end = self._post_step[-1].timestamp_s
        window = [sample for sample in self._post_step
                  if sample.timestamp_s >= end - self.criteria.stable_window_s]
        if len(window) < self.criteria.stable_min_samples:
            return None
        final = _metrics(window)
        if final.span_s < self.criteria.stable_window_s:
            return None
        amplitude = final.mean_ppm - self._baseline.mean_ppm
        threshold = self.departure_threshold_ppm
        assert threshold is not None and self._departure_sign is not None
        if abs(amplitude) < threshold or amplitude * self._departure_sign <= 0:
            return None
        drift_band = max(
            self.criteria.stable_drift_abs_ppm,
            self.criteria.stable_drift_rel_amplitude * abs(amplitude),
        )
        sd_band = max(
            self.criteria.stable_sd_abs_ppm,
            self.criteria.stable_sd_rel_amplitude * abs(amplitude),
        )
        if abs(final.slope_ppm_per_s) * final.span_s > drift_band:
            return None
        if final.sd_ppm > sd_band:
            return None
        self._result = self._build_result(window, final, amplitude)
        self._state = "completed"
        return self._result

    def _build_result(self, window, final, amplitude):
        assert self._baseline is not None
        assert self._command_at is not None and self._change_at is not None
        intervals = [right.timestamp_s - left.timestamp_s
                     for left, right in zip(self._samples, self._samples[1:])]
        resolution = statistics.median(intervals) if intervals else 0.0
        crossings = [self._sustained_crossing(fraction, amplitude)
                     for fraction in (0.1, 0.5, 0.9)]
        elapsed = [None if value is None else value - self._command_at
                   for value in crossings]
        rise = (elapsed[2] - elapsed[0]
                if elapsed[0] is not None and elapsed[2] is not None else None)
        caveats = (
            "This is the response of the complete sampling path, including gas transport, "
            "analyser processing and acquisition; it is not an analyser-only response time.",
            "Crossing times are quantised by the NO sampling interval.",
            "The recommended delay requires flow_reached_at from the controller.",
        )
        return AnalyserResponseResult(
            criteria=self.criteria,
            baseline=self._baseline,
            final=final,
            signed_amplitude_ppm=amplitude,
            command_at=self._command_at,
            change_at=self._change_at,
            stable_window_start=window[0].timestamp_s,
            stable_window_end=window[-1].timestamp_s,
            command_to_change_s=self._change_at - self._command_at,
            command_to_stable_s=window[0].timestamp_s - self._command_at,
            t10_s=elapsed[0], t50_s=elapsed[1], t90_s=elapsed[2],
            rise_10_90_s=rise,
            sample_resolution_s=resolution,
            source_id=self._samples[0].source_id,
            first_sequence=self._samples[0].sequence,
            last_sequence=self._samples[-1].sequence,
            raw_accepted_samples=tuple(self._samples),
            quality="Accepted: sustained departure and stable final plateau met all criteria.",
            caveats=caveats,
        )

    def _sustained_crossing(self, fraction: float, amplitude: float) -> float | None:
        assert self._baseline is not None
        target = self._baseline.mean_ppm + fraction * amplitude
        sign = 1 if amplitude > 0 else -1
        streak: list[ResponseSample] = []
        for sample in self._post_step:
            crossed = sign * (sample.no_ppm - target) >= 0
            if crossed:
                streak.append(sample)
                if len(streak) >= self.criteria.departure_samples:
                    return streak[0].timestamp_s
            else:
                streak.clear()
        return None
