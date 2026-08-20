"""PySide6 + pyqtgraph implementation of the Logging & Graphs plot area.

This began as the step-2 rendering spike and is now the panel the Qt app
draws with, so it takes its metric table from
``flow_controller.core.graph_history`` and its colours from the theme instead
of carrying copies of either.  What it deliberately keeps is the spike's
*interface*: history is duck-typed (``.times``, ``.values(unit, metric)``,
``.unit_meta(unit)``) and the constructor still accepts every rendering
option the benchmark sweeps, so ``bench_render_cpu.py`` measures the same
widget the operator uses rather than a lookalike.

The scaling policy stays in ``flow_controller.domain.graphing``: limits are
computed by ``padded_limits``/``should_rescale`` and pushed into pyqtgraph,
rather than being left to automatic range selection. Pyqtgraph repaints on
every range change, so the hysteresis is a performance measure here too.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..core.graph_history import GROUP_LABELS, GROUP_ORDER, METRICS
from ..domain.graphing import padded_limits, should_rescale
from . import qt_theme as theme

#: Stable dash codes shared with the core metric table.
_DASH_CODES = {'solid': '-', 'dash': '--', 'dot': ':', 'dashdot': '-.'}

#: ``metric key -> (label, group, unit, sample key, dash)``.  Derived from the
#: core table rather than restated: the metrics a panel can draw are exactly
#: the metrics the history stores, and two lists that must agree are better
#: written as one.
GRAPH_METRICS = {
    metric.key: (metric.label, metric.group, metric.unit, metric.sample_key,
                 _DASH_CODES[metric.dash])
    for metric in METRICS
}
GRAPH_GROUP_LABELS = GROUP_LABELS

_PEN_STYLES = {
    '-': Qt.PenStyle.SolidLine,
    '--': Qt.PenStyle.DashLine,
    ':': Qt.PenStyle.DotLine,
    '-.': Qt.PenStyle.DashDotLine,
}


class GraphHistory:
    """The subset of the app's graph state this panel needs.

    Kept as an explicit seam so the spike can be driven from synthetic data
    now and from ``FlowControllerApp`` later without changing the panel.
    """

    def __init__(self):
        self.times = []
        self._series = {}
        self._meta = {}

    def set_unit_meta(self, unit, *, label, color):
        self._meta[unit] = {'label': label, 'color': color}

    def unit_meta(self, unit):
        return self._meta.get(unit, {'label': f'Unit {unit}', 'color': '#efe9dc'})

    def set_series(self, unit, metric, values):
        self._series[(unit, metric)] = values

    def values(self, unit, metric):
        return self._series.get((unit, metric))


class QtGraphPanel(QWidget):
    """Lazily-rendered multi-axis trace panel.

    Rendering runs only while the widget is visible *and* at least one series
    is selected. History is owned by the caller and keeps accumulating
    regardless, so re-showing the panel draws the full trace rather than
    restarting it.
    """

    RENDER_MS = 200
    LIMIT_CHECK_FRAMES = 5

    def __init__(self, history: GraphHistory, parent=None, *,
                 gap_aware=True, downsample=False, clip_to_view=False,
                 pen_width=1, use_opengl=False):
        """``gap_aware`` breaks traces at dropped readings.

        ``downsample``/``clip_to_view`` are pyqtgraph's long-history
        optimisations.  They cost more than they save at the 600-point history
        this app keeps, so they default off; they become worth turning on if
        the history limit is raised substantially.

        ``pen_width`` of 1 gets Qt's fast cosmetic-pen path; anything wider
        goes through the general stroker and costs noticeably more.
        ``use_opengl`` moves rasterisation onto the GPU.
        """
        super().__init__(parent)
        self._history = history
        self._gap_aware = gap_aware
        self._downsample = downsample
        self._clip_to_view = clip_to_view
        self._pen_width = pen_width
        self._selected = []
        self._plots = {}
        self._curves = {}
        self._frame_index = 0
        self._needs_limits = False
        self._manual_limits = {}
        self._grid = True

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

        self._placeholder = QLabel(
            "Tick a controller and a measurement to start plotting.")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: {theme.font_pt(10)}pt;")
        self._layout.addWidget(self._placeholder)

        # Antialiasing is the single biggest pyqtgraph cost and buys little on
        # dense telemetry traces, so it stays off by default.
        pg.setConfigOptions(antialias=False, background=theme.BG_CARD,
                            foreground=theme.TEXT_MUTED, useOpenGL=use_opengl,
                            enableExperimental=use_opengl)
        self._graphics = pg.GraphicsLayoutWidget()
        self._graphics.setBackground(theme.BG_PANEL)
        self._graphics.hide()
        self._layout.addWidget(self._graphics)

        self._timer = QTimer(self)
        self._timer.setInterval(self.RENDER_MS)
        self._timer.timeout.connect(self._render_tick)

    # ------------------------------------------------------------------ #
    #  Selection and lazy activation                                      #
    # ------------------------------------------------------------------ #
    def set_selection(self, selection):
        """Set the visible ``(unit, metric)`` series and rebuild the layout."""
        self._selected = list(selection)
        self._rebuild()
        self._sync_activation()

    def set_grid(self, enabled):
        self._grid = bool(enabled)
        for plot in self._plots.values():
            plot.showGrid(x=self._grid, y=self._grid, alpha=0.35)

    def set_manual_limits(self, limits):
        """``{'x': (lo, hi), <group>: (lo, hi)}``; missing keys auto-scale."""
        self._manual_limits = dict(limits or {})
        self._update_limits(force=True)

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_activation()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._sync_activation()

    def _sync_activation(self):
        if self.isVisible() and self._curves:
            if not self._timer.isActive():
                self._frame_index = 0
                self._timer.start()
                self._render_tick()
        elif self._timer.isActive():
            self._timer.stop()

    # ------------------------------------------------------------------ #
    #  Layout                                                             #
    # ------------------------------------------------------------------ #
    def _rebuild(self):
        self._graphics.clear()
        self._plots = {}
        self._curves = {}

        groups = [
            group for group in GROUP_ORDER
            if any(GRAPH_METRICS[metric][1] == group
                   for _unit, metric in self._selected)
        ]
        if not groups:
            self._graphics.hide()
            self._placeholder.show()
            return
        self._placeholder.hide()
        self._graphics.show()
        self._build_legend()

        first = None
        for row, group in enumerate(groups, start=1):
            plot = self._graphics.addPlot(row=row, col=0)
            plot.setTitle(GRAPH_GROUP_LABELS[group], color=theme.TEXT_BRIGHT,
                          size=f'{theme.font_pt(10)}pt', justify='left')
            plot.showGrid(x=self._grid, y=self._grid, alpha=0.35)
            plot.getViewBox().setBackgroundColor(theme.BG_CARD)
            # Limits are driven by the shared domain helpers, so pyqtgraph's
            # own per-frame autorange is turned off.
            plot.disableAutoRange()
            for side in ('left', 'bottom'):
                axis = plot.getAxis(side)
                axis.setPen(pg.mkPen(theme.BORDER))
                axis.setTextPen(pg.mkPen(theme.TEXT_MUTED))
            if first is None:
                first = plot
            else:
                plot.setXLink(first)

            units = []
            for unit, metric in self._selected:
                label, metric_group, unit_label, _key, style = (
                    GRAPH_METRICS[metric])
                if metric_group != group:
                    continue
                meta = self._history.unit_meta(unit)
                pen = pg.mkPen(
                    color=meta['color'], width=self._pen_width,
                    style=_PEN_STYLES.get(style, Qt.PenStyle.SolidLine))
                pen.setCosmetic(True)
                curve = plot.plot(
                    pen=pen, name=f"{meta['label']} — {label}")
                if self._downsample:
                    # Peak downsampling keeps spikes visible while cutting the
                    # segment count Qt rasterises on long histories.
                    curve.setDownsampling(auto=True, method='peak')
                if self._clip_to_view:
                    curve.setClipToView(True)
                self._curves[(unit, metric)] = curve
                units.append(unit_label)
            plot.setLabel('left', units[0] if len(set(units)) == 1 else '',
                          color=theme.TEXT_MUTED, size=f'{theme.font_pt(8)}pt')
            self._plots[group] = plot

        # Only the bottom plot carries the shared time axis label.
        self._plots[groups[-1]].setLabel(
            'bottom', 'Time (s)', color=theme.TEXT_MUTED,
            size=f'{theme.font_pt(8)}pt')

        # The plots are new, so their view ranges are pyqtgraph's default 0-1
        # and have nothing to do with the data.  Scale on the very next frame
        # rather than at the next periodic check, or a fresh selection spends
        # the best part of a second showing traces off the top of the axes.
        self._needs_limits = True

    def _build_legend(self):
        """One key above the plots: colour is the controller, dash the metric.

        A legend inside each subplot repeated the same controllers once per
        axis and sat on top of the traces while doing it.  Splitting the two
        encodings apart says the same thing in a dozen entries instead of
        fifty, and leaves the data unobscured.
        """
        legend = pg.LegendItem(offset=None, horSpacing=16, verSpacing=-4,
                               labelTextColor=theme.TEXT_BRIGHT,
                               labelTextSize=f'{theme.font_pt(8)}pt',
                               brush=pg.mkBrush(theme.BG_CARD),
                               pen=pg.mkPen(theme.BORDER))

        entries = []
        for unit in dict.fromkeys(unit for unit, _metric in self._selected):
            meta = self._history.unit_meta(unit)
            entries.append((meta['color'], Qt.PenStyle.SolidLine,
                            meta['label']))
        metrics = list(dict.fromkeys(
            metric for _unit, metric in self._selected))
        if len(metrics) > 1:
            # With one metric the dash carries no information, and a key for
            # it would be a line explaining itself.
            for metric in metrics:
                label, _group, _unit, _key, style = GRAPH_METRICS[metric]
                entries.append((theme.TEXT_MUTED,
                                _PEN_STYLES.get(style, Qt.PenStyle.SolidLine),
                                label))

        for color, style, text in entries:
            pen = pg.mkPen(color=color, width=self._pen_width, style=style)
            pen.setCosmetic(True)
            legend.addItem(pg.PlotDataItem(pen=pen), text)

        # pyqtgraph fills a legend column by column, so asking for exactly
        # half the entries per column gives two rows with the controllers in
        # the left columns and the dash key in the right ones, rather than the
        # two kinds of entry interleaved.
        columns = min(8, max(1, (len(entries) + 1) // 2))
        legend.setColumnCount(columns)
        rows = -(-len(entries) // columns)
        self._graphics.addItem(legend, row=0, col=0)
        self._graphics.ci.layout.setRowFixedHeight(
            0, rows * (theme.scale(15)) + 12)

    # ------------------------------------------------------------------ #
    #  Rendering                                                          #
    # ------------------------------------------------------------------ #
    def _render_tick(self):
        try:
            self.render_frame()
        except Exception:
            # A rendering fault must never stop control or acquisition.
            pass

    def render_frame(self):
        """Push the newest history into the curves and rescale if needed."""
        if not self._curves:
            return
        times = np.fromiter(
            self._history.times, dtype=float, count=len(self._history.times))
        empty = times[:0]
        for (unit, metric), curve in self._curves.items():
            values = self._history.values(unit, metric)
            if not values:
                curve.setData(empty, empty)
                continue
            # Missing readings become NaN so the trace breaks instead of
            # interpolating across a gap; 'finite' makes Qt honour that.
            ys = np.fromiter(
                (np.nan if value is None else value for value in values),
                dtype=float, count=len(values))
            count = min(len(ys), len(times))
            if count == 0:
                curve.setData(empty, empty)
                continue
            xs, ys = times[-count:], ys[-count:]
            # connect='finite' makes Qt build a per-point connection array,
            # which costs far more than the isfinite scan that detects whether
            # it is needed at all.  Clean data -- the overwhelmingly common
            # case -- therefore draws as one polyline.
            if self._gap_aware and not np.isfinite(ys).all():
                curve.setData(xs, ys, connect='finite')
            else:
                curve.setData(xs, ys)
        self._frame_index += 1
        if self._needs_limits:
            self._update_limits(force=True)
            self._needs_limits = False
        elif self._frame_index % self.LIMIT_CHECK_FRAMES == 0:
            self._update_limits()

    def _group_bounds(self, group):
        low = high = None
        for (_unit, metric), curve in self._curves.items():
            if GRAPH_METRICS[metric][1] != group:
                continue
            _xs, ys = curve.getData()
            if ys is None or len(ys) == 0:
                continue
            finite = ys[np.isfinite(ys)]
            if finite.size == 0:
                continue
            value_low, value_high = float(finite.min()), float(finite.max())
            low = value_low if low is None else min(low, value_low)
            high = value_high if high is None else max(high, value_high)
        return low, high

    def _update_limits(self, force=False):
        """Apply manual limits, or auto-scale only when it is worth it.

        Pyqtgraph repaints on any range change, so hysteresis avoids needless
        full repaints.
        """
        if not self._plots:
            return
        x_limits = self._manual_limits.get('x')
        times = self._history.times
        for group, plot in self._plots.items():
            box = plot.getViewBox()
            y_limits = self._manual_limits.get(group)
            if y_limits is not None:
                plot.setYRange(*y_limits, padding=0)
            else:
                low, high = self._group_bounds(group)
                current = tuple(box.viewRange()[1])
                if low is not None and (force or should_rescale(
                        current, low, high)):
                    plot.setYRange(*padded_limits(low, high), padding=0)
            if x_limits is not None:
                plot.setXRange(*x_limits, padding=0)
            elif len(times):
                first, last = times[0], times[-1]
                current = tuple(box.viewRange()[0])
                if force or should_rescale(current, first, last):
                    plot.setXRange(
                        *padded_limits(first, last, pad=0.02), padding=0)
