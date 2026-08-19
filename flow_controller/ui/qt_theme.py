"""Palette and stylesheet for the Qt view layer.

The colours are lifted from the existing Tk app so the port stays recognisably
the same instrument, rather than a different-looking program.  What changes is
what the toolkit can express with them: hover, focus, pressed and disabled
states are real here, instead of being approximated by hand-drawn canvas items.

The surface treatment is frosted glass.  Every panel is a translucent sheet over
a single painted backdrop, so depth comes from light passing through layers
rather than from flat blocks of grey.  Qt stylesheets have no ``backdrop-filter``
— the effect is built from a soft painted wallpaper plus grain (see
``qt_widgets.GlassBackdrop``) with translucent, rim-lit panels drawn on top.

Nothing here is a literal.  Every name below is *derived* from
``qt_config``, and ``apply()`` rebuilds the whole module from a config dict.
That is what lets the Settings dialog re-theme a running window: callers keep
reading ``theme.ACCENT`` and get the new value.  The one rule that falls out of
it — and it is easy to break — is that no other module may capture a theme value
in a default argument, because defaults bind once at import and would then
survive a re-theme as stale copies.
"""

from __future__ import annotations

from . import qt_config

CONFIG = qt_config.defaults()
CONFIG_ERROR = None


# --------------------------------------------------------------------- #
#  Helpers                                                              #
# --------------------------------------------------------------------- #
def rgba(values):
    """``(r, g, b, a)`` -> a Qt stylesheet ``rgba()`` literal."""
    return 'rgba({}, {}, {}, {})'.format(*values)


def hex_rgb(value):
    """``'#rrggbb'`` -> ``(r, g, b)``."""
    text = str(value).lstrip('#')
    if len(text) == 3:
        text = ''.join(character * 2 for character in text)
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)


def tint(color, alpha):
    """A config colour plus an alpha -> the RGBA tuple the painters want."""
    return hex_rgb(color) + (int(alpha),)


#: Family names that are CSS keywords rather than fonts.  They belong in a
#: stylesheet chain and mean nothing to QFont, which wants real families.
_GENERIC_FAMILIES = {'monospace', 'sans-serif', 'serif', 'cursive', 'fantasy'}


def families(family, fallback):
    """``('JetBrains Mono', "'Consolas', monospace")`` -> a QFont family list.

    The stylesheet gets a fallback chain for free -- it is CSS, and CSS walks
    the list.  ``QFont`` does not: asked for one absent family it substitutes
    something of its own choosing, which is how a widget that sets its font in
    code ends up in a different face from the widget beside it that was styled.
    ``QFont.setFamilies`` takes the same list, so both paths can be fed from
    the one config value.
    """
    names = [family]
    for part in str(fallback).split(','):
        name = part.strip().strip('\'"')
        if name and name.lower() not in _GENERIC_FAMILIES and name not in names:
            names.append(name)
    return names


def font_pt(size):
    """Scale a design-time point size by the user's base font size.

    Widget helpers set point sizes directly on ``QFont`` rather than through
    the stylesheet, so they need this to follow the same knob the stylesheet
    does.  Floored at 6pt: below that the readings stop being readable across
    a room, which is the whole job of this screen.
    """
    return max(6, round(size * FONT_SCALE))


def scale(pixels):
    """Scale a design-time pixel width by the user's base font size.

    The fixed widths in the unit card exist to keep its columns aligned down
    the stack.  They were measured against 10pt text, so if the base size goes
    up and they do not, the labels they were sized for get clipped.
    """
    return max(1, round(pixels * FONT_SCALE))


