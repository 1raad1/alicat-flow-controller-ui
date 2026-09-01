import math
import unittest

from flow_controller.domain.analyser_response import AnalyserResponseDetector, ResponseCriteria


def ready_detector(*, baseline=50.0, criteria=None):
    detector = AnalyserResponseDetector(criteria)
    sequence = 100
    for timestamp in range(16):
        detector.add_sample(timestamp, baseline + 0.05 * math.sin(timestamp), "mexa-no", sequence)
        sequence += 1
    assert detector.baseline_ready
    detector.begin_step(16.0)
    return detector, sequence


def complete_response(*, rising=True):
    detector, sequence = ready_detector()
    result = None
    target = 100.0 if rising else 10.0
    for timestamp in range(16, 61):
        elapsed = timestamp - 16
        value = 50.0 + (target - 50.0) * (1.0 - math.exp(-elapsed / 3.0))
        result = detector.add_sample(
            timestamp, value, "mexa-no", sequence, allow_stable=timestamp >= 22
        ) or result
        sequence += 1
        if result:
            break
    return detector, result


class AnalyserResponseTests(unittest.TestCase):
    def test_completes_rising_and_falling_response(self):
        for rising in (True, False):
            with self.subTest(rising=rising):
                detector, result = complete_response(rising=rising)
                self.assertEqual(detector.state, "completed")
                self.assertIsNotNone(result)
                self.assertGreater(result.signed_amplitude_ppm * (1 if rising else -1), 35)
                self.assertGreaterEqual(result.t10_s, 0)
                self.assertLess(result.t10_s, result.t50_s)
                self.assertLess(result.t50_s, result.t90_s)
                self.assertAlmostEqual(result.rise_10_90_s, result.t90_s - result.t10_s)
                self.assertIn("not an analyser-only", " ".join(result.caveats))
                self.assertEqual(
                    len(result.raw_accepted_samples),
                    result.last_sequence - result.first_sequence + 1,
                )

    def test_no_change_never_completes(self):
        detector, sequence = ready_detector()
        for timestamp in range(16, 80):
            self.assertIsNone(detector.add_sample(
                timestamp, 50.05, "mexa-no", sequence, allow_stable=True))
            sequence += 1
        self.assertEqual(detector.state, "response")

    def test_isolated_noisy_departures_do_not_trigger_change(self):
        detector, sequence = ready_detector()
        for index, timestamp in enumerate(range(16, 56)):
            value = 56.0 if index in {4, 13, 25} else 50.0
            self.assertIsNone(detector.add_sample(
                timestamp, value, "mexa-no", sequence, allow_stable=True))
            sequence += 1
        self.assertEqual(detector.state, "response")
        self.assertIsNone(detector.result)

    def test_first_sustained_departure_time_is_not_replaced(self):
        detector, sequence = ready_detector()
        for timestamp, value in zip(range(16, 25), [50, 55, 56, 57, 50, 50, 43, 42, 41]):
            detector.add_sample(timestamp, value, "mexa-no", sequence)
            sequence += 1
        self.assertEqual(detector._change_at, 17)

    def test_allow_stable_gate_prevents_early_completion(self):
        detector, sequence = ready_detector()
        for timestamp in range(16, 51):
            value = 90.0 if timestamp >= 18 else 50.0
            self.assertIsNone(detector.add_sample(timestamp, value, "mexa-no", sequence))
            sequence += 1
        self.assertEqual(detector.state, "response")
        self.assertIsNotNone(detector.add_sample(
            51, 90.0, "mexa-no", sequence, allow_stable=True))

    def test_rejects_invalid_stream(self):
        cases = [
            ({"timestamp_s": 15, "no_ppm": 50, "source_id": "mexa-no", "sequence": 116}, "timestamps"),
            ({"timestamp_s": 16, "no_ppm": 50, "source_id": "other", "sequence": 116}, "source"),
            ({"timestamp_s": 16, "no_ppm": 50, "source_id": "mexa-no", "sequence": 118}, "sequence"),
            ({"timestamp_s": 22, "no_ppm": 50, "source_id": "mexa-no", "sequence": 116}, "gap"),
        ]
        for kwargs, message in cases:
            with self.subTest(message=message):
                detector, _ = ready_detector()
                with self.assertRaisesRegex(ValueError, message):
                    detector.add_sample(**kwargs)
                self.assertEqual(detector.state, "failed")

    def test_recommendation_uses_start_of_stable_window_and_rounds_up(self):
        _, result = complete_response()
        flow_reached_at = result.command_at
        enriched = result.with_flow_reached_at(flow_reached_at)
        expected = result.stable_window_start - result.command_at
        quantum = result.criteria.recommendation_quantum_s
        rounded = math.ceil(result.criteria.recommendation_factor * expected / quantum) * quantum
        self.assertEqual(enriched.flow_to_stable_s, expected)
        self.assertEqual(enriched.recommended_delay_s, rounded)
        self.assertEqual(enriched.sample_resolution_s, 1.0)
        self.assertGreater(result.stable_window_end, result.stable_window_start)

    def test_flow_settling_after_stability_start_never_makes_negative_delay(self):
        _, result = complete_response()
        enriched = result.with_flow_reached_at(result.stable_window_end)
        self.assertEqual(enriched.flow_to_stable_s, 0.0)
        self.assertEqual(enriched.recommended_delay_s, 5.0)

    def test_baseline_requires_span_and_low_drift(self):
        detector = AnalyserResponseDetector()
        for sequence, timestamp in enumerate(range(0, 16, 3)):
            detector.add_sample(timestamp, 20 + timestamp, "mexa-no", sequence)
        self.assertFalse(detector.baseline_ready)
        self.assertIsNotNone(detector.baseline_metrics)

    def test_timeout_and_cancel_are_terminal(self):
        detector, _ = ready_detector(criteria=ResponseCriteria(timeout_s=30.0))
        with self.assertRaises(TimeoutError):
            detector.check_timeout(47.0)
        self.assertEqual(detector.state, "failed")
        other = AnalyserResponseDetector()
        other.cancel("operator stop")
        self.assertEqual(other.state, "cancelled")
        self.assertEqual(other.failure_reason, "operator stop")


if __name__ == "__main__":
    unittest.main()
