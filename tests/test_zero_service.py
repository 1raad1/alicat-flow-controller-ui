import asyncio
import unittest
from queue import Queue

from flow_controller.app import AlicatDetectorUI
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
    def make_app(self):
        app = AlicatDetectorUI.__new__(AlicatDetectorUI)
        app._zero_request_queue = Queue()
        app.setpoint_queue = Queue()
        app._last_sp = {'A': 7.0, 'B': 4.0}
        app.completions = []
        app._post_ui = lambda *args: app.completions.append(args)
        return app

    async def test_zero_uses_existing_connection_and_has_queue_priority(self):
        app = self.make_app()
        request = ZeroRequest('fuel', ('A',))
        app._zero_request_queue.put(request)
        app.setpoint_queue.put(('A', 8.0))
        app.setpoint_queue.put(('B', 4.5))
        controllers = {
            'A': FakeController([0.0]),
            'B': FakeController([4.0]),
        }

        serviced = await app._service_zero_requests(controllers)

        self.assertTrue(serviced)
        self.assertEqual(controllers['A'].setpoints, [0.0])
        self.assertEqual(controllers['B'].setpoints, [])
        self.assertEqual(app._last_sp['A'], 0.0)
        self.assertEqual(app.setpoint_queue.get_nowait(), ('B', 4.5))
        _, completed_request, confirmed, errors = app.completions[0]
        self.assertIs(completed_request, request)
        self.assertEqual(confirmed, {'A': 0.0})
        self.assertEqual(errors, {})

    async def test_zero_retries_and_reports_unconfirmed_controller(self):
        app = self.make_app()
        app._zero_request_queue.put(ZeroRequest('all', ('A', 'B')))
        controllers = {
            'A': FakeController([0.5, 0.0]),
            'B': FakeController([OSError('offline'), float('nan')]),
        }

        await app._service_zero_requests(controllers)

        self.assertEqual(controllers['A'].setpoints, [0.0, 0.0])
        self.assertEqual(controllers['B'].setpoints, [0.0, 0.0])
        _, _request, confirmed, errors = app.completions[0]
        self.assertEqual(confirmed, {'A': 0.0})
        self.assertIn('B', errors)


if __name__ == '__main__':
    unittest.main()
