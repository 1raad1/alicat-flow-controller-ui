"""CSV acquisition log.

One row per serial polling pass.  The column set is decided once, when
logging starts, from the units that are actually on the monitor at that
moment -- not from the fixed RQL roles -- so General-zone and custom units
appear automatically.  Deciding it once matters: a file whose columns shift
mid-run cannot be loaded by anything.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path

#: Column order for the per-unit block, paired with the sample key each is
#: read from.  ``valve_drive`` is handled separately because the sample
#: carries a tuple of drives and only the first is logged.
UNIT_COLUMNS = (
    ('flow', 'flow'),
    ('sp', 'sp'),
    ('press', 'press'),
    ('temp', 'temp'),
    ('internal_sp_error', 'internal_error'),
)

#: Zones in the order their units appear on screen, so the CSV column order
#: matches what the operator is looking at.
ZONE_ORDER = ("Zone 1", "Zone 2", "Pilot", "General")

UNASSIGNED_ZONE = "-- unassigned --"
UNSELECTED_GAS = "-- select --"


def unit_label(unit, gas, zone):
    """A descriptive column prefix, falling back to the bare unit letter."""
    if gas and zone and zone != UNASSIGNED_ZONE:
        return f"{gas}_{zone.replace(' ', '')}_U{unit}"
    return f"Unit{unit}"


def order_units(assignments):
    """Order ``{unit: (gas, zone)}`` by zone, dropping unassigned units.

    Units whose zone is not one of :data:`ZONE_ORDER` are excluded rather than
    appended: they have no assignment, so there is no meaningful label to put
    at the head of their columns.
    """
    buckets = {zone: [] for zone in ZONE_ORDER}
    for unit, (gas, zone) in assignments.items():
        if gas in (UNSELECTED_GAS, '', None) or zone == UNASSIGNED_ZONE:
            continue
        if zone in buckets:
            buckets[zone].append(unit)
    ordered = []
    for zone in ZONE_ORDER:
        ordered.extend(buckets[zone])
    return ordered


def build_header(units, assignments):
    """Column names for ``units`` in the given order, plus derived phi."""
    header = ['timestamp']
    for unit in units:
        gas, zone = assignments.get(unit, ('', ''))
        label = unit_label(unit, gas, zone)
        for suffix, _sample_key in UNIT_COLUMNS:
            header.append(f"{label}_{suffix}")
        header.append(f"{label}_valve_drive_1_pct")
    header += ['phi_stage1_live', 'phi_stage2_live', 'phi_global_live']
    return header


def _format(value):
    return f"{value:.4f}" if value is not None else ''


class CsvLogger:
    """An open acquisition log, or nothing at all.

    ``source`` records who started it -- the operator or a LabVIEW datagram --
    because stopping is reported differently for each and the UDP status line
    has to be restored only in the LabVIEW case.
    """

    def __init__(self):
        self._file = None
        self._writer = None
        self.path = None
        self.units = []
        self.source = None

    @property
    def active(self):
        return self._file is not None

    def start(self, path, assignments, *, source="Manual"):
        """Open ``path`` and write the header.  Raises on failure."""
        if self.active:
            raise RuntimeError("logging is already running")
        units = order_units(assignments)
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        # buffering=1: line-buffered, so every row reaches the OS immediately
        # rather than sitting in a buffer that a power cut would discard.
        handle = open(output, 'w', newline='', buffering=1)
        try:
            writer = csv.writer(handle)
            writer.writerow(build_header(units, assignments))
        except Exception:
            handle.close()
            raise
        self._file = handle
        self._writer = writer
        self.path = str(output)
        self.units = units
        self.source = source
        return self.path

    def write_row(self, samples, phi_values, timestamp=None):
        """Append one pass.  Never raises -- a log fault must not stop control."""
        if not self.active:
            return False
        try:
            stamp = timestamp or datetime.now()
            row = [stamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]]
            for unit in self.units:
                sample = samples.get(unit, {})
                for _suffix, sample_key in UNIT_COLUMNS:
                    row.append(_format(sample.get(sample_key)))
                drives = sample.get('valve_drives') or ()
                row.append(_format(drives[0]) if drives else '')
            row += [_format(value) for value in phi_values]
            self._writer.writerow(row)
            try:
                self._file.flush()
            except Exception:
                pass
            return True
        except Exception:
            return False

    def stop(self):
        """Flush, fsync and close.  Returns the path that was written."""
        path = self.path
        handle = self._file
        self._file = None
        self._writer = None
        self.path = None
        self.source = None
        if handle is not None:
            # fsync as well as flush: line buffering gets rows out of Python,
            # but only fsync commits them past the OS cache.
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except Exception:
                pass
            try:
                handle.close()
            except Exception:
                pass
        return path


def resolve_path(raw, *, fallback, base_dir, source="Manual"):
    """Normalise an operator-typed destination into a concrete .csv path.

    A LabVIEW-triggered run gets a timestamp appended, because those arrive
    unattended and repeatedly -- overwriting the previous capture would be a
    silent data loss.
    """
    base = Path((raw or '').strip() or fallback).expanduser()
    if not base.is_absolute():
        base = Path(base_dir) / base
    if base.suffix.lower() != '.csv':
        base = base.with_suffix('.csv')
    if source == "LabVIEW":
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
        return base, base.with_name(f"{base.stem}_{stamp}{base.suffix}")
    return base, base
