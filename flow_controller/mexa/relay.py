"""Outbound-only relay clients. TLS transport plus per-session sample signatures.

The relay knows role access keys, not the analyser's shared signing key. A
receiver-generated challenge binds samples to a new session after every outage.
Only a single latest sample is held; acquisition never waits for the network.
"""

import asyncio
from copy import deepcopy
import hmac
import ipaddress
import json
import logging
import re
import secrets
import ssl
import threading
import time
from urllib.parse import urlsplit

from .records import MAX_AGE, MAX_LINE, epoch, validate_packet
from .transport import signature


class RelayError(ValueError):
    """A fixed, credential-free diagnostic safe for the local UI."""


def validate_key(key):
    if not isinstance(key, str) or not 32 <= len(key) <= 128 or any(not 33 <= ord(c) <= 126 for c in key):
        raise ValueError("Use a randomly generated key of 32–128 printable ASCII characters, without spaces")


def validate_relay_url(url):
    if not isinstance(url, str) or len(url) > 256 or not url.isascii() or any(c.isspace() for c in url):
        raise ValueError("Enter the relay's wss://hostname/mexa URL")
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise ValueError("Invalid relay URL") from None
    if (not host or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path != "/mexa" or port == 0):
        raise ValueError("Use wss://hostname/mexa without credentials, query parameters or fragments")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (address.is_unspecified or address.is_multicast):
        raise ValueError("Use the relay server's reachable hostname")
    if parsed.scheme != "wss" and not (parsed.scheme == "ws" and address and address.is_loopback):
        raise ValueError("Internet relay requires wss:// encryption; ws:// is allowed only on numeric loopback for same-PC connections")
    return url


def quiet_logger(name):
    # WebSocket DEBUG logs can contain authentication frames. Never propagate
    # library wire logs into application logs, including when root DEBUG is on.
    logger = logging.Logger(name)
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


def encode(value):
    raw = json.dumps(value, separators=(",", ":"), allow_nan=False)
    if len(raw.encode("utf-8")) > MAX_LINE:
        raise RelayError("Relay message exceeds size limit")
    return raw


def decode(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_LINE:
        raise RelayError("Invalid relay message")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise RelayError("Invalid relay JSON") from None
    if not isinstance(value, dict):
        raise RelayError("Invalid relay message")
    return value


def is_digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


async def send(ws, value):
    await asyncio.wait_for(ws.send(encode(value)), 2)


async def receive(ws, timeout=5):
    return decode(await asyncio.wait_for(ws.recv(), timeout))


class _RelayClient:
    def __init__(self, url, access_key, token, role, on_status):
        validate_relay_url(url)
        validate_key(access_key)
        validate_key(token)
        if access_key == token:
            raise ValueError("Relay access key and analyser shared key must be different")
        try:
            from websockets.asyncio.client import connect
        except ImportError:
            raise ValueError("Relay support needs websockets. Rerun the app installer to install updated requirements.") from None
        self.connect = connect
        self.url, self.access_key, self.token = url, access_key, token
        self.role, self.on_status = role, on_status
        self.stop_event = threading.Event()
        self.lifecycle_lock = threading.Lock()
        self.loop = self.task = None
        self.thread = threading.Thread(target=self._thread_main, name="mexa-relay-" + role, daemon=True)

    def start(self):
        self.thread.start()

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            pass
        finally:
            with self.lifecycle_lock:
                self.loop = self.task = None

    async def _run(self):
        from websockets.exceptions import ConnectionClosed
        with self.lifecycle_lock:
            self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        delay = 1
        while not self.stop_event.is_set():
            stage = "connect"
            started = time.monotonic()
            try:
                self.on_status(False, "Connecting outward to MEXA relay…")
                # Normal certificate and hostname verification is mandatory.
                # Use the configured system proxy, except for local tests.
                options = dict(open_timeout=5, close_timeout=1, ping_interval=2,
                               ping_timeout=3, max_size=MAX_LINE, max_queue=2,
                               compression=None, write_limit=MAX_LINE,
                               logger=quiet_logger("mexa.relay.client"))
                if self.url.startswith("ws://"):
                    options["proxy"] = None
                else:
                    context = ssl.create_default_context()
                    context.minimum_version = ssl.TLSVersion.TLSv1_2
                    options["ssl"] = context
                async with self.connect(self.url, **options) as ws:
                    stage = "access"
                    await send(ws, {"type": "join", "version": 1, "role": self.role, "key": self.access_key})
                    if await receive(ws) != {"type": "accepted"}:
                        raise RelayError("Relay access was not accepted")
                    stage = "peer"
                    peer = "receiver" if self.role == "publisher" else "analyser"
                    self.on_status(False, f"Relay connected; waiting for {peer} PC")
                    # WebSocket keepalive detects a broken route while no peer
                    # is present; there is deliberately no sample-age timeout yet.
                    if decode(await ws.recv()) != {"type": "paired"}:
                        raise RelayError("Unexpected relay pairing response")
                    stage = "stream"
                    await self.session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self.stop_event.is_set():
                    if isinstance(exc, RelayError):
                        detail = str(exc)
                    elif isinstance(exc, ssl.SSLCertVerificationError):
                        detail = "TLS certificate verification failed; check relay certificate and PC clock. Verification was not bypassed."
                    elif isinstance(exc, ConnectionClosed):
                        code = exc.rcvd.code if exc.rcvd else None
                        detail = {4001: "Relay access denied; check this PC's role access key",
                                  4009: "This relay role is already connected; stop the other copy first",
                                  1012: "Other PC disconnected; waiting to establish a new session",
                                  1008: "Relay rejected the session protocol"}.get(code, "Relay connection closed")
                    elif stage == "connect":
                        detail = "Cannot reach relay; check URL, hosting, TLS and outbound WebSocket access on port 443 with IT"
                    elif stage == "access":
                        detail = "Relay access timed out or failed; check endpoint and role access key"
                    else:
                        detail = "Relay session interrupted or no fresh samples arrived within 5 seconds"
                    # Never include arbitrary exception text, HTTP responses,
                    # close reasons, URLs or keys in this diagnostic.
                    self.on_status(False, detail + ". Live capture must be restarted after reconnection.")
            if time.monotonic() - started > 10:
                delay = 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)

    def stop(self):
        self.stop_event.set()
        with self.lifecycle_lock:
            if self.loop and self.task:
                try:
                    self.loop.call_soon_threadsafe(self.task.cancel)
                except RuntimeError:
                    pass
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(3)


