import asyncio
import os
import unittest
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from flow_controller.core.session import FlowSession
from flow_controller.domain.safety import ZeroRequest


class FakeController:
    def __init__(self, readbacks):
        self.readbacks = iter(readbacks)
        self.setpoints = []

    async def set_flow_rate(self, value):
        self.setpoints.append(value)

    async def get(self):
        value = next(self.readbacks)
        if isinstance(value, Exception):
            raise value
        return {'setpoint': value}


class ZeroServiceTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self):
        session = FlowSession(worker=SimpleNamespace(shutdown=lambda: None))
        self.addCleanup(session.shutdown)
        session._zero_request_queue = Queue()
        session.setpoint_queue = Queue()
        session._last_sp = {'A': 7.0, 'B': 4.0}
        session.completions = []
        session._post = lambda *args: session.completions.append(args)
        return session

    async def test_zero_uses_existing_connection_and_has_queue_priority(self):
        session = self.make_session()
        request = ZeroRequest('fuel', ('A',))
        session._zero_request_queue.put(request)
        session.setpoint_queue.put(('A', 8.0))
        session.setpoint_queue.put(('B', 4.5))
        controllers = {
            'A': FakeController([0.0]),
            'B': FakeController([4.0]),
        }

        serviced = await session._service_zero_requests(controllers)

        self.assertTrue(serviced)
        self.assertEqual(controllers['A'].setpoints, [0.0])
        self.assertEqual(controllers['B'].setpoints, [])
        self.assertEqual(session._last_sp['A'], 0.0)
        self.assertEqual(session.setpoint_queue.get_nowait(), ('B', 4.5))
        _, completed_request, confirmed, errors = session.completions[0]
        self.assertIs(completed_request, request)
        self.assertEqual(confirmed, {'A': 0.0})
        self.assertEqual(errors, {})

    async def test_zero_retries_and_reports_unconfirmed_controller(self):
        session = self.make_session()
        session._zero_request_queue.put(ZeroRequest('all', ('A', 'B')))
        controllers = {
            'A': FakeController([0.5, 0.0]),
            'B': FakeController([OSError('offline'), float('nan')]),
        }

        await session._service_zero_requests(controllers)

        self.assertEqual(controllers['A'].setpoints, [0.0, 0.0])
        self.assertEqual(controllers['B'].setpoints, [0.0, 0.0])
        _, _request, confirmed, errors = session.completions[0]
        self.assertEqual(confirmed, {'A': 0.0})
        self.assertIn('B', errors)

    async def test_zero_interrupts_poll_delay(self):
        session = self.make_session()
        session.is_monitoring = True
        session.poll_interval_s = 5.0
        sleeps = []

        async def sleep(delay):
            sleeps.append(delay)
            session._zero_request_queue.put(ZeroRequest('all', ('A',)))

        with patch('flow_controller.core.session.asyncio.sleep', sleep):
            await session._wait_for_next_poll()

        self.assertEqual(sleeps, [0.05])

    async def test_zero_preempts_local_batch_and_preserves_unaffected_command(self):
        session = self.make_session()
        events = []

        class Controller:
            def __init__(self, unit):
                self.unit = unit

            async def set_flow_rate(self, value):
                events.append((self.unit, value))
                if self.unit == 'A' and value == 8.0:
                    session._zero_locked_units.update(('A', 'B'))
                    session._last_sp.update(A=0.0, B=0.0)
                    session._zero_request_queue.put(ZeroRequest('fuel', ('A', 'B')))

            async def get(self):
                return {'setpoint': 0.0}

        for command in [('A', 8.0), ('C', 3.0), ('B', 9.0)]:
            session.setpoint_queue.put(command)
        await session._write_pending_setpoints(
            {unit: Controller(unit) for unit in ('A', 'B', 'C')}, {})

        self.assertEqual(events, [('A', 8.0), ('A', 0.0), ('B', 0.0), ('C', 3.0)])
        self.assertEqual(session._last_sp['A'], 0.0)
        self.assertEqual(session._last_sp['B'], 0.0)

    async def test_failed_zero_does_not_restore_inflight_nonzero_command(self):
        session = self.make_session()

        class Controller:
            async def set_flow_rate(self, value):
                if value:
                    session._zero_locked_units.add('A')
                    session._last_sp['A'] = 0.0
                    session._zero_request_queue.put(ZeroRequest('all', ('A',)))
                else:
                    raise OSError('connection lost')

        session.setpoint_queue.put(('A', 8.0))
        await session._write_pending_setpoints({'A': Controller()}, {})

        self.assertEqual(session._last_sp['A'], 0.0)
        self.assertIn('A', session.completions[0][3])

    async def test_reconnect_honours_zero_locks(self):
        session = self.make_session()
        session._watchdog_locked_units.add('A')
        controller = FakeController([0.0])

        await session._restore_setpoints({'A': controller})

        self.assertEqual(controller.setpoints, [0.0])

    async def test_zero_requested_during_restore_precedes_next_restore(self):
        session = self.make_session()
        events = []

        class Controller:
            def __init__(self, unit):
                self.unit = unit
                self.value = 0.0

            async def set_flow_rate(self, value):
                self.value = value
                events.append((self.unit, value))
                if self.unit == 'A' and value == 7.0:
                    session._last_sp['A'] = 0.0
                    session._zero_request_queue.put(ZeroRequest('fuel', ('A',)))

            async def get(self):
                return {'setpoint': self.value}

        await session._restore_setpoints(
            {unit: Controller(unit) for unit in ('A', 'B')})

        self.assertEqual(events, [('A', 7.0), ('A', 0.0), ('B', 4.0)])

    async def test_zero_lock_rejects_even_small_positive_commands(self):
        session = self.make_session()
        session._zero_locked_units.add('A')
        self.assertFalse(session.queue_setpoint('A', 0.0005))
        controller = FakeController([])
        session.setpoint_queue.put(('A', 0.0005))
        await session._write_pending_setpoints({'A': controller}, {})
        self.assertEqual(controller.setpoints, [])


if __name__ == '__main__':
    unittest.main()
