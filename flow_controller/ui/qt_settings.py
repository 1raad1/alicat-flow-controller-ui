"""In-app appearance settings.

A dialog over the same ``ui_theme.json`` that ``qt_config`` reads, so a change
made here and a change made in a text editor are the same change — there is no
second store to drift out of sync.

Two commit paths on purpose.  **Apply** re-themes the running window without
touching the disk, which is how you actually pick a colour: you look at it on
the real screen, against real readings.  **Save** writes the file so the choice
survives a restart.  Cancel restores whatever was in force when the dialog
opened, including any Applies made since — otherwise "cancel" would leave the
window in a state the operator never chose.

The dialog does not attempt to expose everything in the config.  The backdrop
blobs stay in the file: they are five-element lists whose useful edits are
compositional, and a row of spin boxes would be a worse editor than the JSON.
"""

from __future__ import annotations

import copy

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QColorDialog, QDialog, QDoubleSpinBox, QFontComboBox, QFormLayout,
    QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSlider,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from . import qt_config
from . import qt_theme as theme
from .qt_widgets import GlassBackdrop, label


# Keys read better with a hand-written name than with a de-underscored one.
COLOR_LABELS = {
    'bg': 'Window', 'bg_panel': 'Panel', 'bg_card': 'Card',
    'bg_card_alt': 'Card (raised)', 'border': 'Border',
    'border_soft': 'Border (soft)',
    'text': 'Text', 'text_bright': 'Text (bright)',
    'text_muted': 'Text (muted)', 'text_dim': 'Text (dim)',
    'accent': 'Accent', 'accent_hover': 'Accent hover',
    'accent_pressed': 'Accent pressed', 'on_accent': 'Text on accent',
    'ok': 'OK', 'info': 'Info', 'warn': 'Warning', 'amber': 'Amber',
    'danger': 'Danger', 'danger_hover': 'Danger hover', 'teal': 'Teal',
    'phi_stage': 'φ stage', 'phi_global': 'φ global',
}

RADIUS_LABELS = {
    'card': 'Card', 'panel': 'Unit card', 'tile': 'Metric tile',
    'control': 'Buttons', 'input': 'Inputs',
}

SPACING_LABELS = {
    'xs': 'Extra small', 'sm': 'Small', 'md': 'Medium', 'lg': 'Large',
    'xl': 'Extra large', 'card_pad': 'Card padding',
    'card_gap': 'Gap between cards', 'field_gap': 'Gap between fields',
}

# (key, caption, minimum, maximum)
GLASS_ALPHAS = (
    ('tint_alpha', 'Card opacity', 0, 255),
    ('tint_soft_alpha', 'Unit card opacity', 0, 255),
    ('tint_bar_alpha', 'Title/status bar opacity', 0, 255),
    ('inset_alpha', 'Metric tile lift', 0, 60),
    ('rim', 'Rim', 0, 120),
    ('rim_high', 'Rim highlight', 0, 200),
    ('rim_low', 'Rim shadow', 0, 120),
    ('sheen', 'Sheen', 0, 90),
)


class Swatch(QPushButton):
    """A colour button that opens the system picker and remembers its value."""

    changed = Signal(str)

    def __init__(self, value, parent=None):
        super().__init__(parent)
        self._value = value
        self.setFixedSize(theme.scale(64), theme.scale(26))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._pick)
        self._refresh()

    def value(self):
        return self._value

    def set_value(self, value):
        self._value = value
        self._refresh()

    def _refresh(self):
        # Styled directly rather than through the sheet: the whole point of the
        # control is to show its own colour, which no stylesheet rule can know.
        self.setStyleSheet(
            f'background-color: {self._value};'
            ' border: 1px solid rgba(255, 255, 255, 60);'
            f' border-radius: {theme.RADIUS_INPUT}px;')
        self.setToolTip(self._value)

    def _pick(self):
        chosen = QColorDialog.getColor(
            QColor(self._value), self, 'Choose colour')
        if chosen.isValid():
            self.set_value(chosen.name())
            self.changed.emit(self._value)


