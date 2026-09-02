"""MEXA-584L protocol derived from the supplied HORIBA v1.00 executable.

Channel/status/PEF reads and explicitly requested MEAS/STANDBY are supported.
No calibration or burner commands exist here. Hardware validation is required.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
import time


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Query:
    command: bytes
    length: int
    delay: float


QUERIES = {
    "status": Query(bytes.fromhex("02 01 01 FC"), 16, .150),
    "subsystem": Query(bytes.fromhex("02 01 AA 53"), 10, .240),
    "channels": Query(bytes.fromhex("02 01 40 BD"), 28, .290),
}

# Auxiliary propane equivalency factor, recovered from ReturnPEF in v1.00.
# It is reported, not used to recalculate HC or burner equivalence ratios.
PEF_QUERY = Query(bytes.fromhex("02 03 18 00 00 E3"), 6, .230)

# CommandFactory.GetCommand in HORIBA v1.00: receive buffer 5 (NAK), ACK 4,
# execution delay 240 ms. Never include controls in the polling allowlist.
MODE_COMMANDS = {
    "meas": Query(bytes.fromhex("02 01 A6 57"), 4, .240),
    "standby": Query(bytes.fromhex("02 01 A7 56"), 4, .240),
}


def check_reply(name: str, reply: bytes) -> bytes:
    return check_response(QUERIES[name], reply, name)


def check_response(query: Query, reply: bytes, name: str) -> bytes:
    if len(reply) != query.length:
        raise ProtocolError(f"{name}: expected {query.length} bytes, got {len(reply)}")
    if reply[0] != 0x06 or reply[1] != query.command[2]:
        raise ProtocolError(f"{name}: response is not the expected ACK")
    if sum(reply) & 255:
        raise ProtocolError(f"{name}: checksum mismatch")
    return reply


def decode_pef(reply):
    check_response(PEF_QUERY, reply, "pef")
    return struct.unpack_from(">h", reply, 3)[0] / 1000


def decode_cycle(replies: dict[str, bytes]) -> dict:
    a, s, c = (check_reply(name, replies[name]) for name in ("status", "subsystem", "channels"))
    # The legacy ParseDatum uses signed, big-endian 16-bit values.
    def value(offset, scale=1):
        return struct.unpack_from(">h", c, offset)[0] / scale

    options = s[6]
    no = value(21) if options & 2 else None
    o2 = value(11, 100) if options & 1 else None
    alarms = []
    if a[3] & 8:
        alarms.append("warm_up")
    if any(a[i] for i in (9, 10, 11, 14)) or (options & 2 and a[12]) or (options & 1 and a[13]):
        alarms.append("calibration_error")
    if s[5] & 2:
        alarms.append("leak")
    if s[5] & 8:
        alarms.append("hang")
    if s[3] & 32:
        alarms.append("filter")
    if options & 8 and c[4] & 2:
        alarms.append("temperature")
    if options & 4 and c[4] & 1:
        alarms.append("rpm")
    # HORIBA's automotive "probe" warning uses CO2 < 1% while measuring.
    # Carbon-free NH3/H2 naturally triggers this. Preserve it as a warning,
    # not evidence of bad sampling or a combustion-validity test.
    warnings = ["automotive_low_co2_probe_warning"] if s[4] & 64 and value(5, 100) < 1 else []
    state = "warm_up" if a[3] & 8 else ("measuring" if s[4] & 64 else "standby")
    if no is None or o2 is None:
        alarms.append("missing_no_or_o2_option")
    if no is not None and not 0 <= no <= 5000:
        alarms.append("no_out_of_range")
    if o2 is not None and not 0 <= o2 <= 25:
        alarms.append("o2_out_of_range")
    return {
        "no_ppm": no, "o2_percent": o2, "co_percent": value(7, 100),
        "co2_percent": value(5, 100), "hc_ppm": value(9),
        "afr": value(13, 10), "lambda": value(15, 1000),
        "rpm": value(23) if options & 4 else None,
        "oil_temperature_c": value(25) if options & 8 else None,
        "options": options,
        "state": state, "alarms": alarms, "warnings": warnings,
        "valid": state == "measuring" and not alarms,
        "raw": {name: reply.hex() for name, reply in replies.items()},
    }


class SerialReader:
    """One port owner. A failed/partial cycle never reuses an earlier value."""

    def __init__(self, port, *, serial_factory=None, sleep=time.sleep):
        if serial_factory is None:
            from serial import Serial
            serial_factory = Serial
        self.port = serial_factory(port=None, baudrate=9600, bytesize=8, parity="N",
                                   stopbits=1, timeout=.15, write_timeout=.5,
                                   xonxoff=False, rtscts=False, dsrdtr=False)
        # Match the legacy .NET SerialPort defaults; configure before opening
        # so pyserial does not deliberately assert DTR/RTS on connection.
        self.port.dtr = False
        self.port.rts = False
        self.port.port = port
        try:
            self.port.open()
        except Exception:
            self.port.close()
            raise
        self.sleep = sleep
        self.last_raw = {}
        self.last_control_reply = ""
        self.last_pef_raw = ""

    def query(self, name):
        return self._exchange(name, QUERIES[name])

    def set_mode(self, mode):
        if mode not in MODE_COMMANDS:
            raise ValueError("Only MEAS and STANDBY are supported")
        self.last_control_reply = ""
        return self._exchange(mode, MODE_COMMANDS[mode])

    def _exchange(self, name, query):
        self.port.reset_input_buffer()
        if self.port.write(query.command) != len(query.command):
            raise ProtocolError("Incomplete serial write")
        self.sleep(query.delay)
        reply = bytearray()
        expected = query.length
        deadline = time.monotonic() + .6
        while len(reply) < expected and time.monotonic() < deadline:
            chunk = self.port.read(expected - len(reply))
            reply.extend(chunk)
            if name in MODE_COMMANDS and reply and reply[0] == 0x15:
                expected = 5  # retain the full NAK for diagnostics; never retry
        if name in QUERIES:
            self.last_raw[name] = bytes(reply).hex()
        elif name == "pef":
            self.last_pef_raw = bytes(reply).hex()
        else:
            self.last_control_reply = bytes(reply).hex()
        return check_response(query, bytes(reply), name)

    def read(self):
        self.last_raw = {}
        replies = {name: self.query(name) for name in QUERIES}
        cycle = decode_cycle(replies)
        self.last_pef_raw = ""
        cycle.update(pef=None, pef_error="")
        try:
            reply = self._exchange("pef", PEF_QUERY)
            cycle["pef"] = decode_pef(reply)
        except (OSError, ValueError) as exc:
            # Failure of the auxiliary read must not erase the channel frame.
            cycle["pef_error"] = str(exc)[:300]
            cycle["warnings"].append("pef_unavailable")
        cycle["raw_pef"] = self.last_pef_raw
        return cycle

    def close(self):
        self.port.close()


def simulated_cycle(index, *, measuring=True):
    """Synthetic frames use the real decoder; never eligible for optimisation."""
    replies = {name: bytearray(query.length) for name, query in QUERIES.items()}
    for name, frame in replies.items():
        frame[0], frame[1] = 6, QUERIES[name].command[2]
    replies["subsystem"][4] = 64 if measuring else 0
    replies["subsystem"][6] = 3
    struct.pack_into(">h", replies["channels"], 11, 1000 + index % 5)
    struct.pack_into(">h", replies["channels"], 21, 100 + index % 7)
    for frame in replies.values():
        frame[-1] = (-sum(frame[:-1])) & 255
    result = decode_cycle({key: bytes(value) for key, value in replies.items()})
    pef = bytes.fromhex("06 18 00 01 F4 ED")
    result.update(pef=decode_pef(pef), raw_pef=pef.hex(), pef_error="")
    return result
