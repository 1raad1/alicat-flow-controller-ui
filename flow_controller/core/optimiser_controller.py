"""Qt orchestration for Bayesian measurements and their analyser delay."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
import time

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from .analyser_response_controller import AnalyserResponseController
from .optimisation import Experiment, MeasurementWindow, FLOW_ABS_TOL, FLOW_REL_TOL
from ..domain.bayesian import finite, suggest
from ..domain.roles import ROLE_MAP
from mexa_bridge.records import LiveWindow


class SuggestionWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, config, trials, limits, parent=None):
        super().__init__(parent)
        self.config, self.trials, self.limits = config, deepcopy(trials), dict(limits)

    def run(self):
        try:
            answer = suggest(self.config, self.trials, self.limits, seed=1729 + len(self.trials))
            if not self.isInterruptionRequested():
                self.succeeded.emit(answer)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(f"Could not generate a suggestion: {exc}")


class OptimiserController(QObject):
    changed = Signal()
    message = Signal(str)
    progress = Signal(str)
    targets_ready = Signal(object)

    def __init__(self, session, parent=None, *, clock=time.monotonic):
        super().__init__(parent)
        self.session = session
        self.experiment = None
        self.worker = None
        self.capture = None
        self.mexa_capture = None
        self.settle_wait = None
        self._capture_delay = 0.0
        self._response_run_id = None
        self._clock = clock
        self.live_mode = False
        self.draft = {}
        self.expanded = False
        self.last_message = "Bayesian suggestions never send commands; the response test requires confirmation."
        self.response = AnalyserResponseController(session, self, self)
        session.samples_updated.connect(self._sample)
        session.mexa.sample_received.connect(self._mexa_sample)
        session.mexa.interrupted.connect(self._mexa_interrupted)
        self._freshness_timer = QTimer(self)
        self._freshness_timer.setInterval(500)
        self._freshness_timer.timeout.connect(self._check_live_freshness)
        self._freshness_timer.start()
        for signal in (session.assignments_changed, session.mode_changed,
                       session.connection_changed, session.monitoring_changed,
                       session.communication_fault, session.sequence_state_changed,
                       session.max_flow_changed, session.unit_ramp_changed,
                       session.zero_started):
            signal.connect(self._configuration_changed)

    @property
    def busy(self):
        # Retain the lock until the queued finished signal is handled, not just
        # until run() returns: a success event may still be waiting on the GUI.
        return self.worker is not None

    def _say(self, text):
        self.last_message = str(text)
        self.message.emit(self.last_message)

    def create(self, path, config):
        self._require_idle()
        self.experiment = Experiment.create(path, config)
        self.draft.clear()
        self._say("Experiment saved. Bounds and reporting basis are fixed for this campaign.")
        self.changed.emit()

    def load(self, path):
        self._require_idle()
        self.experiment = Experiment.load(path)
        self.draft.clear()
        self._say("Experiment opened. A pending test remains pending; no flows were loaded or sent.")
        self.changed.emit()

    def _require_idle(self):
        response = getattr(self, "response", None)
        if self.busy or self.capture or self.settle_wait or (response and response.active):
            raise ValueError("Finish the calculation or measurement window first.")

    def _require_experiment(self):
        if self.experiment is None:
            raise ValueError("Create or open an experiment first.")
        return self.experiment

    def limits(self):
        return {role: ceiling for role, unit in self.session.assignments.items() if unit
                and (ceiling := self.session.max_flow_for(unit)) is not None}

    def ask(self):
        self._require_idle()
        experiment = self._require_experiment()
        if experiment.pending:
            raise ValueError("Complete or mark the current test invalid first.")
        self.worker = SuggestionWorker(experiment.config, experiment.trials, self.limits(), self)
        self.worker.succeeded.connect(self._suggestion)
        self.worker.failed.connect(self._say)
        self.worker.finished.connect(self._worker_finished)
        self._say("Calculating a suggestion in the background. Flow control is unchanged.")
        self.changed.emit()
        self.worker.start()

    def _suggestion(self, answer):
        if self.worker is not None and self.worker.isInterruptionRequested():
            return
        try:
            # Recheck limits in case they changed while the worker was fitting.
            targets = self.experiment.config.targets(answer["point"])
            self._check_limits(targets)
            self.experiment.add_trial(answer)
            self.draft.clear()
            self._say("Suggestion saved. Review the point before loading its target fields.")
        except (ValueError, OSError) as exc:
            self._say(str(exc))
        self.changed.emit()

    def _worker_finished(self):
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.deleteLater()
        self.changed.emit()

    def repeat(self, trial_id):
        self._require_idle()
        experiment = self._require_experiment()
        trial = next((t for t in experiment.trials if t["id"] == trial_id), None)
        if trial is None or trial["status"] != "completed":
            raise ValueError("Select a completed test to repeat.")
        self._check_limits(experiment.config.targets(trial["point"]))
        experiment.add_trial({"point": list(trial["point"]), "method": "Operator-requested repeat",
                              "repeat_of": trial_id})
        self.draft.clear()
        self._say("Repeat saved as a new test. No flows changed.")
        self.changed.emit()

    def _pending_targets(self):
        experiment = self._require_experiment()
        if not experiment.pending:
            raise ValueError("Suggest a test first.")
        return experiment.config.targets(experiment.pending["point"])

    def _check_limits(self, targets):
        for role, ceiling in self.limits().items():
            if targets.get(role, 0) > ceiling:
                raise ValueError(f"{role} exceeds its current MAX FLOW ({ceiling:g} SLPM).")

    def _check_assignment(self, targets):
        if not self.session.is_staged:
            raise ValueError("Switch to Staged (RQL) mode first.")
        counts = Counter(ROLE_MAP.get(tuple(pair)) for pair in self.session.selection.values())
        required = {"nh3_rich", "h2_rich", "rich_air", "lean_air"}
        required.update(role for role, target in targets.items() if target > 0)
        for role in required:
            if not self.session.assignments.get(role) or counts[role] != 1:
                raise ValueError(f"Exactly one controller must be assigned to {role}.")
        self._check_limits(targets)

    def prepare_targets(self):
        """Only fill target/setpoint fields. The operator uses existing controls."""
        self._require_idle()
        targets = self._pending_targets()
        self._check_assignment(targets)
        if self.session.sequence_state != "idle":
            raise ValueError("Stop the active sequence before loading experiment targets.")
        if self.experiment.pending.get("window"):
            raise ValueError("This test already has a captured window. Record its result or mark it invalid.")
        self.session.set_autocalc_request(self.experiment.config.request(self.experiment.pending["point"]))
        self.session.set_targets(targets)
        self.targets_ready.emit(dict(targets))
        self._say("Target fields loaded, including pilot/unused lines at zero; no commands sent. "
                  "Review all fields and transitions before applying.")
        return targets

    def _checked_readings(self, targets):
        self._check_assignment(targets)
        if not self.session.is_monitoring or not self.session.controllers_connected:
            raise ValueError("Connect and monitor the controllers before capturing a measurement.")
        if self.session.sequence_state != "idle" or self.session._ramps.active:
            raise ValueError("Finish sequences and ramps before measuring.")
        stamp = getattr(self.session, "_latest_timestamp", None)
        freshness = max(5.0, 3 * self.session.poll_interval_s)
        if stamp is None or not -1 <= (datetime.now() - stamp).total_seconds() <= freshness:
            raise ValueError("Fresh flow telemetry is required. Start a new window after readings recover.")
        samples = self.session.latest_samples()  # never fall back to cached good data
        flows = {role: 0.0 for role in targets}
        setpoints = {role: 0.0 for role in targets}
        for unit in self.session.assigned_units():
            pair = self.session.selection[unit]
            role = ROLE_MAP.get(tuple(pair))
            target = targets.get(role, 0.0)
            sample = samples.get(unit, {})
            value = finite(sample.get("flow"), f"Fresh flow for unit {unit}")
            setpoint = finite(sample.get("sp"), f"Fresh setpoint for unit {unit}")
            tolerance = max(FLOW_ABS_TOL, FLOW_REL_TOL * target)
            setpoint_tolerance = 1e-6 if target == 0 else tolerance
            if abs(value - target) > tolerance or abs(setpoint - target) > setpoint_tolerance:
                if role not in targets:
                    raise ValueError(f"Unit {unit}: pilot and all additional gas lines must be off for this measurement.")
                raise ValueError(f"Unit {unit} is not at the proposed condition. Re-settle before measuring.")
            if role in targets:
                flows[role] = value
                setpoints[role] = setpoint
        return stamp, flows, setpoints

    def _measurement_context(self):
        """Return compact, read-only rig metadata for a condition boundary."""
        snapshot = self.session.read_snapshot()
        assigned_units = {unit for unit in snapshot["assignments"].values() if unit}
        return {
            "captured_at": snapshot.get("captured_at"),
            "operating_mode": snapshot.get("combustion", {}).get("operating_mode"),
            "poll_interval_s": snapshot.get("connection", {}).get("poll_interval_s"),
            "units": {unit: snapshot.get("units", {}).get(unit)
                      for unit in sorted(assigned_units)},
            "declared_limits_slpm": snapshot.get("declared_limits", {}),
            "prepared_targets_slpm": snapshot.get("combustion", {}).get(
                "prepared_targets", {}),
            "live_phi": snapshot.get("combustion", {}).get("live_phi", {}),
        }

    def start_window(self, pilot_off=False, settled=False, *, live=False):
        self._require_idle()
        targets = self._pending_targets()
        if not pilot_off or not settled:
            raise ValueError(
                "Confirm pilot off and stable burner/flows. The analyser must already be settled "
                "unless this campaign has a selected response-delay calibration.")
        if self.experiment.pending.get("window"):
            raise ValueError("This test already has a completed window. Save its result or mark it invalid.")
        stamp, flows, setpoints = self._checked_readings(targets)
        baseline = self.session.mexa.checked_sample() if live else None
        delay = self.experiment.response_delay_seconds if live else 0.0
        if delay > 0:
            run = self.experiment.selected_response_run or {}
            self.settle_wait = {
                "started": self._clock(), "delay_s": delay,
                "targets": dict(targets), "response_run_id": run.get("id"),
            }
            self._say(
                f"MEXA receiver logging continues. Waiting {delay:g} s calibrated "
                "response delay before the averaging window.")
            self.changed.emit()
            return
        self._begin_window(stamp, flows, setpoints=setpoints, targets=targets,
                           baseline=baseline)

    def _begin_window(self, stamp, flows, *, setpoints, targets, baseline=None, delay=0.0,
                      response_run_id=None):
        """Start averaging after any response delay has elapsed."""
        live = baseline is not None
        mexa_capture = LiveWindow(baseline) if live else None
        flow_log = self.session.log_path if self.session.logging_active else None
        self.capture = MeasurementWindow(
            self.experiment.config, targets, self.session.assignments,
            rig_context=self._measurement_context(), flow_audit_log=flow_log)
        self.capture.add(stamp, flows, setpoints)
        self.mexa_capture = mexa_capture
        self._capture_delay = float(delay)
        self._response_run_id = response_run_id
        self._say("Capturing flows and new MEXA samples. Keep the pilot off and condition steady."
                  if live else "Capturing flows. Average raw dry NO and O2 over the same window.")
        self.changed.emit()

    def _sample(self, _generation):
        if self.capture is None:
            return
        try:
            stamp, flows, setpoints = self._checked_readings(self.capture.targets)
            if (self.capture.stamps and
                    (stamp - self.capture.stamps[-1]).total_seconds() > max(5, 3 * self.session.poll_interval_s)):
                raise ValueError("A gap in flow telemetry interrupted the measurement.")
            if (self.capture.stamps and
                    (stamp - self.capture.stamps[0]).total_seconds() > 3600):
                raise ValueError("The measurement exceeded the one-hour capture limit.")
            self.capture.add(stamp, flows, setpoints)
            if self.mexa_capture:
                self.session.mexa.checked_sample()
            elapsed = (self.capture.stamps[-1] - self.capture.stamps[0]).total_seconds()
            self.progress.emit(f"Capturing: {elapsed:.0f} / {self.experiment.config.window_seconds:g} s minimum; "
                               f"{len(self.capture.rows)} fresh passes"
                               + (f"; {len(self.mexa_capture.samples)} MEXA samples" if self.mexa_capture else ""))
        except ValueError as exc:
            self.cancel_window(f"Window discarded: {exc}")

    def finish_window(self):
        if self.capture is None:
            raise ValueError("Start a measurement window first.")
        self._checked_readings(self.capture.targets)
        window = self.capture.finish(end_context=self._measurement_context())
        if self.mexa_capture:
            self.session.mexa.checked_sample()
            window["mexa"] = self.mexa_capture.finish(window, self.experiment.config.window_seconds)
        if self._capture_delay:
            window["pre_window_delay_s"] = self._capture_delay
            window["response_run_id"] = self._response_run_id
            window["total_observation_s"] = self._capture_delay + window["duration_s"]
        self.experiment.record_window(window)
        self.capture = None
        self.mexa_capture = None
        self._capture_delay = 0.0
        self._response_run_id = None
        self._say("Window saved with MEXA means. Review the result and confirm the reporting basis."
                  if window.get("mexa") else "Window saved. Enter its matching uncorrected dry NO and O2 averages.")
        self.changed.emit()
        return window

    def cancel_window(self, reason="Window discarded. Re-settle before starting another window."):
        self.capture = None
        self.mexa_capture = None
        self.settle_wait = None
        self._capture_delay = 0.0
        self._response_run_id = None
        self._say(reason)
        self.changed.emit()

    def _configuration_changed(self, *_args):
        if self.capture or self.settle_wait:
            self.cancel_window("Window discarded because the rig configuration or run state changed.")

    def _mexa_sample(self, sample):
        if self.mexa_capture:
            try:
                self.mexa_capture.add(sample)
            except ValueError as exc:
                self.cancel_window(f"Window discarded: {exc}")

    def _mexa_interrupted(self, reason):
        if self.mexa_capture or self.settle_wait:
            self.cancel_window(f"Window discarded: {reason}")

    def _check_live_freshness(self):
        if self.settle_wait:
            try:
                baseline = self.session.mexa.checked_sample()
                stamp, flows, setpoints = self._checked_readings(self.settle_wait["targets"])
                elapsed = self._clock() - self.settle_wait["started"]
                remaining = max(0.0, self.settle_wait["delay_s"] - elapsed)
                if remaining > 0:
                    self.progress.emit(
                        f"Response delay: {remaining:.0f} s remaining before averaging; "
                        "transient MEXA data remain in the receiver log.")
                    return
                wait = self.settle_wait
                self.settle_wait = None
                self._begin_window(
                    stamp, flows, setpoints=setpoints, targets=wait["targets"], baseline=baseline,
                    delay=wait["delay_s"],
                    response_run_id=wait.get("response_run_id"))
            except ValueError as exc:
                self.cancel_window(f"Window discarded during response delay: {exc}")
            return
        if self.mexa_capture:
            try:
                self.session.mexa.checked_sample()
                self._checked_readings(self.capture.targets)
            except ValueError as exc:
                self.cancel_window(f"Window discarded: {exc}")

    def complete(self, no_ppm, o2_percent, no_sem=None, notes="", basis_confirmed=False):
        self._require_idle()
        if not basis_confirmed:
            raise ValueError("Confirm these are uncorrected dry averages from the saved window.")
        value = self._require_experiment().complete(no_ppm, o2_percent, no_sem, notes)
        self.draft.clear()
        self._say(f"Saved result and condition log: {value:.3f} ppm NO at "
                  f"{self.experiment.config.reference_o2:g}% O2. "
                  "This is not a total-NOx or combustion-efficiency measurement.")
        self.changed.emit()

    def complete_from_mexa(self, notes="", basis_confirmed=False):
        self._require_idle()
        if not basis_confirmed:
            raise ValueError("Confirm the saved MEXA window contains uncorrected dry readings")
        experiment = self._require_experiment()
        measurement = ((experiment.pending or {}).get("window") or {}).get("mexa")
        if not measurement:
            raise ValueError("No saved live MEXA measurement for this test")
        value = experiment.complete(measurement["no_ppm"], measurement["o2_percent"],
                                    notes=notes, from_mexa=True)
        self.draft.clear()
        self._say(f"Saved MEXA result and condition log: {value:.3f} ppm NO at "
                  f"{experiment.config.reference_o2:g}% O2. "
                  "No burner commands sent.")
        self.changed.emit()

    def invalidate(self, reason):
        self._require_idle()
        self._require_experiment().invalidate(reason)
        self.draft.clear()
        self._say("Test marked invalid, condition log saved, and point excluded from the model. "
                  "Flows are unchanged.")
        self.changed.emit()

    def shutdown(self):
        if self.worker:
            self.worker.requestInterruption()
            if not self.worker.wait(200):
                return False
        self.capture = None
        self.mexa_capture = None
        self.settle_wait = None
        self.response.shutdown()
        self._freshness_timer.stop()
        return True
