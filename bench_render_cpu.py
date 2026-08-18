"""Measure CPU actually burned by each graph panel's real render loop.

The per-frame timings in ``spike_qt_graph.py --bench`` force a synchronous
repaint, which flatters neither toolkit fairly: matplotlib blits over a cached
background while Qt repaints the whole scene.  What the operator actually cares
about is CPU burned per wall-clock second with the panel running its normal
loop, so that is what this measures.

Each run does two passes over the same window:

    baseline  event loop + 10 Hz acquisition, no rendering
    total     the same, plus the panel rendering at 5 Hz

The difference is the cost of drawing.  Imports are lazy so each toolkit can be
run under whichever interpreter has it installed:

    python              bench_render_cpu.py mpl
    <qt-venv>/python    bench_render_cpu.py qt
"""

from __future__ import annotations

import argparse
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


# ---------------------------------------------------------------------- #
#  Qt / pyqtgraph                                                         #
# ---------------------------------------------------------------------- #
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


# ---------------------------------------------------------------------- #
#  Tk / matplotlib                                                        #
# ---------------------------------------------------------------------- #
def run_mpl(unit_count, metrics, render):
    import tkinter as tk

    import matplotlib
    matplotlib.use('TkAgg')
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

    from flow_controller.domain.graphing import padded_limits, should_rescale

    groups = {}
    for metric in metrics:
        groups.setdefault(
            {'flow': 'flow', 'sp': 'flow', 'press': 'pressure',
             'temp': 'temperature'}[metric], []).append(metric)

    times = deque(maxlen=POINTS)
    series = {}
    for index in range(unit_count):
        for metric in metrics:
            series[(index, metric)] = deque(maxlen=POINTS)
    for step in range(POINTS):
        times.append(step * 0.3)
        for index in range(unit_count):
            for offset, metric in enumerate(metrics):
                series[(index, metric)].append(sample(index, offset, step))

    root = tk.Tk()
    root.geometry('1100x750')
    figure, axes = plt.subplots(len(groups), 1, figsize=(11, 7.5),
                                facecolor='#111110', sharex=True)
    if len(groups) == 1:
        axes = [axes]
    lines = {}
    for axis, (group, group_metrics) in zip(axes, groups.items()):
        axis.set_facecolor('#181714')
        for index in range(unit_count):
            for metric in group_metrics:
                (line,) = axis.plot(
                    [], [], color=COLORS[index % len(COLORS)],
                    linestyle='--' if metric == 'sp' else '-',
                    linewidth=1.45, label=f'u{index} {metric}')
                line.set_animated(True)
                lines[(index, metric)] = (group, line)
        axis.grid(True, color='#292925', linewidth=0.55, alpha=0.9)
        axis.legend(loc='upper left', fontsize=7, ncol=2)
    figure.tight_layout()
    canvas = FigureCanvasTkAgg(figure, master=root)
    canvas.get_tk_widget().pack(fill='both', expand=True)
    canvas.draw()
    background = canvas.copy_from_bbox(figure.bbox)
    root.update()

    state = {'step': POINTS, 'frame': 0, 'bg': background, 'running': True,
             'pending': set()}

    def reschedule(delay, callback):
        state['pending'].add(root.after(delay, callback))

    def feed():
        if not state['running']:
            return
        state['step'] += 1
        times.append(state['step'] * 0.3)
        for index in range(unit_count):
            for offset, metric in enumerate(metrics):
                series[(index, metric)].append(
                    sample(index, offset, state['step']))
        reschedule(FEED_MS, feed)

    def draw():
        if not state['running']:
            return
        t = np.fromiter(times, dtype=float, count=len(times))
        for key, (_group, line) in lines.items():
            values = series[key]
            y = np.fromiter(values, dtype=float, count=len(values))
            count = min(len(y), len(t))
            line.set_data(t[-count:], y[-count:])
        state['frame'] += 1
        rescaled = False
        if state['frame'] % 5 == 0:
            for axis in axes:
                low = high = None
                for line in axis.get_lines():
                    y = line.get_ydata()
                    if len(y) == 0:
                        continue
                    finite = y[np.isfinite(y)]
                    if finite.size == 0:
                        continue
                    lo, hi = float(finite.min()), float(finite.max())
                    low = lo if low is None else min(low, lo)
                    high = hi if high is None else max(high, hi)
                if low is not None and should_rescale(axis.get_ylim(), low, high):
                    axis.set_ylim(*padded_limits(low, high))
                    rescaled = True
                if should_rescale(axis.get_xlim(), t[0], t[-1]):
                    axis.set_xlim(*padded_limits(t[0], t[-1], pad=0.02))
                    rescaled = True
        if rescaled or state['bg'] is None:
            canvas.draw()
            state['bg'] = canvas.copy_from_bbox(figure.bbox)
        canvas.restore_region(state['bg'])
        for _group, line in lines.values():
            line.axes.draw_artist(line)
        canvas.blit(figure.bbox)
        reschedule(RENDER_MS, draw)

    reschedule(FEED_MS, feed)
    if render:
        reschedule(0, draw)
    root.update()

    start_cpu = time.process_time()
    start_wall = time.perf_counter()
    root.after(int(SECONDS * 1000), root.quit)
    root.mainloop()
    cpu = time.process_time() - start_cpu
    wall = time.perf_counter() - start_wall
    # Drop queued callbacks before teardown, or Tcl invokes them against
    # already-deleted commands while the interpreter is shutting down.
    state['running'] = False
    for handle in state['pending']:
        try:
            root.after_cancel(handle)
        except tk.TclError:
            pass
    plt.close(figure)
    root.destroy()
    return cpu, wall


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('toolkit', choices=('qt', 'mpl'))
    args = parser.parse_args()
    runner = run_qt if args.toolkit == 'qt' else run_mpl
    name = 'Qt / pyqtgraph' if args.toolkit == 'qt' else 'Tk / matplotlib'

    print(f"{name}  --  {SECONDS:.0f}s window, "
          f"feed {1000 // FEED_MS} Hz, render {1000 // RENDER_MS} Hz\n")
    print(f"{'shape':<20} {'idle CPU':>10} {'render CPU':>11} "
          f"{'draw cost':>10} {'% of core':>10}")
    print('-' * 66)
    for label, unit_count, metrics in SHAPES:
        random.seed(1)
        base_cpu, _ = runner(unit_count, metrics, render=False)
        random.seed(1)
        total_cpu, wall = runner(unit_count, metrics, render=True)
        draw = total_cpu - base_cpu
        print(f"{label:<20} {base_cpu:>9.3f}s {total_cpu:>10.3f}s "
              f"{draw:>9.3f}s {total_cpu / wall * 100:>9.1f}%")


if __name__ == '__main__':
    main()
