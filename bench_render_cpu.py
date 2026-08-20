"""Measure CPU burned by the graph panel's real render loop.

The per-frame timings in ``spike_qt_graph.py --bench`` force a synchronous
repaint. What the operator actually cares about is CPU burned per wall-clock
second with the panel running its normal loop, so that is what this measures.

Each run does two passes over the same window:

    baseline  event loop + 10 Hz acquisition, no rendering
    total     the same, plus the panel rendering at 5 Hz

The difference is the cost of drawing:

    python bench_render_cpu.py
"""

from __future__ import annotations

import math
import random
import sys
import time
from collections import deque

POINTS = 600
FEED_MS = 100        # acquisition rate, as in the app
RENDER_MS = 200      # render rate, as in v3
SECONDS = 8.0

SHAPES = (
    ("6 series / 3 axes", 3, ('flow', 'sp')),
    ("20 series / 5 axes", 5, ('flow', 'sp', 'press', 'temp')),
)
COLORS = ['#f25d38', '#4ade80', '#60a5fa', '#facc15', '#c084fc']


def sample(index, offset, step):
    return (10 + 4 * index + 3 * offset
            + 6 * math.sin(step / 25.0 + index + offset) + random.random())


def run_qt(unit_count, metrics, render):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMainWindow

    from flow_controller.ui.qt_graph_panel import GraphHistory, QtGraphPanel

    app = QApplication.instance() or QApplication(sys.argv)
    history = GraphHistory()
    history.times = deque(maxlen=POINTS)
    selection = []
    for index in range(unit_count):
        unit = index + 1
        history.set_unit_meta(unit, label=f'Unit {unit}',
                              color=COLORS[index % len(COLORS)])
        for metric in metrics:
            history.set_series(unit, metric, deque(maxlen=POINTS))
            selection.append((unit, metric))
    for step in range(POINTS):
        history.times.append(step * 0.3)
        for index in range(unit_count):
            for offset, metric in enumerate(metrics):
                history.values(index + 1, metric).append(
                    sample(index, offset, step))

    window = QMainWindow()
    window.resize(1100, 750)
    panel = QtGraphPanel(history, pen_width=1)
    window.setCentralWidget(panel)
    window.show()
    if render:
        panel.set_selection(selection)
    app.processEvents()

    state = {'step': POINTS}

    def feed():
        state['step'] += 1
        history.times.append(state['step'] * 0.3)
        for index in range(unit_count):
            for offset, metric in enumerate(metrics):
                history.values(index + 1, metric).append(
                    sample(index, offset, state['step']))

    feeder = QTimer()
    feeder.setInterval(FEED_MS)
    feeder.timeout.connect(feed)
    feeder.start()

    app.processEvents()
    start_cpu = time.process_time()
    start_wall = time.perf_counter()
    stop = QTimer()
    stop.setSingleShot(True)
    stop.timeout.connect(app.quit)
    stop.start(int(SECONDS * 1000))
    app.exec()
    cpu = time.process_time() - start_cpu
    wall = time.perf_counter() - start_wall
    window.close()
    app.processEvents()
    return cpu, wall


def main():
    print(f"Qt / pyqtgraph  --  {SECONDS:.0f}s window, "
          f"feed {1000 // FEED_MS} Hz, render {1000 // RENDER_MS} Hz\n")
    print(f"{'shape':<20} {'idle CPU':>10} {'render CPU':>11} "
          f"{'draw cost':>10} {'% of core':>10}")
    print('-' * 66)
    for label, unit_count, metrics in SHAPES:
        random.seed(1)
        base_cpu, _ = run_qt(unit_count, metrics, render=False)
        random.seed(1)
        total_cpu, wall = run_qt(unit_count, metrics, render=True)
        draw = total_cpu - base_cpu
        print(f"{label:<20} {base_cpu:>9.3f}s {total_cpu:>10.3f}s "
              f"{draw:>9.3f}s {total_cpu / wall * 100:>9.1f}%")


if __name__ == '__main__':
    main()
