"""LabVIEW UDP trigger listener.

Bare ``log`` and ``stop`` messages retain the CSV logging interface. Bounded
JSON packets are dispatched to the session for correlated pressure acquisition
and acknowledgements. Software triggers associate records; they do not provide
sample-accurate hardware synchronization. Socket reads use a short timeout so
listener shutdown does not wait for a new datagram.
"""

from __future__ import annotations

import json
import socket
import threading

#: Recognised commands, lowercased and stripped before comparison.
COMMANDS = ('log', 'stop')
MAX_PACKET_BYTES = 16_384

#: Read timeout.  Bounds how long stop() waits for the thread to notice.
POLL_TIMEOUT_S = 0.5


class UdpCommandListener:
    """Receives legacy commands and bounded JSON datagrams on a daemon thread.

    Callbacks fire on the listener thread; the session is responsible for
    marshalling them onto the GUI thread.
    """

    def __init__(self, host='127.0.0.1', port=5056, *,
                 on_command=None, on_ready=None, on_error=None,
                 on_ignored=None, on_packet=None):
        self.host = host
        self.port = port
        self._on_command = on_command or (lambda _command: None)
        self._on_ready = on_ready or (lambda _host, _port: None)
        self._on_error = on_error or (lambda _error, _host, _port: None)
        self._on_ignored = on_ignored or (lambda _text, _sender: None)
        self._on_packet = on_packet
        self._stop_event = None
        self._socket = None
        self._thread = None

    @property
    def listening(self):
        return self._socket is not None

    def start(self, host=None, port=None):
        """Bind and begin receiving.  Any previous listener is stopped first."""
        if self._thread is not None and self._thread.is_alive():
            self.stop()
            self._thread.join(timeout=POLL_TIMEOUT_S * 2)
        if host is not None:
            self.host = host
        if port is not None:
            self.port = port
        stop_event = threading.Event()
        self._stop_event = stop_event
        self._thread = threading.Thread(
            target=self._loop, args=(stop_event, self.host, self.port),
            daemon=True, name="labview-udp-listener")
        self._thread.start()

    def stop(self):
        if self._stop_event is not None:
            self._stop_event.set()
        udp_socket = self._socket
        if udp_socket is not None:
            # Closing the socket as well as setting the event turns a blocking
            # recvfrom into an immediate error rather than a timeout wait.
            try:
                udp_socket.close()
            except Exception:
                pass

    def reply(self, sender, payload):
        """Reply on the listening socket so LabVIEW can use its original UDP socket."""
        sock = self._socket
        if sock is None:
            return False
        try:
            encoded = json.dumps(payload, allow_nan=False, separators=(',', ':')).encode('utf-8')
            if len(encoded) > MAX_PACKET_BYTES:
                return False
            sock.sendto(encoded, sender)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def _loop(self, stop_event, host, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(POLL_TIMEOUT_S)
        try:
            sock.bind((host, port))
        except OSError as exc:
            try:
                sock.close()
            except Exception:
                pass
            self._on_error(str(exc), host, port)
            return

        self._socket = sock
        self._on_ready(host, port)
        try:
            while not stop_event.is_set():
                try:
                    payload, sender = sock.recvfrom(MAX_PACKET_BYTES + 1)
                except socket.timeout:
                    continue
                except OSError as exc:
                    if not stop_event.is_set():
                        self._on_error(str(exc), host, port)
                    break

                if len(payload) > MAX_PACKET_BYTES:
                    self.reply(sender, {'protocol': 'flow-pressure-v1', 'type': 'ack',
                                        'ok': False, 'error': 'Packet exceeds 16 KiB.'})
                    continue
                text = payload.decode('utf-8', errors='replace').strip()
                command = text.lower()
                if command in COMMANDS:
                    self._on_command(command)
                elif text.startswith('{') and self._on_packet is not None:
                    try:
                        packet = json.loads(text)
                    except (ValueError, RecursionError):
                        self.reply(sender, {'protocol': 'flow-pressure-v1', 'type': 'ack',
                                            'ok': False, 'error': 'Invalid JSON.'})
                        continue
                    self._on_packet(packet, sender)
                else:
                    self._on_ignored(text[:512], sender)
        finally:
            try:
                sock.close()
            except Exception:
                pass
            if self._socket is sock:
                self._socket = None
