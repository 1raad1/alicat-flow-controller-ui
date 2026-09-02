"""Independent acquisition worker. No dependency on flow-control services."""

import threading
import time
import uuid

from .protocol import MODE_COMMANDS, SerialReader, simulated_cycle
from .records import AuditLog, make_packet
from .transport import StreamServer
from .relay import RelayPublisher


class Bridge:
    def __init__(self, *, host, port, token, serial_port, directory=None, save_logs=True, simulated=False,
                 validated=False, dry=False, on_sample=lambda packet: None,
                 on_status=lambda text: None, on_control=lambda text: None,
                 transport="lan", relay_url="", relay_key=""):
        if transport not in ("lan", "relay"):
            raise ValueError("Choose Wormhole or Direct LAN")
        self.source_id = str(uuid.uuid4())
        self.serial_port, self.simulated = serial_port, simulated
        self.validated, self.dry = validated, dry
        self.on_sample, self.on_status = on_sample, on_status
        self.on_control = on_control
        self.stop_event = threading.Event()
        self.mode_lock = threading.Lock()
        self._pending_mode = None
        self._mode_busy = False
        self._last_state = None
        self._last_read_at = 0.
        self._simulated_state = "measuring"
        if save_logs and not str(directory or "").strip():
            raise ValueError("Choose a local log directory or turn off local logging")
        self.log = AuditLog(directory, "mexa-source") if save_logs else None
        try:
            self.server = (RelayPublisher(relay_url, relay_key, token, on_status) if transport == "relay"
                           else StreamServer(host, port, token, on_status))
        except Exception:
            if self.log is not None:
                self.log.close()
            raise
        self.thread = threading.Thread(target=self._run, name="mexa-acquisition", daemon=True)
        self.thread.start()

    @property
    def running(self):
        return self.thread.is_alive()

    def _mode_allowed(self, mode):
        expected = {"meas": "standby", "standby": "measuring"}.get(mode)
        return (expected is not None and self._last_state == expected
                and 0 <= time.monotonic() - self._last_read_at <= 5)

    def can_request_mode(self, mode):
        with self.mode_lock:
            return (self.running and not self.stop_event.is_set()
                    and not self._mode_busy and self._mode_allowed(mode))

    def request_mode(self, mode):
        """Queue one operator request; only the acquisition thread owns serial I/O."""
        if mode not in MODE_COMMANDS:
            raise ValueError("Only MEAS and STANDBY are supported")
        with self.mode_lock:
            if not self.running or self.stop_event.is_set() or self._mode_busy or not self._mode_allowed(mode):
                raise ValueError("Wait for fresh standby/measuring status and any pending command to finish")
            self._pending_mode = mode
            self._mode_busy = True

    def _publish(self, cycle, seq, begin):
        packet = make_packet(cycle, self.source_id, seq, simulated=self.simulated,
                             validated=self.validated, dry=self.dry,
                             cycle_s=min(10., time.monotonic() - begin))
        if self.log is not None:
            self.log.write(packet)
        self.server.publish(packet)
        self.on_sample(packet)

    @staticmethod
    def _control_cycle(mode, phase, reply="", detail=""):
        return {"no_ppm": None, "o2_percent": None, "co_percent": None,
                "co2_percent": None, "hc_ppm": None, "state": "error", "valid": False,
                "alarms": ["local_mode_change"], "warnings": [], "raw": {},
                "control": {"mode": mode, "phase": phase, "reply": reply, "detail": detail[:300]}}

    def _run(self):
        reader = None
        seq = 0
        try:
            while not self.stop_event.is_set():
                begin = time.monotonic()
                seq += 1
                with self.mode_lock:
                    mode, self._pending_mode = self._pending_mode, None
                    allowed = self._mode_allowed(mode) if mode else False
                if mode:
                    # Invalidate capture before the command. A lost marker still
                    # leaves all subsequent samples unvalidated until restart.
                    self.validated = False
                    self._publish(self._control_cycle(mode, "requested"), seq, begin)
                    reply = ""
                    try:
                        if not allowed or self.stop_event.is_set() or (reader is None and not self.simulated):
                            phase, detail = "cancelled", "Status changed or reader stopping; no command sent"
                        elif self.simulated:
                            self._simulated_state = "measuring" if mode == "meas" else "standby"
                            phase, detail = "acknowledged", "SIMULATION only; no serial command sent"
                        else:
                            reader.set_mode(mode)
                            reply = reader.last_control_reply
                            phase, detail = "acknowledged", "ACK received; check subsequent reported state"
                    except Exception as exc:
                        reply = reader.last_control_reply if reader else ""
                        phase, detail = "failed", f"Outcome uncertain: {exc}. Not retried; inspect instrument"
                        if reader is not None:
                            reader.close()
                            reader = None
                    with self.mode_lock:
                        self._last_state = None
                        self._last_read_at = 0.
                        self._mode_busy = False
                    seq += 1
                    self._publish(self._control_cycle(mode, phase, reply, detail), seq, begin)
                    self.on_control(f"{mode.upper()}: {detail}. Recheck and restart reader before optimisation.")
                    continue
                try:
                    if self.simulated:
                        cycle = simulated_cycle(seq, measuring=self._simulated_state == "measuring")
                    else:
                        if reader is None:
                            reader = SerialReader(self.serial_port)
                        cycle = reader.read()
                except Exception as exc:
                    cycle = {"no_ppm": None, "o2_percent": None, "co_percent": None,
                             "co2_percent": None, "hc_ppm": None, "state": "error",
                             "valid": False, "alarms": [str(exc)[:300]], "warnings": [],
                             "raw": dict(reader.last_raw) if reader else {}}
                    if reader is not None:
                        reader.close()
                        reader = None
                with self.mode_lock:
                    self._last_state = cycle["state"]
                    self._last_read_at = time.monotonic()
                if self.stop_event.is_set():
                    break
                self._publish(cycle, seq, begin)
                # Maximum nominal rate is 1 Hz. Slower failed cycles are never
                # manufactured into repeated "fresh" measurements.
                self.stop_event.wait(max(.1, 1 - (time.monotonic() - begin)))
        except Exception as exc:
            self.on_status(f"Acquisition STOPPED: {exc}. Restart after resolving the fault.")
        finally:
            try:
                if reader is not None:
                    reader.close()
            finally:
                try:
                    if self.log is not None:
                        self.log.close()
                finally:
                    self.server.stop()

    def stop(self):
        self.stop_event.set()
        self.thread.join(4)
        return not self.thread.is_alive()
