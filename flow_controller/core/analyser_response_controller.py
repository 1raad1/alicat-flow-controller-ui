"""Guarded Qt orchestration for a two-condition NO response test."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
# Wall-clock seconds are compared with acquisition timestamps for timeouts.
import time

from PySide6.QtCore import QObject, QTimer, Signal

from .optimisation import FLOW_ABS_TOL, FLOW_REL_TOL
from ..domain.analyser_response import AnalyserResponseDetector
from ..domain.bayesian import finite
from ..domain.roles import RAMP_KEYS, ROLE_LABELS
from ..mexa.records import epoch


FLOW_STABLE_HOLD_S = 3.0
BASELINE_TIMEOUT_S = 120.0


class AnalyserResponseController(QObject):
    """Measure one A-to-B system response using validated live NO samples.

    The only automatic actuation is the explicitly confirmed transition to
    condition B. Every command uses :meth:`FlowSession.set_role_setpoint`, so
    controller ceilings, zero locks and configured ramp policies remain in
    force. Cancelling never sends a recovery condition.
    """

    changed = Signal()
    message = Signal(str)
    progress = Signal(str)

    def __init__(self, session, owner, parent=None, *, wall_clock=time.time):
        super().__init__(parent)
        self.session = session
        self.owner = owner
        self._wall_clock = wall_clock
        self.detector = None
        self.phase = "idle"
        self.flow_reached_at = None
        self._flow_hold_started = None
        self._condition_a = None
        self._condition_b = None
        self._log_path = ""
        self._started_at = None
        self.last_message = "Store live conditions A and B to measure the NO response."

        session.samples_updated.connect(self._flow_sample)
        session.mexa.sample_received.connect(self._mexa_sample)
        session.mexa.interrupted.connect(
            lambda reason: self._interrupt(f"MEXA interrupted: {reason}"))
        for signal in (session.assignments_changed, session.mode_changed,
                       session.connection_changed, session.monitoring_changed,
                       session.communication_fault, session.sequence_state_changed,
                       session.max_flow_changed, session.unit_ramp_changed,
                       session.zero_started):
            signal.connect(self._configuration_changed)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)

    @property
    def experiment(self):
        return self.owner.experiment

    @property
    def active(self):
        return self.phase in ("baseline", "response")

    def _say(self, text):
        self.last_message = str(text)
        self.message.emit(self.last_message)

    def condition(self, label):
        if self.experiment is None:
            return None
        return self.experiment.response_condition(label)

    def _require_available(self):
        if self.experiment is None:
            raise ValueError("Create or open a Bayesian experiment first.")
        if self.active:
            raise ValueError("Cancel the active response measurement first.")
        if self.owner.worker or self.owner.capture or self.owner.settle_wait:
            raise ValueError("Finish the optimiser calculation or measurement first.")
        if not self.session.controllers_connected or not self.session.is_monitoring:
            raise ValueError("Connect and monitor the flow controllers first.")
        if not self.session.estop_armed:
            raise ValueError("The flow safety controls are not armed.")
        if self.session.sequence_state != "idle" or self.session._ramps.active:
            raise ValueError("Finish sequences and ramps first.")
        if self.session.ignition_state != "IDLE":
            raise ValueError("Finish or abort the ignition sequence first.")

    def _require_fresh_flow(self):
        stamp = getattr(self.session, "_latest_timestamp", None)
        freshness = max(5.0, 3 * self.session.poll_interval_s)
        if stamp is None or not -1 <= (datetime.now() - stamp).total_seconds() <= freshness:
            raise ValueError("Fresh flow telemetry is required.")
        return stamp

    def _live_snapshot(self):
        self._require_available()
        self._require_fresh_flow()
        samples = self.session.latest_samples()
        targets = self.session.commanded_setpoints()
        measured, assignments, cleaned_targets = {}, {}, {}
        for meta in self.session.track_metas():
            if meta.key not in ROLE_LABELS:
                continue
            sample = samples.get(meta.unit, {})
            flow = finite(sample.get("flow"), f"Fresh flow for unit {meta.unit}")
            setpoint = finite(sample.get("sp"), f"Fresh setpoint for unit {meta.unit}")
            target = finite(targets.get(meta.key, setpoint), f"Setpoint for {meta.key}")
            tolerance = max(FLOW_ABS_TOL, FLOW_REL_TOL * max(abs(target), 1e-12))
            if abs(flow - target) > tolerance or abs(setpoint - target) > tolerance:
                raise ValueError(
                    f"{ROLE_LABELS[meta.key]} is not settled at its commanded setpoint.")
            cleaned_targets[meta.key] = target
            measured[meta.key] = flow
            assignments[meta.key] = meta.unit
        if not cleaned_targets:
            raise ValueError("Assign at least one supported burner role first.")
        request = self.session.autocalc_request
        return {
            "target_flows": cleaned_targets,
            "measured_flows": measured,
            "assignments": assignments,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            **({"request": asdict(request)} if request is not None else {}),
        }

    def store_condition(self, label):
        snapshot = self._live_snapshot()
        self.experiment.set_response_condition(label, snapshot)
        self._say(
            f"Condition {str(label).upper()} stored from settled live flows. "
            "Any earlier selected response calibration was cleared.")
        self.changed.emit()
        self.owner.changed.emit()
        return snapshot

    def _frozen_conditions(self):
        first, second = self.condition("A"), self.condition("B")
        if first is None or second is None:
            raise ValueError("Store both live conditions A and B first.")
        if (first["assignments"] != second["assignments"]
                or set(first["target_flows"]) != set(second["target_flows"])):
            raise ValueError("Conditions A and B use different controller assignments.")
        changed = [role for role in first["target_flows"]
                   if abs(first["target_flows"][role] - second["target_flows"][role]) > 1e-9]
        if not changed:
            raise ValueError("Conditions A and B are identical; an NO response cannot be measured.")
        return first, second, changed

    def transition_text(self):
        first, second, changed = self._frozen_conditions()
        lines = []
        for role in changed:
            unit = self.session.unit_for_role(role)
            if unit != first["assignments"][role]:
                raise ValueError("Current controller assignments differ from the stored conditions.")
            start, target = first["target_flows"][role], second["target_flows"][role]
            maximum = self.session.max_flow_for(unit)
            if maximum is not None and target > maximum:
                raise ValueError(
                    f"{ROLE_LABELS[role]} condition B exceeds the current MAX FLOW of {maximum:g} SLPM.")
            seconds = self.session.ramp_seconds_for(unit, role, target - start)
            if seconds > 0:
                policy = f"ramped over approximately {seconds:g} s"
            elif role in RAMP_KEYS:
                policy = "DIRECT STEP because RAMPING OFF is declared"
            else:
                policy = "direct step"
            lines.append(
                f"{ROLE_LABELS[role]} / unit {unit}: {start:g} → {target:g} SLPM ({policy})")
        return "\n".join(lines)

    def _matches(self, snapshot):
        samples = self.session.latest_samples()
        for role, target in snapshot["target_flows"].items():
            unit = self.session.unit_for_role(role)
            if unit != snapshot["assignments"].get(role):
                return False
            sample = samples.get(unit, {})
            try:
                flow = finite(sample.get("flow"), "flow")
                setpoint = finite(sample.get("sp"), "setpoint")
            except ValueError:
                return False
            tolerance = max(FLOW_ABS_TOL, FLOW_REL_TOL * max(abs(target), 1e-12))
            if abs(flow - target) > tolerance or abs(setpoint - target) > tolerance:
                return False
        return True

    def _inside_commanded_transition(self):
        """Reject telemetry outside the frozen A-to-B transition envelope."""
        samples = self.session.latest_samples()
        for role, start in self._condition_a["target_flows"].items():
            target = self._condition_b["target_flows"][role]
            unit = self.session.unit_for_role(role)
            sample = samples.get(unit, {})
            try:
                flow = finite(sample.get("flow"), "flow")
                setpoint = finite(sample.get("sp"), "setpoint")
            except ValueError:
                return False
            tolerance = max(FLOW_ABS_TOL, FLOW_REL_TOL * max(abs(start), abs(target), 1e-12))
            lower, upper = min(start, target) - tolerance, max(start, target) + tolerance
            if not lower <= flow <= upper or not lower <= setpoint <= upper:
                return False
        return True

    def start(self, *, confirmed=False):
        self._require_available()
        if not confirmed:
            raise ValueError("Confirm the displayed A-to-B transition before starting.")
        first, second, _changed = self._frozen_conditions()
        self.transition_text()  # recheck assignments, limits and ramp policy
        self._require_fresh_flow()
        if not self._matches(first):
            raise ValueError("Return the rig to stored condition A and let every flow settle first.")
        sample = self.session.mexa.checked_sample()
        self.detector = AnalyserResponseDetector()
        self.phase = "baseline"
        self.flow_reached_at = None
        self._flow_hold_started = None
        self._condition_a, self._condition_b = first, second
        self._log_path = sample.log_path
        self._started_at = self._wall_clock()
        self._timer.start()
        self._say(
            "Collecting a stable 15 s NO baseline at condition A. Condition B will be "
            "commanded automatically when the baseline passes its drift check.")
        self.changed.emit()
        self.owner.changed.emit()
        return True

    def _command_b(self, latest_sample):
        if not self._matches(self._condition_a):
            raise ValueError("Condition A moved before the step; response measurement cancelled.")
        self.transition_text()
        # Use the last packet time for response timestamps; use local elapsed
        # time for the watchdog so clock offsets cannot cause false timeouts.
        command_at = epoch(latest_sample.packet["acquired_at"])
        self.detector.begin_step(command_at)
        self._started_at = self._wall_clock()
        changed = [role for role in self._condition_b["target_flows"]
                   if abs(self._condition_a["target_flows"][role]
                          - self._condition_b["target_flows"][role]) > 1e-9]
        for role in changed:
            if not self.session.set_role_setpoint(
                    role, self._condition_b["target_flows"][role]):
                raise ValueError(
                    f"The session refused {ROLE_LABELS[role]}; the transition may be partial. "
                    "Use the established operator recovery procedure.")
        self.phase = "response"
        self._say(
            "Condition B commanded through the normal setpoint/ramp path. Waiting for "
            "a sustained NO change, settled B flows and a stable final plateau.")
        self.changed.emit()

    def _mexa_sample(self, sample):
        if not self.active:
            return
        try:
            problem = sample.problem(experimental=True)
            if problem:
                raise ValueError(problem)
            if not sample.log_path or sample.log_path != self._log_path:
                raise ValueError("The MEXA receiver log or acquisition changed.")
            packet = sample.packet
            result = self.detector.add_sample(
                epoch(packet["acquired_at"]), packet["no_ppm"],
                packet["source_id"], packet["seq"],
                allow_stable=self.flow_reached_at is not None)
            if self.phase == "baseline":
                metrics = self.detector.baseline_metrics
                if self.detector.baseline_ready:
                    self._command_b(sample)
                elif metrics:
                    self.progress.emit(
                        f"Baseline: {metrics.span_s:.0f} / 15 s; "
                        f"NO {metrics.mean_ppm:.2f} ppm; drift "
                        f"{metrics.slope_ppm_per_s:+.3f} ppm/s")
            elif result is not None:
                self._finish(result)
            else:
                elapsed = epoch(packet["acquired_at"]) - self.detector.command_at
                state = "change detected" if self.detector.change_detected else "waiting for NO change"
                flow = "B flows stable" if self.flow_reached_at is not None else "waiting for B flows"
                self.progress.emit(f"Response: {elapsed:.0f} s; {state}; {flow}.")
        except (ValueError, RuntimeError, OSError) as exc:
            self.cancel(f"Response measurement discarded: {exc}")

    def _flow_sample(self, _generation):
        if not self.active:
            return
        try:
            if self.phase == "baseline":
                if not self._matches(self._condition_a):
                    raise ValueError("Condition A did not remain settled during the baseline.")
                return
            if not self._inside_commanded_transition():
                raise ValueError("A flow or setpoint left the confirmed A-to-B transition envelope.")
            stamp = getattr(self.session, "_latest_timestamp", None)
            if stamp is None:
                raise ValueError("Fresh flow telemetry was lost.")
            timestamp = stamp.timestamp()
            if self._matches(self._condition_b):
                if self._flow_hold_started is None:
                    self._flow_hold_started = timestamp
                elif (self.flow_reached_at is None
                      and timestamp - self._flow_hold_started >= FLOW_STABLE_HOLD_S):
                    self.flow_reached_at = self._flow_hold_started
                    self.progress.emit("Condition B flows have remained settled for 3 s.")
            else:
                if self.flow_reached_at is not None:
                    raise ValueError("Condition B flows moved after reaching the target.")
                self._flow_hold_started = None
        except ValueError as exc:
            self.cancel(f"Response measurement discarded: {exc}")

    def _finish(self, result):
        if self.flow_reached_at is None:
            raise ValueError("The NO plateau completed before condition B flow stability was confirmed.")
        result = result.with_flow_reached_at(self.flow_reached_at)
        raw = []
        for sample in result.raw_accepted_samples:
            elapsed = sample.timestamp_s - result.command_at
            raw.append({
                "timestamp": sample.timestamp_s,
                "elapsed_s": elapsed,
                "no_ppm": sample.no_ppm,
                "seq": sample.sequence,
                "phase": "baseline" if elapsed <= 0 else "response",
                "flow_stable": bool(self.flow_reached_at
                                    and sample.timestamp_s >= self.flow_reached_at),
            })
        record = {
            "successful": True,
            "source_id": result.source_id,
            "source": "validated live MEXA NO stream",
            "provenance": self._log_path,
            "algorithm_version": "response-detector-1",
            "recommended_delay_s": result.recommended_delay_s,
            "command_to_change_s": result.command_to_change_s,
            "command_to_stable_s": result.command_to_stable_s,
            "flow_to_stable_s": result.flow_to_stable_s,
            "t10_s": result.t10_s, "t50_s": result.t50_s,
            "t90_s": result.t90_s, "rise_10_90_s": result.rise_10_90_s,
            "baseline_no_ppm": result.baseline.mean_ppm,
            "final_no_ppm": result.final.mean_ppm,
            "baseline_sd_ppm": result.baseline.sd_ppm,
            "final_sd_ppm": result.final.sd_ppm,
            "signed_amplitude_ppm": result.signed_amplitude_ppm,
            "sample_resolution_s": result.sample_resolution_s,
            "criteria": asdict(result.criteria),
            "caveat": " ".join(result.caveats),
            "raw_samples": raw,
        }
        stored = self.experiment.record_response_run(record)
        self.phase = "complete"
        self._timer.stop()
        self._say(
            f"Response measured: change after {result.command_to_change_s:.1f} s; "
            f"stable after {result.command_to_stable_s:.1f} s from command and "
            f"{result.flow_to_stable_s:.1f} s after B flows settled. Future live "
            f"windows will wait {result.recommended_delay_s:g} s, then average for "
            f"{self.experiment.config.window_seconds:g} s (total logged observation "
            f"{self.experiment.total_live_logging_seconds:g} s).")
        self.changed.emit()
        self.owner.changed.emit()
        return stored

    def _tick(self):
        if not self.active:
            self._timer.stop()
            return
        try:
            self.session.mexa.checked_sample()
            self._require_fresh_flow()
            if (self.phase == "baseline"
                    and self._wall_clock() - self._started_at > BASELINE_TIMEOUT_S):
                raise TimeoutError("A stable NO baseline was not found within 120 seconds.")
            if self.phase == "response":
                self.detector.check_timeout(self.detector.command_at + self._wall_clock() - self._started_at)
        except (ValueError, TimeoutError) as exc:
            self.cancel(f"Response measurement discarded: {exc}")

    def _configuration_changed(self, *_args):
        if self.active:
            self.cancel("Response measurement discarded because the rig configuration or run state changed.")

    def _interrupt(self, reason):
        if self.active:
            self.cancel(f"Response measurement discarded: {reason}")

    def cancel(self, reason="Response measurement cancelled. No recovery setpoints were sent."):
        if self.detector and self.detector.state not in ("completed", "failed", "cancelled"):
            self.detector.cancel(reason)
        self.phase = "idle"
        self.detector = None
        self.flow_reached_at = None
        self._flow_hold_started = None
        self._condition_a = self._condition_b = None
        self._log_path = ""
        self._started_at = None
        self._timer.stop()
        self._say(reason)
        self.changed.emit()
        self.owner.changed.emit()

    def shutdown(self):
        self._timer.stop()
        if self.active:
            self.cancel("Response measurement stopped during application shutdown.")
        return True
