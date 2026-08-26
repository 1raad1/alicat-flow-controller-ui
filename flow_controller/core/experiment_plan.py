"""Deterministic, hardware-independent experiment plans.

The runner in this module decides *when* a stage advances.  It deliberately
does not know how to write an Alicat, show a dialog, or schedule a Qt timer.
Its caller executes the emitted actions through :class:`FlowSession`, keeping
the existing setpoint queue and verified-zero path as the only hardware
authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import Mapping


PLAN_FORMAT = "flow-controller-experiment-plan"
PLAN_VERSION = 1

RUN_IDLE = "idle"
RUN_RUNNING = "running"
RUN_HOLDING = "holding"
RUN_AWAITING_OPERATOR = "awaiting_operator"
RUN_FINISHED = "finished"
RUN_ABORTED = "aborted"

TIMEOUT_ABORT = "abort"
TIMEOUT_HOLD = "hold"
TIMEOUT_OPERATOR = "request_operator"
TIMEOUT_ACTIONS = frozenset(
    (TIMEOUT_ABORT, TIMEOUT_HOLD, TIMEOUT_OPERATOR))

ABORT_ZERO_ALL = "zero_all"
ABORT_ZERO_FUEL = "zero_fuel"
ABORT_ACTIONS = frozenset((ABORT_ZERO_ALL, ABORT_ZERO_FUEL))

METRIC_KEYS = {
    "flow": "flow",
    "setpoint": "sp",
    "pressure": "press",
    "temperature": "temp",
    # Short internal spellings remain accepted for hand-written early drafts.
    "sp": "sp",
    "press": "press",
    "temp": "temp",
}
METRICS = frozenset(METRIC_KEYS)


class PlanValidationError(ValueError):
    """The plan is unsafe or structurally ambiguous and must not be loaded."""


def _number(value, field_name, *, minimum=None, positive=False):
    if isinstance(value, bool):
        raise PlanValidationError(f"{field_name} must be a finite number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PlanValidationError(
            f"{field_name} must be a finite number.") from exc
    if not math.isfinite(number):
        raise PlanValidationError(f"{field_name} must be finite.")
    if positive and number <= 0.0:
        raise PlanValidationError(f"{field_name} must be greater than zero.")
    if minimum is not None and number < minimum:
        raise PlanValidationError(
            f"{field_name} must be at least {minimum:g}.")
    return number


def _setpoints(raw, field_name):
    if not isinstance(raw, Mapping) or not raw:
        raise PlanValidationError(
            f"{field_name} must contain at least one role and setpoint.")
    cleaned = {}
    for role, value in raw.items():
        key = str(role).strip()
        if not key:
            raise PlanValidationError(f"{field_name} contains an empty role.")
        cleaned[key] = _number(
            value, f"{field_name}.{key}", minimum=0.0)
    return cleaned


def _targets(raw, field_name):
    if not isinstance(raw, Mapping) or not raw:
        raise PlanValidationError(
            f"{field_name} must contain at least one role and target.")
    cleaned = {}
    for role, value in raw.items():
        key = str(role).strip()
        if not key:
            raise PlanValidationError(f"{field_name} contains an empty role.")
        cleaned[key] = _number(value, f"{field_name}.{key}")
    return cleaned


@dataclass(frozen=True)
class AbortProcedure:
    """Required fail action for a plan.

    The first format intentionally exposes only the session's two verified-zero
    operations.  Multi-step burner-specific shutdowns can be added as a later
    additive format version once their confirmation semantics are defined.
    """

    action: str

    def __post_init__(self):
        if self.action not in ABORT_ACTIONS:
            raise PlanValidationError(
                "abort.action must be 'zero_all' or 'zero_fuel'.")

    def to_dict(self):
        return {"action": self.action}

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, Mapping):
            raise PlanValidationError(
                "abort is required and must declare a verified-zero action.")
        return cls(action=str(raw.get("action", "")))


@dataclass(frozen=True)
class StabilityCondition:
    """Require every declared role to stay within tolerance continuously."""

    metric: str = "flow"
    targets: dict[str, float] = field(default_factory=dict)
    tolerance: float = 0.05
    stable_s: float = 5.0

    def __post_init__(self):
        if self.metric not in METRICS:
            raise PlanValidationError(
                f"condition.metric must be one of {', '.join(sorted(METRICS))}.")
        object.__setattr__(self, "targets", _targets(
            self.targets, "condition.targets"))
        object.__setattr__(self, "tolerance", _number(
            self.tolerance, "condition.tolerance", minimum=0.0))
        object.__setattr__(self, "stable_s", _number(
            self.stable_s, "condition.stable_s", minimum=0.0))

    def evaluate(self, telemetry):
        """Return ``(satisfied, reason)`` against ``{role: {metric: value}}``."""
        if not isinstance(telemetry, Mapping):
            return False, "telemetry is unavailable"
        for role, wanted in self.targets.items():
            reading = telemetry.get(role)
            if not isinstance(reading, Mapping):
                return False, f"{role} has no telemetry"
            value = reading.get(METRIC_KEYS[self.metric])
            try:
                actual = float(value)
            except (TypeError, ValueError):
                return False, f"{role} has no {self.metric} reading"
            if not math.isfinite(actual):
                return False, f"{role} has a non-finite {self.metric} reading"
            if abs(actual - wanted) > self.tolerance:
                return False, (
                    f"{role} {self.metric} is {actual:.3g}; "
                    f"waiting for {wanted:.3g} ± {self.tolerance:.3g}")
        return True, "all readings are within tolerance"

    def to_dict(self):
        return {
            "type": "all_within",
            "metric": self.metric,
            "targets": dict(self.targets),
            "tolerance": self.tolerance,
            "stable_s": self.stable_s,
        }

    @classmethod
    def from_dict(cls, raw, default_targets=None):
        if not isinstance(raw, Mapping):
            raise PlanValidationError("condition must be an object.")
        kind = str(raw.get("type", "all_within"))
        if kind != "all_within":
            raise PlanValidationError(
                "condition.type must be 'all_within' in plan format v1.")
        targets = raw.get("targets", default_targets)
        return cls(
            metric=str(raw.get("metric", "flow")),
            targets=targets,
            tolerance=raw.get("tolerance", 0.05),
            stable_s=raw.get("stable_s", 5.0))


@dataclass(frozen=True)
class PlanStage:
    name: str
    setpoints: dict[str, float] = field(default_factory=dict)
    sequence: str | None = None
    condition: StabilityCondition | None = None
    min_dwell_s: float = 0.0
    timeout_s: float = 60.0
    on_timeout: str = TIMEOUT_ABORT
    next_stage: str | None = None

    def __post_init__(self):
        name = str(self.name).strip()
        if not name:
            raise PlanValidationError("Every stage must have a name.")
        object.__setattr__(self, "name", name)
        sequence = str(self.sequence).strip() if self.sequence else None
        object.__setattr__(self, "sequence", sequence)
        if bool(self.setpoints) == bool(sequence):
            raise PlanValidationError(
                f"Stage '{name}' must declare exactly one of setpoints or sequence.")
        if self.setpoints:
            object.__setattr__(self, "setpoints", _setpoints(
                self.setpoints, f"stages.{name}.setpoints"))
        object.__setattr__(self, "min_dwell_s", _number(
            self.min_dwell_s, f"stages.{name}.min_dwell_s", minimum=0.0))
        object.__setattr__(self, "timeout_s", _number(
            self.timeout_s, f"stages.{name}.timeout_s", positive=True))
        if self.timeout_s < self.min_dwell_s:
            raise PlanValidationError(
                f"Stage '{name}' timeout must not be shorter than its minimum dwell.")
        if self.on_timeout not in TIMEOUT_ACTIONS:
            raise PlanValidationError(
                f"Stage '{name}' has an unsupported timeout action.")

    def to_dict(self):
        raw = {
            "name": self.name,
            "min_dwell_s": self.min_dwell_s,
            "timeout_s": self.timeout_s,
            "on_timeout": self.on_timeout,
        }
        if self.setpoints:
            raw["setpoints"] = dict(self.setpoints)
        else:
            raw["sequence"] = self.sequence
        if self.condition is not None:
            raw["condition"] = self.condition.to_dict()
        if self.next_stage is not None:
            raw["next"] = self.next_stage
        return raw

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, Mapping):
            raise PlanValidationError("Every stage must be an object.")
        setpoints = raw.get("setpoints") or {}
        condition_raw = raw.get("condition")
        condition = (StabilityCondition.from_dict(
            condition_raw, default_targets=setpoints)
                     if condition_raw is not None else None)
        return cls(
            name=str(raw.get("name", "")),
            setpoints=setpoints,
            sequence=raw.get("sequence"),
            condition=condition,
            min_dwell_s=raw.get("min_dwell_s", 0.0),
            timeout_s=raw.get("timeout_s", 60.0),
            on_timeout=str(raw.get("on_timeout", TIMEOUT_ABORT)),
            next_stage=raw.get("next"))


@dataclass(frozen=True)
class ExperimentPlan:
    name: str
    abort: AbortProcedure
    stages: tuple[PlanStage, ...]
    notes: str = ""

    def __post_init__(self):
        name = str(self.name).strip()
        if not name:
            raise PlanValidationError("Plan name is required.")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "stages", tuple(self.stages))
        if not self.stages:
            raise PlanValidationError("A plan must contain at least one stage.")
        names = [stage.name for stage in self.stages]
        if len(names) != len(set(names)):
            raise PlanValidationError("Stage names must be unique.")
        known = set(names)
        for stage in self.stages:
            if stage.next_stage is not None and stage.next_stage not in known:
                raise PlanValidationError(
                    f"Stage '{stage.name}' points to unknown stage "
                    f"'{stage.next_stage}'.")
        self._reject_cycles()

    def _reject_cycles(self):
        positions = {stage.name: index for index, stage in enumerate(self.stages)}
        for start in self.stages:
            seen = set()
            stage = start
            while stage is not None:
                if stage.name in seen:
                    raise PlanValidationError(
                        "Plan format v1 does not allow stage cycles.")
                seen.add(stage.name)
                if stage.next_stage is not None:
                    stage = self.stages[positions[stage.next_stage]]
                else:
                    index = positions[stage.name] + 1
                    stage = self.stages[index] if index < len(self.stages) else None

    def to_dict(self):
        return {
            "format": PLAN_FORMAT,
            "version": PLAN_VERSION,
            "name": self.name,
            "notes": self.notes,
            "abort": self.abort.to_dict(),
            "stages": [stage.to_dict() for stage in self.stages],
        }

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, Mapping) or raw.get("format") != PLAN_FORMAT:
            raise PlanValidationError("Not a flow-controller experiment plan.")
        try:
            version = int(raw.get("version", 0))
        except (TypeError, ValueError) as exc:
            raise PlanValidationError("Plan version must be an integer.") from exc
        if version != PLAN_VERSION:
            raise PlanValidationError(
                f"Unsupported experiment-plan version {version}.")
        return cls(
            name=str(raw.get("name", "")),
            notes=str(raw.get("notes", "")),
            abort=AbortProcedure.from_dict(raw.get("abort")),
            stages=tuple(PlanStage.from_dict(stage)
                         for stage in raw.get("stages", ())))

    @classmethod
    def load(cls, path):
        path = Path(path)
        try:
            with path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanValidationError(f"Could not read plan: {exc}") from exc
        return cls.from_dict(raw)

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, allow_nan=False)
        return path

    def validate_for(self, *, roles=None, ceilings=None):
        """Reject unknown roles and commands above declared ceilings."""
        known = set(roles) if roles is not None else None
        limits = dict(ceilings or {})
        errors = []
        for stage in self.stages:
            referenced_roles = set(stage.setpoints)
            if stage.condition is not None:
                referenced_roles.update(stage.condition.targets)
            for role in referenced_roles:
                if known is not None and role not in known:
                    errors.append(f"Stage '{stage.name}' uses unknown role '{role}'.")
            for role, value in stage.setpoints.items():
                ceiling = limits.get(role)
                if ceiling is not None and value > float(ceiling):
                    errors.append(
                        f"Stage '{stage.name}' requests {value:g} for {role}, "
                        f"above its {float(ceiling):g} limit.")
        if errors:
            raise PlanValidationError("\n".join(errors))
        return True


@dataclass(frozen=True)
class PlanEvent:
    kind: str
    stage: str | None = None
    detail: str = ""
    setpoints: dict[str, float] = field(default_factory=dict)
    sequence: str | None = None
    abort_action: str | None = None


class ExperimentPlanRunner:
    """Clock-driven state machine that emits declarative actions."""

    def __init__(self, plan, *, clock):
        if not isinstance(plan, ExperimentPlan):
            raise TypeError("plan must be an ExperimentPlan")
        self.plan = plan
        self._clock = clock
        self.state = RUN_IDLE
        self.stage_index = -1
        self.stage_started_at = None
        self.stable_started_at = None
        self.reason = ""
        self._positions = {
            stage.name: index for index, stage in enumerate(plan.stages)}

    @property
    def stage(self):
        if 0 <= self.stage_index < len(self.plan.stages):
            return self.plan.stages[self.stage_index]
        return None

    def start(self):
        if self.state != RUN_IDLE:
            return []
        self.state = RUN_RUNNING
        self.stage_index = 0
        return [self._enter(self._clock())]

    def tick(self, telemetry=None):
        if self.state != RUN_RUNNING or self.stage is None:
            return []
        now = self._clock()
        elapsed = max(0.0, now - self.stage_started_at)
        stage = self.stage

        if elapsed >= stage.timeout_s:
            if stage.on_timeout == TIMEOUT_ABORT:
                return self.abort(
                    f"Stage '{stage.name}' timed out after {stage.timeout_s:g} s.")
            self.reason = (
                f"Stage '{stage.name}' timed out after {stage.timeout_s:g} s.")
            if stage.on_timeout == TIMEOUT_HOLD:
                self.state = RUN_HOLDING
                kind = "held"
            else:
                self.state = RUN_AWAITING_OPERATOR
                kind = "operator_required"
            return [PlanEvent(kind, stage.name, self.reason)]

        ready = elapsed >= stage.min_dwell_s
        detail = "minimum dwell has not elapsed"
        if stage.condition is not None:
            satisfied, detail = stage.condition.evaluate(telemetry)
            if satisfied:
                if self.stable_started_at is None:
                    self.stable_started_at = now
                stable_for = max(0.0, now - self.stable_started_at)
                ready = ready and stable_for >= stage.condition.stable_s
                if not ready:
                    detail = (
                        f"readings stable for {stable_for:.2f} of "
                        f"{stage.condition.stable_s:g} s")
            else:
                self.stable_started_at = None
                ready = False
        if not ready:
            self.reason = detail
            return []
        return self._advance(now)

    def _advance(self, now):
        stage = self.stage
        events = [PlanEvent("stage_completed", stage.name)]
        if stage.next_stage is not None:
            next_index = self._positions[stage.next_stage]
        else:
            next_index = self.stage_index + 1
        if next_index >= len(self.plan.stages):
            self.state = RUN_FINISHED
            self.reason = "Plan finished."
            events.append(PlanEvent("finished", stage.name, self.reason))
            return events
        self.stage_index = next_index
        events.append(self._enter(now))
        return events

    def _enter(self, now):
        stage = self.stage
        self.stage_started_at = now
        self.stable_started_at = None
        self.reason = ""
        return PlanEvent(
            "stage_entered", stage.name,
            setpoints=dict(stage.setpoints), sequence=stage.sequence)

    def abort(self, reason="Plan aborted by the operator."):
        if self.state in (RUN_IDLE, RUN_FINISHED, RUN_ABORTED):
            return []
        stage_name = self.stage.name if self.stage is not None else None
        self.state = RUN_ABORTED
        self.reason = str(reason)
        return [PlanEvent(
            "abort", stage_name, self.reason,
            abort_action=self.plan.abort.action)]

    def resolve_operator(self, decision):
        """Resolve a hold/operator timeout with ``advance`` or ``abort``."""
        if self.state not in (RUN_HOLDING, RUN_AWAITING_OPERATOR):
            return []
        if decision == "abort":
            return self.abort("Plan aborted after timeout review.")
        if decision != "advance":
            raise ValueError("decision must be 'advance' or 'abort'")
        self.state = RUN_RUNNING
        return self._advance(self._clock())


class ExperimentPlanExecutor:
    """Execute runner events through injected, already-validated operations.

    This adapter remains free of Qt and hardware libraries.  A UI timer calls
    :meth:`tick`; tests can call it against a simulated rig.  The callback
    boundary also makes it impossible for plan parsing code to acquire a serial
    handle accidentally.
    """

    def __init__(self, runner, *, telemetry, set_role_setpoint,
                 start_sequence, sequence_active, abort_action, narrate=None):
        self.runner = runner
        self._telemetry = telemetry
        self._set_role_setpoint = set_role_setpoint
        self._start_sequence = start_sequence
        self._sequence_active = sequence_active
        self._abort_action = abort_action
        self._narrate = narrate or (lambda _message: None)
        self._stage_sequence = False

    @property
    def state(self):
        return self.runner.state

    def start(self):
        return self._dispatch(self.runner.start())

    def tick(self):
        # A saved sequence is itself the stage's entry transition.  The plan
        # clock still advances for timeout accounting, but it cannot complete
        # the stage until replay has finished.
        if self._stage_sequence and self._sequence_active():
            now = self.runner._clock()
            stage = self.runner.stage
            if (stage is not None
                    and now - self.runner.stage_started_at >= stage.timeout_s):
                return self._dispatch(self.runner.tick(self._telemetry()))
            return []
        self._stage_sequence = False
        return self._dispatch(self.runner.tick(self._telemetry()))

    def abort(self, reason="Plan aborted by the operator."):
        return self._dispatch(self.runner.abort(reason))

    def resolve_operator(self, decision):
        return self._dispatch(self.runner.resolve_operator(decision))

    def _dispatch(self, events):
        delivered = []
        for event in events:
            delivered.append(event)
            if event.kind == "stage_entered":
                self._narrate(f"Plan stage started: {event.stage}.")
                if event.sequence:
                    self._stage_sequence = True
                    if not self._start_sequence(event.sequence):
                        return delivered + self.abort(
                            f"Could not start sequence '{event.sequence}'.")
                else:
                    for role, value in event.setpoints.items():
                        if not self._set_role_setpoint(role, value):
                            return delivered + self.abort(
                                f"Setpoint {role}={value:g} was refused.")
            elif event.kind == "stage_completed":
                self._narrate(f"Plan stage completed: {event.stage}.")
            elif event.kind == "abort":
                self._stage_sequence = False
                self._narrate(event.detail)
                if not self._abort_action(event.abort_action):
                    self._narrate(
                        f"ERROR: abort action {event.abort_action} was refused.")
            elif event.kind in ("held", "operator_required", "finished"):
                self._narrate(event.detail)
        return delivered
