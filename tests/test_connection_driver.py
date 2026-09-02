"""Exercise the real Alicat handle lifecycle with an in-memory serial stream."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alicat import FlowMeter
import serial

from flow_controller.core.session import FlowSession


class MemorySerialStream:
    def __init__(self):
        self.commands = []
        self.gas_indexes = {}
        self.closed = False
        self.close_awaited = False

    def write(self, data):
        command = data.decode().strip()
        self.commands.append(command)
        if '$$W46=' in command:
            self.gas_indexes[command[0]] = int(command.split('=')[1])

    async def readuntil(self, separator):
        command = self.commands[-1]
        unit = command[0]
        if '$$' in command:
            response = f'{unit} {self.gas_indexes[unit]}'
        else:
            response = f'{unit} 14.7 25 0 0 0 H2'
        return response.encode() + separator

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.close_awaited = True


class ConnectionDriverTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_connection(self, release_after):
        port = 'COM_TEST_DRIVER'
        self.assertNotIn(port, FlowMeter.open_ports)
        clock = SimpleNamespace(now=0.0)
        stream = MemorySerialStream()
        attempts = []
        session = SimpleNamespace(
            connection_progress=SimpleNamespace(emit=Mock()), _log_conn=Mock())

        async def open_serial_connection(**kwargs):
            self.assertEqual(kwargs['url'], port)
            attempts.append(clock.now)
            # The driver's lazy-open task must belong to a fresh, sole owner.
            self.assertEqual(FlowMeter.open_ports[port][1], 1)
            if clock.now < release_after:
                raise serial.SerialException(
                    f"could not open port '{port}': "
                    "PermissionError(13, 'Access is denied.', None, 5)")
            return stream, stream

        async def sleep(delay):
            # No failed driver instance may remain registered between retries.
            self.assertNotIn(port, FlowMeter.open_ports)
            clock.now += delay

        with (patch('serial_asyncio_fast.open_serial_connection',
                    side_effect=open_serial_connection),
              patch('flow_controller.core.session.time',
                    SimpleNamespace(monotonic=lambda: clock.now)),
              patch('flow_controller.core.session.asyncio.sleep', side_effect=sleep)):
            confirmed, errors = await FlowSession._configure_async(
                session, port, 57600, ('A', 'B'),
                {'A': 'H2', 'B': 'H2'}, {'A': 7, 'B': 3})

        self.assertNotIn(port, FlowMeter.open_ports)
        return confirmed, errors, attempts, stream

    async def test_real_driver_recovers_after_five_seconds_and_closes_shared_handle(self):
        confirmed, errors, attempts, stream = await self.exercise_connection(5.5)

        self.assertEqual(errors, {})
        self.assertEqual(set(confirmed), {'A', 'B'})
        self.assertEqual(attempts, [index * 0.5 for index in range(12)])
        self.assertEqual(stream.commands, [
            'A$$W46=7', 'A$$R46', 'A', 'B$$W46=3', 'B$$R46', 'B'])
        self.assertTrue(stream.closed)
        self.assertTrue(stream.close_awaited)

    async def test_real_driver_exhausts_retries_without_writing_or_leaking_registry(self):
        confirmed, errors, attempts, stream = await self.exercise_connection(float('inf'))

        self.assertEqual(confirmed, {})
        self.assertIn('Access is denied', errors['serial port'])
        self.assertIn('Automatic retries exhausted', errors['serial port'])
        self.assertEqual(attempts, [index * 0.5 for index in range(13)])
        self.assertEqual(stream.commands, [])


if __name__ == '__main__':
    unittest.main()
