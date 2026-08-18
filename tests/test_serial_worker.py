import asyncio
import unittest

from flow_controller.infrastructure.serial_worker import SerialIOWorker


class SerialIOWorkerTests(unittest.TestCase):
    def test_submit_returns_coroutine_result(self):
        worker = SerialIOWorker()
        try:
            future = worker.submit(asyncio.sleep(0, result=42))
            self.assertEqual(future.result(timeout=2), 42)
        finally:
            worker.shutdown()


if __name__ == "__main__":
    unittest.main()

