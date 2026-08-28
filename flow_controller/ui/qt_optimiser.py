"""Operator-approved Bayesian experiments with live or manual NO/O2 measurements."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)
import pyqtgraph as pg

from ..core.session import DEFAULT_LOG_DIR
from ..domain.bayesian import SearchConfig, finite
from . import qt_theme as theme
from .qt_widgets import Card


SUFFIX = ".fcbo.json"


def note(text):
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setObjectName("Hint")
    return widget


class ExperimentDialog(QDialog):
    def __init__(self, request=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Bayesian experiment")
        self.resize(theme.scale(540), theme.scale(550))
        layout = QVBoxLayout(self)
        layout.addWidget(note(
            "Pilot-off NH3/H2, rich stage 1 and lean overall. Enter a rig-approved search "
            "region; these bounds do not establish flame stability or safe transitions."))
        form = QFormLayout()
        self.entries = {}
        for key, title, value in (
            ("power", "Fixed thermal input (kW)", getattr(request, "power_kw", 10)),
            ("split", "Fixed stage-1 fuel split (%)", 100 * getattr(request, "split_rich", 1)),
            ("reference", "Reporting reference O2 (dry vol%)", 15),
            ("initial", "Initial space-filling points", 16),
            ("window", "Minimum averaging window (s)", 30),
        ):
            entry = QLineEdit(f"{value:g}")
            entry.setAccessibleName(title)
            self.entries[key] = entry
            form.addRow(title, entry)
        layout.addLayout(form)
        grid = QGridLayout()
        for column, title in enumerate(("Variable", "Lower", "Upper")):
            grid.addWidget(QLabel(title), 0, column)
        self.bounds = []
        for row, title in enumerate(("H2 in fuel (volume %)", "Stage-1 phi", "Overall phi"), 1):
            grid.addWidget(QLabel(title), row, 0)
            pair = []
            for column in (1, 2):
                entry = QLineEdit()
                entry.setPlaceholderText("Required")
                entry.setAccessibleName(f"{title} {'lower' if column == 1 else 'upper'}")
                grid.addWidget(entry, row, column)
                pair.append(entry)
            self.bounds.append(pair)
        layout.addLayout(grid)
        layout.addWidget(note(
            "Objective: dry NO × (20.9 − reference O2) / (20.9 − measured O2). "
            "Use uncorrected dry analyser readings. NO is not total NOx; NH3 slip, "
            "N2O and combustion efficiency are not measured or constrained here."))
        self.approved = QCheckBox("Search region and transition procedure checked\n"
                                  "Raw dry NO/O2 reporting basis verified")
        layout.addWidget(self.approved)
        self.error = note("")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.config = None

    def accept(self):
        try:
            if not self.approved.isChecked():
                raise ValueError("Check the search region and reporting basis before continuing.")
            bounds = [[finite(entry.text(), "Bound") for entry in pair] for pair in self.bounds]
            bounds[0] = [value / 100 for value in bounds[0]]
            initial = finite(self.entries["initial"].text(), "Initial points")
            if not initial.is_integer():
                raise ValueError("Initial points must be a whole number.")
            self.config = SearchConfig(
                power_kw=self.entries["power"].text(), bounds=bounds,
                split_rich=finite(self.entries["split"].text(), "Fuel split") / 100,
                reference_o2=self.entries["reference"].text(),
                initial_points=int(initial), window_seconds=self.entries["window"].text())
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        super().accept()


class OptimiserPane(Card):
    def __init__(self, controller, parent=None):
        super().__init__("Bayesian optimiser", collapsed=not controller.expanded,
                         help_text="Live/manual NO/O2 experiments. Suggestions never actuate the rig. "
                         "A noisy Matérn Gaussian process proposes one test at a time after the initial design.",
                         parent=parent)
        self.controller = controller
        self._trial_id = None
        self.toggled.connect(self._expanded)
        self.add(note("MEXA-584L  ·  live or manual NO + O2  ·  pilot off"))
        actions = QHBoxLayout()
        self.new_button = self._button("New experiment", self._new, actions)
        self.open_button = self._button("Open…", self._open, actions)
        self.add_layout(actions)
        self.summary = note("")
        self.add(self.summary)
        self.tabs = QTabWidget()
        self.add(self.tabs)
        self._build_test()
        self._build_history()
        self.status = note(controller.last_message)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.add(self.status)
        controller.changed.connect(self.refresh)
        controller.message.connect(self.status.setText)
        controller.progress.connect(self.status.setText)
        self.refresh()

    def _expanded(self, expanded):
        self.controller.expanded = bool(expanded)

    def _button(self, title, callback, layout):
        button = QPushButton(title)
        button.clicked.connect(lambda _checked=False: self._run(callback))
        layout.addWidget(button)
        return button

    def _run(self, callback):
        try:
            callback()
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))

    def _build_test(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.candidate = note("No pending test.")
        self.candidate.setObjectName("")
        self.candidate.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.candidate)
        line = QHBoxLayout()
        self.ask_button = self._button("Suggest next test", self.controller.ask, line)
        self.load_button = self._button("Load target fields", self.controller.prepare_targets, line)
        self.load_button.setToolTip("Populate the existing controller fields. Does not send commands.")
        layout.addLayout(line)
        layout.addWidget(note("Review all fields, then apply through the existing controls. "
                              "Switch off the pilot using your established procedure."))
        self.pilot = QCheckBox("Pilot is off throughout this measurement")
        self.settled = QCheckBox("Burner and analyser settled\n(including sample-line delay)")
        layout.addWidget(self.pilot)
        layout.addWidget(self.settled)
        self.live = QCheckBox("Capture NO/O2 automatically from the MEXA network link")
        self.live.setChecked(self.controller.live_mode)
        self.live.toggled.connect(lambda checked: setattr(self.controller, "live_mode", checked))
        layout.addWidget(self.live)
        layout.addWidget(note("Connect in the MEXA analyser tab first. Live capture requires validated, "
                              "uncorrected dry readings, with no gaps or alarms. Simulation is excluded."))
        line = QHBoxLayout()
        self.start_button = self._button("Start window", self._start, line)
        self.finish_button = self._button("Finish window", self.controller.finish_window, line)
        self.cancel_button = self._button("Discard window", self.controller.cancel_window, line)
        layout.addLayout(line)
        self.window_label = note("No measurement window saved.")
        layout.addWidget(self.window_label)
        form = QFormLayout()
        self.inputs = {}
        for key, title, placeholder in (
            ("no", "Raw dry NO (ppm)", "Window average, 0–5000"),
            ("o2", "Dry O2 (vol%)", "Same window, below 20.9"),
            ("sem", "NO standard error (ppm)", "Optional; blank = unknown"),
            ("notes", "Notes", "Calibration, observations, limitations"),
        ):
            entry = QLineEdit(str(self.controller.draft.get(key, "")))
            entry.setPlaceholderText(placeholder)
            entry.setAccessibleName(title)
            entry.textChanged.connect(lambda value, name=key: self.controller.draft.update({name: value}))
            form.addRow(title, entry)
            self.inputs[key] = entry
        layout.addLayout(form)
        self.basis = QCheckBox("Uncorrected dry averages from this saved window")
        layout.addWidget(self.basis)
        layout.addWidget(note("Unknown noise is fitted by the model. Optional SEM covers NO only; "
                              "O2 uncertainty and calibration bias are not propagated. Live samples "
                              "may be autocorrelated, so their SD is recorded but not treated as SEM."))
        line = QHBoxLayout()
        self.save_button = self._button("Save result", self._save, line)
        self.invalid_button = self._button("Mark test invalid…", self._invalid, line)
        layout.addLayout(line)
        layout.addStretch(1)
        self.tabs.addTab(page, "Current test")

    def _build_history(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.best_label = note("No completed tests.")
        layout.addWidget(self.best_label)
        self.history = QListWidget()
        self.history.setFixedHeight(theme.scale(130))
        self.history.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.history.currentItemChanged.connect(self._selection)
        layout.addWidget(self.history)
        self.plot = pg.PlotWidget(background=theme.BG)
        self.plot.setFixedHeight(theme.scale(180))
        self.plot.setLabel("left", "Corrected dry NO", units="ppm")
        self.plot.setLabel("bottom", "Test number")
        self.plot.showGrid(x=True, y=True, alpha=.15)
        layout.addWidget(self.plot)
        line = QHBoxLayout()
        self.repeat_button = self._button("Repeat selected", self._repeat, line)
        self.export_button = self._button("Export CSV…", self._export, line)
        layout.addLayout(line)
        layout.addWidget(note("Lowest observed value is provisional. Repeat promising points and "
                              "reference conditions; this does not prove a global minimum."))
        layout.addStretch(1)
        self.tabs.addTab(page, "History")

    def _switch_ok(self):
        experiment = self.controller.experiment
        if experiment and experiment.pending:
            return QMessageBox.question(
                self, "Switch experiment?", "The pending test is saved in its experiment file. "
                "Unsaved measurement text will be cleared. Continue?") == QMessageBox.StandardButton.Yes
        return True

    def _new(self):
        if not self._switch_ok():
            return
        dialog = ExperimentDialog(self.controller.session.autocalc_request, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Create experiment", str(DEFAULT_LOG_DIR / "experiments" / ("ammonia-no" + SUFFIX)),
            "Bayesian experiment (*.fcbo.json)")
        if path:
            if not path.lower().endswith(SUFFIX):
                path += SUFFIX
            self.controller.create(path, dialog.config)

    def _open(self):
        if not self._switch_ok():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open experiment",
                                            str(DEFAULT_LOG_DIR / "experiments"),
                                            "Bayesian experiment (*.fcbo.json)")
        if path:
            self.controller.load(path)

    def _start(self):
        self.controller.start_window(self.pilot.isChecked(), self.settled.isChecked(), live=self.live.isChecked())

    def _save(self):
        pending = self.controller.experiment.pending if self.controller.experiment else None
        if pending and (pending.get("window") or {}).get("mexa"):
            self.controller.complete_from_mexa(self.inputs["notes"].text(), self.basis.isChecked())
            return
        sem = self.inputs["sem"].text().strip()
        self.controller.complete(self.inputs["no"].text(), self.inputs["o2"].text(),
                                 sem or None, self.inputs["notes"].text(), self.basis.isChecked())

    def _invalid(self):
        reason, ok = QInputDialog.getText(self, "Mark test invalid", "Reason (excluded from the model):")
        if ok:
            self.controller.invalidate(reason)

    def _repeat(self):
        item = self.history.currentItem()
        if item:
            self.controller.repeat(item.data(Qt.ItemDataRole.UserRole))
            self.tabs.setCurrentIndex(0)

    def _export(self):
        experiment = self.controller.experiment
        if experiment is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export experiment results",
                                            str(experiment.path.with_suffix(".csv")), "CSV (*.csv)")
        if path:
            experiment.export_csv(path)
            self.status.setText(f"CSV exported to {path}")

    def _selection(self, *_args):
        experiment = self.controller.experiment
        item = self.history.currentItem()
        trial = next((t for t in experiment.trials if item and t["id"] == item.data(Qt.ItemDataRole.UserRole)),
                     None) if experiment else None
        self.repeat_button.setEnabled(bool(trial and trial["status"] == "completed"
                                           and not experiment.pending and not self.controller.busy))

    def refresh(self):
        c = self.controller
        experiment = c.experiment
        pending = experiment.pending if experiment else None
        trial_id = pending["id"] if pending else None
        if trial_id != self._trial_id:
            self._trial_id = trial_id
            for key, entry in self.inputs.items():
                entry.setText(str(c.draft.get(key, "")))
            for box in (self.pilot, self.settled, self.basis):
                box.setChecked(False)
        busy = c.busy
        capturing = c.capture is not None
        window = pending.get("window") if pending else None
        mexa = (window or {}).get("mexa")
        self.live.setEnabled(bool(pending and not capturing and not window and not busy))
        self.pilot.setEnabled(not capturing and not window)
        self.settled.setEnabled(not capturing and not window)
        self.new_button.setEnabled(not busy and not capturing)
        self.open_button.setEnabled(not busy and not capturing)
        self.ask_button.setEnabled(bool(experiment and not pending and not busy))
        self.load_button.setEnabled(bool(pending and not busy and not capturing and not window))
        self.start_button.setEnabled(bool(pending and not busy and not capturing and not window))
        self.finish_button.setEnabled(capturing)
        self.cancel_button.setEnabled(capturing)
        self.save_button.setEnabled(bool(window and not capturing and not busy))
        self.invalid_button.setEnabled(bool(pending and not capturing and not busy))
        self.export_button.setEnabled(bool(experiment and not busy))
        for entry in self.inputs.values():
            entry.setEnabled(bool(pending))
        for key in ("no", "o2", "sem"):
            self.inputs[key].setReadOnly(bool(mexa))
        if mexa:
            self.inputs["no"].setText(f"{mexa['no_ppm']:.8g}")
            self.inputs["o2"].setText(f"{mexa['o2_percent']:.8g}")
            self.inputs["sem"].setText("")
        self.summary.setText("Create a campaign with fixed power, fuel split and O2 reference."
                             if not experiment else
                             f"{experiment.path.name}\n{experiment.config.power_kw:g} kW · "
                             f"{experiment.config.split_rich * 100:g}% fuel in stage 1 · "
                             f"NO @ {experiment.config.reference_o2:g}% O2")
        if experiment:
            self.summary.setToolTip(str(experiment.path) + "\nBounds: " + str(experiment.config.bounds))
        self.candidate.setText("No pending test." if not pending else
                               f"Test {pending['number']} · {pending['method']}\n"
                               f"H2 {pending['point'][0] * 100:.4g}% · "
                               f"φ1 {pending['point'][1]:.5g} · φ overall {pending['point'][2]:.5g}")
        self.window_label.setText(
            f"Saved window: {window['duration_s']:.1f} s, {window['samples']} passes\n"
            f"{window['start']} → {window['end']}"
            + (f"\nMEXA: {mexa['samples']} samples · NO SD {mexa['no_sd']:.3g} ppm · "
               f"O2 SD {mexa['o2_sd']:.3g}%" if mexa else "") if window else
            ("Window capture in progress." if capturing else "No measurement window saved."))
        selected = self.history.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self.history.clear()
        completed = []
        for t in experiment.trials if experiment else []:
            outcome = f"{t['result']['corrected_no']:.3f} ppm" if t["status"] == "completed" else t["status"]
            item = QListWidgetItem(f"#{t['number']}  {outcome}  ·  H2 {t['point'][0] * 100:.3g}%  "
                                   f"φ1 {t['point'][1]:.4g}  φ {t['point'][2]:.4g}")
            item.setData(Qt.ItemDataRole.UserRole, t["id"])
            item.setToolTip(str(t))
            self.history.addItem(item)
            if t["id"] == selected_id:
                self.history.setCurrentItem(item)
            if t["status"] == "completed":
                completed.append(t)
        self.plot.clear()
        if completed:
            best = min(completed, key=lambda t: t["result"]["corrected_no"])
            self.best_label.setText(f"Lowest observed: {best['result']['corrected_no']:.3f} ppm "
                                   f"at {experiment.config.reference_o2:g}% O2 · test {best['number']}")
            self.plot.plot([t["number"] for t in completed], [t["result"]["corrected_no"] for t in completed],
                           pen=None, symbol="o", symbolSize=7, symbolBrush=theme.TEXT_BRIGHT)
        else:
            self.best_label.setText("No completed tests.")
        self._selection()
