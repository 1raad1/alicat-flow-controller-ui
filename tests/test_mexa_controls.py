"""Local analyser controls: fake serial only; never touch a physical instrument."""

from copy import deepcopy
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from flow_controller.mexa.bridge import Bridge
from flow_controller.mexa.protocol import MODE_COMMANDS, ProtocolError, SerialReader, simulated_cycle
from flow_controller.mexa.records import AuditLog, validate_packet
from flow_controller.mexa.transport import StreamClient, connection_hint


KEY = "local-control-tests-shared-key-12345"


class FakeSerial:
    response = bytes.fromhex("06 a6 00 54")

    def __init__(self, **settings):
        self.writes = []
        self.buffer = b""

    def open(self):
        pass

    def close(self):
        pass

    def reset_input_buffer(self):
        self.buffer = b""

    def write(self, command):
        self.writes.append(command)
        self.buffer = self.response
        return len(command)

    def read(self, count):
        result, self.buffer = self.buffer[:1], self.buffer[1:]
        return result


class ModeProtocolTests(unittest.TestCase):
    def test_recovered_commands_and_fragmented_ack(self):
        for mode, command, reply in (("meas", "0201a657", "06a60054"),
                                     ("standby", "0201a756", "06a70053")):
            with self.subTest(mode=mode):
                reader = SerialReader("FAKE", serial_factory=FakeSerial, sleep=lambda _: None)
                self.addCleanup(reader.close)
                reader.port.response = bytes.fromhex(reply)
                self.assertEqual(reader.set_mode(mode).hex(), reply)
                self.assertEqual(reader.port.writes, [bytes.fromhex(command)])
                self.assertEqual(reader.last_control_reply, reply)
                self.assertEqual(reader.last_raw, {})
                self.assertEqual(MODE_COMMANDS[mode].delay, .240)

    def test_invalid_mode_never_writes(self):
        reader = SerialReader("FAKE", serial_factory=FakeSerial, sleep=lambda _: None)
        self.addCleanup(reader.close)
        for mode in ("zero", "span", "purge", "raw", "MEAS"):
            with self.assertRaises(ValueError):
                reader.set_mode(mode)
        self.assertEqual(reader.port.writes, [])

    def test_bad_reply_rejected_without_retry(self):
        for reply in ("06a70053", "06a60055", "15a6000144", "", "06a6"):
            with self.subTest(reply=reply):
                reader = SerialReader("FAKE", serial_factory=FakeSerial, sleep=lambda _: None)
                self.addCleanup(reader.close)
                reader.port.response = bytes.fromhex(reply)
                # Advance the deadline deterministically for missing/truncated data.
                with patch("flow_controller.mexa.protocol.time.monotonic",
                           side_effect=[0, .1, .2, .3, .4, .5, .7]):
                    with self.assertRaises(ProtocolError):
                        reader.set_mode("meas")
                self.assertEqual(len(reader.port.writes), 1)
                self.assertEqual(reader.last_control_reply, reply)


class FakeReader:
    def __init__(self):
        self.state = "measuring"
        self.calls = []
        self.last_control_reply = ""
        self.last_raw = {}
        self.fail_control = False

    def read(self):
        self.calls.append(("read", threading.get_ident()))
        cycle = simulated_cycle(1)
        cycle.update(state=self.state, valid=self.state == "measuring")
        self.last_raw = cycle["raw"]
        return cycle

    def set_mode(self, mode):
        self.calls.append((mode, threading.get_ident()))
        if self.fail_control:
            raise TimeoutError("No ACK")
        self.last_control_reply = "06a60054" if mode == "meas" else "06a70053"
        self.state = "measuring" if mode == "meas" else "standby"
        return bytes.fromhex(self.last_control_reply)

    def close(self):
        pass


