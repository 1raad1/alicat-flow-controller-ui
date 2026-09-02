"""Qt-facing analyser receiver, with a thread-safe snapshot for the flow log."""

from copy import deepcopy
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal, Qt

from mexa_bridge.records import (AuditLog, CHANNEL_FIELDS, ReceivedSample, RECEIVER_LOG_REQUIRED,
                            csv_text, epoch, utc_now)
from mexa_bridge.transport import StreamClient
from mexa_bridge.relay import RelayReceiver
from mexa_bridge.relay import validate_key
from mexa_bridge.quick_tunnel import HostStatus, QuickTunnelHost


class MexaController(QObject):
    changed = Signal()
    sample_received = Signal(object)
    interrupted = Signal(str)
    _incoming = Signal(int, object)
    _status = Signal(int, bool, str)
    _host_update = Signal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.client = None
        self.log = None
        self.latest = None
        self.lock = threading.Lock()
        self.generation = 0
        self._receiving = False
        self.temporary_host = None
        self.host_status = HostStatus()
        self.host_provider = "wormhole"
        self._host_generation = 0
        self._host_pending = None
        self._temporary_local_url = ""
        self.status = "MEXA disconnected"
        self.link_status = "Network: disconnected"
        self.settings = {"host": "127.0.0.1", "port": 61234, "token": "", "directory": "", "save_logs": True,
                         "transport": "lan", "relay_url": "", "relay_key": ""}
        self._incoming.connect(self._receive, Qt.ConnectionType.QueuedConnection)
        self._status.connect(self._set_status, Qt.ConnectionType.QueuedConnection)
        self._host_update.connect(self._host_changed, Qt.ConnectionType.QueuedConnection)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def start_temporary_host(self, token, directory=None, *, save_logs=True, executable="", provider="wormhole"):
        if self.client is not None or self.temporary_host is not None:
            raise ValueError("Disconnect MEXA and stop the current temporary relay first")
        validate_key(token)
        if save_logs and not str(directory or "").strip():
            raise ValueError("Choose a received-data log directory or turn off receiver logging")
        if provider not in ("wormhole", "cloudflare"):
            raise ValueError("Choose Wormhole or Cloudflare as the tunnel provider")
        self.host_provider = provider
        self._host_generation += 1
        generation = self._host_generation
        self._host_pending = (token, directory, save_logs)
        self.host_status = HostStatus("starting", "Preparing temporary relay…")
        self.temporary_host = QuickTunnelHost(lambda status: self._host_update.emit(generation, status), executable,
                                              provider=provider)
        self.changed.emit()
        self.temporary_host.start()

    def connect_temporary_host(self, token, directory=None, *, save_logs=True):
        if self.temporary_host is None or self.host_status.state != "ready":
            raise ValueError("Start the temporary relay and wait for its tunnel to register")
        self._temporary_local_url = self.host_status.local_url
        self.connect_bridge("127.0.0.1", 61234, token, directory, save_logs=save_logs,
                            transport="relay", relay_url=self.host_status.local_url,
                            relay_key=self.host_status.receiver_key)

    def _host_changed(self, generation, status):
        if generation != self._host_generation:
            return
        # Stop cancels auto-connect even if a ready event was already queued.
        if self.host_status.state == "stopping" and status.state not in ("stopped", "failed"):
            return
        self.host_status = status
        if status.state == "ready" and self._host_pending is not None:
            token, directory, save_logs = self._host_pending
            self._host_pending = None
            try:
                self.connect_temporary_host(token, directory, save_logs=save_logs)
            except (ValueError, OSError) as exc:
                self.status = f"Tunnel started, but receiver could not connect: {exc}"
        elif status.state == "reconnecting":
            self._set_status(self.generation, False, status.message)
        elif status.state in ("failed", "stopped"):
            self._host_pending = None
            self.temporary_host = None
            if self.client is not None and self.settings.get("relay_url") == self._temporary_local_url:
                self.disconnect_bridge()
            if self._temporary_local_url and self.settings.get("relay_url") == self._temporary_local_url:
                self.settings.update(relay_url="", relay_key="")
            self._temporary_local_url = ""
        self.changed.emit()

    def stop_temporary_host(self):
        self._host_pending = None
        if self.client is not None and self.settings.get("relay_url") == self._temporary_local_url:
            self.disconnect_bridge()
        if self.temporary_host is not None:
            self.host_status = HostStatus("stopping", "Stopping the tunnel and local relay…")
            self.temporary_host.stop()
            self.changed.emit()

    def connect_bridge(self, host, port, token, directory=None, *, save_logs=True,
                       transport="lan", relay_url="", relay_key=""):
        if self.client is not None:
            raise ValueError("Disconnect the current MEXA link first")
        generation = self.generation + 1
        if transport not in ("lan", "relay"):
            raise ValueError("Choose Direct LAN or Internet relay")
        callbacks = (lambda packet: self._incoming.emit(generation, packet),
                     lambda ready, text: self._status.emit(generation, ready, text))
        client = (RelayReceiver(relay_url, relay_key, token, *callbacks) if transport == "relay"
                  else StreamClient(host, port, token, *callbacks))
        if save_logs and not str(directory or "").strip():
            raise ValueError("Choose a received-data log directory or turn off receiver logging")
        log = AuditLog(directory, "mexa-received") if save_logs else None
        self.generation = generation
        self._receiving = True
        self.client, self.log = client, log
        self.settings = dict(host=host, port=port, token=token, directory=str(directory or ""), save_logs=bool(save_logs),
                             transport=transport, relay_url=relay_url, relay_key=relay_key)
        self.status = "Connecting to MEXA bridge…"
        self.link_status = "Network: connecting to " + self._endpoint_text()
        self.changed.emit()
        client.start()

    def _endpoint_text(self):
        if self.settings.get("transport") == "relay":
            return "relay " + self.settings["relay_url"]
        return f"{self.settings['host']}:{self.settings['port']}"

    def disconnect_bridge(self):
        self.generation += 1  # ignore queued callbacks from a previous connection
        self._receiving = False
        client, self.client = self.client, None
        if client:
            client.stop()
        close_error = None
        if self.log:
            try:
                self.log.close()
            except OSError as exc:
                close_error = exc
            finally:
                self.log = None
        with self.lock:
            self.latest = None
        self.status = "MEXA disconnected" + (f"; log close failed: {close_error}" if close_error else "")
        self.link_status = "Network: disconnected"
        self.interrupted.emit(self.status)
        self.changed.emit()

    def _receive(self, generation, packet):
        if generation != self.generation or not self._receiving:
            return
        stamp = utc_now()
        try:
            if self.log is not None:
                self.log.write(packet, stamp)
        except OSError as exc:
            self.disconnect_bridge()
            self.status = f"MEXA reception stopped: local log failed ({exc})"
            self.changed.emit()
            return
        sample = ReceivedSample(deepcopy(packet), stamp, time.monotonic(), str(self.log.path) if self.log else "")
        with self.lock:
            self.latest = sample
        self.link_status = f"Network: connected to {self._endpoint_text()}; receiving records"
        self.status = sample.problem() or ("SIMULATED MEXA data" if packet["simulated"] else "MEXA live")
        self.sample_received.emit(sample)
        self.changed.emit()

    def _set_status(self, generation, ready, text):
        if generation != self.generation:
            return
        self.status = text
        self.link_status = f"Network: {text}"
        if not ready:
            with self.lock:
                self.latest = None
            self.interrupted.emit(text)
        self.changed.emit()

    def _tick(self):
        if self.latest:
            problem = self.latest.problem()
            if problem:
                self.status = problem
            self.changed.emit()

    def checked_sample(self):
        sample = self.latest
        problem = sample.problem(experimental=True) if sample else "Fresh MEXA data is required"
        if problem:
            raise ValueError(problem)
        if not sample.log_path:
            raise ValueError(RECEIVER_LOG_REQUIRED)
        return sample

    def csv_snapshot(self, flow_timestamp):
        """Latest causal reading, explicitly marked held/new in CsvLogger."""
        with self.lock:
            sample = self.latest
        if sample is None:
            return {}
        p = sample.packet
        # The serial flow poll can run ahead of the GUI's timestamp or vice
        # versa. Never join a measurement from the future onto an earlier row.
        age = flow_timestamp.timestamp() - epoch(p["acquired_at"])
        problem = sample.problem()
        usable = not problem and 0 <= age <= 5
        fresh = not sample.freshness_problem() and 0 <= age <= 5
        result = {"mexa_source_id": p["source_id"], "mexa_seq": p["seq"],
                "mexa_acquired_at": p["acquired_at"], "mexa_received_at": sample.received_at,
                "mexa_age_s": round(age, 3), "mexa_valid": usable,
                "mexa_no_ppm": p["no_ppm"] if usable else None,
                "mexa_o2_percent": p["o2_percent"] if usable else None,
                "mexa_quality": problem or ("Future reading relative to flow row" if age < 0 else ""),
                "mexa_state": p["state"], "mexa_simulated": p["simulated"],
                "mexa_basis": p["basis"], "mexa_validated": p["validated"]}
        result.update({f"mexa_reported_{key}": p.get(key) if fresh else None for key in CHANNEL_FIELDS})
        result.update({f"mexa_raw_{name}": p["raw"].get(name, "") if fresh else ""
                       for name in ("status", "subsystem", "channels")})
        result.update(mexa_options=p.get("options") if fresh else None,
                      mexa_cycle_s=p["cycle_s"],
                      mexa_alarms=csv_text("; ".join(p["alarms"])),
                      mexa_warnings=csv_text("; ".join(p["warnings"])),
                      mexa_pef_error=csv_text(p.get("pef_error", "")),
                      mexa_raw_pef=p.get("raw_pef", "") if fresh else "")
        return result

    def shutdown(self):
        self.timer.stop()
        self.disconnect_bridge()
        self._host_pending = None
        self._host_generation += 1
        if self.temporary_host is not None:
            self.temporary_host.stop(wait=True)
            self.temporary_host = None
        self.host_status = HostStatus()
        if self._temporary_local_url and self.settings.get("relay_url") == self._temporary_local_url:
            self.settings.update(relay_url="", relay_key="")
        self._temporary_local_url = ""
