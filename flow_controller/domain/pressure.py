"""Validated pressure results and deterministic processing of completed captures.

RMS and peak are computed after removing the complete record's mean. The
analysis band limits only the dominant spectral search. Spectrum amplitudes
use Welch's RMS spectrum convention, not a PSD or peak-amplitude convention.
DAQ clipping must be reported by the acquisition system; it cannot reliably
be inferred from a saved waveform. Files must be completely written first.
"""

from __future__ import annotations

from array import array
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np


PROTOCOL = "flow-pressure-v1"
PROCESSOR_ID = "python-welch-flattop-v1"
MAX_SAMPLES = 20_000_000
MAX_FILE_BYTES = 1_000_000_000
MAX_SUMMARY_BYTES = 16_384
MAX_MANIFEST_BYTES = 65_536


def _json_object(payload, limit=MAX_SUMMARY_BYTES):
    if not isinstance(payload, dict):
        raise ValueError("Pressure payload must be a JSON object.")

    def visit(value, depth=0):
        if depth > 8:
            raise ValueError("Pressure payload is nested too deeply.")
        if isinstance(value, dict):
            if len(value) > 64 or any(not isinstance(k, str) for k in value):
                raise ValueError("Pressure object has too many or invalid keys.")
            for child in value.values():
                visit(child, depth + 1)
        elif isinstance(value, list):
            if len(value) > 64:
                raise ValueError("Pressure payload lists are too large.")
            for child in value:
                visit(child, depth + 1)
        elif type(value) not in (str, int, float, bool, type(None)):
            raise ValueError("Pressure payload must contain only JSON values.")
        elif isinstance(value, str) and len(value) > limit:
            raise ValueError("Pressure payload string is too large.")

    visit(payload)
    try:
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Pressure payload must contain finite JSON values.") from exc
    if len(encoded.encode("utf-8")) >= limit:
        raise ValueError(f"Pressure payload must be smaller than {limit} bytes.")


def _string(value, name, maximum=256):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a nonempty string of at most {maximum} characters.")
    if any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must not contain control characters.")
    return value


def _number(value, name, minimum=None, positive=False):
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite number.")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a finite number.") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number.")
    if positive and result <= 0 or minimum is not None and result < minimum:
        raise ValueError(f"{name} must be {'positive' if positive else f'at least {minimum}' }.")
    return result


def _integer(value, name, minimum):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}.")
    return value


def _timestamp(value, name):
    _string(value, name, 64)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO timestamp with a timezone.") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone.")
    try:
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name} is outside the supported UTC date range.") from exc


def _quality(value):
    if not isinstance(value, dict) or set(value) != {"clipped", "nonfinite"}:
        raise ValueError("quality must contain clipped and nonfinite boolean flags.")
    if value["clipped"] is not False or value["nonfinite"] is not False:
        raise ValueError("Pressure captures with clipped or nonfinite quality flags are invalid.")
    return {"clipped": False, "nonfinite": False}


def _analysis(value, rate, count=None):
    if not isinstance(value, dict):
        raise ValueError("analysis must be an object.")
    fields = {"id", "band_hz", "window", "segment_samples", "overlap_samples",
              "detrend", "amplitude_convention"}
    optional = {"scale_pa_per_unit", "offset_pa"}
    if not fields <= value.keys() or value.keys() - fields - optional:
        raise ValueError("analysis has missing or unrecognized settings.")
    band = value["band_hz"]
    if not isinstance(band, list) or len(band) != 2:
        raise ValueError("analysis.band_hz must be [low, high].")
    low = _number(band[0], "analysis.band_hz low", 0)
    high = _number(band[1], "analysis.band_hz high", 0)
    if not low < high <= rate / 2:
        raise ValueError("analysis.band_hz must satisfy 0 <= low < high <= Nyquist.")
    segment = _integer(value["segment_samples"], "analysis.segment_samples", 16)
    overlap = _integer(value["overlap_samples"], "analysis.overlap_samples", 0)
    if segment > MAX_SAMPLES or count is not None and segment > count:
        raise ValueError("analysis.segment_samples exceeds the available or supported sample count.")
    if overlap >= segment:
        raise ValueError("analysis.overlap_samples must be smaller than segment_samples.")
    if value["detrend"] != "constant" or value["amplitude_convention"] != "rms_spectrum":
        raise ValueError("analysis requires constant detrending and rms_spectrum amplitude convention.")
    result = {"id": _string(value["id"], "analysis.id"), "band_hz": [low, high],
              "window": _string(value["window"], "analysis.window"),
              "segment_samples": segment, "overlap_samples": overlap,
              "detrend": "constant", "amplitude_convention": "rms_spectrum"}
    for key in optional & value.keys():
        result[key] = _number(value[key], f"analysis.{key}", positive=key == "scale_pa_per_unit")
    return result


