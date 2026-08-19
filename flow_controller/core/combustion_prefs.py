"""What the live combustion estimate needs to know, remembered between runs.

Two kinds of declaration, both properties of the rig or of the machine running
the software rather than of a session:

* the **inlet diameters** -- one for the standard single-inlet rig, one per
  stage for the RQL burner.  Bulk velocity cannot be computed without a bore,
  the controllers obviously do not report it, and the same burner is the same
  bore tomorrow, so it is typed once and kept.
* the **number of Stage 2 inlets**.  Its entered diameter describes one inlet;
  velocity uses the combined area of every identical Stage 2 inlet.
* whether the estimate runs **live** and **how often**.  The arithmetic itself
  is a few dozen floating-point operations per pass and costs nothing; what
  costs is repainting a dozen tiles ten times a second on a laptop that is also
  driving the graph.  ``interval`` is in acquisition passes, so 5 means the
  card refreshes on every fifth pass and ``live = false`` means it does not
  refresh at all.

Stored next to the install like the other preference files, so it travels with
the application and is findable by whoever is standing at the rig.  Everything
degrades rather than raises: a missing, unreadable or half-written file leaves
the estimate running with no declared bore, which costs a velocity reading and
nothing else.  None of these figures is written to hardware.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ENV_VAR = 'FLOW_CONTROLLER_COMBUSTION_PREFS'
FILENAME = 'combustion_prefs.json'

#: The three inlets an estimate can be asked about.  ``all`` is the whole rig
#: -- one inlet, every gas -- which is what standard mode shows; the two stage
#: scopes are the RQL burner's own inlets.
SCOPE_ALL = 'all'
SCOPE_STAGE1 = 'stage1'
SCOPE_STAGE2 = 'stage2'
SCOPES = (SCOPE_ALL, SCOPE_STAGE1, SCOPE_STAGE2)

#: Scope -> the field its diameter is stored under.  Separate names rather than
#: a nested dict so a hand-edited file stays flat and obvious.
DIAMETER_FIELDS = {
    SCOPE_ALL: 'inlet_mm',
    SCOPE_STAGE1: 'stage1_mm',
    SCOPE_STAGE2: 'stage2_mm',
}

#: A bore past this is a typo or a units mix-up -- metres typed into a
#: millimetre box reads as a velocity a thousand times too low, which is the
#: kind of wrong number that gets believed.
MAX_DIAMETER_MM = 2000.0

#: Enough to catch a pasted diameter or flow figure without constraining any
#: plausible multi-port injector arrangement.
MAX_INLET_COUNT = 100

#: Most acquisition passes the estimate may skip between refreshes.  At the
#: rig's ~10 Hz this is about a minute, which is already well past the point
#: where the card has stopped being a live reading.
MAX_INTERVAL = 600

#: What counts as a yes when the file has been edited by hand.
TRUTHS = frozenset({'1', 'true', 'yes', 'on'})

#: Everything the file may hold, with the value in force when it does not.
#: A diameter of ``None`` is "not declared", which is what suppresses the
#: velocity tile rather than filling it with a guess.
DEFAULTS = {
    'inlet_mm': None,
    'stage1_mm': None,
    'stage2_mm': None,
    'stage2_inlets': 1,
    'live': True,
    'interval': 1,
}


def path():
    """Where the file lives; ``FLOW_CONTROLLER_COMBUSTION_PREFS`` overrides it."""
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / FILENAME


def clean_diameter(value):
    """One declared bore in millimetres, or ``None`` for no declaration.

    Zero and negatives withdraw the declaration rather than clamping to
    something tiny: a bore of nothing would give an infinite velocity, and the
    absent declaration is the safe reading of the number.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not number > 0.0 or number != number or number == float('inf'):
        return None
    return min(number, MAX_DIAMETER_MM)


def clean_interval(value):
    """Passes between refreshes: a whole number, at least 1."""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return DEFAULTS['interval']
    return max(1, min(number, MAX_INTERVAL))


def clean_inlet_count(value):
    """Number of identical Stage 2 inlets: a whole number, at least one."""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return DEFAULTS['stage2_inlets']
    return max(1, min(number, MAX_INLET_COUNT))


def clean_live(value):
    """Whether the estimate runs at all.  Absent or unreadable means yes."""
    if value is None:
        return DEFAULTS['live']
    if isinstance(value, str):
        return value.strip().lower() in TRUTHS
    return bool(value)


def clean_field(field, value):
    """Clean ``value`` against whatever rule belongs to ``field``."""
    if field in DIAMETER_FIELDS.values():
        return clean_diameter(value)
    if field == 'interval':
        return clean_interval(value)
    if field == 'stage2_inlets':
        return clean_inlet_count(value)
    if field == 'live':
        return clean_live(value)
    return None


def clean(raw):
    """A complete settings map from whatever the file holds that makes sense."""
    prefs = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return prefs
    for field in DEFAULTS:
        if field in raw:
            prefs[field] = clean_field(field, raw[field])
    return prefs


def load(store_path=None):
    """The settings in force, defaults included."""
    target = Path(store_path) if store_path else path()
    try:
        with target.open('r', encoding='utf-8') as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return dict(DEFAULTS)
    return clean(raw)


def save(prefs, store_path=None):
    """Write the settings back out.  Returns an error string, or ``None``."""
    target = Path(store_path) if store_path else path()
    cleaned = clean(prefs)
    payload = {field: (round(value, 4) if isinstance(value, float) else value)
               for field, value in cleaned.items() if value is not None}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write('\n')
    except OSError as exc:
        return f'{target}: {exc}'
    return None
