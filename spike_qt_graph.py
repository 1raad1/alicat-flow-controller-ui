"""Step-2 spike runner: the Qt/pyqtgraph graph panel, standalone.

Two modes:

    python spike_qt_graph.py --demo    live window, look and feel + lazy behaviour
    python spike_qt_graph.py --bench   compare Qt rendering options

Nothing here is imported by the running application.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from collections import deque

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QTabWidget, QVBoxLayout, QWidget,
)

from flow_controller.ui.qt_graph_panel import (
    GRAPH_METRICS, GraphHistory, QtGraphPanel,
)

HISTORY_POINTS = 600
UNITS = [
    (1, 'NH3 Zone 1', '#f25d38'),
    (2, 'H2 Zone 1', '#4ade80'),
    (3, 'Air Zone 1', '#60a5fa'),
    (4, 'CH4 Pilot', '#facc15'),
    (5, 'NH3 Zone 2', '#c084fc'),
]


def build_history(unit_count, metrics, points=HISTORY_POINTS, prefill=True):
    """A GraphHistory with deques shaped like the app's own."""
    history = GraphHistory()
    history.times = deque(maxlen=points)
    for index in range(unit_count):
        unit, label, color = UNITS[index % len(UNITS)]
        history.set_unit_meta(unit, label=label, color=color)
        for metric in metrics:
            history.set_series(unit, metric, deque(maxlen=points))
    if prefill:
        for step in range(points):
            append_sample(history, unit_count, metrics, step)
    return history


def append_sample(history, unit_count, metrics, step):
    history.times.append(step * 0.3)
    for index in range(unit_count):
        unit = UNITS[index % len(UNITS)][0]
        for offset, metric in enumerate(metrics):
            base = 10 + 4 * index + 3 * offset
            value = base + 6 * math.sin(step / 25.0 + index + offset) \
                + random.random()
            history.values(unit, metric).append(value)


# ---------------------------------------------------------------------- #
#  Benchmark                                                              #
# ---------------------------------------------------------------------- #
def bench(app, unit_count, metrics, frames=60, **panel_options):
    history = build_history(unit_count, metrics)
    selection = [
        (UNITS[i % len(UNITS)][0], metric)
        for i in range(unit_count) for metric in metrics
    ]
    window = QMainWindow()
    window.resize(900, 600)
    panel = QtGraphPanel(history, **panel_options)
    window.setCentralWidget(panel)
    window.show()
    panel.set_selection(selection)
    app.processEvents()
    panel.render_frame()
    panel._update_limits(force=True)
    viewport = panel._graphics.viewport()
    viewport.repaint()
    app.processEvents()

    start = time.perf_counter()
    for _ in range(frames):
        panel.render_frame()
        # repaint() is synchronous, so the paint cost lands inside the timed
        # region.
        viewport.repaint()
        app.processEvents()
    elapsed = time.perf_counter() - start
    window.close()
    return elapsed / frames * 1000.0


VARIANTS = (
    ("naive port (width 2, finite-connect, downsample)",
     dict(pen_width=2, gap_aware=True, downsample=True, clip_to_view=True)),
    ("width 2 pens, adaptive gaps",
     dict(pen_width=2, gap_aware=True)),
    ("width 1 cosmetic pens, adaptive gaps",
     dict(pen_width=1, gap_aware=True)),
    ("width 1 + OpenGL",
     dict(pen_width=1, gap_aware=True, use_opengl=True)),
)


def run_bench():
    app = QApplication.instance() or QApplication(sys.argv)
    random.seed(1)
    print(f"points={HISTORY_POINTS}  frames=60\n")
    shapes = ((3, ('flow', 'sp')), (5, ('flow', 'sp', 'press', 'temp')))

    for unit_count, metrics in shapes:
        series = unit_count * len(metrics)
        print(f"=== {series} series ===")
        for label, options in VARIANTS:
            qt_ms = bench(app, unit_count, metrics, **options)
            print(f"  {label:<44} {qt_ms:>7.2f} ms/frame")
        print()


