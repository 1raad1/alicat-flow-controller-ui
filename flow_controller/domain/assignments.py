"""Pure assignment validation for supported combustion configurations."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable


GasZone = tuple[str, str]

PILOT_OPTIONS: frozenset[GasZone] = frozenset({
    ("NH3", "Pilot"), ("H2", "Pilot"), ("CH4", "Pilot"),
})

_FULL_RQL_CORE: frozenset[GasZone] = frozenset({
    ("NH3", "Zone 1"), ("H2", "Zone 1"), ("Air", "Zone 1"),
    ("NH3", "Zone 2"), ("H2", "Zone 2"), ("Air", "Zone 2"),
})

_RICH_QUENCH_CORE: frozenset[GasZone] = frozenset({
    ("NH3", "Zone 1"), ("H2", "Zone 1"), ("Air", "Zone 1"),
    ("Air", "Zone 2"),
})

FULL_RQL: frozenset[GasZone] = frozenset({
    *_FULL_RQL_CORE, ("CH4", "Pilot"),
})

RICH_QUENCH: frozenset[GasZone] = frozenset({
    *_RICH_QUENCH_CORE, ("CH4", "Pilot"),
})


def assess_autocalc(pairs: Iterable[GasZone]) -> tuple[str | None, list[str]]:
    """Return the matching automatic-calculation mode and any problems."""
    configured = list(pairs)
    pair_set = set(configured)
    pilot_pairs = pair_set & PILOT_OPTIONS
    required: frozenset[GasZone] | None
    mode: str | None
    if len(pilot_pairs) == 1 and _FULL_RQL_CORE.issubset(pair_set):
        mode = "FULL_RQL"
        required = _FULL_RQL_CORE | pilot_pairs
    elif len(pilot_pairs) == 1 and _RICH_QUENCH_CORE.issubset(pair_set):
        mode = "RICH_QUENCH"
        required = _RICH_QUENCH_CORE | pilot_pairs
    else:
        mode, required = None, None

    problems: list[str] = []
    if required is None:
        candidates = (("FULL_RQL", _FULL_RQL_CORE),
                      ("RICH_QUENCH", _RICH_QUENCH_CORE))
        best_name, best_core = min(
            candidates, key=lambda candidate: len(candidate[1] - pair_set))
        missing = best_core - pair_set
        friendly_name = (
            "Full RQL" if best_name == "FULL_RQL" else "Rich + quench-air")
        missing_parts = [
            f"{gas}/{zone}" for gas, zone in sorted(missing)
        ]
        if not pilot_pairs:
            missing_parts.append("one of NH3/Pilot, H2/Pilot, CH4/Pilot")
        if missing_parts:
            problems.append(
                f"Missing for {friendly_name}: {', '.join(missing_parts)}")
        if len(pilot_pairs) > 1:
            selected = ", ".join(gas for gas, _zone in sorted(pilot_pairs))
            problems.append(
                f"Pilot is assigned to {selected} (select exactly one fuel)")
    else:
        for pair, count in Counter(configured).items():
            if count > 1 and pair in required:
                problems.append(
                    f"{pair[0]} to {pair[1]} is set on {count} units (must be 1)")
    return mode, problems

