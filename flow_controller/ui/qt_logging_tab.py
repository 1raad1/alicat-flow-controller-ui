"""Select, plot and export retained acquisition history.

Acquisition continues while the tab is hidden. Graphs render only when visible
and selected. Exports include every retained series and run on a session-owned
worker, which survives replacement of the widgets during a theme change.
"""

from __future__ import annotations

from datetime import datetime
from importlib.util import find_spec
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QGridLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from ..domain.graphing import parse_axis_limits
from ..core.session import DEFAULT_LOG_DIR
from . import qt_theme as theme
from .qt_graph_panel import GRAPH_METRICS, QtGraphPanel
from .qt_motion_panels import MotionSplitter
from .qt_widgets import Card, divider, label, row

#: ``(key, caption, default minimum, default maximum)`` for every axis the
#: operator can pin.  ``x`` is the shared time axis; the rest are the metric
#: groups, which is what the panel stacks its plots by.
AXES = (
    ('x', 'Time (s)', '0', '60'),
    ('flow', 'Flow / SP', '0', '10'),
    ('pressure', 'Pressure', '0', '30'),
    ('temperature', 'Temperature', '0', '100'),
    ('error', 'SP Error', '-1', '1'),
    ('valve', 'Valve Drive', '0', '100'),
)

#: The human name each axis is complained about by, so a rejected entry names
#: the row the operator is looking at rather than an internal key.
AXIS_NAMES = {'x': 'Time axis', 'flow': 'Flow axis', 'pressure': 'Pressure axis',
              'temperature': 'Temperature axis', 'error': 'Setpoint-error axis',
              'valve': 'Valve-drive axis'}

#: Metric keys in the order their columns appear, from the panel's own table.
METRIC_KEYS = tuple(GRAPH_METRICS)


def _wrapped(text, **kwargs):
    """Build a label that wraps within the control column."""
    widget = label(text, **kwargs)
    widget.setWordWrap(True)
    return widget


def _lighten(color, amount):
    """Blend a ``#rrggbb`` colour toward white by ``amount`` (0-1)."""
    amount = min(0.55, max(0.0, amount))
    value = color.lstrip('#')
    parts = [int(value[index:index + 2], 16) for index in (0, 2, 4)]
    parts = [int(part + (255 - part) * amount) for part in parts]
    return '#%02x%02x%02x' % tuple(parts)


def trace_colors(units, selection):
    """Use the gas colour, lightening subsequent units of the same gas."""
    seen = {}
    colors = {}
    for unit in units:
        gas, _zone = selection.get(unit, ('', ''))
        base = theme.GAS_COLORS.get(gas, theme.TEXT)
        count = seen.get(gas, 0)
        seen[gas] = count + 1
        colors[unit] = base if count == 0 else _lighten(base, 0.24 * count)
    return colors


class _HistoryView:
    """Adapt session history to the renderer without copying its deques."""

    def __init__(self, session):
        self._session = session
        self._meta = {}

    @property
    def times(self):
        return self._session.history.times()

    @property
    def revision(self):
        return self._session.history.revision

    def values(self, unit, metric):
        return self._session.history.raw(unit, metric)

    def set_meta(self, meta):
        self._meta = dict(meta)

    def unit_meta(self, unit):
        return self._meta.get(
            unit, {'label': f'Unit {unit}', 'color': theme.TEXT})