# --------------------------------------------------------------------- #
#  Build                                                                #
# --------------------------------------------------------------------- #
def apply(config=None):
    """Rebuild every theme name from ``config``.

    Rewrites this module's globals in place, so code that already holds a
    reference to the module (``from . import qt_theme as theme``) sees the new
    values without reimporting.
    """
    global CONFIG
    if config is not None:
        CONFIG = config

    spacing = CONFIG['spacing']
    colors = CONFIG['colors']
    radius = CONFIG['radius']
    font = CONFIG['font']
    glass = CONFIG['glass']

    values = {}

    # -- Spacing scale.  One scale, used everywhere; ad-hoc pixel values are
    #    what made the first pass feel cramped in places and loose in others.
    values.update(
        PAD_XS=spacing['xs'], PAD_SM=spacing['sm'], PAD_MD=spacing['md'],
        PAD_LG=spacing['lg'], PAD_XL=spacing['xl'],
        CARD_PAD=spacing['card_pad'], CARD_GAP=spacing['card_gap'],
        FIELD_GAP=spacing['field_gap'],
    )

    # -- Opaque colours
    values.update(
        BG=colors['bg'], BG_PANEL=colors['bg_panel'], BG_CARD=colors['bg_card'],
        BG_CARD_ALT=colors['bg_card_alt'],
        BORDER=colors['border'], BORDER_SOFT=colors['border_soft'],
        TEXT=colors['text'], TEXT_BRIGHT=colors['text_bright'],
        TEXT_MUTED=colors['text_muted'], TEXT_DIM=colors['text_dim'],
        ACCENT=colors['accent'], ACCENT_HOVER=colors['accent_hover'],
        ACCENT_PRESSED=colors['accent_pressed'], ON_ACCENT=colors['on_accent'],
        OK=colors['ok'], INFO=colors['info'], WARN=colors['warn'],
        AMBER=colors['amber'], DANGER=colors['danger'],
        DANGER_HOVER=colors['danger_hover'], TEAL=colors['teal'],
        PHI_STAGE=colors['phi_stage'], PHI_GLOBAL=colors['phi_global'],
        GAS_COLORS=dict(colors['gas']),
    )

    # -- Type
    values.update(
        FONT_UI_FAMILY=font['ui_family'],
        FONT_MONO_FAMILY=font['mono_family'],
        FONT_UI=f"'{font['ui_family']}', {font['ui_fallback']}",
        FONT_MONO=f"'{font['mono_family']}', {font['mono_fallback']}",
        FONT_UI_FAMILIES=families(font['ui_family'], font['ui_fallback']),
        FONT_MONO_FAMILIES=families(font['mono_family'],
                                    font['mono_fallback']),
        FONT_SIZE=font['size'],
        FONT_SCALE=font['size'] / 10.0,
    )

    # -- Radii
    values.update(
        RADIUS_CARD=radius['card'], RADIUS_PANEL=radius['panel'],
        RADIUS_TILE=radius['tile'], RADIUS_CONTROL=radius['control'],
        RADIUS_INPUT=radius['input'],
    )

    # -- Glass.  Alphas are deliberately conservative: this is a control
    #    interface, and text over a backdrop has to stay readable at a glance.
    values.update(
        GLASS_TINT=tint(glass['tint'], glass['tint_alpha']),
        GLASS_TINT_SOFT=tint(glass['tint_soft'], glass['tint_soft_alpha']),
        GLASS_TINT_BAR=tint(glass['tint_bar'], glass['tint_bar_alpha']),
        GLASS_INSET=(255, 255, 255, int(glass['inset_alpha'])),
        GLASS_RIM=(255, 255, 255, int(glass['rim'])),
        GLASS_RIM_HI=(255, 255, 255, int(glass['rim_high'])),
        GLASS_RIM_LO=(255, 255, 255, int(glass['rim_low'])),
        GLASS_SHEEN=(255, 255, 255, int(glass['sheen'])),
        GRAIN_OPACITY=float(glass['grain']),
        BACKDROP_BASE=tuple(glass['backdrop_base']),
        BACKDROP_BLOBS=tuple(tuple(blob) for blob in glass['backdrop_blobs']),
    )

    globals().update(values)
    globals()['STYLESHEET'] = _stylesheet()
    return CONFIG


def reload_from_disk():
    """Re-read the config file and rebuild.  Returns an error string, or None."""
    global CONFIG_ERROR
    config, error = qt_config.load()
    CONFIG_ERROR = error
    apply(config)
    return error


