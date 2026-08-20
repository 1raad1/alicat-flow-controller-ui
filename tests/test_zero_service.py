import asyncio
import os
import unittest
from queue import Queue
from types import SimpleNamespace

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


if __name__ == '__main__':
    unittest.main()
