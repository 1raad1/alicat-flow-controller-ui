"""Relay transport tests: loopback/synthetic records only, never instrument I/O."""

import asyncio
from datetime import datetime, timedelta, timezone
import csv
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import zipfile
from urllib.request import urlopen

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from flow_controller.core.mexa_controller import MexaController
from mexa_bridge.app import BridgeWindow
from mexa_bridge.bridge import Bridge
from mexa_bridge.protocol import simulated_cycle
from mexa_bridge.records import CHANNEL_FIELDS, MAX_LINE, make_packet
from mexa_bridge.relay import (RelayPublisher, RelayReceiver, decode, encode, quiet_logger,
                                        receive, send, validate_relay_url)
from mexa_bridge.relay_server import RelayService
from mexa_bridge.transport import signature
from flow_controller.ui.qt_mexa import MexaTab

PUBLISH_KEY = "publisher-test-key-" + "p" * 32
RECEIVE_KEY = "receiver-test-key-" + "r" * 32
SHARED_KEY = "measurement-test-key-" + "s" * 32


def packet(seq=1, source_id=None):
    cycle = simulated_cycle(seq)
    cycle.update(afr=14.7, **{"lambda": 1.234}, rpm=2400, oil_temperature_c=85,
                 options=15, no_ppm=-2, valid=False, alarms=["no_out_of_range"])
    return make_packet(cycle, source_id or str(uuid.uuid4()), seq, simulated=True,
                       validated=False, dry=True, cycle_s=.9)


def wait_for(predicate, timeout=5, qt=False):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if qt:
            QApplication.processEvents()
        if predicate():
            return True
        threading.Event().wait(.01)
    return bool(predicate())


class RunningRelay:
    def __init__(self, port=0, context=None):
        self.ready = threading.Event()
        self.error = None
        self.service = RelayService(PUBLISH_KEY, RECEIVE_KEY)
        self.port, self.context = port, context
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("Test relay failed to start")
        if self.error:
            raise self.error
        scheme = "wss" if context else "ws"
        self.url = f"{scheme}://127.0.0.1:{self.port}/mexa"

    def run(self):
        async def serve():
            self.loop = asyncio.get_running_loop()
            self.stop_event = asyncio.Event()
            async with await self.service.start(port=self.port, ssl_context=self.context) as server:
                self.port = server.sockets[0].getsockname()[1]
                self.ready.set()
                await self.stop_event.wait()
        try:
            asyncio.run(serve())
        except Exception as exc:
            self.error = exc
            self.ready.set()

    def stop(self):
        if self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.stop_event.set)
            self.thread.join(4)
        if self.thread.is_alive():
            raise RuntimeError("Test relay failed to stop")


async def join(url, role, key):
    ws = await connect(url, proxy=None, compression=None, max_size=MAX_LINE,
                       close_timeout=.3, logger=quiet_logger("test.relay"))
    await send(ws, {"type": "join", "version": 1, "role": role, "key": key})
    return ws


