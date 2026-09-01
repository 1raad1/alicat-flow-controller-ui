"""Durable experiment records and measurement validation, without hardware I/O."""

from __future__ import annotations

from copy import deepcopy
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import uuid

import numpy as np

from ..domain.bayesian import SearchConfig, corrected_no, finite
from ..domain.combustion import CombustionCalculator
from ..domain.gas_properties import O2_CORRECTION_AIR_PERCENT
from ..domain.roles import ROLES
from ..mexa.records import PROTOCOL, epoch, number


SCHEMA = 2
SUPPORTED_SCHEMAS = (1, SCHEMA)
MAX_TRIALS = 500
FLOW_REL_TOL = .03
FLOW_ABS_TOL = .05  # SLPM measurement acceptance, NOT a safety threshold
MAX_RESPONSE_RUNS = 20
MAX_RESPONSE_SAMPLES = 4000
MAX_STORED_FLOW = 1_000_000.0
RESPONSE_ROLES = frozenset(key for key, _label in ROLES)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def atomic_save(path, data):
    """Commit a complete JSON snapshot or retain the previous file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp",
                                             dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Experiment:
    def __init__(self, path, data):
        self.path = Path(path)
        self.data = data
        self.config = SearchConfig(**data["config"])

    @classmethod
    def create(cls, path, config):
        path = Path(path)
        if path.exists():
            raise ValueError("That experiment file already exists. Open it or choose a new name.")
        data = {"schema": SCHEMA, "id": str(uuid.uuid4()), "created_at": now_iso(),
                "config": config.to_dict(), "trials": [],
                "analyser_response": {"conditions": {"A": None, "B": None},
                                      "runs": [], "selected_run_id": None},
                "objective": "dry NO ppm corrected to fixed O2; not total NOx",
                "pilot": "off during measurements"}
        atomic_save(path, data)
        return cls(path, data)

    @classmethod
    def load(cls, path):
        path = Path(path)
        if path.stat().st_size > 10_000_000:
            raise ValueError("Experiment file exceeds 10 MB.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["schema"] not in SUPPORTED_SCHEMAS:
                raise ValueError("Unsupported experiment file version.")
            experiment = cls(path, data)
            validate_analyser_response(data.get("analyser_response"))
            trials = data["trials"]
            if not isinstance(trials, list) or len(trials) > MAX_TRIALS:
                raise ValueError("Invalid trial list.")
            identifiers = set()
            pending = 0
            for index, trial in enumerate(trials, 1):
                if (not isinstance(trial.get("id"), str) or not trial["id"]
                        or type(trial.get("number")) is not int or trial["number"] != index
                        or not isinstance(trial.get("method"), str)):
                    raise ValueError("Invalid trial identity, number or method.")
                if trial["id"] in identifiers:
                    raise ValueError("Duplicate trial ID.")
                identifiers.add(trial["id"])
                experiment.config.request(trial["point"])
                if trial["status"] not in ("pending", "completed", "invalid"):
                    raise ValueError("Invalid trial status.")
                pending += trial["status"] == "pending"
                if trial.get("window") is not None:
                    validate_window(trial["window"], experiment.config)
                    run_id = trial["window"].get("response_run_id")
                    response = data.get("analyser_response") or {}
                    if run_id is not None and not any(
                            run.get("id") == run_id for run in response.get("runs", [])):
                        raise ValueError("Measurement window refers to an unknown analyser-response run.")
                if trial["status"] == "completed":
                    validate_window(trial["window"], experiment.config)
                    result = trial["result"]
                    if not isinstance(result.get("notes", ""), str):
                        raise ValueError("Invalid result notes.")
                    value, sem = corrected_no(result["no_ppm"], result["o2_percent"],
                                              experiment.config.reference_o2,
                                              result.get("no_sem"))
                    if abs(finite(result["corrected_no"], "Corrected NO") - value) > 1e-7:
                        raise ValueError("Stored NO correction does not match raw readings.")
                    if result.get("corrected_sem") != sem:
                        raise ValueError("Stored NO uncertainty does not match raw readings.")
                    validate_mexa_result(trial["window"], result)
            if pending > 1:
                raise ValueError("Only one pending experiment is supported.")
            return experiment
        except (KeyError, TypeError, IndexError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid experiment file: {exc}") from exc

    @property
    def trials(self):
        return self.data["trials"]

    @property
    def pending(self):
        return next((t for t in self.trials if t["status"] == "pending"), None)

    def _commit(self, data):
        atomic_save(self.path, data)
        self.data = data

    def _response_copy(self):
        data = deepcopy(self.data)
        response = data.setdefault(
            "analyser_response",
            {"conditions": {"A": None, "B": None}, "runs": [], "selected_run_id": None})
        return data, response

    def set_response_condition(self, label, snapshot):
        """Store condition A or B and invalidate calibration for the old transition."""
        label = response_condition_label(label)
        validate_response_condition(snapshot)
        data, response = self._response_copy()
        response["conditions"][label] = deepcopy(snapshot)
        response["selected_run_id"] = None
        validate_analyser_response(response)
        self._commit(data)

    def response_condition(self, label):
        label = response_condition_label(label)
        response = self.data.get("analyser_response") or {}
        return deepcopy((response.get("conditions") or {}).get(label))

    def record_response_run(self, result):
        """Append a completed detector run and select it when it was successful."""
        data, response = self._response_copy()
        if any(response["conditions"].get(label) is None for label in ("A", "B")):
            raise ValueError("Store analyser-response conditions A and B before recording a run.")
        run = deepcopy(result)
        run.setdefault("id", str(uuid.uuid4()))
        run.setdefault("recorded_at", now_iso())
        run.setdefault("successful", True)
        validate_response_run(run)
        if any(existing["id"] == run["id"] for existing in response["runs"]):
            raise ValueError("Duplicate analyser-response run ID.")
        response["runs"].append(run)
        referenced = {
            trial.get("window", {}).get("response_run_id")
            for trial in data.get("trials", []) if trial.get("window")
        } - {None}
        if len(referenced) >= MAX_RESPONSE_RUNS and run["id"] not in referenced:
            raise ValueError(
                "This campaign already references the maximum number of response calibrations.")
        retained = [item for item in response["runs"] if item["id"] in referenced]
        retained_ids = {item["id"] for item in retained}
        for item in reversed(response["runs"]):
            if item["id"] not in retained_ids and len(retained) < MAX_RESPONSE_RUNS:
                retained.append(item)
                retained_ids.add(item["id"])
        response["runs"] = sorted(
            retained, key=lambda item: response["runs"].index(item))
        if run["successful"]:
            response["selected_run_id"] = run["id"]
        elif response.get("selected_run_id") not in {item["id"] for item in response["runs"]}:
            response["selected_run_id"] = None
        validate_analyser_response(response)
        self._commit(data)
        return deepcopy(run)

    @property
    def selected_response_run(self):
        response = self.data.get("analyser_response") or {}
        selected = response.get("selected_run_id")
        return deepcopy(next((run for run in response.get("runs", [])
                              if run.get("id") == selected), None))

    @property
    def response_delay_seconds(self):
        run = self.selected_response_run
        return 0.0 if run is None else float(run["recommended_delay_s"])

    @property
    def total_live_logging_seconds(self):
        return self.response_delay_seconds + self.config.window_seconds

    def add_trial(self, suggestion):
        if self.pending:
            raise ValueError("Complete or mark the pending test invalid before another suggestion.")
        if len(self.trials) >= MAX_TRIALS:
            raise ValueError("This experiment has reached its 500-test limit. Start a new campaign.")
        self.config.request(suggestion["point"])
        data = deepcopy(self.data)
        trial = dict(suggestion, id=str(uuid.uuid4()), number=len(self.trials) + 1,
                     status="pending", created_at=now_iso(), window=None, result=None)
        data["trials"].append(trial)
        self._commit(data)
        return trial

    def _pending_copy(self):
        if not self.pending:
            raise ValueError("Suggest a test first.")
        data = deepcopy(self.data)
        return data, next(t for t in data["trials"] if t["id"] == self.pending["id"])

    def record_window(self, window):
        validate_window(window, self.config)
        run_id = window.get("response_run_id")
        response = self.data.get("analyser_response") or {}
        if run_id is not None and not any(
                run.get("id") == run_id for run in response.get("runs", [])):
            raise ValueError("Measurement window refers to an unknown analyser-response run.")
        data, trial = self._pending_copy()
        trial["window"] = deepcopy(window)
        self._commit(data)

    def complete(self, no_ppm, o2_percent, no_sem=None, notes="", *, from_mexa=False):
        data, trial = self._pending_copy()
        validate_window(trial["window"], self.config)
        if bool(trial["window"].get("mexa")) != from_mexa:
            raise ValueError("Use the saved MEXA means for a live window, or manual inputs for a manual window")
        value, sem = corrected_no(no_ppm, o2_percent, self.config.reference_o2, no_sem)
        trial["result"] = {"no_ppm": finite(no_ppm, "NO"),
                           "o2_percent": finite(o2_percent, "O2"),
                           "no_sem": None if no_sem is None else finite(no_sem, "NO SEM"),
                           "corrected_no": value, "corrected_sem": sem,
                           "notes": str(notes)[:4000], "recorded_at": now_iso(),
                           "basis": "uncorrected dry inputs; NO SEM only, no O2 uncertainty"}
        trial["result"]["source"] = "mexa_stream" if from_mexa else "manual"
        validate_mexa_result(trial["window"], trial["result"])
        trial["status"] = "completed"
        self._commit(data)
        return value

    def invalidate(self, reason):
        if not str(reason).strip():
            raise ValueError("Enter a reason for marking this test invalid.")
        data, trial = self._pending_copy()
        trial.update(status="invalid", reason=str(reason)[:4000], invalidated_at=now_iso())
        self._commit(data)

    def export_csv(self, path):
        if Path(path).resolve() == self.path.resolve():
            raise ValueError("CSV export cannot overwrite the experiment file.")
        with Path(path).open("w", newline="", encoding="utf-8-sig") as handle:
            fields = ["test", "status", "h2_fraction", "phi_stage1", "phi_overall",
                      "power_kw", "split_rich", "reference_o2", "no_ppm_dry",
                      "o2_percent_dry", "corrected_no_ppm", "corrected_no_sem",
                      "observed_h2_fraction", "observed_phi_stage1", "observed_phi_overall",
                      "observed_power_kw", "observed_split_rich",
                      "window_start", "window_end", "notes",
                      "pre_window_delay_s", "response_run_id", "total_observation_s",
                      "measurement_source", "mexa_source_id", "mexa_first_seq", "mexa_last_seq",
                      "mexa_sample_count", "mexa_no_sd", "mexa_o2_sd", "mexa_audit_log"]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for t in self.trials:
                result, window = t.get("result") or {}, t.get("window") or {}
                mexa = window.get("mexa") or {}
                observed = window.get("observed_point", [None] * 3)
                request = self.config.request(t["point"])
                note = result.get("notes", t.get("reason", ""))
                # Neutralise spreadsheet formula injection in operator notes.
                if note.lstrip().startswith(("=", "+", "-", "@")):
                    note = "'" + note
                writer.writerow({
                    "test": t["number"], "status": t["status"],
                    "h2_fraction": request.h2_fraction,
                    "phi_stage1": request.phi_stage1,
                    "phi_overall": request.phi_global,
                    "power_kw": request.power_kw, "split_rich": request.split_rich,
                    "reference_o2": self.config.reference_o2,
                    "no_ppm_dry": result.get("no_ppm"),
                    "o2_percent_dry": result.get("o2_percent"),
                    "corrected_no_ppm": result.get("corrected_no"),
                    "corrected_no_sem": result.get("corrected_sem"),
                    "observed_h2_fraction": observed[0],
                    "observed_phi_stage1": observed[1],
                    "observed_phi_overall": observed[2],
                    "observed_power_kw": window.get("power_kw"),
                    "observed_split_rich": window.get("split_rich"),
                    "window_start": window.get("start"), "window_end": window.get("end"),
                    "pre_window_delay_s": window.get("pre_window_delay_s", 0),
                    "response_run_id": window.get("response_run_id"),
                    "total_observation_s": window.get("total_observation_s", window.get("duration_s")),
                    "notes": note, "measurement_source": result.get("source", "manual"),
                    "mexa_source_id": mexa.get("source_id"),
                    "mexa_first_seq": mexa.get("first_seq"), "mexa_last_seq": mexa.get("last_seq"),
                    "mexa_sample_count": mexa.get("samples"), "mexa_no_sd": mexa.get("no_sd"),
                    "mexa_o2_sd": mexa.get("o2_sd"), "mexa_audit_log": mexa.get("log_path"),
                })


def response_condition_label(label):
    if not isinstance(label, str) or label.upper() not in ("A", "B"):
        raise ValueError("Analyser-response condition must be A or B.")
    return label.upper()


def _bounded_json(value, name, *, max_depth=5, max_items=40_000, max_bytes=2_000_000):
    """Reject unbounded, non-JSON or non-finite metadata before it reaches a campaign."""
    remaining = [max_items]

    def visit(item, depth):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > max_depth:
            raise ValueError(f"{name} is too large or deeply nested.")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, (int, float)):
            finite(item, name)
            return
        if isinstance(item, str):
            if len(item) > 4000:
                raise ValueError(f"{name} contains an oversized string.")
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 128:
                    raise ValueError(f"{name} contains an invalid key.")
                visit(child, depth + 1)
            return
        raise ValueError(f"{name} contains a value that cannot be stored as JSON.")

    visit(value, 0)
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not valid finite JSON.") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} is too large.")


def _stored_number(value, name):
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite JSON number.")
    return finite(value, name)


def _iso_timestamp(value, name):
    if not isinstance(value, str) or not value or len(value) > 80:
        raise ValueError(f"{name} must be an ISO timestamp.")
    try:
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO timestamp.") from exc


def validate_response_condition(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("Analyser-response condition must be a snapshot object.")
    allowed = {"target_flows", "measured_flows", "assignments", "captured_at", "request"}
    if not set(snapshot) <= allowed:
        raise ValueError("Analyser-response condition contains unknown fields.")
    targets = snapshot.get("target_flows")
    measured = snapshot.get("measured_flows")
    assignments = snapshot.get("assignments")
    if not all(isinstance(item, dict) for item in (targets, measured, assignments)):
        raise ValueError("Response-condition flows and assignments must be objects.")
    roles = set(targets)
    if not roles or roles != set(measured) or roles != set(assignments):
        raise ValueError("Response-condition target, measured and assignment roles must match.")
    if not roles <= RESPONSE_ROLES or any(not isinstance(role, str) or not role for role in roles):
        raise ValueError("Response condition contains an unknown controller role.")
    for role in roles:
        for collection, description in ((targets, "target"), (measured, "measured")):
            value = _stored_number(collection[role], f"Response-condition {description} flow")
            if not 0 <= value <= MAX_STORED_FLOW:
                raise ValueError("Response-condition flow is outside the storage range.")
        unit = assignments[role]
        if not isinstance(unit, str) or not unit.strip() or len(unit) > 128:
            raise ValueError("Response-condition assignment must name a controller unit.")
    if len(set(assignments.values())) != len(assignments):
        raise ValueError("A controller unit cannot be assigned to two response-condition roles.")
    _iso_timestamp(snapshot.get("captured_at"), "Response-condition capture time")
    if "request" in snapshot:
        if not isinstance(snapshot["request"], dict):
            raise ValueError("Response-condition request metadata must be an object.")
        _bounded_json(snapshot["request"], "Response-condition request metadata",
                      max_depth=3, max_items=100, max_bytes=16_000)


def validate_response_run(run):
    if not isinstance(run, dict):
        raise ValueError("Analyser-response run must be an object.")
    _bounded_json(run, "Analyser-response run")
    for key in ("id", "source_id"):
        if not isinstance(run.get(key), str) or not run[key].strip() or len(run[key]) > 128:
            raise ValueError(f"Analyser-response {key} is invalid.")
    for key in ("source", "provenance", "algorithm_version"):
        if key in run and (not isinstance(run[key], str) or not run[key].strip()
                           or len(run[key]) > 512):
            raise ValueError(f"Analyser-response {key} is invalid.")
    _iso_timestamp(run.get("recorded_at"), "Analyser-response recording time")
    if type(run.get("successful")) is not bool:
        raise ValueError("Analyser-response successful flag must be Boolean.")
    timings = ("recommended_delay_s", "command_to_change_s", "command_to_stable_s",
               "flow_to_stable_s")
    values = {}
    for key in timings:
        values[key] = _stored_number(run.get(key), key)
        if not 0 <= values[key] <= 3600:
            raise ValueError("Analyser-response timing is outside 0 to 3600 seconds.")
    if values["command_to_change_s"] > values["command_to_stable_s"]:
        raise ValueError("NO cannot stabilise before its detected change.")
    for key in ("t10_s", "t50_s", "t90_s", "rise_10_90_s"):
        if key in run and run[key] is not None and not 0 <= _stored_number(run[key], key) <= 3600:
            raise ValueError(f"{key} is outside 0 to 3600 seconds.")
    for key in ("baseline_no_ppm", "final_no_ppm", "baseline_sd_ppm", "final_sd_ppm"):
        if key in run:
            value = _stored_number(run[key], key)
            ceiling = 5000 if not key.endswith("sd_ppm") else 5000
            if not 0 <= value <= ceiling:
                raise ValueError(f"{key} is outside the analyser range.")
    if "criteria" in run and not isinstance(run["criteria"], dict):
        raise ValueError("Analyser-response criteria must be an object.")
    if "caveat" in run and (not isinstance(run["caveat"], str) or len(run["caveat"]) > 4000):
        raise ValueError("Analyser-response caveat is invalid.")
    samples = run.get("raw_samples")
    if not isinstance(samples, list) or not 3 <= len(samples) <= MAX_RESPONSE_SAMPLES:
        raise ValueError("Analyser-response run must contain 3 to 4000 raw samples.")
    previous_stamp = previous_elapsed = None
    previous_seq = None
    saw_baseline = saw_response = False
    allowed_sample = {"timestamp", "elapsed_s", "no_ppm", "seq", "phase", "flow_stable"}
    for sample in samples:
        if not isinstance(sample, dict) or set(sample) - allowed_sample:
            raise ValueError("Invalid analyser-response raw sample shape.")
        if not {"timestamp", "elapsed_s", "no_ppm", "seq", "phase"} <= set(sample):
            raise ValueError("Analyser-response raw sample is incomplete.")
        stamp = _stored_number(sample["timestamp"], "Sample timestamp")
        elapsed = _stored_number(sample["elapsed_s"], "Sample elapsed time")
        no_ppm = _stored_number(sample["no_ppm"], "Sample NO")
        seq = sample["seq"]
        if (not 0 <= stamp <= 100_000_000_000 or not -3600 <= elapsed <= 3600
                or not 0 <= no_ppm <= 5000 or type(seq) is not int or seq < 1):
            raise ValueError("Analyser-response raw sample is outside its valid range.")
        if sample["phase"] not in ("baseline", "response"):
            raise ValueError("Analyser-response sample phase must be baseline or response.")
        if sample["phase"] == "baseline":
            if saw_response or elapsed > 0:
                raise ValueError("Baseline response samples must precede the command.")
            saw_baseline = True
        else:
            if elapsed < 0:
                raise ValueError("Response samples cannot precede the command.")
            saw_response = True
        if "flow_stable" in sample and type(sample["flow_stable"]) is not bool:
            raise ValueError("Analyser-response flow_stable must be Boolean.")
        if (previous_stamp is not None
                and (stamp <= previous_stamp or elapsed <= previous_elapsed or seq <= previous_seq)):
            raise ValueError("Analyser-response sample timestamps and sequence must advance.")
        previous_stamp, previous_elapsed, previous_seq = stamp, elapsed, seq
    if not saw_baseline or not saw_response:
        raise ValueError("Analyser-response samples must cover baseline and response phases.")


def validate_analyser_response(response):
    if response is None:  # schema-1/2 campaigns created before response calibration
        return
    if not isinstance(response, dict) or set(response) != {"conditions", "runs", "selected_run_id"}:
        raise ValueError("Invalid analyser-response campaign data.")
    conditions = response["conditions"]
    if not isinstance(conditions, dict) or set(conditions) != {"A", "B"}:
        raise ValueError("Analyser-response conditions must contain A and B.")
    for snapshot in conditions.values():
        if snapshot is not None:
            validate_response_condition(snapshot)
    runs = response["runs"]
    if not isinstance(runs, list) or len(runs) > MAX_RESPONSE_RUNS:
        raise ValueError("Invalid analyser-response run history.")
    identifiers = set()
    for run in runs:
        validate_response_run(run)
        if run["id"] in identifiers:
            raise ValueError("Duplicate analyser-response run ID.")
        identifiers.add(run["id"])
    selected = response["selected_run_id"]
    if selected is not None:
        matches = [run for run in runs if run["id"] == selected]
        if len(matches) != 1 or not matches[0]["successful"]:
            raise ValueError("Selected analyser-response run is missing or unsuccessful.")


def observed_condition(flows):
    calc = CombustionCalculator()
    flows = {key: max(0.0, finite(value, "Measured flow")) for key, value in flows.items()}
    nh3 = flows.get("nh3_rich", 0) + flows.get("nh3_lean", 0)
    h2 = flows.get("h2_rich", 0) + flows.get("h2_lean", 0)
    if nh3 + h2 <= 0:
        raise ValueError("No measured NH3/H2 fuel flow.")
    point = [h2 / (nh3 + h2),
             calc.phi(flows["nh3_rich"], flows["h2_rich"], flows["rich_air"]),
             calc.phi(nh3, h2, flows["rich_air"] + flows["lean_air"])]
    return point, calc.power_kw({"NH3": nh3, "H2": h2})


def observed_split(flows):
    rich = finite(flows.get("nh3_rich", 0), "Measured rich NH3") + finite(
        flows.get("h2_rich", 0), "Measured rich H2")
    lean = finite(flows.get("nh3_lean", 0), "Measured lean NH3") + finite(
        flows.get("h2_lean", 0), "Measured lean H2")
    if rich + lean <= 0:
        raise ValueError("No measured NH3/H2 fuel flow.")
    return rich / (rich + lean)


def validate_window(window, config):
    if not isinstance(window, dict):
        raise ValueError("Capture and finish a valid flow-measurement window first.")
    if window.get("pilot_off_confirmed") is not True:
        raise ValueError("Pilot-off confirmation is missing.")
    if len(window["observed_point"]) != 3:
        raise ValueError("Measured operating point must have three coordinates.")
    for value, (lower, upper) in zip(window["observed_point"], config.bounds[:3]):
        if not lower - 1e-10 <= finite(value, "Measured coordinate") <= upper + 1e-10:
            raise ValueError("Measured operating point is outside the campaign bounds.")
    if (not config.optimise_power
            and abs(finite(window["power_kw"], "Measured power") / config.power_kw - 1) > .03):
        raise ValueError("Measured thermal input differs from the experiment by more than 3%.")
    if (type(window["samples"]) is not int or window["samples"] < 3
            or not config.window_seconds <= finite(window["duration_s"], "Window duration") <= 3600):
        raise ValueError("Measurement window is too short or has fewer than three fresh polling passes.")
    duration = (datetime.fromisoformat(window["end"]) - datetime.fromisoformat(window["start"])).total_seconds()
    if abs(duration - window["duration_s"]) > .001:
        raise ValueError("Measurement timestamps disagree with the recorded duration.")
    delay = finite(window.get("pre_window_delay_s", 0), "Pre-window response delay")
    if not 0 <= delay <= 3600:
        raise ValueError("Pre-window response delay is outside 0 to 3600 seconds.")
    run_id = window.get("response_run_id")
    if run_id is not None and (not isinstance(run_id, str) or not run_id or len(run_id) > 128):
        raise ValueError("Invalid analyser-response run reference.")
    total = finite(window.get("total_observation_s", window["duration_s"] + delay),
                   "Total observation duration")
    if not window["duration_s"] <= total <= 7200 or abs(total - window["duration_s"] - delay) > .001:
        raise ValueError("Total observation duration must equal response delay plus measurement window.")
    point, power = observed_condition(window["mean_flows"])
    split = observed_split(window["mean_flows"])
    if (not np.allclose(point, window["observed_point"], atol=1e-8, rtol=1e-8)
            or abs(power - window["power_kw"]) > 1e-8
            or ("split_rich" in window and abs(split - window["split_rich"]) > 1e-8)):
        raise ValueError("Measured coordinates do not match the saved flow averages.")
    observed = config.observed_vector(window)
    for name, value, (lower, upper) in zip(config.variable_names, observed, config.bounds):
        if not lower - 1e-10 <= value <= upper + 1e-10:
            raise ValueError(f"Measured {name} is outside the campaign bounds.")
    if "mexa" in window:
        validate_mexa_window(window, config)


def validate_mexa_window(window, config):
    data = window["mexa"]
    if (not isinstance(data, dict) or data["protocol"] != PROTOCOL or data["validated"] is not True
            or data["simulated"] is not False or data["basis"] != "dry_uncorrected"):
        raise ValueError("Invalid MEXA measurement provenance")
    uuid.UUID(data["source_id"])
    if (type(data["samples"]) is not int or not 3 <= data["samples"] <= 4000
            or type(data["first_seq"]) is not int or type(data["last_seq"]) is not int
            or data["first_seq"] < 1 or data["last_seq"] - data["first_seq"] + 1 != data["samples"]):
        raise ValueError("MEXA sample count/sequence mismatch")
    start, end = epoch(data["start"]), epoch(data["end"])
    flow_start = datetime.fromisoformat(window["start"]).timestamp()
    flow_end = datetime.fromisoformat(window["end"]).timestamp()
    if (abs((end - start) - number(data["duration_s"], "MEXA duration")) > .001
            or end - start < config.window_seconds or not flow_start <= start < end <= flow_end
            or start - flow_start > 5 or flow_end - end > 5):
        raise ValueError("MEXA measurement does not cover the saved flow window")
    corrected_no(data["no_ppm"], data["o2_percent"], config.reference_o2)
    for key in ("no", "o2"):
        if number(data[key + "_sd"], "MEXA standard deviation") < 0:
            raise ValueError("Negative MEXA standard deviation")
        limits = data[key + "_range"]
        mean = data["no_ppm" if key == "no" else "o2_percent"]
        ceiling = 5000 if key == "no" else O2_CORRECTION_AIR_PERCENT
        if len(limits) != 2 or not 0 <= number(limits[0], "minimum") <= mean <= number(limits[1], "maximum") <= ceiling:
            raise ValueError("Invalid MEXA measurement range")
    if not isinstance(data["log_path"], str) or not data["log_path"]:
        raise ValueError("MEXA audit-log reference is missing")


def validate_mexa_result(window, result):
    mexa = window.get("mexa")
    if mexa:
        if (result.get("source") != "mexa_stream" or result["no_ppm"] != mexa["no_ppm"]
                or result["o2_percent"] != mexa["o2_percent"] or result.get("no_sem") is not None):
            raise ValueError("MEXA result differs from the captured means")
    elif result.get("source", "manual") != "manual":
        raise ValueError("A streamed result must have a saved MEXA window")


class MeasurementWindow:
    """Accumulate fresh, in-tolerance flow passes; no gas analyser is polled."""

    def __init__(self, config, targets, assignments):
        self.config = config
        self.targets = dict(targets)
        self.assignments = dict(assignments)
        self.rows = []
        self.stamps = []

    def add(self, stamp, flows):
        if self.stamps and stamp <= self.stamps[-1]:
            raise ValueError("Polling timestamps must advance throughout the measurement.")
        for role, target in self.targets.items():
            value = finite(flows.get(role), f"Flow for {role}")
            if abs(value - target) > max(FLOW_ABS_TOL, FLOW_REL_TOL * target):
                raise ValueError(f"{role} is not tracking the proposed flow. Re-settle and start a new window.")
        self.stamps.append(stamp)
        self.rows.append(dict(flows))

    def finish(self):
        if len(self.rows) < 3:
            raise ValueError("At least three fresh polling passes are required.")
        duration = (self.stamps[-1] - self.stamps[0]).total_seconds()
        means = {key: float(np.mean([row[key] for row in self.rows])) for key in self.targets}
        point, power = observed_condition(means)
        split = observed_split(means)
        window = {"start": self.stamps[0].astimezone(timezone.utc).isoformat(),
                  "end": self.stamps[-1].astimezone(timezone.utc).isoformat(),
                  "duration_s": duration, "samples": len(self.rows), "mean_flows": means,
                  "observed_point": point, "power_kw": power, "split_rich": split,
                  "assignments": self.assignments, "pilot_off_confirmed": True,
                  "tracking_relative_tolerance": FLOW_REL_TOL,
                  "tracking_absolute_tolerance_slpm": FLOW_ABS_TOL}
        validate_window(window, self.config)
        return window
