"""Low-level Alicat serial commands and response parsing.

This module deliberately has no Tkinter dependency.  The UI owns presentation;
this class owns protocol formatting, serial transactions, and parsing.
"""

from __future__ import annotations

from collections.abc import Callable
import math
import re
import time

import serial


LogCallback = Callable[[str], None]


class AlicatProtocol:
    """Synchronous protocol helpers executed by the serial-owner worker."""

    def __init__(self, logger: LogCallback | None = None):
        self._logger = logger

    def _log(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)

    def query_gases(self, port: str, unit: str, baudrate: int) -> dict[int, str]:
        """Read the gas table supported by one controller."""
        try:
            command = f"{unit}??G*\r".encode()
            with serial.Serial(port, baudrate=baudrate, timeout=3) as connection:
                connection.reset_input_buffer()
                connection.write(command)
                chunks: list[bytes] = []
                for _ in range(40):
                    time.sleep(0.05)
                    waiting = connection.in_waiting
                    if waiting:
                        chunks.append(connection.read(waiting))
                    elif chunks:
                        break
            return self.parse_gas_table(b"".join(chunks).decode(errors="replace"))
        except Exception as exc:
            self._log(f"Unit {unit} gas query error: {exc}")
            return {}

    @staticmethod
    def parse_gas_table(response: str) -> dict[int, str]:
        """Parse both labelled and legacy whitespace gas-table responses."""
        gases: dict[int, str] = {}
        labelled = re.compile(r"[A-Z]\s+G(\d+)\s+(\S+)", re.IGNORECASE)
        for match in labelled.finditer(response):
            gases[int(match.group(1))] = match.group(2).strip()
        if gases:
            return gases

        clean = re.sub(r"\b[A-Z]\b", "", response)
        tokens = clean.split()
        index = 0
        while index + 1 < len(tokens):
            try:
                gases[int(tokens[index])] = tokens[index + 1]
                index += 2
            except ValueError:
                index += 1
        return gases

    @staticmethod
    def parse_numeric_response(raw: str | None, unit: str) -> list[float] | None:
        """Return finite numeric fields from an Alicat telemetry response."""
        if not raw:
            return None
        parts = (raw.replace('%', ' ').replace(',', ' ')
                    .replace('=', ' ').replace(':', ' ').split())
        if not parts or any(part == '?' for part in parts):
            return None
        if parts[0].upper() == str(unit).upper():
            parts = parts[1:]
        values: list[float] = []
        for part in parts:
            try:
                value = float(part)
                if math.isfinite(value):
                    values.append(value)
            except (TypeError, ValueError):
                continue
        return values or None

    def set_gas(self, port: str, unit: str, gas_index: int, baudrate: int) -> None:
        """Set a gas register and verify its readback."""
        command_write = f"{unit}$$W46={gas_index}\r".encode()
        command_read = f"{unit}$$R46\r".encode()
        with serial.Serial(port, baudrate=baudrate, timeout=2) as connection:
            connection.reset_input_buffer()
            connection.write(command_write)
            time.sleep(0.1)
            connection.read(connection.in_waiting or 64)
            connection.write(command_read)
            time.sleep(0.1)
            response = connection.read(
                connection.in_waiting or 64).decode(errors="replace")
        try:
            register_value = int(response.split()[-1]) & 0b111111111
        except (ValueError, IndexError) as exc:
            raise OSError(
                f"Could not parse gas register readback: {response!r}") from exc
        if register_value != (gas_index & 0b111111111):
            raise OSError(
                f"Register readback mismatch: wrote {gas_index}, got {register_value}")

