"""Authenticated, bounded TCP stream. No remote-control messages are accepted.

Challenge/response keeps the shared key off the wire. Each sample is signed for
that connection. Traffic is NOT encrypted: use a trusted lab LAN, not the internet.
Slow clients lose their connection; acquisition and the local audit log continue.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import threading

from .records import MAX_LINE, validate_packet


DEFAULT_PORT = 61234


def validate_endpoint(host, port, token):
    if ipaddress.ip_address(host).version != 4:
        raise ValueError("Use a numeric IPv4 address")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    if not isinstance(token, str) or not 32 <= len(token) <= 128 or not token.isascii():
        raise ValueError("Use a shared key of 32–128 ASCII characters")


def signature(token, text):
    return hmac.new(token.encode("ascii"), text.encode("utf-8"), hashlib.sha256).hexdigest()


def send_line(sock, value):
    line = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(line) > MAX_LINE:
        raise ValueError("Network record exceeds size limit")
    sock.sendall(line)


class Lines:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = bytearray()

    def read(self):
        while b"\n" not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Stream closed")
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_LINE:
                raise ValueError("Oversized stream record")
        line, _, rest = self.buffer.partition(b"\n")
        self.buffer = bytearray(rest)
        return json.loads(line)


def close_socket(sock):
    if sock:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


class StreamServer:
    def __init__(self, host, port, token, status=lambda text: None):
        validate_endpoint(host, port, token)
        self.token, self.status = token, status
        self.stop_event = threading.Event()
        self.condition = threading.Condition()
        self.latest = None
        self.revision = 0
        self.client = None
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Do not use SO_REUSEADDR on Windows; another listener must not
            # silently share this acquisition port.
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self.listener.bind((host, port))
            self.listener.listen(2)
            self.listener.settimeout(.3)
        except Exception:
            self.listener.close()
            raise
        self.thread = threading.Thread(target=self._run, name="mexa-stream-server", daemon=True)
        self.thread.start()

    def publish(self, packet):
        with self.condition:
            self.latest = deepcopy(validate_packet(packet))
            self.revision += 1
            self.condition.notify_all()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                client, _address = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.client = client
            try:
                client.settimeout(2)
                nonce = secrets.token_hex(32)
                send_line(client, {"schema": 1, "challenge": nonce})
                hello = Lines(client).read()
                proof = hello.get("proof", "")
                if not isinstance(proof, str) or not hmac.compare_digest(proof, signature(self.token, "auth:" + nonce)):
                    raise ValueError("Authentication failed")
                send_line(client, {"authenticated": True, "signature": signature(self.token, "server:" + nonce)})
                self.status("Flow-controller client connected")
                revision = -1
                while not self.stop_event.is_set():
                    with self.condition:
                        self.condition.wait_for(lambda: self.stop_event.is_set() or self.revision != revision, .5)
                        if self.stop_event.is_set():
                            break
                        if self.latest is None or self.revision == revision:
                            revision = self.revision
                            continue
                        packet, revision = self.latest, self.revision
                    payload = json.dumps(packet, separators=(",", ":"), allow_nan=False)
                    send_line(client, {"payload": payload, "signature": signature(self.token, nonce + ":" + payload)})
            except (OSError, ValueError, TypeError, AttributeError):
                if not self.stop_event.is_set():
                    self.status("Client disconnected or rejected; local acquisition continues")
            finally:
                close_socket(client)
                self.client = None

    def stop(self):
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        close_socket(self.listener)
        close_socket(self.client)
        self.thread.join(2.5)


class StreamClient:
    def __init__(self, host, port, token, on_packet, on_status):
        validate_endpoint(host, port, token)
        self.endpoint, self.token = (host, port), token
        self.on_packet, self.on_status = on_packet, on_status
        self.stop_event = threading.Event()
        self.sock = None
        self.thread = threading.Thread(target=self._run, name="mexa-stream-client", daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            stage = "connect"
            try:
                self.on_status(False, "Connecting to MEXA bridge…")
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock = sock
                if self.stop_event.is_set():
                    break
                sock.settimeout(2)
                sock.connect(self.endpoint)
                if self.stop_event.is_set():
                    break
                stage = "authenticate"
                reader = Lines(sock)
                hello = reader.read()
                nonce = hello["challenge"]
                if hello.get("schema") != 1 or not isinstance(nonce, str) or len(nonce) != 64:
                    raise ValueError("Invalid bridge handshake")
                send_line(sock, {"proof": signature(self.token, "auth:" + nonce)})
                confirmation = reader.read()
                proof = confirmation.get("signature", "")
                if confirmation.get("authenticated") is not True or not isinstance(proof, str) or not hmac.compare_digest(
                        proof, signature(self.token, "server:" + nonce)):
                    raise ValueError("Bridge authentication failed")
                self.on_status(True, "Bridge authenticated; waiting for analyser samples")
                stage = "stream"
                sock.settimeout(5)
                previous = None
                while not self.stop_event.is_set():
                    envelope = reader.read()
                    payload, proof = envelope["payload"], envelope["signature"]
                    if not isinstance(payload, str) or not isinstance(proof, str) or not hmac.compare_digest(
                            proof, signature(self.token, nonce + ":" + payload)):
                        raise ValueError("MEXA sample authentication failed")
                    packet = validate_packet(json.loads(payload))
                    identity = (packet["source_id"], packet["seq"])
                    if previous and (identity[0] != previous[0] or identity[1] != previous[1] + 1):
                        raise ValueError("MEXA sample sequence gap or restart; reconnecting")
                    previous = identity
                    self.on_packet(packet)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                if not self.stop_event.is_set():
                    hint = {"connect": "Check the analyser PC's adapter IP, listener, TCP port and firewall/eduroam routing.",
                            "authenticate": "Bridge reached, but authentication failed. Check the shared key and port.",
                            "stream": "Stream lost; the active live window must be restarted."}[stage]
                    self.on_status(False, f"MEXA link interrupted: {exc}. {hint}")
            finally:
                close_socket(self.sock)
                self.sock = None
            self.stop_event.wait(1)

    def stop(self):
        self.stop_event.set()
        close_socket(self.sock)
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(2.5)
