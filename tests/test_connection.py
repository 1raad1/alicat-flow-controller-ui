import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from serial import SerialException

import flow_controller.core.session as session_module
from flow_controller.core.session import FlowSession
from flow_controller.domain.models import ControllerInfo, DiscoveryResult


class FakeHardware:
    def __init__(self):
        self.timeouts = 0


class SharedFakeMeter:
    instances = []
    events = []
    gas_names = {0: "Air", 3: "H2", 7: "H2"}

    def __init__(self, **kwargs):
        self.unit = kwargs["unit"]
        self.hw = FakeHardware()
        self.selected = {}
        self.instances.append(self)
        self.events.append("open")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.events.append("close")

    async def set_gas(self, gas):
        self.events.append(f"gas:{self.unit}:{gas}")
        self.selected[self.unit] = self.gas_names.get(gas, gas)

    async def get(self):
        self.events.append(f"get:{self.unit}")
        return {"gas": self.selected[self.unit], "mass_flow": 0.0}


def windows_port_denied():
    # pyserial embeds the Windows PermissionError repr in SerialException.
    return SerialException(
        "could not open port 'COM_TEST': "
        "PermissionError(13, 'Access is denied.', None, 5)")


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    async def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        SharedFakeMeter.instances = []
        SharedFakeMeter.events = []

    @staticmethod
    def session_with_scan():
        return SimpleNamespace(_log_conn=Mock(), connection_progress=Mock(),
                              last_scan=DiscoveryResult([
            ControllerInfo("A", {"gas": "H2"}, {0: "Air", 7: "H2"}),
            ControllerInfo("B", {"gas": "H2"}, {0: "Air", 3: "H2"}),
        ]))

    def configure_with_clock(self, meter_type, clock, session=None):
        session = session or self.session_with_scan()

        async def sleep_after_close(delay):
            self.assertEqual(meter_type.events[-1], "close")
            await clock.sleep(delay)

        with (patch("flow_controller.core.session.FlowMeter", meter_type),
              patch("flow_controller.core.session.time",
                    SimpleNamespace(monotonic=clock.monotonic)),
              patch("flow_controller.core.session.asyncio.sleep", sleep_after_close)):
            return asyncio.run(FlowSession._configure_async(
                session, "COM_TEST", 57600, ("A", "B"),
                {"A": "H2", "B": "H2"}, {"A": 7, "B": 3}))

    @staticmethod
    def locked_meter(clock, release_after=None, error_factory=windows_port_denied):
        class LockedMeter(SharedFakeMeter):
            attempts_at = []

            async def __aenter__(self):
                self.attempts_at.append(clock.now)
                return self

            async def set_gas(self, gas):
                # The real driver opens lazily on its first transaction.
                if release_after is None or clock.now < 100.0 + release_after:
                    raise error_factory()
                await super().set_gas(gas)

        return LockedMeter

    def test_cached_gas_indexes_are_case_insensitive(self):
        session = self.session_with_scan()

        indexes = FlowSession._cached_gas_indexes(
            session, {"A": "h2", "B": "H2"})

        self.assertEqual(indexes, {"A": 7, "B": 3})

    def test_configure_reuses_one_port_handle_for_all_units(self):
        session = self.session_with_scan()
        gas_map = {"A": "H2", "B": "H2"}
        indexes = FlowSession._cached_gas_indexes(session, gas_map)

        with patch("flow_controller.core.session.FlowMeter", SharedFakeMeter):
            confirmed, errors = asyncio.run(FlowSession._configure_async(
                session, "COM_TEST", 57600, ("A", "B"), gas_map, indexes))

        self.assertEqual(errors, {})
        self.assertEqual(set(confirmed), {"A", "B"})
        self.assertEqual(len(SharedFakeMeter.instances), 1)
        self.assertEqual(SharedFakeMeter.events.count("open"), 1)
        self.assertEqual(SharedFakeMeter.events.count("close"), 1)
        self.assertIn("gas:A:7", SharedFakeMeter.events)
        self.assertIn("gas:B:3", SharedFakeMeter.events)

    def test_active_gas_without_table_is_confirmed_without_guessing_index(self):
        class ActiveGasMeter(SharedFakeMeter):
            async def get(self):
                self.events.append(f"get:{self.unit}")
                return {"gas": "CustomMix", "mass_flow": 0.0}

        session = SimpleNamespace(last_scan=DiscoveryResult([
            ControllerInfo("C", {"gas": "CustomMix"}, {}),
        ]))
        gas_map = {"C": "CustomMix"}
        indexes = FlowSession._cached_gas_indexes(session, gas_map)

        with patch("flow_controller.core.session.FlowMeter", ActiveGasMeter):
            confirmed, errors = asyncio.run(FlowSession._configure_async(
                session, "COM_TEST", 57600, ("C",), gas_map, indexes))

        self.assertEqual(errors, {})
        self.assertEqual(set(confirmed), {"C"})
        self.assertFalse(any(event.startswith("gas:")
                             for event in ActiveGasMeter.events))

    def test_unknown_per_meter_gas_index_fails_instead_of_using_global_table(self):
        session = SimpleNamespace(last_scan=DiscoveryResult([
            ControllerInfo("C", {"gas": "Air"}, {}),
        ]))
        gas_map = {"C": "CustomMix"}
        indexes = FlowSession._cached_gas_indexes(session, gas_map)

        with patch("flow_controller.core.session.FlowMeter", SharedFakeMeter):
            confirmed, errors = asyncio.run(FlowSession._configure_async(
                session, "COM_TEST", 57600, ("C",), gas_map, indexes))

        self.assertEqual(confirmed, {})
        self.assertIn("No gas-table index is known", errors["C"])
        self.assertEqual(SharedFakeMeter.instances, [])
        self.assertFalse(any(event.startswith("gas:")
                             for event in SharedFakeMeter.events))

    def test_access_denied_open_recovers_after_five_seconds_with_per_unit_indexes(self):
        session = self.session_with_scan()
        clock = FakeClock()
        meter = self.locked_meter(clock, release_after=5.5)
        confirmed, errors = self.configure_with_clock(meter, clock, session)

        self.assertEqual(errors, {})
        self.assertEqual(set(confirmed), {"A", "B"})
        self.assertEqual(meter.attempts_at, [100.0 + i * 0.5 for i in range(12)])
        self.assertEqual(clock.sleeps, [0.5] * 11)
        self.assertEqual(meter.events.count("close"), 12)
        self.assertEqual([event for event in meter.events if event.startswith("gas:")],
                         ["gas:A:7", "gas:B:3"])
        session._log_conn.assert_called_once()
        session.connection_progress.emit.assert_called_once_with("Waiting for COM_TEST…")

    def test_permanently_locked_port_has_six_second_deadline_and_no_gas_writes(self):
        session = self.session_with_scan()
        clock = FakeClock()
        meter = self.locked_meter(clock)
        confirmed, errors = self.configure_with_clock(meter, clock, session)

        self.assertEqual(confirmed, {})
        self.assertEqual(meter.attempts_at, [100.0 + i * 0.5 for i in range(13)])
        self.assertEqual(clock.now, 106.0)
        self.assertEqual(clock.sleeps, [0.5] * 12)
        self.assertEqual(meter.events.count("close"), 13)
        self.assertFalse(any(event.startswith("gas:") for event in meter.events))
        self.assertIn(str(windows_port_denied()), errors["serial port"])
        self.assertIn("retries exhausted", errors["serial port"].lower())
        session._log_conn.assert_called_once()
        session.connection_progress.emit.assert_called_once_with("Waiting for COM_TEST…")

    def test_successful_first_open_has_no_retry_sleep_or_progress(self):
        session = self.session_with_scan()
        clock = FakeClock()
        confirmed, errors = self.configure_with_clock(SharedFakeMeter, clock, session)

        self.assertEqual(set(confirmed), {"A", "B"})
        self.assertEqual(errors, {})
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(SharedFakeMeter.events.count("open"), 1)
        session._log_conn.assert_not_called()
        session.connection_progress.emit.assert_not_called()

    def test_retry_sleep_is_capped_by_remaining_deadline(self):
        clock = FakeClock()
        meter = self.locked_meter(clock)
        with patch("flow_controller.core.session.CONNECT_OPEN_RETRY_S", 2.5):
            confirmed, errors = self.configure_with_clock(meter, clock)

        self.assertEqual(confirmed, {})
        self.assertIn("serial port", errors)
        self.assertEqual(clock.sleeps, [2.5, 2.5, 1.0])
        self.assertEqual(meter.attempts_at, [100.0, 102.5, 105.0, 106.0])

    def test_write_permission_error_is_not_replayed(self):
        class WriteDeniedMeter(SharedFakeMeter):
            async def set_gas(self, gas):
                self.events.append(f"write:{self.unit}:{gas}")
                raise PermissionError(13, "Access is denied")

        clock = FakeClock()
        confirmed, errors = self.configure_with_clock(WriteDeniedMeter, clock)

        self.assertEqual(confirmed, {})
        self.assertEqual(set(errors), {"A", "B"})
        self.assertTrue(all("PermissionError" in error for error in errors.values()))
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(WriteDeniedMeter.events.count("open"), 1)
        self.assertEqual(WriteDeniedMeter.events.count("close"), 1)
        self.assertEqual([event for event in WriteDeniedMeter.events if event.startswith("write:")],
                         ["write:A:7", "write:B:3"])

    def test_cancellation_during_retry_propagates_after_cleanup(self):
        class CancelClock(FakeClock):
            async def sleep(self, delay):
                raise asyncio.CancelledError()

        clock = CancelClock()
        meter = self.locked_meter(clock)
        with self.assertRaises(asyncio.CancelledError):
            self.configure_with_clock(meter, clock)
        self.assertEqual(meter.attempts_at, [100.0])
        self.assertEqual(meter.events, ["open", "close"])

    def test_cancellation_during_transaction_closes_handle_and_propagates(self):
        class CancelMeter(SharedFakeMeter):
            async def set_gas(self, gas):
                raise asyncio.CancelledError()

        clock = FakeClock()
        with self.assertRaises(asyncio.CancelledError):
            self.configure_with_clock(CancelMeter, clock)
        self.assertEqual(CancelMeter.events, ["open", "close"])
        self.assertEqual(clock.sleeps, [])

    def test_wrapped_port_open_denial_is_retried(self):
        def wrapped_error():
            denied = PermissionError(13, "permission denied")
            serial_error = SerialException("could not open port 'COM_TEST'")
            serial_error.__cause__ = denied
            wrapper = RuntimeError("driver transaction failed")
            wrapper.__context__ = serial_error
            return wrapper

        clock = FakeClock()
        meter = self.locked_meter(clock, release_after=0.5, error_factory=wrapped_error)
        confirmed, errors = self.configure_with_clock(meter, clock)

        self.assertEqual(set(confirmed), {"A", "B"})
        self.assertEqual(errors, {})
        self.assertEqual(clock.sleeps, [0.5])

    def test_port_denial_classifier_requires_serial_open_and_access_evidence(self):
        errno_error = OSError("permission denied")
        errno_error.errno = 13
        errno_open = SerialException("could not open port 'COM_TEST'")
        errno_open.__cause__ = errno_error
        windows_error = OSError("permission denied")
        windows_error.winerror = 5
        windows_open = SerialException("could not open port 'COM_TEST'")
        windows_open.__context__ = windows_error
        for error, expected in (
                (windows_port_denied(), True),
                (errno_open, True),
                (windows_open, True),
                (SerialException("could not open port 'COM_TEST': device not found"), False),
                (SerialException("write failed: Access is denied"), False),
                (PermissionError(13, "could not open port: Access is denied"), False)):
            with self.subTest(error=str(error)):
                self.assertEqual(session_module._is_port_open_denied(error), expected)

    def test_non_access_error_is_not_retried(self):
        class BrokenMeter(SharedFakeMeter):
            attempts = 0

            async def __aenter__(self):
                type(self).attempts += 1
                raise SerialException("could not open port 'COM_TEST': adapter disconnected")

        session = self.session_with_scan()
        clock = FakeClock()
        confirmed, errors = self.configure_with_clock(BrokenMeter, clock, session)

        self.assertEqual(confirmed, {})
        self.assertEqual(BrokenMeter.attempts, 1)
        self.assertEqual(clock.sleeps, [])
        self.assertIn("adapter disconnected", errors["serial port"])


if __name__ == "__main__":
    unittest.main()
