"""The Operation & Monitoring tab: the screen a run is actually driven from.

Two columns over a collapsible sequence panel.  The left column holds the
things the operator sets up -- the mode switch, logging, targets, and saved
sequences -- and the right column holds what the rig is doing about it.  They are separated
because they change on completely different timescales: the left column is
touched a handful of times per run, the right column changes ten times a
second, and interleaving them would put a live number next to every button.

**Progressive disclosure by operating mode.**  Staged (RQL) mode shows the
auto-calculation and stage grouping.  Standard mode uses the same saved-
sequence workflow but hides staged arithmetic that has no meaning without
two zones and a pilot.

Nothing here writes to hardware directly.  Every setpoint goes out through
:meth:`FlowSession.set_role_setpoint`, which ramps the lines that must be
ramped and honours the zero lock.
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog,
                               QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QInputDialog, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy, QSpinBox,
                               QSplitter, QVBoxLayout, QWidget, QWidgetAction)

from ..core.combustion_prefs import (
    GEOMETRY_AREA, GEOMETRY_DIAMETER, MAX_INLET_COUNT, SCOPE_ALL,
    SCOPE_STAGE1, SCOPE_STAGE2,
)
from ..core.sequence import Sequence, opening_mismatches
from ..core.session import (DEFAULT_LOG_DIR, MODE_STAGED, MODE_STANDARD,
                           SEQ_IDLE)
from ..domain import combustion, roles, rql
from ..domain.graphing import auto_bar_span
from . import qt_theme as theme
from .qt_agent_launcher import AgentLauncherPane
from .qt_sequence_panel import SequencePanel
from .qt_experiment_plan import ExperimentPlanPane
from .qt_widgets import (Card, MetricTile, StageHeader, UnitCard,
                         divider, field_grid, label, mono, row)

#: Roles the auto-calculation produces a target for, in tile order.
TARGET_KEYS = ('nh3_rich', 'h2_rich', 'nh3_lean',
               'h2_lean', 'rich_air', 'lean_air')

#: Short captions for the tiles, where the full role label is too wide.
SHORT_LABELS = {
    'nh3_rich': 'NH3-1', 'h2_rich': 'H2-1', 'rich_air': 'AIR-1',
    'nh3_lean': 'NH3-2', 'h2_lean': 'H2-2', 'lean_air': 'AIR-2',
    'ch4_pilot': 'CH4',
}

#: How often the combustion estimate may refresh, as (caption, passes).  In
#: acquisition passes rather than seconds, because that is the thing being
#: skipped: at the rig's usual rate "every 10th" is about once a second.
COMBUSTION_RATES = (('every pass', 1), ('every 2nd pass', 2),
                    ('every 5th pass', 5), ('every 10th pass', 10),
                    ('every 25th pass', 25), ('every 50th pass', 50))

def _fmt(value, decimals=2, dash='--'):
    """A derived number for a tile, or a dash where there is no answer.

    ``None`` and zero both mean "not computable" for everything on these
    cards -- an undeclared bore, no fuel to divide by, no air to burn in --
    and printing 0.00 for any of them would look like a measurement.
    """
    if value is None or value <= 0.0:
        return dash
    return f'{value:.{decimals}f}'


def _area_from_diameter(diameter_mm):
    """Cross-sectional area in mm² for a stored inlet diameter."""
    if diameter_mm is None:
        return None
    return math.pi * float(diameter_mm) ** 2 / 4.0


SEQUENCE_SUFFIX = '.fcseq.json'
_WINDOWS_RESERVED_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{number}' for number in range(1, 10)),
    *(f'LPT{number}' for number in range(1, 10)),
}


def _sequence_stem(value):
    """A safe display name and filename stem for a saved sequence."""
    name = str(value).strip()
    if name.casefold().endswith(SEQUENCE_SUFFIX):
        name = name[:-len(SEQUENCE_SUFFIX)].rstrip()
    if not name:
        raise ValueError('Enter a name for the sequence.')
    if len(name) > 96:
        raise ValueError('Sequence names must be 96 characters or fewer.')
    if any(character in name for character in '<>:"/\\|?*'):
        raise ValueError('Sequence names cannot contain < > : " / \\ | ? or *.')
    if name.endswith(('.', ' ')):
        raise ValueError('Sequence names cannot end with a dot or space.')
    if name.split('.', 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"'{name}' is reserved by Windows.")
    return name

class SavedSequenceRow(QWidget):
    """One entry in the quick list: a name that loads, and three actions.

    The list used to load and run from the same click, which put an
    irreversible action -- writing the opening setpoints and starting the clock
    -- behind the only gesture there was.  An operator who wanted to *look* at
    what they recorded yesterday had to run it to see it.  So the row is split:
    the name loads the sequence into the panel and stops there, ▶ is the
    one-click start, ✎ renames it, and ✕ removes the file.  The actions are
    deliberately small and deliberately separate from the name.

    Deleting is here rather than behind a right-click menu because a folder
    that only ever grows is a folder nobody prunes: after a week of trials the
    twenty most recent are all called some variation of the same thing, and the
    list stops being worth reading.  It asks first -- see
    :meth:`OperationTab._delete_saved` -- since nothing else in the app removes
    a file the operator made.
    """

    play = Signal()
    rename = Signal()
    remove = Signal()

    def __init__(self, name, parent=None):
        super().__init__(parent)
        line = QHBoxLayout(self)
        line.setContentsMargins(0, 1, theme.PAD_XS, 1)
        line.setSpacing(theme.PAD_SM)

        self._name = name
        self._label = QLabel(name)
        # Ignored horizontally so the label takes whatever the row has left
        # rather than asking for the width of the untruncated name.  With a
        # size hint in play, eliding in resizeEvent would shrink the hint,
        # win back the space, un-elide, and oscillate.
        self._label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                  QSizePolicy.Policy.Preferred)
        line.addWidget(self._label, 1)

        self._buttons = []
        for text, object_name, tip, signal in (
                ('▶', 'RowPlay',
                 'Load and run this sequence once, from the top.', self.play),
                ('✎', 'RowRename',
                 'Rename this saved sequence.', self.rename),
                ('✕', 'RowDelete',
                 'Delete this sequence file.  You will be asked first.',
                 self.remove)):
            button = QPushButton(text)
            button.setObjectName(object_name)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedWidth(theme.scale(24))
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setToolTip(tip)
            button.clicked.connect(signal.emit)
            line.addWidget(button)
            self._buttons.append(button)
        self._line = line

    def sizeHint(self):
        """Ask for the height, and for almost none of the width.

        The list gives every row the width of the widest hint it is offered,
        and this one has no horizontal scrollbar.  Hinting at the width of the
        untruncated name would therefore push the button off the right-hand
        edge, where it is still there, still clickable, and impossible to see.
        Ask small and the view stretches the row to the viewport instead.
        """
        return QSize(theme.scale(48), super().sizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Measured off the row, not off the label: at this point the layout
        # has not necessarily placed its children yet, so the label's own
        # width can still be the one it had before the resize -- which would
        # elide a name that fits perfectly well.
        margins = self._line.contentsMargins()
        room = (self.width() - margins.left() - margins.right()
                - self._line.spacing() * len(self._buttons)
                - sum(button.width() for button in self._buttons))
        self._label.setText(self._label.fontMetrics().elidedText(
            self._name, Qt.TextElideMode.ElideMiddle, max(24, room)))


UNAVAILABLE_NOTE = ('This assignment does not cover the RQL roles, so targets '
                    'cannot be calculated from power and φ. Set flows '
                    'directly on the controller cards.')


def _number(entry, fallback=0.0):
    try:
        return float(entry.text())
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------------- #
#  Safety bar                                                             #
# ---------------------------------------------------------------------- #
class SafetyBar(QWidget):
    """Batch send and zero commands, built into the tab strip's corner.

    It lives in this module because it is part of the operation surface, but
    it is mounted by the main window rather than by the tab: an emergency
    control that is only reachable from the tab you happen to be on is not an
    emergency control.
    """

    def __init__(self, session, send_all, parent=None):
        super().__init__(parent)
        self.session = session
        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.PAD_MD, theme.PAD_XS,
                                  theme.PAD_LG, theme.PAD_XS)
        layout.setSpacing(theme.PAD_SM)

        self.buttons = {}
        for text, handler, variant, tip in (
                ('SET ALL FLOWS', send_all, 'accent',
                 "Send every controller card's SP together."),
                ('ZERO FUEL', session.zero_fuel, None,
                 'Zero every assigned non-air controller.'),
                ('ZERO ALL', session.zero_all, None,
                 'Zero every assigned controller.')):
            button = QPushButton(text)
            if variant:
                button.setProperty('variant', variant)
            else:
                button.setObjectName('SafetyButton')
            # Wrapped rather than connected straight through: ``clicked``
            # carries a checked flag and these commands take no argument.
            button.clicked.connect(lambda _checked=False, call=handler: call())
            button.setToolTip(tip)
            layout.addWidget(button)
            self.buttons[text] = button

        session.estop_armed_changed.connect(self._set_armed)
        self._set_armed(session.estop_armed)

    def _set_armed(self, armed):
        for button in self.buttons.values():
            button.setEnabled(bool(armed))
        self.setToolTip('' if armed else
                        'Connect the flow meters to arm these controls.')


# ---------------------------------------------------------------------- #
#  Operation tab                                                          #
# ---------------------------------------------------------------------- #
class OperationTab(QWidget):
    """Everything an operator touches between connecting and shutting down."""

    #: Emitted when the operator asks for a log file that does not resolve,
    #: so the window can put it in the status bar rather than a dialog.
    status = Signal(str)

    def __init__(self, session, parent=None, *, agent_manager=None,
                 agent_collapsed=True):
        super().__init__(parent)
        self.session = session
        self.agent_manager = agent_manager
        self.agent_collapsed = bool(agent_collapsed)
        self._cards = {}
        self._card_keys = {}
        #: Unit to the largest figure that line has been asked for, and unit to
        #: the span its bar is currently drawn against.  Two dictionaries
        #: because the span is a rounded figure *above* the peak: feeding it
        #: back in as the next peak would make every pass inflate the bar.
        self._peaks = {}
        self._scales = {}
        self._stage_headers = {}
        self._target_tiles = {}
        self._combustion = {}
        self._combustion_std = {}
        #: Scope -> geometry selector, value entry, and unit label widgets. A
        #: list per scope because both mode-specific cards are built.
        self._combustion_geometry_combos = {}
        self._combustion_geometry_entries = {}
        self._combustion_geometry_units = {}
        #: Scope -> labels displaying the effective inlet area.
        self._combustion_area_labels = {}
        self._combustion_inlet_spins = {}
        self._combustion_live_boxes = []
        self._combustion_rate_combos = []
        self._combustion_menus = []
        #: Acquisition passes seen, and whether the cards are currently
        #: showing the paused state -- so pausing repaints once rather than
        #: on every pass it then declines to draw.
        self._combustion_tick = 0
        self._combustion_paused = False
        self._pending_generation = None
        # Kept on the session so an appearance re-theme, which rebuilds this
        # tab, does not unexpectedly put the operator back into list view.
        self._cards_view = getattr(session, 'controller_cards_view', 'list')
        if self._cards_view not in ('list', 'grid'):
            self._cards_view = 'list'

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._split = QSplitter(Qt.Orientation.Vertical)
        self._split.setHandleWidth(4)
        self._split.addWidget(self._build_columns())
        self.sequence_panel = SequencePanel(session)
        self.sequence_panel.setVisible(False)
        self._split.addWidget(self.sequence_panel)
        self._split.setStretchFactor(0, 1)
        self._split.setStretchFactor(1, 0)
        outer.addWidget(self._split, 1)

        session.mode_changed.connect(self._on_mode)
        session.connection_changed.connect(self._on_connection)
        session.monitoring_changed.connect(self._on_monitoring)
        session.assignments_changed.connect(lambda _map: self._rebuild_cards())
        session.autocalc_changed.connect(self._on_autocalc)
        session.targets_changed.connect(self._on_targets)
        session.samples_updated.connect(self._on_samples)
        session.logging_changed.connect(self._on_logging)
        session.udp_changed.connect(self._on_udp)
        session.logged.connect(self._on_logged)
        session.ramp_progress.connect(self._on_ramp)
        # A sequence just saved has to appear in the quick-play list without the
        # operator hunting for a refresh, and the list must not offer a second
        # run while one is already going out.
        session.sequence_saved.connect(lambda _path: self._refresh_saved())
        session.sequence_state_changed.connect(self._on_sequence_state)
        session.combustion_changed.connect(
            lambda _prefs: self._apply_combustion_prefs())

        self._apply_combustion_prefs()
        self._on_mode(session.operating_mode)
        self._on_connection(session.controllers_connected)
        self._on_monitoring(session.is_monitoring)
        # Assessed rather than read off the session: the cached pair is only
        # written when an assignment change is announced, and a tab built
        # after that has already happened would start out of step with it.
        config, problems = session.check_autocalc()
        self._on_autocalc(not problems, config)
        # The rest of the screen is signal-driven, which is only enough for a
        # tab that existed when the signal went out.  A tab built later — a
        # re-theme rebuilds the whole window — has to catch up from the state
        # the session is holding, or it opens claiming the run is idle while
        # the rig is running.
        self._on_targets(dict(session.target_flows))
        self._on_logging(session.logging_active, session.log_path)

    # ------------------------------------------------------------------ #
    #  Mode strip                                                         #
    # ------------------------------------------------------------------ #
    def _build_mode_strip(self):
        """Mode and monitoring, at the head of the left column.

        These used to sit in a bar spanning both columns, which spent a row of
        the window's height on two controls -- and spent it on the right-hand
        side too, where the plots are and where every pixel of height is
        another few seconds of trace.  Mode belongs beside the cards it
        governs: switching to Standard is what hides the staged target
        calculator below it, so the switch and its effect are visible in the
        same glance.

        The strip is pinned above the left column's scroll area rather than
        placed inside it, so scrolling down to the sequence list does not
        carry Start Monitoring off the top of the screen.
        """
        holder = QWidget()
        holder.setObjectName('Row')
        bar = QHBoxLayout(holder)
        bar.setContentsMargins(theme.PAD_LG, theme.PAD_SM,
                               theme.PAD_SM + 2, theme.PAD_SM)
        bar.setSpacing(theme.PAD_SM)

        bar.addWidget(label('MODE', color=theme.TEXT_DIM, size=7, bold=True))
        self._mode_buttons = {}
        for mode, text in ((MODE_STANDARD, 'Standard'),
                           (MODE_STAGED, 'Staged (RQL)')):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setProperty('density', 'compact')
            button.clicked.connect(
                lambda _checked=False, value=mode:
                self.session.set_operating_mode(value))
            bar.addWidget(button)
            self._mode_buttons[mode] = button

        bar.addStretch(1)
        self.monitor_btn = QPushButton('Start Monitoring')
        self.monitor_btn.setProperty('variant', 'accent')
        self.monitor_btn.setProperty('density', 'compact')
        self.monitor_btn.clicked.connect(
            lambda: self.session.toggle_monitoring())
        bar.addWidget(self.monitor_btn)
        return holder

    def _on_sequence_state(self, state):
        """Grey the quick-play list while a recording or replay owns the clock."""
        self.saved_list.setEnabled(state == SEQ_IDLE)

    def _toggle_sequence(self, shown):
        self.sequence_btn.setText((
            '▾  Record / Replay Flow Sequence' if shown
            else '▸  Record / Replay Flow Sequence'))
        self.sequence_panel.setVisible(shown)
        if shown:
            # Opening the panel is the other moment the folder is worth
            # re-reading: a sequence saved from the panel's own Save button in a
            # previous session landed there without this tab hearing about it.
            self._refresh_saved()
        if shown and self._split.sizes()[1] < 80:
            total = sum(self._split.sizes()) or self.height()
            self._split.setSizes([int(total * 0.52), int(total * 0.48)])

    # ------------------------------------------------------------------ #
    #  Columns                                                            #
    # ------------------------------------------------------------------ #
    def _build_columns(self):
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(4)
        splitter.addWidget(self._build_left_column())
        splitter.addWidget(self._build_right_column())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([520, 1040])
        return splitter

    @staticmethod
    def _scroll_column():
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # The viewport would otherwise fill itself opaquely and mask the
        # backdrop the cards are supposed to be floating over.
        scroll.viewport().setAutoFillBackground(False)
        return scroll

    def _build_left_column(self):
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self._build_mode_strip())
        column.addWidget(self._build_left_cards(), 1)
        return holder

    def _build_left_cards(self):
        scroll = self._scroll_column()
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(theme.PAD_LG, theme.PAD_LG,
                                  theme.PAD_SM + 2, theme.PAD_LG)
        column.setSpacing(theme.CARD_GAP)
        column.addWidget(self._card_logging())
        self._autocalc_card = self._card_autocalc()
        column.addWidget(self._autocalc_card)
        self._sequence_card = self._card_sequence()
        column.addWidget(self._sequence_card)
        if self.agent_manager is not None:
            self.agent_pane = AgentLauncherPane(
                self.agent_manager, collapsed=self.agent_collapsed)
            column.addWidget(self.agent_pane)
        column.addWidget(self._card_syslog())
        column.addStretch(1)
        scroll.setWidget(holder)
        # Wide enough that the fixed-width entry fields never force a clip;
        # the splitter can still be dragged wider.
        scroll.setMinimumWidth(holder.sizeHint().width() + 26)
        return scroll

    # -- logging --------------------------------------------------------- #
    def _card_logging(self):
        card = Card(
            'Logging & Acquisition',
            help_text=('Write one CSV row per completed monitoring pass. The '
                       'columns are fixed from the assignment present when '
                       'logging starts, so zones cannot move while a log is '
                       'open. The LabVIEW UDP listener accepts "log" to open '
                       'a timestamped copy and "stop" to close it; rows are '
                       'written only while monitoring is running.'))

        self.log_path = QLineEdit(str(DEFAULT_LOG_DIR / 'run.csv'))
        # textEdited, not textChanged: the field is also written to from
        # ``_on_logging`` to show the file actually opened, and a LabVIEW run
        # opens a timestamped sibling.  Feeding that back would stamp the
        # stamp, and every unattended trigger would lengthen the name again.
        self.log_path.textEdited.connect(self._remember_log_destination)
        self._remember_log_destination(self.log_path.text())
        browse = QPushButton('Browse…')
        browse.setProperty('variant', 'quiet')
        browse.clicked.connect(self._browse_log)
        caption = QLabel('Log file')
        caption.setObjectName('FieldLabel')
        card.add(row(caption, self.log_path, browse, stretch_at=1))

        self.start_log_btn = QPushButton('Start Logging')
        self.start_log_btn.setProperty('variant', 'accent')
        self.start_log_btn.clicked.connect(self._start_logging)
        self.stop_log_btn = QPushButton('Stop Logging')
        self.stop_log_btn.setEnabled(False)
        self.stop_log_btn.clicked.connect(lambda: self.session.stop_logging())
        self.log_state = label('OFF', color=theme.TEXT_DIM, size=9,
                               monospace=True)
        card.add(row(self.start_log_btn, self.stop_log_btn, self.log_state,
                     None))

        grid, entries = field_grid([('LabVIEW UDP host', '127.0.0.1'),
                                    ('LabVIEW UDP port', 61557)], width=96)
        self.udp_host = entries['LabVIEW UDP host']
        self.udp_port = entries['LabVIEW UDP port']
        card.add_layout(grid)

        self.udp_btn = QPushButton('Start Listener')
        self.udp_btn.setProperty('variant', 'quiet')
        self.udp_btn.clicked.connect(self._toggle_udp)
        self.udp_state = label('listener off', color=theme.TEXT_DIM, size=8,
                               monospace=True)
        card.add(row(self.udp_btn, self.udp_state, None))

        return card

    def _remember_log_destination(self, text):
        """Tell the session where an unattended log should be written.

        A LabVIEW ``log`` datagram opens a file with nobody at the keyboard,
        so the destination cannot be read off this field at the moment it
        arrives — the tab that owns the field is thrown away and rebuilt on a
        re-theme.  It is pushed across as it is typed instead.
        """
        self.session.log_destination = text
        self.session.log_dir = DEFAULT_LOG_DIR

    def _browse_log(self):
        DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path, _filter = QFileDialog.getSaveFileName(
            self, 'Log file', self.log_path.text() or str(DEFAULT_LOG_DIR),
            'CSV files (*.csv);;Excel workbooks (*.xlsx);;All files (*)')
        if path:
            self.log_path.setText(path)
            self._remember_log_destination(path)

    def _start_logging(self):
        # ``resolve_log_path`` answers with two paths: the destination to show
        # the operator, and the file actually opened.  They differ for LabVIEW
        # runs, which get a timestamped sibling per session.
        shown, actual = self.session.resolve_log_path(
            self.log_path.text(), DEFAULT_LOG_DIR)
        self.log_path.setText(str(shown))
        self.session.start_logging(actual)

    def _toggle_udp(self):
        # The button's own text is the state: it is set from ``udp_changed``,
        # which is the only thing that knows whether the socket really opened.
        if self.udp_btn.text().startswith('Stop'):
            self.session.stop_udp()
        else:
            self.session.start_udp(self.udp_host.text().strip() or '127.0.0.1',
                                   int(_number(self.udp_port, 61557)))

    # -- auto-calculate --------------------------------------------------- #
    def _card_autocalc(self):
        card = Card(
            'Auto-Calculate Flows', index=1,
            help_text=('Calculate and store controller targets from firing '
                       'power, hydrogen fraction, stage split, and equivalence '
                       'ratios. Calculation alone never sends a flow command.'))
        grid, entries = field_grid([
            ('Power (kW)', 10), ('H₂ percentage (%)', 30),
            ('Stage 1 split (%)', 99.99), ('φ stage 1', 1.1),
            ('φ global', 0.6),
        ])
        self._calc_fields = entries
        card.add_layout(grid)

        calculate = QPushButton('Calculate && Store Targets   (no flows sent)')
        calculate.setProperty('variant', 'accent')
        calculate.clicked.connect(self._calculate)
        card.add(calculate)

        self.calc_status = QLabel('No targets stored yet.')
        self.calc_status.setObjectName('Hint')
        self.calc_status.setWordWrap(True)
        card.add(self.calc_status)

        tiles = QGridLayout()
        tiles.setSpacing(theme.PAD_SM)
        for index, key in enumerate(TARGET_KEYS):
            tile = MetricTile(SHORT_LABELS[key])
            tile.set_value('—')
            tiles.addWidget(tile, index // 3, index % 3)
            self._target_tiles[key] = tile
        card.add_layout(tiles)
        return card

    def _calculate(self):
        fields = self._calc_fields
        request = rql.AutoCalcRequest(
            power_kw=_number(fields['Power (kW)']),
            h2_fraction=_number(fields['H₂ percentage (%)']) / 100.0,
            phi_stage1=_number(fields['φ stage 1']),
            phi_global=_number(fields['φ global']),
            split_rich=_number(fields['Stage 1 split (%)']) / 100.0)
        config = self.session.autocalc_config or rql.FULL_RQL
        try:
            targets = rql.auto_calc(request, config=config,
                                    calculator=self.session.calc)
        except rql.AutoCalcError as exc:
            self.calc_status.setText(str(exc))
            self.calc_status.setStyleSheet(f'color: {theme.DANGER_HOVER};')
            return
        self.calc_status.setStyleSheet('')
        stored = self.session.set_targets(targets)
        dropped = len(targets) - len(stored)
        summary = (f"Targets stored for {request.power_kw:g} kW · "
                   f"{request.h2_fraction * 100:g}% H₂ · "
                   f"φ global {request.phi_global:g}.")
        if dropped:
            summary += (f"  {dropped} target(s) had no assigned controller "
                        "and were discarded.")
        self.calc_status.setText(summary)

    def _on_targets(self, targets):
        for key, tile in self._target_tiles.items():
            value = targets.get(key)
            tile.set_value('—' if value is None else f'{value:.2f}')
        # New targets change what the bars are measured against.
        self._refresh_readings(force=True)

    def _on_autocalc(self, available, config):
        """Reflect what the current assignment can actually calculate."""
        split = self._calc_fields.get('Stage 1 split (%)')
        if split is not None:
            # No assessment yet means nothing has ruled the second stage out.
            staged_split = (config or rql.FULL_RQL) == rql.FULL_RQL
            split.setEnabled(staged_split)
            split.setToolTip('' if staged_split else
                             'This assignment has no second fuel zone, so all '
                             'the fuel goes through stage 1.')
        if not available:
            self.calc_status.setText(UNAVAILABLE_NOTE)
        elif self.calc_status.text() == UNAVAILABLE_NOTE:
            # The assignment that made it unavailable has been repaired; a
            # refusal left on screen after the reason is gone is worse than
            # no message, because the operator stops reading the line.
            self.calc_status.setText('No targets stored yet.')

    def _on_ramp(self, key, percent):
        self.status.emit(
            f'{roles.ROLE_LABELS.get(key, key)}: ramping {percent}%')

    def send_all(self):
        for card in self._cards.values():
            card._emit_setpoint()
        self.status.emit(
            f'{len(self._cards)} controller setpoint(s) queued together.')

    # -- sequence --------------------------------------------------------- #
    def _card_sequence(self):
        card = Card(
            'Sequences', index=None,
            help_text=('Record every commanded setpoint while monitoring, '
                       'edit the resulting curve, then replay or repeat it. '
                       'Automated test sequences use the same section but '
                       'advance through labelled stages from live meter conditions. '
                       'Clicking a saved name loads it into the panel and '
                       'nothing moves. ▶ loads and runs it once, with no '
                       'repeats, and only if the rig is already standing at '
                       'the flows it opens with; otherwise the lines that '
                       'disagree are shown. ✎ renames the saved file. '
                       '✕ deletes it after confirmation.'))
        self.sequence_btn = QPushButton('▸  Record / Replay Flow Sequence')
        self.sequence_btn.setCheckable(True)
        self.sequence_btn.setProperty('variant', 'quiet')
        self.sequence_btn.toggled.connect(self._toggle_sequence)
        card.add(self.sequence_btn)

        card.add(divider())
        card.add(label('SAVED FLOW SEQUENCES  —  CLICK LOAD,  ▶ RUN,  ✎ RENAME,  ✕ DELETE',
                       color=theme.TEXT_DIM, size=7, bold=True))
        self.saved_list = QListWidget()
        self.saved_list.setFixedHeight(theme.scale(104))
        # Elided rather than wrapped or scrolled sideways: this column has no
        # horizontal scrollbar, and a long timestamped filename must not be able
        # to widen it.
        self.saved_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.saved_list.setWordWrap(False)
        self.saved_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.saved_list.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Fixed)
        # Rows carry a widget with a button at their right-hand end, and the
        # default Fixed mode lays them out once at whatever width the list
        # happened to have then.  Adjust re-lays them on every resize, which is
        # what keeps that button inside a column the splitter can narrow.
        self.saved_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.saved_list.itemClicked.connect(self._on_saved_clicked)
        card.add(self.saved_list)

        card.add(divider())
        self._experiment_plan_card = ExperimentPlanPane(
            self.session.experiment_plans)
        card.add(self._experiment_plan_card)

        self._refresh_saved()
        return card

    def _refresh_saved(self):
        """Re-read the sequence folder, newest first.

        Cheap enough to do on every relevant event rather than watching the
        directory: it is one ``glob`` of a folder holding a handful of files,
        and a list that quietly disagrees with the disk is worse than a
        ``glob``.
        """
        self.saved_list.clear()
        try:
            paths = sorted(Path(self.session.sequence_dir).glob('*.fcseq.json'),
                           key=lambda path: path.stat().st_mtime, reverse=True)
        except OSError:
            # No folder yet, or one we cannot read.  Nothing has been saved that
            # we could offer, which is not an error worth a dialog.
            paths = []
        for path in paths[:20]:
            name = path.name[:-len('.fcseq.json')]
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(f'{path}\n\nClick the name to load it. ▶ loads '
                            'and runs it once, from the top. ✎ renames it. '
                            '✕ deletes it.')
            widget = SavedSequenceRow(name)
            widget.play.connect(
                lambda chosen=str(path): self._play_saved(chosen))
            widget.rename.connect(
                lambda chosen=str(path): self._rename_saved(chosen))
            widget.remove.connect(
                lambda chosen=str(path): self._delete_saved(chosen))
            item.setSizeHint(widget.sizeHint())
            self.saved_list.addItem(item)
            self.saved_list.setItemWidget(item, widget)
        if not paths:
            note = QListWidgetItem('No saved sequences yet')
            note.setFlags(Qt.ItemFlag.NoItemFlags)
            self.saved_list.addItem(note)

    def _on_saved_clicked(self, item):
        """Load one saved sequence into the panel.  Nothing moves.

        Loading and running used to be the same click, which meant the list
        could only be browsed by running things.  Opening it here writes no
        setpoints and starts no clock: it puts the curve on screen, where it
        can be read, edited, and started from the panel's own transport.
        """
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        if not self._load_saved(path):
            return
        # Shown, because a sequence that has been loaded and is nowhere to be
        # seen is indistinguishable from a click that did nothing.
        self.sequence_btn.setChecked(True)
        self.status.emit(f"Loaded '{self.session.sequence.name}' — press "
                         'Replay on the panel, or ▶ beside its name, to '
                         'run it.')

    def _load_saved(self, path):
        """Read the file onto the session, or say what stopped it."""
        if self.session.sequence_state != SEQ_IDLE:
            self.status.emit('Finish the recording or replay in progress first.')
            return False
        # ``load_sequence`` has already said why when it answers None.
        return self.session.load_sequence(Path(path)) is not None

    def _play_saved(self, path):
        """Load one saved sequence and run it once, if the rig is where it starts.

        The check is on the *measured* flows rather than on the commanded ones.
        What matters is where the rig actually is: a replay primes by writing the
        opening setpoints, so starting one from somewhere else means every line
        jumps at t=0, and on the air and pilot lines that is a transient into the
        burner rather than a transition.
        """
        if not self._load_saved(path):
            return
        sequence = self.session.sequence
        flows = {track.key: self.session.flow_for_role(track.key)
                 for track in sequence.tracks}
        mismatches = opening_mismatches(sequence, flows)
        if mismatches and not self._confirm_start(sequence, mismatches):
            return
        # Shown before it runs: a sequence started from a one-line list still
        # deserves the timeline, the curves and the Stop button.
        self.sequence_btn.setChecked(True)
        self.sequence_panel.request_replay(repeats=1)

    def _rename_saved(self, path):
        """Ask for a new saved-sequence name, then rename file and metadata."""
        target = Path(path)
        current = target.name[:-len(SEQUENCE_SUFFIX)]
        name, accepted = QInputDialog.getText(
            self, 'Rename saved sequence', 'New name:', text=current)
        if not accepted:
            return
        if not self._rename_saved_to(target, name):
            return

    def _rename_saved_to(self, path, name):
        """Rename one sequence without overwriting another saved run."""
        target = Path(path)
        try:
            clean_name = _sequence_stem(name)
        except ValueError as exc:
            self.status.emit(f'Could not rename sequence: {exc}')
            return False

        destination = target.with_name(clean_name + SEQUENCE_SUFFIX)
        if destination == target:
            return True
        if destination.exists():
            self.status.emit(
                f"Could not rename sequence: '{clean_name}' already exists.")
            return False

        try:
            renamed = Sequence.load(target)
            renamed.name = clean_name
            renamed.path = None
            renamed.save(destination)
        except Exception as exc:
            self.status.emit(f'Could not rename {target.name}: {exc}')
            return False

        try:
            target.unlink()
        except OSError as exc:
            # The old file is still the authoritative copy.  Remove the newly
            # written sibling so a failed rename never leaves two entries.
            try:
                destination.unlink()
            except OSError:
                self.status.emit(
                    f'Could not remove {target.name} after writing '
                    f'{destination.name}: {exc}. Both files remain.')
            else:
                self.status.emit(f'Could not rename {target.name}: {exc}')
            self._refresh_saved()
            return False

        current = self.session.sequence
        if current is not None and current.path is not None:
            try:
                same_sequence = current.path.resolve() == target.resolve()
            except OSError:
                same_sequence = current.path == target
            if same_sequence:
                # Preserve edits currently open on the panel; renaming a saved
                # file must not reload its older on-disk curve over that work.
                current.name = clean_name
                current.path = destination
                self.session.set_sequence(current)

        self.status.emit(
            f"Renamed '{target.name[:-len(SEQUENCE_SUFFIX)]}' to "
            f"'{clean_name}'.")
        self._refresh_saved()
        return True

    def _delete_saved(self, path):
        """Remove one saved sequence from disk, once the operator confirms.

        The file goes for good -- there is no undo and nothing is moved to the
        recycle bin, because the sequence folder is the app's own and a
        half-deleted entry sitting in it would still be listed.  So the name is
        put in front of the operator first.

        What is on the panel is left exactly as it is, even when it was loaded
        from the file just deleted.  The curves on screen are the operator's
        working copy; discarding them because their origin was removed would
        throw away editing they may be about to save under another name.
        """
        target = Path(path)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle('Delete this sequence?')
        box.setText(f"Delete '{target.name[:-len('.fcseq.json')]}'?")
        box.setInformativeText(
            f'{target}\n\nThe file is removed from disk and cannot be '
            'recovered from here. Anything already loaded onto the panel '
            'stays there and can be saved again under a new name.')
        delete = box.addButton('Delete', QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not delete:
            return

        try:
            target.unlink()
        except OSError as exc:
            # Open in another program, or on a share that has gone away.  The
            # list is re-read anyway: if it went despite the error, saying it
            # did not would be worse than the error.
            self.status.emit(f'Could not delete {target.name}: {exc}')
        else:
            self.status.emit(f'Deleted {target.name}.')
        self._refresh_saved()

    def _confirm_start(self, sequence, mismatches):
        """Name the lines that disagree, and let the operator overrule it."""
        lines = '\n'.join(
            f"    {track.label}:  starts at {wanted:.3f} — now {actual:.3f} SLPM"
            for track, wanted, actual in mismatches[:8])
        more = ('\n    …and '
                f'{len(mismatches) - 8} more') if len(mismatches) > 8 else ''
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle('Flows do not match the start of this sequence')
        box.setText(f"'{sequence.name}' does not open where the rig is standing.")
        box.setInformativeText(
            f"{lines}{more}\n\nRunning it now writes those opening setpoints "
            "immediately, so each line above moves in one step at t = 0. Set "
            "the flows to match first, or run it anyway if the jump is "
            "acceptable.")
        play = box.addButton('Run anyway', QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is play

    # -- system log ------------------------------------------------------- #
    def _card_syslog(self):
        card = Card(
            'System Log', collapsed=True,
            help_text=('Show recent connection, control, logging, sequence, '
                       'and safety events. The newest 2,000 lines are kept.'))
        self.syslog = QPlainTextEdit()
        self.syslog.setReadOnly(True)
        self.syslog.setFixedHeight(theme.scale(140))
        # A run can log thousands of lines; without a cap the widget grows
        # without bound behind a collapsed header where nobody would see it.
        self.syslog.setMaximumBlockCount(2000)
        card.add(self.syslog)
        return card

    def _on_logged(self, channel, text):
        if channel == 'system':
            self.syslog.appendPlainText(text)

    # ------------------------------------------------------------------ #
    #  Right column                                                       #
    # ------------------------------------------------------------------ #
    def _build_right_column(self):
        # Scrolled, like the left column.  Readings are the one thing on this
        # screen that must never be squeezed: raise the base font and a plain
        # layout answers the shortfall by compressing every widget that will
        # allow it, which clips the setpoint entries rather than whitespace.
        scroll = self._scroll_column()
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(theme.PAD_SM + 2, theme.PAD_LG,
                                  theme.PAD_LG, theme.PAD_LG)
        column.setSpacing(theme.CARD_GAP)

        self._cards_card = Card(
            'Live Controller Readings & Manual Control', collapsible=False,
            help_text=('Review each assigned controller, enter individual '
                       'setpoints, and configure its remembered full scale and '
                       'ramp behavior.'))
        self._cards_view_buttons = {}
        for view, text in (('list', 'List'), ('grid', 'Grid')):
            button = QPushButton(text)
            button.setObjectName(
                'ControllerListView' if view == 'list'
                else 'ControllerGridView')
            button.setAccessibleName(f'Show controllers in {view} view')
            button.setCheckable(True)
            button.setProperty('density', 'compact')
            button.clicked.connect(
                lambda _checked=False, selected=view:
                self._set_cards_view(selected))
            self._cards_card.add_header_widget(button)
            self._cards_view_buttons[view] = button
        self._sync_cards_view_buttons()
        self._empty_note = label(
            'No controllers assigned yet — connect and assign them on the '
            'Connection tab.', color=theme.TEXT_DIM, size=9)
        self._empty_note.setWordWrap(True)
        self._cards_card.add(self._empty_note)

        # The cards live in their own holder rather than directly in the card
        # body, so a rebuild can empty one layout instead of picking the
        # controllers back out of a layout that also holds the empty note and
        # the trailing stretch.
        self._cards_holder = QWidget()
        self._cards_layout = QGridLayout(self._cards_holder)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setHorizontalSpacing(theme.PAD_SM)
        self._cards_layout.setVerticalSpacing(theme.PAD_SM)
        self._cards_layout.setColumnStretch(0, 1)
        self._cards_layout.setColumnStretch(1, 1)
        self._cards_card.add(self._cards_holder)
        self._cards_card.body_layout.addStretch(1)
        column.addWidget(self._cards_card, 1)

        self._combustion_card = self._build_combustion_staged()
        column.addWidget(self._combustion_card)
        self._combustion_std_card = self._build_combustion_standard()
        column.addWidget(self._combustion_std_card)
        scroll.setWidget(holder)
        return scroll

    # -- combustion estimate ---------------------------------------------- #
    #
    # Two cards, built once and shown one at a time.  They answer the same
    # question from the two shapes the rig comes in: the staged card reads the
    # RQL roles, so its numbers agree stage by stage with the ignition
    # sequence and the CSV; the standard card reads the assigned *gas* of
    # every controller, because a burner that is not staged has no roles and
    # three NH₃ lines into one inlet are simply three NH₃ lines.
    #
    # Everything on both is derived from flows the meters are already
    # reporting.  Nothing here is written to hardware, and nothing here
    # touches acquisition, logging or the ramps -- which is what makes it safe
    # to pause.

    def _tile_strip(self, specs, store, *, size=10):
        """A row of tiles from ``[(key, caption, colour)]``, kept in ``store``."""
        strip = QHBoxLayout()
        strip.setSpacing(theme.PAD_SM + 1)
        for key, caption, color in specs:
            if key is None:
                # A hairline between the measured flows and what was worked
                # out from them: two different kinds of number, and the eye
                # should not have to be told which is which.
                separator = QWidget()
                separator.setFixedWidth(1)
                separator.setStyleSheet(
                    'background-color: rgba(255, 255, 255, 24);')
                strip.addSpacing(theme.PAD_XS)
                strip.addWidget(separator)
                strip.addSpacing(theme.PAD_XS)
                continue
            tile = MetricTile(caption, color=color, size=size)
            tile.setSizePolicy(QSizePolicy.Policy.Preferred,
                               QSizePolicy.Policy.Preferred)
            strip.addWidget(tile, 1)
            store[key] = tile
        return strip

    def _build_combustion_staged(self):
        card = Card(
            'Combustion — Staged', collapsible=False,
            help_text=('The pilot share of Stage 1 fuel by volume, plus each '
                       "stage's live φ, power, and inlet bulk velocity. Enter "
                       'the two inlet cross-sectional areas to calculate '
                       'velocity. Nothing here is sent to the controllers.'))

        # Three compact columns preserve the original card height while still
        # giving each part of the burner a clear visual boundary.  The pilot
        # is deliberately separate from Stage 1 even though its methane is
        # included in the Stage 1 and global combustion balances.
        groups = QHBoxLayout()
        groups.setSpacing(theme.PAD_MD)
        groups.addWidget(self._combustion_group(
            'PILOT', 'SHARE OF STAGE 1 FUEL', (
                ('pilot_split', 'PILOT SPLIT  %', theme.TEXT_BRIGHT),
            )), 2)
        groups.addWidget(self._combustion_group(
            'STAGE 1', 'PILOT INCLUDED', (
                ('phi1', 'φ', theme.TEXT_BRIGHT),
                ('vel1', 'INLET VEL  m/s', theme.TEXT_BRIGHT),
                ('power1', 'POWER  kW', theme.TEXT_BRIGHT),
            )), 3)
        groups.addWidget(self._combustion_group(
            'STAGE 2', '', (
                ('phi2', 'φ', theme.TEXT_BRIGHT),
                ('vel2', 'INLET VEL  m/s', theme.TEXT_BRIGHT),
                ('power2', 'POWER  kW', theme.TEXT_BRIGHT),
            )), 3)
        card.add_layout(groups)

        for key in ('phi1', 'phi2'):
            self._combustion[key].value.setFont(mono(16, True))
        self._attach_combustion_menu(card, (
            (SCOPE_STAGE1, 'Stage 1 inlet'),
            (SCOPE_STAGE2, 'Stage 2 inlet'),
        ))
        return card

    def _combustion_group(self, title, subtitle, specs):
        """A compact horizontal group of the few values used during a run."""
        group = QFrame()
        group.setObjectName('CombustionStageCard')
        layout = QVBoxLayout(group)
        layout.setContentsMargins(theme.PAD_MD, theme.PAD_SM + 2,
                                  theme.PAD_MD, theme.PAD_MD)
        layout.setSpacing(theme.PAD_SM)

        heading = QHBoxLayout()
        heading.setSpacing(theme.PAD_SM)
        heading.addWidget(label(title, color=theme.TEXT_BRIGHT, size=9, bold=True,
                                object_name='CombustionGroupTitle'))
        if subtitle:
            heading.addWidget(label(
                subtitle, color=theme.TEXT_MUTED, size=7,
                object_name='CombustionGroupSubtitle'))
        heading.addStretch(1)
        layout.addLayout(heading)

        layout.addLayout(self._tile_strip(specs, self._combustion, size=12))
        return group

    def _build_combustion_standard(self):
        card = Card(
            'Combustion', collapsible=False,
            help_text=('Live φ, power, and inlet bulk velocity for the gases '
                       'assigned to the standard single-inlet rig. Use the '
                       'menu to set inlet diameter and estimate refresh.'))
        strip = self._tile_strip((
            ('phi', 'φ', theme.TEXT_BRIGHT),
            ('vel', 'INLET VEL  m/s', theme.TEXT_BRIGHT),
            ('power', 'POWER  kW', theme.TEXT_BRIGHT),
        ), self._combustion_std, size=12)
        self._combustion_std['phi'].value.setFont(mono(16, True))
        card.add_layout(strip)
        self._attach_combustion_menu(card, ((SCOPE_ALL, 'Inlet'),))
        return card

    def _attach_combustion_menu(self, card, scopes):
        """Put inlet geometry and refresh controls behind a header menu."""
        button = QPushButton('☰')
        button.setObjectName('CardMenuButton')
        button.setAccessibleName('Combustion estimate settings')
        button.setToolTip('Inlet geometry and live-estimate settings')
        button.setFixedSize(theme.scale(30), theme.scale(26))

        menu = QMenu(button)
        menu.setObjectName('CombustionSettingsMenu')
        panel = QWidget()
        panel.setObjectName('CombustionMenuPanel')
        panel.setMinimumWidth(theme.scale(360))
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(theme.PAD_MD, theme.PAD_MD,
                                  theme.PAD_MD, theme.PAD_MD)
        layout.setSpacing(theme.PAD_SM)
        layout.addWidget(label('ESTIMATE SETTINGS', color=theme.TEXT_BRIGHT,
                               size=8, bold=True))

        for scope, caption in scopes:
            geometry = QWidget()
            geometry.setObjectName('Row')
            line = QHBoxLayout(geometry)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(theme.PAD_SM)
            line.addWidget(label(caption, color=theme.TEXT_MUTED, size=8))
            geometry_mode = QComboBox()
            geometry_mode.setObjectName('CombustionGeometryMode')
            geometry_mode.addItem('Diameter', GEOMETRY_DIAMETER)
            geometry_mode.addItem('Area', GEOMETRY_AREA)
            geometry_mode.setFixedWidth(theme.scale(98))
            geometry_mode.setToolTip(
                'Use diameter for a circular inlet, or enter the actual '
                'cross-sectional area for square and other inlet shapes.')
            geometry_mode.currentIndexChanged.connect(
                lambda _index, scope=scope, combo=geometry_mode:
                self._on_combustion_geometry(scope, combo.currentData()))
            self._combustion_geometry_combos.setdefault(
                scope, []).append(geometry_mode)
            line.addWidget(geometry_mode)
            line.addStretch(1)
            entry = QLineEdit()
            entry.setObjectName('CombustionGeometryInput')
            entry.setProperty('scope', scope)
            entry.setFixedWidth(theme.scale(72))
            entry.setAlignment(Qt.AlignmentFlag.AlignRight)
            entry.setPlaceholderText('—')
            entry.setToolTip(
                'Circular diameter in mm, or actual cross-sectional area in '
                'mm². This value is used only for the bulk-velocity estimate.')
            entry.editingFinished.connect(
                lambda scope=scope, entry=entry:
                self._on_combustion_geometry_value(scope, entry.text()))
            self._combustion_geometry_entries.setdefault(scope, []).append(entry)
            line.addWidget(entry)
            units = label('mm', color=theme.TEXT_MUTED, size=8)
            units.setFixedWidth(theme.scale(30))
            self._combustion_geometry_units.setdefault(scope, []).append(units)
            line.addWidget(units)
            layout.addWidget(geometry)
            if scope == SCOPE_STAGE2:
                count_row = QWidget()
                count_row.setObjectName('Row')
                count_line = QHBoxLayout(count_row)
                count_line.setContentsMargins(0, 0, 0, 0)
                count_line.setSpacing(theme.PAD_SM)
                count_line.addWidget(label('Number of Stage 2 inlets',
                                           color=theme.TEXT_MUTED, size=8))
                count_line.addStretch(1)
                count = QSpinBox()
                count.setObjectName('CombustionInletCountInput')
                count.setRange(1, MAX_INLET_COUNT)
                count.setFixedWidth(theme.scale(72))
                count.setAlignment(Qt.AlignmentFlag.AlignRight)
                count.setToolTip(
                    'Number of identical Stage 2 inlets. Bulk velocity uses '
                    'the entered per-inlet area multiplied across this many '
                    'identical inlets.')
                count.valueChanged.connect(
                    lambda value, scope=scope:
                    self.session.set_combustion_inlets(scope, value))
                self._combustion_inlet_spins.setdefault(scope, []).append(count)
                count_line.addWidget(count)
                layout.addWidget(count_row)
            area = label('AREA  — mm²', color=theme.TEXT_MUTED, size=7,
                         monospace=True)
            area.setAlignment(Qt.AlignmentFlag.AlignRight)
            self._combustion_area_labels.setdefault(scope, []).append(area)
            layout.addWidget(area)

        layout.addWidget(divider())
        estimate = QWidget()
        estimate.setObjectName('Row')
        estimate_line = QHBoxLayout(estimate)
        estimate_line.setContentsMargins(0, 0, 0, 0)
        estimate_line.setSpacing(theme.PAD_SM)
        live = QCheckBox('Compute live')
        live.setToolTip(
            'Uncheck to stop recomputing and redrawing this card.\n'
            'Acquisition, logging, ramps and the sequence are unaffected.')
        live.toggled.connect(self._on_combustion_live)
        self._combustion_live_boxes.append(live)
        estimate_line.addWidget(live)
        estimate_line.addStretch(1)

        combo = QComboBox()
        for caption, passes in COMBUSTION_RATES:
            combo.addItem(caption, passes)
        combo.setFixedWidth(theme.scale(118))
        combo.setToolTip('How often the card refreshes, in acquisition passes.')
        combo.currentIndexChanged.connect(
            lambda _index, combo=combo:
            self.session.set_combustion_interval(combo.currentData()))
        self._combustion_rate_combos.append(combo)
        estimate_line.addWidget(combo)
        layout.addWidget(estimate)

        action = QWidgetAction(menu)
        action.setDefaultWidget(panel)
        menu.addAction(action)
        button.setMenu(menu)
        card.add_header_widget(button)
        self._combustion_menus.append(menu)

    # -- combustion: operator input ---------------------------------------- #
    def _on_combustion_geometry(self, scope, mode):
        self.session.set_combustion_geometry(scope, mode)
        self._apply_combustion_prefs()

    def _on_combustion_geometry_value(self, scope, text):
        """Clean a diameter/area entry, then redraw every copy of it."""
        value = text.strip() or None
        if self.session.combustion_geometry(scope) == GEOMETRY_AREA:
            self.session.set_combustion_area(scope, value)
        else:
            self.session.set_combustion_diameter(scope, value)
        # Unconditionally, not only when the session announces a change: a
        # figure that cleaned to the one already in force still has to replace
        # whatever was typed, or the box keeps showing a number nothing uses.
        self._apply_combustion_prefs()

    def _on_combustion_live(self, running):
        self.session.set_combustion_live(running)
        self._apply_combustion_prefs()

    def _apply_combustion_prefs(self):
        """Draw the stored settings onto both cards, then catch the tiles up."""
        for scope, entries in self._combustion_geometry_entries.items():
            mode = self.session.combustion_geometry(scope)
            value = (self.session.combustion_area(scope)
                     if mode == GEOMETRY_AREA
                     else self.session.combustion_diameter(scope))
            text = '' if value is None else f'{value:g}'
            for entry in entries:
                if entry.text() != text:
                    entry.setText(text)
        for scope, combos in self._combustion_geometry_combos.items():
            mode = self.session.combustion_geometry(scope)
            for combo in combos:
                combo.blockSignals(True)
                combo.setCurrentIndex(combo.findData(mode))
                combo.blockSignals(False)
        for scope, units in self._combustion_geometry_units.items():
            unit_text = ('mm²' if self.session.combustion_geometry(scope)
                         == GEOMETRY_AREA else 'mm')
            for unit in units:
                unit.setText(unit_text)
        for scope, spins in self._combustion_inlet_spins.items():
            count = self.session.combustion_inlets(scope)
            for spin in spins:
                spin.blockSignals(True)
                spin.setValue(count)
                spin.blockSignals(False)
        for scope, labels in self._combustion_area_labels.items():
            area = self.session.combustion_area(scope)
            if area is None:
                text = 'AREA  — mm²'
            elif scope == SCOPE_STAGE2:
                count = self.session.combustion_inlets(scope)
                text = (f'AREA  {area:.4g} mm² × {count} = '
                        f'{area * count:.4g} mm² TOTAL')
            else:
                text = f'AREA  {area:.4g} mm²'
            for area_label in labels:
                area_label.setText(text)
        live = self.session.combustion_live
        for box in self._combustion_live_boxes:
            # Blocked, because these are being set *from* the stored state:
            # letting them report back would write the setting to itself and,
            # on a failed save, log the same line every time the card is drawn.
            box.blockSignals(True)
            box.setChecked(live)
            box.blockSignals(False)
        interval = self.session.combustion_interval
        for combo in self._combustion_rate_combos:
            index = combo.findData(interval)
            if index < 0:
                # A hand-edited settings file can hold a figure the menu does
                # not offer.  Show it rather than quietly rounding it to one
                # that is on the list.
                combo.addItem(f'every {interval} passes', interval)
                index = combo.count() - 1
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.setEnabled(live)
            combo.blockSignals(False)
        self._refresh_combustion(self.session.live_samples(), force=True)

    # -- combustion: live numbers ------------------------------------------ #
    def _refresh_combustion(self, samples, force=False, flows=None):
        """Recompute whichever card is showing -- or decline to.

        Declining is the point of the switch above: the arithmetic is a few
        dozen floating-point operations, but redrawing a dozen tiles at the
        acquisition rate on a machine already drawing the graph is not free.
        A forced refresh is one the operator asked for by other means -- a
        mode change, a new bore, unpausing -- and those are never skipped.
        """
        if not self.session.combustion_live:
            if not self._combustion_paused:
                self._combustion_paused = True
                self._show_combustion_paused()
            return
        self._combustion_tick += 1
        if self._combustion_paused:
            # Coming back from paused with stale figures on screen; whatever
            # the interval says, this pass is drawn.
            force = True
            self._combustion_paused = False
        interval = self.session.combustion_interval
        if not force and interval > 1 and self._combustion_tick % interval:
            return
        if self.session.is_staged:
            self._refresh_combustion_staged(samples, flows)
        else:
            self._refresh_combustion_standard(samples)

    def _show_combustion_paused(self):
        """Blank the derived numbers rather than leave a frozen one on screen.

        A stale number on a live card is worse than no number: it reads as the
        rig's current state and is not.
        """
        for store in (self._combustion, self._combustion_std):
            for key, widget in store.items():
                if key.startswith('summary'):
                    widget.setText('estimate paused — tick “Compute live” '
                                   'to resume')
                else:
                    widget.set_value('--')

    def _refresh_combustion_staged(self, samples, flows=None):
        if flows is None:
            flows = {key: self.session.flow_for_role(key, samples)
                     for key, _role_label in roles.ROLES}
        pilot = max(0.0, flows.get('ch4_pilot', 0.0))
        stage1_fuel = pilot + sum(
            max(0.0, flows.get(key, 0.0))
            for key in ('nh3_rich', 'h2_rich', 'ch4_stage1'))
        pilot_split = None if stage1_fuel <= 0.0 else pilot / stage1_fuel * 100.0
        self._combustion['pilot_split'].set_value(
            '--' if pilot_split is None else f'{pilot_split:.1f}')

        stage1 = self.session.combustion_estimate(SCOPE_STAGE1, samples)
        stage2 = self.session.combustion_estimate(SCOPE_STAGE2, samples)
        for key, value in (('phi1', stage1.phi), ('phi2', stage2.phi)):
            self._combustion[key].set_value(_fmt(value))
        self._combustion['power1'].set_value(_fmt(stage1.power_kw))
        self._combustion['power2'].set_value(_fmt(stage2.power_kw))
        self._combustion['vel1'].set_value(_fmt(stage1.velocity))
        self._combustion['vel2'].set_value(_fmt(stage2.velocity))

    def _refresh_combustion_standard(self, samples):
        estimate = self.session.combustion_estimate(SCOPE_ALL, samples)
        self._combustion_std['phi'].set_value(_fmt(estimate.phi))
        self._combustion_std['power'].set_value(_fmt(estimate.power_kw))
        self._combustion_std['vel'].set_value(_fmt(estimate.velocity))

    # -- live cards ------------------------------------------------------- #
    def _sync_cards_view_buttons(self):
        """Make the two header buttons read as one exclusive view switch."""
        for view, button in self._cards_view_buttons.items():
            active = view == self._cards_view
            button.setChecked(active)
            button.setProperty('variant', 'accent' if active else 'quiet')
            button.style().unpolish(button)
            button.style().polish(button)

    def _set_cards_view(self, view):
        """Switch between full-width rows and compact two-column cards."""
        if view not in ('list', 'grid'):
            return
        if view == self._cards_view:
            self._sync_cards_view_buttons()
            return
        self._cards_view = view
        self.session.controller_cards_view = view
        self._sync_cards_view_buttons()
        self._rebuild_cards()

    def _rebuild_cards(self):
        """Rebuild the controller cards from the current assignment.

        Rebuilt rather than patched because a reassignment can change which
        controllers exist, what they are called and which stage they belong
        to all at once, and a card left over from the previous assignment
        would keep accepting setpoints for a role it no longer fills.
        """
        # Carried across the rebuild: an SP box holds a number the operator
        # typed and has not sent yet, and losing it because they switched
        # mode or moved somebody else's zone would cost them the reading of
        # the number, which is the part that takes care.
        pending = {unit: card.entry.text() for unit, card in self._cards.items()}
        while self._cards_layout.count():
            item = self._cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._cards = {}
        self._card_keys = {}
        self._stage_headers = {}

        metas = self.session.track_metas()
        self._empty_note.setVisible(not metas)
        self._cards_holder.setVisible(bool(metas))
        if not metas:
            return

        by_key = {meta.key: meta for meta in metas}
        if self.session.is_staged:
            groups = [(title, [by_key[key] for key in keys if key in by_key])
                      for title, keys in roles.STAGES]
            others = [meta for meta in metas
                      if meta.key not in roles.ROLE_LABELS]
            if others:
                groups.append(('Other Controllers', others))
        else:
            # Standard mode has no stages to group by, so the cards go in one
            # list in rig order.  Inventing a grouping here would be claiming
            # a structure the operator has explicitly said is not there.
            groups = [('Controllers', metas)]

        grid_view = self._cards_view == 'grid'
        layout_row = 0
        for title, group in groups:
            if not group:
                continue
            header = StageHeader(title)
            self._cards_layout.addWidget(header, layout_row, 0, 1, 2)
            layout_row += 1
            self._stage_headers[title] = header
            if grid_view:
                for index, meta in enumerate(group):
                    card = self._add_card(meta, compact=True)
                    self._cards_layout.addWidget(
                        card, layout_row + index // 2, index % 2)
                layout_row += (len(group) + 1) // 2
            else:
                for meta in group:
                    card = self._add_card(meta)
                    self._cards_layout.addWidget(card, layout_row, 0, 1, 2)
                    layout_row += 1
        for unit, text in pending.items():
            card = self._cards.get(unit)
            if card is not None:
                card.entry.setText(text)
        self._refresh_readings(force=True)

    def _add_card(self, meta, *, compact=False):
        color = theme.GAS_COLORS.get(meta.gas, theme.TEXT)
        _gas, zone = self.session.selection.get(meta.unit, ('', ''))
        scale = self._scale_for(meta)
        # Unit and zone, not the role label: the gas is already the card's
        # title and the stage is already the group header, whereas the zone is
        # the one thing on this card the operator can now change at runtime.
        caption = f'Unit {meta.unit}'
        if zone:
            caption += f'  ·  {zone}'
        card = UnitCard(meta.unit, meta.gas or '?', color, scale,
                        caption=caption,
                        declared_scale=self.session.full_scale_for(meta.unit),
                        declared_ramp=self.session.ramp_rate_for(meta.unit),
                        declared_ramp_off=self.session.ramp_disabled_for(
                            meta.unit),
                        declared_max_flow=self.session.max_flow_for(meta.unit),
                        compact=compact)
        card.setToolTip(f'{meta.label} — unit {meta.unit}')
        card.setpoint_requested.connect(self._on_setpoint)
        card.full_scale_requested.connect(self._on_full_scale)
        card.ramp_rate_requested.connect(self._on_ramp_rate)
        card.max_flow_requested.connect(self._on_max_flow)
        card.ramp_off_requested.connect(self._on_ramp_off)
        self._cards[meta.unit] = card
        self._card_keys[meta.unit] = meta.key
        return card

    def _scale_for(self, meta):
        """A bar scale from what this rig is being asked for.

        The controllers do not report their full scale over the wire, so there
        is no honest figure to read; the largest thing this role has been asked
        for is the closest thing to one, rounded up by
        :func:`~flow_controller.domain.graphing.auto_bar_span` so that the peak
        itself is not the end of the track.  ``FlowBar`` floors the span by the
        live reading as well, so a reading past the span draws a full bar rather
        than one running off the end of the card.
        """
        candidates = [self._peaks.get(meta.unit, 0.0),
                      self.session.target_flows.get(meta.key, 0.0)]
        sample = self.session.live_samples().get(meta.unit) or {}
        for field in ('sp', 'flow'):
            try:
                candidates.append(float(sample.get(field) or 0.0))
            except (TypeError, ValueError):
                pass
        peak = max(candidates)
        self._peaks[meta.unit] = peak
        span = auto_bar_span(peak)
        self._scales[meta.unit] = span
        return span

    def _on_setpoint(self, unit, value):
        key = self._card_keys.get(unit)
        if key is None:
            return
        self.session.set_role_setpoint(key, value)

    def _on_full_scale(self, unit, value):
        """The operator declared this meter's full scale, or asked for auto."""
        self.session.set_full_scale(unit, value)
        card = self._cards.get(unit)
        if card is not None:
            # The session cleans the figure -- a typo past the ceiling comes
            # back clamped -- so the box shows what is actually in force.
            card.set_declared_scale(self.session.full_scale_for(unit))
        self._refresh_readings(force=True)

    def _on_ramp_rate(self, unit, value):
        """The operator declared how fast this line may move, or asked for none."""
        self.session.set_ramp_rate(unit, value)
        card = self._cards.get(unit)
        if card is not None:
            card.set_declared_ramp(self.session.ramp_rate_for(unit))

    def _on_max_flow(self, unit, value):
        """The operator declared this unit's command ceiling, or cleared it."""
        self.session.set_max_flow(unit, value)
        card = self._cards.get(unit)
        if card is not None:
            card.set_declared_max_flow(self.session.max_flow_for(unit))

    def _on_ramp_off(self, unit, off):
        """The operator turned this controller's ramping off, or back on.

        The stored rate is left alone: turning ramping back on should bring
        back the figure that was typed, not leave the operator to remember it.
        """
        self.session.set_ramp_disabled(unit, off)
        card = self._cards.get(unit)
        if card is not None:
            card.set_ramp_disabled(self.session.ramp_disabled_for(unit))

    # ------------------------------------------------------------------ #
    #  Live updates                                                       #
    # ------------------------------------------------------------------ #
    def _on_samples(self, _generation):
        # A tab nobody is looking at still receives every sample.  Repainting
        # it ten times a second to show numbers to an empty screen is the
        # kind of cost that makes a monitoring app feel heavy, and the show
        # event catches the display up the moment it matters again.
        if self.isVisible():
            self._refresh_readings()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_readings(force=True)

    def _refresh_readings(self, force=False):
        samples = self.session.live_samples()
        flows = {}
        for unit, card in self._cards.items():
            sample = samples.get(unit) or {}
            flow = sample.get('flow')
            setpoint = sample.get('sp')
            live = flow is not None
            key = self._card_keys[unit]
            # Zero into the sums below, because a line that did not answer
            # contributes nothing to a total -- but the raw reading goes to
            # the card, so it can say "no answer" instead of drawing the same
            # zero a closed controller would report.
            flows[key] = float(flow or 0.0)
            # The bar spans a round figure above the largest thing this line has
            # been asked for, stored target included.  Spanning the peak exactly
            # is what made every meter read full: the first reading of a fresh
            # session is its own peak, so the bar was pegged before the rig had
            # done anything.
            peak = max(self._peaks.get(unit, 0.0), float(setpoint or 0.0),
                       flows[key],
                       float(self.session.target_flows.get(key, 0.0)))
            if peak > self._peaks.get(unit, 0.0):
                self._peaks[unit] = peak
                span = auto_bar_span(peak)
                if span != self._scales.get(unit):
                    self._scales[unit] = span
                    card.set_scale(span)
            card.update_readings(flow, setpoint, sample.get('press'),
                                 sample.get('temp'), live=live)

        self._refresh_combustion(samples, force=force, flows=flows)
        if not self.session.is_staged:
            return
        for title, keys in roles.STAGES:
            header = self._stage_headers.get(title)
            if header is None:
                continue
            fuel = sum(flows.get(key, 0.0) for key in keys
                       if key in roles.FUEL_KEYS)
            air = sum(flows.get(key, 0.0) for key in keys
                      if key in roles.AIR_KEYS)
            summary = f'fuel {fuel:6.2f}'
            if air:
                summary += f'    air {air:7.2f}'
            header.set_summary(f'{summary}  SLPM')

    # ------------------------------------------------------------------ #
    #  Session state                                                      #
    # ------------------------------------------------------------------ #
    def _on_mode(self, mode):
        staged = mode == MODE_STAGED
        for value, button in self._mode_buttons.items():
            button.setChecked(value == mode)
            button.setProperty('variant', 'accent' if value == mode else None)
            # A QSS selector keyed on a property does not re-evaluate on its
            # own when the property changes.
            button.style().unpolish(button)
            button.style().polish(button)
        self._autocalc_card.setVisible(staged)
        self._combustion_card.setVisible(staged)
        self._combustion_std_card.setVisible(not staged)
        # The card being uncovered has whatever the other mode last left on
        # it, and the interval must not make it wait to be corrected.
        self._combustion_paused = False
        # Auto-calculate remains the staged setup step. Sequences are a named
        # workspace rather than the next numbered instruction, so its title
        # deliberately has no badge in either operating mode.
        self._autocalc_card.set_index(1 if staged else None)
        self._sequence_card.set_index(None)
        self._rebuild_cards()

    def _on_connection(self, connected):
        self.monitor_btn.setEnabled(connected)
        if not connected:
            self.monitor_btn.setText('Start Monitoring')
        self._rebuild_cards()

    def _on_monitoring(self, monitoring):
        self.monitor_btn.setText('Stop Monitoring' if monitoring
                                 else 'Start Monitoring')
        self.monitor_btn.setProperty('variant',
                                     'danger' if monitoring else 'accent')
        self.monitor_btn.style().unpolish(self.monitor_btn)
        self.monitor_btn.style().polish(self.monitor_btn)

    def _on_logging(self, active, path):
        self.start_log_btn.setEnabled(not active)
        self.stop_log_btn.setEnabled(active)
        self.log_state.setText('RECORDING' if active else 'OFF')
        self.log_state.setStyleSheet(
            f'color: {theme.OK if active else theme.TEXT_DIM};'
            ' background: transparent;')
        if path:
            self.log_path.setText(str(path))
        self.log_path.setEnabled(not active)

    def _on_udp(self, active, message):
        self.udp_btn.setText('Stop Listener' if active else 'Start Listener')
        self.udp_state.setText(message)
        self.udp_state.setStyleSheet(
            f'color: {theme.OK if active else theme.TEXT_DIM};'
            ' background: transparent;')
