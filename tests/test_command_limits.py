"""Fail-closed command ceilings at the session and hardware boundaries."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from flow_controller.core.sequence import HOLD, Keyframe, Sequence, Track
from flow_controller.core.session import FlowSession


class CommandLimitTests(unittest.TestCase):
    def setUp(self):
        self.session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(self.session.shutdown)
        # Preferences are deliberately persisted in production, but these tests
        # must not alter the operator's own per-unit declarations.
        self.session.unit_prefs = {}
        self.session.assignments['nh3_rich'] = 'A'
        self.session._live_samples['A'] = {'flow': 0.0}
        self.failures = []
        self.session.failed.connect(
            lambda title, detail: self.failures.append((title, detail)))
        self.persist = patch(
            'flow_controller.core.session.unit_prefs.save', return_value=None)
        self.persist.start()
        self.addCleanup(self.persist.stop)

    def sequence(self, value):
        return Sequence(name='ceiling test', tracks=[Track(
            key='nh3_rich', label='NH3',
            keyframes=[Keyframe(0.0, value, HOLD), Keyframe(1.0, value, HOLD)])])

    def test_full_scale_is_display_only_and_max_flow_limits_commands(self):
        self.session.set_full_scale('A', 2.0)
        self.session.set_max_flow('A', 5.0)

        self.assertFalse(self.session.set_role_setpoint('nh3_rich', 6.0))
        self.assertTrue(self.session.set_role_setpoint('nh3_rich', 5.0))
        self.assertEqual(self.session.setpoint_queue.get_nowait(), ('A', 5.0))
        self.assertIn('command ceiling', self.failures[-1][1])

    def test_the_guarded_queue_enforces_the_same_limit_for_non_card_callers(self):
        self.session.set_max_flow('A', 5.0)
        self.assertFalse(self.session.queue_setpoint('A', 5.1))
        self.assertFalse(self.session.queue_setpoint('A', float('nan')))
        self.assertTrue(self.session.queue_setpoint('A', 5.0))
        self.assertEqual(self.session.setpoint_queue.get_nowait(), ('A', 5.0))

    def test_over_limit_sequences_cannot_be_adopted_or_loaded(self):
        self.session.set_max_flow('A', 5.0)
        sequence = self.sequence(6.0)
        self.assertFalse(self.session.set_sequence(sequence))
        self.assertIn('command ceiling', self.failures[-1][1])

        with tempfile.TemporaryDirectory() as directory:
            path = sequence.save(Path(directory) / 'over-limit.fcseq.json')
            self.assertIsNone(self.session.load_sequence(path))
        self.assertIn('command ceiling', self.failures[-1][1])

    def test_an_unassigned_track_can_be_loaded_for_editing(self):
        self.session.assignments['nh3_rich'] = None
        self.session.set_max_flow('A', 5.0)
        self.assertEqual(self.session.sequence_validation_errors(self.sequence(6.0)), [])

    def test_duplicate_roles_are_rejected_before_replay(self):
        sequence = self.sequence(1.0)
        sequence.tracks.append(Track(
            key='nh3_rich', label='duplicate',
            keyframes=[Keyframe(0.0, 1.0, HOLD)]))
        self.assertFalse(self.session.set_sequence(sequence))
        self.assertIn('Duplicate sequence role', self.failures[-1][1])

    def test_lowered_limit_drops_a_value_that_was_already_queued(self):
        class Controller:
            def __init__(self):
                self.writes = []

            async def set_flow_rate(self, value):
                self.writes.append(value)

        controller = Controller()
        self.assertTrue(self.session.queue_setpoint('A', 5.0))
        self.session.set_max_flow('A', 2.0)
        asyncio.run(self.session._write_pending_setpoints(
            {'A': controller}, {'A': 0}))
        self.assertEqual(controller.writes, [])

    def test_uncertain_setpoint_write_emits_communication_fault(self):
        class Controller:
            async def set_flow_rate(self, _value):
                raise asyncio.TimeoutError()

        faults = []
        self.session.communication_fault.connect(faults.append)
        self.assertTrue(self.session.queue_setpoint('A', 1.0))

        asyncio.run(self.session._write_pending_setpoints(
            {'A': Controller()}, {'A': 0}))

        self.assertEqual(len(faults), 1)
        self.assertIn('write timeout', faults[0])
        self.assertIn('uncertain', faults[0])

    def test_reconnect_restores_zero_instead_of_an_over_limit_last_value(self):
        class Controller:
            def __init__(self):
                self.writes = []

            async def set_flow_rate(self, value):
                self.writes.append(value)

            async def get(self):
                return {'setpoint': self.writes[-1]}

        controller = Controller()
        self.session._last_sp['A'] = 5.0
        self.session.unit_prefs['A'] = {'max_flow': 2.0}
        asyncio.run(self.session._restore_setpoints({'A': controller}))
        self.assertEqual(controller.writes, [0.0])
        self.assertEqual(self.session._last_sp['A'], 0.0)

    def test_lowering_limit_below_live_command_requests_verified_unit_zero(self):
        self.session.controllers_connected = True
        self.session.is_monitoring = True
        self.session._monitor_future = SimpleNamespace(
            done=lambda: False, result=lambda timeout: None)
        self.session._last_sp['A'] = 5.0
        self.session.set_max_flow('A', 2.0)
        request = self.session._zero_request_queue.get_nowait()
        self.assertEqual(request.scope, 'limit')
        self.assertEqual(request.units, ('A',))
        self.assertEqual(self.session._last_sp['A'], 0.0)


if __name__ == '__main__':
    unittest.main()
