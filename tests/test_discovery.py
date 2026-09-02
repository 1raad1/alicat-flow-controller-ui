import asyncio
import unittest

from flow_controller.services.discovery import DiscoveryService


class FakeHardware:
    def __init__(self):
        self.timeouts = 0


class FakeMeter:
    def __init__(self, replies, events, **kwargs):
        self.unit = kwargs["unit"]
        self.keys = []
        self.hw = FakeHardware()
        self._replies = replies
        self._events = events
        events.append("open")

    async def get(self):
        self._events.append(f"get:{self.unit}")
        reply = self._replies[self.unit]
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def close(self):
        self._events.append("close")


class FakeProtocol:
    def __init__(self, tables, events):
        self._tables = tables
        self._events = events

    def query_gases(self, _port, unit, _baudrate):
        if "close" not in self._events:
            raise AssertionError("gas table queried before discovery port closed")
        self._events.append(f"gas:{unit}")
        return self._tables[unit]


class DiscoveryServiceTests(unittest.TestCase):
    def test_scan_closes_discovery_port_before_per_controller_gas_queries(self):
        events = []
        replies = {
            "A": {"gas": "Air", "mass_flow": 1.0},
            "B": {"gas": "H2", "mass_flow": 2.0},
        }
        tables = {"A": {0: "Air", 1: "N2"}, "B": {0: "H2", 4: "He"}}
        service = DiscoveryService(
            FakeProtocol(tables, events),
            meter_factory=lambda **kwargs: FakeMeter(replies, events, **kwargs),
        )

        result = asyncio.run(service.scan("COM_TEST", 115200, ("A", "B"), 0.01))

        self.assertIsNone(result.error)
        self.assertEqual((result.port, result.baudrate), ("COM_TEST", 115200))
        self.assertEqual([controller.unit for controller in result.controllers], ["A", "B"])
        self.assertEqual(result.controllers[0].gas_options(), ["Air", "N2"])
        self.assertEqual(result.controllers[1].gas_options(), ["H2", "He"])
        self.assertLess(events.index("close"), events.index("gas:A"))
        self.assertLess(events.index("gas:A"), events.index("gas:B"))

    def test_expected_timeout_is_skipped_and_scan_continues(self):
        events = []
        replies = {
            "A": asyncio.TimeoutError(),
            "B": {"gas": "Air", "mass_flow": 1.0},
        }
        service = DiscoveryService(
            FakeProtocol({"B": {0: "Air"}}, events),
            meter_factory=lambda **kwargs: FakeMeter(replies, events, **kwargs),
        )

        result = asyncio.run(service.scan("COM_TEST", 57600, ("A", "B"), 0.01))

        self.assertIsNone(result.error)
        self.assertEqual([controller.unit for controller in result.controllers], ["B"])
        self.assertIn("get:B", events)

    def test_fatal_serial_error_stops_scan_and_still_closes_port(self):
        events = []
        replies = {
            "A": {"gas": "Air", "mass_flow": 1.0},
            "B": OSError("adapter disconnected"),
            "C": {"gas": "H2", "mass_flow": 2.0},
        }
        service = DiscoveryService(
            FakeProtocol({"A": {0: "Air"}}, events),
            meter_factory=lambda **kwargs: FakeMeter(replies, events, **kwargs),
        )

        result = asyncio.run(
            service.scan("COM_TEST", 19200, ("A", "B", "C"), 0.01))

        self.assertEqual(result.error, "OSError: adapter disconnected")
        self.assertEqual([controller.unit for controller in result.controllers], ["A"])
        self.assertIn("close", events)
        self.assertNotIn("get:C", events)
        self.assertFalse(any(event.startswith("gas:") for event in events))


if __name__ == "__main__":
    unittest.main()