class BridgeControlTests(unittest.TestCase):
    def setUp(self):
        self.reader = FakeReader()
        self.factory = patch("flow_controller.mexa.bridge.SerialReader", return_value=self.reader)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.samples = []
        self.messages = []
        self.first = threading.Event()

    def start_bridge(self, **kwargs):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        defaults = dict(host="127.0.0.1", port=port, token=KEY, serial_port="FAKE",
                        save_logs=False, validated=True, dry=True,
                        on_sample=lambda p: (self.samples.append(p), self.first.set()),
                        on_control=self.messages.append)
        defaults.update(kwargs)
        bridge = Bridge(**defaults)
        self.addCleanup(bridge.stop)
        self.assertTrue(self.first.wait(3))
        return bridge

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline:
            threading.Event().wait(.01)
        self.assertTrue(predicate())

    def test_no_controls_on_start_or_stop(self):
        bridge = self.start_bridge()
        self.assertTrue(bridge.stop())
        self.assertTrue(self.reader.calls)
        self.assertTrue(all(name == "read" for name, _ in self.reader.calls))

    def test_control_uses_serial_owner_and_invalidates_before_write(self):
        bridge = self.start_bridge()
        original = self.reader.set_mode
        def set_mode(mode):
            self.assertEqual(self.samples[-1]["control"]["phase"], "requested")
            self.assertFalse(self.samples[-1]["valid"])
            self.assertFalse(self.samples[-1]["validated"])
            return original(mode)
        self.reader.set_mode = set_mode
        bridge.request_mode("standby")
        self.wait_for(lambda: bridge.can_request_mode("meas"))
        self.assertEqual(self.samples[-1]["state"], "standby")
        bridge.request_mode("meas")
        self.wait_for(lambda: len(self.messages) == 2 and bridge.can_request_mode("standby"))
        bridge.stop()
        self.assertEqual({ident for _, ident in self.reader.calls}, {bridge.thread.ident})
        self.assertEqual([name for name, _ in self.reader.calls if name != "read"], ["standby", "meas"])
        marker = next(i for i, p in enumerate(self.samples) if "control" in p)
        self.assertTrue(all(not p["validated"] for p in self.samples[marker:]))
        self.assertEqual([p["seq"] for p in self.samples], list(range(1, len(self.samples) + 1)))
        self.assertTrue(self.samples[-1]["valid"])
        self.assertTrue(all(validate_packet(p) for p in self.samples))

    def test_pending_and_stopped_requests_cannot_execute_twice(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def sample(p):
            self.samples.append(p)
            self.first.set()
            release.wait(3)
        bridge = self.start_bridge(on_sample=sample)
        bridge.request_mode("standby")
        with self.assertRaises(ValueError):
            bridge.request_mode("standby")
        bridge.stop_event.set()
        release.set()
        self.assertTrue(bridge.stop())
        self.assertTrue(all(name == "read" for name, _ in self.reader.calls))
        with self.assertRaises(ValueError):
            bridge.request_mode("meas")

    def test_unknown_stale_or_wrong_state_refuses_control(self):
        bridge = self.start_bridge()
        with self.assertRaises(ValueError):
            bridge.request_mode("zero")
        with bridge.mode_lock:
            bridge._last_read_at = time.monotonic() - 10
        with self.assertRaises(ValueError):
            bridge.request_mode("standby")
        for state, mode in (("warm_up", "meas"), ("error", "standby"), ("measuring", "meas")):
            with bridge.mode_lock:
                bridge._last_read_at = time.monotonic()
                bridge._last_state = state
            with self.assertRaises(ValueError):
                bridge.request_mode(mode)

    def test_uncertain_command_is_not_replayed_after_reconnect(self):
        self.reader.fail_control = True
        bridge = self.start_bridge()
        bridge.request_mode("standby")
        self.wait_for(lambda: self.messages and bridge.can_request_mode("standby"))
        bridge.stop()
        controls = [p["control"] for p in self.samples if "control" in p]
        self.assertEqual([c["phase"] for c in controls], ["requested", "failed"])
        self.assertIn("uncertain", controls[-1]["detail"])
        self.assertEqual([name for name, _ in self.reader.calls if name != "read"], ["standby"])
        self.assertFalse(self.samples[-1]["validated"])

    def test_simulated_controls_never_open_serial(self):
        bridge = self.start_bridge(simulated=True)
        bridge.request_mode("standby")
        self.wait_for(lambda: bridge.can_request_mode("meas"))
        bridge.stop()
        self.assertFalse(self.reader.calls)
        self.assertTrue(all(p["simulated"] and not p["validated"] for p in self.samples))

    def test_audit_failure_prevents_control_write(self):
        original = AuditLog.write
        def write(log, packet, *args):
            if "control" in packet:
                raise OSError("disk full")
            return original(log, packet, *args)
        with tempfile.TemporaryDirectory() as directory, patch.object(AuditLog, "write", write):
            bridge = self.start_bridge(directory=directory, save_logs=True)
            bridge.request_mode("standby")
            bridge.thread.join(3)
            self.assertFalse(bridge.running)
            self.assertTrue(all(name == "read" for name, _ in self.reader.calls))

    def test_control_record_cannot_claim_validity(self):
        bridge = self.start_bridge(simulated=True)
        bridge.request_mode("standby")
        self.wait_for(lambda: bool(self.messages))
        bridge.stop()
        original = next(p for p in self.samples if "control" in p)
        for fields in ({"validated": True}, {"valid": True}, {"control": {"mode": "zero"}}):
            packet = deepcopy(original)
            packet.update(fields)
            with self.assertRaises(ValueError):
                validate_packet(packet)


class ConnectionDiagnosticTests(unittest.TestCase):
    def test_connect_and_authentication_failures_are_distinct(self):
        self.assertIn("before authentication", connection_hint("connect", ("10.97.74.19", 61234)))
        self.assertIn("10.97.74.19:61234", connection_hint("connect", ("10.97.74.19", 61234)))
        self.assertIn("TCP reached", connection_hint("authenticate", ("10.97.74.19", 61234)))

    def test_receiver_rejects_wildcard_multicast_and_broadcast(self):
        for address in ("0.0.0.0", "224.0.0.1", "255.255.255.255"):
            with self.assertRaises(ValueError):
                StreamClient(address, 61234, KEY, lambda p: None, lambda *args: None)


if __name__ == "__main__":
    unittest.main()
