"""Typed domain objects shared by the UI and controller services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ControllerInfo:
    """A controller found during discovery and its device-specific gas table."""

    unit: str
    data: dict[str, Any]
    supported_gases: dict[int, str] = field(default_factory=dict)

    @property
    def active_gas(self) -> str:
        value = str(self.data.get("gas", "Unknown")).strip()
        return value or "Unknown"

    def gas_options(self) -> list[str]:
        """Return supported gas names in register order without duplicates."""
        names: list[str] = []
        seen: set[str] = set()
        for _index, name in sorted(self.supported_gases.items()):
            cleaned = str(name).strip()
            key = cleaned.casefold()
            if cleaned and key not in seen:
                names.append(cleaned)
                seen.add(key)

        # A live reading proves the active gas is usable even if older
        # firmware omitted it from the gas-table response.
        active = self.active_gas
        if active.casefold() != "unknown" and active.casefold() not in seen:
            names.append(active)
        return names


@dataclass(slots=True)
class DiscoveryResult:
    """Controllers found during one scan plus an optional fatal scan error."""

    controllers: list[ControllerInfo]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    """One timestamped device sample, independent of any GUI widget."""

    unit: str
    timestamp: float
    flow: float | None
    setpoint: float | None
    pressure: float | None
    temperature: float | None
    internal_error: float | None = None
    valve_drive: float | None = None