class RelayConfigurationTests(unittest.TestCase):
    def test_only_secure_remote_urls_or_explicit_numeric_loopback(self):
        for url in ("wss://relay.example/mexa", "wss://relay.example:443/mexa", "ws://127.0.0.1:8765/mexa", "ws://[::1]:8765/mexa"):
            self.assertEqual(validate_relay_url(url), url)
        for url in ("", "ws://relay.example/mexa", "https://relay.example/mexa", "ws://10.97.74.19/mexa",
                    "ws://localhost/mexa", "wss://user:secret@relay.example/mexa", "wss://relay.example/mexa?key=secret",
                    "wss://relay.example/mexa#secret", "wss://relay.example/", "wss://relay.example:0/mexa",
                    "wss://relay.example:70000/mexa", "wss://0.0.0.0/mexa", "wss://relay.\nexample/mexa"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_relay_url(url)

    def test_keys_are_required_and_distinct(self):
        for pub, rec in (("", RECEIVE_KEY), (PUBLISH_KEY, "short"), (PUBLISH_KEY, PUBLISH_KEY)):
            with self.assertRaises(ValueError):
                RelayService(pub, rec)
        with self.assertRaises(ValueError):
            RelayReceiver("wss://relay.example/mexa", SHARED_KEY, SHARED_KEY, lambda p: None, lambda *s: None)

    def test_built_in_relay_cannot_bind_outside_numeric_loopback(self):
        async def check():
            service = RelayService(PUBLISH_KEY, RECEIVE_KEY)
            for host in ("0.0.0.0", "192.0.2.10", "::", "localhost"):
                with self.subTest(host=host), self.assertRaises(ValueError):
                    await service.start(host, 0)
            # TLS is useful for client verification tests, not an escape from
            # the built-in relay's loopback-only boundary.
            with self.assertRaises(ValueError):
                await service.start("0.0.0.0", 0, ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))
        asyncio.run(check())

    def test_messages_are_bounded_and_do_not_echo_untrusted_text(self):
        for raw in (b"bytes", "[]", "not-json", "[" * 5000, "x" * (MAX_LINE + 1)):
            with self.assertRaisesRegex(ValueError, "Invalid relay"):
                decode(raw)
        with self.assertRaises(ValueError):
            encode({"payload": "x" * MAX_LINE})

    def test_reader_package_has_transports_but_no_standalone_host_or_runtime_data(self):
        from build_mexa_package import build
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "bridge.zip"
            build(archive_path)
            prefix = "MEXA-584L-bridge/"
            with zipfile.ZipFile(archive_path) as archive:
                self.assertIsNone(archive.testzip())
                names = archive.namelist()
                for name in ("mexa_bridge/relay.py", "mexa_bridge/transport.py", "mexa_bridge/app.py",
                             "docs/MEXA_SETUP.md", "docs/MEXA_QUICK_TUNNEL.md"):
                    self.assertIn(prefix + name, names)
                for name in ("mexa_bridge/relay_server.py", "mexa_bridge/relay_host.py",
                             "run_mexa_relay_local.bat", "requirements-relay.txt", "CACHYOS_START_HERE.md"):
                    self.assertNotIn(prefix + name, names)
                self.assertFalse(any(name.endswith((".csv", ".jsonl", ".exe", ".pem", ".env")) for name in names))
                self.assertIn(b"websockets", archive.read(prefix + "requirements-mexa.txt"))
            with self.assertRaises(FileExistsError):
                build(archive_path)


