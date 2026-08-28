"""No instrument I/O: protocol fixtures, fake serial and loopback transport."""

from copy import deepcopy
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from flow_controller.mexa.protocol import (ProtocolError, QUERIES, SerialReader,
                                           check_reply, decode_cycle, simulated_cycle)
from flow_controller.mexa.records import (AuditLog, LiveWindow, ReceivedSample, epoch,
                                          make_packet, utc_now, validate_packet)
from flow_controller.mexa.transport import (Lines, StreamClient, StreamServer, send_line,
                                            signature, validate_endpoint)
from flow_controller.mexa.bridge import Bridge
from flow_controller.core.csv_logger import CsvLogger


KEY = "a-test-key-with-at-least-32-characters"


def packet(seq=1, *, source=None, simulated=False):
    return make_packet(simulated_cycle(seq), source or str(uuid.uuid4()), seq,
                       simulated=simulated, validated=True, dry=True, cycle_s=.7)


def replies():
    return {key: bytearray.fromhex(value) for key, value in simulated_cycle(0)["raw"].items()}


def fixed_frames(frames):
    for frame in frames.values():
        frame[-1] = -sum(frame[:-1]) & 255
    return {key: bytes(value) for key, value in frames.items()}


class ProtocolTests(unittest.TestCase):
    def test_read_query_allowlist_and_scaling(self):
        self.assertEqual({q.command.hex() for q in QUERIES.values()}, {"020101fc", "0201aa53", "020140bd"})
        result = simulated_cycle(0)
        self.assertEqual(result["no_ppm"], 100)
        self.assertEqual(result["o2_percent"], 10)
        self.assertTrue(result["valid"])
        self.assertEqual(result["state"], "measuring")
        self.assertIn("automotive_low_co2_probe_warning", result["warnings"])

    def test_checksum_length_command_and_nack(self):
        good = bytes(replies()["channels"])
        for bad in (good[:-1], b"\x15" + good[1:], good[:1] + b"\x01" + good[2:],
                    good[:-1] + bytes([(good[-1] + 1) % 256])):
            with self.subTest(bad=bad), self.assertRaises(ProtocolError):
                check_reply("channels", bad)

    def test_optional_channels_are_missing_not_zero_or_old(self):
        frames = replies()
        frames["subsystem"][6] = 0
        result = decode_cycle(fixed_frames(frames))
        self.assertIsNone(result["no_ppm"])
        self.assertIsNone(result["o2_percent"])
        self.assertFalse(result["valid"])

    def test_known_alarm_flags_reject_measurements(self):
        for section, offset, bit, alarm in (
            ("status", 3, 8, "warm_up"), ("status", 9, 1, "calibration_error"),
            ("status", 12, 1, "calibration_error"), ("status", 13, 1, "calibration_error"),
            ("subsystem", 5, 2, "leak"), ("subsystem", 5, 8, "hang"),
            ("subsystem", 3, 32, "filter"),
        ):
            with self.subTest(alarm=alarm):
                frames = replies()
                frames[section][offset] |= bit
                result = decode_cycle(fixed_frames(frames))
                self.assertFalse(result["valid"])
                self.assertIn(alarm, result["alarms"])

    def test_standby_invalid(self):
        frames = replies()
        frames["subsystem"][4] = 0
        result = decode_cycle(fixed_frames(frames))
        self.assertEqual(result["state"], "standby")
        self.assertFalse(result["valid"])

    def test_independent_channel_fixture(self):
        frames = replies()
        # Independently specified channel bytes: CO2=.45%, CO=.12%, HC=57ppm,
        # O2=8.76%, NO=1234ppm. Not constructed by the simulator's encoder.
        frames["channels"][5:13] = bytes.fromhex("00 2d 00 0c 00 39 03 6c")
        frames["channels"][21:23] = bytes.fromhex("04 d2")
        decoded = decode_cycle(fixed_frames(frames))
        self.assertEqual([decoded[k] for k in ("co2_percent", "co_percent", "hc_ppm", "o2_percent", "no_ppm")],
                         [.45, .12, 57, 8.76, 1234])

    def test_signed_negative_readings_are_not_clipped_to_zero(self):
        frames = replies()
        frames["channels"][21:23] = b"\xff\xff"
        result = decode_cycle(fixed_frames(frames))
        self.assertEqual(result["no_ppm"], -1)
        self.assertFalse(result["valid"])

    def test_fragmented_serial_reads_and_no_control_commands(self):
        frames = fixed_frames(replies())
        commands = []
        settings = []

        class FakeSerial:
            def __init__(self, **kwargs):
                settings.append(kwargs)
                self.buffer = b""
            def reset_input_buffer(self):
                self.buffer = b""
            def open(self):
                assert self.port == "FAKE"
                assert self.dtr is False and self.rts is False
            def write(self, command):
                commands.append(command)
                name = next(name for name, query in QUERIES.items() if query.command == command)
                self.buffer = frames[name]
                return len(command)
            def read(self, count):
                data, self.buffer = self.buffer[:2], self.buffer[2:]
                return data
            def close(self):
                pass

        reader = SerialReader("FAKE", serial_factory=FakeSerial, sleep=lambda _: None)
        try:
            result = reader.read()
        finally:
            reader.close()
        self.assertEqual(commands, [query.command for query in QUERIES.values()])
        self.assertEqual(settings[0]["baudrate"], 9600)
        self.assertIsNone(settings[0]["port"])
        self.assertEqual(settings[0]["parity"], "N")
        self.assertEqual(result["no_ppm"], 100)
        self.assertEqual(reader.last_raw, {key: val.hex() for key, val in frames.items()})