class LoggingTab(QWidget):
    """Series selection, axis limits, export -- and the plots themselves."""

    #: Anything the operator should read but that does not deserve a dialog:
    #: a rejected axis limit, an export that had nothing to write.  Events, so
    #: whatever shows them should let them go again.
    status = Signal(str)

    #: What the plot panel is doing, as a standing description rather than an
    #: event.  Worth carrying into the window chrome: whether anything is being
    #: plotted decides what this screen costs, and the operator is usually
    #: looking at one of the other two.
    graphs_status = Signal(str)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self._history_view = _HistoryView(session)
        # Ticked boxes outlive the grid that shows them.  The grid is rebuilt
        # whenever an assignment changes, and an operator who moves one
        # controller between zones has not asked to lose the six others'
        # selections.
        self._ticks = {}
        self._boxes = {}
        self._units = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._split = MotionSplitter(Qt.Orientation.Horizontal)
        self._split.addWidget(self._build_controls())
        self._split.addWidget(self._build_plots())
        self._split.configure_panel(0, 'Plot controls')
        self._split.setStretchFactor(0, 0)
        self._split.setStretchFactor(1, 1)
        self._split.set_default_sizes([430, 1130])
        panel_bar = QWidget()
        self.panel_bar = panel_bar
        panel_layout = QHBoxLayout(panel_bar)
        panel_layout.setContentsMargins(theme.PAD_LG, theme.PAD_XS,
                                        theme.PAD_LG, theme.PAD_XS)
        self.controls_panel_btn = QPushButton('Plot controls')
        self.controls_panel_btn.setObjectName('PanelToggle')
        self.controls_panel_btn.setCheckable(True)
        self.controls_panel_btn.setChecked(True)
        self.controls_panel_btn.setProperty('density', 'compact')
        self.controls_panel_btn.setToolTip('Fold the controls to give the plots more space')
        self.controls_panel_btn.toggled.connect(
            lambda shown: self._split.set_panel_collapsed(0, not shown))
        self._split.panelCollapsedChanged.connect(self._controls_collapsed)
        panel_layout.addWidget(self.controls_panel_btn)
        reset = QPushButton('Reset layout')
        reset.setProperty('density', 'compact')
        reset.clicked.connect(lambda: self._split.reset_layout())
        panel_layout.addWidget(reset)
        panel_layout.addStretch(1)
        outer.addWidget(panel_bar)
        outer.addWidget(self._split, 1)

        session.assignments_changed.connect(lambda _map: self._refresh_units())
        session.monitoring_changed.connect(lambda _on: self._update_status())
        session.history_export.active_changed.connect(self._export_active_changed)
        session.history_export.completed.connect(self._export_finished)
        session.history_export.failed.connect(self._export_failed)
        session.history_export.cancelled.connect(self._export_cancelled)
        self._export_active_changed(session.history_export.active)
        self._refresh_units()

    def _controls_collapsed(self, index, collapsed):
        if index == 0:
            self.controls_panel_btn.blockSignals(True)
            self.controls_panel_btn.setChecked(not collapsed)
            self.controls_panel_btn.blockSignals(False)

    # ------------------------------------------------------------------ #
    #  Left column                                                        #
    # ------------------------------------------------------------------ #
    def _build_controls(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Otherwise the viewport fills opaquely and masks the backdrop the
        # cards are supposed to be floating over.
        scroll.viewport().setAutoFillBackground(False)

        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(theme.PAD_LG, theme.PAD_LG,
                                  theme.PAD_SM + 2, theme.PAD_LG)
        column.setSpacing(theme.CARD_GAP)
        column.addWidget(self._card_series())
        column.addWidget(self._card_axes())
        column.addWidget(self._card_history())
        column.addStretch(1)
        scroll.setWidget(holder)
        self._controls = scroll
        return scroll

    def _fit_controls(self):
        """Give the control column the width its cards actually need.

        The width cannot be settled while the column is being built.  The
        checkbox grid is filled in from the assignment afterwards, and the
        stylesheet that decides the fonts is applied by the window later
        still, so a hint measured here describes an empty card in the wrong
        font -- which is how the last metric column and the MAXIMUM field end
        up past the edge.  It is measured again once there is something real
        to measure, and only ever grows: a column that shrank back while the
        operator was ticking boxes would be worse than one that is wide.
        """
        needed = self._controls.widget().sizeHint().width() + 26
        if needed <= self._controls.minimumWidth():
            return
        self._controls.setMinimumWidth(needed)
        rest = max(360, self._split.width() - needed - self._split.handleWidth())
        self._split.setSizes([needed, rest])

    # -- displayed series ------------------------------------------------ #
    def _card_series(self):
        card = Card(
            'Displayed Series', index=1,
            help_text=('Choose which controllers and measurements appear on '
                       'the live plots. History collection and CSV logging are '
                       'unaffected; nothing here changes what is recorded.'))

        self._grid_holder = QWidget()
        self._grid = QGridLayout(self._grid_holder)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(theme.PAD_SM)
        self._grid.setVerticalSpacing(theme.PAD_XS)
        card.add(self._grid_holder)

        card.add_spacing(theme.PAD_SM)
        card.add(divider())
        card.add_spacing(theme.PAD_SM)

        presets = []
        for text, keys in (('Flow + SP', {'flow', 'sp'}),
                           ('All Telemetry', set(METRIC_KEYS)),
                           ('None', set())):
            button = QPushButton(text)
            button.setProperty('density', 'compact')
            button.setProperty('variant', 'quiet')
            button.clicked.connect(
                lambda _checked=False, chosen=keys: self._apply_preset(chosen))
            presets.append(button)
        card.add(row(*presets, None))
        return card

    def _refresh_units(self):
        """Rebuild the unit rows to match the current assignment."""
        self._units = list(self.session.assigned_units())
        colors = trace_colors(self._units, self.session.selection)
        self._history_view.set_meta({
            unit: {'label': f"{self.session.selection.get(unit, ('', ''))[0]} "
                            f"U{unit}".strip(),
                   'color': colors[unit]}
            for unit in self._units})

        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Unparent before scheduling the delete.  deleteLater only
                # takes effect when the event loop next gets round to it, and
                # until then a widget that is merely out of the layout is
                # still a child with its old geometry: it keeps painting over
                # the row that replaced it, and still counts towards the
                # column's size hint.  Old ticks appearing on a freshly
                # reassigned grid is exactly that.
                widget.setParent(None)
                widget.deleteLater()
        self._boxes = {}

        if not self._units:
            self._grid.addWidget(
                _wrapped('Assign controllers on the Connection tab, then '
                         'choose series to plot.', color=theme.TEXT_DIM,
                         size=8), 0, 0, 1, len(METRIC_KEYS) + 1)
            self._push_selection()
            self._fit_controls()
            return

        self._grid.addWidget(label('CONTROLLER', color=theme.TEXT_DIM, size=7,
                                   bold=True), 0, 0)
        for column, key in enumerate(METRIC_KEYS, start=1):
            caption = label(GRAPH_METRICS[key][0], color=theme.TEXT_DIM,
                            size=7, bold=True)
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._grid.addWidget(caption, 0, column)

        for line, unit in enumerate(self._units, start=1):
            gas, zone = self.session.selection.get(unit, ('', ''))
            caption = label(f"{gas}  U{unit}", color=colors[unit], size=8,
                            bold=True, monospace=True)
            caption.setToolTip(f"Unit {unit} — {gas}, {zone}")
            self._grid.addWidget(caption, line, 0)
            for column, key in enumerate(METRIC_KEYS, start=1):
                box = QCheckBox()
                box.setChecked(self._ticks.get((unit, key), False))
                box.setToolTip(f"{GRAPH_METRICS[key][0]} — {gas} U{unit}")
                box.toggled.connect(
                    lambda checked, u=unit, k=key: self._on_tick(u, k, checked))
                self._grid.addWidget(box, line, column,
                                     Qt.AlignmentFlag.AlignCenter)
                self._boxes[(unit, key)] = box
        self._push_selection()
        self._fit_controls()

    def _on_tick(self, unit, key, checked):
        self._ticks[(unit, key)] = bool(checked)
        self._push_selection()

    def _apply_preset(self, keys):
        for (unit, key), box in self._boxes.items():
            # blockSignals rather than a guard flag: without it a preset over
            # eight controllers rebuilds the plot layout forty-eight times.
            box.blockSignals(True)
            box.setChecked(key in keys)
            box.blockSignals(False)
            self._ticks[(unit, key)] = key in keys
        self._push_selection()

    def _selection(self):
        """The ticked series, in the order the plots should stack them."""
        return [(unit, key) for unit in self._units for key in METRIC_KEYS
                if self._ticks.get((unit, key))]

    def _push_selection(self):
        self.graph.set_selection(self._selection())
        self._update_status()

    # -- axis limits ----------------------------------------------------- #
    def _card_axes(self):
        card = Card(
            'Axis Limits', index=2,
            help_text=('Leave an axis on Auto to follow its data, or untick '
                       'Auto and enter fixed minimum and maximum values. Reset '
                       'Auto restores automatic scaling for every axis.'))
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(theme.PAD_SM)
        grid.setVerticalSpacing(theme.PAD_XS)
        for column, caption in enumerate(('AXIS', 'AUTO', 'MINIMUM',
                                          'MAXIMUM')):
            grid.addWidget(label(caption, color=theme.TEXT_DIM, size=7,
                                 bold=True), 0, column)

        self._axis_rows = {}
        for line, (key, caption, low, high) in enumerate(AXES, start=1):
            grid.addWidget(label(caption, size=8), line, 0)
            auto = QCheckBox()
            auto.setChecked(True)
            grid.addWidget(auto, line, 1, Qt.AlignmentFlag.AlignCenter)
            fields = []
            for column, default in ((2, low), (3, high)):
                entry = QLineEdit(default)
                entry.setFixedWidth(theme.scale(66))
                entry.setEnabled(False)
                grid.addWidget(entry, line, column)
                fields.append(entry)
            auto.toggled.connect(
                lambda checked, pair=fields: [field.setEnabled(not checked)
                                              for field in pair])
            self._axis_rows[key] = (auto, fields[0], fields[1])
        card.add_layout(grid)

        card.add_spacing(theme.PAD_SM)
        self._grid_box = QCheckBox('Grid')
        self._grid_box.setChecked(True)
        self._grid_box.toggled.connect(lambda on: self.graph.set_grid(on))
        apply_btn = QPushButton('Apply Axes')
        apply_btn.setProperty('variant', 'accent')
        apply_btn.setProperty('density', 'compact')
        apply_btn.clicked.connect(lambda: self._apply_axes())
        reset_btn = QPushButton('Reset Auto')
        reset_btn.setProperty('variant', 'quiet')
        reset_btn.setProperty('density', 'compact')
        reset_btn.clicked.connect(self._reset_axes)
        card.add(row(self._grid_box, None, reset_btn, apply_btn))

        self._axis_error = _wrapped('', color=theme.DANGER_HOVER, size=8)
        self._axis_error.setVisible(False)
        card.add(self._axis_error)
        return card

    def _apply_axes(self, announce=True):
        """Read the six rows, or refuse the first one that does not parse."""
        limits = {}
        for key, (auto, low, high) in self._axis_rows.items():
            try:
                pinned = parse_axis_limits(auto.isChecked(), low.text(),
                                           high.text(), AXIS_NAMES[key])
            except ValueError as exc:
                # One bad row rejects the lot.  Applying the rows that did
                # parse would leave the screen showing a mixture of what was
                # asked for and what was there before, with nothing saying
                # which axis is which.
                if announce:
                    self._show_axis_error(str(exc))
                return False
            if pinned is not None:
                limits[key] = pinned
        self._show_axis_error('')
        self.graph.set_manual_limits(limits)
        return True

    def _show_axis_error(self, message):
        self._axis_error.setText(message)
        self._axis_error.setVisible(bool(message))
        if message:
            self.status.emit(message)

    def _reset_axes(self):
        for auto, low, high in self._axis_rows.values():
            auto.setChecked(True)
        self._grid_box.setChecked(True)
        self._apply_axes(announce=False)

    # -- history and export ---------------------------------------------- #
    def _card_history(self):
        card = Card(
            'History & Export', index=3,
            help_text=('Set how many monitoring samples remain in memory, '
                       'export every stored sample for every assigned '
                       'controller, or clear the retained history. Export is '
                       'not limited to the currently displayed series.'))
        self._limit_entry = QLineEdit(str(self.session.history.limit))
        self._limit_entry.setFixedWidth(theme.scale(66))
        limit_btn = QPushButton('Apply')
        limit_btn.setProperty('variant', 'quiet')
        limit_btn.setProperty('density', 'compact')
        limit_btn.clicked.connect(lambda: self._apply_limit())
        caption = label('History (samples)', size=8, color=theme.TEXT_MUTED)
        card.add(row(caption, None, self._limit_entry, limit_btn))
        card.add_spacing(theme.PAD_SM)

        export = QPushButton('Export Data…')
        self._export_button = export
        export.setProperty('density', 'compact')
        export.clicked.connect(lambda: self._export())
        self._cancel_export_button = QPushButton('Cancel export')
        self._cancel_export_button.setProperty('variant', 'quiet')
        self._cancel_export_button.clicked.connect(self._cancel_export)
        clear = QPushButton('Clear History')
        clear.setProperty('variant', 'quiet')
        clear.setProperty('density', 'compact')
        clear.clicked.connect(lambda: self._clear())
        card.add(row(export, self._cancel_export_button, clear, None))
        return card

    def _apply_limit(self):
        try:
            samples = int(float(self._limit_entry.text()))
        except (TypeError, ValueError):
            self.status.emit('History length must be a whole number of '
                             'samples.')
            self._limit_entry.setText(str(self.session.history.limit))
            return
        applied = self.session.set_history_limit(samples)
        self._limit_entry.setText(str(applied))

    def _clear(self):
        self.session.clear_history()
        self.graph.render_frame()
        self.status.emit('Graph history cleared.')

    def _export(self):
        if self.session.history_export.active:
            return
        if not self.session.history.times():
            self.status.emit('Nothing to export yet. No samples collected.')
            return
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        suggested = DEFAULT_LOG_DIR / f'alicat_export_{stamp}.csv'
        filters = 'CSV (*.csv)'
        if find_spec('openpyxl') is not None:
            filters += ';;Excel workbook (*.xlsx)'
        path, chosen = QFileDialog.getSaveFileName(
            self, 'Export graph history', str(suggested), filters)
        if not path:
            return
        path = Path(path)
        if not path.suffix:
            path = path.with_suffix('.xlsx' if '*.xlsx' in chosen else '.csv')
        try:
            snapshot = self.session.history.export_snapshot()
            if not snapshot.times:
                self.status.emit('Nothing to export. History was cleared.')
                return
            started = self.session.history_export.start(snapshot, path)
        except Exception as exc:
            self.status.emit(f'Export failed: {exc}')
            return
        if started:
            self.status.emit(f'Exporting {len(snapshot.times)} samples to {path}…')

    def _export_active_changed(self, active):
        self._export_button.setEnabled(not active)
        self._export_button.setText('Exporting…' if active else 'Export Data…')
        self._cancel_export_button.setVisible(active)
        self._cancel_export_button.setEnabled(active)
        self._cancel_export_button.setText('Cancel export')

    def _cancel_export(self):
        if self.session.history_export.cancel():
            self._cancel_export_button.setEnabled(False)
            self._cancel_export_button.setText('Cancelling…')

    def _export_finished(self, path, count):
        self.status.emit(f'Exported {count} samples to {path}')

    def _export_failed(self, message):
        self.status.emit(f'Export failed: {message}')

    def _export_cancelled(self):
        self.status.emit('History export cancelled.')

    # ------------------------------------------------------------------ #
    #  Right column                                                       #
    # ------------------------------------------------------------------ #
    def _build_plots(self):
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(theme.PAD_SM + 2, theme.PAD_LG,
                                  theme.PAD_LG, theme.PAD_LG)
        column.setSpacing(theme.PAD_SM)

        self._status = label('Waiting for monitoring', color=theme.TEXT_DIM,
                             size=8, monospace=True)
        column.addWidget(row(label('LIVE TRENDS', color=theme.TEXT_DIM, size=7,
                                   bold=True), None, self._status))

        self.graph = QtGraphPanel(self._history_view)
        column.addWidget(self.graph, 1)
        return holder

    def graphs_text(self):
        """The standing description, for chrome built after this tab was."""
        return self._status.text()

    def _set_graphs_status(self, text):
        if text == self._status.text():
            return
        self._status.setText(text)
        self.graphs_status.emit(text)

    def _update_status(self):
        selection = self._selection()
        if not selection:
            self._set_graphs_status(
                'No series selected' if self.session.is_monitoring
                else 'Waiting for monitoring')
            return
        groups = {GRAPH_METRICS[key][1] for _unit, key in selection}
        if not self.graph.isVisible():
            # Said plainly rather than left blank: a plot that has stopped
            # updating because the tab is hidden looks identical to one that
            # has stopped because acquisition died.
            self._set_graphs_status(
                f'{len(selection)} series · paused (tab hidden)')
        else:
            self._set_graphs_status(
                f'{len(selection)} series · {len(groups)} '
                f"{'axis' if len(groups) == 1 else 'axes'}")

    def showEvent(self, event):
        super().showEvent(event)
        # First sight of the real fonts: the stylesheet is applied by the
        # window, so this is the earliest the cards can be measured honestly.
        self._fit_controls()
        self._update_status()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._update_status()
