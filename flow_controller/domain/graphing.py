"""Pure validation helpers for graph display controls."""

from __future__ import annotations

import math


def parse_axis_limits(
        automatic: bool,
        minimum_text: str,
        maximum_text: str,
        axis_name: str,
) -> tuple[float, float] | None:
    """Return validated manual limits, or ``None`` for automatic scaling."""
    if automatic:
        return None
    try:
        minimum = float(minimum_text.strip())
        maximum = float(maximum_text.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{axis_name} limits must be numeric.") from exc
    if not math.isfinite(minimum) or not math.isfinite(maximum):
        raise ValueError(f"{axis_name} limits must be finite numbers.")
    if minimum >= maximum:
        raise ValueError(
            f"{axis_name} minimum must be smaller than its maximum.")
    return minimum, maximum


def padded_limits(
        low: float,
        high: float,
        *,
        pad: float = 0.05,
        minimum_span: float = 1e-6,
) -> tuple[float, float]:
    """Return display limits enclosing ``low``..``high`` with a margin.

    A flat series has no span of its own, so it is given ``minimum_span``
    around its value rather than collapsing to a zero-height axis.
    """
    if not math.isfinite(low) or not math.isfinite(high):
        raise ValueError("Axis bounds must be finite numbers.")
    if low > high:
        low, high = high, low
    span = high - low
    if span < minimum_span:
        centre = (high + low) / 2.0
        half = max(minimum_span, abs(centre) * pad) / 2.0
        return centre - half, centre + half
    margin = span * pad
    return low - margin, high + margin


#: Leading digits an automatic bar span may land on, within one decade.  Finer
#: than the usual 1/2/5 ladder because these bars are read at a glance for "how
#: hard is this line working": jumping a 10 SLPM line onto a 20 SLPM span would
#: park it at half full for the rest of the run.
BAR_STEPS = (1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)


def auto_bar_span(
        peak: float,
        *,
        floor: float = 1.0,
        headroom: float = 1.15,
) -> float:
    """A tracking-bar span for a meter whose full scale nobody has declared.

    The controllers do not report their range over the wire, so the largest
    figure a line has been asked for is the only honest basis for one.  Two
    things are added to it:

    ``floor``
        A span of zero has no width to fill, and the first reading to arrive
        against it -- one that is merely nonzero -- fills the bar completely.
        That is what made a freshly connected rig show every meter pegged.

    ``headroom``
        A span *equal* to the peak reads as 100% at every new peak, which is
        the same defect one sample later.  Rounding up to a readable number
        above it leaves the bar somewhere in its track, and keeps it there:
        the span is a round figure the next peak will usually not exceed, so
        the bar climbs rather than re-scaling under the operator.
    """
    try:
        peak = float(peak)
    except (TypeError, ValueError):
        peak = 0.0
    floor = max(0.0, float(floor))
    if not math.isfinite(peak) or peak <= 0.0:
        return floor
    wanted = max(floor, peak * float(headroom))
    decade = 10.0 ** math.floor(math.log10(wanted))
    mantissa = wanted / decade
    for step in BAR_STEPS:
        if mantissa <= step + 1e-9:
            # Rounded because 1.2 * 10 lands on 12.000000000000002, and the
            # figure is shown to the operator as well as drawn.
            return round(step * decade, 9)
    return round(10.0 * decade, 9)


def should_rescale(
        current: tuple[float, float] | None,
        data_low: float,
        data_high: float,
        *,
        shrink_ratio: float = 0.5,
) -> bool:
    """Decide whether an auto-scaled axis is worth redrawing.

    Rescaling forces a full figure repaint, so it is only worth doing when
    the data has moved outside the current limits, or when it has shrunk far
    enough that most of the axis is empty.  Without this hysteresis every
    frame would rescale by a pixel and the blitting fast path would never be
    taken.
    """
    if current is None:
        return True
    current_low, current_high = current
    if not (math.isfinite(current_low) and math.isfinite(current_high)):
        return True
    if not (math.isfinite(data_low) and math.isfinite(data_high)):
        return False
    if data_low < current_low or data_high > current_high:
        return True
    current_span = current_high - current_low
    if current_span <= 0:
        return True
    return (data_high - data_low) / current_span < shrink_ratio