# ---------------------------------------------------------------------- #
#  Demo window                                                            #
# ---------------------------------------------------------------------- #
class SpikeWindow(QMainWindow):
    """Two tabs, so the lazy-activation rule is visible in Qt terms."""

    def __init__(self, unit_count=4, metrics=('flow', 'sp', 'press', 'temp')):
        super().__init__()
        self.setWindowTitle("Flow Controller v3 — Qt graph panel spike")
        self.resize(1280, 860)
        self.setStyleSheet("""
            QMainWindow, QWidget { background-color: #111110; color: #d8d2c3;
                                   font-family: 'Yu Gothic UI'; font-size: 10pt; }
            QGroupBox { border: 1px solid #3a3a34; border-radius: 5px;
                        margin-top: 10px; padding: 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px;
                               color: #efe9dc; }
            QTabWidget::pane { border: 1px solid #3a3a34; }
            QTabBar::tab { background: #1a1a17; color: #8a8a84;
                           padding: 9px 18px; margin-right: 6px;
                           border-top-left-radius: 3px;
                           border-top-right-radius: 3px; }
            QTabBar::tab:selected { background: #22221f; color: #efe9dc;
                                    border-bottom: 2px solid #f25d38; }
            QCheckBox { color: #d8d2c3; }
        """)
        self._unit_count = unit_count
        self._metrics = metrics
        self._step = HISTORY_POINTS
        self._history = build_history(unit_count, metrics)
        self._boxes = {}

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        idle = QWidget()
        idle_layout = QVBoxLayout(idle)
        idle_label = QLabel(
            "Operation & Monitoring (stand-in).\n\n"
            "History keeps accumulating while this tab is open.\n"
            "Switch to Logging & Graphs and tick a series to start rendering.")
        idle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        idle_layout.addWidget(idle_label)
        self.tabs.addTab(idle, "Operation & Monitoring")

        graphs = QWidget()
        graph_layout = QVBoxLayout(graphs)
        graph_layout.addWidget(self._build_controls())
        self.panel = QtGraphPanel(self._history)
        graph_layout.addWidget(self.panel, stretch=1)
        self.tabs.addTab(graphs, "Logging & Graphs")

        self._status = QLabel("No series selected")
        self._status.setStyleSheet("color: #8a8a84;")
        graph_layout.addWidget(self._status)

        # Acquisition is independent of rendering.
        self._feed = QTimer(self)
        self._feed.setInterval(100)
        self._feed.timeout.connect(self._append)
        self._feed.start()

    def _build_controls(self):
        box = QGroupBox("Series")
        grid = QGridLayout(box)
        for column, metric in enumerate(self._metrics, start=1):
            header = QLabel(GRAPH_METRICS[metric][0])
            header.setStyleSheet("color: #8a8a84;")
            grid.addWidget(header, 0, column)
        for row in range(self._unit_count):
            unit, label, color = UNITS[row % len(UNITS)]
            name = QLabel(label)
            name.setStyleSheet(f"color: {color};")
            grid.addWidget(name, row + 1, 0)
            for column, metric in enumerate(self._metrics, start=1):
                check = QCheckBox()
                check.setChecked(False)  # nothing plots until asked for
                check.stateChanged.connect(self._selection_changed)
                self._boxes[(unit, metric)] = check
                grid.addWidget(check, row + 1, column)
        return box

    def _selection_changed(self):
        selection = [key for key, box in self._boxes.items() if box.isChecked()]
        self.panel.set_selection(selection)
        self._status.setText(
            f"{len(selection)} series selected" if selection
            else "No series selected")

    def _append(self):
        append_sample(self._history, self._unit_count, self._metrics,
                      self._step)
        self._step += 1


def run_demo():
    app = QApplication.instance() or QApplication(sys.argv)
    random.seed(1)
    window = SpikeWindow()
    window.show()
    sys.exit(app.exec())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bench', action='store_true',
                        help="compare Qt panel rendering options")
    parser.add_argument('--demo', action='store_true',
                        help="open the live spike window (default)")
    args = parser.parse_args()
    if args.bench:
        run_bench()
    else:
        run_demo()


if __name__ == '__main__':
    main()
