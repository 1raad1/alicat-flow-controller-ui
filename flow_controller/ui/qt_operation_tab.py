"""The Operation & Monitoring tab: the screen a run is actually driven from.

Two columns over a collapsible sequence panel.  The left column holds the
things the operator sets up -- logging, targets, the ignition sequence -- and
the right column holds what the rig is doing about it.  They are separated
because they change on completely different timescales: the left column is
touched a handful of times per run, the right column changes ten times a
second, and interleaving them would put a live number next to every button.

**Progressive disclosure by operating mode.**  Staged (RQL) mode shows the
auto-calculate card, the ignition sequence, the stage grouping and the
equivalence-ratio strip.  Standard mode shows none of them, because none of
them mean anything without two zones and a pilot: a φ computed from a rig
that is not staged is a number with no referent, and an ignition ramp keyed
by role would address controllers that have no role.  Hiding them is not
tidying -- :meth:`FlowSession.ready_ignition` refuses in standard mode too,
so the screen and the session agree rather than the screen merely being
polite about it.

Nothing here writes to hardware directly.  Every setpoint goes out through
:meth:`FlowSession.set_role_setpoint`, which ramps the lines that must be
ramped and honours the zero lock.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFileDialog, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QSplitter,
                               QVBoxLayout, QWidget)

from ..core.sequence import opening_mismatches
from ..core.session import MODE_STAGED, MODE_STANDARD, SEQ_IDLE
from ..domain import roles, rql
from ..domain.graphing import auto_bar_span
from . import qt_theme as theme
from .qt_sequence_panel import SequencePanel
from .qt_widgets import (Card, MetricTile, StageHeader, StateBanner, UnitCard,
                         divider, field_grid, label, row)

#: Roles the auto-calculation produces a target for, in tile order.
TARGET_KEYS = ('nh3_rich', 'h2_rich', 'nh3_lean',
               'h2_lean', 'rich_air', 'lean_air')

#: Short captions for the tiles, where the full role label is too wide.
SHORT_LABELS = {
    'nh3_rich': 'NH3-1', 'h2_rich': 'H2-1', 'rich_air': 'AIR-1',
    'nh3_lean': 'NH3-2', 'h2_lean': 'H2-2', 'lean_air': 'AIR-2',
    'ch4_pilot': 'CH4',
}

#: Ignition banner kinds emitted by the session, mapped to the banner's own
#: vocabulary.  The session speaks about the *sequence*; the banner speaks
#: about colour.
BANNER_KINDS = {'pre': 'ready', 'ignited': 'running', 'ok': 'running',
                'warn': 'ready', 'error': 'fault'}

DEFAULT_LOG_DIR = Path.home() / 'Documents' / 'Flow Controller'

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
    """The two zero commands, built to sit in the tab strip's corner.

    It lives in this module because it is part of the operation surface, but
    it is mounted by the main window rather than by the tab: an emergency
    control that is only reachable from the tab you happen to be on is not an
    emergency control.
    """

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.PAD_MD, theme.PAD_XS,
                                  theme.PAD_LG, theme.PAD_XS)
        layout.setSpacing(theme.PAD_SM)

        self.buttons = {}
        for text, handler in (('ZERO FUEL', session.zero_fuel),
                              ('ZERO ALL', session.zero_all)):
            button = QPushButton(text)
            button.setObjectName('SafetyButton')
            # Wrapped rather than connected straight through: ``clicked``
            # carries a checked flag and these commands take no argument.
            button.clicked.connect(lambda _checked=False, call=handler: call())
            layout.addWidget(button)
            self.buttons[text] = button

        session.estop_armed_changed.connect(self._set_armed)
        self._set_armed(session.estop_armed)

    def _set_armed(self, armed):
        for button in self.buttons.values():
            button.setEnabled(bool(armed))
        self.setToolTip('' if armed else
                        'Connect the flow meters to arm the zero commands.')


# ---------------------------------------------------------------------- #
#  Operation tab                                                          #
# ---------------------------------------------------------------------- #
class OperationTab(QWidget):
    """Everything an operator touches between connecting and shutting down."""

    #: Emitted when the operator asks for a log file that does not resolve,
    #: so the window can put it in the status bar rather than a dialog.
    status = Signal(str)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
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
        self._pending_generation = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

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
        session.poll_rate.connect(self._on_poll_rate)
        session.banner.connect(self._on_banner)
        session.ignition_changed.connect(self._on_ignition)
        session.logging_changed.connect(self._on_logging)
        session.udp_changed.connect(self._on_udp)
        session.logged.connect(self._on_logged)
        session.ramp_progress.connect(self._on_ramp)
        # A sequence just saved has to appear in the quick-play list without the
        # operator hunting for a refresh, and the list must not offer a second
        # run while one is already going out.
        session.sequence_saved.connect(lambda _path: self._refresh_saved())
        session.sequence_state_changed.connect(self._on_sequence_state)

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
        self._on_ignition(session.ignition_state)
        self._on_logging(session.logging_active, session.log_path)

    # ------------------------------------------------------------------ #
    #  Header                                                             #
    # ------------------------------------------------------------------ #
    def _build_header(self):
        holder = QWidget()
        holder.setObjectName('Row')
        bar = QHBoxLayout(holder)
        bar.setContentsMargins(theme.PAD_LG, theme.PAD_SM,
                               theme.PAD_LG, theme.PAD_SM)
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

        bar.addSpacing(theme.PAD_LG)
        self.monitor_btn = QPushButton('Start Monitoring')
        self.monitor_btn.setProperty('variant', 'accent')
        self.monitor_btn.clicked.connect(
            lambda: self.session.toggle_monitoring())
        bar.addWidget(self.monitor_btn)
        self._poll_label = label('poll  —', color=theme.TEXT_MUTED, size=8,
                                 monospace=True)
        bar.addWidget(self._poll_label)

        bar.addStretch(1)
        return holder

    def _on_sequence_state(self, state):
        """Grey the quick-play list while a recording or replay owns the clock."""
        self.saved_list.setEnabled(state == SEQ_IDLE)

    def _toggle_sequence(self, shown):
        self.sequence_btn.setText(('▾  Record / Replay Sequence' if shown
                                   else '▸  Record / Replay Sequence'))
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
        scroll = self._scroll_column()
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(theme.PAD_LG, theme.PAD_LG,
                                  theme.PAD_SM + 2, theme.PAD_LG)
        column.setSpacing(theme.CARD_GAP)
        column.addWidget(self._card_logging())
        self._autocalc_card = self._card_autocalc()
        column.addWidget(self._autocalc_card)
        self._ignition_card = self._card_ignition()
        column.addWidget(self._ignition_card)
        self._batch_card = self._card_batch()
        column.addWidget(self._batch_card)
        self._sequence_card = self._card_sequence()
        column.addWidget(self._sequence_card)
        column.addWidget(self._card_syslog())
        column.addStretch(1)
        scroll.setWidget(holder)
        # Wide enough that the fixed-width entry fields never force a clip;
        # the splitter can still be dragged wider.
        scroll.setMinimumWidth(holder.sizeHint().width() + 26)
        return scroll

    # -- logging --------------------------------------------------------- #
    def _card_logging(self):
        card = Card('Logging & Acquisition')

        self.log_path = QLineEdit(str(DEFAULT_LOG_DIR / 'run.csv'))
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

        hint = QLabel('The CSV columns are written from the assignment in '
                      'force when logging starts, so zones cannot be moved '
                      'while a log is open.')
        hint.setObjectName('Hint')
        hint.setWordWrap(True)
        card.add(hint)
        return card

    def _browse_log(self):
        DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path, _filter = QFileDialog.getSaveFileName(
            self, 'Log file', self.log_path.text() or str(DEFAULT_LOG_DIR),
            'CSV files (*.csv);;Excel workbooks (*.xlsx);;All files (*)')
        if path:
            self.log_path.setText(path)

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
        card = Card('Auto-Calculate Flows', index=1)
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
        self._refresh_readings()

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

    # -- ignition --------------------------------------------------------- #
    def _card_ignition(self):
        card = Card('Ignition Sequence', index=2)
        self.banner = StateBanner()
        card.add(self.banner)

        grid, entries = field_grid([
            ('Fuel pre-ignition (%)', 80), ('Air pre-ignition (%)', 80),
            ('Ramp steps', 10), ('Step interval (s)', 0.5),
        ], width=64)
        self._ignition_fields = entries
        card.add_layout(grid)

        self.step1 = QPushButton('STEP 1 — Pre-Ignition   (set scaled flows)')
        self.step1.setProperty('variant', 'ready')
        self.step1.clicked.connect(self._pre_ignition)
        card.add(self.step1)

        self.step2 = QPushButton('STEP 2 — Ignite   (ramp to target flows)')
        self.step2.setProperty('variant', 'accent')
        self.step2.setEnabled(False)
        self.step2.clicked.connect(self._ignite)
        card.add(self.step2)

        card.add(divider())
        stage = QPushButton('Stage Targets to SP Fields   (no flows sent)')
        stage.setProperty('variant', 'quiet')
        stage.clicked.connect(self._stage_targets)
        card.add(stage)
        return card

    def _pre_ignition(self):
        fields = self._ignition_fields
        self.session.ready_ignition(
            _number(fields['Fuel pre-ignition (%)']) / 100.0,
            _number(fields['Air pre-ignition (%)']) / 100.0,
            int(_number(fields['Ramp steps'], 10)),
            _number(fields['Step interval (s)'], 0.5))

    def _ignite(self):
        fields = self._ignition_fields
        self.session.ignite(int(_number(fields['Ramp steps'], 10)),
                            _number(fields['Step interval (s)'], 0.5))

    def _stage_targets(self):
        """Put the stored targets in the SP boxes without sending anything.

        Two steps rather than one on purpose: the operator gets to read every
        number that is about to be commanded, in the same place they would
        have typed it, before any of it leaves the machine.
        """
        staged = 0
        for key, target in self.session.target_flows.items():
            unit = self.session.unit_for_role(key)
            card = self._cards.get(unit)
            if card is not None:
                card.entry.setText(f'{target:.2f}')
                staged += 1
        self.status.emit(f'{staged} target(s) staged into the SP fields. '
                         'No flows sent.')

    def _on_ignition(self, state):
        self.step2.setEnabled(state == 'PRE_IGNITION')
        if state == 'IDLE':
            self.banner.set_state('idle', 'IDLE — calculate targets first')

    def _on_banner(self, text, kind):
        self.banner.set_state(BANNER_KINDS.get(kind, 'idle'), text)

    def _on_ramp(self, key, percent):
        self.status.emit(
            f'{roles.ROLE_LABELS.get(key, key)}: ramping {percent}%')

    # -- batch ------------------------------------------------------------ #
    def _card_batch(self):
        card = Card('Batch Control', index=3)
        send_all = QPushButton("Set All Flows Together   (send every card's SP)")
        send_all.clicked.connect(self._send_all)
        card.add(send_all)

        zero = QPushButton('Zero All Flows   (monitoring continues)')
        zero.setProperty('variant', 'danger')
        zero.clicked.connect(lambda: self.session.zero_all())
        card.add(zero)

        hint = QLabel('Air and pilot lines are always approached as a ramp, '
                      'whichever button sends them — a step change there is a '
                      'pressure transient into the burner.')
        hint.setObjectName('Hint')
        hint.setWordWrap(True)
        card.add(hint)
        return card

    def _send_all(self):
        for card in self._cards.values():
            card._emit_setpoint()

    # -- sequence --------------------------------------------------------- #
    def _card_sequence(self):
        card = Card('Sequence', index=4)
        self.sequence_btn = QPushButton('▸  Record / Replay Sequence')
        self.sequence_btn.setCheckable(True)
        self.sequence_btn.setProperty('variant', 'quiet')
        self.sequence_btn.toggled.connect(self._toggle_sequence)
        card.add(self.sequence_btn)

        hint = QLabel('Captures every setpoint the session commands while the '
                      'monitor is running, so the curve can be edited and the '
                      'transition replayed or repeated.')
        hint.setObjectName('Hint')
        hint.setWordWrap(True)
        card.add(hint)

        card.add(divider())
        card.add(label('SAVED SEQUENCES  —  CLICK TO RUN ONCE',
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
        self.saved_list.itemClicked.connect(self._on_saved_clicked)
        card.add(self.saved_list)

        saved_hint = QLabel('One pass, no repeats. A sequence is only started '
                            'if the rig is already standing at the flows it '
                            'opens with; otherwise you are told which lines '
                            'disagree.')
        saved_hint.setObjectName('Hint')
        saved_hint.setWordWrap(True)
        card.add(saved_hint)
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
            item = QListWidgetItem(path.name[:-len('.fcseq.json')])
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(f'{path}\n\nClick to load and run this sequence '
                            'once, from the top.')
            self.saved_list.addItem(item)
        if not paths:
            note = QListWidgetItem('No saved sequences yet')
            note.setFlags(Qt.ItemFlag.NoItemFlags)
            self.saved_list.addItem(note)

    def _on_saved_clicked(self, item):
        """Load one saved sequence and run it once, if the rig is where it starts.

        The check is on the *measured* flows rather than on the commanded ones.
        What matters is where the rig actually is: a replay primes by writing the
        opening setpoints, so starting one from somewhere else means every line
        jumps at t=0, and on the air and pilot lines that is a transient into the
        burner rather than a transition.
        """
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        if self.session.sequence_state != SEQ_IDLE:
            self.status.emit('Finish the recording or replay in progress first.')
            return
        sequence = self.session.load_sequence(Path(path))
        if sequence is None:
            # ``load_sequence`` has already said why.
            return

        flows = {track.key: self.session.flow_for_role(track.key)
                 for track in sequence.tracks}
        mismatches = opening_mismatches(sequence, flows)
        if mismatches and not self._confirm_start(sequence, mismatches):
            return
        # Shown before it runs: a sequence started from a one-line list still
        # deserves the timeline, the curves and the Stop button.
        self.sequence_btn.setChecked(True)
        self.sequence_panel.request_replay(repeats=1)

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
        card = Card('System Log', collapsed=True)
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

        self._cards_card = Card('Live Controller Readings & Manual Control',
                                collapsible=False)
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
        self._cards_layout = QVBoxLayout(self._cards_holder)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(theme.PAD_SM)
        self._cards_card.add(self._cards_holder)
        self._cards_card.body_layout.addStretch(1)
        column.addWidget(self._cards_card, 1)

        self._combustion_card = self._build_combustion()
        column.addWidget(self._combustion_card)
        scroll.setWidget(holder)
        return scroll

    def _build_combustion(self):
        card = Card('Combustion   (NH₃ / H₂ / CH₄ — pilot included in φ)',
                    collapsible=False)
        strip = QHBoxLayout()
        strip.setSpacing(theme.PAD_SM + 1)
        for key, _role_label in roles.ROLES:
            color = theme.INFO if key in roles.AIR_KEYS else theme.TEXT
            tile = MetricTile(SHORT_LABELS[key], color=color, size=10)
            tile.set_value('0.000')
            strip.addWidget(tile, 1)
            self._combustion[key] = tile

        total = MetricTile('TOTAL FUEL', color=theme.WARN, size=10)
        total.set_value('0.000')
        strip.addWidget(total, 1)
        self._combustion['total'] = total

        separator = QWidget()
        separator.setFixedWidth(1)
        separator.setStyleSheet('background-color: rgba(255, 255, 255, 24);')
        strip.addSpacing(theme.PAD_XS)
        strip.addWidget(separator)
        strip.addSpacing(theme.PAD_XS)

        for key, caption, color in (('phi1', 'φ STAGE 1', theme.PHI_STAGE),
                                    ('phi2', 'φ STAGE 2', theme.PHI_STAGE),
                                    ('phig', 'φ GLOBAL', theme.PHI_GLOBAL)):
            tile = MetricTile(caption, color=color, size=16)
            tile.setSizePolicy(QSizePolicy.Policy.Preferred,
                               QSizePolicy.Policy.Preferred)
            strip.addWidget(tile, 1)
            self._combustion[key] = tile
        card.add_layout(strip)
        return card

    # -- live cards ------------------------------------------------------- #
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

        for title, group in groups:
            if not group:
                continue
            header = StageHeader(title)
            self._cards_layout.addWidget(header)
            self._stage_headers[title] = header
            for meta in group:
                self._add_card(meta)
        for unit, text in pending.items():
            card = self._cards.get(unit)
            if card is not None:
                card.entry.setText(text)
        self._refresh_readings()

    def _add_card(self, meta):
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
                        declared_ramp=self.session.ramp_rate_for(meta.unit))
        card.setToolTip(f'{meta.label} — unit {meta.unit}')
        card.setpoint_requested.connect(self._on_setpoint)
        card.full_scale_requested.connect(self._on_full_scale)
        card.ramp_rate_requested.connect(self._on_ramp_rate)
        self._cards_layout.addWidget(card)
        self._cards[meta.unit] = card
        self._card_keys[meta.unit] = meta.key

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
        self._refresh_readings()

    def _on_ramp_rate(self, unit, value):
        """The operator declared how fast this line may move, or asked for none."""
        self.session.set_ramp_rate(unit, value)
        card = self._cards.get(unit)
        if card is not None:
            card.set_declared_ramp(self.session.ramp_rate_for(unit))

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
        self._refresh_readings()

    def _refresh_readings(self):
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

        for key, _role_label in roles.ROLES:
            self._combustion[key].set_value(f'{flows.get(key, 0.0):.3f}')
        self._combustion['total'].set_value(
            f"{sum(flows.get(key, 0.0) for key in roles.FUEL_KEYS):.3f}")
        phi_1, phi_2, phi_global = self.session.phi_values()
        for tile_key, value in (('phi1', phi_1), ('phi2', phi_2),
                                ('phig', phi_global)):
            # ``phi`` returns 0.0 when there is no air to divide by, which is
            # not a lean mixture — it is no answer at all.
            self._combustion[tile_key].set_value(
                '--' if value <= 0 else f'{value:.2f}')

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
        self._ignition_card.setVisible(staged)
        self._combustion_card.setVisible(staged)
        # Numbered over the steps this mode actually shows.  ``isVisible`` is
        # no use here: at construction nothing has been shown yet, so the
        # mode itself is what decides.
        for step, card in enumerate([card for card, shown in (
                (self._autocalc_card, staged), (self._ignition_card, staged),
                (self._batch_card, True), (self._sequence_card, True))
                if shown], start=1):
            card.set_index(step)
        self._rebuild_cards()

    def _on_connection(self, connected):
        self.monitor_btn.setEnabled(connected)
        if not connected:
            self.monitor_btn.setText('Start Monitoring')
            self._poll_label.setText('poll  —')
        self._rebuild_cards()

    def _on_monitoring(self, monitoring):
        self.monitor_btn.setText('Stop Monitoring' if monitoring
                                 else 'Start Monitoring')
        self.monitor_btn.setProperty('variant',
                                     'danger' if monitoring else 'accent')
        self.monitor_btn.style().unpolish(self.monitor_btn)
        self.monitor_btn.style().polish(self.monitor_btn)
        if not monitoring:
            self._poll_label.setText('poll  stopped')

    def _on_poll_rate(self, hz, ms):
        self._poll_label.setText(f'poll  {hz:.1f} Hz  ({ms:.0f} ms/pass)')

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
