"""Rolling history for every acquired metric.

History is kept for everything the monitor reads, whether or not it is
currently on screen.  That is the whole point: an operator who switches to
the graph tab twenty minutes into a run sees the preceding twenty minutes,
and a metric that turns out to matter can be plotted after the fact instead
of having to be predicted before the run.

Storage is a fixed-length deque per unit and metric, so a long run costs a
bounded amount of memory rather than growing until it is a problem.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    """One plottable quantity."""

    key: str
    label: str
    group: str          # axis group -- metrics sharing a group share an axis
    unit: str           # axis unit label
    sample_key: str     # key in the acquisition sample
    dash: str           # 'solid' | 'dash' | 'dot' | 'dashdot'


#: Everything the monitor records.  ``flow`` and ``sp`` deliberately share the
#: ``flow`` group so a trace and its command sit on one axis and can be read
#: against each other.
METRICS = (
    Metric('flow', 'Flow', 'flow', 'SLPM', 'flow', 'solid'),
    Metric('sp', 'Setpoint', 'flow', 'SLPM', 'sp', 'dash'),
    Metric('press', 'Pressure', 'pressure', 'psia', 'press', 'solid'),
    Metric('temp', 'Temperature', 'temperature', '°C', 'temp', 'solid'),
    Metric('internal_error', 'SP Error', 'error', 'SLPM', 'internal_error', 'dot'),
    Metric('valve_drive', 'Valve Drive', 'valve', '%', 'valve_drive', 'dashdot'),
)

METRICS_BY_KEY = {metric.key: metric for metric in METRICS}

GROUP_LABELS = {
    'flow': 'Flow & Setpoint',
    'pressure': 'Pressure',
    'temperature': 'Temperature',
    'error': 'Internal Setpoint Error',
    'valve': 'Valve Drive',
}

#: Group order used when several axes are stacked, so the layout does not
#: reshuffle as series are ticked on and off.
GROUP_ORDER = ('flow', 'pressure', 'temperature', 'error', 'valve')


class GraphHistory:
    """Bounded per-unit, per-metric history sharing one time axis.

    The time axis is common to every series because all metrics in a pass are
    read from the same acquisition sweep.  Units added mid-run therefore have
    shorter histories, and are aligned to the *end* of the time axis.
    """

    def __init__(self, limit=3600):
        self.limit = limit
        self._times = deque(maxlen=limit)
        self._t0 = None
        self._history: dict[str, dict[str, deque]] = {}
        self._last_generation = None

    # -- shape ------------------------------------------------------------ #

    @property
    def units(self):
        return list(self._history)

    def set_units(self, units):
        """Track exactly ``units``, keeping history for those already present."""
        for unit in units:
            if unit not in self._history:
                self._history[unit] = {
                    metric.key: deque(maxlen=self.limit) for metric in METRICS
                }
        for unit in list(self._history):
            if unit not in units:
                del self._history[unit]

    def set_limit(self, limit):
        """Re-bound every deque, keeping the most recent ``limit`` samples."""
        if limit == self.limit:
            return
        self.limit = limit
        self._times = deque(self._times, maxlen=limit)
        for metrics in self._history.values():
            for key, history in list(metrics.items()):
                metrics[key] = deque(history, maxlen=limit)

    # -- writing ---------------------------------------------------------- #

    def push(self, generation, timestamp, samples):
        """Append one acquisition pass.

        ``generation`` is the monitor's pass counter.  The UI and the graph
        renderer both run faster than the serial loop, so without it the same
        hardware sample would be appended several times and the time axis
        would stretch to fit readings that were never taken.
        """
        if generation is not None and generation == self._last_generation:
            return False
        self._last_generation = generation
        if self._t0 is None:
            self._t0 = timestamp
        self._times.append((timestamp - self._t0).total_seconds())
        for unit, metrics in self._history.items():
            sample = samples.get(unit, {})
            drives = sample.get('valve_drives') or ()
            for metric in METRICS:
                if metric.key == 'valve_drive':
                    value = drives[0] if drives else None
                else:
                    value = sample.get(metric.sample_key)
                metrics[metric.key].append(value)
        return True

    def clear(self, generation=None):
        self._times.clear()
        self._t0 = None
        self._last_generation = generation
        for metrics in self._history.values():
            for history in metrics.values():
                history.clear()

    # -- reading ---------------------------------------------------------- #

    def times(self):
        return self._times

    def series(self, unit, metric_key):
        """The ``(times, values)`` pair for one series, tail-aligned.

        A unit that joined the run late has fewer samples than the time axis
        has entries; its trace belongs at the recent end, not stretched across
        the whole window.
        """
        history = self._history.get(unit, {}).get(metric_key)
        if not history or not self._times:
            return [], []
        count = min(len(history), len(self._times))
        if count == 0:
            return [], []
        times = list(self._times)[-count:]
        values = list(history)[-count:]
        return times, values

    def raw(self, unit, metric_key):
        """The stored deque for one series, without copying it.

        The live renderer converts straight to an array and tail-aligns as it
        goes, so handing it the deque saves copying the whole window on every
        frame -- at a few hundred samples times a dozen traces that is the
        difference between free and measurable.  :meth:`series` stays the
        accessor for callers that want a plain aligned pair.
        """
        return self._history.get(unit, {}).get(metric_key) or ()

    def export_rows(self, units=None, metric_keys=None):
        """``(header, rows)`` covering every stored sample.

        Missing readings stay empty, matching the CSV log: a blank cell is a
        read that failed, and filling it in would invent data.
        """
        units = list(self._history) if units is None else list(units)
        keys = ([metric.key for metric in METRICS] if metric_keys is None
                else list(metric_keys))
        header = ['time_s']
        columns = []
        for unit in units:
            for key in keys:
                metric = METRICS_BY_KEY[key]
                header.append(f"U{unit}_{metric.key}_{metric.unit}")
                columns.append(self._history.get(unit, {}).get(key))
        rows = []
        times = list(self._times)
        for index, moment in enumerate(times):
            row = [moment]
            for column in columns:
                if column is None:
                    row.append(None)
                    continue
                # Tail-align the same way series() does.
                offset = index - (len(times) - len(column))
                row.append(column[offset] if 0 <= offset < len(column) else None)
            rows.append(row)
        return header, rows
