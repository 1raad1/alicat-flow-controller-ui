"""Read-only rig state for automation clients.

This module deliberately has no Qt, serial, or controller dependency.  It turns
the state already owned by :class:`FlowSession` into copied JSON-compatible
data, and derives time-windowed observations from ``GraphHistory``.  A future
MCP server can therefore expose these functions without gaining a route to the
session's command queues.
"""

from __future__ import annotations

from datetime import datetime
import math

from ..domain import roles
from .graph_history import METRICS_BY_KEY


TELEMETRY_FIELDS = {
    'flow': 'flow',
    'setpoint': 'sp',
    'pressure': 'press',
    'temperature': 'temp',
}


def _finite_or_none(value):
    """Return a JSON-safe finite float, preserving a missing reading."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _timestamp(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _command_limit(session, unit):
    """Read a future command ceiling without creating one in Step 1.

    The application currently has no command ceiling.  Supporting the optional
    accessor and a pre-existing ``max_flow`` record makes the read model
    forwards-compatible while keeping this step entirely non-mutating.
    """
    getter = getattr(session, 'max_flow_for', None)
    if callable(getter):
        return _finite_or_none(getter(unit))
    record = getattr(session, 'unit_prefs', {}).get(unit, {})
    return _finite_or_none(record.get('max_flow'))


def build_snapshot(session):
    """Return a copied, JSON-compatible description of the current rig.

    ``session`` is duck-typed so tests and a future headless runner can supply
    a small fake.  ``full_scale`` is exposed as display metadata, never as a
    command ceiling; the separately named ``command_max_flow`` remains ``None``
    until the command-ceiling feature is deliberately introduced.
    """
    # ``_latest_samples`` represents this polling pass, including intentional
    # blanks after a failed read.  Falling back to the display cache preserves
    # a useful snapshot before the first complete pass, but never turns a
    # current failed read into a falsely fresh value once monitoring is live.
    live_samples = (getattr(session, '_latest_samples', {}) or
                    getattr(session, '_live_samples', {}) or {})
    assignments = dict(getattr(session, 'assignments', {}) or {})
    custom = dict(getattr(session, 'custom_assignments', {}) or {})
    selection = dict(getattr(session, 'selection', {}) or {})
    prefs = getattr(session, 'unit_prefs', {}) or {}

    units = set(live_samples) | set(selection) | set(prefs) | set(custom)
    units.update(unit for unit in assignments.values() if unit)
    role_for_unit = {unit: role for role, unit in assignments.items() if unit}
    role_for_unit.update({unit: role for unit, role in custom.items()})

    telemetry = {}
    unit_records = {}
    for unit in sorted(units):
        sample = live_samples.get(unit, {}) or {}
        readings = {
            public: _finite_or_none(sample.get(sample_key))
            for public, sample_key in TELEMETRY_FIELDS.items()
        }
        telemetry[unit] = readings
        gas, zone = selection.get(unit, (None, None))
        record = prefs.get(unit, {}) or {}
        ramp_off = bool(record.get('ramp_off'))
        ramp = _finite_or_none(record.get('ramp'))
        limit = _command_limit(session, unit)
        unit_records[unit] = {
            'role': role_for_unit.get(unit),
            'assignment': {'gas': gas, 'zone': zone},
            'telemetry': dict(readings),
            'ramp_policy': {
                'declared_rate': ramp,
                'disabled': ramp_off,
                'effective_rate': None if ramp_off else ramp,
                'role_requires_minimum_ramp': role_for_unit.get(unit) in roles.RAMP_KEYS,
            },
            'display_full_scale': _finite_or_none(record.get('full_scale')),
            'command_max_flow': limit,
        }

    role_records = {}
    for role, label in roles.ROLES:
        unit = assignments.get(role)
        gas, zone = selection.get(unit, (None, None)) if unit else (None, None)
        role_records[role] = {
            'label': label,
            'unit': unit,
            'assignment': {'gas': gas, 'zone': zone},
        }

    connection = {
        'connected': bool(getattr(session, 'controllers_connected', False)),
        'connecting': bool(getattr(session, 'is_connecting', False)),
        'monitoring': bool(getattr(session, 'is_monitoring', False)),
        'port': getattr(session, 'port', None),
        'baudrate': getattr(session, 'baudrate', None),
        'poll_interval_s': _finite_or_none(getattr(session, 'poll_interval_s', None)),
        'latest_sample_at': _timestamp(getattr(session, '_latest_timestamp', None)),
    }
    return {
        'captured_at': connection['latest_sample_at'],
        'connection': connection,
        'assignments': assignments,
        'roles': role_records,
        'telemetry': telemetry,
        'units': unit_records,
        # Sparse by design: absent means no command limit has been declared.
        'declared_limits': {
            unit: record['command_max_flow'] for unit, record in unit_records.items()
            if record['command_max_flow'] is not None
        },
    }


def _window_start(times, window_s):
    if window_s is None:
        return 0
    try:
        duration = float(window_s)
    except (TypeError, ValueError) as exc:
        raise ValueError('window_s must be a non-negative finite number') from exc
    if not math.isfinite(duration) or duration < 0.0:
        raise ValueError('window_s must be a non-negative finite number')
    return next((index for index, moment in enumerate(times)
                 if moment >= times[-1] - duration), len(times))


def windowed_history(history, *, window_s=None, units=None, metric_keys=None):
    """Return copied graph history, trimmed to the most recent time window."""
    units = list(history.units) if units is None else list(units)
    keys = list(METRICS_BY_KEY) if metric_keys is None else list(metric_keys)
    unknown = set(keys) - set(METRICS_BY_KEY)
    if unknown:
        raise ValueError(f"unknown metric key(s): {', '.join(sorted(unknown))}")

    series = {}
    end_s = None
    for unit in units:
        per_unit = {}
        for key in keys:
            times, values = history.series(unit, key)
            start = _window_start(times, window_s) if times else 0
            times, values = times[start:], values[start:]
            if times:
                end_s = times[-1] if end_s is None else max(end_s, times[-1])
            per_unit[key] = {'times_s': times, 'values': [_finite_or_none(v) for v in values]}
        series[unit] = per_unit
    return {'window_s': window_s, 'end_s': end_s, 'series': series}


def flow_stability(history, unit, *, duration_s, tolerance):
    """Whether ``flow`` has tracked ``sp`` continuously for ``duration_s``.

    A true result requires a full, gap-free-enough historical window: the
    oldest checked sample must be at or before the requested start.  Missing or
    non-finite telemetry is never silently treated as stable.
    """
    try:
        duration = float(duration_s)
        allowed_error = float(tolerance)
    except (TypeError, ValueError) as exc:
        raise ValueError('duration_s and tolerance must be finite non-negative numbers') from exc
    if (not math.isfinite(duration) or duration < 0.0 or
            not math.isfinite(allowed_error) or allowed_error < 0.0):
        raise ValueError('duration_s and tolerance must be finite non-negative numbers')

    flow_times, flows = history.series(unit, 'flow')
    sp_times, setpoints = history.series(unit, 'sp')
    count = min(len(flow_times), len(sp_times), len(flows), len(setpoints))
    if not count:
        return {'stable': False, 'unit': unit, 'observed_s': 0.0, 'samples': 0,
                'max_error': None, 'reason': 'no history'}
    flow_times, flows, setpoints = (flow_times[-count:], flows[-count:], setpoints[-count:])
    end_s = flow_times[-1]
    start_s = end_s - duration
    index = next((i for i, moment in enumerate(flow_times) if moment >= start_s), count)
    checked_times, checked_flows, checked_setpoints = (
        flow_times[index:], flows[index:], setpoints[index:])
    if not checked_times:
        return {'stable': False, 'unit': unit, 'observed_s': 0.0, 'samples': 0,
                'max_error': None, 'reason': 'no samples in window'}
    observed_s = max(0.0, end_s - flow_times[0])
    errors = []
    for flow, setpoint in zip(checked_flows, checked_setpoints):
        flow, setpoint = _finite_or_none(flow), _finite_or_none(setpoint)
        if flow is None or setpoint is None:
            return {'stable': False, 'unit': unit, 'observed_s': observed_s,
                    'samples': len(checked_times), 'max_error': None,
                    'reason': 'missing telemetry'}
        errors.append(abs(flow - setpoint))
    max_error = max(errors, default=None)
    if flow_times[0] > start_s:
        reason = 'insufficient history'
    elif max_error is not None and max_error > allowed_error:
        reason = 'outside tolerance'
    else:
        reason = 'stable'
    return {'stable': reason == 'stable', 'unit': unit, 'observed_s': observed_s,
            'samples': len(checked_times), 'max_error': max_error, 'reason': reason}


def derive_state(session, *, duration_s, tolerance):
    """Return role stability and current equivalence ratios from a session."""
    phi = session.phi_values()
    assignments = getattr(session, 'assignments', {}) or {}
    return {
        'phi': {'stage_1': _finite_or_none(phi[0]), 'stage_2': _finite_or_none(phi[1]),
                'global': _finite_or_none(phi[2])},
        'roles': {
            role: (flow_stability(session.history, unit, duration_s=duration_s,
                                  tolerance=tolerance) if unit else {
                                      'stable': False, 'unit': None, 'observed_s': 0.0,
                                      'samples': 0, 'max_error': None,
                                      'reason': 'unassigned'})
            for role, unit in assignments.items()
        },
    }