def validate_pressure_summary(payload):
    """Return a detached, normalized summary or raise an explanatory ValueError.

    Transport request_id is validated but deliberately excluded from storage.
    Timezone offsets are accepted and normalized to UTC. Unknown fields are
    rejected so unvalidated analysis settings cannot silently affect comparisons.
    """
    _json_object(payload)
    required = {"protocol", "type", "experiment_id", "trial_id", "capture_id", "start", "end",
                "sample_rate_hz", "sample_count", "units", "channel", "calibration_id", "rms_pa",
                "peak_abs_pa", "dominant_frequency_hz", "dominant_amplitude_pa", "quality", "analysis"}
    optional = {"rms_window_sd_pa", "raw_file", "raw_sha256", "request_id"}
    if not required <= payload.keys():
        raise ValueError(f"Pressure summary is missing: {', '.join(sorted(required - payload.keys()))}.")
    if payload.keys() - required - optional:
        raise ValueError("Pressure summary contains unrecognized fields.")
    if payload["protocol"] != PROTOCOL or payload["type"] != "pressure_summary":
        raise ValueError("Expected flow-pressure-v1 pressure_summary.")
    if payload["units"] != "Pa":
        raise ValueError("Pressure summary units must be Pa.")
    result = {"protocol": PROTOCOL, "type": "pressure_summary", "units": "Pa"}
    for key in ("experiment_id", "trial_id", "capture_id", "channel", "calibration_id"):
        result[key] = _string(payload[key], key)
    if "request_id" in payload:
        _string(payload["request_id"], "request_id")
    rate = _number(payload["sample_rate_hz"], "sample_rate_hz", positive=True)
    count = _integer(payload["sample_count"], "sample_count", 16)
    start, end = _timestamp(payload["start"], "start"), _timestamp(payload["end"], "end")
    duration = (end - start).total_seconds()
    try:
        expected = (count - 1) / rate
    except OverflowError as exc:
        raise ValueError("sample_count is too large.") from exc
    if duration < 0 or not math.isfinite(expected) or abs(duration - expected) > max(0.1, 2 / rate):
        raise ValueError("Capture timestamps do not match sample_count and sample_rate_hz.")
    result.update(start=start.isoformat(), end=end.isoformat(), sample_rate_hz=rate, sample_count=count)
    result["analysis"] = _analysis(payload["analysis"], rate, count)
    result["quality"] = _quality(payload["quality"])
    for key in ("rms_pa", "peak_abs_pa", "dominant_frequency_hz", "dominant_amplitude_pa"):
        result[key] = _number(payload[key], key, 0)
    if result["peak_abs_pa"] < result["rms_pa"]:
        raise ValueError("peak_abs_pa must be at least rms_pa.")
    low, high = result["analysis"]["band_hz"]
    if not low <= result["dominant_frequency_hz"] <= high:
        raise ValueError("dominant_frequency_hz is outside analysis.band_hz.")
    if "rms_window_sd_pa" in payload:
        result["rms_window_sd_pa"] = _number(payload["rms_window_sd_pa"], "rms_window_sd_pa", 0)
    if "raw_file" in payload:
        result["raw_file"] = _string(payload["raw_file"], "raw_file", 4096)
    if "raw_sha256" in payload:
        digest = payload["raw_sha256"]
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            raise ValueError("raw_sha256 must contain 64 hexadecimal characters.")
        result["raw_sha256"] = digest.lower()
    _json_object(result)
    return result


def pressure_signature(summary):
    """Comparable acquisition/analysis settings; record duration is excluded."""
    valid = validate_pressure_summary(summary)
    return {key: valid[key] for key in ("sample_rate_hz", "channel", "calibration_id", "analysis", "units")}


def _stat_token(path):
    info = path.stat()
    if not path.is_file() or info.st_size > MAX_FILE_BYTES:
        raise ValueError(f"Pressure raw file must be a regular file no larger than {MAX_FILE_BYTES} bytes.")
    return info.st_size, info.st_mtime_ns, info.st_ino


