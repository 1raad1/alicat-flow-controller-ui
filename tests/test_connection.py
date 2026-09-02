import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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


class RetryFakeMeter(SharedFakeMeter):
    attempts = 0

    async def __aenter__(self):
        type(self).attempts += 1
        return self

    async def set_gas(self, gas):
        if self.attempts < 3:
            raise PermissionError(13, "Access is denied")
        await super().set_gas(gas)


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        SharedFakeMeter.instances = []
        SharedFakeMeter.events = []
        RetryFakeMeter.instances = []
        RetryFakeMeter.events = []
        RetryFakeMeter.attempts = 0

    @staticmethod
    def session_with_scan():
        return SimpleNamespace(last_scan=DiscoveryResult([
            ControllerInfo("A", {"gas": "H2"}, {0: "Air", 7: "H2"}),
            ControllerInfo("B", {"gas": "H2"}, {0: "Air", 3: "H2"}),
        ]))

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

    def test_access_denied_open_is_retried_and_then_succeeds(self):
        session = self.session_with_scan()
        gas_map = {"A": "Air"}

        with (patch("flow_controller.core.session.FlowMeter", RetryFakeMeter),
              patch("flow_controller.core.session.CONNECT_OPEN_RETRY_S", 0)):
            confirmed, errors = asyncio.run(FlowSession._configure_async(
                session, "COM_TEST", 57600, ("A",), gas_map, {"A": 0}))

        self.assertEqual(errors, {})
        self.assertEqual(set(confirmed), {"A"})
        self.assertEqual(RetryFakeMeter.attempts, 3)
        self.assertEqual(RetryFakeMeter.events.count("close"), 3)

    def test_non_access_error_is_not_retried(self):
        class BrokenMeter(SharedFakeMeter):
            attempts = 0

            async def __aenter__(self):
                type(self).attempts += 1
                raise OSError("adapter disconnected")

        session = self.session_with_scan()
        with patch("flow_controller.core.session.FlowMeter", BrokenMeter):
            confirmed, errors = asyncio.run(FlowSession._configure_async(
                session, "COM_TEST", 57600, ("A",), {"A": "Air"}, {"A": 0}))

        self.assertEqual(confirmed, {})
        self.assertEqual(BrokenMeter.attempts, 1)
        self.assertIn("adapter disconnected", errors["serial port"])


if __name__ == "__main__":
    unittest.main()
