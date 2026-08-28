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
from ..mexa.records import PROTOCOL, epoch, number


SCHEMA = 1
MAX_TRIALS = 500
FLOW_REL_TOL = .03
FLOW_ABS_TOL = .05  # SLPM measurement acceptance, NOT a safety threshold


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
            if data["schema"] != SCHEMA:
                raise ValueError("Unsupported experiment file version.")
            experiment = cls(path, data)
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
                      "observed_power_kw", "window_start", "window_end", "notes",
                      "measurement_source", "mexa_source_id", "mexa_first_seq", "mexa_last_seq",
                      "mexa_sample_count", "mexa_no_sd", "mexa_o2_sd", "mexa_audit_log"]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for t in self.trials:
                result, window = t.get("result") or {}, t.get("window") or {}
                mexa = window.get("mexa") or {}
                observed = window.get("observed_point", [None] * 3)
                note = result.get("notes", t.get("reason", ""))
                # Neutralise spreadsheet formula injection in operator notes.
                if note.lstrip().startswith(("=", "+", "-", "@")):
                    note = "'" + note
                writer.writerow(dict(zip(fields, [
                    t["number"], t["status"], *t["point"], self.config.power_kw,
                    self.config.split_rich, self.config.reference_o2,
                    result.get("no_ppm"), result.get("o2_percent"), result.get("corrected_no"),
                    result.get("corrected_sem"), *observed, window.get("power_kw"),
                    window.get("start"), window.get("end"), note, result.get("source", "manual"),
                    mexa.get("source_id"), mexa.get("first_seq"), mexa.get("last_seq"),
                    mexa.get("samples"), mexa.get("no_sd"), mexa.get("o2_sd"), mexa.get("log_path")])))


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


def validate_window(window, config):
    if not isinstance(window, dict):
        raise ValueError("Capture and finish a valid flow-measurement window first.")
    if window.get("pilot_off_confirmed") is not True:
        raise ValueError("Pilot-off confirmation is missing.")
    if len(window["observed_point"]) != 3:
        raise ValueError("Measured operating point must have three coordinates.")
    for value, (lower, upper) in zip(window["observed_point"], config.bounds):
        if not lower - 1e-10 <= finite(value, "Measured coordinate") <= upper + 1e-10:
            raise ValueError("Measured operating point is outside the campaign bounds.")
    if abs(finite(window["power_kw"], "Measured power") / config.power_kw - 1) > .03:
        raise ValueError("Measured thermal input differs from the experiment by more than 3%.")
    if (type(window["samples"]) is not int or window["samples"] < 3
            or not config.window_seconds <= finite(window["duration_s"], "Window duration") <= 3600):
        raise ValueError("Measurement window is too short or has fewer than three fresh polling passes.")
    duration = (datetime.fromisoformat(window["end"]) - datetime.fromisoformat(window["start"])).total_seconds()
    if abs(duration - window["duration_s"]) > .001:
        raise ValueError("Measurement timestamps disagree with the recorded duration.")
    point, power = observed_condition(window["mean_flows"])
    if (not np.allclose(point, window["observed_point"], atol=1e-8, rtol=1e-8)
            or abs(power - window["power_kw"]) > 1e-8):
        raise ValueError("Measured coordinates do not match the saved flow averages.")
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
        if len(limits) != 2 or not 0 <= number(limits[0], "minimum") <= mean <= number(limits[1], "maximum") <= (5000 if key == "no" else 20.9):
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
        window = {"start": self.stamps[0].astimezone(timezone.utc).isoformat(),
                  "end": self.stamps[-1].astimezone(timezone.utc).isoformat(),
                  "duration_s": duration, "samples": len(self.rows), "mean_flows": means,
                  "observed_point": point, "power_kw": power,
                  "assignments": self.assignments, "pilot_off_confirmed": True,
                  "tracking_relative_tolerance": FLOW_REL_TOL,
                  "tracking_absolute_tolerance_slpm": FLOW_ABS_TOL}
        validate_window(window, self.config)
        return window
