"""Qt orchestration for Bayesian measurements and their analyser delay."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
import uuid

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from .analyser_response_controller import AnalyserResponseController
from .optimisation import Experiment, MeasurementWindow, FLOW_ABS_TOL, FLOW_REL_TOL, atomic_save
from ..domain.bayesian import finite, suggest
from ..domain.pressure import load_pressure_result, process_pressure_file
from ..domain.tdms_capture import validate_tdms_source, folder_snapshot, find_tdms_capture, process_tdms_capture
from ..domain.roles import ROLE_MAP
from mexa_bridge.records import LiveWindow, epoch


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


class PressureWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self.source = deepcopy(source)

    def run(self):
        try:
            result = (process_pressure_file(self.source) if isinstance(self.source, dict)
                      else load_pressure_result(self.source))
            if not self.isInterruptionRequested():
                self.succeeded.emit(result)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(f"Pressure import failed: {exc}")


class TdmsWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, source, capture, baseline=None, path=None, parent=None):
        super().__init__(parent)
        self.source, self.capture = deepcopy(source), deepcopy(capture)
        self.baseline, self.path = deepcopy(baseline), path

    def run(self):
        try:
            result = (process_tdms_capture(self.path, self.source, self.capture) if self.path else
                      find_tdms_capture(self.source, self.capture, self.baseline or {},
                                        cancel=self.isInterruptionRequested, progress=self.progress.emit))
            if not self.isInterruptionRequested():
                self.succeeded.emit(result)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(f"TDMS import: {exc} Choose the matching TDMS file to retry.")


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
        self.pressure_worker = None
        self.labview_armed = False
        self._labview_options = {}
        self._labview_started = False
        self._labview_log_owned = None
        self._legacy_run = None
        self._tdms_baseline = {}
        self._armed_tdms_source = None
        self._pressure_error = None
        self._pressure_job_identity = None
        self._file_request = None
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
        session.labview_command.connect(self._legacy_labview_command)
        session.labview_packet.connect(self._labview_packet)
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
        return self.worker is not None or self.pressure_worker is not None

    def _say(self, text):
        self.last_message = str(text)
        self.message.emit(self.last_message)

    def create(self, path, config):
        self._require_idle()
        self.disarm_labview()
        self.experiment = Experiment.create(path, config)
        self.draft.clear()
        self._say("Experiment saved. Bounds and reporting basis are fixed for this campaign.")
        self.changed.emit()

    def load(self, path):
        self._require_idle()
        self.disarm_labview()
        self.experiment = Experiment.load(path)
        if self.experiment.pending:
            self.experiment.ensure_capture_id()
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

    def labview_request(self):
        """Read the stable acquisition identity for the current pending condition."""
        experiment = self._require_experiment()
        trial = experiment.pending
        if trial is None:
            raise ValueError("Suggest a test first.")
        return {"protocol": "flow-pressure-v1", "type": "start",
                "request_id": trial["capture_id"] + ":start",
                "experiment_id": experiment.data["id"], "trial_id": trial["id"],
                "capture_id": trial["capture_id"]}

    def export_labview_request(self, path):
        self._require_idle()
        experiment = self._require_experiment()
        if Path(path).resolve() == experiment.path.resolve():
            raise ValueError("LabVIEW request cannot overwrite the experiment file.")
        experiment.ensure_capture_id()
        atomic_save(path, self.labview_request())
        self._say(f"LabVIEW start request saved to {path}. Arm this trial before sending it.")
        return Path(path)

    def arm_labview(self, pilot_off=False, settled=False, *, live=False):
        self._require_idle()
        targets = self._pending_targets()
        if not pilot_off or not settled:
            raise ValueError("Confirm pilot off and settled burner/flows before arming LabVIEW.")
        if self.experiment.pending.get("window"):
            raise ValueError("This trial already has a saved window.")
        self._checked_readings(targets)
        if live:
            self.session.mexa.checked_sample()
        source = self.tdms_source
        baseline = folder_snapshot(source["folder"]) if source else {}
        self.experiment.ensure_capture_id()
        self._armed_tdms_source = source
        self._tdms_baseline = baseline
        self._labview_options = {"pilot_off": True, "settled": True, "live": bool(live)}
        self.labview_armed = True
        self._labview_started = False
        self._pressure_error = None
        self._say("LabVIEW trigger armed for this trial. Start the LabVIEW recording when ready.")
        self.changed.emit()
        return self.labview_request()

    def disarm_labview(self):
        self.labview_armed = False
        self._labview_options = {}
        self.changed.emit()

    @property
    def tdms_source(self):
        return deepcopy(self.experiment.data.get("tdms_source")) if self.experiment else None

    @property
    def legacy_capture_active(self):
        return self._legacy_run is not None

    @property
    def legacy_collecting_after_stop(self):
        return bool(self._legacy_run and self._legacy_run.get("stop") is not None)

    @property
    def tdms_auto_pending(self):
        return isinstance(self.pressure_worker, TdmsWorker) and self.pressure_worker.path is None

    @property
    def labview_tail_remaining_s(self):
        run = self._legacy_run
        if not run or run.get("stop") is None:
            return 0.0
        return max(0.0, run["deadline"] - self._clock())

    def configure_tdms_source(self, source):
        self._require_idle()
        experiment = self._require_experiment()
        normalized = validate_tdms_source(source)
        self.disarm_labview()
        experiment.set_tdms_source(normalized)
        self._say("TDMS source saved. Existing LabVIEW log/stop triggers can be used unchanged.")
        self.changed.emit()

    def _legacy_labview_command(self, command):
        """Record now; after stop retain the same condition for the delayed NO tail."""
        try:
            if command == "log" and self.labview_armed and not self._labview_started:
                self._require_idle()
                if not self.session.logging_active:
                    raise ValueError("Could not open the flow CSV log. Check its destination before recording again.")
                targets = self._pending_targets()
                stamp, flows, setpoints = self._checked_readings(targets)
                live = self._labview_options.get("live", False)
                baseline = self.session.mexa.checked_sample() if live else None
                delay = self.experiment.response_delay_seconds if live else 0.0
                start = datetime.now().astimezone(timezone.utc)
                run = self.experiment.selected_response_run or {}
                self._legacy_run = {"start": start, "stop": None, "started": self._clock(),
                                    "delay_s": delay, "no_start": start + timedelta(seconds=delay),
                                    "response_run_id": run.get("id") if delay else None,
                                    "source": deepcopy(self._armed_tdms_source),
                                    "baseline": deepcopy(self._tdms_baseline)}
                self._begin_window(stamp, flows, setpoints=setpoints, targets=targets, baseline=baseline)
                self._labview_started = True
                self.session.labview_stop_deferred = True
                self._labview_log_owned = self.session.log_path if self.session.logging_active else None
                self.changed.emit()
            elif command == "stop" and self._legacy_run:
                run = self._legacy_run
                if run["stop"] is None:
                    run["stop"] = datetime.now().astimezone(timezone.utc)
                    run["deadline"] = max(self._clock() + run["delay_s"],
                                          run["started"] + run["delay_s"] + self.experiment.config.window_seconds)
                    self._say("LabVIEW recording stopped. Keep the condition steady while delayed NO recording finishes.")
                    self.changed.emit()
                self._maybe_finish_legacy()
        except (ValueError, OSError) as exc:
            if self._legacy_run:
                self.cancel_window(f"LabVIEW window discarded: {exc}")
            else:
                self._say(f"LabVIEW trigger: {exc}")

    def _legacy_ready(self):
        run = self._legacy_run
        if not run or run["stop"] is None or self.labview_tail_remaining_s > 0 or not self.capture:
            return False
        first, last = self.capture.stamps[0], self.capture.stamps[-1]
        minimum = self.experiment.config.window_seconds
        if (len(self.capture.stamps) < 3 or (last - first).total_seconds() < minimum
                or last.timestamp() < max(run["stop"].timestamp() + run["delay_s"],
                                         run["no_start"].timestamp() + minimum)):
            return False
        if self.mexa_capture:
            selected = [s for s in self.mexa_capture.samples
                        if run["no_start"].timestamp() <= epoch(s.packet["acquired_at"]) <= last.timestamp()]
            if (len(selected) < 3 or epoch(selected[-1].packet["acquired_at"]) -
                    epoch(selected[0].packet["acquired_at"]) < minimum):
                return False
        return True

    def _maybe_finish_legacy(self):
        if self._legacy_ready():
            try:
                self.finish_window()
            except OSError as exc:
                self._say(f"Could not save the NO window: {exc}. Recording continues; check the save location.")
        elif self.legacy_collecting_after_stop:
            self.progress.emit(f"Keep condition steady: {self.labview_tail_remaining_s:.0f} s NO tail remaining; "
                               "waiting for full fresh NO/flow coverage.")
            self.changed.emit()

    def _packet_trial(self, packet):
        if not isinstance(packet, dict) or packet.get("protocol") != "flow-pressure-v1":
            raise ValueError("Expected flow-pressure-v1 JSON object.")
        for key in ("request_id", "experiment_id", "trial_id", "capture_id"):
            if not isinstance(packet.get(key), str) or not 1 <= len(packet[key]) <= 128:
                raise ValueError(f"A bounded {key} string is required.")
        experiment = self._require_experiment()
        if packet["experiment_id"] != experiment.data["id"]:
            raise ValueError("Message belongs to another experiment.")
        trial = next((t for t in experiment.trials if t["id"] == packet["trial_id"]), None)
        if trial is None or trial.get("capture_id") != packet["capture_id"]:
            raise ValueError("Message belongs to an unknown or discarded acquisition.")
        if trial["status"] == "invalid":
            raise ValueError("This trial was marked invalid.")
        return trial

    def _labview_state(self, trial):
        current = bool(self.experiment.pending and trial["id"] == self.experiment.pending["id"])
        if trial["status"] == "completed":
            state = "completed"
        elif trial.get("pressure"):
            state = "pressure_saved"
        elif current and self.pressure_worker:
            state = "processing"
        elif current and self._pressure_error:
            state = "pressure_error"
        elif trial.get("window"):
            state = "window_saved"
        elif current and self.settle_wait:
            state = "waiting_for_analyser"
        elif current and self.capture:
            state = "capturing"
        else:
            state = "armed" if current and self.labview_armed else "unarmed"
        window = trial.get("window") or {}
        window_start = window.get("start")
        if current and self.capture and self.capture.stamps:
            window_start = self.capture.stamps[0].astimezone(timezone.utc).isoformat()
        result = {"state": state, "window_start": window_start,
                  "window_end": window.get("end"),
                  "minimum_recording_s": self.experiment.config.window_seconds}
        if current and self._pressure_error and not trial.get("pressure"):
            result["pressure_error"] = self._pressure_error
        return result

    def handle_labview_packet(self, packet):
        """Handle one correlated message on the GUI thread and return its acknowledgement."""
        response = {"protocol": "flow-pressure-v1", "type": "ack", "ok": False}
        if isinstance(packet, dict):
            response.update({key: packet[key] for key in
                             ("request_id", "experiment_id", "trial_id", "capture_id")
                             if isinstance(packet.get(key), str) and len(packet[key]) <= 128})
        try:
            trial = self._packet_trial(packet)
            kind = packet.get("type")
            if kind == "status":
                pass
            elif kind == "start":
                if not trial.get("window") and not self.capture and not self.settle_wait:
                    if not self.labview_armed or trial is not self.experiment.pending:
                        raise ValueError("Arm the current trial in the flow software before starting it.")
                    was_logging = self.session.logging_active
                    if not was_logging:
                        self.session._udp_start_logging()
                    try:
                        if not self.session.logging_active:
                            raise ValueError("Could not open the flow CSV log.")
                        self.start_window(**self._labview_options)
                    except Exception:
                        if not was_logging and self.session.logging_active:
                            self.session._udp_stop_logging()
                        raise
                    if not was_logging:
                        self._labview_log_owned = self.session.log_path
                    self._labview_started = True
            elif kind == "stop":
                if trial["status"] == "completed":
                    response.update(ok=True, **self._labview_state(trial))
                    return response
                if not trial.get("window"):
                    if not self._labview_started:
                        raise ValueError("This acquisition was not started by LabVIEW.")
                    if self.settle_wait:
                        raise ValueError("Analyser delay is still running; wait for capturing before recording pressure.")
                    self.finish_window()
                if self._labview_log_owned is not None and self.session.log_path == self._labview_log_owned:
                    self.session._udp_stop_logging()
                self._labview_log_owned = None
                self.labview_armed = False
                self.changed.emit()
            elif kind == "pressure_summary":
                if self.busy:
                    raise ValueError("Wait for the current background operation to finish.")
                self.experiment.attach_pressure(packet)
                if trial["status"] == "pending":
                    self._pressure_error = None
                    self._say("Pressure summary saved for this trial. Review and save the NO result.")
                    self.changed.emit()
            elif kind == "file_ready":
                if trial["status"] != "pending" and not trial.get("pressure"):
                    raise ValueError("A completed test cannot receive a new pressure recording.")
                fingerprint = json.dumps({k: v for k, v in packet.items() if k != "request_id"},
                                         sort_keys=True, allow_nan=False)
                if self._file_request != fingerprint or (not self.pressure_worker and not trial.get("pressure")):
                    if trial.get("pressure"):
                        raise ValueError("Pressure is already saved; use status to check completion.")
                    self._start_pressure_import(packet)
                    self._file_request = fingerprint
            else:
                raise ValueError("Unknown message type; use start, stop, status, pressure_summary or file_ready.")
            # Persistence replaces trial objects, so read the committed version.
            trial = self._packet_trial(packet)
            response.update(ok=True, **self._labview_state(trial))
        except (ValueError, OSError, TypeError, KeyError) as exc:
            response["error"] = str(exc)
            self._say(f"LabVIEW: {exc}")
        return response

    def _labview_packet(self, packet, sender):
        self.session._udp.reply(sender, self.handle_labview_packet(packet))

    def import_pressure(self, path):
        self._start_pressure_import(str(path))

    def import_tdms(self, path):
        self._start_tdms_import(path=str(path))

    def _start_tdms_import(self, *, path=None, baseline=None):
        self._require_idle()
        experiment = self._require_experiment()
        trial = experiment.pending
        acquisition = ((trial or {}).get("window") or {}).get("labview_capture")
        if not acquisition:
            raise ValueError("Record a window using the armed LabVIEW log/stop triggers first.")
        if trial.get("pressure"):
            raise ValueError("Pressure is already saved for this trial.")
        source = acquisition.get("tdms_source")
        if not source:
            raise ValueError("Choose the TDMS source folder, channel and calibration first.")
        capture = {key: value for key, value in self.labview_request().items()
                   if key in ("experiment_id", "trial_id", "capture_id")}
        capture.update(start=acquisition["start"], end=acquisition["stop"])
        self._pressure_job_identity = tuple(capture[key] for key in ("experiment_id", "trial_id", "capture_id"))
        self._pressure_error = None
        self.pressure_worker = TdmsWorker(source, capture, baseline=baseline, path=path, parent=self)
        self.pressure_worker.succeeded.connect(self._pressure_result)
        self.pressure_worker.failed.connect(self._pressure_failed)
        self.pressure_worker.progress.connect(self.progress)
        self.pressure_worker.finished.connect(self._pressure_finished)
        self._say("NO window saved. Looking for the matching completed TDMS recording in the background.")
        self.changed.emit()
        self.pressure_worker.start()

    def _start_pressure_import(self, source):
        self._require_idle()
        experiment = self._require_experiment()
        trial = experiment.pending
        if not trial or not trial.get("window"):
            raise ValueError("Finish the measurement window before importing pressure.")
        if trial.get("pressure"):
            raise ValueError("Pressure is already saved for this trial.")
        self._pressure_job_identity = (experiment.data["id"], trial["id"], trial["capture_id"])
        self._pressure_error = None
        self.pressure_worker = PressureWorker(source, self)
        self.pressure_worker.succeeded.connect(self._pressure_result)
        self.pressure_worker.failed.connect(self._pressure_failed)
        self.pressure_worker.finished.connect(self._pressure_finished)
        self._say("Reading the completed recording and calculating pressure metrics in the background.")
        self.changed.emit()
        self.pressure_worker.start()

    def _pressure_result(self, summary):
        if self.pressure_worker is not None and self.pressure_worker.isInterruptionRequested():
            return
        try:
            identity = tuple(summary[key] for key in ("experiment_id", "trial_id", "capture_id"))
            if identity != self._pressure_job_identity:
                raise ValueError("Imported pressure belongs to another acquisition.")
            self.experiment.attach_pressure(summary)
            self._pressure_error = None
            self._say(f"Pressure saved: RMS {summary['rms_pa']:.4g} Pa, "
                      f"dominant frequency {summary['dominant_frequency_hz']:.4g} Hz.")
        except (ValueError, OSError, KeyError) as exc:
            self._pressure_failed(str(exc))
        self.changed.emit()

    def _pressure_failed(self, error):
        self._pressure_error = str(error)
        self._say(self._pressure_error)
        self.changed.emit()

    def _pressure_finished(self):
        worker, self.pressure_worker = self.pressure_worker, None
        if worker is not None:
            worker.deleteLater()
        self.changed.emit()

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
        self._labview_started = False
        self._pressure_error = None
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
            self._maybe_finish_legacy()
        except ValueError as exc:
            self.cancel_window(f"Window discarded: {exc}")

    def finish_window(self):
        if self.capture is None:
            raise ValueError("Start a measurement window first.")
        legacy = self._legacy_run
        if legacy and not self._legacy_ready():
            raise ValueError("Wait for LabVIEW stop and the complete delayed NO window; keep the condition steady.")
        self._checked_readings(self.capture.targets)
        window = self.capture.finish(end_context=self._measurement_context())
        if self.mexa_capture:
            self.session.mexa.checked_sample()
            no_window = dict(window, start=legacy["no_start"].isoformat()) if legacy else window
            window["mexa"] = self.mexa_capture.finish(no_window, self.experiment.config.window_seconds)
        if legacy:
            source = legacy["source"]
            window["labview_capture"] = {
                "start": legacy["start"].isoformat(), "stop": legacy["stop"].isoformat(),
                "no_start": legacy["no_start"].isoformat(), "no_end": window["end"],
                "delay_s": legacy["delay_s"], "pressure_min_seconds": source["min_recording_s"] if source else 1.0,
                "tdms_source": deepcopy(source),
            }
            if legacy["response_run_id"]:
                window["response_run_id"] = legacy["response_run_id"]
            window["total_observation_s"] = window["duration_s"]
        if self._capture_delay:
            window["pre_window_delay_s"] = self._capture_delay
            window["response_run_id"] = self._response_run_id
            window["total_observation_s"] = self._capture_delay + window["duration_s"]
        self.experiment.record_window(window)
        self.capture = None
        self.mexa_capture = None
        self._capture_delay = 0.0
        self._response_run_id = None
        self._legacy_run = None
        self.session.labview_stop_deferred = False
        self.labview_armed = False
        if self._labview_log_owned is not None and self.session.log_path == self._labview_log_owned:
            self.session._udp_stop_logging()
        self._labview_log_owned = None
        self._say("Window saved with MEXA means. Review the result and confirm the reporting basis."
                  if window.get("mexa") else "Window saved. Enter its matching uncorrected dry NO and O2 averages.")
        self.changed.emit()
        if legacy and self.experiment.config.objective_mode == "map_no_pressure":
            if legacy["source"]:
                try:
                    self._start_tdms_import(baseline=legacy["baseline"])
                except (ValueError, OSError) as exc:
                    self._pressure_failed(f"NO window saved; TDMS import needs attention: {exc}")
            else:
                self._say("NO window saved. Choose a TDMS source and the matching file to add pressure.")
        return window

    def cancel_window(self, reason="Window discarded. Re-settle before starting another window."):
        abandoned = self.capture is not None or self.settle_wait is not None
        self.capture = None
        self.mexa_capture = None
        self.settle_wait = None
        self._capture_delay = 0.0
        self._response_run_id = None
        self._legacy_run = None
        self.session.labview_stop_deferred = False
        self.labview_armed = False
        self._labview_started = False
        self._labview_options = {}
        if abandoned and self.experiment and self.experiment.pending:
            self.experiment.reset_capture()
        if self._labview_log_owned is not None and self.session.log_path == self._labview_log_owned:
            self.session._udp_stop_logging()
        self._labview_log_owned = None
        self._say(reason)
        self.changed.emit()

    def _configuration_changed(self, *_args):
        if self.capture or self.settle_wait:
            self.cancel_window("Window discarded because the rig configuration or run state changed.")
        elif self.labview_armed:
            self.disarm_labview()
            self._say("LabVIEW disarmed because the rig configuration or run state changed.")

    def _mexa_sample(self, sample):
        if self.mexa_capture:
            try:
                self.mexa_capture.add(sample)
                self._maybe_finish_legacy()
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
        if self._legacy_run:
            try:
                self._checked_readings(self.capture.targets)
                self._maybe_finish_legacy()
            except ValueError as exc:
                self.cancel_window(f"Window discarded: {exc}")

    def complete(self, no_ppm, o2_percent, no_sem=None, notes="", basis_confirmed=False):
        self._require_idle()
        if not basis_confirmed:
            raise ValueError("Confirm these are uncorrected dry averages from the saved window.")
        value = self._require_experiment().complete(no_ppm, o2_percent, no_sem, notes)
        self.disarm_labview()
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
        self.disarm_labview()
        self.draft.clear()
        self._say(f"Saved MEXA result and condition log: {value:.3f} ppm NO at "
                  f"{experiment.config.reference_o2:g}% O2. "
                  "No burner commands sent.")
        self.changed.emit()

    def invalidate(self, reason):
        self._require_idle()
        self._require_experiment().invalidate(reason)
        self.disarm_labview()
        self.draft.clear()
        self._say("Test marked invalid, condition log saved, and point excluded from the model. "
                  "Flows are unchanged.")
        self.changed.emit()

    def shutdown(self):
        if self.pressure_worker:
            self.pressure_worker.requestInterruption()
            if not self.pressure_worker.wait(200):
                return False
        if self.worker:
            self.worker.requestInterruption()
            if not self.worker.wait(200):
                return False
        self.capture = None
        self.mexa_capture = None
        self.settle_wait = None
        self._legacy_run = None
        self.session.labview_stop_deferred = False
        self.response.shutdown()
        self._freshness_timer.stop()
        return True