class RecordTests(unittest.TestCase):
    def sample(self, **changes):
        p = packet()
        p.update(changes)
        return ReceivedSample(p, utc_now(), time.monotonic(), "audit.jsonl")

    def test_simulation_cannot_claim_validation(self):
        p = packet(simulated=True)
        self.assertFalse(p["validated"])
        self.assertIn("Simulated", ReceivedSample(p, utc_now(), time.monotonic(), "log").problem(experimental=True))

    def test_basis_and_validation_required_for_experiment_only(self):
        for fields, word in (({"basis": "unknown"}, "basis"), ({"validated": False}, "Validate"),
                             ({"o2_percent": 20.9}, "20.9")):
            sample = self.sample(**fields)
            self.assertEqual(sample.problem(), "")
            self.assertIn(word, sample.problem(experimental=True))

    def test_freshness_checks_both_clocks(self):
        sample = self.sample()
        now = epoch(sample.packet["acquired_at"])
        self.assertIn("Stale", sample.problem(now=now + 6))
        self.assertIn("clocks", sample.problem(now=now - 2))
        self.assertIn("Stale", sample.problem(now=now, mono=sample.received_mono + 6))

    def test_strict_packet_validation(self):
        for key, value in (("seq", True), ("seq", -1), ("no_ppm", float("nan")),
                           ("no_ppm", "123"), ("simulated", 0), ("source_id", "bad"),
                           ("acquired_at", "2026-08-27T12:00:00"), ("state", "bad"),
                           ("o2_percent", None), ("raw", {"channels": "zz"})):
            p = packet()
            p[key] = value
            with self.subTest(key=key), self.assertRaises((ValueError, TypeError)):
                validate_packet(p)

    def test_audit_is_flushed_and_round_trips_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            log = AuditLog(directory, "test")
            p = packet()
            p.update(valid=False, no_ppm=None, state="error", alarms=["timeout"])
            try:
                log.write(p, "received")
                raw = json.loads(log.path.read_text())
                self.assertIsNone(raw["no_ppm"])
                self.assertEqual(raw["raw"], p["raw"])
                with log.path.with_suffix(".csv").open(newline="") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(rows[0]["no_ppm"], "")
                self.assertEqual(rows[0]["valid"], "False")
            finally:
                log.close()

    def test_contiguous_live_window_and_correct_means(self):
        p = packet()
        base = datetime.now(timezone.utc)
        def sample(seq, seconds):
            item = deepcopy(p)
            item.update(seq=seq, acquired_at=(base + timedelta(seconds=seconds)).isoformat(), no_ppm=100 + seq)
            return ReceivedSample(item, utc_now(), time.monotonic(), "log.jsonl")
        with patch("flow_controller.mexa.records.time.time", return_value=base.timestamp()):
            capture = LiveWindow(sample(1, 0))
        for seq, seconds in ((2, 1), (3, 3), (4, 6)):
            with patch("flow_controller.mexa.records.time.time", return_value=base.timestamp() + seconds):
                capture.add(sample(seq, seconds))
        result = capture.finish({"start": base.isoformat(), "end": (base + timedelta(seconds=6)).isoformat()}, 5)
        self.assertEqual(result["samples"], 3)
        self.assertEqual(result["no_ppm"], 103)
        self.assertEqual(result["first_seq"], 2)
        self.assertEqual(result["last_seq"], 4)
        self.assertEqual(result["no_sd"], 1)
        with self.assertRaises(ValueError):
            capture.finish({"start": base.isoformat(), "end": (base + timedelta(seconds=5)).isoformat()}, 5)

    def test_live_window_rejects_duplicate_gap_source_change_and_alarm(self):
        for fields in ({"seq": 1}, {"seq": 3}, {"seq": 2, "source_id": str(uuid.uuid4())},
                       {"seq": 2, "valid": False, "alarms": ["filter"]}):
            base = self.sample()
            capture = LiveWindow(base)
            item = deepcopy(base.packet)
            item.update(fields)
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                capture.add(ReceivedSample(item, utc_now(), time.monotonic(), "audit.jsonl"))

    def test_live_window_requires_a_receiver_audit_record(self):
        sample = ReceivedSample(packet(), utc_now(), time.monotonic(), "")
        with self.assertRaisesRegex(ValueError, "Save received MEXA logs"):
            LiveWindow(sample)

    def test_combined_flow_log_labels_held_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            log = CsvLogger()
            path = Path(directory) / "flows.csv"
            log.start(path, {}, mexa=True)
            sample = {"mexa_source_id": "x", "mexa_seq": 1, "mexa_valid": True, "mexa_no_ppm": 100}
            log.write_row({}, (None, None, None), mexa=sample)
            log.write_row({}, (None, None, None), mexa=sample)
            log.write_row({}, (None, None, None), mexa={})
            log.stop()
            with path.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([r["mexa_new_sample"] for r in rows], ["True", "False", "False"])
            self.assertEqual(rows[2]["mexa_no_ppm"], "")


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.key = KEY
        # Reserve an OS-selected loopback port, never a serial connection.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()

    def server(self):
        server = StreamServer("127.0.0.1", self.port, self.key)
        self.addCleanup(server.stop)
        return server

    def test_authenticated_loopback(self):
        server = self.server()
        result = []
        ready = threading.Event()
        client = StreamClient("127.0.0.1", self.port, self.key,
                              lambda p: (result.append(p), ready.set()), lambda *args: None)
        self.addCleanup(client.stop)
        p = packet()
        server.publish(p)
        client.start()
        self.assertTrue(ready.wait(3))
        self.assertEqual(result, [p])

    def test_wrong_key_gets_no_measurements(self):
        server = self.server()
        server.publish(packet())
        result = []
        failed = threading.Event()
        client = StreamClient("127.0.0.1", self.port, "b" * 40, result.append,
                              lambda ready, text: failed.set() if "interrupted" in text else None)
        self.addCleanup(client.stop)
        client.start()
        self.assertTrue(failed.wait(3))
        self.assertEqual(result, [])

    def test_sequence_gap_interrupts_before_delivery(self):
        server = self.server()
        p = packet()
        server.publish(p)
        first = threading.Event()
        failed = threading.Event()
        result = []
        client = StreamClient("127.0.0.1", self.port, self.key,
                              lambda p: (result.append(p), first.set()),
                              lambda ready, text: failed.set() if "sequence gap" in text else None)
        self.addCleanup(client.stop)
        client.start()
        self.assertTrue(first.wait(3))
        p = dict(p, seq=3)
        server.publish(p)
        self.assertTrue(failed.wait(3))
        self.assertEqual(len(result), 1)

    def test_new_connection_has_new_challenge(self):
        server = self.server()
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as sock:
            first = Lines(sock).read()["challenge"]
            send_line(sock, {"proof": "wrong"})
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as sock:
            second = Lines(sock).read()["challenge"]
            send_line(sock, {"proof": "wrong"})
        self.assertNotEqual(first, second)
        self.assertNotEqual(signature(self.key, first + ":data"), signature(self.key, second + ":data"))

    def test_size_limited_stream_parser(self):
        class FakeSocket:
            def recv(self, count):
                return b"x" * count
        with self.assertRaisesRegex(ValueError, "Oversized"):
            Lines(FakeSocket()).read()

    def test_stop_during_unreachable_connection_is_bounded(self):
        client = StreamClient("127.0.0.1", self.port, self.key, lambda p: None, lambda *args: None)
        client.start()
        client.stop()
        self.assertFalse(client.thread.is_alive())

    def test_fragmented_network_lines(self):
        class FakeSocket:
            chunks = [b'{"a":', b'1}\n{"b":2}\n']
            def recv(self, count):
                return self.chunks.pop(0)
        reader = Lines(FakeSocket())
        self.assertEqual(reader.read(), {"a": 1})
        self.assertEqual(reader.read(), {"b": 2})

    def test_invalid_endpoint_and_key(self):
        for host, port, key in (("hostname", 1234, self.key), ("127.0.0.1", 0, self.key),
                                ("127.0.0.1", 1234, "short")):
            with self.assertRaises(ValueError):
                validate_endpoint(host, port, key)

    def test_bridge_simulation_streams_and_logs_without_serial(self):
        with tempfile.TemporaryDirectory() as directory, patch("flow_controller.mexa.bridge.SerialReader") as serial:
            ready = threading.Event()
            source = []
            bridge = Bridge(host="127.0.0.1", port=self.port, token=self.key, serial_port="NEVER",
                            directory=directory, simulated=True, validated=True, dry=True,
                            on_sample=lambda p: (source.append(p), ready.set()))
            try:
                self.assertTrue(ready.wait(3))
                self.assertTrue(source[0]["simulated"])
                self.assertFalse(source[0]["validated"])
                self.assertTrue(bridge.log.path.read_text())
                serial.assert_not_called()
            finally:
                self.assertTrue(bridge.stop())

    def test_log_failure_stops_stream_not_silently_unlogged_data(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(AuditLog, "write", side_effect=OSError("disk full")):
            messages = []
            source = []
            bridge = Bridge(host="127.0.0.1", port=self.port, token=self.key, serial_port="NEVER",
                            directory=directory, simulated=True, on_sample=source.append,
                            on_status=messages.append)
            bridge.thread.join(3)
            self.assertFalse(bridge.running)
            self.assertEqual(source, [])
            self.assertTrue(any("STOPPED" in message for message in messages))
            bridge.stop()

    def test_bridge_stream_only_never_creates_a_log(self):
        with patch("flow_controller.mexa.bridge.AuditLog", side_effect=AssertionError("Unexpected disk write")) as log:
            ready = threading.Event()
            bridge = Bridge(host="127.0.0.1", port=self.port, token=self.key, serial_port="NEVER",
                            save_logs=False, simulated=True, on_sample=lambda p: ready.set())
            try:
                self.assertTrue(ready.wait(3))
                self.assertIsNone(bridge.log)
                self.assertTrue(bridge.running)
                log.assert_not_called()
            finally:
                self.assertTrue(bridge.stop())

    def test_bridge_logging_requires_a_directory(self):
        with self.assertRaisesRegex(ValueError, "log directory"):
            Bridge(host="127.0.0.1", port=self.port, token=self.key, serial_port="NEVER", save_logs=True)


if __name__ == "__main__":
    unittest.main()
