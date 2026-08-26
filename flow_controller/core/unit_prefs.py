"""Per-controller settings the operator declares, remembered between runs.

Three declarations, all properties of the device or the line rather than of a
session:

* ``full_scale`` -- the meter's full scale in SLPM, which sets the span of the
  tracking bar.  The instruments do not report it over the wire.
* ``ramp`` -- how fast that line is allowed to move, in SLPM per second, applied
  to every setpoint the application writes to it.
* ``max_flow`` -- the largest setpoint the application may command, in SLPM.
  Unlike ``full_scale`` this is a control limit, not a presentation setting.
* ``ramp_off`` -- ramping turned off outright.  Not the same thing as an absent
  ``ramp``: no rate only means none was typed, and the application still holds
  the pilot and air lines to a minimum move time of its own.  This flag is the
  operator saying that this controller takes its setpoints exactly as written,
  and it is deliberately a separate field so that saying it has to be an act.

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

#: The widest command ceiling the application will retain.  This is an input
#: sanity bound, not a substitute for the declared per-unit ``max_flow``.
MAX_COMMAND_FLOW = 10000.0

#: The figures a unit's record may hold, and the ceiling each is held to.
CEILINGS = {'full_scale': MAX_FULL_SCALE, 'ramp': MAX_RAMP_RATE,
            'max_flow': MAX_COMMAND_FLOW}

#: The fields that are a declaration rather than a figure.  Only ``True`` is
#: ever stored -- "not turned off" is the absence of the field -- so a record
#: that has never been touched stays absent from the file entirely.
FLAGS = ('ramp_off',)

#: What counts as a yes when the file has been edited by hand.  Anything else
#: -- ``false``, ``0``, a typo -- reads as not declared, which is the state that
#: keeps the application's own pacing in force.
TRUTHS = frozenset({'1', 'true', 'yes', 'on'})


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


def clean_flag(value):
    """One declaration: ``True``, or ``None`` for "not declared".

    ``False`` and ``None`` come back the same way on purpose.  A flag that is
    not set is a flag that is not in the file, so turning one back off through
    :meth:`~flow_controller.core.session.FlowSession._set_pref` removes it
    rather than writing a ``false`` that would have to be read again later.
    """
    if isinstance(value, str):
        return True if value.strip().lower() in TRUTHS else None
    return True if value else None


def clean_field(field, value):
    """Clean ``value`` against whatever rule belongs to ``field``."""
    if field in FLAGS:
        return clean_flag(value)
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
    for field in FLAGS:
        if clean_flag(raw.get(field)):
            record[field] = True
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
            payload[str(unit)] = {
                field: (value if field in FLAGS else round(value, 4))
                for field, value in cleaned.items()}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write('\n')
    except OSError as exc:
        return f'{target}: {exc}'
    return None
