"""Qt-thread adapter between experiment plans and :class:`FlowSession`."""

from __future__ import annotations

import time
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt, Signal

from .experiment_plan import (
    ABORT_ZERO_ALL,
    ABORT_ZERO_FUEL,
    ExperimentPlan,
    ExperimentPlanExecutor,
    ExperimentPlanRunner,
    PlanValidationError,
    RUN_ABORTED,
    RUN_AWAITING_OPERATOR,
    RUN_FINISHED,
    RUN_HOLDING,
    RUN_IDLE,
    RUN_RUNNING,
    TIMEOUT_ABORT,
)
from .sequence import Sequence, opening_mismatches


PLAN_TICK_MS = 100
ACTIVE_STATES = frozenset(
    (RUN_RUNNING, RUN_HOLDING, RUN_AWAITING_OPERATOR))


class ExperimentPlanController(QObject):
    """Own one reviewed plan while delegating all commands to ``FlowSession``."""

    plan_changed = Signal(object)
    state_changed = Signal(str)
    stage_changed = Signal(str, int, int)
    attention_required = Signal(str)

    def __init__(self, session, parent=None, *, clock=time.monotonic,
                 watchdog_factory=threading.Timer):
        super().__init__(parent)
        self.session = session
        self.plan = None
        self.plan_path = None
        self._clock = clock
        self._executor = None
        self._active_plan = None
        self._sequence_stage_active = False
        self._frozen_sequences = {}
        self._agent_started = False
        self._watchdog_factory = watchdog_factory
        self._watchdog_timer = None
        self._watchdog_generation = 0
        self._watchdog_lock = threading.Lock()
        self._abort_request = None

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(PLAN_TICK_MS)
        self._timer.timeout.connect(self.tick)

        session.monitoring_changed.connect(self._on_monitoring)
        session.assignments_changed.connect(self._on_assignments_changed)
        session.connection_changed.connect(self._on_connection)
        session.communication_fault.connect(self._on_communication_fault)
        session.max_flow_changed.connect(self._on_live_limit_changed)
        session.unit_ramp_changed.connect(self._on_live_ramp_changed)
        session.zero_started.connect(self._on_external_zero)
        session.sequence_ended.connect(self._on_sequence_ended)

    @property
    def state(self):
        return self._executor.state if self._executor is not None else RUN_IDLE

    @property
    def is_active(self):
        return self.state in ACTIVE_STATES

    @property
    def stage(self):
        return (self._executor.runner.stage
                if self._executor is not None else None)

    @property
    def reason(self):
        return (self._executor.runner.reason
                if self._executor is not None else "")

    def load(self, path):
        if self.is_active:
            self.session.failed.emit(
                "Experiment Plan", "Abort the running plan before loading another.")
            return None
        try:
            plan = ExperimentPlan.load(path)
            self._validate(plan, plan_path=path)
        except (PlanValidationError, OSError, ValueError) as exc:
            self.session.failed.emit(
                "Experiment Plan", f"Could not load that plan:\n\n{exc}")
            return None
        self.plan = plan
        self.plan_path = Path(path)
        self._executor = None
        self._active_plan = None
        self._frozen_sequences = {}
        self._agent_started = False
        self.plan_changed.emit(plan)
        self.state_changed.emit(RUN_IDLE)
        self.session._log(
            f"Experiment plan loaded: '{plan.name}' ({len(plan.stages)} stage(s)).")
        return plan

    def set_plan(self, plan, *, path=None):
        if self.is_active:
            return False
        try:
            self._validate(plan, plan_path=path)
        except PlanValidationError as exc:
            self.session.failed.emit("Experiment Plan", str(exc))
            return False
        self.plan = plan
        self.plan_path = Path(path) if path else None
        self._executor = None
        self._active_plan = None
        self._frozen_sequences = {}
        self._agent_started = False
        self.plan_changed.emit(plan)
        self.state_changed.emit(RUN_IDLE)
        return True

    def _validate(self, plan, *, plan_path=None, frozen_sequences=None):
        if not isinstance(plan, ExperimentPlan):
            raise PlanValidationError("That is not an experiment plan.")
        roles = {key for key, unit in self.session.assignments.items() if unit}
        roles.update(self.session.custom_assignments.values())
        ceilings = {}
        for role in roles:
            unit = self.session.unit_for_role(role)
            maximum = self.session.max_flow_for(unit) if unit else None
            if maximum is not None:
                ceilings[role] = maximum
        plan.validate_for(roles=roles, ceilings=ceilings)
        for stage in plan.stages:
            if stage.sequence:
                if frozen_sequences is None:
                    path = self._resolve_sequence(
                        stage.sequence, plan_path=plan_path)
                    sequence = Sequence.load(path)
                else:
                    raw = frozen_sequences.get(str(stage.sequence))
                    if raw is None:
                        raise PlanValidationError(
                            f"Stage '{stage.name}' is missing its armed sequence "
                            f"snapshot: {stage.sequence}")
                    sequence = Sequence.from_dict(raw)
                errors = self.session.sequence_validation_errors(sequence)
                if errors:
                    raise PlanValidationError(
                        f"Stage '{stage.name}' has an invalid sequence:\n"
                        + "\n".join(errors))
        return True

    def start(self, *, frozen_plan=None, frozen_sequences=None,
              agent_started=False):
        if self.plan is None:
            self.session.failed.emit(
                "Experiment Plan", "Load or submit a plan first.")
            return False
        if self.is_active:
            return False
        if not self.session.controllers_connected or not self.session.is_monitoring:
            self.session.failed.emit(
                "Experiment Plan", "Connect the controllers and start monitoring first.")
            return False
        if self.session.sequence_state != "idle":
            self.session.failed.emit(
                "Experiment Plan", "Stop the current recording or replay first.")
            return False
        if self.session.ignition_state != "IDLE":
            self.session.failed.emit(
                "Experiment Plan", "Finish or abort the ignition sequence first.")
            return False
        if agent_started and (frozen_plan is None or frozen_sequences is None):
            self.session.failed.emit(
                "Experiment Plan",
                "An agent-started plan requires the exact armed plan bundle.")
            return False
        try:
            execution_plan = (
                ExperimentPlan.from_dict(frozen_plan)
                if frozen_plan is not None else self.plan)
            self._validate(
                execution_plan, plan_path=self.plan_path,
                frozen_sequences=frozen_sequences)
            if frozen_sequences is None:
                captured_sequences = {}
                for stage in execution_plan.stages:
                    if stage.sequence:
                        sequence = Sequence.load(
                            self._resolve_sequence(stage.sequence))
                        errors = self.session.sequence_validation_errors(sequence)
                        if errors:
                            raise PlanValidationError("\n".join(errors))
                        captured_sequences[stage.sequence] = Sequence.from_dict(
                            sequence.to_dict())
            else:
                captured_sequences = {
                    str(reference): Sequence.from_dict(raw)
                    for reference, raw in frozen_sequences.items()
                }
        except (PlanValidationError, OSError, ValueError) as exc:
            self.session.failed.emit("Experiment Plan", str(exc))
            return False
        self._frozen_sequences = captured_sequences
        self._agent_started = bool(agent_started)
        self._active_plan = execution_plan

        include_air = execution_plan.abort.action == ABORT_ZERO_ALL
        self._abort_request = self.session.make_zero_request(
            include_air=include_air,
            scope=f"plan_watchdog_{'all' if include_air else 'fuel'}")
        if not self._abort_request.units:
            self._frozen_sequences = {}
            self._agent_started = False
            self._active_plan = None
            self.session.failed.emit(
                "Experiment Plan",
                "The declared abort procedure has no selected controllers to zero.")
            return False

        runner = ExperimentPlanRunner(execution_plan, clock=self._clock)
        self._executor = ExperimentPlanExecutor(
            runner,
            telemetry=self._role_telemetry,
            set_role_setpoint=self.session.set_role_setpoint,
            start_sequence=self._start_sequence,
            sequence_active=lambda: self._sequence_stage_active,
            abort_action=self._execute_abort,
            narrate=self.session._log,
        )
        self.session._log(
            f"Experiment plan started: '{execution_plan.name}'. Abort procedure: "
            f"{execution_plan.abort.action}.")
        events = self._executor.start()
        self._publish(events)
        if self.state == RUN_RUNNING:
            self._timer.start()
            return True
        return False

    def tick(self):
        if self._executor is None or self.state != RUN_RUNNING:
            self._timer.stop()
            return []
        events = self._executor.tick()
        self._publish(events)
        return events

    def abort(self, reason="Experiment plan aborted by the operator."):
        if self._executor is None or not self.is_active:
            return False
        events = self._executor.abort(reason)
        self._publish(events)
        return bool(events)

    def resolve_timeout(self, decision):
        if self._executor is None:
            return False
        events = self._executor.resolve_operator(decision)
        self._publish(events)
        if self.state == RUN_RUNNING:
            self._timer.start()
        return bool(events)

    def _publish(self, events):
        for event in events:
            if event.kind == "stage_entered":
                stage = self._executor.runner.stage
                self.stage_changed.emit(
                    event.stage or "", self._executor.runner.stage_index + 1,
                    len(self._active_plan.stages))
                self._arm_stage_watchdog(stage)
            elif event.kind in ("held", "operator_required"):
                self.attention_required.emit(event.detail)
        state = self.state
        self.state_changed.emit(state)
        if state not in (RUN_RUNNING,):
            self._timer.stop()
            self._cancel_stage_watchdog()
            if state in (RUN_FINISHED, RUN_ABORTED):
                self._frozen_sequences = {}
                self._agent_started = False
                self._active_plan = None

    def _cancel_stage_watchdog(self):
        with self._watchdog_lock:
            self._watchdog_generation += 1
            timer = self._watchdog_timer
            self._watchdog_timer = None
        if timer is not None:
            timer.cancel()

    def _arm_stage_watchdog(self, stage):
        """Arm a non-Qt safe-abort deadline for the newly entered stage."""
        self._cancel_stage_watchdog()
        if stage is None or stage.on_timeout != TIMEOUT_ABORT:
            return
        with self._watchdog_lock:
            self._watchdog_generation += 1
            token = self._watchdog_generation
            request = self._abort_request

            def fire():
                with self._watchdog_lock:
                    if token != self._watchdog_generation:
                        return
                reason = (
                    f"Stage '{stage.name}' timed out after "
                    f"{stage.timeout_s:g} s while the UI watchdog was armed.")
                if self.session.enqueue_watchdog_zero(request, reason):
                    self.session._post(
                        self._complete_watchdog_abort, token,
                        tuple(request.units), reason)

            timer = self._watchdog_factory(stage.timeout_s, fire)
            timer.daemon = True
            self._watchdog_timer = timer
        timer.start()

    def _complete_watchdog_abort(self, token, units, reason):
        """Finish plan state cleanup after the watchdog has already zeroed."""
        try:
            with self._watchdog_lock:
                current = token == self._watchdog_generation
            if not current or self._executor is None or not self.is_active:
                return
            if self.session.sequence_state == "replaying":
                self.session.stop_replay(
                    reason="cancelled by experiment-plan watchdog")
            events = self._executor.runner.abort(reason)
            self.session._log(reason)
            self._publish(events)
        finally:
            self.session.release_watchdog_zero_lock(units)

    def _role_telemetry(self):
        timestamp = self.session._latest_timestamp
        if timestamp is None:
            return {}
        age = max(0.0, (datetime.now() - timestamp).total_seconds())
        freshness_s = max(1.0, 3.0 * max(0.1, self.session.poll_interval_s))
        if age > freshness_s:
            return {}
        samples = self.session.latest_samples() or {}
        result = {}
        for role, unit in self.session.assignments.items():
            if unit:
                result[role] = dict(samples.get(unit, {}) or {})
        for unit, role in self.session.custom_assignments.items():
            result[role] = dict(samples.get(unit, {}) or {})
        return result

    def _resolve_sequence(self, raw, *, plan_path=None):
        path = Path(raw)
        if not path.is_absolute():
            owner = Path(plan_path) if plan_path else self.plan_path
            base = owner.parent if owner else self.session.sequence_dir
            path = base / path
        return path

    def _start_sequence(self, raw):
        frozen = self._frozen_sequences.get(raw)
        if frozen is not None:
            sequence = Sequence.from_dict(frozen.to_dict())
        else:
            path = self._resolve_sequence(raw)
            try:
                sequence = Sequence.load(path)
            except (OSError, ValueError) as exc:
                self.session._log(f"Plan sequence could not be loaded: {exc}")
                return False
        errors = self.session.sequence_validation_errors(sequence)
        if errors:
            self.session._log("Plan sequence rejected: " + "; ".join(errors))
            return False
        measured = {
            track.key: self.session.flow_for_role(track.key)
            for track in sequence.tracks
        }
        mismatches = opening_mismatches(sequence, measured)
        if mismatches:
            names = ", ".join(track.label for track, _wanted, _actual in mismatches)
            self.session._log(
                f"Plan sequence opening condition rejected; flows differ on {names}.")
            return False
        self.session.set_sequence(sequence)
        started = self.session.start_replay(sequence, repeats=1)
        self._sequence_stage_active = bool(started)
        return bool(started)

    def _on_sequence_ended(self, finished, reason):
        if not self._sequence_stage_active:
            return
        self._sequence_stage_active = False
        if not finished and self.is_active:
            self.abort(f"Plan sequence {reason}.")

    def _execute_abort(self, action):
        if self.session.sequence_state == "replaying":
            self.session.stop_replay(reason="cancelled by experiment-plan abort")
        if action == ABORT_ZERO_ALL:
            return bool(self.session.zero_all())
        if action == ABORT_ZERO_FUEL:
            return bool(self.session.zero_fuel())
        return False

    def _on_external_zero(self, request):
        # The runner may already be RUN_ABORTED because its own abort action
        # emitted this signal.  A manual zero instead revokes the plan without
        # recursively issuing a second zero.
        if str(getattr(request, "scope", "")).startswith("plan_watchdog_"):
            return
        if self._executor is None or not self.is_active:
            return
        events = self._executor.runner.abort(
            "Experiment plan cancelled by a zero-flow command.")
        self._publish(events)

    def _on_monitoring(self, active):
        if not active and self.is_active:
            self.abort("Experiment plan aborted because monitoring stopped.")

    def _on_connection(self, connected):
        if not connected and self.is_active:
            self.abort("Experiment plan aborted because controllers disconnected.")

    def _on_assignments_changed(self, _assignments):
        if self.is_active:
            self.abort("Experiment plan aborted because assignments changed.")

    def _on_communication_fault(self, detail):
        if self.is_active:
            self.abort(
                f"Experiment plan aborted after communication fault: {detail}")

    def _on_live_limit_changed(self, _unit, _value):
        if self.is_active and self._agent_started:
            self.abort(
                "Agent-started experiment plan aborted because MAX FLOW changed.")

    def _on_live_ramp_changed(self, _unit, _value):
        if self.is_active and self._agent_started:
            self.abort(
                "Agent-started experiment plan aborted because ramp settings changed.")

    def shutdown(self):
        self._timer.stop()
        self._cancel_stage_watchdog()
        self._frozen_sequences = {}
        self._agent_started = False
        self._active_plan = None
        self._sequence_stage_active = False
        if self._executor is not None and self.is_active:
            # Application shutdown retains its established explicit-zero
            # semantics; MainWindow already warns that closing is not ZERO ALL.
            self._executor.runner.abort("Experiment plan cancelled at shutdown.")
            self.state_changed.emit(RUN_ABORTED)
