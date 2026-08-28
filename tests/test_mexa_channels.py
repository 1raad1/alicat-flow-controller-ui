"""Complete reported-channel acquisition and export, without instrument I/O."""

from copy import deepcopy
import csv
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from flow_controller.core.csv_logger import CsvLogger
from flow_controller.core.mexa_controller import MexaController
from flow_controller.mexa.protocol import PEF_QUERY, QUERIES, ProtocolError, SerialReader, decode_cycle, decode_pef, simulated_cycle
from flow_controller.mexa.records import (AuditLog, CHANNEL_FIELDS, ReceivedSample, additional_reading_text,
                                          make_packet, utc_now, validate_packet)
from flow_controller.mexa.transport import StreamServer
from flow_controller.ui.qt_mexa import MexaTab


def frames():
    result = {key: bytearray.fromhex(value) for key, value in simulated_cycle(0)["raw"].items()}
    result["subsystem"][6] = 15
    result["channels"][5:17] = bytes.fromhex("00 2D 00 0C 00 39 03 6C 00 93 04 D2")
    result["channels"][21:27] = bytes.fromhex("04 D2 09 60 00 55")
    for frame in result.values():
        frame[-1] = -sum(frame[:-1]) & 255
    return {key: bytes(value) for key, value in result.items()}


EXPECTED = {"no_ppm": 1234, "o2_percent": 8.76, "co_percent": .12, "co2_percent": .45,
            "hc_ppm": 57, "afr": 14.7, "lambda": 1.234, "rpm": 2400, "oil_temperature_c": 85, "pef": .5}


class FakeSerial:
    def __init__(self, **settings):
        self.frames = frames()
        self.writes = []
        self.buffer = b""
        self.pef_reply = bytes.fromhex("06 18 00 01 F4 ED")

    def open(self):
        pass

    def close(self):
        pass

    def reset_input_buffer(self):
        self.buffer = b""

    def write(self, command):
        self.writes.append(command)
        if command == PEF_QUERY.command:
            self.buffer = self.pef_reply
        else:
            name = next(key for key, query in QUERIES.items() if query.command == command)
            self.buffer = self.frames[name]
        return len(command)

    def read(self, count):
        result, self.buffer = self.buffer[:min(2, count)], self.buffer[min(2, count):]
        return result


def full_cycle():
    reader = SerialReader("FAKE", serial_factory=FakeSerial, sleep=lambda _: None)
    try:
        return reader.read()
    finally:
        reader.close()


def packet():
    return make_packet(full_cycle(), str(uuid.uuid4()), 1, simulated=False, validated=True, dry=True, cycle_s=.95)


class ChannelProtocolTests(unittest.TestCase):
    def test_all_channel_offsets_scales_and_pef(self):
        result = full_cycle()
        for key, expected in EXPECTED.items():
            self.assertEqual(result[key], expected, key)
        self.assertEqual(result["options"], 15)
        self.assertEqual(result["raw"], {key: value.hex() for key, value in frames().items()})
        self.assertEqual(result["raw_pef"], "06180001f4ed")
        self.assertEqual(result["pef_error"], "")

    def test_missing_options_never_reuse_or_fabricate_sensor_values(self):
        data = {key: bytearray(value) for key, value in frames().items()}
        data["subsystem"][6] = 0
        data["subsystem"][-1] = -sum(data["subsystem"][:-1]) & 255
        result = decode_cycle({key: bytes(value) for key, value in data.items()})
        for key in ("no_ppm", "o2_percent", "rpm", "oil_temperature_c"):
            self.assertIsNone(result[key])
        self.assertEqual(result["afr"], 14.7)
        self.assertFalse(result["valid"])

    def test_signed_auxiliary_values_are_retained_not_clipped(self):
        data = {key: bytearray(value) for key, value in frames().items()}
        data["channels"][13:17] = bytes.fromhex("FF F6 FF FF")
        data["channels"][23:27] = bytes.fromhex("FF FF FF FE")
        data["channels"][-1] = -sum(data["channels"][:-1]) & 255
        result = decode_cycle({key: bytes(value) for key, value in data.items()})
        self.assertEqual([result[key] for key in ("afr", "lambda", "rpm", "oil_temperature_c")], [-1, -.001, -1, -2])

    def test_pef_query_failure_does_not_erase_channels_or_reuse_previous_pef(self):
        reader = SerialReader("FAKE", serial_factory=FakeSerial, sleep=lambda _: None)
        self.addCleanup(reader.close)
        self.assertEqual(reader.read()["pef"], .5)
        reader.port.pef_reply = bytes.fromhex("06 18 00 01 F4 EE")
        result = reader.read()
        self.assertIsNone(result["pef"])
        self.assertEqual(result["no_ppm"], 1234)
        self.assertTrue(result["valid"])
        self.assertIn("checksum", result["pef_error"])
        self.assertIn("pef_unavailable", result["warnings"])
        self.assertEqual(result["raw_pef"], "06180001f4ee")
        self.assertEqual(reader.last_control_reply, "")

    def test_pef_reply_is_checked(self):
        self.assertEqual(decode_pef(bytes.fromhex("06 18 00 01 F4 ED")), .5)
        for bad in (b"", bytes.fromhex("06 18 00 01 F4 EE"), bytes.fromhex("15 18 00 00 D3")):
            with self.assertRaises(ProtocolError):
                decode_pef(bad)

    def test_extended_packet_fields_are_bounded_and_typed(self):
        for key, value in (("afr", float("nan")), ("lambda", "1.0"), ("rpm", True),
                           ("oil_temperature_c", float("inf")), ("pef", "bad"),
                           ("options", -1), ("options", True), ("options", 256),
                           ("raw_pef", "zz"), ("raw_pef", "00" * 7), ("pef_error", 4),
                           ("pef_error", "failed")):
            p = packet()
            p[key] = value
            with self.subTest(key=key), self.assertRaises((ValueError, TypeError)):
                validate_packet(p)

    def test_old_v1_sender_remains_compatible_without_fake_values(self):
        p = packet()
        for key in (*CHANNEL_FIELDS[5:], "options", "raw_pef", "pef_error"):
            p.pop(key)
        self.assertIs(validate_packet(p), p)
        sample = ReceivedSample(p, utc_now(), time.monotonic(), "log")
        text = additional_reading_text(sample)
        self.assertIn("CO 0.12%", text)
        self.assertIn("AFR —", text)
        self.assertIn("RPM —", text)


class ChannelLogAndUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.controller = MexaController()
        self.addCleanup(self.controller.shutdown)

    def test_complete_packet_survives_network_and_both_csv_exports(self):
        p = packet()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        key = "all-channel-loopback-test-shared-key"
        server = StreamServer("127.0.0.1", port, key)
        self.addCleanup(server.stop)
        server.publish(p)
        self.controller.connect_bridge("127.0.0.1", port, key, self.directory.name)
        deadline = time.monotonic() + 4
        while self.controller.latest is None and time.monotonic() < deadline:
            self.app.processEvents()
            threading.Event().wait(.01)
        self.assertIsNotNone(self.controller.latest)
        self.assertEqual(self.controller.latest.packet, p)
        saved = json.loads(self.controller.log.path.read_text())
        with self.controller.log.path.with_suffix(".csv").open(newline="") as handle:
            audit = next(csv.DictReader(handle))
        snapshot = self.controller.csv_snapshot(datetime.now())
        flow = CsvLogger()
        flow_path = Path(self.directory.name) / "flow.csv"
        flow.start(flow_path, {}, mexa=True)
        self.assertTrue(flow.write_row({}, (None, None, None), mexa=snapshot))
        flow.stop()
        with flow_path.open(newline="") as handle:
            row = next(csv.DictReader(handle))
        for field, expected in EXPECTED.items():
            self.assertEqual(saved[field], expected, field)
            self.assertEqual(float(audit[field]), expected, field)
            self.assertEqual(float(row[f"mexa_reported_{field}"]), expected, field)
        self.assertEqual(row["mexa_raw_channels"], p["raw"]["channels"])
        self.assertEqual(audit["raw_status"], p["raw"]["status"])
        self.assertEqual(row["mexa_raw_pef"], p["raw_pef"])
        tab = MexaTab(self.controller)
        self.addCleanup(tab.close)
        for expected in ("CO 0.12%", "CO₂ 0.45%", "HC 57 ppm", "AFR 14.7", "λ 1.234", "PEF 0.500", "RPM 2400", "85 °C"):
            self.assertIn(expected, tab.additional_readings.text())

    def test_stale_and_future_channels_are_not_attached_to_flow_rows(self):
        for seconds in (-10, 2):
            p = packet()
            p["acquired_at"] = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
            self.controller.latest = ReceivedSample(p, utc_now(), time.monotonic(), "log")
            snapshot = self.controller.csv_snapshot(datetime.now())
            for field in CHANNEL_FIELDS:
                self.assertIsNone(snapshot[f"mexa_reported_{field}"])
            self.assertEqual(snapshot["mexa_raw_channels"], "")
            self.assertNotIn("2400", additional_reading_text(self.controller.latest))

    def test_error_and_control_packets_do_not_retain_previous_extra_channels(self):
        from flow_controller.mexa.bridge import Bridge
        cycle = Bridge._control_cycle("standby", "requested")
        p = make_packet(cycle, str(uuid.uuid4()), 2, simulated=False, validated=False, dry=True, cycle_s=.1)
        for field in CHANNEL_FIELDS:
            self.assertIsNone(p[field])
        self.assertEqual(p["raw_pef"], "")
        sample = ReceivedSample(p, utc_now(), time.monotonic(), "log")
        self.assertIn("AFR —", additional_reading_text(sample))

    def test_new_diagnostic_text_is_csv_safe(self):
        p = packet()
        p.update(pef=None, pef_error="=BAD()", warnings=["+BAD()"])
        self.controller.latest = ReceivedSample(p, utc_now(), time.monotonic(), "log")
        snapshot = self.controller.csv_snapshot(datetime.now())
        self.assertEqual(snapshot["mexa_pef_error"], "'=BAD()")
        self.assertEqual(snapshot["mexa_warnings"], "'+BAD()")
        log = AuditLog(self.directory.name, "audit")
        try:
            log.write(p)
            with log.path.with_suffix(".csv").open(newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["pef_error"], "'=BAD()")
        finally:
            log.close()


if __name__ == "__main__":
    unittest.main()
