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


PROTOCOL = "mexa584l-horiba-v1-readonly"
MAX_AGE = 5.0
MAX_LINE = 16384
RECEIVER_LOG_REQUIRED = ("Enable 'Save received MEXA logs on this PC' and reconnect before live optimiser capture")
CSV_FIELDS = ("source_id", "seq", "acquired_at", "received_at", "no_ppm", "o2_percent",
              "co_percent", "co2_percent", "hc_ppm", "state", "valid", "simulated",
              "validated", "basis", "alarms", "warnings")


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
    raw = packet["raw"]
    if not isinstance(raw, dict) or set(raw) - {"status", "subsystem", "channels"}:
        raise ValueError("Invalid raw frame fields")
    for frame in raw.values():
        if not isinstance(frame, str) or len(frame) > 256:
            raise ValueError("Oversized raw frame")
        bytes.fromhex(frame)
    if packet["valid"]:
        if (packet["state"] != "measuring" or packet["alarms"] or packet["no_ppm"] is None
                or packet["o2_percent"] is None or not 0 <= packet["no_ppm"] <= 5000
                or not 0 <= packet["o2_percent"] <= 25):
            raise ValueError("Valid flag contradicts measurement or state")
    return packet


def make_packet(cycle, source_id, seq, *, simulated, validated, dry, cycle_s):
    return validate_packet(dict(cycle, schema=1, protocol=PROTOCOL, source_id=source_id,
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
        for key in ("alarms", "warnings"):
            row[key] = "; ".join(record[key])
            if row[key].lstrip().startswith(("=", "+", "-", "@")):
                row[key] = "'" + row[key]
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

    def problem(self, *, experimental=False, now=None, mono=None):
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono
        age = now - epoch(self.packet["acquired_at"])
        if not -1 <= age <= MAX_AGE or not 0 <= mono - self.received_mono <= MAX_AGE:
            return "Stale reading or PC clocks disagree (synchronise both clocks)"
        if self.packet["cycle_s"] > 3:
            return "Analyser acquisition cycle exceeded 3 seconds"
        if not self.packet["valid"]:
            return "Analyser not ready: " + ", ".join([self.packet["state"], *self.packet["alarms"]])
        if experimental:
            if self.packet["simulated"]:
                return "Simulated readings cannot be used in an experiment"
            if not self.packet["validated"]:
                return "Validate the serial readings against the instrument before optimisation"
            if self.packet["basis"] != "dry_uncorrected":
                return "Uncorrected dry NO/O2 basis has not been confirmed"
            if self.packet["o2_percent"] >= 20.9:
                return "O2 must be below 20.9% for oxygen correction"
        return ""


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
    return {"source_id": first["source_id"], "protocol": PROTOCOL,
            "first_seq": first["seq"], "last_seq": last["seq"], "samples": len(selected),
            "start": first["acquired_at"], "end": last["acquired_at"], "duration_s": duration,
            "no_ppm": statistics.mean(no), "o2_percent": statistics.mean(o2),
            "no_sd": statistics.stdev(no), "o2_sd": statistics.stdev(o2),
            "no_range": [min(no), max(no)], "o2_range": [min(o2), max(o2)],
            "log_path": selected[0].log_path, "basis": "dry_uncorrected",
            "validated": True, "simulated": False,
            "averaging": "arithmetic channel means; oxygen correction of means; SEM unknown"}


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
