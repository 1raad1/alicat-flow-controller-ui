"""Pressure contract and offline processing tests; no DAQ connection."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np

from flow_controller.domain import pressure


def summary():
    start = datetime(2026, 9, 3, tzinfo=timezone.utc)
    return {
        "protocol": "flow-pressure-v1", "type": "pressure_summary",
        "experiment_id": "experiment-1", "trial_id": "trial-1", "capture_id": "capture-1",
        "start": start.isoformat(), "end": (start + timedelta(seconds=4.095)).isoformat(),
        "sample_rate_hz": 1000, "sample_count": 4096, "units": "Pa", "channel": "pressure",
        "calibration_id": "calibration-2026", "rms_pa": 2.0, "peak_abs_pa": 3.0,
        "dominant_frequency_hz": 125.0, "dominant_amplitude_pa": 2.0,
        "quality": {"clipped": False, "nonfinite": False},
        "analysis": {"id": "labview-v1", "band_hz": [50, 300], "window": "flattop",
                     "segment_samples": 1024, "overlap_samples": 512, "detrend": "constant",
                     "amplitude_convention": "rms_spectrum"},
    }


def manifest(path):
    base = summary()
    keys = ("protocol", "experiment_id", "trial_id", "capture_id", "start", "sample_rate_hz",
            "channel", "calibration_id", "quality", "analysis")
    result = {key: base[key] for key in keys}
    result.update(type="file_ready", raw_file=str(path), format="csv", column="pressure_pa")
    return result


class PressureValidationTests(unittest.TestCase):
    def test_dual_summary_validation_and_signature(self):
        base = summary()
        metrics = {key: base.get(key, 0) for key in ("rms_pa", "peak_abs_pa", "dominant_frequency_hz",
                                                    "dominant_amplitude_pa", "rms_window_sd_pa")}
        dual = {key: base[key] for key in ("protocol", "type", "experiment_id", "trial_id",
                                           "capture_id", "start", "end", "sample_rate_hz",
                                           "sample_count", "units")}
        dual.update(raw_file="C:/capture.tdms", raw_sha256="a" * 64)
        dual["association"] = {
            "mode": "tdms-retrospective", "trigger_start": base["start"], "trigger_end": base["end"],
            "timing_source": "tdms_waveform", "sample_offset": 0,
            "source_sample_count": base["sample_count"], "source_start": base["start"]}
        dual["transducers"] = [{"id": f"pressure_{index}", "label": f"PT{index}",
                                 "channel": f"raw/p{index}", "calibration_id": f"cal-{index}",
                                 "analysis": deepcopy(base["analysis"]),
                                 "quality": deepcopy(base["quality"]), "metrics": deepcopy(metrics)}
                                for index in (1, 2)]
        valid = pressure.validate_pressure_summary(dual)
        self.assertEqual(valid["transducers"][1]["id"], "pressure_2")
        self.assertEqual(len(pressure.pressure_signature(valid)["transducers"]), 2)
        for mutate in (lambda x: x["transducers"].pop(),
                       lambda x: x["transducers"][1].update(id="pressure_1"),
                       lambda x: x["transducers"][1].update(label="pt1"),
                       lambda x: x["transducers"][0]["metrics"].update(dominant_amplitude_pa=float("nan"))):
            bad = deepcopy(dual)
            mutate(bad)
            with self.assertRaises(ValueError):
                pressure.validate_pressure_summary(bad)
    def test_normalized_detached_summary_and_signature(self):
        data = summary()
        data.update(request_id="request-1", raw_sha256="AB" * 32)
        data["start"] = "2026-09-03T01:00:00+01:00"
        result = pressure.validate_pressure_summary(data)
        self.assertNotIn("request_id", result)
        self.assertEqual(result["start"], "2026-09-03T00:00:00+00:00")
        self.assertEqual(result["raw_sha256"], "ab" * 32)
        signature = pressure.pressure_signature(result)
        other = deepcopy(result)
        other["sample_count"] = 8192
        other["end"] = "2026-09-03T00:00:08.191+00:00"
        self.assertEqual(signature, pressure.pressure_signature(other))
        data["analysis"]["band_hz"][0] = 1
        self.assertEqual(result["analysis"]["band_hz"], [50, 300])
        other["analysis"]["segment_samples"] = 2048
        self.assertNotEqual(signature, pressure.pressure_signature(other))

    def test_invalid_summary_values(self):
        bad_values = {
            "rms_pa": [-1, float("nan"), float("inf"), True, "2"],
            "sample_rate_hz": [0, -1, False], "sample_count": [15, 16.0, True],
            "peak_abs_pa": [1], "dominant_frequency_hz": [0, 501],
            "dominant_amplitude_pa": [-1], "calibration_id": ["", "x" * 257],
            "raw_sha256": ["bad"], "units": ["mPa"],
            "start": ["2026-09-03T00:00:00", "2026-09-04T00:00:00Z"],
            "end": ["2026-09-03T00:00:06Z"],
            "quality": [{"clipped": True, "nonfinite": False}, {"clipped": False},
                        {"clipped": 0, "nonfinite": False}],
        }
        for key, values in bad_values.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    data = summary()
                    data[key] = value
                    with self.assertRaises(ValueError):
                        pressure.validate_pressure_summary(data)

    def test_invalid_analysis(self):
        for key, value in (("band_hz", [10, 501]), ("band_hz", [300, 50]),
                           ("segment_samples", 8192), ("overlap_samples", 1024),
                           ("detrend", False), ("amplitude_convention", "psd"),
                           ("scale_pa_per_unit", -1), ("offset_pa", True)):
            with self.subTest(key=key, value=value):
                data = summary()
                data["analysis"][key] = value
                with self.assertRaises(ValueError):
                    pressure.validate_pressure_summary(data)

    def test_oversized_nested_and_unknown_payload(self):
        for extra in ("x" * 17000, [[[[[[[[[[1]]]]]]]]]], list(range(100))):
            data = summary()
            data["extra"] = extra
            with self.assertRaises(ValueError):
                pressure.validate_pressure_summary(data)


class PressureFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "pressure.csv"

    def write_samples(self, samples, header="pressure_pa"):
        with self.path.open("w", encoding="utf-8-sig") as handle:
            handle.write(header + "\n")
            for sample in samples:
                handle.write(str(sample) + "\n")

    def test_calibrated_sinusoid_dc_and_full_record_metrics(self):
        samples = 7 + 2 * np.sin(2 * np.pi * 125 * np.arange(4096) / 1000)
        self.write_samples(samples)
        data = manifest(self.path)
        data.update(scale_pa_per_unit=3, offset_pa=10)
        result = pressure.process_pressure_file(data)
        self.assertAlmostEqual(result["rms_pa"], 6 / np.sqrt(2), places=10)
        self.assertAlmostEqual(result["peak_abs_pa"], 6, places=10)
        self.assertEqual(result["dominant_frequency_hz"], 125)
        self.assertAlmostEqual(result["dominant_amplitude_pa"], 6 / np.sqrt(2), places=10)
        self.assertAlmostEqual(result["rms_window_sd_pa"], 0, places=10)
        self.assertEqual(result["analysis"]["id"], pressure.PROCESSOR_ID)
        self.assertEqual(result["analysis"]["scale_pa_per_unit"], 3)
        self.assertEqual(len(result["raw_sha256"]), 64)
        self.assertEqual(result["end"], "2026-09-03T00:00:04.095000+00:00")
        # Excluding this tone from the band must not reduce the full-signal RMS.
        data["analysis"]["band_hz"] = [200, 300]
        out_of_band = pressure.process_pressure_file(data)
        self.assertEqual(out_of_band["rms_pa"], result["rms_pa"])
        self.assertLess(out_of_band["dominant_amplitude_pa"], 1e-10)

    def test_flat_zero_and_short_segment_sd(self):
        self.write_samples(np.zeros(1024))
        result = pressure.process_pressure_file(manifest(self.path))
        for key in ("rms_pa", "peak_abs_pa", "dominant_amplitude_pa", "rms_window_sd_pa"):
            self.assertEqual(result[key], 0)
        self.assertGreaterEqual(result["dominant_frequency_hz"], 50)

    def test_batched_high_overlap_matches_scipy_welch(self):
        from scipy.signal import get_window, welch
        samples = np.random.default_rng(8).normal(size=4096)
        expected_f, expected_s = welch(samples, fs=1000, window=get_window("flattop", 1024),
                                       nperseg=1024, noverlap=1023, detrend="constant", scaling="spectrum")
        actual_f, actual_s = pressure._welch_spectrum(samples, 1000, 1024, 1023)
        np.testing.assert_array_equal(actual_f, expected_f)
        np.testing.assert_allclose(actual_s, expected_s, rtol=1e-12)

    def test_json_summary_roundtrip_and_relative_manifest_dispatch(self):
        result_file = self.path.with_suffix(".json")
        result_file.write_text(json.dumps(summary()), encoding="utf-8")
        self.assertEqual(pressure.load_pressure_result(result_file), pressure.validate_pressure_summary(summary()))
        self.write_samples(np.zeros(4096))
        data = manifest(self.path.name)
        result_file.write_text(json.dumps(data), encoding="utf-8-sig")
        result = pressure.load_pressure_result(result_file)
        self.assertEqual(result["sample_count"], 4096)
        self.assertEqual(Path(result["raw_file"]), self.path)
        with self.assertRaisesRegex(ValueError, "absolute"):
            pressure.process_pressure_file(data)

    def test_clipped_nonfinite_scaling_and_missing_channel(self):
        self.write_samples(np.zeros(4096))
        for changes in ({"quality": {"clipped": True, "nonfinite": False}},
                        {"scale_pa_per_unit": 0}, {"offset_pa": float("nan")},
                        {"column": "absent"}):
            with self.subTest(changes=changes):
                data = manifest(self.path)
                data.update(changes)
                with self.assertRaises(ValueError):
                    pressure.process_pressure_file(data)
        self.write_samples([float("nan")] * 4096)
        with self.assertRaisesRegex(ValueError, "nonfinite"):
            pressure.process_pressure_file(manifest(self.path))

    def test_changed_file_and_limits(self):
        self.write_samples(np.zeros(4096))
        data = manifest(self.path)
        real_read = pressure._read_samples

        def mutate(path, payload):
            result = real_read(path, payload)
            with path.open("a") as handle:
                handle.write("0\n")
            return result

        with patch.object(pressure, "_read_samples", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "changed during processing"):
                pressure.process_pressure_file(data)
        with patch.object(pressure, "MAX_FILE_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "no larger"):
                pressure.process_pressure_file(data)
        with patch.object(pressure, "MAX_SAMPLES", 2048):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                pressure.process_pressure_file(data)

    def test_missing_and_optional_tdms(self):
        self.path.write_bytes(b"fake tdms")
        data = manifest(self.path)
        data.update(format="tdms", group="DAQ")
        with patch.dict("sys.modules", {"nptdms": None}):
            with self.assertRaisesRegex(ValueError, "pip install npTDMS"):
                pressure.process_pressure_file(data)

        class FakeTdms:
            @classmethod
            def open(cls, path):
                return cls()

            def __enter__(self):
                return {"DAQ": {"pressure": np.zeros(4096)}}

            def __exit__(self, *args):
                pass

        with patch.dict("sys.modules", {"nptdms": types.SimpleNamespace(TdmsFile=FakeTdms)}):
            self.assertEqual(pressure.process_pressure_file(data)["rms_pa"], 0)
            data["channel"] = "missing"
            with self.assertRaisesRegex(ValueError, "does not exist"):
                pressure.process_pressure_file(data)


if __name__ == "__main__":
    unittest.main()
