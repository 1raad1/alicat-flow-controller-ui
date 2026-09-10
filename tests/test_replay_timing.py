"""Replay timing across a rate-limited return between passes."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication

from flow_controller.core.sequence import HOLD, LINEAR, Keyframe, Sequence, Track
from flow_controller.core.session import FlowSession


class ReplayTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_return_time_does_not_shorten_next_pass(self):
        session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(session.shutdown)
        session.controllers_connected = True
        session.is_monitoring = True
        session.assignments['nh3_rich'] = 'A'
        session.unit_prefs = {'A': {'ramp': 1.0, 'ramp_off': False}}
        session._settle_enabled = False
        sequence = Sequence(tracks=[Track(
            key='nh3_rich', label='NH3', keyframes=[
                Keyframe(0.0, 1.0, LINEAR), Keyframe(10.0, 9.0, HOLD)])])

        with patch('flow_controller.core.session.time.monotonic') as clock:
            clock.return_value = 100.0
            self.assertTrue(session.start_replay(sequence, repeats=2))
            session._replay_timer.stop()
            clock.return_value = 110.0
            session._sequence_tick()
            self.assertEqual(session._player.cycle, 2)
            for now in range(111, 119):
                clock.return_value = float(now)
                session._sequence_tick()
                self.assertEqual(session._player.position, 0.0)
            self.assertEqual(session._player.commanded['nh3_rich'], 1.0)
            self.assertFalse(session._player.returning)
            clock.return_value = 119.0
            session._sequence_tick()
            self.assertEqual(session._player.position, 1.0)
            self.assertAlmostEqual(session._player.commanded['nh3_rich'], 1.8)
            clock.return_value = 128.0
            session._sequence_tick()
            self.assertEqual(session.sequence_state, 'idle')


if __name__ == '__main__':
    unittest.main()
