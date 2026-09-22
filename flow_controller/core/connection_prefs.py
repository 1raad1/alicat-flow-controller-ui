"""Last-used connection and assignment choices, remembered between runs.

This file records operator-facing defaults only.  It never represents a live
connection or a valid discovery result: a new process must still scan and
connect before it can control hardware.

Like the other preference stores, malformed or unreadable data degrades to an
empty set of preferences.  Writes use a sibling temporary file followed by an
atomic replace so an interrupted write cannot leave half a JSON document.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


ENV_VAR = 'FLOW_CONTROLLER_CONNECTION_PREFS'
FILENAME = 'connection_prefs.json'

MAX_TEXT = 128
MIN_BAUD = 300
MAX_BAUD = 4_000_000


def path():
    """Where the file lives; ``FLOW_CONTROLLER_CONNECTION_PREFS`` overrides it."""
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / FILENAME


def _text(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_TEXT or any(ord(char) < 32 for char in value):
        return None
    return value


def _baud(value):
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not MIN_BAUD <= value <= MAX_BAUD:
        return None
    return value


def _assignment(raw):
    if not isinstance(raw, dict):
        return None
    gas = _text(raw.get('gas'))
    zone = _text(raw.get('zone'))
    included = raw.get('included')
    if gas is None or zone is None or not isinstance(included, bool):
        return None
    return {'gas': gas, 'zone': zone, 'included': included}


def clean(raw):
    """Return the meaningful, validated subset of a preference document."""
    if not isinstance(raw, dict):
        return {}

    cleaned = {'connections': {}}
    last_port = _text(raw.get('last_port'))
    last_baud = _baud(raw.get('last_baud'))
    if last_port is not None:
        cleaned['last_port'] = last_port
    if last_baud is not None:
        cleaned['last_baud'] = last_baud

    connections = raw.get('connections')
    if not isinstance(connections, dict):
        return cleaned
    for raw_port, raw_record in connections.items():
        port = _text(raw_port)
        if port is None or not isinstance(raw_record, dict):
            continue
        record = {}
        baud = _baud(raw_record.get('baud'))
        if baud is not None:
            record['baud'] = baud
        units = raw_record.get('units')
        if isinstance(units, dict):
            valid_units = {}
            for raw_unit, raw_assignment in units.items():
                unit = _text(raw_unit)
                assignment = _assignment(raw_assignment)
                if unit is not None and assignment is not None:
                    valid_units[unit] = assignment
            if valid_units:
                record['units'] = valid_units
        if record:
            cleaned['connections'][port] = record
    return cleaned


def load(store_path=None):
    """Load valid preferences; missing, corrupt, and unreadable files are empty."""
    target = Path(store_path) if store_path else path()
    try:
        with target.open('r', encoding='utf-8') as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    return clean(raw)


def save(prefs, store_path=None):
    """Atomically write valid preferences.  Returns an error string or ``None``."""
    target = Path(store_path) if store_path else path()
    temporary = target.with_name(target.name + '.tmp')
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(clean(prefs), handle, indent=2, sort_keys=True)
            handle.write('\n')
        os.replace(temporary, target)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        return f'{target}: {exc}'
    return None
