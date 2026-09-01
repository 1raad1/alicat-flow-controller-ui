"""Strict wire records, local audit logs, freshness and window summaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import json
import math
import os
from pathlib import Path
import statistics
import time
import uuid

from ..domain.gas_properties import O2_CORRECTION_AIR_PERCENT

PROTOCOL = "mexa584l-horiba-v1-readonly"
MAX_AGE = 5.0
MAX_LINE = 16384
RECEIVER_LOG_REQUIRED = ("Enable 'Save received MEXA logs on this PC' and reconnect before live optimiser capture")
CHANNEL_FIELDS = ("no_ppm", "o2_percent", "co_percent", "co2_percent", "hc_ppm",
                  "afr", "lambda", "rpm", "oil_temperature_c", "pef")
CSV_FIELDS = ("source_id", "seq", "acquired_at", "received_at", "no_ppm", "o2_percent",
              "co_percent", "co2_percent", "hc_ppm", "state", "valid", "simulated",
              "validated", "basis", "alarms", "warnings", "afr", "lambda", "rpm",
              "oil_temperature_c", "pef", "options", "cycle_s", "pef_error",
              "raw_status", "raw_subsystem", "raw_channels", "raw_pef")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def epoch(stamp):
    if not isinstance(stamp, str) or len(stamp) > 48:
        raise ValueError("Invalid acquisition timestamp")
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is None:
        raise ValueError("Acquisition timestamp must include a timezone")
    return parsed.timestamp()


def number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"Invalid {name}")
    return float(value)


def validate_packet(packet):
    if not isinstance(packet, dict) or packet.get("schema") != 1 or packet.get("protocol") != PROTOCOL:
        raise ValueError("Unsupported MEXA record")
    uuid.UUID(packet["source_id"])
    if type(packet["seq"]) is not int or not 1 <= packet["seq"] < 2**53:
        raise ValueError("Invalid sample sequence")
    epoch(packet["acquired_at"])
    if not 0 <= number(packet["cycle_s"], "cycle time") <= 10:
        raise ValueError("Invalid cycle duration")
    for key in ("valid", "simulated", "validated"):
        if type(packet[key]) is not bool:
            raise ValueError(f"Invalid {key} flag")
    if packet["basis"] not in ("unknown", "dry_uncorrected"):
        raise ValueError("Unknown measurement basis")
    if packet["state"] not in ("measuring", "standby", "warm_up", "error"):
        raise ValueError("Unknown analyser state")
    for key in ("alarms", "warnings"):
        if not isinstance(packet[key], list) or len(packet[key]) > 20 or any(
                not isinstance(item, str) or len(item) > 300 for item in packet[key]):
            raise ValueError("Invalid analyser messages")
    for key in ("no_ppm", "o2_percent", "co_percent", "co2_percent", "hc_ppm"):
        if packet[key] is not None:
            number(packet[key], key)
    # New fields are optional on older v1 senders. Never fill a missing sensor
    # or an old packet with a zero or the previous sample's value.
    for key in CHANNEL_FIELDS[5:]:
        if packet.get(key) is not None:
            number(packet[key], key)
    if packet.get("options") is not None and (type(packet["options"]) is not int or not 0 <= packet["options"] <= 255):
        raise ValueError("Invalid option-presence flags")
    if not isinstance(packet.get("pef_error", ""), str) or len(packet.get("pef_error", "")) > 300:
        raise ValueError("Invalid PEF error")
    if packet.get("pef_error") and packet.get("pef") is not None:
        raise ValueError("Failed PEF query cannot supply a value")
    raw_pef = packet.get("raw_pef", "")
    if not isinstance(raw_pef, str) or len(raw_pef) > 12:
        raise ValueError("Invalid raw PEF reply")
    bytes.fromhex(raw_pef)
    raw = packet["raw"]
    if not isinstance(raw, dict) or set(raw) - {"status", "subsystem", "channels"}:
        raise ValueError("Invalid raw frame fields")
    for frame in raw.values():
        if not isinstance(frame, str) or len(frame) > 256:
            raise ValueError("Oversized raw frame")
        bytes.fromhex(frame)
    if "control" in packet:
        control = packet["control"]
        if (not isinstance(control, dict) or set(control) != {"mode", "phase", "reply", "detail"}
                or control["mode"] not in ("meas", "standby")
                or control["phase"] not in ("requested", "acknowledged", "failed", "cancelled")
                or not isinstance(control["reply"], str) or len(control["reply"]) > 10
                or not isinstance(control["detail"], str) or len(control["detail"]) > 300
                or packet["valid"] or packet["validated"]):
            raise ValueError("Invalid local analyser control record")
        bytes.fromhex(control["reply"])
    if packet["valid"]:
        if (packet["state"] != "measuring" or packet["alarms"] or packet["no_ppm"] is None
                or packet["o2_percent"] is None or not 0 <= packet["no_ppm"] <= 5000
                or not 0 <= packet["o2_percent"] <= 25):
            raise ValueError("Valid flag contradicts measurement or state")
    return packet


def make_packet(cycle, source_id, seq, *, simulated, validated, dry, cycle_s):
    fields = dict.fromkeys(CHANNEL_FIELDS)
    fields.update(options=None, pef_error="", raw_pef="")
    fields.update(cycle)
    return validate_packet(dict(fields, schema=1, protocol=PROTOCOL, source_id=source_id,
                                seq=seq, acquired_at=utc_now(), cycle_s=cycle_s,
                                simulated=simulated, validated=bool(validated and not simulated),
                                basis="dry_uncorrected" if dry else "unknown"))


class AuditLog:
    """Exclusive new files; each raw record is flushed before it is published."""

    def __init__(self, directory, prefix):
        destination = Path(directory)
        destination.mkdir(parents=True, exist_ok=True)
        name = f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        self.path = destination / (name + ".jsonl")
        self.raw = self.path.open("x", encoding="utf-8", buffering=1)
        try:
            self.csv = self.path.with_suffix(".csv").open("x", encoding="utf-8", newline="", buffering=1)
            self.writer = csv.DictWriter(self.csv, fieldnames=CSV_FIELDS, extrasaction="ignore")
            self.writer.writeheader()
        except Exception:
            self.raw.close()
            raise

    def write(self, packet, received_at=None):
        record = dict(packet, received_at=received_at)
        self.raw.write(json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n")
        self.raw.flush()
        row = dict(record)
        row.update({f"raw_{name}": record["raw"].get(name, "") for name in ("status", "subsystem", "channels")})
        row["pef_error"] = csv_text(record.get("pef_error", ""))
        for key in ("alarms", "warnings"):
            row[key] = "; ".join(record[key])
            row[key] = csv_text(row[key])
        self.writer.writerow(row)
        self.csv.flush()

    def close(self):
        try:
            for handle in (self.raw, self.csv):
                if not handle.closed:
                    handle.flush()
                    os.fsync(handle.fileno())
        finally:
            self.raw.close()
            self.csv.close()


@dataclass(frozen=True)
class ReceivedSample:
    packet: dict
    received_at: str
    received_mono: float
    log_path: str

    def freshness_problem(self, *, now=None, mono=None):
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono
        age = now - epoch(self.packet["acquired_at"])
        if not -1 <= age <= MAX_AGE or not 0 <= mono - self.received_mono <= MAX_AGE:
            return "Stale reading or PC clocks disagree (synchronise both clocks)"
        return ""

    def problem(self, *, experimental=False, now=None, mono=None):
        freshness = self.freshness_problem(now=now, mono=mono)
        if freshness:
            return freshness
        if self.packet["cycle_s"] > 3:
            return "Analyser acquisition cycle exceeded 3 seconds"
        if not self.packet["valid"]:
            messages = {"no_out_of_range": f"NO out of range ({self.packet['no_ppm']} ppm; expected 0–5000)",
                        "o2_out_of_range": f"O2 out of range ({self.packet['o2_percent']}%; expected 0–25)"}
            alarms = [messages.get(alarm, alarm) for alarm in self.packet["alarms"]]
            return "Analyser not ready: " + ", ".join([self.packet["state"], *alarms])
        if experimental:
            if self.packet["simulated"]:
                return "Simulated readings cannot be used in an experiment"
            if not self.packet["validated"]:
                return "Validate the serial readings against the instrument before optimisation"
            if self.packet["basis"] != "dry_uncorrected":
                return "Uncorrected dry NO/O2 basis has not been confirmed"
            if self.packet["o2_percent"] >= O2_CORRECTION_AIR_PERCENT:
                return "O2 must be below 20.9% for oxygen correction"
        return ""


def reading_text(sample):
    """Show fresh reported values, including invalid ones, without implying validity."""
    if sample is None or sample.freshness_problem():
        return "NO — ppm   ·   O₂ — %"
    p = sample.packet
    no = "—" if p["no_ppm"] is None else f"{p['no_ppm']:g}"
    o2 = "—" if p["o2_percent"] is None else f"{p['o2_percent']:.2f}"
    prefix = "SIMULATION · " if p["simulated"] else ""
    if sample.problem():
        prefix += "INVALID · "
    return f"{prefix}NO {no} ppm   ·   O₂ {o2}%"


def csv_text(text):
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def additional_reading_text(sample):
    """Reported channels are not certified by the NO/O2 validity flag."""
    p = sample.packet if sample and not sample.freshness_problem() else {}
    def value(key, precision):
        item = p.get(key)
        return "—" if item is None else f"{item:.{precision}f}"
    return (f"CO {value('co_percent', 2)}%   ·   CO₂ {value('co2_percent', 2)}%   ·   HC {value('hc_ppm', 0)} ppm\n"
            f"AFR {value('afr', 1)}   ·   λ {value('lambda', 3)}   ·   PEF {value('pef', 3)}\n"
            f"RPM {value('rpm', 0)}   ·   Oil temperature {value('oil_temperature_c', 0)} °C\n"
            "Other reported channels, not separately validated. Missing/unfitted channels show —. "
            "Analyser AFR/λ are not the NH3/H2 burner equivalence ratios.")


def summarise(samples, start, end, minimum):
    selected = [s for s in samples if start <= epoch(s.packet["acquired_at"]) <= end]
    if len(selected) < 3:
        raise ValueError("At least three new MEXA samples inside the flow window are required")
    first, last = selected[0].packet, selected[-1].packet
    duration = epoch(last["acquired_at"]) - epoch(first["acquired_at"])
    if duration < minimum:
        raise ValueError(f"MEXA samples span {duration:.1f} s; keep capturing for at least {minimum:g} s")
    if epoch(first["acquired_at"]) - start > MAX_AGE or end - epoch(last["acquired_at"]) > MAX_AGE:
        raise ValueError("MEXA coverage does not reach the flow-window boundaries")
    no = [s.packet["no_ppm"] for s in selected]
    o2 = [s.packet["o2_percent"] for s in selected]
    def statistics_for(values):
        return {"count": len(values), "mean": statistics.mean(values),
                "sd": statistics.stdev(values) if len(values) > 1 else None,
                "min": min(values), "max": max(values)}

    channel_statistics = {}
    for key in CHANNEL_FIELDS:
        values = [sample.packet.get(key) for sample in selected
                  if sample.packet.get(key) is not None]
        if values:
            channel_statistics[key] = statistics_for(values)
    cycles = statistics_for([sample.packet["cycle_s"] for sample in selected])
    return {"source_id": first["source_id"], "protocol": PROTOCOL,
            "first_seq": first["seq"], "last_seq": last["seq"], "samples": len(selected),
            "start": first["acquired_at"], "end": last["acquired_at"], "duration_s": duration,
            "no_ppm": statistics.mean(no), "o2_percent": statistics.mean(o2),
            "no_sd": statistics.stdev(no), "o2_sd": statistics.stdev(o2),
            "no_range": [min(no), max(no)], "o2_range": [min(o2), max(o2)],
            "log_path": selected[0].log_path, "basis": "dry_uncorrected",
            "validated": True, "simulated": False,
            "averaging": "arithmetic channel means; oxygen correction of means; SEM unknown",
            "channel_statistics": channel_statistics,
            "channel_statistics_basis": (
                "NO/O2 validated for optimisation; other reported channels are informational "
                "and not separately validated; missing values are excluded, never filled"),
            "cycle_statistics_s": cycles,
            "received_start": selected[0].received_at,
            "received_end": selected[-1].received_at,
            "states_seen": sorted({sample.packet["state"] for sample in selected}),
            "alarms_seen": sorted({message for sample in selected
                                   for message in sample.packet["alarms"]}),
            "warnings_seen": sorted({message for sample in selected
                                     for message in sample.packet["warnings"]})}


class LiveWindow:
    """Only new, contiguous, validated readings from one acquisition run."""

    def __init__(self, baseline):
        problem = baseline.problem(experimental=True)
        if problem:
            raise ValueError(problem)
        if not baseline.log_path:
            raise ValueError(RECEIVER_LOG_REQUIRED)
        self.source_id = baseline.packet["source_id"]
        self.seq = baseline.packet["seq"]
        self.last_stamp = epoch(baseline.packet["acquired_at"])
        self.log_path = baseline.log_path
        self.samples = []

    def add(self, sample):
        problem = sample.problem(experimental=True)
        if problem:
            raise ValueError(problem)
        p = sample.packet
        stamp = epoch(p["acquired_at"])
        if p["source_id"] != self.source_id or sample.log_path != self.log_path:
            raise ValueError("MEXA acquisition or receiver restarted")
        if p["seq"] != self.seq + 1:
            raise ValueError("MEXA sample sequence is not contiguous")
        if not 0 < stamp - self.last_stamp <= MAX_AGE:
            raise ValueError("MEXA timestamp gap or clock change")
        if len(self.samples) >= 4000:
            raise ValueError("MEXA capture exceeded its sample limit")
        self.seq, self.last_stamp = p["seq"], stamp
        self.samples.append(sample)

    def finish(self, window, minimum):
        start = datetime.fromisoformat(window["start"]).timestamp()
        end = datetime.fromisoformat(window["end"]).timestamp()
        return summarise(self.samples, start, end, minimum)
