"""A runnable Qt mock-up of the Operation & Monitoring surface.

This is a *look and feel* spike, not working control code.  It reproduces the
real screen — the same cards, the same fields, the same seven controllers, the
same safety affordances — driven by a simulated plant so the live behaviour is
visible.  Nothing here talks to hardware and nothing in the running Tk app
imports it.

    <qt-venv>/python spike_qt_ui.py
"""

from __future__ import annotations

import random
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from flow_controller.ui import qt_theme as theme
from flow_controller.ui.qt_settings import SettingsDialog
from flow_controller.ui.qt_widgets import (
    Card, GlassBackdrop, GlassBar, MetricTile, StageHeader, StateBanner,
    StatusDot, UnitCard, label,
)

# unit -> (gas, full scale in SLPM).  The gas colour is deliberately *not*
# stored here: it is looked up from the theme when the card is built, so a
# recoloured gas follows through on the next re-theme.
CONTROLLERS = {
    1: ('NH3', 40.0), 2: ('H2', 20.0), 3: ('Air', 250.0),
    4: ('NH3', 40.0), 5: ('H2', 20.0), 6: ('Air', 250.0),
    7: ('CH4', 5.0),
}
# Controllers are grouped the way the rig is operated.  An operator reasons
# about "stage 2 is running lean", never about "unit 5" — a flat list of seven
# makes them recover the grouping from the labels every time they look.
STAGES = (
    ('Stage 1', (1, 2, 3)),
    ('Stage 2', (4, 5, 6)),
    ('Pilot', (7,)),
)

STORED_TARGETS = [
    ('NH3-1', 18.42), ('H2-1', 6.11), ('NH3-2', 9.05), ('H2-2', 3.02),
    ('Air-1', 96.30), ('Air-2', 142.70),
]
# Which controller feeds which combustion tile.
TILE_UNITS = {'NH3-1': 1, 'H2-1': 2, 'Air-1': 3,
              'NH3-2': 4, 'H2-2': 5, 'Air-2': 6, 'CH4': 7}
# Volumetric stoichiometric air per unit of fuel, air taken as 21% O2:
#   NH3 + 0.75 O2,   H2 + 0.5 O2,   CH4 + 2 O2
STOICH_AIR = {1: 0.75 / 0.21, 4: 0.75 / 0.21,
              2: 0.5 / 0.21, 5: 0.5 / 0.21,
              7: 2.0 / 0.21}


def field_grid(specs, *, columns=2, width=82):
    """``[(caption, default)]`` laid out as aligned label/entry pairs."""
    grid = QGridLayout()
    grid.setContentsMargins(0, 2, 0, 2)
    grid.setHorizontalSpacing(theme.PAD_MD)
    grid.setVerticalSpacing(theme.PAD_SM + 2)
    entries = {}
    for index, (caption, default) in enumerate(specs):
        row, column = divmod(index, columns)
        caption_label = QLabel(caption)
        caption_label.setObjectName('FieldLabel')
        entry = QLineEdit(str(default))
        entry.setFixedWidth(theme.scale(width))
        entry.setAlignment(Qt.AlignmentFlag.AlignRight)
        grid.addWidget(caption_label, row, column * 2)
        grid.addWidget(entry, row, column * 2 + 1)
        entries[caption] = entry
    for column in range(columns):
        grid.setColumnStretch(column * 2, 1)
    return grid, entries


def row(*widgets, spacing=None, margins=(0, 0, 0, 0), stretch_at=None):
    # spacing defaults to None rather than to the theme value: a default
    # argument binds once at import and would survive a re-theme as a stale
    # copy of the old spacing scale.
    container = QWidget()
    container.setObjectName('Row')
    layout = QHBoxLayout(container)
    layout.setContentsMargins(*margins)
    layout.setSpacing(theme.PAD_SM if spacing is None else spacing)
    for index, widget in enumerate(widgets):
        if widget is None:
            layout.addStretch(1)
        else:
            layout.addWidget(widget, 1 if index == stretch_at else 0)
    return container


class OperationWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Alicat Flow Controller v3')
        self.resize(1560, 940)

        # Plant state lives on the window, not in the widgets, so the view can
        # be thrown away and rebuilt without losing the run.
        self._sim = {unit: {'sp': 0.0, 'flow': 0.0} for unit in CONTROLLERS}
        self._tick_count = 0
        self._settings = None

        self._build_ui()
        if theme.CONFIG_ERROR:
            self._log(f'Theme config ignored ({theme.CONFIG_ERROR}); '
                      'using defaults.')

        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _build_ui(self):
        """Build the whole view from the theme as it currently stands.

        Re-callable, because that is how a re-theme is applied.  Live-patching
        the existing widgets cannot work: layout margins and spacing are read
        once at construction and there is no way to write them back, and a
        colour already baked into a widget cannot be traced to the token it
        came from — ``ACCENT`` and the NH₃ gas colour are the same hex string
        by default, so there is nothing to map backwards.
        """
        self.setStyleSheet(theme.STYLESHEET)

        # Everything above this is translucent, so the backdrop is the only
        # thing in the window that actually paints a colour.
        root = GlassBackdrop()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_title_bar())
        root_layout.addWidget(self._build_tabs(), 1)
        root_layout.addWidget(self._build_status_bar())
        # Replaces and deletes whatever was there before.
        self.setCentralWidget(root)

    # ------------------------------------------------------------------ #
    #  Re-theming                                                         #
    # ------------------------------------------------------------------ #
    def _open_settings(self):
        # Modeless on purpose: picking a colour means watching it land on the
        # real screen, against real readings, not on a preview swatch.
        if self._settings is None:
            self._settings = SettingsDialog(theme.CONFIG, self)
            self._settings.applied.connect(self._retheme)
            self._settings.finished.connect(self._settings_closed)
        self._settings.show()
        self._settings.raise_()

    def _settings_closed(self, _result):
        self._settings = None

    def _retheme(self, config):
        state = self._capture_state()
        theme.apply(config)
        self._build_ui()
        self._restore_state(state)
        self._log('Appearance updated.')

    def _capture_state(self):
        """Everything the view holds that the plant model does not."""
        return {
            'syslog': self._syslog.toPlainText(),
            'banner': self._banner.state(),
            'logging': self._log_state.text() == 'RECORDING',
            'calc': self._calc_status.text(),
            'tiles': {caption: tile.value.text()
                      for caption, tile in self._target_tiles.items()},
            'entries': {unit: card.entry.text()
                        for unit, card in self._cards.items()},
            'armed': self._step2.isEnabled(),
            'syslog_open': not self._syslog_card.is_collapsed(),
        }

    def _restore_state(self, state):
        self._syslog.setPlainText(state['syslog'])
        self._banner.set_state(*state['banner'])
        self._set_logging(state['logging'], announce=False)
        self._calc_status.setText(state['calc'])
        for caption, text in state['tiles'].items():
            self._target_tiles[caption].set_value(text)
        for unit, text in state['entries'].items():
            self._cards[unit].entry.setText(text)
        self._step2.setEnabled(state['armed'])
        if state['syslog_open']:
            self._syslog_card.set_collapsed(False, animate=False)

    # ------------------------------------------------------------------ #
    #  Chrome                                                             #
    # ------------------------------------------------------------------ #
    def _build_title_bar(self):
        bar = GlassBar('bottom')
        bar.setObjectName('TitleBar')
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(theme.PAD_XL, theme.PAD_MD + 2,
                                  theme.PAD_XL, theme.PAD_MD + 2)
        layout.setSpacing(theme.PAD_MD)

        name = QLabel('Alicat Flow Controller')
        name.setObjectName('TitleName')
        layout.addWidget(name)
        version = QLabel('v3.0.0')
        version.setObjectName('TitleSub')
        layout.addWidget(version)
        layout.addSpacing(theme.PAD_LG)
        subtitle = QLabel('Multi-Gas Control  ·  Live Monitoring  ·  Logging')
        subtitle.setObjectName('TitleSub')
        layout.addWidget(subtitle)
        layout.addStretch(1)

        self._link_dot = StatusDot(theme.OK)
        layout.addWidget(self._link_dot)
        layout.addWidget(label('7 controllers · COM4 · 10.2 Hz',
                               color=theme.TEXT_MUTED, size=8, monospace=True))

        layout.addSpacing(theme.PAD_SM)
        settings = QPushButton('⚙')
        settings.setObjectName('IconButton')
        settings.setToolTip('Appearance settings')
        settings.setCursor(Qt.CursorShape.PointingHandCursor)
        settings.clicked.connect(self._open_settings)
        layout.addWidget(settings)
        return bar

    def _build_tabs(self):
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        # '&&' because QTabBar reads a single '&' as a mnemonic marker and
        # renders it as an underline on the following letter.
        tabs.addTab(self._placeholder_tab(
            'Connection & Assignment',
            'Port discovery, unit ID scan, and gas assignment live here.'),
            'Connection && Assignment')
        tabs.addTab(self._build_operation_tab(), 'Operation && Monitoring')
        tabs.addTab(self._placeholder_tab(
            'Logging & Graphs',
            'Graphs stay idle until this tab is open and a series is chosen.'),
            'Logging && Graphs')
        tabs.setCurrentIndex(1)

        # The safety controls ride in the tab strip, always reachable from any
        # tab — the same placement as the Tk app, but as real buttons with
        # enabled/hover states rather than hand-coloured tk.Buttons.
        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(theme.PAD_MD, theme.PAD_XS,
                                         theme.PAD_LG, theme.PAD_XS)
        corner_layout.setSpacing(theme.PAD_SM)
        reconnect = QPushButton('⟳  Reconnect Meters')
        reconnect.setProperty('variant', 'quiet')
        corner_layout.addWidget(reconnect)
        for text in ('ZERO FUEL', 'ZERO ALL'):
            button = QPushButton(text)
            button.setObjectName('SafetyButton')
            button.setEnabled(True)
            corner_layout.addWidget(button)
        tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        return tabs

    def _placeholder_tab(self, title, note):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addStretch(1)
        heading = label(title, color=theme.TEXT_MUTED, size=13)
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)
        body = label(note, color=theme.TEXT_DIM, size=9)
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(body)
        layout.addStretch(1)
        return page

    def _build_status_bar(self):
        bar = GlassBar('top')
        bar.setObjectName('StatusBar')
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(theme.PAD_XL, theme.PAD_SM + 1,
                                  theme.PAD_XL, theme.PAD_SM + 1)
        layout.setSpacing(theme.PAD_XL + 6)
        self._status_labels = {}
        for key, text in (
                ('poll', 'poll  10.2 Hz  (0 ms extra)'),
                ('log', 'log  OFF'),
                ('udp', 'UDP  127.0.0.1:61557  ready'),
                ('safety', 'discrepancy  nominal'),
        ):
            widget = QLabel(text)
            layout.addWidget(widget)
            self._status_labels[key] = widget
        layout.addStretch(1)
        layout.addWidget(QLabel('uptime  00:14:22'))
        return bar

    # ------------------------------------------------------------------ #
    #  Operation tab                                                      #
    # ------------------------------------------------------------------ #
    def _build_operation_tab(self):
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(4)
        splitter.addWidget(self._build_left_column())
        splitter.addWidget(self._build_right_column())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([520, 1040])
        return splitter

    def _build_left_column(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # The viewport would otherwise fill itself opaquely and mask the
        # backdrop that the cards are supposed to be floating over.
        scroll.viewport().setAutoFillBackground(False)
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(theme.PAD_LG, theme.PAD_LG,
                                  theme.PAD_SM + 2, theme.PAD_LG)
        column.setSpacing(theme.CARD_GAP)
        column.addWidget(self._card_logging())
        column.addWidget(self._card_autocalc())
        column.addWidget(self._card_ignition())
        column.addWidget(self._card_safety())
        column.addWidget(self._card_syslog())
        column.addStretch(1)
        scroll.setWidget(holder)
        # Wide enough that the fixed-width entry fields never force a clip;
        # the splitter can still be dragged wider.
        scroll.setMinimumWidth(holder.sizeHint().width() + 26)
        return scroll

    def _card_logging(self):
        card = Card('Logging & Acquisition')

        path = QLineEdit(r'C:\rig\logs\run_2026-08-16.xlsx')
        browse = QPushButton('Browse…')
        browse.setProperty('variant', 'quiet')
        caption = QLabel('Log file')
        caption.setObjectName('FieldLabel')
        card.add(row(caption, path, browse, stretch_at=1))

        self._start_log = QPushButton('Start Logging')
        self._start_log.setProperty('variant', 'accent')
        self._stop_log = QPushButton('Stop Logging')
        self._stop_log.setEnabled(False)
        self._log_state = label('OFF', color=theme.TEXT_DIM, size=9,
                                monospace=True)
        self._start_log.clicked.connect(lambda: self._set_logging(True))
        self._stop_log.clicked.connect(lambda: self._set_logging(False))
        card.add(row(self._start_log, self._stop_log, self._log_state, None))

        grid, _ = field_grid([('LabVIEW UDP port', 61557),
                              ('Extra pass delay (ms)', 0)], width=70)
        card.add_layout(grid)

        hint = QLabel('Leave the delay at 0 normally. LabVIEW logs use a '
                      'timestamped copy of the selected file.')
        hint.setObjectName('Hint')
        hint.setWordWrap(True)
        card.add(hint)
        return card

    def _card_autocalc(self):
        card = Card('Auto-Calculate Flows', index=1)
        grid, _ = field_grid([
            ('Power (kW)', 10), ('H₂ percentage (%)', 30),
            ('Stage 1 split (%)', 99.99), ('φ stage 1', 1.1),
            ('φ global', 0.6),
        ])
        card.add_layout(grid)

        calculate = QPushButton('Calculate && Store Targets   (no flows sent)')
        calculate.setProperty('variant', 'accent')
        calculate.clicked.connect(self._calculate)
        card.add(calculate)

        self._calc_status = QLabel('No targets stored yet.')
        self._calc_status.setObjectName('Hint')
        card.add(self._calc_status)

        tiles = QGridLayout()
        tiles.setSpacing(theme.PAD_SM)
        self._target_tiles = {}
        for index, (caption, _value) in enumerate(STORED_TARGETS):
            tile = MetricTile(caption)
            tiles.addWidget(tile, index // 3, index % 3)
            self._target_tiles[caption] = tile
        card.add_layout(tiles)
        return card

    def _card_ignition(self):
        card = Card('Ignition Sequence', index=2)
        self._banner = StateBanner()
        card.add(self._banner)

        grid, _ = field_grid([
            ('Fuel pre-ignition (%)', 80), ('Air pre-ignition (%)', 80),
            ('Ramp steps', 10), ('Step interval (s)', 0.5),
        ], width=64)
        card.add_layout(grid)
        card.add(row(label('φ ratio  1.00×', color=theme.TEAL, size=9,
                           monospace=True),
                     None,
                     label('total  5.0 s', color=theme.TEXT_DIM, size=9,
                           monospace=True)))

        self._step1 = QPushButton('STEP 1 — Pre-Ignition   (set scaled flows)')
        self._step1.setProperty('variant', 'ready')
        self._step1.clicked.connect(self._pre_ignition)
        card.add(self._step1)

        self._step2 = QPushButton('STEP 2 — Ignite   (ramp to target flows)')
        self._step2.setProperty('variant', 'accent')
        self._step2.setEnabled(False)
        self._step2.clicked.connect(self._ignite)
        card.add(self._step2)

        card.add(self._divider())

        stage = QPushButton('Stage Targets to SP Fields   (no flows sent)')
        stage.setProperty('variant', 'quiet')
        stage.clicked.connect(self._stage_targets)
        card.add(stage)

        send_all = QPushButton("Set All Flows Together   (send every card's SP)")
        send_all.clicked.connect(self._send_all)
        card.add(send_all)

        zero = QPushButton('Zero All Flows   (monitoring continues)')
        zero.setProperty('variant', 'danger')
        zero.clicked.connect(self._zero_all)
        card.add(zero)
        return card

    def _card_safety(self):
        card = Card('System Safety', index=3)
        enabled = QCheckBox('Enable discrepancy monitoring')
        enabled.setChecked(True)
        card.add(enabled)
        grid, _ = field_grid([('Max discrepancy (%)', 5),
                              ('Suppress repeat (s)', 30)], width=60)
        card.add_layout(grid)
        self._safety_status = row(
            StatusDot(theme.OK, diameter=7),
            label('All flows nominal', color=theme.OK, size=9, monospace=True),
            None)
        card.add(self._safety_status)
        return card

    def _card_syslog(self):
        card = Card('System Log', collapsed=True)
        self._syslog_card = card
        self._syslog = QPlainTextEdit()
        self._syslog.setReadOnly(True)
        self._syslog.setFixedHeight(theme.scale(120))
        self._syslog.setPlainText(
            '19:04:11  System initialised.\n'
            '19:04:12  Discovered 7 controllers on COM4.\n'
            '19:04:12  Monitoring started at 10.2 Hz.')
        card.add(self._syslog)
        return card

    def _divider(self):
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFixedHeight(1)
        # Translucent, like everything else on the sheet — an opaque hairline
        # reads as a scratch on the glass.
        line.setStyleSheet('background-color: rgba(255, 255, 255, 20);'
                           ' border: none;')
        return line

    # ------------------------------------------------------------------ #
    #  Right column                                                       #
    # ------------------------------------------------------------------ #
    def _build_right_column(self):
        # Scrolled, like the left column.  Readings are the one thing on this
        # screen that must never be squeezed: raise the base font and the rows
        # need more height than the window has, and a plain layout answers that
        # by compressing every widget that will allow it -- which clips the
        # setpoint entries and their Set buttons rather than the whitespace.
        # With a scroll area the shortfall becomes a scrollbar instead.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(theme.PAD_SM + 2, theme.PAD_LG,
                                  theme.PAD_LG, theme.PAD_LG)
        column.setSpacing(theme.CARD_GAP)

        cards_card = Card('Live Controller Readings & Manual Control',
                          collapsible=False)
        self._cards = {}
        self._stage_headers = {}
        for stage, units in STAGES:
            header = StageHeader(stage)
            cards_card.add(header)
            self._stage_headers[stage] = header
            for unit in units:
                gas, full_scale = CONTROLLERS[unit]
                card = UnitCard(unit, gas, theme.GAS_COLORS[gas], full_scale)
                card.setpoint_requested.connect(self._apply_setpoint)
                cards_card.add(card)
                self._cards[unit] = card
        cards_card.body_layout.addStretch(1)
        column.addWidget(cards_card, 1)

        column.addWidget(self._build_combustion())
        scroll.setWidget(holder)
        return scroll

    def _build_combustion(self):
        card = Card('Combustion   (NH₃ / H₂ / CH₄ — pilot included in φ)',
                    collapsible=False)
        strip = QHBoxLayout()
        strip.setSpacing(theme.PAD_SM + 1)
        self._combustion = {}
        for caption in ('NH3-1', 'H2-1', 'NH3-2', 'H2-2', 'CH4',
                        'Air-1', 'Air-2'):
            color = theme.INFO if caption.startswith('Air') else theme.TEXT
            tile = MetricTile(caption, color=color, size=10)
            tile.set_value('0.000')
            strip.addWidget(tile, 1)
            self._combustion[caption] = tile

        total = MetricTile('TOTAL FUEL', color=theme.WARN, size=10)
        total.set_value('0.000')
        strip.addWidget(total, 1)
        self._combustion['TOTAL'] = total

        separator = QFrame()
        separator.setFixedWidth(1)
        separator.setStyleSheet('background-color: rgba(255, 255, 255, 24);'
                                ' border: none;')
        strip.addSpacing(theme.PAD_XS)
        strip.addWidget(separator)
        strip.addSpacing(theme.PAD_XS)

        for caption, color in (('φ STAGE 1', '#fb923c'),
                               ('φ GLOBAL', '#34d399')):
            tile = MetricTile(caption, color=color, size=16)
            tile.setSizePolicy(QSizePolicy.Policy.Preferred,
                               QSizePolicy.Policy.Preferred)
            strip.addWidget(tile, 1)
            self._combustion[caption] = tile
        card.add_layout(strip)
        return card

    # ------------------------------------------------------------------ #
    #  Simulated behaviour                                                #
    # ------------------------------------------------------------------ #
    def _log(self, message):
        self._syslog.appendPlainText(f'19:0{self._tick_count % 10}:00  {message}')

    def _calculate(self):
        for caption, value in STORED_TARGETS:
            self._target_tiles[caption].set_value(f'{value:.2f}')
        self._calc_status.setText(
            'Targets stored for 10.0 kW · 30% H₂ · φ global 0.60')
        self._banner.set_state('idle', 'IDLE — targets stored, ready to arm')
        self._log('Targets calculated and stored. No flows sent.')

    def _stage_targets(self):
        for unit, target in zip((1, 2, 4, 5, 3, 6),
                                (v for _c, v in STORED_TARGETS)):
            self._cards[unit].entry.setText(f'{target:.2f}')
        self._log('Stored targets staged into SP fields. No flows sent.')

    def _send_all(self):
        for card in self._cards.values():
            card._emit_setpoint()
        self._log('Batch setpoint sent to all controllers.')

    def _pre_ignition(self):
        for unit, target in zip((1, 2, 4, 5, 3, 6),
                                (v for _c, v in STORED_TARGETS)):
            self._sim[unit]['sp'] = target * 0.8
            self._cards[unit].entry.setText(f'{target * 0.8:.2f}')
        self._banner.set_state('ready', 'PRE-IGNITION — scaled flows set')
        self._step2.setEnabled(True)
        self._log('Pre-ignition flows set at 80%.')

    def _ignite(self):
        for unit, target in zip((1, 2, 4, 5, 3, 6),
                                (v for _c, v in STORED_TARGETS)):
            self._sim[unit]['sp'] = target
            self._cards[unit].entry.setText(f'{target:.2f}')
        self._sim[7]['sp'] = 1.2
        self._cards[7].entry.setText('1.20')
        self._banner.set_state('running', 'IGNITED — ramping to target flows')
        self._step2.setEnabled(False)
        self._log('Ignition ramp started: 10 steps at 0.5 s.')

    def _zero_all(self):
        for state in self._sim.values():
            state['sp'] = 0.0
        for card in self._cards.values():
            card.entry.setText('0')
        self._banner.set_state('idle', 'IDLE — all flows zeroed')
        self._step2.setEnabled(False)
        self._log('All setpoints zeroed. Monitoring continues.')

    def _apply_setpoint(self, unit, value):
        self._sim[unit]['sp'] = value
        self._log(f'Unit {unit} setpoint -> {value:.3f} SLPM')

    def _set_logging(self, active, *, announce=True):
        self._start_log.setEnabled(not active)
        self._stop_log.setEnabled(active)
        self._log_state.setText('RECORDING' if active else 'OFF')
        self._log_state.setStyleSheet(
            f'color: {theme.OK if active else theme.TEXT_DIM};'
            ' background: transparent;')
        self._status_labels['log'].setText(
            'log  run_2026-08-16.xlsx' if active else 'log  OFF')
        # Restoring the flag after a rebuild is not an operator action, so it
        # must not write a line into the run's log.
        if announce:
            self._log('Logging started.' if active else 'Logging stopped.')

    def _tick(self):
        self._tick_count += 1
        readings = {}
        for unit, (_gas, full_scale) in CONTROLLERS.items():
            state = self._sim[unit]
            # First-order approach to setpoint plus a little sensor noise, so
            # the tracking bar and the discrepancy colouring actually move.
            state['flow'] += (state['sp'] - state['flow']) * 0.18
            reading = max(0.0, state['flow']
                          + random.gauss(0, full_scale * 0.002))
            readings[unit] = reading
            self._cards[unit].update_readings(
                reading, state['sp'],
                14.7 + reading * 0.02 + random.gauss(0, 0.01),
                22.0 + reading * 0.05 + random.gauss(0, 0.05))

        # Each stage header carries its own totals, so the grouping is doing
        # work rather than only drawing a line.
        for stage, units in STAGES:
            fuel = sum(readings[unit] for unit in units if unit in STOICH_AIR)
            air = sum(readings[unit] for unit in units
                      if CONTROLLERS[unit][0] == 'Air')
            summary = f'fuel {fuel:6.2f}'
            if air:
                summary += f'    air {air:7.2f}'
            self._stage_headers[stage].set_summary(f'{summary}  SLPM')

        for caption, unit in TILE_UNITS.items():
            self._combustion[caption].set_value(f'{readings[unit]:.3f}')
        fuel_total = sum(readings[unit] for unit in STOICH_AIR)
        self._combustion['TOTAL'].set_value(f'{fuel_total:.3f}')

        self._combustion['φ STAGE 1'].set_value(
            self._equivalence((1, 2), (3,), readings))
        self._combustion['φ GLOBAL'].set_value(
            self._equivalence(tuple(STOICH_AIR), (3, 6), readings))

    @staticmethod
    def _equivalence(fuel_units, air_units, readings):
        """φ = stoichiometric air demand / air actually supplied."""
        air = sum(readings[unit] for unit in air_units)
        if air < 0.05:
            return '--'
        demand = sum(readings[unit] * STOICH_AIR[unit] for unit in fuel_units)
        return f'{demand / air:.2f}'


def main():
    random.seed(3)
    app = QApplication.instance() or QApplication(sys.argv)
    window = OperationWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