class RelayPublisher(_RelayClient):
    def __init__(self, url, access_key, token, status=lambda text: None):
        super().__init__(url, access_key, token, "publisher", lambda ready, text: status(text))
        self.sample_lock = threading.Lock()
        self.latest = None
        self.revision = 0
        self.start()

    def publish(self, packet):
        packet = deepcopy(validate_packet(packet))
        with self.sample_lock:
            self.latest = packet
            self.revision += 1

    async def session(self, ws):
        challenge = await receive(ws)
        nonce, proof = challenge.get("nonce"), challenge.get("proof")
        if (set(challenge) != {"type", "nonce", "proof"} or challenge["type"] != "challenge"
                or not is_digest(nonce) or not is_digest(proof)
                or not hmac.compare_digest(proof, signature(self.token, "relay-receiver:" + nonce))):
            raise RelayError("Receiver authentication failed; check the analyser shared key on both PCs")
        await send(ws, {"type": "proof", "signature": signature(self.token, "relay-publisher:" + nonce)})
        with self.sample_lock:
            revision = self.revision  # never send a held sample from before pairing
        self.on_status(True, "Receiver authenticated through relay; streaming new measurements")

        async def transmit():
            nonlocal revision
            while True:
                with self.sample_lock:
                    packet, current = self.latest, self.revision
                if packet is not None and current != revision:
                    revision = current
                    payload = encode(packet)
                    await send(ws, {"type": "sample", "payload": payload,
                                    "signature": signature(self.token, "relay-sample:" + nonce + ":" + payload)})
                await asyncio.sleep(.05)

        async def reject_incoming():
            await ws.recv()
            raise RelayError("Unexpected inbound message; relay cannot operate the analyser")

        tasks = [asyncio.create_task(transmit()), asyncio.create_task(reject_incoming())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class RelayReceiver(_RelayClient):
    def __init__(self, url, access_key, token, on_packet, on_status):
        super().__init__(url, access_key, token, "receiver", on_status)
        self.on_packet = on_packet
        self.last_identity = None

    async def session(self, ws):
        nonce = secrets.token_hex(32)
        await send(ws, {"type": "challenge", "nonce": nonce,
                        "proof": signature(self.token, "relay-receiver:" + nonce)})
        response = await receive(ws)
        if (set(response) != {"type", "signature"} or response["type"] != "proof"
                or not is_digest(response["signature"])
                or not hmac.compare_digest(response["signature"], signature(self.token, "relay-publisher:" + nonce))):
            raise RelayError("Analyser authentication failed; check the analyser shared key on both PCs")
        self.on_status(True, "Analyser authenticated through relay; waiting for fresh samples")
        previous = None
        while True:
            envelope = await receive(ws)
            payload, proof = envelope.get("payload"), envelope.get("signature")
            if (set(envelope) != {"type", "payload", "signature"} or envelope["type"] != "sample"
                    or not isinstance(payload, str) or not is_digest(proof)
                    or not hmac.compare_digest(proof, signature(self.token, "relay-sample:" + nonce + ":" + payload))):
                raise RelayError("MEXA relay sample authentication failed")
            try:
                packet = validate_packet(decode(payload))
            except (ValueError, TypeError, KeyError, AttributeError):
                raise RelayError("Invalid MEXA relay sample") from None
            identity = (packet["source_id"], packet["seq"])
            if previous and (identity[0] != previous[0] or identity[1] != previous[1] + 1):
                raise RelayError("MEXA sequence gap or restart; a new capture window is required")
            if self.last_identity and identity[0] == self.last_identity[0] and identity[1] <= self.last_identity[1]:
                raise RelayError("Repeated or reordered MEXA sample rejected")
            if not -1 <= time.time() - epoch(packet["acquired_at"]) <= MAX_AGE:
                raise RelayError("Stale or future MEXA sample rejected; synchronise both PC clocks")
            previous = self.last_identity = identity
            self.on_packet(packet)
