"""Independent acquisition worker. No dependency on flow-control services."""

import threading
import time
import uuid

from .protocol import SerialReader, simulated_cycle
from .records import AuditLog, make_packet
from .transport import StreamServer


class Bridge:
    def __init__(self, *, host, port, token, serial_port, directory, simulated=False,
                 validated=False, dry=False, on_sample=lambda packet: None,
                 on_status=lambda text: None):
        self.source_id = str(uuid.uuid4())
        self.serial_port, self.simulated = serial_port, simulated
        self.validated, self.dry = validated, dry
        self.on_sample, self.on_status = on_sample, on_status
        self.stop_event = threading.Event()
        self.log = AuditLog(directory, "mexa-source")
        try:
            self.server = StreamServer(host, port, token, on_status)
        except Exception:
            self.log.close()
            raise
        self.thread = threading.Thread(target=self._run, name="mexa-acquisition", daemon=True)
        self.thread.start()

    @property
    def running(self):
        return self.thread.is_alive()

    def _run(self):
        reader = None
        seq = 0
        try:
            while not self.stop_event.is_set():
                begin = time.monotonic()
                seq += 1
                try:
                    if self.simulated:
                        cycle = simulated_cycle(seq)
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
                if self.stop_event.is_set():
                    break
                packet = make_packet(cycle, self.source_id, seq, simulated=self.simulated,
                                     validated=self.validated, dry=self.dry,
                                     cycle_s=min(10., time.monotonic() - begin))
                self.log.write(packet)
                self.server.publish(packet)
                self.on_sample(packet)
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
                    self.log.close()
                finally:
                    self.server.stop()

    def stop(self):
        self.stop_event.set()
        self.thread.join(4)
        return not self.thread.is_alive()
