"""Qt-facing analyser receiver, with a thread-safe snapshot for the flow log."""

from copy import deepcopy
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal, Qt

from ..mexa.records import AuditLog, ReceivedSample, RECEIVER_LOG_REQUIRED, epoch, utc_now
from ..mexa.transport import StreamClient


class MexaController(QObject):
    changed = Signal()
    sample_received = Signal(object)
    interrupted = Signal(str)
    _incoming = Signal(int, object)
    _status = Signal(int, bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.client = None
        self.log = None
        self.latest = None
        self.lock = threading.Lock()
        self.generation = 0
        self._receiving = False
        self.status = "MEXA disconnected"
        self.settings = {"host": "127.0.0.1", "port": 61234, "token": "", "directory": "", "save_logs": True}
        self._incoming.connect(self._receive, Qt.ConnectionType.QueuedConnection)
        self._status.connect(self._set_status, Qt.ConnectionType.QueuedConnection)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def connect_bridge(self, host, port, token, directory=None, *, save_logs=True):
        if self.client is not None:
            raise ValueError("Disconnect the current MEXA link first")
        generation = self.generation + 1
        client = StreamClient(host, port, token,
                              lambda packet: self._incoming.emit(generation, packet),
                              lambda ready, text: self._status.emit(generation, ready, text))
        if save_logs and not str(directory or "").strip():
            raise ValueError("Choose a received-data log directory or turn off receiver logging")
        log = AuditLog(directory, "mexa-received") if save_logs else None
        self.generation = generation
        self._receiving = True
        self.client, self.log = client, log
        self.settings = dict(host=host, port=port, token=token, directory=str(directory or ""), save_logs=bool(save_logs))
        self.status = "Connecting to MEXA bridge…"
        self.changed.emit()
        client.start()

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
        self.status = sample.problem() or ("SIMULATED MEXA data" if packet["simulated"] else "MEXA live")
        self.sample_received.emit(sample)
        self.changed.emit()

    def _set_status(self, generation, ready, text):
        if generation != self.generation:
            return
        self.status = text
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
        return {"mexa_source_id": p["source_id"], "mexa_seq": p["seq"],
                "mexa_acquired_at": p["acquired_at"], "mexa_received_at": sample.received_at,
                "mexa_age_s": round(age, 3), "mexa_valid": usable,
                "mexa_no_ppm": p["no_ppm"] if usable else None,
                "mexa_o2_percent": p["o2_percent"] if usable else None,
                "mexa_state": p["state"], "mexa_simulated": p["simulated"],
                "mexa_basis": p["basis"], "mexa_validated": p["validated"]}

    def shutdown(self):
        self.timer.stop()
        self.disconnect_bridge()