def _stylesheet():
    pt = font_pt
    return f"""
/* Nothing fills its own rectangle by default.  The backdrop is painted once,
   at the root, and every panel above it is translucent — so a widget that
   quietly painted an opaque background would punch a hole in the effect. */
QWidget {{
    background: transparent;
    color: {TEXT};
    font-family: {FONT_UI};
    font-size: {FONT_SIZE}pt;
}}
QMainWindow, QDialog {{ background-color: {BG}; }}

/* ---- Title bar ---------------------------------------------------- */
#TitleName {{ color: {TEXT_BRIGHT}; font-size: {pt(13)}pt; }}
#TitleSub  {{ color: {TEXT_DIM};    font-size: {pt(9)}pt; }}

/* The run states that outlive a single screen -- poll rate, log file, LabVIEW
   listener, sequence, graphs.  They read beside the app's name rather than
   along the bottom of the window: the eye is already at the top of the screen
   for the tab strip and the connection state, and a second strip at the far
   edge meant looking away from the run to find out about it. */
#TitleStatus {{ color: {TEXT_MUTED}; font-size: {pt(8)}pt;
                font-family: {FONT_MONO}; }}
#TitleStatusSep {{ color: {rgba(hex_rgb(TEXT_DIM) + (110,))};
                   font-size: {pt(8)}pt; }}
#TitleDivider {{ background-color: rgba(255, 255, 255, 20); }}
/* The settings control, and below it the window controls.  Neither sets a
   font here: they are drawn from an icon font whose size means something
   different from a text size, so the window picks the face and the size
   together and a stylesheet rule would only overrule half of that.  Both do
   clear the padding every other button gets, because these are square marks
   at a size the window fixes: the inherited 9px 18px would leave a fixed
   34x26 chip no room to draw the glyph in at all. */
#IconButton {{
    background-color: rgba(255, 255, 255, 12);
    border: 1px solid rgba(255, 255, 255, 24);
    border-radius: {RADIUS_CONTROL}px;
    padding: 0;
    color: {TEXT_MUTED};
}}
#IconButton:hover {{ background-color: rgba(255, 255, 255, 28);
                     border-color: rgba(255, 255, 255, 50);
                     color: {TEXT_BRIGHT}; }}

/* Minimise, maximise, close -- the window controls, now that the window draws
   its own frame.  Borderless and unfilled until the mouse is on them, because
   they sit permanently in the corner of every screen and three lit chips there
   would compete with the run controls that actually want to be noticed.  Close
   is the one that turns red, on the same reasoning as the safety buttons: the
   irreversible action should not look like its two harmless neighbours. */
#WinButton, #WinClose {{
    background-color: transparent;
    border: none;
    border-radius: {RADIUS_CONTROL}px;
    padding: 0;
    color: {TEXT_MUTED};
}}
#WinButton:hover   {{ background-color: rgba(255, 255, 255, 28);
                      color: {TEXT_BRIGHT}; }}
#WinButton:pressed {{ background-color: rgba(255, 255, 255, 14); }}
#WinClose:hover    {{ background-color: {DANGER}; color: #ffffff; }}
#WinClose:pressed  {{ background-color: {DANGER_HOVER}; color: #ffffff; }}

/* The one-click start beside a saved sequence.  Quiet until it is pointed at:
   it sits in a list that is mostly read, and a row of accent-coloured buttons
   would read as the list's subject rather than as one action on each row. */
#RowPlay {{
    background-color: rgba(255, 255, 255, 14);
    border: 1px solid rgba(255, 255, 255, 26);
    border-radius: {RADIUS_CONTROL}px;
    padding: 1px 0;
    color: {TEXT_MUTED};
    font-size: {pt(8)}pt;
}}
#RowPlay:hover  {{ background-color: {ACCENT}; border-color: {ACCENT};
                   color: {ON_ACCENT}; }}
#RowPlay:pressed {{ background-color: {ACCENT_HOVER};
                    border-color: {ACCENT_HOVER}; }}
#RowPlay:disabled {{ color: {TEXT_DIM};
                     background-color: rgba(255, 255, 255, 8);
                     border-color: rgba(255, 255, 255, 14); }}

#RowRename {{
    background-color: transparent;
    border: 1px solid rgba(255, 255, 255, 18);
    border-radius: {RADIUS_CONTROL}px;
    padding: 1px 0;
    color: {TEXT_MUTED};
    font-size: {pt(8)}pt;
}}
#RowRename:hover {{ background-color: rgba(255, 255, 255, 26);
                    border-color: {INFO}; color: {TEXT_BRIGHT}; }}
#RowRename:pressed {{ background-color: rgba(0, 0, 0, 60); }}
#RowRename:disabled {{ color: {TEXT_DIM};
                       border-color: rgba(255, 255, 255, 10); }}

/* Delete, beside it.  Quieter still until it is pointed at, and then red
   rather than accent: it is the one control on the row that cannot be undone,
   and it must not be mistakable for the one beside it that only starts a run. */
#RowDelete {{
    background-color: transparent;
    border: 1px solid rgba(255, 255, 255, 18);
    border-radius: {RADIUS_CONTROL}px;
    padding: 1px 0;
    color: {TEXT_DIM};
    font-size: {pt(8)}pt;
}}
#RowDelete:hover  {{ background-color: {DANGER}; border-color: {DANGER_HOVER};
                     color: #ffffff; }}
#RowDelete:pressed {{ background-color: {DANGER_HOVER};
                      border-color: {DANGER_HOVER}; }}
#RowDelete:disabled {{ color: {rgba(hex_rgb(TEXT_DIM) + (110,))};
                       background-color: transparent;
                       border-color: rgba(255, 255, 255, 10); }}

/* ---- Tabs ---------------------------------------------------------- */
QTabWidget::pane {{ border: none; background: transparent; }}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_MUTED};
    padding: {PAD_MD}px {PAD_XL + 2}px;
    margin-right: 4px;
    border: none;
    border-bottom: 2px solid transparent;
    border-top-left-radius: {RADIUS_CONTROL}px;
    border-top-right-radius: {RADIUS_CONTROL}px;
}}
QTabBar::tab:hover {{
    color: {TEXT};
    background-color: rgba(255, 255, 255, 12);
}}
QTabBar::tab:selected {{
    color: {TEXT_BRIGHT};
    background-color: rgba(255, 255, 255, 20);
    border-bottom: 2px solid {ACCENT};
}}

/* ---- Card interior ------------------------------------------------- */
/* The card frame itself is painted, not styled — see qt_widgets.Card. */
#CardTitle {{
    color: {TEXT_BRIGHT};
    font-size: {pt(10)}pt;
    font-weight: 600;
    letter-spacing: 0.2px;
}}
#CardIndex {{
    color: {ON_ACCENT};
    background-color: {ACCENT};
    border-radius: 9px;
    font-size: {pt(8)}pt;
    font-weight: 700;
    min-width: 18px;  max-width: 18px;
    min-height: 18px; max-height: 18px;
}}
#CardInfo {{
    color: {INFO};
    font-size: {pt(10)}pt;
    min-width: 16px;
    padding: 0 1px;
}}
#CardInfo:hover {{ color: {TEXT_BRIGHT}; }}
#CardChevron {{ color: {TEXT_DIM}; font-size: {pt(9)}pt; padding-right: 2px; }}
#Hint {{ color: {TEXT_DIM}; font-size: {pt(8)}pt; }}
#FieldLabel {{ color: {TEXT_MUTED}; font-size: {pt(9)}pt; }}
#SectionLabel {{ color: {TEXT_BRIGHT}; font-size: {pt(9)}pt; font-weight: 700;
                 letter-spacing: 0.8px; }}
#CombustionStageCard {{
    background-color: rgba(0, 0, 0, 42);
    border: 1px solid rgba(255, 255, 255, 24);
    border-radius: {RADIUS_TILE}px;
}}
#CombustionGroupTitle {{ letter-spacing: 0.8px; }}
#CombustionGroupSubtitle {{ letter-spacing: 0.4px; }}
#CardMenuButton {{
    background-color: rgba(255, 255, 255, 10);
    border: 1px solid rgba(255, 255, 255, 22);
    border-radius: {RADIUS_CONTROL}px;
    padding: 0;
    color: {TEXT_BRIGHT};
    font-size: {pt(10)}pt;
}}
#CardMenuButton:hover {{
    background-color: rgba(255, 255, 255, 24);
    border-color: rgba(255, 255, 255, 44);
}}
#CardMenuButton::menu-indicator {{ width: 0; image: none; }}
QMenu#CombustionSettingsMenu {{
    background-color: {BG_CARD_ALT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_CONTROL}px;
    padding: {PAD_XS}px;
}}
#CombustionMenuPanel {{ background-color: transparent; }}

/* ---- Inputs -------------------------------------------------------- */
QLineEdit, QSpinBox, QDoubleSpinBox {{
    background-color: rgba(0, 0, 0, 92);
    border: 1px solid rgba(255, 255, 255, 26);
    border-radius: {RADIUS_INPUT}px;
    padding: 7px 10px;
    color: {TEXT_BRIGHT};
    font-family: {FONT_MONO};
    selection-background-color: {ACCENT};
    selection-color: {ON_ACCENT};
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
    border-color: rgba(255, 255, 255, 46); }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {ACCENT}; background-color: rgba(0, 0, 0, 120); }}
QLineEdit:disabled {{ color: {TEXT_DIM};
                      border-color: rgba(255, 255, 255, 14); }}

QComboBox, QFontComboBox {{
    background-color: rgba(0, 0, 0, 92);
    border: 1px solid rgba(255, 255, 255, 26);
    border-radius: {RADIUS_INPUT}px;
    padding: 7px 10px;
    color: {TEXT_BRIGHT};
}}
QComboBox:hover, QFontComboBox:hover {{ border-color: rgba(255, 255, 255, 46); }}
QComboBox::drop-down, QFontComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView, QFontComboBox QAbstractItemView {{
    background-color: {BG_CARD_ALT};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: {ON_ACCENT};
    padding: 4px;
    outline: none;
}}

/* Every list in the app, styled once.  They were each carrying their own
   inline sheet, which is how one of them came to be missing it entirely and
   sat in the middle of the glass as a square, opaque, system-grey box.  The
   treatment is the input treatment -- a well sunk into the sheet -- because
   that is what a list is: somewhere content is held, not a panel floating on
   top of one. */
QListWidget {{
    background-color: rgba(0, 0, 0, 92);
    border: 1px solid rgba(255, 255, 255, 26);
    border-radius: {RADIUS_INPUT}px;
    padding: {PAD_XS}px;
    color: {TEXT};
    outline: none;
}}
QListWidget:focus {{ border-color: {ACCENT}; }}
QListWidget:disabled {{ color: {TEXT_DIM};
                        border-color: rgba(255, 255, 255, 14); }}
QListWidget::item {{
    padding: 4px 6px;
    border-radius: {max(3, RADIUS_INPUT - 2)}px;
    color: {TEXT};
}}
QListWidget::item:hover {{ background-color: rgba(255, 255, 255, 14); }}
QListWidget::item:selected {{
    background-color: {rgba(tint(ACCENT, 62))};
    color: {TEXT_BRIGHT};
}}

QCheckBox {{ color: {TEXT}; spacing: 9px; padding: 2px 0; }}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border: 1px solid rgba(255, 255, 255, 34);
    border-radius: 4px;
    background-color: rgba(0, 0, 0, 92);
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    image: none;
}}

QSlider::groove:horizontal {{ height: 4px; border-radius: 2px;
                              background: rgba(255, 255, 255, 26); }}
QSlider::sub-page:horizontal {{ height: 4px; border-radius: 2px;
                                background: {ACCENT}; }}
QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -6px 0;
                              border-radius: 7px; background: {TEXT_BRIGHT}; }}

/* ---- Buttons ------------------------------------------------------- */
QPushButton {{
    background-color: rgba(255, 255, 255, 16);
    border: 1px solid rgba(255, 255, 255, 28);
    border-radius: {RADIUS_CONTROL}px;
    padding: 9px 18px;
    color: {TEXT};
}}
QPushButton:hover   {{ background-color: rgba(255, 255, 255, 30);
                       border-color: rgba(255, 255, 255, 52);
                       color: {TEXT_BRIGHT}; }}
QPushButton:pressed {{ background-color: rgba(0, 0, 0, 60); }}
QPushButton:disabled {{ color: {TEXT_DIM};
                        background-color: rgba(255, 255, 255, 6);
                        border-color: rgba(255, 255, 255, 12); }}

QPushButton[variant="accent"] {{
    background-color: {ACCENT}; border-color: {ACCENT_HOVER};
    color: {ON_ACCENT}; font-weight: 600;
}}
QPushButton[variant="accent"]:hover   {{ background-color: {ACCENT_HOVER};
                                         border-color: {ACCENT_HOVER}; }}
QPushButton[variant="accent"]:pressed {{ background-color: {ACCENT_PRESSED}; }}
QPushButton[variant="accent"]:disabled {{
    background-color: {rgba(hex_rgb(ACCENT) + (55,))};
    border-color: {rgba(hex_rgb(ACCENT) + (60,))};
    color: rgba(255, 220, 205, 90); }}

QPushButton[variant="danger"] {{
    background-color: {rgba(hex_rgb(DANGER) + (34,))};
    border: 1px solid {rgba(hex_rgb(DANGER_HOVER) + (130,))};
    color: #f9a8a8; font-weight: 600;
}}
QPushButton[variant="danger"]:hover   {{ background-color: {DANGER};
                                         border-color: {DANGER_HOVER};
                                         color: #ffffff; }}
QPushButton[variant="danger"]:pressed {{ background-color: #7f1d1d; }}
QPushButton[variant="danger"]:disabled {{
    background-color: {rgba(hex_rgb(DANGER) + (14,))};
    border-color: {rgba(hex_rgb(DANGER_HOVER) + (50,))};
    color: rgba(249, 168, 168, 90); }}

QPushButton[variant="ready"] {{
    background-color: {rgba(hex_rgb(WARN) + (26,))};
    border: 1px solid {rgba(hex_rgb(WARN) + (130,))};
    color: {WARN}; font-weight: 600;
}}
QPushButton[variant="ready"]:hover {{ background-color: {WARN};
                                      border-color: {WARN};
                                      color: #241a02; }}
QPushButton[variant="ready"]:disabled {{
    background-color: {rgba(hex_rgb(WARN) + (10,))};
    border-color: {rgba(hex_rgb(WARN) + (45,))};
    color: {rgba(hex_rgb(WARN) + (95,))}; }}

QPushButton[variant="quiet"] {{
    background-color: rgba(255, 255, 255, 8);
    border: 1px solid rgba(255, 255, 255, 18);
    color: {TEXT_MUTED};
}}
QPushButton[variant="quiet"]:hover {{ color: {TEXT_BRIGHT};
                                      background-color: rgba(255, 255, 255, 22);
                                      border-color: rgba(255, 255, 255, 40); }}

/* Buttons that live inside a dense row, where the default 18px side padding
   would eat the label rather than frame it.  The 7px top and bottom is not
   arbitrary: it lands the button on the same height as the QLineEdit beside it.
   They cannot simply share a padding, because the entry is set in the mono face
   and the button in the UI face, and the two have different metrics at the same
   point size -- the 2px trim is that difference.

   The attribute is "density" rather than the more natural "size" because
   ``size`` is already a QWidget property of type QSize: setProperty('size',
   'compact') is silently dropped as an untranslatable type, and the selector
   then matches nothing at all. */
QPushButton[density="compact"] {{ padding: 7px 8px; }}

#SafetyButton {{
    background-color: {rgba(hex_rgb(DANGER) + (38,))};
    border: 1px solid {rgba(hex_rgb(DANGER_HOVER) + (140,))};
    color: #f9a8a8; font-weight: 700; font-size: {pt(9)}pt;
    padding: 9px 18px; border-radius: {RADIUS_CONTROL}px;
}}
#SafetyButton:hover {{ background-color: {DANGER_HOVER}; color: #ffffff;
                       border-color: #f87171; }}
#SafetyButton:disabled {{ background-color: rgba(255, 255, 255, 6);
                          border-color: rgba(255, 255, 255, 14);
                          color: {TEXT_DIM}; }}

/* ---- Splitter, scroll --------------------------------------------- */
QSplitter::handle {{ background-color: rgba(255, 255, 255, 14);
                     margin: 12px 0; border-radius: 2px; }}
QSplitter::handle:horizontal {{ width: 4px; }}
QSplitter::handle:hover {{ background-color: {ACCENT}; }}

QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px;
                       margin: 2px 0 2px 0; }}
QScrollBar::handle:vertical {{ background: rgba(255, 255, 255, 34);
                               border-radius: 5px; min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: rgba(255, 255, 255, 62); }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---- Log view ------------------------------------------------------ */
QPlainTextEdit {{
    background-color: rgba(0, 0, 0, 92);
    border: 1px solid rgba(255, 255, 255, 20);
    border-radius: {RADIUS_CONTROL}px;
    padding: 7px 9px;
    color: {TEXT_MUTED};
    font-family: {FONT_MONO};
    font-size: {pt(8)}pt;
}}

/* ---- Status bar ---------------------------------------------------- */
/* All that is left down here is the one-off message; the standing fields
   moved up beside the title.  The bar hides itself when there is nothing to
   say, so an empty strip is never taking a line off the run. */
#StatusBar QLabel {{ color: {TEXT}; font-size: {pt(8)}pt;
                     font-family: {FONT_MONO}; }}

QToolTip {{
    background-color: {BG_CARD_ALT};
    color: {TEXT_BRIGHT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_INPUT}px;
    padding: 6px 9px;
}}
"""


# Build once at import so plain ``theme.ACCENT`` access works immediately.
CONFIG_ERROR = reload_from_disk()
