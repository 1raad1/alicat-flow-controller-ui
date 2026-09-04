"""Real TDMS waveform timing, calibration, selection and completed-file discovery."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from flow_controller.domain import tdms_capture as td
from flow_controller.domain.pressure import validate_pressure_summary

try:
    from nptdms import ChannelObject, TdmsWriter
except ImportError:
    ChannelObject = TdmsWriter = None


@unittest.skipIf(TdmsWriter is None, "Optional npTDMS is not installed")
class TdmsCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "capture.tdms"
        self.start = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
        self.capture = {"experiment_id": "exp", "trial_id": "trial", "capture_id": "capture",
                        "start": self.start.isoformat(), "end": (self.start + timedelta(seconds=5)).isoformat()}
        self.source = {"folder": str(self.root), "group": "raw", "channel": "pressure",
                       "calibration_id": "cal-1", "segment_samples": 1000, "overlap_samples": 500}

    def write(self, path=None, *, start=True, increment=.001, count=5000, values=None, offset=0, dual=False):
        path = path or self.path
        props = {"unit_string": "Volts", "wf_samples": 1017}
        if start is not False:
            stamp = self.start if start is True else start
            props["wf_start_time"] = np.datetime64(stamp.replace(tzinfo=None), "us")
            props["wf_start_offset"] = offset
        if increment is not None:
            props["wf_increment"] = increment
        values = 3 + 2 * np.sin(2 * np.pi * 125 * np.arange(count) / 1000) if values is None else values
        channels = [
                ChannelObject("raw", "pressure", values, properties=props),
                ChannelObject("converted", "pressure", np.zeros(32), properties=props),
                ChannelObject("FFT", "pressure (FFT - (Peak))", np.ones(32), properties=props),
            ]
        if dual:
            channels.append(ChannelObject("raw", "pressure2", values * 2, properties=props))
        with TdmsWriter(path) as writer:
            writer.write_segment(channels)
        return path

    def dual_source(self):
        shared = {key: value for key, value in self.source.items()
                  if key not in ("group", "channel", "calibration_id")}
        shared["transducers"] = [
            {"id": "pressure_1", "label": "PT1", "group": "raw", "channel": "pressure",
             "calibration_id": "cal-1", "scale_pa_per_unit": 1, "offset_pa": 0,
             "clip_min": None, "clip_max": None},
            {"id": "pressure_2", "label": "PT2", "group": "raw", "channel": "pressure2",
             "calibration_id": "cal-2", "scale_pa_per_unit": 1, "offset_pa": 0,
             "clip_min": None, "clip_max": None},
        ]
        return shared

    def test_dual_source_and_capture_process_two_synchronised_channels(self):
        self.write(dual=True)
        source = self.dual_source()
        self.assertEqual(td.validate_tdms_source(source)["transducers"][1]["id"], "pressure_2")
        result = td.process_tdms_capture(self.path, source, self.capture)
        self.assertEqual([item["id"] for item in result["transducers"]], ["pressure_1", "pressure_2"])
        self.assertAlmostEqual(result["transducers"][1]["metrics"]["dominant_amplitude_pa"],
                               2 * result["transducers"][0]["metrics"]["dominant_amplitude_pa"])
        self.assertEqual(validate_pressure_summary(result), result)
        duplicate = self.dual_source()
        duplicate["transducers"][1]["channel"] = "pressure"
        with self.assertRaisesRegex(ValueError, "distinct"):
            td.validate_tdms_source(duplicate)
        duplicate_label = self.dual_source()
        duplicate_label["transducers"][1]["label"] = "pt1"
        with self.assertRaisesRegex(ValueError, "unique"):
            td.validate_tdms_source(duplicate_label)

    def test_inspection_uses_actual_count_metadata_and_flags_fft(self):
        self.write(offset=.25)
        rows = td.inspect_tdms(self.path)
        self.assertEqual(len(rows), 3)
        raw = next(row for row in rows if row["group"] == "raw")
        self.assertEqual(raw["samples"], 5000)  # wf_samples is only a chunk-size property.
        self.assertEqual(raw["sample_rate_hz"], 1000)
        self.assertEqual(raw["unit"], "Volts")
        self.assertEqual(raw["start"], (self.start + timedelta(seconds=.25)).isoformat())
        self.assertFalse(raw["is_spectrum"])
        self.assertTrue(next(row for row in rows if row["group"] == "FFT")["is_spectrum"])

    def test_metadata_processing_calibration_and_unchecked_clipping_provenance(self):
        self.write()
        result = td.process_tdms_capture(self.path, dict(self.source, scale_pa_per_unit=3, offset_pa=10), self.capture)
        self.assertAlmostEqual(result["rms_pa"], 6 / np.sqrt(2), places=10)
        self.assertAlmostEqual(result["peak_abs_pa"], 6, places=10)
        self.assertEqual(result["dominant_frequency_hz"], 125)
        self.assertEqual(result["channel"], "raw/pressure")
        self.assertFalse(result["quality"]["clipping_checked"])
        self.assertEqual(result["association"]["timing_source"], "tdms_waveform")
        self.assertEqual(result["association"]["source_sample_count"], 5000)
        self.assertEqual(len(result["raw_sha256"]), 64)
        self.assertEqual(validate_pressure_summary(result), result)

    def test_near_trigger_whole_file_preserves_samples(self):
        self.write(start=self.start + timedelta(milliseconds=10))
        result = td.process_tdms_capture(self.path, self.source, self.capture)
        self.assertEqual(result["sample_count"], 5000)
        self.assertEqual(result["association"]["sample_offset"], 0)
        self.assertEqual(result["start"], (self.start + timedelta(milliseconds=10)).isoformat())

    def test_long_continuous_file_crops_selected_interval_and_keeps_source_provenance(self):
        self.write(start=self.start - timedelta(seconds=10), count=25000)
        result = td.process_tdms_capture(self.path, self.source, self.capture)
        self.assertEqual(result["sample_count"], 5000)
        self.assertEqual(result["association"]["sample_offset"], 10000)
        self.assertEqual(result["association"]["source_sample_count"], 25000)
        self.assertEqual(result["start"], self.start.isoformat())
        self.assertAlmostEqual(result["rms_pa"], np.sqrt(2), places=10)

    def test_explicit_trigger_fallback_only_when_metadata_missing_and_duration_matches(self):
        self.write(start=False, increment=None)
        with self.assertRaisesRegex(ValueError, "sample-rate fallback"):
            td.process_tdms_capture(self.path, self.source, self.capture)
        profile = dict(self.source, sample_rate_hz=1000)
        with self.assertRaisesRegex(ValueError, "explicitly enable"):
            td.process_tdms_capture(self.path, profile, self.capture)
        profile["use_trigger_time"] = True
        result = td.process_tdms_capture(self.path, profile, self.capture)
        self.assertEqual(result["association"]["timing_source"], "trigger")
        self.assertEqual(result["start"], self.capture["start"])
        self.write(start=False, increment=None, count=10000)
        with self.assertRaisesRegex(ValueError, "duration does not match"):
            td.process_tdms_capture(self.path, profile, self.capture)

    def test_wrong_time_rate_metadata_and_fft_rejected(self):
        self.write(start=self.start - timedelta(hours=1))
        with self.assertRaisesRegex(ValueError, "does not cover"):
            td.process_tdms_capture(self.path, self.source, self.capture)
        self.write()
        with self.assertRaisesRegex(ValueError, "disagrees"):
            td.process_tdms_capture(self.path, dict(self.source, sample_rate_hz=2000), self.capture)
        with self.assertRaisesRegex(ValueError, "FFT/spectrum"):
            td.process_tdms_capture(self.path, dict(self.source, group="FFT", channel="pressure (FFT - (Peak))"), self.capture)
        for increment in (0, float("nan"), -1):
            self.write(increment=increment)
            with self.subTest(increment=increment), self.assertRaises(ValueError):
                td.process_tdms_capture(self.path, self.source, self.capture)

    def test_nonfinite_and_configured_clipping_limits_rejected(self):
        values = np.zeros(5000)
        values[100] = float("nan")
        self.write(values=values)
        with self.assertRaisesRegex(ValueError, "nonfinite"):
            td.process_tdms_capture(self.path, self.source, self.capture)
        self.write(values=np.ones(5000))
        source = dict(self.source, clip_min=-1, clip_max=1)
        with self.assertRaisesRegex(ValueError, "clipping"):
            td.process_tdms_capture(self.path, source, self.capture)
        source["clip_max"] = 2
        result = td.process_tdms_capture(self.path, source, self.capture)
        self.assertTrue(result["quality"]["clipping_checked"])

    def test_source_validation_and_minimum_duration(self):
        for changes in ({"folder": "relative"}, {"sample_rate_hz": True}, {"clip_min": -1},
                        {"scale_pa_per_unit": 0}, {"overlap_samples": 1000},
                        {"min_recording_s": 3601}, {"use_trigger_time": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                td.validate_tdms_source(dict(self.source, **changes))
        self.write()
        with self.assertRaisesRegex(ValueError, "min_recording_s"):
            td.process_tdms_capture(self.path, dict(self.source, min_recording_s=6), self.capture)

    def test_association_validation_rejects_forged_offsets_and_quality_types(self):
        self.write()
        result = td.process_tdms_capture(self.path, self.source, self.capture)
        for changes in ({"sample_offset": 1}, {"source_sample_count": 10},
                        {"timing_source": "mtime"}, {"trigger_end": result["start"]}):
            bad = deepcopy(result)
            bad["association"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_pressure_summary(bad)
        result["quality"]["clipping_checked"] = 0
        with self.assertRaises(ValueError):
            validate_pressure_summary(result)

    def test_changed_file_rejected_and_incomplete_segment_rejected(self):
        self.write()
        original = td.pressure_metrics

        def modified(*args):
            result = original(*args)
            with self.path.open("ab") as handle:
                handle.write(b"extra")
            return result

        with patch.object(td, "pressure_metrics", side_effect=modified):
            with self.assertRaisesRegex(ValueError, "changed"):
                td.process_tdms_capture(self.path, self.source, self.capture)
        self.write()
        self.path.write_bytes(self.path.read_bytes()[:-8])
        with self.assertRaisesRegex(ValueError, "incomplete"):
            td.process_tdms_capture(self.path, self.source, self.capture)

    def test_folder_discovery_ignores_old_and_unrelated_and_waits_for_stability(self):
        old = self.write(self.root / "old.tdms")
        nested = self.root / "nested"
        nested.mkdir()
        self.write(nested / "nested.tdms")
        baseline = td.folder_snapshot(self.root)
        self.assertEqual(list(baseline), [str(old)])
        self.write(self.root / "wrong-time.tdms", start=self.start - timedelta(hours=1))
        self.write()
        progress = []
        result = td.find_tdms_capture(self.source, self.capture, baseline, timeout_s=2, stable_s=.01, progress=progress.append)
        self.assertEqual(result["raw_file"], str(self.path))
        self.assertIn("finish writing", progress[0])

    def test_discovery_rejects_old_only_ambiguity_and_cancellation(self):
        self.write()
        baseline = td.folder_snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "timed out"):
            td.find_tdms_capture(self.source, self.capture, baseline, timeout_s=.02, stable_s=0)
        self.write(self.root / "another.tdms")
        with self.assertRaisesRegex(ValueError, "Multiple TDMS"):
            td.find_tdms_capture(self.source, self.capture, {}, timeout_s=1, stable_s=0)
        with self.assertRaisesRegex(ValueError, "cancelled"):
            td.find_tdms_capture(self.source, self.capture, {}, cancel=lambda: True, timeout_s=1)

    def test_discovery_accepts_changed_file_since_arming(self):
        self.write(count=1000)
        baseline = td.folder_snapshot(self.root)
        self.write()
        result = td.find_tdms_capture(self.source, self.capture, baseline, timeout_s=1, stable_s=0)
        self.assertEqual(result["sample_count"], 5000)


if __name__ == "__main__":
    unittest.main()
