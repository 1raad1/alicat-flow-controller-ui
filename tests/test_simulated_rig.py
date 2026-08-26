import asyncio
from datetime import datetime, timedelta
import math
import unittest

from flow_controller.core.agent_read_model import flow_stability
from flow_controller.core.graph_history import GraphHistory
from flow_controller.core.simulated_rig import SimulatedRig


class SimulatedRigTests(unittest.TestCase):
    def test_flow_follows_the_exact_first_order_lag_solution(self):
        rig = SimulatedRig(('A',), time_constant_s=2.0)
        rig.set_setpoint('A', 10.0)
        rig.advance(2.0)
        self.assertAlmostEqual(rig.reading('A')['mass_flow'], 10.0 * (1.0 - math.exp(-1.0)))
        rig.advance(2.0)
        self.assertAlmostEqual(rig.reading('A')['mass_flow'], 10.0 * (1.0 - math.exp(-2.0)))

    def test_units_are_independent_and_sample_matches_app_telemetry_shape(self):
        rig = SimulatedRig(('A', 'B'), time_constant_s=1.0, pressure=15.0,
                           temperature=25.0)
        rig.set_setpoint('A', 3.0)
        rig.set_setpoint('B', 7.0)
        rig.advance(1.0)
        samples = rig.samples()
        self.assertLess(samples['A']['flow'], samples['B']['flow'])
        self.assertEqual(samples['A']['press'], 15.0)
        self.assertEqual(samples['A']['temp'], 25.0)
        self.assertEqual(samples['A']['internal_error'],
                         samples['A']['flow'] - samples['A']['sp'])

    def test_async_controller_facade_is_compatible_with_monitor_code(self):
        rig = SimulatedRig(('A',), time_constant_s=1.0)

        async def exercise():
            controller = rig.controller('A')
            await controller.set_flow_rate(4.0)
            rig.advance(1.0)
            return await controller.get()

        reading = asyncio.run(exercise())
        self.assertEqual(reading['setpoint'], 4.0)
        self.assertGreater(reading['mass_flow'], 0.0)

    def test_lagged_samples_drive_a_headless_stability_gate(self):
        rig = SimulatedRig(('A',), time_constant_s=1.0)
        history = GraphHistory()
        history.set_units(['A'])
        started = datetime(2026, 8, 26, 12, 0)
        rig.set_setpoint('A', 2.0)
        for generation in range(1, 9):
            rig.advance(1.0)
            history.push(generation, started + timedelta(seconds=generation - 1),
                         rig.samples())
            if generation == 1:
                self.assertFalse(
                    flow_stability(history, 'A', duration_s=0.0,
                                   tolerance=0.05)['stable'])
        settled = flow_stability(history, 'A', duration_s=1.0, tolerance=0.05)
        self.assertTrue(settled['stable'])
        self.assertEqual(settled['reason'], 'stable')

    def test_invalid_clock_and_units_fail_explicitly(self):
        with self.assertRaises(ValueError):
            SimulatedRig((), time_constant_s=1.0)
        with self.assertRaises(ValueError):
            SimulatedRig(('A',), time_constant_s=0.0)
        rig = SimulatedRig()
        with self.assertRaises(ValueError):
            rig.set_setpoint('A', -0.1)
        with self.assertRaises(ValueError):
            rig.advance(-0.1)
        with self.assertRaises(KeyError):
            rig.set_setpoint('missing', 1.0)


if __name__ == '__main__':
    unittest.main()
