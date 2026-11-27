"""Regressions for verified plan aborts and declared ramp durations."""

import asyncio
import os
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication

from flow_controller.core.experiment_plan import (
    AbortProcedure, ExperimentPlan, PlanStage,
)
from flow_controller.core.session import (
    DEFERRED_ZERO_TIMEOUT_S, FlowSession, MAX_RAMP_STEPS,
)


class ControlReviewRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(self.session.shutdown)
        self.session.unit_prefs = {}
        self.session.selection = {'A': ('NH3', 'Zone 1')}
        self.session.assignments['nh3_rich'] = 'A'
        self.session.controllers_connected = True
        self.session.is_monitoring = True
        self.session._last_sp['A'] = 1.0
        self.session._monitor_future = Future()
        self.addCleanup(self.finish_monitor)
        self.jobs = []
        self.session._submit = self.submit
        self.failures = []
        self.session.failed.connect(
            lambda title, detail: self.failures.append((title, detail)))

    def finish_monitor(self):
        if self.session._monitor_future is not None:
            if not self.session._monitor_future.done():
                self.session._monitor_future.set_result(None)

    def submit(self, coroutine, callback=None):
        self.jobs.append((coroutine, callback))
        self.addCleanup(coroutine.close)
        return Future()

    def start_plan(self):
        plan = ExperimentPlan('abort test', AbortProcedure('zero_all'), (
            PlanStage('wait', {'nh3_rich': 1.0}, min_dwell_s=5, timeout_s=30),))
        self.assertTrue(self.session.experiment_plans.set_plan(plan))
        self.assertTrue(self.session.experiment_plans.start())

    def complete_zero(self):
        events = []

        class Controller:
            def __init__(self, **kwargs):
                self.unit = kwargs['unit']

            async def __aenter__(self):
                events.append(('open', self.unit))
                return self

            async def __aexit__(self, *_args):
                events.append(('close', self.unit))

            async def set_flow_rate(self, value):
                events.append(('set', value))

            async def get(self):
                return {'setpoint': 0.0}

        coroutine, callback = self.jobs.pop(0)
        with patch('flow_controller.core.session.FlowController', Controller):
            result = asyncio.run(coroutine)
        future = Future()
        future.set_result(result)
        callback(future)
        self.assertEqual(events, [('open', 'A'), ('set', 0.0), ('close', 'A')])
        self.assertFalse(self.session._zero_action_active)
        self.assertFalse(self.session._zero_locked_units)
        self.assertEqual(self.session._last_sp['A'], 0.0)
        self.assertEqual(self.failures, [])

    def test_monitor_stop_defers_plan_zero_until_serial_handles_close(self):
        self.start_plan()
        self.session.setpoint_queue.put(('B', 2.0))
        self.session.stop_monitoring()
        self.assertEqual(self.session.experiment_plans.state, 'aborted')
        self.assertTrue(self.session._zero_action_active)
        self.assertIn('A', self.session._zero_locked_units)
        self.assertFalse(self.session.queue_setpoint('A', 2.0))
        self.assertEqual(self.jobs, [])
        future = self.session._monitor_future
        future.set_result(None)
        self.session._on_monitor_done(future)
        self.assertEqual(len(self.jobs), 1)
        self.complete_zero()
        self.assertEqual(self.session.setpoint_queue.get_nowait(), ('B', 2.0))
        self.assertTrue(self.session.setpoint_queue.empty())

    def test_reconnect_waits_for_plan_abort_zero(self):
        self.start_plan()
        self.assertTrue(self.session.restart_connection())
        self.assertEqual(self.session.experiment_plans.state, 'aborted')
        self.assertTrue(self.session._restart_pending)
        self.assertEqual(self.jobs, [])
        future = self.session._monitor_future
        future.set_result(None)
        with patch.object(self.session, '_begin_serial_restart') as restart:
            self.session._on_monitor_done(future)
            restart.assert_not_called()
            self.complete_zero()
            restart.assert_called_once_with()
        self.assertTrue(self.session.setpoint_queue.empty())

    def test_disconnect_still_executes_already_requested_plan_abort(self):
        self.start_plan()
        self.session.disconnect_all()
        self.assertFalse(self.session.controllers_connected)
        future = self.session._monitor_future
        future.set_result(None)
        self.session._on_monitor_done(future)
        self.complete_zero()
        self.assertFalse(self.session.estop_armed)

    def test_serial_close_error_does_not_discard_deferred_zero(self):
        self.start_plan()
        self.session.stop_monitoring()
        future = self.session._monitor_future
        future.set_exception(OSError('connection lost'))
        self.session._on_monitor_done(future)
        self.complete_zero()

    def record_zero_finished(self):
        finished = []
        self.session.zero_finished.connect(
            lambda request, confirmed, errors: finished.append(
                (request, confirmed, errors)))
        return finished

    def test_deferred_zero_timer_runs_while_waiting_for_monitor(self):
        self.start_plan()
        self.session.stop_monitoring()
        self.assertIsNotNone(self.session._deferred_zero_request)
        self.assertTrue(self.session._deferred_zero_timer.isActive())
        self.assertEqual(self.session._deferred_zero_timer.interval(),
                         int(DEFERRED_ZERO_TIMEOUT_S * 1000))

    def test_deferred_zero_times_out_when_monitor_never_closes(self):
        finished = self.record_zero_finished()
        self.start_plan()
        self.session.stop_monitoring()
        units = self.session._deferred_zero_request.units
        self.session._deferred_zero_timed_out()
        self.assertEqual(len(finished), 1)
        request, confirmed, errors = finished[0]
        self.assertEqual(request.units, units)
        self.assertEqual(confirmed, {})
        self.assertEqual(set(errors), set(units))
        for reason in errors.values():
            self.assertIn('zero was not sent', reason)
            self.assertIn(f'{DEFERRED_ZERO_TIMEOUT_S:g} s', reason)
        self.assertFalse(self.session._zero_action_active)
        self.assertFalse(self.session._emergency_stop_active)
        self.assertEqual(self.session._zero_locked_units, set())
        self.assertIsNone(self.session._deferred_zero_request)
        self.assertIsNone(self.session._active_zero_request)
        self.assertTrue(self.session.estop_armed)
        self.assertEqual(self.session._last_sp['A'], 0.0)
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.failures, [])

    def test_monitor_closing_after_timeout_sends_no_late_zero(self):
        finished = self.record_zero_finished()
        self.start_plan()
        self.session.stop_monitoring()
        self.session._deferred_zero_timed_out()
        future = self.session._monitor_future
        future.set_result(None)
        self.session._on_monitor_done(future)
        self.assertEqual(self.jobs, [])
        self.assertEqual(len(finished), 1)
        self.assertFalse(self.session._zero_action_active)
        self.assertFalse(self.session._emergency_stop_active)
        self.assertEqual(self.session._last_sp['A'], 0.0)

    def test_restart_after_deferred_zero_timeout_still_reopens_port(self):
        self.start_plan()
        self.session.stop_monitoring()
        self.session._deferred_zero_timed_out()
        self.assertTrue(self.session.restart_connection())
        self.assertTrue(self.session._restart_pending)
        future = self.session._monitor_future
        future.set_result(None)
        with patch.object(self.session, '_begin_serial_restart') as restart:
            self.session._on_monitor_done(future)
            restart.assert_called_once_with()
        self.assertEqual(self.jobs, [])

    def test_timely_monitor_close_cancels_deferred_zero_timeout(self):
        finished = self.record_zero_finished()
        self.start_plan()
        self.session.stop_monitoring()
        future = self.session._monitor_future
        future.set_result(None)
        self.session._on_monitor_done(future)
        self.assertFalse(self.session._deferred_zero_timer.isActive())
        self.complete_zero()
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0][2], {})
        self.session._deferred_zero_timed_out()
        self.assertEqual(len(finished), 1)
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.failures, [])

    def test_long_and_fractional_ramps_preserve_requested_duration(self):
        self.session._live_samples['A'] = {'flow': 0.0}
        self.session._ramps.start = Mock(return_value=True)
        for duration in (1000.0, 10.25, 0.25):
            with self.subTest(duration=duration):
                self.assertTrue(self.session.start_ramp(
                    'nh3_rich', 10.0, seconds=duration))
                args = self.session._ramps.start.call_args.args
                steps, interval = args[2:4]
                self.assertLessEqual(steps, MAX_RAMP_STEPS)
                self.assertAlmostEqual(steps * interval, max(0.5, duration))

    def test_declared_slow_rate_reaches_scheduler_without_acceleration(self):
        self.session._live_samples['A'] = {'flow': 0.0}
        self.session.unit_prefs = {'A': {'ramp': 0.01, 'ramp_off': False}}
        self.session._ramps.start = Mock(return_value=True)
        self.assertTrue(self.session.set_role_setpoint('nh3_rich', 10.0))
        args = self.session._ramps.start.call_args.args
        self.assertLessEqual(10.0 / (args[2] * args[3]), 0.01)
