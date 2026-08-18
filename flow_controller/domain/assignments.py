"""Pure assignment validation for supported combustion configurations."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable


GasZone = tuple[str, str]

FULL_RQL: frozenset[GasZone] = frozenset({
    ("NH3", "Zone 1"), ("H2", "Zone 1"), ("Air", "Zone 1"),
    ("NH3", "Zone 2"), ("H2", "Zone 2"), ("Air", "Zone 2"),
    ("CH4", "Pilot"),
})

RICH_QUENCH: frozenset[GasZone] = frozenset({
    ("NH3", "Zone 1"), ("H2", "Zone 1"), ("Air", "Zone 1"),
    ("Air", "Zone 2"), ("CH4", "Pilot"),
})


def assess_autocalc(pairs: Iterable[GasZone]) -> tuple[str | None, list[str]]:
    """Return the matching automatic-calculation mode and any problems."""
    configured = list(pairs)
    pair_set = set(configured)
    required: frozenset[GasZone] | None
    mode: str | None
    if FULL_RQL.issubset(pair_set):
        mode, required = "FULL_RQL", FULL_RQL
    elif RICH_QUENCH.issubset(pair_set):
        mode, required = "RICH_QUENCH", RICH_QUENCH
    else:
        mode, required = None, None

    problems: list[str] = []
    if required is None:
        candidates = (("FULL_RQL", FULL_RQL), ("RICH_QUENCH", RICH_QUENCH))
        best_name, _best_required = min(
            candidates, key=lambda candidate: len(candidate[1] - pair_set))
        missing = _best_required - pair_set
        friendly_name = (
            "Full RQL" if best_name == "FULL_RQL" else "Rich + quench-air")
        missing_text = ", ".join(
            f"{gas}/{zone}" for gas, zone in sorted(missing))
        problems.append(f"Missing for {friendly_name}: {missing_text}")
    else:
        for pair, count in Counter(configured).items():
            if count > 1 and pair in required:
                problems.append(
                    f"{pair[0]} to {pair[1]} is set on {count} units (must be 1)")
    return mode, problems