def _read_samples(path, payload):
    if payload["format"] == "csv":
        column = _string(payload.get("column", "pressure_pa"), "column")
        values = array("d")
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if not reader.fieldnames or reader.fieldnames.count(column) != 1:
                raise ValueError(f"CSV must contain exactly one {column!r} column.")
            for row_number, row in enumerate(reader, start=2):
                if len(values) >= MAX_SAMPLES:
                    raise ValueError(f"Pressure capture exceeds {MAX_SAMPLES} samples.")
                try:
                    values.append(float(row[column]))
                except (ValueError, TypeError, KeyError) as exc:
                    raise ValueError(f"Invalid pressure value in CSV row {row_number}.") from exc
        return np.frombuffer(values, dtype=np.float64)
    if payload["format"] == "tdms":
        group = _string(payload.get("group"), "group")
        try:
            from nptdms import TdmsFile
        except ImportError as exc:
            raise ValueError("TDMS processing requires npTDMS; install it with 'pip install npTDMS'.") from exc
        with TdmsFile.open(path) as source:
            try:
                selected = source[group][payload["channel"]]
            except KeyError as exc:
                raise ValueError("Selected TDMS group/channel does not exist.") from exc
            increment = getattr(selected, "properties", {}).get("wf_increment")
            if increment is not None:
                increment = float(increment)
                if (not math.isfinite(increment) or increment <= 0
                        or not math.isclose(1 / increment, payload["sample_rate_hz"], rel_tol=1e-6)):
                    raise ValueError("TDMS waveform sample rate differs from sample_rate_hz in the manifest.")
            if len(selected) > MAX_SAMPLES:
                raise ValueError(f"Pressure capture exceeds {MAX_SAMPLES} samples.")
            return np.asarray(selected[:], dtype=np.float64)
    raise ValueError("Pressure file format must be csv or tdms.")


