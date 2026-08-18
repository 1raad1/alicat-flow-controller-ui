"""Pure selection rules for operator zero-flow actions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ZeroRequest:
    """A priority zero command to be serviced by the serial monitor owner."""

    scope: str
    units: tuple[str, ...]


def select_zero_units(
        unit_gases: Iterable[tuple[str, str]], *, include_air: bool) -> list[str]:
    """Return unique units to zero, preserving assignment order.

    ``include_air=False`` follows the operator rule that every selected gas
    except a gas named exactly ``Air`` is treated as fuel.
    """
    units: list[str] = []
    seen: set[str] = set()
    for unit, gas in unit_gases:
        cleaned_unit = str(unit).strip()
        if not cleaned_unit or cleaned_unit in seen:
            continue
        if not include_air and str(gas).strip().casefold() == "air":
            continue
        units.append(cleaned_unit)
        seen.add(cleaned_unit)
    return units