class RelayProtocolTests(unittest.TestCase):
    def setUp(self):
        self.relay = RunningRelay()
        self.addCleanup(self.relay.stop)

    def test_wrong_role_key_rejected_and_duplicate_does_not_evict_owner(self):
        async def check():
            wrong = await join(self.relay.url, "publisher", RECEIVE_KEY)
            with self.assertRaises(ConnectionClosed) as caught:
                await receive(wrong)
            self.assertEqual(caught.exception.rcvd.code, 4001)
            owner = await join(self.relay.url, "publisher", PUBLISH_KEY)
            self.assertEqual(await receive(owner), {"type": "accepted"})
            duplicate = await join(self.relay.url, "publisher", PUBLISH_KEY)
            with self.assertRaises(ConnectionClosed) as caught:
                await receive(duplicate)
            self.assertEqual(caught.exception.rcvd.code, 4009)
            await owner.ping()
            await owner.close()
        asyncio.run(check())

    def test_endpoint_and_browser_origins_rejected(self):
        async def check():
            for url, options in ((self.relay.url + "?key=secret", {}), (self.relay.url, {"origin": "https://untrusted.example"})):
                with self.assertRaises(InvalidStatus):
                    async with connect(url, proxy=None, logger=quiet_logger("test.relay"), **options):
                        self.fail("Unexpected connection")
        asyncio.run(check())

    def test_health_endpoint_has_no_credentials_or_measurements(self):
        with urlopen(self.relay.url.replace("ws://", "http://").replace("/mexa", "/healthz"), timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"MEXA relay running\n")

    def test_rate_limit_closes_flooding_publisher(self):
        async def check():
            pub = await join(self.relay.url, "publisher", PUBLISH_KEY)
            await receive(pub)
            rec = await join(self.relay.url, "receiver", RECEIVE_KEY)
            await receive(rec)
            await receive(rec)
            await receive(pub)
            await send(rec, {"type": "challenge", "nonce": "a" * 64, "proof": "b" * 64})
            await receive(pub)
            await send(pub, {"type": "proof", "signature": "c" * 64})
            await receive(rec)
            try:
                for _ in range(10):
                    await send(pub, {"type": "sample", "payload": "{}", "signature": "d" * 64})
                await receive(pub)
            except ConnectionClosed as exc:
                self.assertEqual(exc.rcvd.code, 1008)
            else:
                self.fail("Flood was not rejected")
            await rec.close()
        asyncio.run(check())

    def test_receiver_cannot_send_control_or_telemetry(self):
        async def check():
            pub = await join(self.relay.url, "publisher", PUBLISH_KEY)
            self.assertEqual(await receive(pub), {"type": "accepted"})
            rec = await join(self.relay.url, "receiver", RECEIVE_KEY)
            self.assertEqual(await receive(rec), {"type": "accepted"})
            self.assertEqual(await receive(rec), {"type": "paired"})
            self.assertEqual(await receive(pub), {"type": "paired"})
            await send(rec, {"type": "command", "mode": "meas"})
            with self.assertRaises(ConnectionClosed) as caught:
                await receive(rec)
            self.assertEqual(caught.exception.rcvd.code, 1008)
            with self.assertRaises(ConnectionClosed):
                await receive(pub)
        asyncio.run(check())

    def test_oversized_message_is_rejected(self):
        async def check():
            ws = await join(self.relay.url, "publisher", PUBLISH_KEY)
            await receive(ws)
            await ws.send("x" * (MAX_LINE + 1))
            with self.assertRaises(ConnectionClosed) as caught:
                await ws.recv()
            self.assertEqual(caught.exception.rcvd.code, 1009)
        asyncio.run(check())

    def test_all_channels_and_invalid_flags_arrive_without_serial_or_disk(self):
        received, pub_status, rec_status = [], [], []
        publisher = RelayPublisher(self.relay.url, PUBLISH_KEY, SHARED_KEY, pub_status.append)
        receiver = RelayReceiver(self.relay.url, RECEIVE_KEY, SHARED_KEY, received.append, lambda ready, s: rec_status.append(s))
        self.addCleanup(publisher.stop)
        self.addCleanup(receiver.stop)
        receiver.start()
        self.assertTrue(wait_for(lambda: any("streaming new" in s for s in pub_status)))
        p = packet()
        publisher.publish(p)
        self.assertTrue(wait_for(lambda: received))
        self.assertEqual(received, [p])
        self.assertFalse(received[0]["valid"])
        self.assertEqual(set(CHANNEL_FIELDS) - received[0].keys(), set())
        self.assertTrue(any("authenticated through relay" in s for s in rec_status))

    def test_pre_pairing_sample_is_not_replayed(self):
        received, statuses = [], []
        publisher = RelayPublisher(self.relay.url, PUBLISH_KEY, SHARED_KEY, statuses.append)
        self.addCleanup(publisher.stop)
        p = packet()
        publisher.publish(p)
        receiver = RelayReceiver(self.relay.url, RECEIVE_KEY, SHARED_KEY, received.append, lambda *s: None)
        self.addCleanup(receiver.stop)
        receiver.start()
        self.assertTrue(wait_for(lambda: any("streaming new" in s for s in statuses)))
        self.assertFalse(wait_for(lambda: received, .2))
        fresh = packet(2, p["source_id"])
        publisher.publish(fresh)
        self.assertTrue(wait_for(lambda: received))
        self.assertEqual(received, [fresh])

    def test_wrong_shared_key_never_delivers_samples(self):
        statuses, received = [], []
        publisher = RelayPublisher(self.relay.url, PUBLISH_KEY, SHARED_KEY, statuses.append)
        receiver = RelayReceiver(self.relay.url, RECEIVE_KEY, "wrong-measurement-key-" + "w" * 32, received.append, lambda *s: None)
        self.addCleanup(publisher.stop)
        self.addCleanup(receiver.stop)
        receiver.start()
        self.assertTrue(wait_for(lambda: any("shared key" in s for s in statuses)))
        publisher.publish(packet())
        self.assertEqual(received, [])
        self.assertNotIn(SHARED_KEY, " ".join(statuses))
        self.assertNotIn(PUBLISH_KEY, " ".join(statuses))

    def run_bad_sample(self, build_envelopes, expected):
        received, statuses = [], []
        receiver = RelayReceiver(self.relay.url, RECEIVE_KEY, SHARED_KEY, received.append, lambda ready, s: statuses.append(s))
        self.addCleanup(receiver.stop)
        async def check():
            pub = await join(self.relay.url, "publisher", PUBLISH_KEY)
            await receive(pub)
            receiver.start()
            await receive(pub)  # paired
            challenge = await receive(pub)
            nonce = challenge["nonce"]
            await send(pub, {"type": "proof", "signature": signature(SHARED_KEY, "relay-publisher:" + nonce)})
            for envelope in build_envelopes(nonce):
                await send(pub, envelope)
            deadline = time.monotonic() + 4
            while not any(expected in s for s in statuses) and time.monotonic() < deadline:
                await asyncio.sleep(.01)
            await pub.close()
        asyncio.run(check())
        receiver.stop()
        self.assertTrue(any(expected in s for s in statuses), statuses)
        return received

    @staticmethod
    def signed(p, nonce):
        payload = encode(p)
        return {"type": "sample", "payload": payload, "signature": signature(SHARED_KEY, "relay-sample:" + nonce + ":" + payload)}

    def test_tampered_signature_is_rejected(self):
        self.assertEqual(self.run_bad_sample(lambda n: [{**self.signed(packet(), n), "signature": "0" * 64}], "sample authentication failed"), [])

    def test_other_session_signature_is_rejected(self):
        self.assertEqual(self.run_bad_sample(lambda n: [self.signed(packet(), "a" * 64)], "sample authentication failed"), [])

    def test_stale_and_future_data_rejected(self):
        for seconds in (-20, 20):
            p = packet()
            p["acquired_at"] = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
            self.assertEqual(self.run_bad_sample(lambda n: [self.signed(p, n)], "Stale or future"), [])

    def test_sequence_gap_and_duplicate_interrupt_stream(self):
        for next_seq in (1, 3):
            first = packet()
            second = packet(next_seq, first["source_id"])
            received = self.run_bad_sample(lambda n: [self.signed(first, n), self.signed(second, n)], "sequence gap")
            self.assertEqual(received, [first])

    def test_waiting_link_stops_promptly(self):
        statuses = []
        client = RelayReceiver(self.relay.url, RECEIVE_KEY, SHARED_KEY, lambda p: None, lambda ready, s: statuses.append(s))
        client.start()
        self.assertTrue(wait_for(lambda: any("waiting for analyser" in s for s in statuses)))
        begin = time.monotonic()
        client.stop()
        self.assertFalse(client.thread.is_alive())
        self.assertLess(time.monotonic() - begin, 3)

    def test_connect_in_progress_can_be_cancelled_without_network(self):
        class BlockedConnect:
            async def __aenter__(self):
                await asyncio.Future()

            async def __aexit__(self, *args):
                pass

        statuses = []
        client = RelayReceiver("wss://relay.example/mexa", RECEIVE_KEY, SHARED_KEY,
                               lambda p: None, lambda ready, s: statuses.append(s))
        client.connect = lambda *args, **kwargs: BlockedConnect()
        client.start()
        self.assertTrue(wait_for(lambda: statuses))
        client.stop()
        self.assertFalse(client.thread.is_alive())


class RelayQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.relay = RunningRelay()
        self.addCleanup(self.relay.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.controller = MexaController()
        self.addCleanup(self.controller.shutdown)

    def test_wormhole_and_lan_controls_enabled_only_for_selected_mode(self):
        window, tab = BridgeWindow(), MexaTab(self.controller)
        self.addCleanup(window.close)
        self.addCleanup(tab.close)
        window.transport.setCurrentIndex(window.transport.findData("lan"))
        self.assertFalse(window.relay_url.isEnabled())
        self.assertTrue(window.host.isEnabled())
        window.transport.setCurrentIndex(window.transport.findData("relay"))
        self.assertTrue(window.relay_url.isEnabled())
        self.assertTrue(window.relay_key.isEnabled())
        self.assertFalse(window.host.isEnabled())
        self.assertFalse(window.port.isEnabled())
        self.assertIn("no local TCP listener", window.listener_label.text())
        self.assertEqual(tab.transport.count(), 2)
        self.assertEqual(tab.transport.currentData(), "host")
        self.assertFalse(hasattr(tab, "relay_url"))
        self.assertFalse(hasattr(tab, "relay_key"))
        self.assertFalse(hasattr(tab, "tunnel_provider"))
        self.assertFalse(tab.host.isEnabled())
        tab.transport.setCurrentIndex(tab.transport.findData("lan"))
        self.assertTrue(tab.host.isEnabled())
        self.assertTrue(tab.port.isEnabled())

    def test_bridge_and_receiver_relay_logs_no_lan_listener_and_recovery(self):
        source_dir = Path(self.directory.name) / "source"
        with patch("mexa_bridge.bridge.StreamServer", side_effect=AssertionError("LAN listener in relay mode")):
            bridge = Bridge(host="ignored", port=61234, token=SHARED_KEY, serial_port="NEVER",
                            simulated=True, directory=source_dir, save_logs=True,
                            transport="relay", relay_url=self.relay.url, relay_key=PUBLISH_KEY)
        self.addCleanup(bridge.stop)
        self.controller.connect_bridge("ignored", 61234, SHARED_KEY, Path(self.directory.name) / "receiver",
                                       transport="relay", relay_url=self.relay.url, relay_key=RECEIVE_KEY)
        self.assertTrue(wait_for(lambda: self.controller.latest is not None, qt=True))
        sample = self.controller.latest
        self.assertEqual(json.loads(self.controller.log.path.read_text().splitlines()[-1])["seq"], sample.packet["seq"])
        self.assertIn("relay", self.controller.link_status)
        with self.assertRaisesRegex(ValueError, "Simulated"):
            self.controller.checked_sample()
        before = len(bridge.log.path.read_text().splitlines())
        port = self.relay.port
        self.relay.stop()
        self.assertTrue(wait_for(lambda: self.controller.latest is None, qt=True))
        self.assertTrue(wait_for(lambda: len(bridge.log.path.read_text().splitlines()) > before, qt=True))
        self.assertTrue(bridge.running)
        restarted = RunningRelay(port)
        self.addCleanup(restarted.stop)
        self.assertTrue(wait_for(lambda: self.controller.latest is not None, timeout=12, qt=True))
        self.assertGreater(self.controller.latest.packet["seq"], sample.packet["seq"])
        self.assertEqual(self.controller.latest.packet["source_id"], sample.packet["source_id"])

    def test_relay_stream_only_does_not_create_logs(self):
        with patch("mexa_bridge.bridge.AuditLog", side_effect=AssertionError("Unexpected source log")):
            bridge = Bridge(host="ignored", port=61234, token=SHARED_KEY, serial_port="NEVER",
                            simulated=True, save_logs=False, transport="relay", relay_url=self.relay.url, relay_key=PUBLISH_KEY)
        self.addCleanup(bridge.stop)
        self.controller.connect_bridge("ignored", 61234, SHARED_KEY, save_logs=False,
                                       transport="relay", relay_url=self.relay.url, relay_key=RECEIVE_KEY)
        self.assertTrue(wait_for(lambda: self.controller.latest is not None, qt=True))
        self.assertIsNone(bridge.log)
        self.assertIsNone(self.controller.log)
        self.assertEqual(self.controller.latest.log_path, "")

    def test_invalid_complete_record_retained_in_logs_but_rejected_by_optimiser(self):
        statuses = []
        publisher = RelayPublisher(self.relay.url, PUBLISH_KEY, SHARED_KEY, statuses.append)
        self.addCleanup(publisher.stop)
        self.controller.connect_bridge("ignored", 61234, SHARED_KEY, self.directory.name,
                                       transport="relay", relay_url=self.relay.url, relay_key=RECEIVE_KEY)
        self.assertTrue(wait_for(lambda: any("streaming new" in s for s in statuses), qt=True))
        p = packet()
        publisher.publish(p)
        self.assertTrue(wait_for(lambda: self.controller.latest is not None, qt=True))
        with self.assertRaises(ValueError):
            self.controller.checked_sample()
        contents = self.controller.log.path.read_text()
        saved = json.loads(contents)
        for key in (PUBLISH_KEY, RECEIVE_KEY, SHARED_KEY):
            self.assertNotIn(key, contents)
        with self.controller.log.path.with_suffix(".csv").open(newline="") as handle:
            row = next(csv.DictReader(handle))
        for field in CHANNEL_FIELDS:
            self.assertEqual(saved[field], p[field])
            self.assertEqual(float(row[field]), p[field])
        snapshot = self.controller.csv_snapshot(datetime.now())
        self.assertFalse(snapshot["mexa_valid"])
        self.assertIsNone(snapshot["mexa_no_ppm"])
        self.assertEqual(snapshot["mexa_reported_no_ppm"], -2)
        self.assertEqual(saved["raw"], p["raw"])
        self.assertEqual(row["raw_pef"], p["raw_pef"])


class RelayTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_send_is_bounded(self):
        class StalledSocket:
            async def send(self, raw):
                await asyncio.Future()

        begin = time.monotonic()
        with self.assertRaises(TimeoutError):
            await send(StalledSocket(), {"type": "sample"})
        self.assertLess(time.monotonic() - begin, 3)


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "TLS fixture generation needs cryptography")
class RelayTLSTests(unittest.TestCase):
    def test_verified_tls_roundtrip_and_untrusted_certificate_rejected(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MEXA loopback test")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                .sign(key, hashes.SHA256()))
        with tempfile.TemporaryDirectory() as directory:
            cert_path, key_path = Path(directory) / "cert.pem", Path(directory) / "key.pem"
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.minimum_version = ssl.TLSVersion.TLSv1_2
            server_context.load_cert_chain(str(cert_path), str(key_path))
            relay = RunningRelay(context=server_context)
            try:
                statuses = []
                rejected = RelayReceiver(relay.url, RECEIVE_KEY, SHARED_KEY, lambda p: self.fail("Untrusted TLS accepted"), lambda ready, s: statuses.append(s))
                rejected.start()
                try:
                    self.assertTrue(wait_for(lambda: any("certificate verification failed" in s for s in statuses)))
                finally:
                    rejected.stop()
                context = ssl.create_default_context(cafile=str(cert_path))
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                received, statuses = [], []
                with patch("mexa_bridge.relay.ssl.create_default_context", return_value=context):
                    publisher = RelayPublisher(relay.url, PUBLISH_KEY, SHARED_KEY, statuses.append)
                    receiver = RelayReceiver(relay.url, RECEIVE_KEY, SHARED_KEY, received.append, lambda *s: None)
                    receiver.start()
                    try:
                        self.assertTrue(wait_for(lambda: any("streaming new" in s for s in statuses)))
                        p = packet()
                        publisher.publish(p)
                        self.assertTrue(wait_for(lambda: received))
                        self.assertEqual(received, [p])
                    finally:
                        receiver.stop()
                        publisher.stop()
            finally:
                relay.stop()


if __name__ == "__main__":
    unittest.main()