class SettingsDialog(QDialog):
    """Appearance settings for the operation window.

    ``applied`` carries a full config dict — the window re-themes from it.  The
    dialog never mutates ``theme.CONFIG`` itself, so the window stays the single
    place that decides when a rebuild happens.
    """

    applied = Signal(dict)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Appearance')
        self.setMinimumSize(560, 620)
        self.setStyleSheet(theme.STYLESHEET)

        # What Cancel goes back to.  Captured before any edit, so cancelling
        # also undoes Applies made while the dialog was open.
        self._original = copy.deepcopy(config)
        self._working = copy.deepcopy(config)

        self._backdrop = GlassBackdrop(self)
        self._backdrop.lower()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(theme.PAD_LG, theme.PAD_LG,
                                 theme.PAD_LG, theme.PAD_LG)
        outer.setSpacing(theme.PAD_MD)

        heading = QVBoxLayout()
        heading.setSpacing(2)
        heading.addWidget(label('Appearance', color=theme.TEXT_BRIGHT, size=13))
        # The path is here so an operator can find the file to hand-edit, so
        # the informative end is the filename — elide in the middle, and keep
        # the whole thing in the tooltip.
        self._path = label('', color=theme.TEXT_DIM, size=8, monospace=True)
        self._path.setToolTip(str(qt_config.path()))
        self._path.setMinimumWidth(0)
        heading.addWidget(self._path)
        outer.addLayout(heading)

        tabs = QTabWidget()
        tabs.addTab(self._scroll(self._colors_page()), 'Colours')
        tabs.addTab(self._scroll(self._type_page()), 'Type && shape')
        tabs.addTab(self._scroll(self._spacing_page()), 'Spacing')
        tabs.addTab(self._scroll(self._glass_page()), 'Glass')
        outer.addWidget(tabs, 1)

        self._status = label('', color=theme.TEXT_DIM, size=8)
        outer.addWidget(self._status)

        buttons = QHBoxLayout()
        buttons.setSpacing(theme.PAD_SM)
        reset = QPushButton('Reset to defaults')
        reset.setProperty('variant', 'quiet')
        reset.clicked.connect(self._reset)
        buttons.addWidget(reset)
        buttons.addStretch(1)
        for text, variant, slot in (('Cancel', 'quiet', self._cancel),
                                    ('Apply', None, self._apply),
                                    ('Save && close', 'accent', self._save)):
            button = QPushButton(text)
            if variant:
                button.setProperty('variant', variant)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        outer.addLayout(buttons)

    def resizeEvent(self, event):
        self._backdrop.setGeometry(self.rect())
        metrics = self._path.fontMetrics()
        self._path.setText(metrics.elidedText(
            str(qt_config.path()), Qt.TextElideMode.ElideMiddle,
            max(120, self.width() - theme.PAD_LG * 2)))
        super().resizeEvent(event)

    # -- page scaffolding -----------------------------------------------
    def _scroll(self, page):
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(page)
        # The viewport fills itself opaquely by default, which would mask the
        # backdrop the rest of the dialog is floating on.
        area.viewport().setAutoFillBackground(False)
        return area

    def _page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(theme.PAD_XS, theme.PAD_SM,
                                  theme.PAD_MD, theme.PAD_MD)
        layout.setSpacing(theme.PAD_MD)
        return page, layout

    def _section(self, layout, title):
        caption = label(title.upper(), color=theme.TEXT_BRIGHT, size=9,
                        bold=True, object_name='SectionLabel')
        layout.addWidget(caption)

    def _hint(self, layout, text):
        note = QLabel(text)
        note.setObjectName('Hint')
        note.setWordWrap(True)
        layout.addWidget(note)

    # -- pages -----------------------------------------------------------
    def _colors_page(self):
        page, layout = self._page()

        self._section(layout, 'Interface')
        layout.addLayout(self._swatch_grid(
            self._working['colors'], COLOR_LABELS,
            [key for key in self._working['colors'] if key != 'gas']))

        self._section(layout, 'Gas colours')
        self._hint(layout,
                   'Used for the controller name, its tracking bar and its '
                   'series on the graphs.')
        layout.addLayout(self._swatch_grid(
            self._working['colors']['gas'], {},
            list(self._working['colors']['gas'])))

        layout.addStretch(1)
        return page

    def _swatch_grid(self, target, labels, keys, columns=2):
        grid = QGridLayout()
        grid.setHorizontalSpacing(theme.PAD_LG)
        grid.setVerticalSpacing(theme.PAD_SM)
        for index, key in enumerate(keys):
            row, column = divmod(index, columns)
            caption = QLabel(labels.get(key, key.replace('_', ' ').capitalize()))
            caption.setObjectName('FieldLabel')
            swatch = Swatch(target[key])
            swatch.changed.connect(
                lambda value, mapping=target, name=key:
                mapping.__setitem__(name, value))
            grid.addWidget(caption, row, column * 2)
            grid.addWidget(swatch, row, column * 2 + 1)
            grid.setColumnStretch(column * 2, 1)
        return grid

    def _type_page(self):
        page, layout = self._page()
        font = self._working['font']

        self._section(layout, 'Typography')
        form = QFormLayout()
        form.setHorizontalSpacing(theme.PAD_LG)
        form.setVerticalSpacing(theme.PAD_SM)

        # Seeded with signals blocked.  A configured family that is not
        # installed on *this* machine -- which is the normal case for the
        # code faces the app asks for first -- makes the combo settle on
        # whatever Qt substitutes, and that would come straight back as an
        # edit, quietly rewriting the config to the substitute merely because
        # the dialog was opened.
        ui_picker = QFontComboBox()
        ui_picker.blockSignals(True)
        ui_picker.setCurrentFont(QFont(font['ui_family']))
        ui_picker.blockSignals(False)
        ui_picker.currentFontChanged.connect(
            lambda value: font.__setitem__('ui_family', value.family()))
        form.addRow(self._field('Interface font'), ui_picker)

        mono_picker = QFontComboBox()
        mono_picker.setFontFilters(QFontComboBox.FontFilter.MonospacedFonts)
        mono_picker.blockSignals(True)
        mono_picker.setCurrentFont(QFont(font['mono_family']))
        mono_picker.blockSignals(False)
        mono_picker.currentFontChanged.connect(
            lambda value: font.__setitem__('mono_family', value.family()))
        form.addRow(self._field('Reading font'), mono_picker)

        size = QSpinBox()
        size.setRange(7, 18)
        size.setSuffix(' pt')
        size.setValue(int(font['size']))
        size.valueChanged.connect(
            lambda value: font.__setitem__('size', value))
        form.addRow(self._field('Base size'), size)
        layout.addLayout(form)
        self._hint(layout,
                   'Base size scales the whole interface — every other point '
                   'size is derived from it. The fallback font chains stay in '
                   'the config file — the interface asks for Avenir and the '
                   'readings for a monospace face, each walking down '
                   'its own chain to whatever this machine has.')

        self._section(layout, 'Corner radius')
        layout.addLayout(self._spin_form(self._working['radius'],
                                         RADIUS_LABELS, 0, 30, ' px'))
        layout.addStretch(1)
        return page

    def _spacing_page(self):
        page, layout = self._page()
        self._section(layout, 'Spacing scale')
        self._hint(layout,
                   'One scale drives every margin and gap in the interface. '
                   'Raise the card padding and gap together for a looser '
                   'layout; lower both to fit more controllers on screen.')
        layout.addLayout(self._spin_form(self._working['spacing'],
                                         SPACING_LABELS, 0, 60, ' px'))
        layout.addStretch(1)
        return page

    def _glass_page(self):
        page, layout = self._page()
        glass = self._working['glass']

        self._section(layout, 'Sheet tints')
        layout.addLayout(self._swatch_grid(
            glass, {'tint': 'Card', 'tint_soft': 'Unit card',
                    'tint_bar': 'Title/status bar'},
            ['tint', 'tint_soft', 'tint_bar'], columns=1))

        self._section(layout, 'Transparency')
        self._hint(layout,
                   'Lower opacity lets more of the backdrop through. Readings '
                   'have to stay legible at a glance, so there is a practical '
                   'floor well above zero.')
        form = QFormLayout()
        form.setHorizontalSpacing(theme.PAD_LG)
        form.setVerticalSpacing(theme.PAD_SM)
        for key, caption, low, high in GLASS_ALPHAS:
            form.addRow(self._field(caption),
                        self._slider(glass, key, low, high))

        grain = QDoubleSpinBox()
        grain.setRange(0.0, 0.30)
        grain.setSingleStep(0.01)
        grain.setDecimals(2)
        grain.setValue(float(glass['grain']))
        grain.valueChanged.connect(
            lambda value: glass.__setitem__('grain', value))
        form.addRow(self._field('Grain'), grain)
        layout.addLayout(form)

        self._section(layout, 'Backdrop')
        layout.addLayout(self._swatch_grid(
            {'0': glass['backdrop_base'][0], '1': glass['backdrop_base'][1],
             '2': glass['backdrop_base'][2]},
            {'0': 'Top', '1': 'Middle', '2': 'Bottom'},
            ['0', '1', '2'], columns=1))
        self._hint(layout,
                   'The coloured blooms behind the glass are edited in the '
                   'config file — each is a position, size, colour and '
                   'strength, and they are chosen against each other.')
        layout.addStretch(1)
        return page

    # -- field builders ---------------------------------------------------
    def _field(self, text):
        caption = QLabel(text)
        caption.setObjectName('FieldLabel')
        return caption

    def _spin_form(self, target, labels, low, high, suffix):
        form = QFormLayout()
        form.setHorizontalSpacing(theme.PAD_LG)
        form.setVerticalSpacing(theme.PAD_SM)
        for key, value in target.items():
            spin = QSpinBox()
            spin.setRange(low, high)
            spin.setSuffix(suffix)
            spin.setValue(int(value))
            spin.valueChanged.connect(
                lambda new, mapping=target, name=key:
                mapping.__setitem__(name, new))
            form.addRow(
                self._field(labels.get(key, key.replace('_', ' ').capitalize())),
                spin)
        return form

    def _slider(self, target, key, low, high):
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.PAD_SM)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(low, high)
        slider.setValue(int(target[key]))
        readout = label(str(int(target[key])), color=theme.TEXT_MUTED, size=8,
                        monospace=True)
        readout.setFixedWidth(theme.scale(30))
        readout.setAlignment(Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter)

        def commit(value):
            target[key] = value
            readout.setText(str(value))

        slider.valueChanged.connect(commit)
        row.addWidget(slider, 1)
        row.addWidget(readout)
        return holder

    # -- commit -----------------------------------------------------------
    def _emit(self):
        self.applied.emit(copy.deepcopy(self._working))
        # The window has re-themed by now, so pick up the new sheet — a dialog
        # still wearing the old palette while it is the thing that changed it
        # looks broken.
        self.setStyleSheet(theme.STYLESHEET)

    def _apply(self):
        self._emit()
        self._status.setText('Applied — not yet saved.')

    def _save(self):
        self._emit()
        error = qt_config.save(self._working)
        if error:
            self._status.setText(f'Could not save: {error}')
            self._status.setStyleSheet(
                f'color: {theme.DANGER_HOVER}; background: transparent;')
            return
        self.accept()

    def _cancel(self):
        # Only rebuild if something was actually applied; a plain cancel with
        # no Apply should not cost a rebuild of the whole window.
        if self._working != self._original:
            self.applied.emit(copy.deepcopy(self._original))
        self.reject()

    def _reset(self):
        # Rebuilding the dialog is the honest way to reset: every control was
        # seeded from the working dict at construction, and there is no reverse
        # path from a dict back into forty already-built widgets.
        self._working = qt_config.defaults()
        self.applied.emit(copy.deepcopy(self._working))
        replacement = SettingsDialog(self._working, self.parent())
        replacement._original = copy.deepcopy(self._original)
        replacement.applied.connect(self.applied)
        self.reject()
        replacement.show()
