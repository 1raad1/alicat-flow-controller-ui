"""User-editable UI configuration.

Everything the look of the app is built from — colours, corner radii, fonts,
spacing and the glass alphas — lives in one JSON file so it can be changed
without touching code.  The in-app Settings dialog reads and writes that same
file, so a hand-edit and a dialog edit cannot drift apart.

The file is optional and partial: anything missing falls back to ``DEFAULTS``,
so a config holding a single overridden colour is valid.  That matters for a
rig instrument — a malformed or half-written config must never be the reason
the control surface fails to start, so ``load`` degrades to defaults rather
than raising.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

# --------------------------------------------------------------------- #
#  Defaults                                                             #
#                                                                       #
#  This dict is the schema.  ``load`` merges the user's file over it key #
#  by key, and anything the file does not mention keeps the value here.  #
# --------------------------------------------------------------------- #
DEFAULTS = {
    'font': {
        # Split family from fallback so the settings dialog can drive the
        # family with a real font picker while the fallback chain — which is
        # CSS syntax, not a font name — stays hand-edited.
        # Two faces, chosen for two jobs.  The interface asks for Avenir: a
        # humanist face reads faster than a code face at label sizes, which is
        # most of what the chrome is.  The readings keep a monospace face,
        # because a column of setpoints that does not line up digit-under-digit
        # is a column you have to read rather than scan.  The families after
        # the first are tried in order, so an install without the first choice
        # lands on something of the same shape rather than on whatever Qt
        # decides is closest.
        'ui_family': 'Avenir',
        'ui_fallback': ("'Avenir Next', 'Nunito Sans', 'Century Gothic', "
                        "'Segoe UI Variable Text', 'Segoe UI', sans-serif"),
        'mono_family': 'JetBrains Mono',
        'mono_fallback': ("'Cascadia Mono', 'Cascadia Code', 'Consolas', "
                          "'DejaVu Sans Mono', monospace"),
        # Base UI point size.  Everything else scales from it, so this one
        # number moves the whole interface.
        'size': 10,
    },
    'radius': {
        'card': 13,
        'panel': 10,
        'tile': 8,
        'control': 7,     # buttons
        'input': 6,       # line edits, combos
    },
    'spacing': {
        'xs': 4, 'sm': 8, 'md': 12, 'lg': 16, 'xl': 22,
        'card_pad': 16,   # inside a card, all four edges
        'card_gap': 14,   # between stacked cards
        'field_gap': 10,  # between rows of controls inside a card
    },
    'colors': {
        'bg': '#0d0d0c',
        'bg_panel': '#181714',
        'bg_card': '#1c1c1a',
        'bg_card_alt': '#22221f',
        'border': '#3a3a34',
        'border_soft': '#292925',

        'text': '#ded8c9',
        'text_bright': '#f4eee1',
        'text_muted': '#96968f',
        'text_dim': '#63635c',

        'accent': '#f25d38',
        'accent_hover': '#ff6f4a',
        'accent_pressed': '#d94a28',
        'on_accent': '#1a0d08',      # text drawn on top of an accent fill

        'ok': '#4ade80',
        'info': '#60a5fa',
        'warn': '#fbbf24',
        'amber': '#e08a2b',
        'danger': '#b91c1c',
        'danger_hover': '#dc2626',
        'teal': '#4ecdc4',
        'phi_stage': '#fb923c',
        'phi_global': '#34d399',

        'gas': {
            'NH3': '#f25d38',
            'H2': '#4ade80',
            'Air': '#60a5fa',
            'CH4': '#facc15',
        },
    },
    'glass': {
        # Body tints.  The colour is the sheet, the alpha is how much of the
        # backdrop comes through it.  Alphas are 0-255.
        'tint': '#191815',        'tint_alpha': 148,   # top-level cards
        'tint_soft': '#21201c',   'tint_soft_alpha': 118,  # nested panels
        'tint_bar': '#12110e',    'tint_bar_alpha': 186,   # title/status bars
        'inset_alpha': 13,        # tiles: glass sitting on glass

        # The rim is what sells the effect — bright at the top-left, gone by
        # the bottom-right.  A flat border reads as a box.
        'rim': 22,
        'rim_high': 58,
        'rim_low': 8,
        'sheen': 15,              # light falling down the front face

        # Grain is what makes translucency read as *frosted* rather than
        # merely see-through.  0 disables it.
        'grain': 0.07,

        # The painted wallpaper the glass refracts.  Without it, translucency
        # is invisible.  Blobs are [x, y, extent, colour, alpha] with x/y/
        # extent as fractions of the window.
        'backdrop_base': ['#17160f', '#100f0e', '#08080a'],
        'backdrop_blobs': [
            [0.13, -0.08, 0.62, '#f25d38', 58],
            [0.97, 0.12, 0.56, '#3b7cc4', 54],
            [0.55, 1.08, 0.66, '#2f8d7e', 44],
            [-0.06, 0.80, 0.50, '#6b47c0', 40],
            [0.72, 0.44, 0.34, '#c04a7a', 22],
        ],
    },
}

ENV_VAR = 'FLOW_CONTROLLER_UI_CONFIG'
FILENAME = 'ui_theme.json'


def path():
    """Where the config lives.

    Next to the application by default, so it travels with the install and is
    findable by whoever is standing at the rig.  ``FLOW_CONTROLLER_UI_CONFIG``
    overrides it for testing or for a per-operator profile.
    """
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / FILENAME


def defaults():
    return copy.deepcopy(DEFAULTS)


def merge(base, override):
    """Deep-merge ``override`` onto a copy of ``base``.

    Only keys already present in ``base`` are taken, so a stale or misspelled
    key in the file is ignored rather than silently creating a setting that
    nothing reads.
    """
    result = copy.deepcopy(base)
    if not isinstance(override, dict):
        return result
    for key, value in override.items():
        if key not in result:
            continue
        if isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load(config_path=None):
    """Read the config, falling back to defaults for anything missing.

    Returns ``(config, error)``.  ``error`` is a human-readable string when the
    file existed but could not be used — the caller can surface it, but the
    config it gets back is always usable.
    """
    target = Path(config_path) if config_path else path()
    if not target.exists():
        return defaults(), None
    try:
        with target.open('r', encoding='utf-8') as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        return defaults(), f'{target.name}: {exc}'
    return merge(DEFAULTS, raw), None


def save(config, config_path=None):
    """Write the config back out.  Returns an error string, or None."""
    target = Path(config_path) if config_path else path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('w', encoding='utf-8') as handle:
            json.dump(config, handle, indent=2)
            handle.write('\n')
    except OSError as exc:
        return f'{target}: {exc}'
    return None
