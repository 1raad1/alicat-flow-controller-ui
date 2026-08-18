"""Per-controller settings the operator declares, remembered between runs.

Two numbers, both properties of the device or the line rather than of a session:

* ``full_scale`` -- the meter's full scale in SLPM, which sets the span of the
  tracking bar.  The instruments do not report it over the wire.
* ``ramp`` -- how fast that line is allowed to move, in SLPM per second, applied
  to every setpoint the application writes to it.

Unit ``C`` is the same 50 SLPM meter tomorrow morning as it was last night, and
the line it feeds takes the same time to settle, so both are declared once and
kept here: a small JSON file next to the install, so it travels with the
application and is findable by whoever is standing at the rig.

Everything here degrades rather than raises.  A missing, unreadable or
half-written file must never be the reason a rig control surface fails to start.
Neither figure is something the hardware is driven *from*: the full scale is a
bar's span, and a missing ramp rate means a setpoint is written as it always
was, so losing this file costs presentation and pacing, not control.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ENV_VAR = 'FLOW_CONTROLLER_UNIT_PREFS'
FILENAME = 'unit_prefs.json'

#: Nothing on this rig is a 10,000 SLPM device; a value past this is a typo or
#: a units mix-up, and a bar scaled by it would read empty at every setpoint.
MAX_FULL_SCALE = 10000.0

#: A rate this high is not a ramp -- at 1000 SLPM/s every line on the rig
#: arrives inside one polling interval, which is what "no limit" already means.
MAX_RAMP_RATE = 1000.0

#: The fields a unit's record may hold, and the ceiling each is held to.
CEILINGS = {'full_scale': MAX_FULL_SCALE, 'ramp': MAX_RAMP_RATE}


def path():
    """Where the file lives; ``FLOW_CONTROLLER_UNIT_PREFS`` overrides it."""
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / FILENAME


def clean(value, ceiling=MAX_FULL_SCALE):
    """One declared figure, or ``None`` for "no declaration".

    Zero and negatives clear the declaration rather than being clamped to
    something tiny.  A bar spanning nothing would read full at any reading at
    all, and a ramp rate of nothing would never arrive -- in both cases the
    absent declaration is the safe reading of the number.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not number > 0.0 or number != number or number == float('inf'):
        return None
    return min(number, float(ceiling))


def clean_field(field, value):
    """Clean ``value`` against the ceiling that belongs to ``field``."""
    if field not in CEILINGS:
        return None
    return clean(value, CEILINGS[field])


def clean_record(raw):
    """One unit's record, keeping only fields that mean something.

    A bare number is the older single-purpose form of this file, when it held
    nothing but full scales; it is read as one so an existing rig does not lose
    figures that were already typed in.
    """
    if not isinstance(raw, dict):
        number = clean(raw, MAX_FULL_SCALE)
        return {} if number is None else {'full_scale': number}
    record = {}
    for field in CEILINGS:
        number = clean_field(field, raw.get(field))
        if number is not None:
            record[field] = number
    return record


def load(store_path=None):
    """``{unit: {field: value}}`` for whatever the file holds that makes sense."""
    target = Path(store_path) if store_path else path()
    try:
        with target.open('r', encoding='utf-8') as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    prefs = {}
    for unit, value in raw.items():
        record = clean_record(value)
        if record:
            prefs[str(unit)] = record
    return prefs


def save(prefs, store_path=None):
    """Write the map back out.  Returns an error string, or ``None``."""
    target = Path(store_path) if store_path else path()
    payload = {}
    for unit, record in prefs.items():
        cleaned = clean_record(record)
        if cleaned:
            payload[str(unit)] = {field: round(value, 4)
                                  for field, value in cleaned.items()}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write('\n')
    except OSError as exc:
        return f'{target}: {exc}'
    return None
