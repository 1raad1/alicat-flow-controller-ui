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

    def replay_player(self, key, prefs, *, saved_rate=0.1):
        session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(session.shutdown)
        session.controllers_connected = True
        session.is_monitoring = True
        session.assignments[key] = 'A'
        session.unit_prefs = {'A': dict(prefs)}
        session._settle_enabled = False
        sequence = Sequence(tracks=[Track(
            key=key, label=key, ramp_rate=saved_rate, keyframes=[
                Keyframe(0.0, 0.0, HOLD), Keyframe(1.0, 10.0, HOLD)])])
        self.assertTrue(session.start_replay(sequence, repeats=2))
        session._replay_timer.stop()
        return session, session._player

    def test_ramp_off_overrides_saved_rate_on_forward_and_repeat_return(self):
        for key in ('nh3_rich', 'rich_air'):
            for prefs in ({'ramp_off': True}, {}):
                with self.subTest(key=key, prefs=prefs):
                    _session, player = self.replay_player(key, prefs)
                    player.tick(1.1, 1.0)
                    self.assertEqual(player.commanded[key], 10.0)
                    self.assertTrue(player.next_cycle())
                    player.tick(0.0, 1.0)
                    self.assertEqual(player.commanded[key], 0.0)
                    self.assertFalse(player.returning)

    def test_enabled_ramping_keeps_saved_rate_and_protected_default(self):
        _session, player = self.replay_player(
            'nh3_rich', {'ramp_off': False})
        player.tick(1.1, 1.0)
        self.assertAlmostEqual(player.commanded['nh3_rich'], 0.1)

        _session, player = self.replay_player(
            'rich_air', {'ramp_off': False}, saved_rate=None)
        player.tick(1.1, 0.25)
        self.assertAlmostEqual(player.commanded['rich_air'], 2.5)

    def test_turning_ramp_off_during_replay_applies_on_next_tick(self):
        session, player = self.replay_player(
            'rich_air', {'ramp_off': False})
        player.tick(1.1, 1.0)
        self.assertAlmostEqual(player.commanded['rich_air'], 0.1)

        session.unit_prefs['A']['ramp_off'] = True
        player.tick(1.1, 0.1)
        self.assertEqual(player.commanded['rich_air'], 10.0)
        self.assertTrue(player.next_cycle())
        player.tick(0.0, 0.1)
        self.assertEqual(player.commanded['rich_air'], 0.0)
        self.assertFalse(player.returning)

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
