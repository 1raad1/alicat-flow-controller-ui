"""Associate existing LabVIEW TDMS waveforms with locally recorded log/stop times.

No changes to a LabVIEW VI or embedded flow-controller identifiers are needed.
Waveform timestamps are interpreted as UTC as required by the TDMS format.
Values returned by npTDMS already include NI scaling: the source's calibration
converts those values, not necessarily ADC counts, into Pa. No unit is inferred
from a group name such as ``converted``. Clip bounds apply before that conversion.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import math
from pathlib import Path
import re
import time

import numpy as np

from .pressure import (
    MAX_SAMPLES, PROCESSOR_ID, PROTOCOL,
    _integer, _json_object, _number, _stat_token, _string, _timestamp,
    pressure_metrics, validate_pressure_summary,
)

MAX_FOLDER_FILES = 5000


def _tdms_class():
    try:
        from nptdms import TdmsFile
    except ImportError as exc:
        raise ValueError("TDMS import requires npTDMS; install it with 'pip install npTDMS'.") from exc
    return TdmsFile


def validate_tdms_source(source, *, require_folder=True):
    """Validate a local folder/channel/calibration profile and fill defaults."""
    _json_object(source)
    required = {"folder", "group", "channel", "calibration_id"}
    optional = {"sample_rate_hz", "scale_pa_per_unit", "offset_pa", "band_low_hz", "band_high_hz",
                "segment_samples", "overlap_samples", "use_trigger_time", "clip_min", "clip_max", "min_recording_s"}
    if not required <= source.keys() or source.keys() - required - optional:
        raise ValueError("TDMS source has missing or unrecognized settings.")
    folder = Path(_string(source["folder"], "folder", 4096)).expanduser()
    if not folder.is_absolute() or (require_folder and not folder.is_dir()):
        raise ValueError("TDMS folder must be an existing absolute directory.")
    result = {"folder": str(folder.resolve())}
    for key in ("group", "channel", "calibration_id"):
        result[key] = _string(source[key], key)
    # The joined name is stored in the pressure signature.
    _string(result["group"] + "/" + result["channel"], "group/channel")
    result["sample_rate_hz"] = (None if source.get("sample_rate_hz") is None else
                                _number(source["sample_rate_hz"], "sample_rate_hz", positive=True))
    result["scale_pa_per_unit"] = _number(source.get("scale_pa_per_unit", 1), "scale_pa_per_unit", positive=True)
    result["offset_pa"] = _number(source.get("offset_pa", 0), "offset_pa")
    result["min_recording_s"] = _number(source.get("min_recording_s", 1), "min_recording_s", positive=True)
    if result["min_recording_s"] > 3600:
        raise ValueError("min_recording_s must not exceed 3600 seconds.")
    result["band_low_hz"] = _number(source.get("band_low_hz", 0), "band_low_hz", 0)
    result["band_high_hz"] = (None if source.get("band_high_hz") is None else
                              _number(source["band_high_hz"], "band_high_hz", positive=True))
    if result["band_high_hz"] is not None and result["band_high_hz"] <= result["band_low_hz"]:
        raise ValueError("band_high_hz must exceed band_low_hz.")
    result["segment_samples"] = _integer(source.get("segment_samples", 4096), "segment_samples", 16)
    result["overlap_samples"] = _integer(source.get("overlap_samples", 2048), "overlap_samples", 0)
    if result["segment_samples"] > MAX_SAMPLES or result["overlap_samples"] >= result["segment_samples"]:
        raise ValueError("TDMS overlap must be smaller than the bounded segment length.")
    result["use_trigger_time"] = source.get("use_trigger_time", False)
    if type(result["use_trigger_time"]) is not bool:
        raise ValueError("use_trigger_time must be a boolean.")
    if (source.get("clip_min") is None) != (source.get("clip_max") is None):
        raise ValueError("Set both clip_min and clip_max, or neither.")
    for key in ("clip_min", "clip_max"):
        result[key] = None if source.get(key) is None else _number(source[key], key)
    if result["clip_min"] is not None and result["clip_min"] >= result["clip_max"]:
        raise ValueError("clip_min must be smaller than clip_max.")
    return result


def _metadata_number(value, name, positive=False):
    if isinstance(value, np.generic):
        value = value.item()
    return _number(value, name, positive=positive)


def _waveform_metadata(channel):
    properties = channel.properties
    increment = properties.get("wf_increment")
    rate = None
    if increment is not None:
        increment = _metadata_number(increment, "TDMS wf_increment", positive=True)
        rate = _number(1 / increment, "TDMS sample rate", positive=True)
    stamp = properties.get("wf_start_time")
    start = None
    if stamp is not None:
        if isinstance(stamp, np.datetime64):
            if np.isnat(stamp):
                raise ValueError("TDMS wf_start_time is invalid.")
            stamp = np.datetime_as_string(stamp, unit="us") + "+00:00"
        elif isinstance(stamp, datetime):
            # TDMS timestamps have a UTC epoch; naive library timestamps are UTC.
            stamp = stamp.replace(tzinfo=timezone.utc).isoformat() if stamp.tzinfo is None else stamp.isoformat()
        start = _timestamp(stamp, "TDMS wf_start_time")
        offset = _metadata_number(properties.get("wf_start_offset", 0), "TDMS wf_start_offset")
        try:
            start += timedelta(seconds=offset)
        except (ValueError, OverflowError) as exc:
            raise ValueError("TDMS wf_start_offset is outside the supported range.") from exc
    unit = properties.get("unit_string", properties.get("NI_UnitDescription", ""))
    return rate, start, str(unit)[:256]


def _is_spectrum(group, channel):
    return bool(re.search(r"\bfft\b|\bspectrum\b", group + " " + channel, flags=re.IGNORECASE))


def _complete_file(tdms):
    status = getattr(tdms, "file_status", None)
    if status is not None and getattr(status, "incomplete_final_segment", False):
        raise ValueError("TDMS final segment is incomplete; wait for LabVIEW to finish writing.")


def inspect_tdms(path):
    """List metadata without loading waveform arrays, including explicit FFT flags."""
    path = Path(path).expanduser().resolve()
    token = _stat_token(path)
    with _tdms_class().open(path) as tdms:
        _complete_file(tdms)
        result = []
        for group in tdms.groups():
            for channel in group.channels():
                rate, start, unit = _waveform_metadata(channel)
                result.append({"group": group.name, "channel": channel.name, "samples": len(channel),
                               "sample_rate_hz": rate, "start": start.isoformat() if start else None,
                               "unit": unit, "is_spectrum": _is_spectrum(group.name, channel.name)})
                if len(result) > MAX_FOLDER_FILES:
                    raise ValueError("TDMS file contains too many channels.")
    if _stat_token(path) != token:
        raise ValueError("TDMS file changed during metadata inspection.")
    return result


def folder_snapshot(folder):
    """Snapshot direct *.tdms files only; nested directories are not searched."""
    folder = Path(folder).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        raise ValueError("TDMS folder must be an existing absolute directory.")
    result = {}
    for path in folder.iterdir():
        if path.suffix.lower() != ".tdms" or not path.is_file():
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue  # A writer may rename its temporary capture between scans.
        result[str(path.resolve())] = [stat.st_size, stat.st_mtime_ns]
        if len(result) > MAX_FOLDER_FILES:
            raise ValueError(f"TDMS folder exceeds {MAX_FOLDER_FILES} files; choose a smaller recording folder.")
    return result


def _capture(capture):
    _json_object(capture)
    required = {"experiment_id", "trial_id", "capture_id", "start", "end"}
    if not required <= capture.keys():
        raise ValueError("TDMS capture requires experiment/trial/capture IDs and trigger start/end.")
    result = {key: _string(capture[key], key) for key in ("experiment_id", "trial_id", "capture_id")}
    start, end = _timestamp(capture["start"], "trigger start"), _timestamp(capture["end"], "trigger end")
    if end <= start:
        raise ValueError("TDMS trigger end must be after its start.")
    result.update(start=start, end=end)
    return result


def _selection(channel, source, capture):
    if _is_spectrum(source["group"], source["channel"]):
        raise ValueError("Select a time-domain pressure waveform, not an FFT/spectrum channel.")
    rate, source_start, _unit = _waveform_metadata(channel)
    configured_rate = source["sample_rate_hz"]
    if rate is None:
        if configured_rate is None:
            raise ValueError("TDMS wf_increment is missing; configure a sample-rate fallback.")
        rate = configured_rate
    elif configured_rate is not None and not math.isclose(rate, configured_rate, rel_tol=1e-6):
        raise ValueError("Configured sample rate disagrees with TDMS wf_increment.")
    count = len(channel)
    if count < 16:
        raise ValueError("Selected TDMS channel contains fewer than 16 samples.")
    start, end = capture["start"], capture["end"]
    duration = (end - start).total_seconds()
    timing_source = "tdms_waveform"
    if source_start is None:
        if not source["use_trigger_time"]:
            raise ValueError("TDMS wf_start_time is missing; explicitly enable one-file-per-trigger timing to continue.")
        if abs(count / rate - duration) > 2:
            raise ValueError("Untimestamped TDMS file duration does not match this trigger; cannot assume one file per trigger.")
        source_start = start
        offset, stop = 0, count
        timing_source = "trigger"
    else:
        source_end = source_start + timedelta(seconds=(count - 1) / rate)
        whole = (abs((source_start - start).total_seconds()) <= 2
                 and abs((source_end - end).total_seconds()) <= 2
                 and abs(count / rate - duration) <= 2
                 and source_start < end and source_end > start)
        if whole:
            offset, stop = 0, count
        else:
            # Crop a continuous recording to samples in [start, end). Permit
            # one sample interval at the end because the last sample precedes stop.
            if source_start > start or source_end < end - timedelta(seconds=1 / rate):
                raise ValueError("TDMS waveform does not cover the requested trigger interval.")
            offset = max(0, int(math.ceil((start - source_start).total_seconds() * rate - 1e-7)))
            stop = min(count, int(math.ceil((end - source_start).total_seconds() * rate - 1e-7)))
    if not 16 <= stop - offset <= MAX_SAMPLES:
        raise ValueError(f"Selected TDMS interval must contain 16 to {MAX_SAMPLES} samples.")
    if (stop - offset) / rate < source["min_recording_s"] - 1e-6:
        raise ValueError("Selected TDMS recording is shorter than min_recording_s.")
    return rate, source_start, offset, stop, timing_source


def process_tdms_capture(path, source, capture):
    """Process one explicitly selected completed file against its physical trigger.

    Automated discovery additionally requires a file to be new/changed since
    arming. Explicit file selection authorizes that association when fallback
    trigger timing is enabled; an mtime is never treated as an acquisition time.
    """
    source, capture = validate_tdms_source(source), _capture(capture)
    path = Path(path).expanduser().resolve()
    initial = _stat_token(path)
    with _tdms_class().open(path) as tdms:
        _complete_file(tdms)
        try:
            channel = tdms[source["group"]][source["channel"]]
        except KeyError as exc:
            raise ValueError("Selected TDMS group/channel does not exist.") from exc
        rate, source_start, offset, stop, timing_source = _selection(channel, source, capture)
        # A slice loads only the selected waveform span, not other channels.
        samples = np.asarray(channel[offset:stop])
        if samples.dtype.kind not in "fiu" or samples.ndim != 1:
            raise ValueError("Selected TDMS channel must contain real numeric scalar samples.")
        samples = samples.astype(np.float64, copy=False)
    if not np.all(np.isfinite(samples)):
        raise ValueError("TDMS pressure contains nonfinite samples.")
    clipping_checked = source["clip_min"] is not None
    if clipping_checked and np.any((samples <= source["clip_min"]) | (samples >= source["clip_max"])):
        raise ValueError("TDMS pressure touches or exceeds configured clipping limits.")
    analysis = {"id": PROCESSOR_ID, "band_hz": [source["band_low_hz"],
                rate / 2 if source["band_high_hz"] is None else source["band_high_hz"]],
                "window": "flattop", "segment_samples": source["segment_samples"],
                "overlap_samples": source["overlap_samples"], "detrend": "constant",
                "amplitude_convention": "rms_spectrum", "scale_pa_per_unit": source["scale_pa_per_unit"],
                "offset_pa": source["offset_pa"]}
    metrics = pressure_metrics(samples, rate, analysis, source["scale_pa_per_unit"], source["offset_pa"])
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as raw:
        for block in iter(lambda: raw.read(1024 * 1024), b""):
            size += len(block)
            if size > initial[0]:
                raise ValueError("TDMS file changed during processing.")
            digest.update(block)
    if _stat_token(path) != initial:
        raise ValueError("TDMS file changed during processing.")
    start = source_start + timedelta(seconds=offset / rate)
    end = source_start + timedelta(seconds=(stop - 1) / rate)
    return validate_pressure_summary({
        "protocol": PROTOCOL, "type": "pressure_summary", "units": "Pa",
        **{key: capture[key] for key in ("experiment_id", "trial_id", "capture_id")},
        "start": start.isoformat(), "end": end.isoformat(), "sample_rate_hz": rate,
        "sample_count": len(samples), "channel": source["group"] + "/" + source["channel"],
        "calibration_id": source["calibration_id"], "analysis": analysis,
        "quality": {"clipped": False, "nonfinite": False, "clipping_checked": clipping_checked},
        **metrics, "raw_file": str(path), "raw_sha256": digest.hexdigest(),
        "association": {"mode": "tdms-retrospective", "trigger_start": capture["start"].isoformat(),
                        "trigger_end": capture["end"].isoformat(), "timing_source": timing_source,
                        "sample_offset": offset, "source_sample_count": len(channel),
                        "source_start": source_start.isoformat()},
    })


def find_tdms_capture(source, capture, baseline, *, cancel=None, timeout_s=60, stable_s=2, progress=None):
    """Wait for one stable new/changed matching TDMS file; never choose the newest.

    Snapshot the folder when arming and call this after stop. Multiple matching
    recordings require explicit file selection. Wrong-channel/time files are
    ignored, while transient write/read errors can recover before the deadline.
    """
    source = validate_tdms_source(source)
    _capture(capture)
    timeout_s = _number(timeout_s, "timeout_s", positive=True)
    stable_s = _number(stable_s, "stable_s", 0)
    if not isinstance(baseline, dict) or len(baseline) > MAX_FOLDER_FILES:
        raise ValueError("Invalid TDMS folder baseline.")
    for path, token in baseline.items():
        if not isinstance(path, str) or not isinstance(token, (tuple, list)) or len(token) != 2:
            raise ValueError("Invalid TDMS folder baseline entry.")
    deadline = time.monotonic() + timeout_s
    observed, cached = {}, {}
    last_error = "No new or changed TDMS recording matches this trigger."
    previous_message = None
    while True:
        if cancel is not None and cancel():
            raise ValueError("TDMS capture search cancelled.")
        now = time.monotonic()
        current = folder_snapshot(source["folder"])
        candidates = {path: token for path, token in current.items() if baseline.get(path) != token}
        matches = []
        unsettled = False
        for path, token in candidates.items():
            if cancel is not None and cancel():
                raise ValueError("TDMS capture search cancelled.")
            previous = observed.get(path)
            if previous is None or previous[0] != token:
                observed[path] = (token, now)
                cached.pop(path, None)
            if now - observed[path][1] < stable_s:
                unsettled = True
                continue
            try:
                if path not in cached:
                    cached[path] = process_tdms_capture(path, source, capture)
                matches.append(cached[path])
            except (ValueError, OSError, KeyError, EOFError, OverflowError) as exc:
                last_error = f"{Path(path).name}: {exc}"
                if isinstance(exc, (OSError, EOFError)) or "incomplete" in str(exc).lower() or "changed" in str(exc).lower():
                    unsettled = True
        if len(matches) > 1:
            raise ValueError("Multiple TDMS recordings match this trigger. Choose the intended file explicitly.")
        if len(matches) == 1 and not unsettled:
            if cancel is not None and cancel():
                raise ValueError("TDMS capture search cancelled.")
            return matches[0]
        message = "Waiting for TDMS recording to finish writing." if unsettled else last_error
        if progress is not None and message != previous_message:
            progress(message)
            previous_message = message
        if time.monotonic() >= deadline:
            raise ValueError(f"TDMS import timed out. {message}")
        time.sleep(min(.25, max(.001, deadline - time.monotonic())))
