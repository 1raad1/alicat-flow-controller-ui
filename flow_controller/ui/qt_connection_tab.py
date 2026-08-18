"""Connection & Assignment — the screen that turns a COM port into a rig.

Four steps, in the order an operator actually performs them: pick the port,
find out what is on it, say what each controller *is*, then open the
connection.  The ordering is the whole point of the screen — a controller
cannot be assigned before it is found, and nothing may be connected before it
is assigned, because an unassigned unit is a gas nobody has named.

Everything here is view.  No serial call, no state rule and no safety
interlock lives in this file: the tab renders :class:`FlowSession` signals and
calls its methods, and the session refuses anything it should refuse.  That is
why, for instance, the Connect button does not check whether monitoring is
running — it asks, and the session says no.

The one piece of judgement the view *does* own is the auto-calculation
prompt.  ``connect_all`` returns the sentinel ``'needs_confirmation'`` rather
than deciding for the operator whether losing automatic RQL targets is
acceptable; asking is a presentation question, so it is answered here.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QVBoxLayout, QWidget,
)

from ..core.session import SCAN_UNITS
from ..domain import roles, rql
from . import qt_theme as theme
from .qt_widgets import (
    Card, FlowBar, StageHeader, StatusDot, label, mono, paint_glass,
)

#: Sentinel row at the foot of every gas-type combo.
ADD_GAS = "+ Add new gas…"

#: Assignment table columns: (heading, design-time width in px).  A width of
#: ``None`` means "take the remaining space".
COLUMNS = (
    ("", 24),
    ("Unit", 66),
    ("Gas (scan)", 78),
    ("Flow (scan)", 92),
    ("Gas Type", 128),
    ("Zone", 118),
)

#: Width the left column is given at startup.  It is derived from the columns
#: rather than guessed, because a splitter narrower than the assignment row
#: clips the zone selector — the one control an operator must not miss.
STEPS_WIDTH = sum(width for _heading, width in COLUMNS) + 132

#: Zone -> accent, so the live column reads the same way the rig is labelled.
ZONE_COLORS = {
    "Zone 1": "#7dd3fc",
    "Zone 2": "#4ade80",
    "Pilot": "#fbbf24",
    "General": "#94a3b8",
}

LOG_LIMIT = 400


def _config_label(config):
    """The auto-calc configuration, in the words used on the Operation tab."""
    if config == rql.FULL_RQL:
        return 'Full RQL'
    if config == rql.RICH_QUENCH:
        return 'Rich + quench air'
    return str(config)


def _timestamped(message):
    return f"{datetime.now().strftime('%H:%M:%S')}  {message}"


def _log_view(height):
    view = QPlainTextEdit()
    view.setReadOnly(True)
    view.setMaximumBlockCount(LOG_LIMIT)
    view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    view.setFixedHeight(theme.scale(height))
    view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return view


def _scrolled(widget):
    """Wrap a column so a short window scrolls it instead of crushing it.

    Qt box layouts take height from whatever allows it, and a card that has
    been squeezed clips its own text rather than reporting that it did.  Every
    tall column in this UI therefore lives in a scroll area with a transparent
    viewport — opaque by default, which would punch a hole in the glass.
    """
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.viewport().setAutoFillBackground(False)
    widget.setAutoFillBackground(False)
    area.setWidget(widget)
    return area


# ---------------------------------------------------------------------- #
#  Assignment row                                                         #
# ---------------------------------------------------------------------- #
class AssignRow(QFrame):
    """One detected controller, and what the operator says it is.

    Inclusion is a real checkbox rather than the Tk version's click-anywhere
    row toggle.  The row still toggles on click, but an operator should not
    have to be told by a hint that it does — and there was no way to see, from
    a greyed row alone, whether it was excluded or simply unreachable.
    """

    changed = Signal()
    gas_requested = Signal(str)      # unit, when "+ Add new gas…" is chosen
    zone_changed = Signal(str, str)  # unit, zone — only while connected

    def __init__(self, controller, gas_options, parent=None):
        super().__init__(parent)
        self.unit = controller.unit
        self._hover = False
        self._locked = False
        self._reverting = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)

        data = controller.data or {}
        scanned_gas = controller.active_gas
        flow = data.get('mass_flow') or 0.0

        row = QHBoxLayout(self)
        row.setContentsMargins(theme.PAD_SM, theme.PAD_SM - 1,
                               theme.PAD_SM, theme.PAD_SM - 1)
        row.setSpacing(theme.PAD_SM)

        self.include = QCheckBox()
        self.include.setChecked(True)
        self.include.setFixedWidth(theme.scale(COLUMNS[0][1]))
        self.include.toggled.connect(self._on_toggled)
        row.addWidget(self.include)

        self._unit_label = label(f"Unit {self.unit}", color='#7dd3fc', size=9,
                                 bold=True, monospace=True)
        self._unit_label.setFixedWidth(theme.scale(COLUMNS[1][1]))
        row.addWidget(self._unit_label)

        self._gas_label = label(scanned_gas, color='#4ade80', size=9,
                                monospace=True)
        self._gas_label.setFixedWidth(theme.scale(COLUMNS[2][1]))
        row.addWidget(self._gas_label)

        self._flow_label = label(f"{flow:.2f} SLPM", color=theme.TEXT_MUTED,
                                 size=8, monospace=True)
        self._flow_label.setFixedWidth(theme.scale(COLUMNS[3][1]))
        row.addWidget(self._flow_label)

        self.gas_combo = QComboBox()
        self.gas_combo.setFixedWidth(theme.scale(COLUMNS[4][1]))
        self.set_gas_options(gas_options)
        # The scanned gas is the best available guess at what this controller
        # is plumbed to, so it is pre-selected — but only when the device
        # actually answered with one.
        if scanned_gas.casefold() != 'unknown':
            index = self.gas_combo.findText(scanned_gas)
            if index >= 0:
                self.gas_combo.setCurrentIndex(index)
        self._last_gas = self.gas_combo.currentText()
        self.gas_combo.textActivated.connect(self._on_gas_activated)
        row.addWidget(self.gas_combo)

        self.zone_combo = QComboBox()
        self.zone_combo.setFixedWidth(theme.scale(COLUMNS[5][1]))
        self.zone_combo.addItems(list(roles.ZONE_OPTIONS))
        self.zone_combo.setCurrentText('General')
        self.zone_combo.currentTextChanged.connect(self._on_zone_changed)
        row.addWidget(self.zone_combo)
        row.addStretch(1)

    # -- state ----------------------------------------------------------
    @property
    def included(self):
        return self.include.isChecked()

    def set_included(self, included):
        self.include.setChecked(bool(included))

    def selection(self):
        """``(gas, zone)`` as the session wants it."""
        return self.gas_combo.currentText(), self.zone_combo.currentText()

    def gas(self):
        return self.gas_combo.currentText()

    def zone(self):
        return self.zone_combo.currentText()

    def set_gas_options(self, options):
        """Rebuild the gas list, keeping whatever was already chosen."""
        current = self.gas_combo.currentText()
        blocked = self.gas_combo.blockSignals(True)
        self.gas_combo.clear()
        self.gas_combo.addItem(roles.UNSELECTED_GAS)
        self.gas_combo.addItems(list(options))
        self.gas_combo.addItem(ADD_GAS)
        index = self.gas_combo.findText(current)
        self.gas_combo.setCurrentIndex(max(index, 0))
        self.gas_combo.blockSignals(blocked)
        self._last_gas = self.gas_combo.currentText()

    def choose_gas(self, name):
        index = self.gas_combo.findText(name)
        if index >= 0:
            self.gas_combo.setCurrentIndex(index)
        self._last_gas = self.gas_combo.currentText()
        self.changed.emit()

    def restore_gas(self):
        """Undo the transient selection of the ``+ Add new gas…`` sentinel."""
        index = self.gas_combo.findText(self._last_gas)
        self.gas_combo.setCurrentIndex(max(index, 0))

    def set_locked(self, locked):
        """Freeze what the hardware holds; leave the grouping editable.

        A gas is programmed into the controller, so changing it after
        connecting would leave the screen claiming one calibration while the
        device holds another — that stays frozen.  A zone is only how this
        application groups the readings, and moving a controller between
        zones should not cost a disconnect and a reconnect, so it does not.

        The one zone that is *not* offered while connected is
        ``-- unassigned --``: that would drop the controller out of the
        assignment while the monitor loop still has it open and polling.
        """
        self._locked = bool(locked)
        self.include.setEnabled(not locked)
        self.gas_combo.setEnabled(not locked)
        self._offer_unassigned(not self._locked)
        self.zone_combo.setEnabled(self._zone_editable())
        self.setCursor(Qt.CursorShape.ArrowCursor if locked
                       else Qt.CursorShape.PointingHandCursor)
        self.update()

    def _zone_editable(self):
        """Editable when free, or when connected and actually being polled."""
        if not self._locked:
            return True
        return self.included and self.zone() != roles.UNASSIGNED_ZONE

    def _offer_unassigned(self, offer):
        """Add or withdraw the ``-- unassigned --`` entry without side effects."""
        index = self.zone_combo.findText(roles.UNASSIGNED_ZONE)
        if offer == (index >= 0):
            return
        if not offer and self.zone() == roles.UNASSIGNED_ZONE:
            # Removing the entry that is currently showing would silently move
            # the row to another zone.  A row sitting on unassigned is not a
            # connected controller, so leaving it is both safe and honest.
            return
        self._reverting = True
        try:
            if offer:
                self.zone_combo.insertItem(0, roles.UNASSIGNED_ZONE)
            else:
                self.zone_combo.removeItem(index)
        finally:
            self._reverting = False

    def _on_zone_changed(self, text):
        """While connected a zone change is a live edit, not a re-selection."""
        if self._reverting:
            return
        if self._locked:
            self.zone_changed.emit(self.unit, text)
        else:
            self.changed.emit()

    def apply_selection(self, gas, zone, *, included=True):
        """Put the row into a state that was already chosen, silently.

        For a screen that is recovering an assignment the session still holds:
        nothing here is a new decision by the operator, so nothing here should
        announce one.
        """
        blocked = self.blockSignals(True)
        self._reverting = True
        try:
            self.include.setChecked(bool(included))
            index = self.gas_combo.findText(gas)
            if index >= 0:
                self.gas_combo.setCurrentIndex(index)
            self._last_gas = self.gas_combo.currentText()
            self.zone_combo.setCurrentText(zone)
        finally:
            self._reverting = False
            self.blockSignals(blocked)
        # Blocked signals stop the row telling anyone; they do not repaint it
        # into the state it was just put in.
        self._paint_state()

    def revert_zone(self, zone):
        """Put the combo back after the session refused the change."""
        self._reverting = True
        try:
            self.zone_combo.setCurrentText(zone)
        finally:
            self._reverting = False

    # -- interaction ----------------------------------------------------
    def _on_toggled(self, _checked):
        self._paint_state()
        self.changed.emit()

    def _on_gas_activated(self, text):
        if text == ADD_GAS:
            self.restore_gas()
            self.gas_requested.emit(self.unit)
            return
        self._last_gas = text
        self.changed.emit()

    def mousePressEvent(self, event):
        # Only clicks on the row itself land here; the combos are children and
        # consume their own, so this cannot toggle inclusion by accident.
        if not self._locked and event.button() == Qt.MouseButton.LeftButton:
            self.include.toggle()
        super().mousePressEvent(event)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def _paint_state(self):
        dimmed = not self.included
        self._unit_label.setStyleSheet(
            f"color: {theme.TEXT_DIM if dimmed else '#7dd3fc'};"
            " background: transparent;")
        self._gas_label.setStyleSheet(
            f"color: {theme.TEXT_DIM if dimmed else '#4ade80'};"
            " background: transparent;")
        self._flow_label.setStyleSheet(
            f"color: {theme.TEXT_DIM if dimmed else theme.TEXT_MUTED};"
            " background: transparent;")
        self.gas_combo.setEnabled(self.included and not self._locked)
        self.zone_combo.setEnabled(self.included and self._zone_editable())
        self.update()

    def paintEvent(self, _event):
        if not self.included:
            # Excluded rows are barely there: present, so the operator can see
            # the unit was found, but plainly not part of the run.
            paint_glass(self, radius=theme.RADIUS_TILE,
                        tint=(255, 255, 255, 5), rim=(255, 255, 255, 12),
                        rim_high=(255, 255, 255, 16), sheen=False)
            return
        rim = (255, 255, 255, 46) if self._hover else theme.GLASS_RIM
        paint_glass(self, radius=theme.RADIUS_TILE,
                    tint=theme.GLASS_TINT_SOFT, rim=rim)


# ---------------------------------------------------------------------- #
#  Live readout row                                                       #
# ---------------------------------------------------------------------- #
class LiveRow(QFrame):
    """One controller's live reading, read-only.

    Deliberately *not* a :class:`UnitCard`: this column exists so an operator
    can confirm that the connection they just made is producing numbers.
    Commanding flow belongs on the Operation tab, where the abort controls
    are, and a setpoint box here would be a way to open a valve from a screen
    that has none.
    """

    def __init__(self, unit, gas, color, parent=None):
        super().__init__(parent)
        self._span = 1.0
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(theme.PAD_MD, theme.PAD_SM + 1,
                                 theme.PAD_MD, theme.PAD_SM + 1)
        outer.setSpacing(theme.PAD_XS + 1)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(theme.PAD_SM)
        identity = label(f"{gas}", color=color, size=9, bold=True)
        identity.setFixedWidth(theme.scale(64))
        top.addWidget(identity)
        top.addWidget(label(f"Unit {unit}", color=theme.TEXT_DIM, size=8,
                            monospace=True))
        top.addStretch(1)

        self._flow = label('   --  ', color=theme.TEXT_BRIGHT, size=13,
                           bold=True, monospace=True)
        top.addWidget(self._flow)
        top.addWidget(label('SLPM', color=theme.TEXT_DIM, size=7))
        self._dot = StatusDot(theme.TEXT_DIM, diameter=7)
        top.addWidget(self._dot)
        outer.addLayout(top)

        self._bar = FlowBar(color)
        outer.addWidget(self._bar)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(theme.PAD_LG)
        self._readings = {}
        for key, caption, suffix in (('sp', 'SP', 'SLPM'),
                                     ('press', 'P', 'psia'),
                                     ('temp', 'T', '°C')):
            bottom.addWidget(label(caption, color=theme.TEXT_DIM, size=7))
            value = label('--', color=theme.TEXT_MUTED, size=8, monospace=True)
            bottom.addWidget(value)
            bottom.addWidget(label(suffix, color=theme.TEXT_DIM, size=7))
            self._readings[key] = value
        bottom.addStretch(1)
        outer.addLayout(bottom)

    def paintEvent(self, _event):
        paint_glass(self, radius=theme.RADIUS_TILE, tint=theme.GLASS_TINT_SOFT)

    def clear(self):
        self._flow.setText('   --  ')
        for value in self._readings.values():
            value.setText('--')
        self._bar.set_state(0.0, 0.0, self._span)
        self._dot.set_color(theme.TEXT_DIM)

    def update_sample(self, sample):
        """Render one pass.  A missing field stays blank rather than stale."""
        flow = sample.get('flow')
        setpoint = sample.get('sp')
        if flow is None:
            self._flow.setText('   --  ')
            self._dot.set_color(theme.WARN)
        else:
            self._flow.setText(f"{flow:7.3f}")
            self._dot.set_color(theme.OK)
        for key in ('sp', 'press', 'temp'):
            value = sample.get(key)
            self._readings[key].setText('--' if value is None
                                        else f"{value:.3f}")
        # The full scale of each controller is not known here, so the track
        # grows to the largest value this unit has actually shown.
        self._span = max(self._span, float(flow or 0.0),
                         float(setpoint or 0.0), 1.0)
        self._bar.set_state(flow or 0.0, setpoint or 0.0, self._span)


# ---------------------------------------------------------------------- #
#  Gas table browser                                                      #
# ---------------------------------------------------------------------- #
class GasTableDialog(QDialog):
    """Browse a controller's own gas table and pick a name from it.

    The table comes from the scan, which reads it from every unit it finds, so
    opening this costs nothing and works while the monitor is running.  The
    Refresh button re-reads the device for the case where the table has been
    reprogrammed since — and that one *is* refused mid-monitor, by the
    session, because it opens the port for itself.
    """

    def __init__(self, session, unit, gases, parent=None):
        super().__init__(parent)
        self._session = session
        self._unit = unit
        self._gases = dict(gases or {})
        self._chosen = None
        self.setWindowTitle(f"Unit {unit} — Controller Gas Table")
        self.setModal(True)
        self.resize(theme.scale(420), theme.scale(440))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(theme.CARD_PAD, theme.CARD_PAD,
                                 theme.CARD_PAD, theme.CARD_PAD)
        outer.setSpacing(theme.PAD_MD)

        outer.addWidget(label(f"Unit {unit} — Controller Gas Table",
                              color=theme.TEXT_BRIGHT, size=11, bold=True))
        self._status = label('', color=theme.TEXT_DIM, size=8)
        outer.addWidget(self._status)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText('Filter…')
        self._filter.textChanged.connect(self._apply_filter)
        outer.addWidget(self._filter)

        self._list = QListWidget()
        self._list.setFont(mono(9))
        self._list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setStyleSheet(
            f"QListWidget {{ background-color: rgba(0, 0, 0, 92);"
            f" border: 1px solid rgba(255, 255, 255, 22);"
            f" border-radius: {theme.RADIUS_CONTROL}px; padding: 4px;"
            f" color: {theme.TEXT}; outline: none; }}"
            f"QListWidget::item {{ padding: 4px 6px;"
            f" border-radius: {theme.RADIUS_INPUT}px; }}"
            f"QListWidget::item:selected {{ background-color: {theme.ACCENT};"
            f" color: {theme.ON_ACCENT}; }}")
        self._list.itemDoubleClicked.connect(lambda _item: self._accept())
        outer.addWidget(self._list, 1)

        self._manual = QLineEdit()
        self._manual.setPlaceholderText('Or type a gas name…')
        self._manual.returnPressed.connect(self._accept)
        outer.addWidget(self._manual)

        buttons = QHBoxLayout()
        buttons.setSpacing(theme.PAD_SM)
        refresh = QPushButton('Re-read device')
        refresh.setProperty('variant', 'quiet')
        refresh.clicked.connect(self._refresh)
        buttons.addWidget(refresh)
        buttons.addStretch(1)
        cancel = QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self._select = QPushButton('Select Gas')
        self._select.setProperty('variant', 'accent')
        self._select.setDefault(True)
        self._select.clicked.connect(self._accept)
        buttons.addWidget(self._select)
        outer.addLayout(buttons)

        self._populate()

    def gas_name(self):
        return self._chosen

    # -- internals ------------------------------------------------------
    def _populate(self):
        self._list.clear()
        for index, name in sorted(self._gases.items()):
            text = str(name).strip()
            if not text:
                continue
            item = QListWidgetItem(f"  {text:<18}  idx {index:>3}")
            item.setData(Qt.ItemDataRole.UserRole, text)
            self._list.addItem(item)
        count = self._list.count()
        if count:
            self._status.setText(f"{count} gases found")
            self._status.setStyleSheet(
                f"color: {theme.TEXT_DIM}; background: transparent;")
        else:
            self._status.setText(
                'No gas table for this unit — type a name below instead.')
            self._status.setStyleSheet(
                f"color: {theme.WARN}; background: transparent;")
        self._apply_filter(self._filter.text())

    def _apply_filter(self, text):
        needle = text.strip().casefold()
        for index in range(self._list.count()):
            item = self._list.item(index)
            name = item.data(Qt.ItemDataRole.UserRole) or ''
            item.setHidden(bool(needle) and needle not in name.casefold())

    def _refresh(self):
        self._status.setText('Querying…')
        self._status.setStyleSheet(
            f"color: {theme.TEXT_DIM}; background: transparent;")
        self._session.query_gas_table(self._unit, self._on_table)

    def _on_table(self, future):
        try:
            gases = future.result()
        except Exception as exc:
            self._status.setText(f'Could not reach controller: {exc}')
            self._status.setStyleSheet(
                f"color: {theme.DANGER_HOVER}; background: transparent;")
            return
        if not gases:
            self._status.setText(
                'Controller returned no gas table — type a name below instead.')
            self._status.setStyleSheet(
                f"color: {theme.WARN}; background: transparent;")
            return
        self._gases = dict(gases)
        self._populate()

    def _accept(self):
        typed = self._manual.text().strip()
        if typed:
            self._chosen = typed
            self.accept()
            return
        item = self._list.currentItem()
        if item is None or item.isHidden():
            self._status.setText('Select a gas, or type a name below.')
            self._status.setStyleSheet(
                f"color: {theme.WARN}; background: transparent;")
            return
        self._chosen = item.data(Qt.ItemDataRole.UserRole)
        self.accept()


# ---------------------------------------------------------------------- #
#  The tab                                                                #
# ---------------------------------------------------------------------- #
class ConnectionTab(QWidget):
    """Steps 1–4 down the left, live confirmation down the right."""

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self._rows = {}
        self._controllers = {}
        self._custom_gases = []
        self._live_rows = {}
        self._scan_active = False
        self._connected = False
        self._conn_seeded = True      # the log still holds its placeholder

        self._restart_timer = QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.setInterval(4000)
        self._restart_timer.timeout.connect(
            lambda: self.restart_status.setText(''))

        self._build()
        self._connect_signals()
        self.session.refresh_ports()
        self._reseed()
        self._sync_buttons()

    def _reseed(self):
        """Recover the rows a previous instance of this tab was showing.

        A re-theme throws the whole view away and builds a new one, and the
        scan is the one thing on this screen that costs real time to get back.
        The session remembers the last result for exactly this; the assignment
        on top of it is the session's too, so both survive the rebuild.

        An excluded row comes back excluded but on the gas and zone the scan
        gave it, not the ones it was last showing: the session's selection is
        the assignment, and a unit that was ticked out of the assignment is not
        in it.  The row is inert either way -- nothing excluded is configured,
        commanded or logged -- so the only cost is re-picking a zone for a unit
        on the way back in.
        """
        result = self.session.last_scan
        if result is None:
            return
        # Taken before the rows are built: building them pushes their own
        # defaults into the session, which is the thing being recovered here.
        selection = dict(self.session.selection)
        self._on_scan_finished(result)
        # The rows are back but the chatter that produced them is not, and a
        # log still saying 'No scan yet' underneath eight recovered rows reads
        # like a bug.  Say where they came from, and that they are only as
        # fresh as the scan behind them.
        found = len(result.controllers)
        self.scan_log.setPlainText(
            f"Recovered {found} controller{'' if found == 1 else 's'} from the "
            "last scan.\nRe-scan if anything on the bus has changed since.")
        if not selection:
            return
        for unit, row in self._rows.items():
            entry = selection.get(unit)
            if entry is None:
                row.apply_selection(row.gas(), row.zone(), included=False)
            else:
                row.apply_selection(entry[0], entry[1])
        self._push_selection()

    # ================================================================== #
    #  Construction                                                      #
    # ================================================================== #
    def _build(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(_scrolled(self._build_steps()))
        splitter.addWidget(_scrolled(self._build_monitor()))
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([theme.scale(STEPS_WIDTH), theme.scale(900)])
        outer.addWidget(splitter)

    def _build_steps(self):
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(theme.PAD_LG, theme.PAD_LG,
                                  theme.PAD_MD, theme.PAD_LG)
        layout.setSpacing(theme.CARD_GAP)
        layout.addWidget(self._build_step1())
        layout.addWidget(self._build_step2())
        layout.addWidget(self._build_step3())
        layout.addWidget(self._build_step4())
        layout.addStretch(1)
        return column

    # -- step 1 ---------------------------------------------------------
    def _build_step1(self):
        card = Card('COM Port', index=1)

        row = QHBoxLayout()
        row.setSpacing(theme.PAD_SM)
        row.addWidget(label('Port', object_name='FieldLabel'))
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setCurrentText('COM3')
        self.port_combo.setMinimumWidth(theme.scale(120))
        row.addWidget(self.port_combo, 1)
        refresh = QPushButton('Refresh')
        refresh.setProperty('variant', 'quiet')
        refresh.clicked.connect(self.session.refresh_ports)
        row.addWidget(refresh)
        card.add_layout(row)

        baud_row = QHBoxLayout()
        baud_row.setSpacing(theme.PAD_SM)
        baud_row.addWidget(label('Baud', object_name='FieldLabel'))
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(
            ['2400', '9600', '19200', '38400', '57600', '115200'])
        self.baud_combo.setCurrentText(str(self.session.baudrate))
        baud_row.addWidget(self.baud_combo, 1)
        card.add_layout(baud_row)

        card.add(label('Must match every device on the line.',
                       object_name='Hint'))
        return card

    # -- step 2 ---------------------------------------------------------
    def _build_step2(self):
        card = Card('Scan Units A–Z', index=2)

        row = QHBoxLayout()
        row.setSpacing(theme.PAD_SM)
        self.scan_btn = QPushButton('Scan A–Z')
        self.scan_btn.setProperty('variant', 'accent')
        self.scan_btn.clicked.connect(self._on_scan_clicked)
        row.addWidget(self.scan_btn)
        self.scan_status = label('Ready to scan', color=theme.TEXT_DIM, size=8)
        row.addWidget(self.scan_status, 1)
        card.add_layout(row)

        self.scan_bar = QProgressBar()
        self.scan_bar.setRange(0, len(SCAN_UNITS))
        self.scan_bar.setValue(0)
        self.scan_bar.setTextVisible(False)
        self.scan_bar.setFixedHeight(6)
        self.scan_bar.setStyleSheet(
            "QProgressBar { background-color: rgba(0, 0, 0, 92);"
            " border: 1px solid rgba(255, 255, 255, 20);"
            " border-radius: 3px; }"
            f"QProgressBar::chunk {{ background-color: {theme.ACCENT};"
            " border-radius: 2px; }}")
        # A full bar left on screen after the scan reads as an alarm stripe;
        # the bar only exists while there is progress to report.
        self.scan_bar.setVisible(False)
        card.add(self.scan_bar)

        card.add(label('Detected controllers', object_name='FieldLabel'))
        self.scan_log = _log_view(140)
        self.scan_log.setPlainText(
            "No scan yet. Click 'Scan A–Z' to begin.")
        card.add(self.scan_log)
        return card

    # -- step 3 ---------------------------------------------------------
    def _build_step3(self):
        card = Card('Assign Controllers', index=3)
        card.add(label('Untick a controller to leave it out of monitoring, '
                       'logging and every safety action.',
                       color=theme.AMBER, size=8))

        row = QHBoxLayout()
        row.setSpacing(theme.PAD_SM)
        select_all = QPushButton('Select all')
        select_all.setProperty('variant', 'quiet')
        select_all.setProperty('density', 'compact')
        select_all.clicked.connect(lambda: self._set_all_included(True))
        row.addWidget(select_all)
        clear = QPushButton('Clear')
        clear.setProperty('variant', 'quiet')
        clear.setProperty('density', 'compact')
        clear.clicked.connect(lambda: self._set_all_included(False))
        row.addWidget(clear)
        row.addStretch(1)
        self.autocalc_label = label('', color=theme.TEXT_DIM, size=8)
        row.addWidget(self.autocalc_label)
        card.add_layout(row)

        header = QWidget()
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(theme.PAD_SM, 0, theme.PAD_SM, 0)
        header_row.setSpacing(theme.PAD_SM)
        for heading, width in COLUMNS:
            cell = label(heading, color=theme.TEXT_DIM, size=7, bold=True)
            cell.setFixedWidth(theme.scale(width))
            header_row.addWidget(cell)
        header_row.addStretch(1)
        card.add(header)

        holder = QWidget()
        self.rows_layout = QVBoxLayout(holder)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(theme.PAD_XS + 1)
        self.rows_placeholder = label('Scan first to see units here.',
                                      color=theme.TEXT_DIM, size=9)
        self.rows_layout.addWidget(self.rows_placeholder)
        card.add(holder)
        return card

    # -- step 4 ---------------------------------------------------------
    def _build_step4(self):
        card = Card('Connect Selected & Monitor', index=4)

        row = QHBoxLayout()
        row.setSpacing(theme.PAD_SM)
        self.connect_btn = QPushButton('Connect Selected')
        self.connect_btn.setProperty('variant', 'accent')
        self.connect_btn.clicked.connect(self._on_connect)
        row.addWidget(self.connect_btn)
        self.disconnect_btn = QPushButton('Disconnect')
        self.disconnect_btn.clicked.connect(self.session.disconnect_all)
        row.addWidget(self.disconnect_btn)
        self.reconnect_btn = QPushButton('Reconnect')
        self.reconnect_btn.setProperty('variant', 'quiet')
        self.reconnect_btn.clicked.connect(self.session.restart_connection)
        row.addWidget(self.reconnect_btn)
        row.addStretch(1)
        card.add_layout(row)

        status_row = QHBoxLayout()
        status_row.setSpacing(theme.PAD_SM)
        self.conn_dot = StatusDot(theme.DANGER, diameter=8)
        status_row.addWidget(self.conn_dot)
        self.conn_status = label('Not connected', color=theme.DANGER, size=9,
                                 bold=True)
        status_row.addWidget(self.conn_status)
        status_row.addStretch(1)
        self.restart_status = label('', color=theme.TEXT_DIM, size=8)
        status_row.addWidget(self.restart_status)
        card.add_layout(status_row)

        self.monitor_btn = QPushButton('Start Live Monitor')
        self.monitor_btn.setProperty('variant', 'ready')
        self.monitor_btn.clicked.connect(self._on_monitor)
        card.add(self.monitor_btn)

        self.conn_log = _log_view(112)
        self.conn_log.setPlainText('No connection attempt yet.')
        card.add(self.conn_log)
        return card

    # -- right column ---------------------------------------------------
    def _build_monitor(self):
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(theme.PAD_MD, theme.PAD_LG,
                                  theme.PAD_LG, theme.PAD_LG)
        layout.setSpacing(theme.CARD_GAP)

        card = Card('Live Monitor', collapsible=False)
        rate_row = QHBoxLayout()
        rate_row.setContentsMargins(0, 0, 0, 0)
        rate_row.addStretch(1)
        self.rate_label = label('', color=theme.TEXT_DIM, size=8,
                                monospace=True)
        rate_row.addWidget(self.rate_label)
        card.add_layout(rate_row)

        holder = QWidget()
        self.live_layout = QVBoxLayout(holder)
        self.live_layout.setContentsMargins(0, 0, 0, 0)
        self.live_layout.setSpacing(theme.PAD_XS + 1)
        self.live_placeholder = label(
            'Connect and start monitoring to view live readings.',
            color=theme.TEXT_DIM, size=9)
        self.live_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_layout.addWidget(self.live_placeholder)
        self.live_layout.addStretch(1)
        card.add(holder, 1)

        # The card fills the column rather than floating at the top of it:
        # this half of the screen is one panel, and a stub card hovering in a
        # field of wallpaper reads as something that failed to load.
        layout.addWidget(card, 1)
        return column

    # ================================================================== #
    #  Wiring                                                            #
    # ================================================================== #
    def _connect_signals(self):
        session = self.session
        session.logged.connect(self._on_logged)
        session.failed.connect(self._on_failed)
        session.ports_changed.connect(self._on_ports)

        session.scan_started.connect(self._on_scan_started)
        session.scan_progress.connect(self._on_scan_progress)
        session.scan_controller.connect(self._on_scan_controller)
        session.scan_finished.connect(self._on_scan_finished)

        session.connecting_changed.connect(self._on_connecting)
        session.connection_changed.connect(self._on_connection)
        session.autocalc_changed.connect(self._on_autocalc)

        session.monitoring_changed.connect(self._on_monitoring)
        session.monitor_stopped.connect(self._on_monitor_stopped)
        session.restart_status.connect(self._on_restart_status)
        session.estop_armed_changed.connect(lambda _armed: self._sync_buttons())
        session.poll_rate.connect(self._on_poll_rate)
        session.samples_updated.connect(self._on_samples)

    # ================================================================== #
    #  Narration                                                         #
    # ================================================================== #
    def _on_logged(self, channel, text):
        if channel != 'connection':
            return
        # Scan chatter belongs beside the progress bar that produced it; the
        # connection log below is for what happened when the port was opened.
        view = self.scan_log if self._scan_active else self.conn_log
        if view is self.conn_log and self._conn_seeded:
            self._conn_seeded = False
            view.setPlainText('')
        view.appendPlainText(_timestamped(text))
        view.verticalScrollBar().setValue(
            view.verticalScrollBar().maximum())

    def _on_failed(self, title, detail):
        QMessageBox.critical(self, title, detail)

    def _on_ports(self, ports):
        current = self.port_combo.currentText().strip()
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        self.port_combo.addItems(ports)
        if current:
            self.port_combo.setCurrentText(current)
        elif ports:
            self.port_combo.setCurrentIndex(0)
        self.port_combo.blockSignals(False)

    # ================================================================== #
    #  Step 1 / 2 — port and scan                                        #
    # ================================================================== #
    def _port(self):
        return self.port_combo.currentText().strip()

    def _baudrate(self):
        try:
            return int(self.baud_combo.currentText())
        except ValueError:
            return self.session.baudrate

    def _on_scan_clicked(self):
        if self._scan_active:
            self.session.cancel_scan()
            self.scan_status.setText('Cancelling…')
            return
        self.session.start_scan(self._port(), self._baudrate())

    def _on_scan_started(self):
        self._scan_active = True
        self._controllers.clear()
        self.scan_btn.setText('Cancel')
        self.scan_bar.setRange(0, len(SCAN_UNITS))
        self.scan_bar.setValue(0)
        self.scan_bar.setVisible(True)
        self.scan_status.setText('Scanning…')
        self.scan_log.setPlainText('')
        self._sync_buttons()

    def _on_scan_progress(self, text, done, total):
        self.scan_status.setText(text)
        if total and total != self.scan_bar.maximum():
            self.scan_bar.setRange(0, total)
        self.scan_bar.setValue(done)

    def _on_scan_controller(self, controller):
        self._controllers[controller.unit] = controller

    def _on_scan_finished(self, result):
        self._scan_active = False
        self.scan_btn.setText('Scan A–Z')
        self.scan_bar.setVisible(False)
        if result is None:
            self.scan_status.setText('Scan failed')
            self._sync_buttons()
            return
        for controller in result.controllers:
            self._controllers[controller.unit] = controller
        found = len(result.controllers)
        self.scan_status.setText(
            f"{found} controller{'' if found == 1 else 's'} found"
            + (f" — {result.error}" if result.error else ''))
        self._populate_rows(result.controllers)
        self._sync_buttons()

    # ================================================================== #
    #  Step 3 — assignment                                               #
    # ================================================================== #
    def _populate_rows(self, controllers):
        for row in self._rows.values():
            self.rows_layout.removeWidget(row)
            # Out of the layout is not out of the window: until the event loop
            # runs the deferred delete, a still-parented row keeps painting at
            # its old geometry on top of the rows that replaced it.
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

        # Gases the devices themselves report but the base list does not know
        # about are offered to every row: if one unit is running Argon, the
        # next one probably can too.
        for controller in controllers:
            gas = controller.active_gas
            if (gas.casefold() != 'unknown'
                    and gas not in roles.BASE_GAS_TYPES
                    and gas not in self._custom_gases):
                self._custom_gases.append(gas)

        self.rows_placeholder.setVisible(not controllers)
        for controller in controllers:
            row = AssignRow(controller, self._gas_options())
            row.changed.connect(self._push_selection)
            row.zone_changed.connect(self._on_row_zone_changed)
            row.gas_requested.connect(self._open_gas_dialog)
            self.rows_layout.addWidget(row)
            self._rows[controller.unit] = row
        self._push_selection()

    def _gas_options(self):
        return list(roles.BASE_GAS_TYPES) + list(self._custom_gases)

    def _set_all_included(self, included):
        for row in self._rows.values():
            row.set_included(included)

    def _push_selection(self):
        """Hand the current assignment to the session and re-check auto-calc."""
        selection = {unit: row.selection()
                     for unit, row in self._rows.items() if row.included}
        self.session.set_selection(selection)
        self._refresh_autocalc()

    def _on_row_zone_changed(self, unit, zone):
        """A zone edited after connecting, without a reconnect.

        This does not go through :meth:`_push_selection`: replacing the whole
        selection would walk straight past the session's own checks on what a
        live reassignment is allowed to disturb.  If the session refuses, the
        combo is put back so the screen never claims a grouping the session
        did not accept.
        """
        if not self.session.set_zone(unit, zone):
            row = self._rows.get(unit)
            current = self.session.selection.get(unit)
            if row is not None and current is not None:
                row.revert_zone(current[1])
            return
        self._refresh_autocalc()
        self._rebuild_live()

    def _refresh_autocalc(self):
        config, problems = self.session.check_autocalc()
        if not self._rows:
            self.autocalc_label.setText('')
        elif problems:
            self.autocalc_label.setText('Auto-calc: unavailable')
            self.autocalc_label.setStyleSheet(
                f"color: {theme.WARN}; background: transparent;")
            self.autocalc_label.setToolTip('\n'.join(problems))
        else:
            self.autocalc_label.setText(f'Auto-calc: {_config_label(config)}')
            self.autocalc_label.setStyleSheet(
                f"color: {theme.OK}; background: transparent;")
            self.autocalc_label.setToolTip('')

    def _open_gas_dialog(self, unit):
        row = self._rows.get(unit)
        if row is None:
            return
        controller = self._controllers.get(unit)
        gases = controller.supported_gases if controller else {}
        dialog = GasTableDialog(self.session, unit, gases, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = (dialog.gas_name() or '').strip()
        if not name:
            return
        if name not in self._custom_gases and name not in roles.BASE_GAS_TYPES:
            self._custom_gases.append(name)
        options = self._gas_options()
        for other in self._rows.values():
            other.set_gas_options(options)
        row.choose_gas(name)

    def _set_rows_locked(self, locked):
        for row in self._rows.values():
            row.set_locked(locked)

    # ================================================================== #
    #  Step 4 — connect and monitor                                      #
    # ================================================================== #
    def _on_connect(self):
        self._push_selection()
        result = self.session.connect_all(self._port(), self._baudrate())
        if result != 'needs_confirmation':
            return
        _config, problems = self.session.check_autocalc()
        detail = '\n'.join(f'  • {problem}' for problem in problems)
        answer = QMessageBox.question(
            self, 'Auto-calculation unavailable',
            'Automatic RQL target calculation will NOT be available with this '
            'assignment:\n\n' + detail
            + '\n\nEvery setpoint will have to be entered by hand. '
              'Connect anyway?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.session.connect_all(self._port(), self._baudrate(),
                                     accept_no_autocalc=True)

    def _on_connecting(self, connecting):
        self.connect_btn.setText('Connecting…' if connecting
                                 else 'Connect Selected')
        self._sync_buttons()

    def _on_connection(self, connected):
        self._connected = connected
        self.conn_status.setText('Connected' if connected else 'Not connected')
        color = theme.OK if connected else theme.DANGER
        self.conn_status.setStyleSheet(
            f"color: {color}; background: transparent;")
        self.conn_dot.set_color(color)
        self._set_rows_locked(connected)
        if connected:
            self._rebuild_live()
        else:
            self._clear_live()
        self._sync_buttons()

    def _on_autocalc(self, available, config):
        if not available:
            return
        self.autocalc_label.setText(f'Auto-calc: {_config_label(config)}')
        self.autocalc_label.setStyleSheet(
            f"color: {theme.OK}; background: transparent;")

    def _on_monitor(self):
        self.session.toggle_monitoring(self._port())

    def _on_monitoring(self, monitoring):
        self.monitor_btn.setText('Stop Live Monitor' if monitoring
                                 else 'Start Live Monitor')
        self.monitor_btn.setProperty('variant',
                                     'danger' if monitoring else 'ready')
        # A property that decides which QSS rule applies only takes effect
        # after the widget is re-polished.
        self.monitor_btn.style().unpolish(self.monitor_btn)
        self.monitor_btn.style().polish(self.monitor_btn)
        if not monitoring:
            self.rate_label.setText('')
            for row in self._live_rows.values():
                row.clear()
        self._sync_buttons()

    def _on_monitor_stopped(self, message, _was_reconnect):
        QMessageBox.warning(self, 'Monitor stopped', message)

    def _on_restart_status(self, text, kind):
        color = {'ok': theme.OK, 'warn': theme.WARN,
                 'danger': theme.DANGER_HOVER}.get(kind, theme.TEXT_DIM)
        self.restart_status.setText(text)
        self.restart_status.setStyleSheet(
            f"color: {color}; background: transparent;")
        self._restart_timer.start()

    def _on_poll_rate(self, hz, ms):
        self.rate_label.setText(f'{hz:5.2f} Hz   {ms:6.1f} ms/pass')

    def _sync_buttons(self):
        session = self.session
        busy = session.is_connecting or self._scan_active
        self.scan_btn.setEnabled(
            self._scan_active
            or not (session.is_connecting or self._connected
                    or session.is_monitoring))
        self.connect_btn.setEnabled(
            not busy and not self._connected and not session.is_monitoring)
        self.disconnect_btn.setEnabled(self._connected)
        # Reconnect re-opens the port under the same assignment; the session
        # refuses it unless it really is connected and not mid-E-STOP.
        self.reconnect_btn.setEnabled(self._connected and session.estop_armed)
        self.monitor_btn.setEnabled(self._connected and not session.is_connecting)

    # ================================================================== #
    #  Live column                                                       #
    # ================================================================== #
    def _empty_live(self):
        """Strip the column back to nothing, keeping the placeholder alive."""
        while self.live_layout.count():
            item = self.live_layout.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self.live_placeholder:
                # See _populate_rows: unparent now, destroy whenever.
                widget.setParent(None)
                widget.deleteLater()
        self._live_rows.clear()

    def _clear_live(self):
        self._empty_live()
        self.live_placeholder.setVisible(True)
        self.live_layout.addStretch(1)
        self.live_layout.addWidget(self.live_placeholder)
        self.live_layout.addStretch(1)

    def _rebuild_live(self):
        """One read-only row per assigned controller, grouped by zone."""
        self._empty_live()
        self.live_placeholder.setVisible(False)

        buckets = {}
        for unit, row in self._rows.items():
            if not row.included:
                continue
            gas, zone = row.selection()
            if gas in (roles.UNSELECTED_GAS, '') or zone == roles.UNASSIGNED_ZONE:
                continue
            buckets.setdefault(zone, []).append((unit, gas))

        for zone in roles.ZONE_OPTIONS:
            members = buckets.get(zone)
            if not members:
                continue
            header = StageHeader(zone)
            header.set_summary(
                f"{len(members)} controller{'' if len(members) == 1 else 's'}")
            self.live_layout.addWidget(header)
            for unit, gas in sorted(members):
                color = theme.GAS_COLORS.get(gas, ZONE_COLORS.get(zone,
                                                                  theme.TEXT))
                live = LiveRow(unit, gas, color)
                self.live_layout.addWidget(live)
                self._live_rows[unit] = live
        self.live_layout.addStretch(1)

    def _on_samples(self, _generation):
        if not self._live_rows or not self.isVisible():
            return
        samples = self.session.live_samples()
        for unit, row in self._live_rows.items():
            sample = samples.get(unit)
            if sample:
                row.update_sample(sample)