def _welch_spectrum(samples, rate, segment, overlap):
    """Average Welch frames in bounded batches, including very high overlap."""
    from scipy.signal import get_window, welch
    step = segment - overlap
    frames = 1 + (len(samples) - segment) // step
    frames_per_batch = max(1, 1_000_000 // segment)
    window = get_window("flattop", segment, fftbins=True)
    accumulated = None
    for first in range(0, frames, frames_per_batch):
        count = min(frames_per_batch, frames - first)
        begin = first * step
        end = begin + segment + (count - 1) * step
        frequencies, spectrum = welch(samples[begin:end], fs=rate, window=window,
                                      nperseg=segment, noverlap=overlap,
                                      detrend="constant", scaling="spectrum")
        if accumulated is None:
            accumulated = spectrum * (count / frames)
        else:
            accumulated += spectrum * (count / frames)
    return frequencies, accumulated


def process_pressure_file(payload):
    """Process a completed CSV/TDMS file-ready manifest using periodic flattop.

    Direct calls require absolute raw_file paths. scale_pa_per_unit and
    offset_pa convert source values to Pa, with calibration provenance retained
    in analysis. For a flat signal the dominant frequency is the first in-band
    FFT bin and its amplitude is zero. Window SD uses nonoverlapping complete
    segments of the mean-removed full signal (population SD; zero if fewer than
    two segments). File size and mtime are checked across reading and hashing.
    """
    _json_object(payload, MAX_MANIFEST_BYTES)
    if payload.get("protocol") != PROTOCOL or payload.get("type") != "file_ready":
        raise ValueError("Expected flow-pressure-v1 file_ready manifest.")
    required = {"protocol", "type", "experiment_id", "trial_id", "capture_id", "start", "sample_rate_hz",
                "raw_file", "format", "channel", "calibration_id", "analysis", "quality"}
    optional = {"column", "group", "scale_pa_per_unit", "offset_pa", "request_id"}
    if not required <= payload.keys():
        raise ValueError(f"File-ready manifest is missing: {', '.join(sorted(required - payload.keys()))}.")
    if payload.keys() - required - optional:
        raise ValueError("File-ready manifest contains unrecognized fields.")
    summary = {key: _string(payload[key], key) for key in ("experiment_id", "trial_id", "capture_id", "channel", "calibration_id")}
    if "request_id" in payload:
        _string(payload["request_id"], "request_id")
    quality = _quality(payload["quality"])
    start = _timestamp(payload["start"], "start")
    rate = _number(payload["sample_rate_hz"], "sample_rate_hz", positive=True)
    scale = _number(payload.get("scale_pa_per_unit", 1), "scale_pa_per_unit", positive=True)
    offset = _number(payload.get("offset_pa", 0), "offset_pa")
    analysis = _analysis(payload["analysis"], rate)
    if analysis["window"] != "flattop":
        raise ValueError("Python pressure processing requires analysis.window='flattop'.")
    for key, value in (("scale_pa_per_unit", scale), ("offset_pa", offset)):
        if key in analysis and analysis[key] != value:
            raise ValueError(f"analysis.{key} conflicts with the file calibration setting.")
        analysis[key] = value
    analysis["id"] = PROCESSOR_ID
    path = Path(_string(payload["raw_file"], "raw_file", 4096)).expanduser()
    if not path.is_absolute():
        raise ValueError("raw_file must be an absolute path for direct processing.")
    path = path.resolve()
    initial_stat = _stat_token(path)
    samples = _read_samples(path, payload)
    if samples.ndim != 1 or len(samples) < 16:
        raise ValueError("Pressure capture must contain at least 16 scalar samples.")
    analysis = _analysis(analysis, rate, len(samples))
    if not np.all(np.isfinite(samples)):
        raise ValueError("Pressure capture contains nonfinite samples.")
    with np.errstate(over="ignore", invalid="ignore"):
        pressure = samples * scale + offset
        pressure -= pressure.mean()
    if not np.all(np.isfinite(pressure)):
        raise ValueError("Calibrated pressure contains nonfinite samples.")
    segment = analysis["segment_samples"]
    frequencies, spectrum = _welch_spectrum(pressure, rate, segment, analysis["overlap_samples"])
    low, high = analysis["band_hz"]
    indices = np.flatnonzero((frequencies >= low) & (frequencies <= high))
    if not len(indices):
        raise ValueError("analysis.band_hz contains no FFT frequency bins at this segment length.")
    index = int(indices[np.argmax(spectrum[indices])])
    # Scaling before squaring avoids overflow for otherwise representable RMS.
    peak = float(np.max(np.abs(pressure)))
    rms = peak * float(np.sqrt(np.mean((pressure / peak) ** 2))) if peak else 0.0
    chunks = len(pressure) // segment
    if chunks >= 2 and peak:
        chunk_rms = peak * np.sqrt(np.mean((pressure[:chunks * segment].reshape(chunks, segment) / peak) ** 2, axis=1))
        rms_sd = float(np.std(chunk_rms / peak) * peak)
    else:
        rms_sd = 0.0
    digest = hashlib.sha256()
    hashed_bytes = 0
    with path.open("rb") as raw:
        for block in iter(lambda: raw.read(1024 * 1024), b""):
            hashed_bytes += len(block)
            if hashed_bytes > initial_stat[0]:
                raise ValueError("Pressure raw file changed during processing; supply a completed file.")
            digest.update(block)
    if _stat_token(path) != initial_stat:
        raise ValueError("Pressure raw file changed during processing; supply a completed file.")
    try:
        end = start + timedelta(seconds=(len(pressure) - 1) / rate)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Capture duration is outside the supported timestamp range.") from exc
    summary.update(protocol=PROTOCOL, type="pressure_summary", units="Pa", start=start.isoformat(),
                   end=end.isoformat(), sample_rate_hz=rate, sample_count=len(pressure),
                   analysis=analysis, quality=quality, rms_pa=rms, peak_abs_pa=peak,
                   dominant_frequency_hz=float(frequencies[index]),
                   dominant_amplitude_pa=float(np.sqrt(spectrum[index])), rms_window_sd_pa=rms_sd,
                   raw_file=str(path), raw_sha256=digest.hexdigest())
    return validate_pressure_summary(summary)


def load_pressure_result(path):
    """Read a JSON summary or file-ready manifest smaller than 64 KiB.

    A manifest's relative raw_file is explicitly resolved relative to the JSON
    file's directory. Summary provenance is preserved without opening raw files.
    """
    source = Path(path).expanduser().resolve()
    with source.open("rb") as handle:
        data = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(data) >= MAX_MANIFEST_BYTES:
        raise ValueError("Pressure JSON file must be smaller than 64 KiB.")
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError("Pressure result file must contain valid UTF-8 JSON.") from exc
    _json_object(payload, MAX_MANIFEST_BYTES)
    if payload.get("type") == "file_ready":
        raw = Path(_string(payload.get("raw_file"), "raw_file", 4096)).expanduser()
        if not raw.is_absolute():
            payload["raw_file"] = str((source.parent / raw).resolve())
        return process_pressure_file(payload)
    return validate_pressure_summary(payload)
